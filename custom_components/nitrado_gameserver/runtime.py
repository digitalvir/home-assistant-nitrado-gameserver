"""Pure runtime helpers for managed Nitrado services."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from dataclasses import replace as dataclass_replace

from .api.nitrado import NitradoService, ParsedServer
from .const import RUNNING_STATUS, STOPPED_STATUS
from .models import ManagedServiceState
from .plugins.base import (
    SUPPORTED,
    CapabilityState,
    CapabilityVerdict,
    ControlContext,
    GameProfile,
    ProfileExtensionManifest,
    async_invoke_profile,
    blocked,
    profile_extension_manifest,
    valid_capability_verdict,
)
from .plugins.generic import GenericProfile
from .plugins.registry import (
    async_select_profile_candidate,
    profile_registration_generation,
    profile_registry_generation,
    select_profile_candidate,
)
from .profile_logging import log_profile_failure

_LOGGER = logging.getLogger(__name__)


class ProfileOperationLock:
    """Async profile lease that records its owning task for reentrancy checks."""

    __slots__ = ("_lock", "_owner")

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task[object] | None = None

    async def __aenter__(self) -> ProfileOperationLock:
        await self._lock.acquire()
        self._owner = asyncio.current_task()
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._owner = None
        self._lock.release()

    def owned_by(self, task: asyncio.Task[object] | None) -> bool:
        """Return whether ``task`` currently executes under this lease."""

        return task is not None and self._owner is task

    def locked(self) -> bool:
        """Return whether any task currently owns or waits behind the lease."""

        return self._lock.locked()


@dataclass(slots=True)
class ServiceRuntime:
    """Runtime state for one managed Nitrado service."""

    state: ManagedServiceState
    service: NitradoService | None = None
    server: ParsedServer | None = None
    profile: GameProfile | None = None
    profile_manifest: ProfileExtensionManifest | None = None
    profile_generation: int = 0
    profile_registration_generation: int = 0
    profile_registry_generation_seen: int = field(default_factory=profile_registry_generation)
    profile_operation_lock: ProfileOperationLock = field(default_factory=ProfileOperationLock)
    active_profile_stream_tasks: set[asyncio.Task[object]] = field(default_factory=set)
    status_fresh: bool = False
    using_cached_data: bool = False
    last_start_verdict: CapabilityVerdict | None = None
    last_stop_verdict: CapabilityVerdict | None = None
    last_idle_shutdown_verdict: CapabilityVerdict | None = None
    last_status: str | None = None
    last_transition_at: int | None = None
    stopped_since: int | None = None
    started_since: int | None = None
    refreshed_at: int | None = None
    idle_started_at: int | None = None
    startup_cooldown_started_at: int | None = None
    shutdown_pending: bool = False
    shutdown_generation: int = 0
    last_shutdown_reason: str = "Never"
    last_reset_reason: str = "Integration started; idle timer reset"
    last_start_time: int | None = None
    last_stop_time: int | None = None
    last_lifecycle_hook_results: dict[str, tuple[dict, ...]] = field(default_factory=dict)
    last_lifecycle_dispatch_errors: dict[str, str] = field(default_factory=dict)
    last_profile_entity_errors: dict[str, dict] = field(default_factory=dict)
    last_profile_refresh_error: str | None = None
    extra: dict = field(default_factory=dict)

    def profile_extra(self) -> dict:
        """Return profile-scoped mutable scratch storage."""

        profile_id = getattr(self.profile, "profile_id", None) or "generic"
        storage = self.extra.setdefault("_profile_extra", {})
        if not isinstance(storage, dict):
            storage = {}
            self.extra["_profile_extra"] = storage
        profile_storage = storage.setdefault(profile_id, {})
        if not isinstance(profile_storage, dict):
            profile_storage = {}
            storage[profile_id] = profile_storage
        return profile_storage

    def update_service(
        self,
        service: NitradoService,
        server: ParsedServer | None = None,
        *,
        select_profile_now: bool = True,
    ) -> None:
        """Update service metadata and select a profile."""

        self.service = service
        if select_profile_now:
            self._select_profile(service, server or self.server)

    def update_server(
        self,
        server: ParsedServer,
        *,
        observed_at: int,
        status_fresh: bool = True,
        using_cached_data: bool = False,
        select_profile_now: bool = True,
    ) -> None:
        """Update server status and lifecycle timestamps."""

        previous = self.last_status
        status = server.raw_status
        self.server = server
        self.status_fresh = status_fresh
        self.using_cached_data = using_cached_data
        self.refreshed_at = observed_at

        if previous != status:
            if previous is not None:
                self.last_transition_at = observed_at
                if status == RUNNING_STATUS:
                    self.last_start_time = observed_at
                if status == STOPPED_STATUS:
                    self.last_stop_time = observed_at
            if status == STOPPED_STATUS:
                self.stopped_since = observed_at
                self.startup_cooldown_started_at = None
            elif previous == STOPPED_STATUS:
                self.stopped_since = None
            if status == RUNNING_STATUS:
                self.started_since = observed_at
                self.startup_cooldown_started_at = observed_at
            elif previous == RUNNING_STATUS:
                self.started_since = None

        self.last_status = status
        if self.service and select_profile_now:
            self._select_profile(self.service, server)

    async def async_select_profile(self, service: NitradoService, server: ParsedServer | None) -> None:
        """Select and apply a profile without blocking Home Assistant's loop."""

        async with self.profile_operation_lock:
            while True:
                registry_generation = profile_registry_generation()
                candidate = await async_select_profile_candidate(service, server)
                selected = candidate.profile
                manifest = candidate.manifest
                selected_id = getattr(selected, "profile_id", None)
                registration_generation = profile_registration_generation(selected_id)
                if not self._profile_replacement_required(selected_id, registration_generation):
                    self.profile_registry_generation_seen = registry_generation
                    return
                if registry_generation != profile_registry_generation():
                    continue
                if registration_generation != profile_registration_generation(selected_id):
                    continue
                if self._profile_replacement_required(selected_id, registration_generation):
                    await self.async_revoke_profile_streams()
                self._apply_selected_profile(
                    selected,
                    manifest=manifest,
                    registration_generation=registration_generation,
                    registry_generation=registry_generation,
                )
                return

    async def async_revoke_profile_streams(self) -> None:
        """Cancel and drain streams before replacing their profile implementation."""

        current = asyncio.current_task()
        pending = tuple(task for task in self.active_profile_stream_tasks if task is not current and not task.done())
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def _select_profile(self, service: NitradoService, server: ParsedServer | None) -> None:
        """Select a profile while retaining the current same-ID instance."""

        candidate = select_profile_candidate(service, server)
        self._apply_selected_profile(candidate.profile, manifest=candidate.manifest)

    def _apply_selected_profile(
        self,
        selected: GameProfile,
        *,
        manifest: ProfileExtensionManifest | None = None,
        registration_generation: int | None = None,
        registry_generation: int | None = None,
    ) -> None:
        """Apply one already-selected profile while preserving same-ID state."""

        selected_id = getattr(selected, "profile_id", None)
        if registration_generation is None:
            registration_generation = profile_registration_generation(selected_id)
        if registry_generation is None:
            registry_generation = profile_registry_generation()
        if self._profile_replacement_required(selected_id, registration_generation):
            manifest = manifest or profile_extension_manifest(selected)
            self.profile = selected
            self.profile_manifest = manifest
            self.profile_generation += 1
            self.profile_registration_generation = registration_generation
            self.extra.pop("profile_status", None)
            self.extra.pop("_profile_resource_cache", None)
            self.last_start_verdict = blocked("The selected game profile changed; fresh status is required.")
            self.last_stop_verdict = blocked("The selected game profile changed; fresh status is required.")
            self.last_idle_shutdown_verdict = blocked(
                "The selected game profile changed; fresh player status is required."
            )
            self.status_fresh = False
            self.using_cached_data = True
            if self.server is not None:
                self.server = dataclass_replace(
                    self.server,
                    player_count=None,
                    player_names=(),
                    query_valid=False,
                    player_source=None,
                )
        self.profile_registry_generation_seen = registry_generation
        self.state.profile_id = selected_id

    def _profile_replacement_required(self, selected_id: str | None, registration_generation: int) -> bool:
        """Return whether profile identity/code generation changed."""

        return (
            self.profile is None
            or getattr(self.profile, "profile_id", None) != selected_id
            or self.profile_registration_generation != registration_generation
        )

    def stopped_for(self, now: int) -> int | None:
        """Return seconds observed stopped, if currently stopped."""

        if self.last_status != STOPPED_STATUS or self.stopped_since is None:
            return None
        return max(0, now - self.stopped_since)

    def last_transition_age(self, now: int) -> int | None:
        """Return seconds since last transition observation."""

        if self.last_transition_at is None:
            return None
        return max(0, now - self.last_transition_at)

    def idle_minutes(self, now: int) -> float:
        """Return minutes spent in the current idle countdown."""

        if self.idle_started_at is None:
            return 0.0
        return max(0.0, (now - self.idle_started_at) / 60)

    def idle_remaining_minutes(self, now: int, idle_minutes: float) -> float:
        """Return minutes remaining before automatic idle shutdown."""

        if self.idle_started_at is None:
            return 0.0
        return max(0.0, float(idle_minutes) - self.idle_minutes(now))

    def in_startup_cooldown(self, now: int, cooldown_minutes: float) -> bool:
        """Return whether startup cooldown is still suppressing idle shutdown."""

        if self.startup_cooldown_started_at is None:
            return False
        return now - self.startup_cooldown_started_at < float(cooldown_minutes) * 60

    def reset_idle_timer(self, reason: str) -> None:
        """Reset idle countdown state with a human-facing reason."""

        self.idle_started_at = None
        self.shutdown_pending = False
        self.last_reset_reason = reason


