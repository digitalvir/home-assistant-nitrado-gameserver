"""Game profile plugin contract."""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import math
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol

from ..api.nitrado import NitradoApiError, NitradoClient, NitradoService, ParsedServer
from ..profile_workers import ProfileWorkerUnavailable, async_run_profile_sync

VALID_ENTITY_PLATFORMS = frozenset({"sensor", "binary_sensor", "button", "switch", "number", "select"})
VALID_ENTITY_CATEGORIES = frozenset({"config", "diagnostic"})
PROFILE_API_VERSION = 2
COCKPIT_API_VERSION = 1
PROFILE_HANDLER_TIMEOUT_SECONDS = 15.0
EXTENSION_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
COCKPIT_ROUTE_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ProfileReadTransport(Protocol):
    """Read-only provider transport bound to the active service."""

    async def fetch_players(self) -> tuple[str, ...]:
        """Fetch the provider's explicit player list."""

    async def list_files(self, directory: str | None = None) -> dict[str, Any]:
        """List provider files for one service."""

    async def download_file(self, path: str) -> str:
        """Download one text file."""

    async def fetch_external_json(
        self,
        url: str,
        *,
        allowed_hosts: Iterable[str],
        headers: dict[str, str] | None = None,
        timeout_seconds: int = 10,
    ) -> dict[str, Any]:
        """Fetch bounded game-native JSON through core's outbound policy."""


class _ProfileReadTransportFacade:
    """Expose only read operations from the mutable provider client."""

    __slots__ = ("__client", "__file_transport", "__service_id")

    def __init__(self, client: NitradoClient, service_id: str, file_transport: Any | None = None) -> None:
        self.__client = client
        self.__file_transport = file_transport or client
        self.__service_id = service_id

    async def fetch_players(self) -> tuple[str, ...]:
        return await self.__client.fetch_players(self.__service_id)

    async def list_files(self, directory: str | None = None) -> dict[str, Any]:
        return await self.__file_transport.list_files(self.__service_id, directory)

    async def download_file(self, path: str) -> str:
        return await self.__file_transport.download_file(self.__service_id, path)

    async def fetch_external_json(
        self,
        url: str,
        *,
        allowed_hosts: Iterable[str],
        headers: dict[str, str] | None = None,
        timeout_seconds: int = 10,
    ) -> dict[str, Any]:
        return await self.__client.fetch_external_json(
            url,
            allowed_hosts=allowed_hosts,
            headers=headers,
            timeout_seconds=timeout_seconds,
        )


def profile_read_transport(
    client: NitradoClient,
    service_id: str,
    *,
    file_transport: Any | None = None,
) -> ProfileReadTransport:
    """Return a capability-limited facade for untrusted profile code."""

    if not isinstance(service_id, str) or not service_id:
        raise NitradoApiError("Profile read transport requires a Nitrado service ID")
    return _ProfileReadTransportFacade(client, service_id, file_transport)


class CapabilityState(StrEnum):
    """Capability support state."""

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    DISABLED = "disabled"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class DataSource(StrEnum):
    """Where a profile value came from."""

    NITRADO = "nitrado"
    GAME_REST = "game_rest"
    RCON = "rcon"
    PROFILE = "profile"
    UNAVAILABLE = "unavailable"


class LifecycleEvent(StrEnum):
    """Profile lifecycle hook events hosted by core."""

    BEFORE_START = "before_start"
    AFTER_START = "after_start"
    BEFORE_STOP = "before_stop"
    AFTER_STOP = "after_stop"
    BEFORE_FILE_WRITE = "before_file_write"
    AFTER_FILE_WRITE = "after_file_write"
    BEFORE_RESTORE = "before_restore"
    AFTER_RESTORE = "after_restore"
    SCHEDULED_VALIDATION = "scheduled_validation"
    STATUS_REFRESH = "status_refresh"


class ValidatorTarget(StrEnum):
    """Dispatch surfaces where a profile validator may be run."""

    ACTION = "action"
    RESOURCE = "resource"
    SURFACE = "surface"
    EDITABLE_FILE = "editable_file"
    LIFECYCLE_HOOK = "lifecycle_hook"


class ValidatorDomain(StrEnum):
    """Semantic domains a profile validator describes."""

    SETTINGS = "settings"
    SAVE = "save"
    RESTORE = "restore"
    SERVER_STATE = "server_state"
    PLAYER_SOURCE = "player_source"


class ResourceContentFamily(StrEnum):
    """Broad resource content family understood by generic core."""

    IMAGE = "image"
    JSON = "json"
    TEXT = "text"
    BINARY = "binary"
    STREAM = "stream"
    UNKNOWN = "unknown"


class ExtensionAccess(StrEnum):
    """Minimum Home Assistant user access for an extension endpoint."""

    AUTHENTICATED = "authenticated"
    ADMIN = "admin"


class ProfileOptionType(StrEnum):
    """Administrator-managed profile option value types."""

    BOOLEAN = "boolean"
    NUMBER = "number"
    TEXT = "text"
    SELECT = "select"
    # Public option-type label, not a credential.
    SECRET = "secret"  # nosec B105


class ActionInputType(StrEnum):
    """Portable input types understood by generic action consumers."""

    BOOLEAN = "boolean"
    NUMBER = "number"
    TEXT = "text"
    SELECT = "select"
    JSON = "json"