def runtime_profile_extension_manifest(runtime: ServiceRuntime) -> ProfileExtensionManifest:
    """Return the immutable manifest snapshot owned by a service runtime."""

    profile = runtime.profile
    if profile is None:
        raise ValueError("No game profile is selected")
    manifest = getattr(runtime, "profile_manifest", None)
    if manifest is None or manifest.profile_id != getattr(profile, "profile_id", None):
        # Compatibility path for pure callers that construct runtimes by hand.
        # Managed HA runtimes populate this snapshot off the event loop during
        # async profile selection and never recompute it during authorization
        # or dispatch.
        manifest = profile_extension_manifest(profile)
        runtime.profile_manifest = manifest
    return manifest


async def evaluate_start(
    runtime: ServiceRuntime, *, now: int, settle_seconds: int, force: bool = False
) -> CapabilityVerdict:
    """Return whether Start should be sent."""

    hard = _hard_status_block(runtime)
    if hard:
        return hard
    context = _context(runtime, force=force)
    if not force:
        core = _core_safe_start(runtime, now=now, settle_seconds=settle_seconds)
        if not core.allowed:
            return core

    profile = runtime.profile or GenericProfile()
    try:
        verdict = await async_invoke_profile(profile.can_start, context)
    except Exception as err:  # noqa: BLE001 - isolate arbitrary profile verdict failures.
        log_profile_failure(_LOGGER, "can_start", err, profile_id=getattr(profile, "profile_id", "unknown"))
        return blocked("The selected game profile failed while checking Start; details were logged.")
    if not valid_capability_verdict(verdict):
        return blocked("The selected game profile returned an invalid Start verdict.")
    if force and verdict.state == CapabilityState.BLOCKED and verdict.overridable:
        return SUPPORTED
    return verdict


async def evaluate_stop(runtime: ServiceRuntime, *, force: bool = False) -> CapabilityVerdict:
    """Return whether Stop should be sent."""

    hard = _hard_status_block(runtime)
    if hard:
        return hard
    context = _context(runtime, force=force)
    if not force:
        core = _core_safe_stop(runtime)
        if not core.allowed:
            return core

    profile = runtime.profile or GenericProfile()
    try:
        verdict = await async_invoke_profile(profile.can_stop, context)
    except Exception as err:  # noqa: BLE001 - isolate arbitrary profile verdict failures.
        log_profile_failure(_LOGGER, "can_stop", err, profile_id=getattr(profile, "profile_id", "unknown"))
        return blocked("The selected game profile failed while checking Stop; details were logged.")
    if not valid_capability_verdict(verdict):
        return blocked("The selected game profile returned an invalid Stop verdict.")
    if force and verdict.state == CapabilityState.BLOCKED and verdict.overridable:
        return SUPPORTED
    return verdict


def _hard_status_block(runtime: ServiceRuntime) -> CapabilityVerdict | None:
    if runtime.server is None:
        return blocked("No server status is available.", overridable=False)
    return None


def _core_safe_start(runtime: ServiceRuntime, *, now: int, settle_seconds: int) -> CapabilityVerdict:
    if runtime.using_cached_data or not runtime.status_fresh:
        return blocked("Status is cached or stale; start was not sent.", overridable=True)
    if runtime.server and runtime.server.raw_status != STOPPED_STATUS:
        return blocked(f"Server status is {runtime.server.raw_status}; start was not sent.", overridable=True)

    stopped_for = runtime.stopped_for(now)
    if stopped_for is not None and stopped_for < settle_seconds:
        return blocked(
            f"Server has been stopped for {stopped_for}s; waiting for {settle_seconds}s settle window.",
            overridable=True,
        )

    transition_age = runtime.last_transition_age(now)
    if transition_age is not None and transition_age < settle_seconds:
        return blocked(
            f"Last transition was {transition_age}s ago; waiting for {settle_seconds}s settle window.",
            overridable=True,
        )
    return SUPPORTED


def _core_safe_stop(runtime: ServiceRuntime) -> CapabilityVerdict:
    if runtime.using_cached_data or not runtime.status_fresh:
        return blocked("Status is cached or stale; stop was not sent.", overridable=True)
    if runtime.server and runtime.server.raw_status != RUNNING_STATUS:
        return blocked(f"Server status is {runtime.server.raw_status}; stop was not sent.", overridable=True)
    return SUPPORTED


def _context(runtime: ServiceRuntime, *, force: bool) -> ControlContext:
    return ControlContext(
        service=runtime.service,
        server=runtime.server,
        status_fresh=runtime.status_fresh,
        using_cached_data=runtime.using_cached_data,
        force=force,
        extra=runtime.profile_extra(),
        options=dict(runtime.extra.get("_persisted_profile_options", {})),
    )