class _UnsetType:
    """Sentinel for profile fields that should not override core data."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSET"


UNSET = _UnsetType()


async def async_invoke_profile(
    callback: Callable[..., Any],
    *args: Any,
    timeout: float | None = None,
    require_async: bool = False,
    worker_identity: Any | None = None,
) -> Any:
    """Invoke one profile callback without blocking Home Assistant's loop.

    Async callbacks receive the core-owned timeout. Synchronous callbacks run
    in a worker under the same budget and are permitted only for contracts that
    must be side-effect-free. External mutation boundaries pass
    ``require_async=True`` so cancellation cannot leave a worker thread
    continuing an unreported mutation after timeout or integration unload.
    """

    budget = PROFILE_HANDLER_TIMEOUT_SECONDS if timeout is None else timeout
    if require_async and not _is_async_callable(callback):
        raise TypeError("Profile mutation callbacks must be async")

    async def await_safely(awaitable: Any) -> Any:
        try:
            async with asyncio.timeout(budget):
                return await awaitable
        except asyncio.CancelledError:
            raise
        except Exception:
            raise
        except BaseException:  # noqa: BLE001 - normalize fatal extension exceptions.
            raise ProfileWorkerUnavailable("The async profile callback terminated abnormally") from None

    if _is_async_callable(callback):
        return await await_safely(callback(*args))
    is_partial = isinstance(callback, functools.partial)
    partial_function = callback.func if is_partial else callback
    bound_owner = getattr(partial_function, "__self__", None)
    bound_function = getattr(partial_function, "__func__", partial_function)
    callable_state: tuple[tuple[str, Any], ...] = ()
    if is_partial:
        partial_args = tuple(
            value if type(value) in {bool, int, float, str, bytes, type(None)} else type(value)
            for value in callback.args
        )
        partial_keywords = callback.keywords or {}
        callable_state = (
            ("partial_args", partial_args),
            (
                "partial_keywords",
                tuple(
                    sorted(
                        (key, value if type(value) in {bool, int, float, str, bytes, type(None)} else type(value))
                        for key, value in partial_keywords.items()
                        if type(key) is str
                    )
                ),
            ),
        )
    if (
        bound_owner is None
        and not inspect.isfunction(bound_function)
        and not inspect.ismethod(bound_function)
        and not is_partial
    ):
        bound_owner = callback
        bound_function = type(callback).__call__
        state_items: list[tuple[str, Any]] = []
        try:
            state = object.__getattribute__(callback, "__dict__")
        except AttributeError:
            state = {}
        if type(state) is dict:
            state_items.extend(
                (key, value)
                for key, value in state.items()
                if type(key) is str and type(value) in {bool, int, float, str, bytes, type(None)}
            )
        for owner_type in type(callback).__mro__:
            slots = owner_type.__dict__.get("__slots__", ())
            if type(slots) is str:
                slots = (slots,)
            if type(slots) not in {tuple, list}:
                continue
            for slot in slots:
                if type(slot) is not str or slot in {"__dict__", "__weakref__"}:
                    continue
                try:
                    value = object.__getattribute__(callback, slot)
                except AttributeError:
                    continue
                if type(value) in {bool, int, float, str, bytes, type(None)}:
                    state_items.append((slot, value))
        callable_state = tuple(sorted(set(state_items)))
    profile_id = inspect.getattr_static(bound_owner, "profile_id", None) if bound_owner is not None else None
    if type(profile_id) is not str:
        profile_id = None
    # Late import avoids the base/registry import cycle while binding quarantine
    # to the exact active extension generation instead of a process-lifetime
    # callback identity.
    from .registry import profile_registration_generation, profile_registry_generation

    identity = worker_identity or (
        "profile_callback",
        profile_id,
        profile_registration_generation(profile_id),
        profile_registry_generation(),
        bound_owner.__class__ if bound_owner is not None else None,
        getattr(bound_function, "__module__", callback.__class__.__module__),
        getattr(bound_function, "__qualname__", callback.__class__.__qualname__),
        bound_function if inspect.isfunction(bound_function) else bound_function.__class__,
        callable_state,
    )
    result = await async_run_profile_sync(identity, callback, *args, timeout=budget)
    if inspect.isawaitable(result):
        return await await_safely(result)
    return result


@dataclass(slots=True, frozen=True)
class CapabilityVerdict:
    """A structured operation/capability verdict."""

    state: CapabilityState
    reason: str = ""
    source: DataSource = DataSource.PROFILE
    overridable: bool = False

    @property
    def allowed(self) -> bool:
        """Return true when the capability can be used normally."""

        return self.state == CapabilityState.SUPPORTED


@dataclass(slots=True, frozen=True)
class EntityDeclaration:
    """Profile-declared entity surface."""

    platform: str
    key: str
    name: str
    kind: str | None = None
    value_fn: Callable[[ProfileEntityContext], Any] | None = None
    available_fn: Callable[[ProfileEntityContext], bool] | None = None
    action_fn: Callable[[ProfileActionContext], Awaitable[Any] | Any] | None = None
    turn_on_fn: Callable[[ProfileActionContext], Awaitable[Any] | Any] | None = None
    turn_off_fn: Callable[[ProfileActionContext], Awaitable[Any] | Any] | None = None
    set_value_fn: Callable[[ProfileActionContext, float], Awaitable[Any] | Any] | None = None
    options_fn: Callable[[ProfileEntityContext], tuple[str, ...] | list[str]] | None = None
    select_option_fn: Callable[[ProfileActionContext, str], Awaitable[Any] | Any] | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class ProfileOptionDeclaration:
    """Administrator-only persisted profile configuration."""

    key: str
    name: str
    option_type: ProfileOptionType
    description: str = ""
    default: Any = False
    attributes: dict[str, Any] = field(default_factory=dict)
    standard_options: bool = False
    onboarding: bool = False
    confirmation_required: bool = False
    acknowledgement_revision: int | None = None
    idle_shutdown_required: bool = False
    repair_if_unacknowledged: bool = False


@dataclass(slots=True, frozen=True)
class ActionInputDeclaration:
    """One typed field accepted by a profile action."""

    key: str
    name: str
    input_type: ActionInputType
    description: str = ""
    required: bool = False
    default: Any = None
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class ResourceDeclaration:
    """Profile-declared read-only resource contract.

    Resources are intentionally generic. A profile may expose an image, JSON
    document, text blob, binary export, or future stream without core knowing
    whether that resource is a map, log, settings preview, or something else.
    """

    key: str
    name: str
    content_type: str
    fetch_fn: Callable[[ProfileActionContext], Awaitable[Any] | Any]
    description: str = ""
    content_family: ResourceContentFamily | None = None
    cache_seconds: int = 0
    validators: tuple[str, ...] = ()
    access: ExtensionAccess = ExtensionAccess.AUTHENTICATED
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class ActionDeclaration:
    """Profile-declared mutation/command contract."""

    key: str
    name: str
    action_fn: Callable[[ProfileActionContext], Awaitable[Any] | Any]
    description: str = ""
    validators: tuple[str, ...] = ()
    requires_confirmation: bool = False
    access: ExtensionAccess = ExtensionAccess.ADMIN
    inputs: tuple[ActionInputDeclaration, ...] = ()
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class SurfaceDeclaration:
    """Profile-declared richer UI/composition contract."""

    key: str
    name: str
    description: str = ""
    resources: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    controls: tuple[str, ...] = ()
    editable_files: tuple[str, ...] = ()
    renderer_hint: str | None = None
    validators: tuple[str, ...] = ()
    access: ExtensionAccess = ExtensionAccess.AUTHENTICATED
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class CockpitDeclaration:
    """Optional profile-owned administrator cockpit contract."""

    key: str
    name: str
    cockpit_api_version: int
    asset_key: str
    frontend_revision: str
    default_route: str
    route_keys: tuple[str, ...]
    access: ExtensionAccess = ExtensionAccess.ADMIN
    route_aliases: tuple[tuple[str, str, str], ...] = ()


@dataclass(slots=True, frozen=True)
class LocalCockpitAsset:
    """Owner-package JavaScript asset supplied during profile registration."""

    package_resource: str
    sha256: str


@dataclass(slots=True, frozen=True)
class LifecycleHookDeclaration:
    """Profile-declared lifecycle hook contract."""

    key: str
    event: LifecycleEvent
    hook_fn: Callable[[ProfileActionContext], Awaitable[CapabilityVerdict | Any] | CapabilityVerdict | Any]
    description: str = ""
    validators: tuple[str, ...] = ()
    order: int = 100
    blocking: bool = True
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class ValidatorDeclaration:
    """Profile-declared reusable safety validator contract."""

    key: str
    name: str
    target: ValidatorTarget
    validate_fn: Callable[[ProfileActionContext, Any | None], Awaitable[CapabilityVerdict] | CapabilityVerdict]
    description: str = ""
    applies_to: tuple[ValidatorTarget, ...] = ()
    domains: tuple[ValidatorDomain, ...] = ()
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class ProfileStatus:
    """Profile-enriched service status fields."""

    player_count: int | _UnsetType | None = UNSET
    player_max: int | _UnsetType | None = UNSET
    player_names: tuple[str, ...] | _UnsetType = UNSET
    player_source: str | _UnsetType | None = UNSET
    query_valid: bool | _UnsetType | None = UNSET
    display_name: str | _UnsetType | None = UNSET
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class MatchResult:
    """Game profile match result."""

    matched: bool
    confidence: float = 0.0
    reason: str = ""


def valid_match_result(value: Any) -> bool:
    """Return whether a profile match result is structurally safe."""

    return (
        type(value) is MatchResult
        and type(value.matched) is bool
        and type(value.confidence) in {int, float}
        and math.isfinite(float(value.confidence))
        and 0.0 <= float(value.confidence) <= 1.0
        and type(value.reason) is str
        and len(value.reason) <= 1024
    )


def valid_capability_verdict(value: Any) -> bool:
    """Return whether a profile capability verdict is structurally safe."""

    return (
        type(value) is CapabilityVerdict
        and type(value.state) is CapabilityState
        and type(value.reason) is str
        and len(value.reason) <= 4096
        and type(value.source) is DataSource
        and type(value.overridable) is bool
    )


@dataclass(slots=True, frozen=True)
class ControlContext:
    """Inputs used to decide whether a command is safe."""

    service: NitradoService | None
    server: ParsedServer | None
    status_fresh: bool
    using_cached_data: bool
    force: bool = False
    extra: dict[str, Any] = field(default_factory=dict)
    options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class ProfileActionContext:
    """Runtime context passed to profile-declared actions and editors."""

    client: ProfileReadTransport
    service: NitradoService | None
    server: ParsedServer | None
    status_fresh: bool
    using_cached_data: bool
    force: bool = False
    now: int | None = None
    payload: Any | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    options: Mapping[str, Any] = field(default_factory=dict)

    @property
    def service_id(self) -> str | None:
        """Return the active Nitrado service ID when available."""

        if self.service is not None:
            return self.service.service_id
        if self.server is not None:
            return self.server.service_id
        return None

    def require_service_id(self) -> str:
        """Return service ID or raise when a profile handler cannot proceed."""

        service_id = self.service_id
        if service_id is None:
            raise NitradoApiError("Profile handler requires a Nitrado service ID")
        return service_id

    async def list_files(self, directory: str | None = None) -> dict[str, Any]:
        """List files for the active service through the controlled context."""

        return await self.client.list_files(directory)

    async def download_text_file(self, path: str) -> str:
        """Download a text file for the active service through the controlled context."""

        return await self.client.download_file(path)


@dataclass(slots=True, frozen=True)
class ProfileEntityContext:
    """Read-only-ish runtime context passed to profile-declared passive entities."""

    service: NitradoService | None
    server: ParsedServer | None
    status_fresh: bool
    using_cached_data: bool
    extra: Mapping[str, Any] = field(default_factory=dict)
    options: Mapping[str, Any] = field(default_factory=dict)

    @property
    def service_id(self) -> str | None:
        """Return the active Nitrado service ID when available."""

        if self.service is not None:
            return self.service.service_id
        if self.server is not None:
            return self.server.service_id
        return None


@dataclass(slots=True, frozen=True)
class CockpitSnapshotContext:
    """Secret-free profile context used to build namespaced cockpit state."""

    service_id: str
    server: ParsedServer | None
    status_fresh: bool
    using_cached_data: bool
    public_options: Mapping[str, Any] = field(default_factory=dict)
    configured_options: frozenset[str] = frozenset()
    option_acknowledgements: Mapping[str, int] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class EditableFileDeclaration:
    """Profile-declared editable text file contract."""

    key: str
    name: str
    path_fn: Callable[[ProfileActionContext], Awaitable[str | None] | str | None]
    description: str = ""
    parser: Callable[[str], Any] | None = None
    serializer: Callable[[Any], str] | None = None
    validator: Callable[[Any], Awaitable[CapabilityVerdict] | CapabilityVerdict] | None = None
    validators: tuple[str, ...] = ()
    redactor: Callable[[str], str] | None = None
    editor_modeler: Callable[[str], Any] | None = None
    editor_patcher: Callable[[str, Any], str] | None = None
    requires_restart: bool = True
    requires_stopped: bool = False
    requires_running: bool = False
    create_backup: bool = True
    access: ExtensionAccess = ExtensionAccess.ADMIN
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class SaveBundleDeclaration:
    """Profile-declared portable save-tree bundle contract.

    Profiles locate and validate game-specific save trees. Generic core owns
    transport, archive handling, authorization, review, replacement, and
    rollback.
    """

    key: str
    name: str
    root_fn: Callable[[ProfileActionContext], Awaitable[str | None] | str | None]
    allowed_suffixes: tuple[str, ...]
    description: str = ""
    required_files: tuple[str, ...] = ()
    excluded_paths: tuple[str, ...] = ()
    editor_root_files: tuple[str, ...] = ()
    editor_root_directories: tuple[str, ...] = ()
    requires_stopped: bool = True
    access: ExtensionAccess = ExtensionAccess.ADMIN
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class ProfileExtensionManifest:
    """Collected profile-declared extension contracts."""

    profile_id: str
    entities: tuple[EntityDeclaration, ...] = ()
    profile_options: tuple[ProfileOptionDeclaration, ...] = ()
    editable_files: tuple[EditableFileDeclaration, ...] = ()
    save_bundles: tuple[SaveBundleDeclaration, ...] = ()
    resources: tuple[ResourceDeclaration, ...] = ()
    actions: tuple[ActionDeclaration, ...] = ()
    surfaces: tuple[SurfaceDeclaration, ...] = ()
    lifecycle_hooks: tuple[LifecycleHookDeclaration, ...] = ()
    validators: tuple[ValidatorDeclaration, ...] = ()
    cockpit: CockpitDeclaration | None = None


class GameProfile(Protocol):
    """Protocol implemented by game profile plugins."""

    api_version: int
    profile_id: str
    name: str
    supported_games: tuple[str, ...]
    idle_shutdown_supported: bool

    def matches(self, service: NitradoService, server: ParsedServer | None = None) -> MatchResult:
        """Return whether this profile matches the Nitrado service."""

    async def enrich_status(
        self,
        client: ProfileReadTransport,
        service: NitradoService,
        server: ParsedServer,
        context: ControlContext,
    ) -> ProfileStatus:
        """Return profile-specific status enrichment."""

    async def suggest_display_name(
        self,
        client: ProfileReadTransport,
        service: NitradoService,
        server: ParsedServer | None,
    ) -> str | None:
        """Return a profile-specific suggested service display name, if available."""

    async def can_start(self, context: ControlContext) -> CapabilityVerdict:
        """Return whether this profile allows safe Start."""

    async def can_stop(self, context: ControlContext) -> CapabilityVerdict:
        """Return whether this profile allows safe Stop."""

    def idle_shutdown_capability(self, context: ControlContext) -> CapabilityVerdict:
        """Return whether this profile supports idle shutdown for this service."""

    def extra_entities(self) -> tuple[EntityDeclaration, ...]:
        """Return extra entity declarations."""

    def profile_options(self) -> tuple[ProfileOptionDeclaration, ...]:
        """Return administrator-only persisted profile options."""

    def editable_files(self) -> tuple[EditableFileDeclaration, ...]:
        """Return editable text-file declarations."""

    def save_bundles(self) -> tuple[SaveBundleDeclaration, ...]:
        """Return portable save-tree bundle declarations."""

    def resources(self) -> tuple[ResourceDeclaration, ...]:
        """Return read-only resource declarations."""

    def actions(self) -> tuple[ActionDeclaration, ...]:
        """Return profile command/action declarations."""

    def surfaces(self) -> tuple[SurfaceDeclaration, ...]:
        """Return rich UI/composition declarations."""

    def lifecycle_hooks(self) -> tuple[LifecycleHookDeclaration, ...]:
        """Return lifecycle hook declarations."""

    def validators(self) -> tuple[ValidatorDeclaration, ...]:
        """Return reusable safety validator declarations."""


SUPPORTED = CapabilityVerdict(CapabilityState.SUPPORTED)


class BaseGameProfile:
    """Safe-default base class for bundled and external game profiles.

    Subclasses provide identity and matching, then override only the
    capabilities they actually support.  The defaults add no player truth,
    mutations, files, resources, entities, or lifecycle policy.
    """

    api_version = PROFILE_API_VERSION
    profile_id = ""
    name = ""
    supported_games: tuple[str, ...] = ()
    idle_shutdown_supported = False

    def matches(self, service: NitradoService, server: ParsedServer | None = None) -> MatchResult:
        """Do not match until a concrete profile implements matching."""

        del service, server
        return MatchResult(False)

    async def enrich_status(
        self,
        client: ProfileReadTransport,
        service: NitradoService,
        server: ParsedServer,
        context: ControlContext,
    ) -> ProfileStatus:
        """Add no status overrides by default."""

        del client, service, server, context
        return ProfileStatus()

    async def suggest_display_name(
        self,
        client: ProfileReadTransport,
        service: NitradoService,
        server: ParsedServer | None,
    ) -> str | None:
        """Add no display-name override by default."""

        del client, service, server
        return None

    async def can_start(self, context: ControlContext) -> CapabilityVerdict:
        """Add no game-specific Start restriction by default."""

        del context
        return SUPPORTED

    async def can_stop(self, context: ControlContext) -> CapabilityVerdict:
        """Add no game-specific Stop restriction by default."""

        del context
        return SUPPORTED

    def idle_shutdown_capability(self, context: ControlContext) -> CapabilityVerdict:
        """Fail closed until the profile explicitly supports player truth."""

        del context
        return unsupported("Selected game profile does not support automatic idle shutdown")

    def extra_entities(self) -> tuple[EntityDeclaration, ...]:
        return ()

    def profile_options(self) -> tuple[ProfileOptionDeclaration, ...]:
        return ()

    def editable_files(self) -> tuple[EditableFileDeclaration, ...]:
        return ()

    def save_bundles(self) -> tuple[SaveBundleDeclaration, ...]:
        return ()

    def resources(self) -> tuple[ResourceDeclaration, ...]:
        return ()

    def actions(self) -> tuple[ActionDeclaration, ...]:
        return ()

    def surfaces(self) -> tuple[SurfaceDeclaration, ...]:
        return ()

    def lifecycle_hooks(self) -> tuple[LifecycleHookDeclaration, ...]:
        return ()

    def validators(self) -> tuple[ValidatorDeclaration, ...]:
        return ()

    def cockpit(self) -> CockpitDeclaration | None:
        return None

    def cockpit_snapshot(self, context: CockpitSnapshotContext) -> Mapping[str, Any]:
        del context
        return {}

    def cockpit_assets(self) -> Mapping[str, LocalCockpitAsset]:
        return {}


class ProfileManifestError(ValueError):
    """Raised when a profile declares an invalid extension manifest."""


def unsupported(reason: str, *, source: DataSource = DataSource.PROFILE) -> CapabilityVerdict:
    """Build an unsupported verdict."""

    return CapabilityVerdict(CapabilityState.UNSUPPORTED, reason=reason, source=source)


def blocked(reason: str, *, overridable: bool = False, source: DataSource = DataSource.PROFILE) -> CapabilityVerdict:
    """Build a blocked verdict."""

    return CapabilityVerdict(CapabilityState.BLOCKED, reason=reason, source=source, overridable=overridable)


def profile_extension_manifest(profile: GameProfile) -> ProfileExtensionManifest:
    """Collect the extension contracts declared by a profile."""

    try:
        manifest = ProfileExtensionManifest(
            profile_id=profile.profile_id,
            entities=_declaration_tuple("entity", profile.extra_entities()),
            profile_options=_declaration_tuple(
                "profile option",
                getattr(profile, "profile_options", lambda: ())(),
            ),
            editable_files=_declaration_tuple("editable file", profile.editable_files()),
            save_bundles=_declaration_tuple("save bundle", getattr(profile, "save_bundles", lambda: ())()),
            resources=_declaration_tuple("resource", profile.resources()),
            actions=_declaration_tuple("action", profile.actions()),
            surfaces=_declaration_tuple("surface", profile.surfaces()),
            lifecycle_hooks=_declaration_tuple("lifecycle hook", profile.lifecycle_hooks()),
            validators=_declaration_tuple("validator", profile.validators()),
            cockpit=getattr(profile, "cockpit", lambda: None)(),
        )
    except ProfileManifestError:
        raise
    except Exception as err:  # noqa: BLE001 - untrusted profile boundary
        profile_id = getattr(profile, "profile_id", "unknown")
        raise ProfileManifestError(
            f"Profile {profile_id} extension declarations failed with {err.__class__.__name__}"
        ) from None
    _validate_manifest_profile_id(manifest.profile_id)
    _validate_entity_declarations(manifest.profile_id, manifest.entities)
    _validate_declaration_keys("profile option", manifest.profile_options)
    _validate_declaration_keys("editable file", manifest.editable_files)
    _validate_declaration_keys("save bundle", manifest.save_bundles)
    _validate_declaration_keys("resource", manifest.resources)
    _validate_declaration_keys("action", manifest.actions)
    _validate_declaration_keys("surface", manifest.surfaces)
    _validate_declaration_keys("lifecycle hook", manifest.lifecycle_hooks)
    _validate_declaration_keys("validator", manifest.validators)
    _validate_manifest_fields(manifest)
    _validate_manifest_references(manifest)
    _validate_mutation_callbacks_async(manifest)
    return _freeze_manifest(manifest)


def _freeze_manifest(manifest: ProfileExtensionManifest) -> ProfileExtensionManifest:
    """Detach and recursively freeze all profile-owned declaration containers."""

    def attrs(value: Mapping[str, Any]) -> Mapping[str, Any]:
        return _freeze_json_value(dict(value))

    entities = tuple(replace(item, attributes=attrs(item.attributes)) for item in manifest.entities)
    profile_options = tuple(
        replace(item, default=_freeze_json_value(item.default), attributes=attrs(item.attributes))
        for item in manifest.profile_options
    )
    editable_files = tuple(
        replace(item, validators=tuple(item.validators), attributes=attrs(item.attributes))
        for item in manifest.editable_files
    )
    save_bundles = tuple(
        replace(
            item,
            required_files=tuple(item.required_files),
            allowed_suffixes=tuple(item.allowed_suffixes),
            excluded_paths=tuple(item.excluded_paths),
            editor_root_files=tuple(item.editor_root_files),
            editor_root_directories=tuple(item.editor_root_directories),
            attributes=attrs(item.attributes),
        )
        for item in manifest.save_bundles
    )
    resources = tuple(
        replace(item, validators=tuple(item.validators), attributes=attrs(item.attributes))
        for item in manifest.resources
    )
    actions = tuple(
        replace(
            item,
            validators=tuple(item.validators),
            inputs=tuple(
                replace(
                    input_item,
                    default=_freeze_json_value(input_item.default),
                    attributes=attrs(input_item.attributes),
                )
                for input_item in item.inputs
            ),
            attributes=attrs(item.attributes),
        )
        for item in manifest.actions
    )
    surfaces = tuple(
        replace(
            item,
            resources=tuple(item.resources),
            actions=tuple(item.actions),
            controls=tuple(item.controls),
            editable_files=tuple(item.editable_files),
            validators=tuple(item.validators),
            attributes=attrs(item.attributes),
        )
        for item in manifest.surfaces
    )
    lifecycle_hooks = tuple(
        replace(item, validators=tuple(item.validators), attributes=attrs(item.attributes))
        for item in manifest.lifecycle_hooks
    )
    validators = tuple(
        replace(
            item,
            applies_to=tuple(item.applies_to),
            domains=tuple(item.domains),
            attributes=attrs(item.attributes),
        )
        for item in manifest.validators
    )
    cockpit = (
        None
        if manifest.cockpit is None
        else replace(
            manifest.cockpit,
            route_keys=tuple(manifest.cockpit.route_keys),
            route_aliases=tuple(tuple(alias) for alias in manifest.cockpit.route_aliases),
        )
    )
    return ProfileExtensionManifest(
        profile_id=manifest.profile_id,
        entities=entities,
        profile_options=profile_options,
        editable_files=editable_files,
        save_bundles=save_bundles,
        resources=resources,
        actions=actions,
        surfaces=surfaces,
        lifecycle_hooks=lifecycle_hooks,
        validators=validators,
        cockpit=cockpit,
    )


def _freeze_json_value(value: Any) -> Any:
    """Return an immutable copy of one validated JSON-like value."""

    if type(value) is dict:
        if any(type(key) is not str or not key or len(key) > 256 for key in value):
            raise ProfileManifestError("Profile declared a non-string or oversized attribute key")
        return MappingProxyType({key: _freeze_json_value(child) for key, child in value.items()})
    if type(value) in {list, tuple}:
        return tuple(_freeze_json_value(child) for child in value)
    if type(value) in {str, bool, int, float, type(None)}:
        return value
    raise ProfileManifestError("Profile declared a non-JSON or subclassed attribute value")


def normalized_profile_entity_key(profile_id: str, key: str) -> str:
    """Return the HA-facing entity key for a profile entity declaration."""

    return key if key.startswith(f"{profile_id}_") else f"{profile_id}_{key}"


def _validate_manifest_profile_id(profile_id: Any) -> None:
    """Raise when a profile ID cannot safely namespace extension declarations."""

    if type(profile_id) is not str or not EXTENSION_KEY_PATTERN.fullmatch(profile_id):
        raise ProfileManifestError("Profile declared an invalid profile_id")


def _declaration_tuple(label: str, value: Any) -> tuple[Any, ...]:
    """Return a normalized declaration tuple from a profile declaration method."""

    if value is None:
        return ()
    if type(value) not in {tuple, list}:
        raise ProfileManifestError(f"Profile {label} declarations must be a tuple or list")
    return tuple(value)


def _validate_entity_declarations(profile_id: str, declarations: tuple[EntityDeclaration, ...]) -> None:
    """Raise when profile entity declarations cannot be hosted consistently."""

    seen: set[str] = set()
    duplicate: set[str] = set()
    for declaration in declarations:
        if type(declaration) is not EntityDeclaration:
            raise ProfileManifestError("Profile declared a non-entity item in entity declarations")
        if type(declaration.key) is not str or not EXTENSION_KEY_PATTERN.fullmatch(declaration.key):
            raise ProfileManifestError("Profile declared an entity with an invalid key")
        if type(declaration.platform) is not str or declaration.platform not in VALID_ENTITY_PLATFORMS:
            raise ProfileManifestError(
                f"Profile declared unsupported entity platform {declaration.platform!r} for {declaration.key}"
            )
        key = normalized_profile_entity_key(profile_id, declaration.key)
        if key in seen:
            duplicate.add(key)
        seen.add(key)
    if duplicate:
        raise ProfileManifestError(f"Profile declared duplicate normalized entity keys: {', '.join(sorted(duplicate))}")


def _validate_declaration_keys(label: str, declarations: tuple[Any, ...]) -> None:
    """Raise when a profile declares duplicate keys in one extension family."""

    seen: set[str] = set()
    duplicate: set[str] = set()
    for declaration in declarations:
        key = getattr(declaration, "key", None)
        if type(key) is not str or not EXTENSION_KEY_PATTERN.fullmatch(key):
            raise ProfileManifestError(f"Profile declared {label} with an invalid key")
        if key in seen:
            duplicate.add(str(key))
        seen.add(str(key))
    if duplicate:
        raise ProfileManifestError(f"Profile declared duplicate {label} keys: {', '.join(sorted(duplicate))}")


def _validate_manifest_fields(manifest: ProfileExtensionManifest) -> None:
    """Raise when declaration fields cannot be used safely by generic core."""

    for entity in manifest.entities:
        _validate_text("entity name", entity.name)
        _validate_optional_text("entity kind", entity.kind)
        _validate_optional_sync_callable("entity value_fn", entity.value_fn)
        _validate_optional_sync_callable("entity available_fn", entity.available_fn)
        _validate_optional_callable("entity action_fn", entity.action_fn)
        _validate_optional_callable("entity turn_on_fn", entity.turn_on_fn)
        _validate_optional_callable("entity turn_off_fn", entity.turn_off_fn)
        _validate_optional_callable("entity set_value_fn", entity.set_value_fn)
        _validate_optional_sync_callable("entity options_fn", entity.options_fn)
        _validate_optional_callable("entity select_option_fn", entity.select_option_fn)
        _validate_mapping("entity attributes", entity.attributes)
        _validate_json_mapping("entity attributes", entity.attributes)
        _validate_entity_attributes(entity)

    for option in manifest.profile_options:
        _validate_instance("profile option declaration", option, ProfileOptionDeclaration)
        _validate_text("profile option name", option.name)
        _validate_enum("profile option type", option.option_type, ProfileOptionType)
        _validate_optional_text("profile option description", option.description)
        _validate_mapping("profile option attributes", option.attributes)
        _validate_json_mapping("profile option attributes", option.attributes)
        _validate_bool("profile option standard_options", option.standard_options)
        _validate_bool("profile option onboarding", option.onboarding)
        _validate_bool("profile option confirmation_required", option.confirmation_required)
        _validate_bool("profile option idle_shutdown_required", option.idle_shutdown_required)
        _validate_bool("profile option repair_if_unacknowledged", option.repair_if_unacknowledged)
        if option.acknowledgement_revision is not None:
            if type(option.acknowledgement_revision) is not int or option.acknowledgement_revision < 1:
                raise ProfileManifestError(
                    f"Profile option {option.key} acknowledgement_revision must be a positive integer"
                )
            if not option.confirmation_required:
                raise ProfileManifestError(
                    f"Profile option {option.key} acknowledgement_revision requires confirmation_required"
                )
        if option.onboarding and not option.standard_options:
            raise ProfileManifestError(f"Profile option {option.key} onboarding requires standard_options")
        if option.repair_if_unacknowledged and option.acknowledgement_revision is None:
            raise ProfileManifestError(
                f"Profile option {option.key} repair_if_unacknowledged requires acknowledgement_revision"
            )
        try:
            validate_profile_option_value(option, option.default, is_default=True)
        except ValueError as err:
            raise ProfileManifestError(
                f"Profile option {option.key} declared invalid default with {err.__class__.__name__}"
            ) from None

    for file in manifest.editable_files:
        _validate_instance("editable file declaration", file, EditableFileDeclaration)
        _validate_text("editable file name", file.name)
        _validate_callable("editable file path_fn", file.path_fn)
        _validate_optional_text("editable file description", file.description)
        _validate_optional_callable("editable file parser", file.parser)
        _validate_optional_callable("editable file serializer", file.serializer)
        _validate_optional_callable("editable file validator", file.validator)
        _validate_key_tuple("editable file validators", file.validators)
        _validate_optional_callable("editable file redactor", file.redactor)
        _validate_bool("editable file requires_restart", file.requires_restart)
        _validate_bool("editable file requires_stopped", file.requires_stopped)
        _validate_bool("editable file requires_running", file.requires_running)
        _validate_bool("editable file create_backup", file.create_backup)
        _validate_enum("editable file access", file.access, ExtensionAccess)
        if file.requires_stopped and file.requires_running:
            raise ProfileManifestError(f"Profile editable file {file.key} cannot require both stopped and running")
        _validate_mapping("editable file attributes", file.attributes)
        _validate_json_mapping("editable file attributes", file.attributes)

    for bundle in manifest.save_bundles:
        _validate_instance("save bundle declaration", bundle, SaveBundleDeclaration)
        _validate_text("save bundle name", bundle.name)
        _validate_callable("save bundle root_fn", bundle.root_fn)
        _validate_optional_text("save bundle description", bundle.description)
        _validate_key_tuple("save bundle required_files", bundle.required_files)
        _validate_key_tuple("save bundle excluded_paths", bundle.excluded_paths)
        _validate_key_tuple("save bundle editor_root_files", bundle.editor_root_files)
        _validate_key_tuple("save bundle editor_root_directories", bundle.editor_root_directories)
        if type(bundle.allowed_suffixes) is not tuple or not bundle.allowed_suffixes:
            raise ProfileManifestError(f"Profile save bundle {bundle.key} must declare allowed suffixes")
        for suffix in bundle.allowed_suffixes:
            if type(suffix) is not str or not suffix.startswith(".") or len(suffix) > 16:
                raise ProfileManifestError(f"Profile save bundle {bundle.key} declared an invalid suffix")
        _validate_save_bundle_paths(bundle)
        _validate_bool("save bundle requires_stopped", bundle.requires_stopped)
        _validate_enum("save bundle access", bundle.access, ExtensionAccess)
        if bundle.access is not ExtensionAccess.ADMIN:
            raise ProfileManifestError(f"Profile save bundle {bundle.key} must require administrator access")
        _validate_mapping("save bundle attributes", bundle.attributes)
        _validate_json_mapping("save bundle attributes", bundle.attributes)

    for resource in manifest.resources:
        _validate_instance("resource declaration", resource, ResourceDeclaration)
        _validate_text("resource name", resource.name)
        _validate_text("resource content_type", resource.content_type)
        if "/" not in resource.content_type.split(";", 1)[0]:
            raise ProfileManifestError(f"Profile resource {resource.key} declared invalid content_type")
        _validate_callable("resource fetch_fn", resource.fetch_fn)
        _validate_optional_text("resource description", resource.description)
        if resource.content_family is not None and type(resource.content_family) is not ResourceContentFamily:
            raise ProfileManifestError(f"Profile resource {resource.key} declared invalid content_family")
        _validate_non_negative_int("resource cache_seconds", resource.cache_seconds)
        _validate_key_tuple("resource validators", resource.validators)
        _validate_enum("resource access", resource.access, ExtensionAccess)
        _validate_mapping("resource attributes", resource.attributes)
        _validate_json_mapping("resource attributes", resource.attributes)
        inferred_family = _resource_content_family(resource)
        if inferred_family == ResourceContentFamily.STREAM and resource.cache_seconds:
            raise ProfileManifestError(f"Profile stream resource {resource.key} cannot be cached")

    for action in manifest.actions:
        _validate_instance("action declaration", action, ActionDeclaration)
        _validate_text("action name", action.name)
        _validate_callable("action action_fn", action.action_fn)
        _validate_optional_text("action description", action.description)
        _validate_key_tuple("action validators", action.validators)
        _validate_bool("action requires_confirmation", action.requires_confirmation)
        _validate_enum("action access", action.access, ExtensionAccess)
        _validate_action_inputs(action)
        _validate_mapping("action attributes", action.attributes)
        _validate_json_mapping("action attributes", action.attributes)

    for surface in manifest.surfaces:
        _validate_instance("surface declaration", surface, SurfaceDeclaration)
        _validate_text("surface name", surface.name)
        _validate_optional_text("surface description", surface.description)
        _validate_key_tuple("surface resources", surface.resources)
        _validate_key_tuple("surface actions", surface.actions)
        _validate_key_tuple("surface controls", surface.controls)
        _validate_key_tuple("surface editable_files", surface.editable_files)
        _validate_optional_text("surface renderer_hint", surface.renderer_hint)
        _validate_key_tuple("surface validators", surface.validators)
        _validate_enum("surface access", surface.access, ExtensionAccess)
        _validate_mapping("surface attributes", surface.attributes)
        _validate_json_mapping("surface attributes", surface.attributes)

    for hook in manifest.lifecycle_hooks:
        _validate_instance("lifecycle hook declaration", hook, LifecycleHookDeclaration)
        if type(hook.event) is not LifecycleEvent:
            raise ProfileManifestError(f"Profile lifecycle hook {hook.key} declared invalid event")
        _validate_callable("lifecycle hook hook_fn", hook.hook_fn)
        _validate_optional_text("lifecycle hook description", hook.description)
        _validate_key_tuple("lifecycle hook validators", hook.validators)
        _validate_int("lifecycle hook order", hook.order)
        _validate_bool("lifecycle hook blocking", hook.blocking)
        _validate_mapping("lifecycle hook attributes", hook.attributes)
        _validate_json_mapping("lifecycle hook attributes", hook.attributes)

    for validator in manifest.validators:
        _validate_instance("validator declaration", validator, ValidatorDeclaration)
        _validate_text("validator name", validator.name)
        if type(validator.target) is not ValidatorTarget:
            raise ProfileManifestError(f"Profile validator {validator.key} declared invalid target")
        _validate_callable("validator validate_fn", validator.validate_fn)
        _validate_optional_text("validator description", validator.description)
        _validate_enum_tuple("validator applies_to", validator.applies_to, ValidatorTarget)
        _validate_enum_tuple("validator domains", validator.domains, ValidatorDomain)
        _validate_mapping("validator attributes", validator.attributes)
        _validate_json_mapping("validator attributes", validator.attributes)

    if manifest.cockpit is not None:
        cockpit = manifest.cockpit
        _validate_instance("cockpit declaration", cockpit, CockpitDeclaration)
        if type(cockpit.key) is not str or not EXTENSION_KEY_PATTERN.fullmatch(cockpit.key):
            raise ProfileManifestError("Profile cockpit declared an invalid key")
        _validate_text("cockpit name", cockpit.name)
        if type(cockpit.cockpit_api_version) is not int:
            raise ProfileManifestError(f"Profile cockpit {cockpit.key} declared an invalid API version")
        if type(cockpit.asset_key) is not str or not EXTENSION_KEY_PATTERN.fullmatch(cockpit.asset_key):
            raise ProfileManifestError(f"Profile cockpit {cockpit.key} declared an invalid asset key")
        if type(cockpit.frontend_revision) is not str or not cockpit.frontend_revision.startswith("sha256:"):
            raise ProfileManifestError(f"Profile cockpit {cockpit.key} declared an invalid frontend revision")
        digest = cockpit.frontend_revision.removeprefix("sha256:")
        if SHA256_PATTERN.fullmatch(digest) is None:
            raise ProfileManifestError(f"Profile cockpit {cockpit.key} declared an invalid frontend digest")
        if type(cockpit.route_keys) not in {tuple, list}:
            raise ProfileManifestError(f"Profile cockpit {cockpit.key} declared invalid routes")
        if not cockpit.route_keys or len(cockpit.route_keys) > 32:
            raise ProfileManifestError(f"Profile cockpit {cockpit.key} must declare 1 through 32 routes")
        routes = tuple(cockpit.route_keys)
        if len(set(routes)) != len(routes) or any(
            type(route) is not str or COCKPIT_ROUTE_PATTERN.fullmatch(route) is None for route in routes
        ):
            raise ProfileManifestError(f"Profile cockpit {cockpit.key} declared invalid or duplicate routes")
        if type(cockpit.default_route) is not str or cockpit.default_route not in routes:
            raise ProfileManifestError(f"Profile cockpit {cockpit.key} default route is not declared")
        _validate_enum("cockpit access", cockpit.access, ExtensionAccess)
        if cockpit.access is not ExtensionAccess.ADMIN:
            raise ProfileManifestError(f"Profile cockpit {cockpit.key} must require administrator access")
        if type(cockpit.route_aliases) not in {tuple, list}:
            raise ProfileManifestError(f"Profile cockpit {cockpit.key} declared invalid route aliases")
        if len(cockpit.route_aliases) > 64:
            raise ProfileManifestError(f"Profile cockpit {cockpit.key} declared too many route aliases")
        seen_aliases: set[tuple[str, str]] = set()
        for alias in cockpit.route_aliases:
            if type(alias) not in {tuple, list} or len(alias) != 3:
                raise ProfileManifestError(f"Profile cockpit {cockpit.key} declared invalid route alias")
            source_profile, source_route, destination_route = alias
            if (
                type(source_profile) is not str
                or EXTENSION_KEY_PATTERN.fullmatch(source_profile) is None
                or type(source_route) is not str
                or COCKPIT_ROUTE_PATTERN.fullmatch(source_route) is None
                or type(destination_route) is not str
                or destination_route not in routes
            ):
                raise ProfileManifestError(f"Profile cockpit {cockpit.key} declared invalid route alias")
            identity = (source_profile, source_route)
            if identity in seen_aliases:
                raise ProfileManifestError(f"Profile cockpit {cockpit.key} declared duplicate route alias")
            seen_aliases.add(identity)


def _validate_manifest_references(manifest: ProfileExtensionManifest) -> None:
    """Raise when declarations reference missing extension keys."""

    validator_targets = {validator.key: validator for validator in manifest.validators}
    resource_keys = {resource.key for resource in manifest.resources}
    stream_resource_keys = {
        resource.key
        for resource in manifest.resources
        if _resource_content_family(resource) == ResourceContentFamily.STREAM
    }
    action_keys = {action.key for action in manifest.actions}
    control_keys = {entity.key for entity in manifest.entities}
    editable_file_keys = {file.key for file in manifest.editable_files}
    profile_option_keys = {option.key for option in manifest.profile_options}
    hosted_option_keys: set[str] = set()

    for entity in manifest.entities:
        option_key = entity.attributes.get("option_key")
        if option_key is None:
            continue
        if option_key in profile_option_keys:
            raise ProfileManifestError(
                f"Profile entity {entity.key} option_key collides with administrator profile option {option_key}"
            )
        if option_key in hosted_option_keys:
            raise ProfileManifestError(f"Profile declared duplicate hosted entity option_key: {option_key}")
        hosted_option_keys.add(option_key)

    for file in manifest.editable_files:
        _validate_validator_refs(
            file.validators, validator_targets, ValidatorTarget.EDITABLE_FILE, f"editable file {file.key}"
        )
    for resource in manifest.resources:
        _validate_validator_refs(
            resource.validators, validator_targets, ValidatorTarget.RESOURCE, f"resource {resource.key}"
        )
    for action in manifest.actions:
        _validate_validator_refs(action.validators, validator_targets, ValidatorTarget.ACTION, f"action {action.key}")
    for surface in manifest.surfaces:
        _validate_refs(surface.resources, resource_keys, f"surface {surface.key} resources")
        streamed = sorted(set(surface.resources) & stream_resource_keys)
        if streamed:
            raise ProfileManifestError(
                f"Profile surface {surface.key} cannot bundle stream resources: {', '.join(streamed)}"
            )
        _validate_refs(surface.actions, action_keys, f"surface {surface.key} actions")
        _validate_refs(surface.controls, control_keys, f"surface {surface.key} controls")
        _validate_refs(surface.editable_files, editable_file_keys, f"surface {surface.key} editable_files")
        _validate_validator_refs(
            surface.validators, validator_targets, ValidatorTarget.SURFACE, f"surface {surface.key}"
        )
    for hook in manifest.lifecycle_hooks:
        _validate_validator_refs(
            hook.validators, validator_targets, ValidatorTarget.LIFECYCLE_HOOK, f"lifecycle hook {hook.key}"
        )


def _validate_mutation_callbacks_async(manifest: ProfileExtensionManifest) -> None:
    """Reject mutation callbacks that cannot cooperate with timeout/unload."""

    for entity in manifest.entities:
        _validate_optional_async_callable("entity action_fn", entity.action_fn)
        _validate_optional_async_callable("entity turn_on_fn", entity.turn_on_fn)
        _validate_optional_async_callable("entity turn_off_fn", entity.turn_off_fn)
        _validate_optional_async_callable("entity set_value_fn", entity.set_value_fn)
        _validate_optional_async_callable("entity select_option_fn", entity.select_option_fn)
    for action in manifest.actions:
        _validate_async_callable("action action_fn", action.action_fn)
    for hook in manifest.lifecycle_hooks:
        _validate_async_callable("lifecycle hook hook_fn", hook.hook_fn)


def validate_profile_option_value(
    declaration: ProfileOptionDeclaration,
    value: Any,
    *,
    is_default: bool = False,
) -> Any:
    """Validate and normalize one persisted administrator profile option."""

    attributes = declaration.attributes
    if declaration.option_type == ProfileOptionType.BOOLEAN:
        if type(value) is not bool:
            raise ValueError("value must be a boolean")
        return value
    if declaration.option_type == ProfileOptionType.NUMBER:
        if type(value) not in {int, float}:
            raise ValueError("value must be a number")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("value must be finite")
        minimum = _finite_attribute(attributes, "min")
        maximum = _finite_attribute(attributes, "max")
        step = _finite_attribute(attributes, "step")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ValueError("min cannot exceed max")
        if step is not None and step <= 0:
            raise ValueError("step must be greater than zero")
        if minimum is not None and number < minimum:
            raise ValueError(f"value must be at least {minimum:g}")
        if maximum is not None and number > maximum:
            raise ValueError(f"value must be at most {maximum:g}")
        return value
    if declaration.option_type in {ProfileOptionType.TEXT, ProfileOptionType.SECRET}:
        if type(value) is not str:
            raise ValueError("value must be text")
        maximum = _bounded_length_attribute(attributes, default=4096)
        if len(value) > maximum:
            raise ValueError(f"value must not exceed {maximum} characters")
        if declaration.option_type == ProfileOptionType.SECRET and is_default and value:
            raise ValueError("secret defaults must be empty")
        multiline = attributes.get("multiline")
        if multiline is not None and type(multiline) is not bool:
            raise ValueError("multiline must be a boolean")
        return value
    if declaration.option_type == ProfileOptionType.SELECT:
        if type(value) is not str:
            raise ValueError("value must be text")
        options = _select_options(attributes)
        if value not in options:
            raise ValueError("value must be one of the declared options")
        return value
    raise ValueError("unsupported option type")


def validate_action_payload(action: ActionDeclaration, payload: Any) -> dict[str, Any] | Any:
    """Validate typed action input while preserving schema-free actions."""

    if not action.inputs:
        return payload
    if payload is None:
        payload = {}
    if not isinstance(payload, Mapping):
        raise ValueError("payload must be an object")
    invalid_keys = [key for key in payload if not isinstance(key, str)]
    if invalid_keys:
        raise ValueError("payload field names must be text")
    declared = {item.key: item for item in action.inputs}
    unknown = sorted(set(payload) - set(declared))
    if unknown:
        raise ValueError(f"payload contains unknown fields: {', '.join(unknown)}")
    normalized: dict[str, Any] = {}
    for key, field_declaration in declared.items():
        if key in payload:
            normalized[key] = _validate_action_input_value(field_declaration, payload[key])
        elif field_declaration.default is not None:
            normalized[key] = _thaw_json_value(field_declaration.default)
        elif field_declaration.required:
            raise ValueError(f"payload field {key} is required")
    return normalized


def _thaw_json_value(value: Any) -> Any:
    """Return a handler-owned mutable copy of one frozen JSON default."""

    if isinstance(value, Mapping):
        return {key: _thaw_json_value(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json_value(child) for child in value]
    return value


def _validate_action_inputs(action: ActionDeclaration) -> None:
    if type(action.inputs) not in {tuple, list}:
        raise ProfileManifestError(f"Profile action {action.key} declared invalid inputs")
    seen: set[str] = set()
    for item in action.inputs:
        _validate_instance("action input declaration", item, ActionInputDeclaration)
        if type(item.key) is not str or not EXTENSION_KEY_PATTERN.fullmatch(item.key):
            raise ProfileManifestError(f"Profile action {action.key} declared an input with an invalid key")
        if item.key in seen:
            raise ProfileManifestError(f"Profile action {action.key} declared duplicate input {item.key}")
        seen.add(item.key)
        _validate_text("action input name", item.name)
        _validate_enum("action input type", item.input_type, ActionInputType)
        _validate_optional_text("action input description", item.description)
        _validate_bool("action input required", item.required)
        _validate_mapping("action input attributes", item.attributes)
        _validate_json_mapping("action input attributes", item.attributes)
        if item.default is not None:
            try:
                _validate_action_input_value(item, item.default)
            except ValueError as err:
                raise ProfileManifestError(
                    f"Profile action {action.key} input {item.key} declared invalid default with "
                    f"{err.__class__.__name__}"
                ) from None


def _validate_action_input_value(declaration: ActionInputDeclaration, value: Any) -> Any:
    if declaration.input_type == ActionInputType.JSON:
        try:
            encoded = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError, OverflowError) as err:
            raise ValueError("value must be finite JSON") from err
        if len(encoded) > 65_536:
            raise ValueError("JSON value is too large")
        return value
    option_type = {
        ActionInputType.BOOLEAN: ProfileOptionType.BOOLEAN,
        ActionInputType.NUMBER: ProfileOptionType.NUMBER,
        ActionInputType.TEXT: ProfileOptionType.TEXT,
        ActionInputType.SELECT: ProfileOptionType.SELECT,
    }[declaration.input_type]
    pseudo_option = ProfileOptionDeclaration(
        key=declaration.key,
        name=declaration.name,
        option_type=option_type,
        default=value,
        attributes=declaration.attributes,
    )
    return validate_profile_option_value(pseudo_option, value)


def _finite_attribute(attributes: Mapping[str, Any], key: str) -> float | None:
    value = attributes.get(key)
    if value is None:
        return None
    if type(value) not in {int, float} or not math.isfinite(float(value)):
        raise ValueError(f"{key} must be a finite number")
    return float(value)


def _bounded_length_attribute(attributes: Mapping[str, Any], *, default: int) -> int:
    value = attributes.get("max_length", default)
    if type(value) is not int or not 1 <= value <= 4096:
        raise ValueError("max_length must be an integer from 1 through 4096")
    return value


def _select_options(attributes: Mapping[str, Any]) -> tuple[str, ...]:
    value = attributes.get("options")
    if type(value) not in {tuple, list} or not value:
        raise ValueError("select options must be a non-empty list")
    options = tuple(value)
    if any(type(item) is not str or not item for item in options) or len(set(options)) != len(options):
        raise ValueError("select options must be unique non-empty strings")
    return options


def _resource_content_family(resource: ResourceDeclaration) -> ResourceContentFamily:
    """Classify a declaration for manifest rules without importing dispatch."""

    if resource.content_family is not None:
        return resource.content_family
    media_type = resource.content_type.split(";", 1)[0].strip().lower()
    if media_type.startswith("image/"):
        return ResourceContentFamily.IMAGE
    if media_type == "application/json" or media_type.endswith("+json"):
        return ResourceContentFamily.JSON
    if media_type in {"application/octet-stream", "application/zip", "application/gzip", "application/x-tar"}:
        return ResourceContentFamily.BINARY
    if media_type in {"text/event-stream", "application/x-ndjson"} or "stream" in media_type:
        return ResourceContentFamily.STREAM
    if media_type.startswith("text/"):
        return ResourceContentFamily.TEXT
    return ResourceContentFamily.UNKNOWN


def _validate_instance(label: str, value: Any, expected: type) -> None:
    if type(value) is not expected:
        raise ProfileManifestError(f"Profile declared invalid {label}")


def _validate_text(label: str, value: Any) -> None:
    if type(value) is not str or not value.strip():
        raise ProfileManifestError(f"Profile declared invalid {label}")


def _validate_optional_text(label: str, value: Any) -> None:
    if value is not None and type(value) is not str:
        raise ProfileManifestError(f"Profile declared invalid {label}")


def _validate_callable(label: str, value: Any) -> None:
    if not callable(value):
        raise ProfileManifestError(f"Profile declared non-callable {label}")


def _validate_optional_callable(label: str, value: Any) -> None:
    if value is not None and not callable(value):
        raise ProfileManifestError(f"Profile declared non-callable {label}")


def _validate_optional_sync_callable(label: str, value: Any) -> None:
    _validate_optional_callable(label, value)
    if value is not None and _is_async_callable(value):
        raise ProfileManifestError(f"Profile declared async {label}; passive entity callbacks must be synchronous")


def _validate_async_callable(label: str, value: Any) -> None:
    _validate_callable(label, value)
    if not _is_async_callable(value):
        raise ProfileManifestError(f"Profile declared synchronous {label}; mutation callbacks must be async")


def _validate_optional_async_callable(label: str, value: Any) -> None:
    if value is not None:
        _validate_async_callable(label, value)


def _validate_bool(label: str, value: Any) -> None:
    if type(value) is not bool:
        raise ProfileManifestError(f"Profile declared invalid {label}")


def _validate_int(label: str, value: Any) -> None:
    if type(value) is not int:
        raise ProfileManifestError(f"Profile declared invalid {label}")


def _validate_non_negative_int(label: str, value: Any) -> None:
    _validate_int(label, value)
    if value < 0:
        raise ProfileManifestError(f"Profile declared invalid {label}")


def _validate_mapping(label: str, value: Any) -> None:
    if type(value) is not dict:
        raise ProfileManifestError(f"Profile declared invalid {label}")


def _validate_json_mapping(label: str, value: Mapping[str, Any]) -> None:
    """Require declaration metadata to be finite, bounded JSON."""

    try:
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        raise ProfileManifestError(f"Profile declared non-JSON {label}") from None
    if len(encoded) > 65_536:
        raise ProfileManifestError(f"Profile declared oversized {label}")


def _validate_entity_attributes(entity: EntityDeclaration) -> None:
    """Raise when HA-facing profile entity attributes are malformed."""

    attributes = entity.attributes
    category = attributes.get("entity_category")
    if category is not None and (type(category) is not str or category not in VALID_ENTITY_CATEGORIES):
        raise ProfileManifestError(f"Profile entity {entity.key} declared invalid entity_category")

    enabled_default = attributes.get("entity_registry_enabled_default")
    if enabled_default is not None and type(enabled_default) is not bool:
        raise ProfileManifestError(f"Profile entity {entity.key} declared invalid entity_registry_enabled_default")

    icon = attributes.get("icon")
    if icon is not None and (type(icon) is not str or not icon.strip()):
        raise ProfileManifestError(f"Profile entity {entity.key} declared invalid icon")

    option_key = attributes.get("option_key")
    if option_key is not None:
        if type(option_key) is not str or not EXTENSION_KEY_PATTERN.fullmatch(option_key):
            raise ProfileManifestError(f"Profile entity {entity.key} declared invalid option_key")
        if entity.platform not in {"switch", "number", "select"}:
            raise ProfileManifestError(
                f"Profile entity {entity.key} declared option_key on unsupported platform {entity.platform}"
            )
        hosted_callbacks = {
            "switch": (entity.value_fn, entity.turn_on_fn, entity.turn_off_fn),
            "number": (entity.value_fn, entity.set_value_fn),
            "select": (entity.value_fn, entity.select_option_fn),
        }[entity.platform]
        if any(callback is not None for callback in hosted_callbacks):
            raise ProfileManifestError(
                f"Profile entity {entity.key} cannot combine option_key with profile-owned state handlers"
            )

    if entity.platform == "number":
        _validate_optional_number_attribute(entity, "default_value")
        minimum = _validate_optional_number_attribute(entity, "native_min_value")
        maximum = _validate_optional_number_attribute(entity, "native_max_value")
        step = _validate_optional_number_attribute(entity, "native_step")
        if step is not None and step <= 0:
            raise ProfileManifestError(f"Profile entity {entity.key} declared invalid native_step")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ProfileManifestError(f"Profile entity {entity.key} declared native_min_value above native_max_value")
        unit = attributes.get("native_unit_of_measurement")
        if unit is not None and (type(unit) is not str or not unit.strip()):
            raise ProfileManifestError(f"Profile entity {entity.key} declared invalid native_unit_of_measurement")

    if entity.platform == "select":
        if "options" in attributes:
            _validate_key_tuple(f"select entity {entity.key} options", attributes["options"])
        if option_key is not None and "options" not in attributes and entity.options_fn is None:
            raise ProfileManifestError(
                f"Profile select entity {entity.key} with option_key must declare options or options_fn"
            )


def _validate_optional_number_attribute(entity: EntityDeclaration, name: str) -> float | None:
    """Return a numeric profile entity attribute or raise when malformed."""

    value = entity.attributes.get(name)
    if value is None:
        return None
    if type(value) not in {int, float}:
        raise ProfileManifestError(f"Profile entity {entity.key} declared invalid {name}")
    number = float(value)
    if not math.isfinite(number):
        raise ProfileManifestError(f"Profile entity {entity.key} declared non-finite {name}")
    return number


def _is_async_callable(value: Any) -> bool:
    """Return true for async functions and callable objects with async __call__."""

    return inspect.iscoroutinefunction(value) or (callable(value) and inspect.iscoroutinefunction(value.__call__))


def _validate_key_tuple(label: str, value: Any) -> None:
    if type(value) not in {tuple, list}:
        raise ProfileManifestError(f"Profile declared invalid {label}")
    for item in value:
        if type(item) is not str or not item.strip():
            raise ProfileManifestError(f"Profile declared invalid {label}")


def _validate_save_bundle_paths(bundle: SaveBundleDeclaration) -> None:
    """Validate game-declared relative paths before provider access."""

    def canonical(label: str, values: Sequence[str]) -> tuple[str, ...]:
        normalized: list[str] = []
        aliases: set[str] = set()
        for raw in values:
            if not raw or raw.startswith("/") or "\\" in raw or any(ord(char) < 32 or ord(char) == 127 for char in raw):
                raise ProfileManifestError(f"Profile save bundle {bundle.key} declared invalid {label}")
            parts = raw.split("/")
            if any(part in {"", ".", ".."} for part in parts):
                raise ProfileManifestError(f"Profile save bundle {bundle.key} declared invalid {label}")
            value = "/".join(parts)
            alias = value.casefold()
            if alias in aliases:
                raise ProfileManifestError(f"Profile save bundle {bundle.key} declared duplicate {label}")
            aliases.add(alias)
            normalized.append(value)
        return tuple(normalized)

    required = canonical("required_files", bundle.required_files)
    excluded = canonical("excluded_paths", bundle.excluded_paths)
    marker_files = canonical("editor_root_files", bundle.editor_root_files)
    marker_dirs = canonical("editor_root_directories", bundle.editor_root_directories)
    suffixes = tuple(suffix.casefold() for suffix in bundle.allowed_suffixes)
    if len(set(suffixes)) != len(suffixes):
        raise ProfileManifestError(f"Profile save bundle {bundle.key} declared duplicate allowed suffixes")
    for path in (*required, *marker_files):
        if not path.casefold().endswith(suffixes):
            raise ProfileManifestError(
                f"Profile save bundle {bundle.key} declared a required or marker file with an unsupported suffix"
            )
    excluded_aliases = tuple(path.casefold() for path in excluded)
    file_aliases: dict[str, str] = {}
    for path in (*required, *marker_files):
        alias = path.casefold()
        prior = file_aliases.get(alias)
        if prior is not None and prior != path:
            raise ProfileManifestError(f"Profile save bundle {bundle.key} declared case-colliding file markers")
        file_aliases[alias] = path
    directory_aliases = {path.casefold() for path in marker_dirs}
    if directory_aliases & set(file_aliases):
        raise ProfileManifestError(f"Profile save bundle {bundle.key} declared conflicting file and directory markers")
    for index, path in enumerate(excluded_aliases):
        if any(
            other != path and (path.startswith(f"{other}/") or other.startswith(f"{path}/"))
            for other in excluded_aliases[index + 1 :]
        ):
            raise ProfileManifestError(f"Profile save bundle {bundle.key} declared overlapping excluded paths")
    for path in required:
        alias = path.casefold()
        if any(alias == item or alias.startswith(f"{item}/") for item in excluded_aliases):
            raise ProfileManifestError(
                f"Profile save bundle {bundle.key} declared a required file under an excluded path"
            )
    for path in (*marker_files, *marker_dirs):
        alias = path.casefold()
        if any(alias == item or alias.startswith(f"{item}/") for item in excluded_aliases):
            raise ProfileManifestError(
                f"Profile save bundle {bundle.key} declared an editor root marker under an excluded path"
            )
    if not marker_files and not marker_dirs:
        raise ProfileManifestError(f"Profile save bundle {bundle.key} must declare editor ZIP root markers")


def _validate_enum_tuple(label: str, value: Any, expected: type) -> None:
    if type(value) not in {tuple, list}:
        raise ProfileManifestError(f"Profile declared invalid {label}")
    for item in value:
        if type(item) is not expected:
            raise ProfileManifestError(f"Profile declared invalid {label}")


def _validate_enum(label: str, value: Any, expected: type) -> None:
    if type(value) is not expected:
        raise ProfileManifestError(f"Profile declared invalid {label}")


def _validate_refs(refs: tuple[str, ...], available: set[str], label: str) -> None:
    missing = sorted(set(refs) - available)
    if missing:
        raise ProfileManifestError(f"Profile declared unresolved {label}: {', '.join(missing)}")


def _validate_validator_refs(
    refs: tuple[str, ...],
    validators: Mapping[str, ValidatorDeclaration],
    target: ValidatorTarget,
    label: str,
) -> None:
    missing = sorted(set(refs) - set(validators))
    if missing:
        raise ProfileManifestError(f"Profile declared unresolved {label} validators: {', '.join(missing)}")
    wrong_target = sorted(
        f"{key} is {validators[key].target.value}" for key in refs if not _validator_applies_to(validators[key], target)
    )
    if wrong_target:
        raise ProfileManifestError(
            f"Profile declared {label} validators for the wrong target: {', '.join(wrong_target)}"
        )


def _validator_applies_to(validator: ValidatorDeclaration, target: ValidatorTarget) -> bool:
    return validator.target == target or target in validator.applies_to
