"""Profile registry helpers."""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import Any

from ..api.nitrado import NitradoService, ParsedServer
from ..profile_logging import log_profile_failure
from ..profile_workers import ProfileWorkerUnavailable, async_run_profile_sync
from .base import (
    EXTENSION_KEY_PATTERN,
    PROFILE_API_VERSION,
    GameProfile,
    ProfileExtensionManifest,
    profile_extension_manifest,
    valid_match_result,
)
from .generic import GenericProfile

_LOGGER = logging.getLogger(__name__)

_EXCLUDED_MODULES = {"base", "generic", "registry"}
PROFILE_DISCOVERY_TIMEOUT_SECONDS = 10.0
PROFILE_MATCH_TIMEOUT_SECONDS = 5.0
PROFILE_REGISTRATION_TIMEOUT_SECONDS = 10.0
_DISCOVERED_PROFILE_FACTORIES: tuple[_ProfileFactory, ...] | None = None
_REGISTERED_PROFILE_FACTORIES: dict[str, _ProfileFactory] = {}
_PROFILE_REGISTRATION_GENERATIONS: dict[str, int] = {}
_PROFILE_REGISTRY_GENERATION = 0
_TIMED_OUT_PROFILE_IDS: set[str] = set()
_TIMED_OUT_MANIFEST_GENERATIONS: set[tuple[str, int, int]] = set()


@dataclass(slots=True, frozen=True)
class _ProfileFactory:
    """Cached profile factory metadata."""

    profile_id: str
    factory: Callable[[], GameProfile]
    generation: int = 0

    def create(self) -> GameProfile:
        """Create and fully validate a fresh profile instance."""

        return self.create_candidate().profile

    def create_candidate(self) -> _ProfileCandidate:
        """Create a profile plus its one immutable validated manifest snapshot."""

        profile = self.factory()
        if getattr(profile, "profile_id", None) != self.profile_id:
            raise ValueError(f"Profile factory {self.profile_id} created an instance with a different identity")
        manifest = _validate_profile_instance(profile, validate_manifest=True)
        if manifest is None:  # pragma: no cover - validate_manifest guarantees a snapshot.
            raise ValueError(f"Profile {self.profile_id} did not produce a manifest")
        return _ProfileCandidate(profile=profile, manifest=manifest)


@dataclass(slots=True, frozen=True)
class _ProfileCandidate:
    """One profile instance bound to its already-validated manifest snapshot."""

    profile: GameProfile
    manifest: ProfileExtensionManifest


@dataclass(slots=True)
class _ProfileRegistration:
    """Reversible registration that preserves the exact vetted factory."""

    factory: _ProfileFactory
    active: bool = True

    def unregister(self) -> None:
        """Remove this exact registration if it is still current."""

        global _PROFILE_REGISTRY_GENERATION
        if self.active and _REGISTERED_PROFILE_FACTORIES.get(self.factory.profile_id) is self.factory:
            _REGISTERED_PROFILE_FACTORIES.pop(self.factory.profile_id, None)
            _PROFILE_REGISTRY_GENERATION += 1
            self.active = False
            clear_discovered_profile_cache()

    def restore(self) -> None:
        """Restore the exact vetted factory without invoking external code again."""

        global _PROFILE_REGISTRY_GENERATION
        if self.active:
            return
        if self.factory.profile_id in _REGISTERED_PROFILE_FACTORIES:
            raise ValueError(f"Profile already registered: {self.factory.profile_id}")
        generation = _PROFILE_REGISTRATION_GENERATIONS.get(self.factory.profile_id, 0) + 1
        _PROFILE_REGISTRATION_GENERATIONS[self.factory.profile_id] = generation
        _PROFILE_REGISTRY_GENERATION += 1
        self.factory = replace(self.factory, generation=generation)
        _REGISTERED_PROFILE_FACTORIES[self.factory.profile_id] = self.factory
        self.active = True
        clear_discovered_profile_cache()


@dataclass(slots=True, frozen=True)
class _PreparedProfileRegistration:
    """One externally supplied factory and the single instance used to vet it."""

    factory: _ProfileFactory
    probe: GameProfile
    manifest: ProfileExtensionManifest


def select_profile(
    service: NitradoService,
    server: ParsedServer | None = None,
    profiles: Iterable[GameProfile] | None = None,
) -> GameProfile:
    """Select the best matching profile for a service."""

    return select_profile_candidate(service, server, profiles).profile


def select_profile_candidate(
    service: NitradoService,
    server: ParsedServer | None = None,
    profiles: Iterable[GameProfile] | None = None,
) -> _ProfileCandidate:
    """Select the best profile while preserving its validated manifest."""

    candidates = (
        tuple(_candidate_from_profile(profile) for profile in profiles)
        if profiles is not None
        else discover_profile_candidates()
    )
    best_candidate: _ProfileCandidate | None = None
    best_confidence = -1.0
    for candidate in candidates:
        profile = candidate.profile
        try:
            result = profile.matches(service, server)
        except Exception as err:  # noqa: BLE001 - isolate arbitrary profile match failures.
            log_profile_failure(
                _LOGGER,
                "match",
                err,
                profile_id=getattr(profile, "profile_id", profile.__class__.__name__),
            )
            continue
        if not valid_match_result(result):
            _LOGGER.warning(
                "Skipping profile %s after invalid match result",
                getattr(profile, "profile_id", profile.__class__.__name__),
            )
            continue
        if result.matched and result.confidence > best_confidence:
            best_candidate = candidate
            best_confidence = result.confidence
    return best_candidate or _candidate_from_profile(GenericProfile())


async def async_select_profile(
    service: NitradoService,
    server: ParsedServer | None = None,
    profiles: Iterable[GameProfile] | None = None,
) -> GameProfile:
    """Select a profile with bounded, isolated third-party discovery/matching."""

    return (await async_select_profile_candidate(service, server, profiles)).profile


async def async_select_profile_candidate(
    service: NitradoService,
    server: ParsedServer | None = None,
    profiles: Iterable[GameProfile] | None = None,
) -> _ProfileCandidate:
    """Select a bounded profile while retaining its validated manifest."""

    registry_generation = profile_registry_generation()
    candidates: list[_ProfileCandidate] = []
    if profiles is not None:
        sources = tuple(
            (
                getattr(profile, "profile_id", profile.__class__.__name__),
                0,
                lambda profile=profile: _candidate_from_profile(profile),
            )
            for profile in profiles
        )
    else:
        sources = tuple(
            (factory.profile_id, factory.generation, factory.create_candidate)
            for factory in _discover_profile_factories()
        )
    for profile_id, registration_generation, create_candidate in sources:
        try:
            candidate = await async_run_profile_sync(
                ("factory", str(profile_id), registration_generation, registry_generation),
                create_candidate,
                timeout=PROFILE_DISCOVERY_TIMEOUT_SECONDS,
            )
        except (TimeoutError, ProfileWorkerUnavailable) as err:
            log_profile_failure(
                _LOGGER,
                "factory_timeout",
                err,
                profile_id=profile_id,
                key=registration_generation,
            )
            continue
        except Exception as err:  # noqa: BLE001 - isolate arbitrary profile factory failures.
            log_profile_failure(
                _LOGGER,
                "factory",
                err,
                profile_id=profile_id,
                key=registration_generation,
            )
            continue
        candidates.append(candidate)

    best_candidate: _ProfileCandidate | None = None
    best_confidence = -1.0
    for candidate in candidates:
        profile = candidate.profile
        profile_id = getattr(profile, "profile_id", profile.__class__.__name__)
        try:
            result = await async_run_profile_sync(
                (
                    "match",
                    str(profile_id),
                    profile_registration_generation(str(profile_id)),
                    registry_generation,
                ),
                profile.matches,
                service,
                server,
                timeout=PROFILE_MATCH_TIMEOUT_SECONDS,
            )
        except (TimeoutError, ProfileWorkerUnavailable) as err:
            log_profile_failure(_LOGGER, "match_timeout", err, profile_id=profile_id)
            continue
        except Exception as err:  # noqa: BLE001 - isolate arbitrary profile match failures.
            log_profile_failure(_LOGGER, "match", err, profile_id=profile_id)
            continue
        if not valid_match_result(result):
            _LOGGER.warning("Skipping profile %s after invalid match result", profile_id)
            continue
        if result.matched and result.confidence > best_confidence:
            best_candidate = candidate
            best_confidence = result.confidence
    return best_candidate or _candidate_from_profile(GenericProfile())


def discover_profiles(*, refresh: bool = False) -> tuple[GameProfile, ...]:
    """Discover profile modules shipped in the plugins package.

    Game modules can expose either ``PROFILE`` or ``PROFILES`` as profile
    classes, no-argument factories, or legacy no-argument instances. Generic is
    always appended last as the low-confidence fallback. Discovery caches
    factories, not instances, so every caller receives fresh profile objects.
    """

    return tuple(candidate.profile for candidate in discover_profile_candidates(refresh=refresh))


def discover_builtin_profiles(*, refresh: bool = False) -> tuple[GameProfile, ...]:
    """Return only profiles shipped by the core integration package.

    External registrations own and bind their assets through their exact
    registration handle; core setup must never reinterpret them as bundled
    files under the core package root.
    """

    return tuple(
        candidate.profile
        for factory in _discover_profile_factories(refresh=refresh)
        if factory.generation == 0
        if (candidate := _safe_candidate(factory)) is not None
    )


def discover_profile_candidates(*, refresh: bool = False) -> tuple[_ProfileCandidate, ...]:
    """Discover fresh profiles bound to one validated manifest snapshot each."""

    factories = _discover_profile_factories(refresh=refresh)
    candidates: list[_ProfileCandidate] = []
    for factory in factories:
        try:
            candidates.append(factory.create_candidate())
        except Exception as err:  # noqa: BLE001 - preserve synchronous compatibility isolation.
            log_profile_failure(_LOGGER, "factory", err, profile_id=factory.profile_id)
    return tuple(candidates)


def _safe_candidate(factory: _ProfileFactory) -> _ProfileCandidate | None:
    """Create one candidate while preserving discovery failure isolation."""

    try:
        return factory.create_candidate()
    except Exception as err:  # noqa: BLE001 - preserve synchronous compatibility isolation.
        log_profile_failure(_LOGGER, "factory", err, profile_id=factory.profile_id)
        return None


def _discover_profile_factories(*, refresh: bool = False) -> tuple[_ProfileFactory, ...]:
    """Discover and cache profile factories."""

    global _DISCOVERED_PROFILE_FACTORIES
    if _DISCOVERED_PROFILE_FACTORIES is not None and not refresh:
        return _DISCOVERED_PROFILE_FACTORIES

    factories: list[_ProfileFactory] = []
    package = importlib.import_module(__package__ or __name__.rsplit(".", 1)[0])
    package_path = getattr(package, "__path__", None)
    if package_path is not None:
        for module_info in pkgutil.iter_modules(package_path, prefix=f"{package.__name__}."):
            short_name = module_info.name.rsplit(".", 1)[-1]
            if short_name in _EXCLUDED_MODULES:
                continue
            try:
                module = importlib.import_module(module_info.name)
            except Exception as err:  # noqa: BLE001 - isolate third-party module import failures.
                log_profile_failure(_LOGGER, "module_import", err, profile_id=module_info.name)
                continue
            factories.extend(_module_profile_factories(module))

    factories.extend(_REGISTERED_PROFILE_FACTORIES.values())
    factories.append(_factory_from_export(GenericProfile))
    _DISCOVERED_PROFILE_FACTORIES = tuple(_dedupe_factories(factories))
    return _DISCOVERED_PROFILE_FACTORIES


def clear_discovered_profile_cache() -> None:
    """Clear cached profile discovery results for tests/developer reloads."""

    global _DISCOVERED_PROFILE_FACTORIES
    _DISCOVERED_PROFILE_FACTORIES = None
    _TIMED_OUT_PROFILE_IDS.clear()


def quarantine_manifest_generation(profile_id: str, registration_generation: int, registry_generation: int) -> None:
    """Quarantine one declaration implementation until registry code changes."""

    _TIMED_OUT_MANIFEST_GENERATIONS.add((str(profile_id), registration_generation, registry_generation))


def manifest_generation_quarantined(
    profile_id: str,
    registration_generation: int,
    registry_generation: int,
) -> bool:
    """Return whether one exact declaration implementation timed out."""

    return (str(profile_id), registration_generation, registry_generation) in _TIMED_OUT_MANIFEST_GENERATIONS


def register_profile(export: Any) -> Callable[[], None]:
    """Register an externally packaged profile class or no-argument factory.

    External integrations may call this during setup without changing this
    integration's package. Each service runtime still receives a fresh profile
    instance. The returned callback unregisters the profile.
    """

    return register_profile_handle(export).unregister


def register_profile_handle(export: Any) -> _ProfileRegistration:
    """Register one export and return an exact reversible internal handle."""

    return commit_profile_registration(prepare_profile_registration(export))


def prepare_profile_registration(export: Any) -> _PreparedProfileRegistration:
    """Instantiate and fully validate an external export exactly once."""

    factory, probe = _factory_and_probe_from_export(export)
    if getattr(probe, "profile_id", None) != factory.profile_id:
        raise ValueError(f"Profile factory identity changed for {factory.profile_id}")
    manifest = _validate_profile_instance(probe, validate_manifest=True)
    if manifest is None:  # pragma: no cover - validation above requires it.
        raise ValueError(f"Profile {factory.profile_id} did not produce a manifest")
    return _PreparedProfileRegistration(factory=factory, probe=probe, manifest=manifest)


def commit_profile_registration(prepared: _PreparedProfileRegistration) -> _ProfileRegistration:
    """Commit one already-vetted registration without invoking external code."""

    factory = prepared.factory
    if factory.profile_id == GenericProfile.profile_id:
        raise ValueError("The reserved generic profile cannot be replaced")
    built_in_ids = {
        candidate.profile_id
        for candidate in _discover_profile_factories()
        if candidate.profile_id not in _REGISTERED_PROFILE_FACTORIES
    }
    if factory.profile_id in built_in_ids:
        raise ValueError(f"Profile ID conflicts with a discovered profile: {factory.profile_id}")
    if factory.profile_id in _REGISTERED_PROFILE_FACTORIES:
        raise ValueError(f"Profile already registered: {factory.profile_id}")

    global _PROFILE_REGISTRY_GENERATION
    generation = _PROFILE_REGISTRATION_GENERATIONS.get(factory.profile_id, 0) + 1
    _PROFILE_REGISTRATION_GENERATIONS[factory.profile_id] = generation
    _PROFILE_REGISTRY_GENERATION += 1
    factory = replace(factory, generation=generation)
    _REGISTERED_PROFILE_FACTORIES[factory.profile_id] = factory
    clear_discovered_profile_cache()

    return _ProfileRegistration(factory)


def profile_registration_generation(profile_id: str | None) -> int:
    """Return the current external registration generation for a profile ID."""

    if not profile_id:
        return 0
    factory = _REGISTERED_PROFILE_FACTORIES.get(profile_id)
    return 0 if factory is None else factory.generation


def profile_registry_generation() -> int:
    """Return the generation of the complete external profile registry."""

    return _PROFILE_REGISTRY_GENERATION


def _module_profile_factories(module: Any) -> tuple[_ProfileFactory, ...]:
    """Return explicit profile factories exported by a plugin module."""

    if hasattr(module, "PROFILES"):
        value = module.PROFILES
        if isinstance(value, tuple | list):
            return tuple(
                factory for item in value if (factory := _safe_factory_from_export(item, module=module)) is not None
            )
    if hasattr(module, "PROFILE"):
        factory = _safe_factory_from_export(module.PROFILE, module=module)
        return () if factory is None else (factory,)
    return ()


def _safe_factory_from_export(value: Any, *, module: Any) -> _ProfileFactory | None:
    """Normalize one module export, logging and skipping broken declarations."""

    try:
        return _factory_from_export(value)
    except Exception as err:  # noqa: BLE001 - isolate arbitrary profile export failures.
        log_profile_failure(_LOGGER, "export", err, profile_id=getattr(module, "__name__", module))
        return None


def _factory_from_export(value: Any) -> _ProfileFactory:
    """Normalize one profile export into a no-argument factory."""

    if isinstance(value, type):
        profile_id = getattr(value, "profile_id", None)
        if not isinstance(profile_id, str) or not EXTENSION_KEY_PATTERN.fullmatch(profile_id):
            raise ValueError(f"Profile class {value!r} does not declare a valid profile_id")
        return _ProfileFactory(profile_id=profile_id, factory=value)

    if callable(value) and not hasattr(value, "matches"):
        profile = value()
        profile_id = getattr(profile, "profile_id", None)
        if not isinstance(profile_id, str) or not EXTENSION_KEY_PATTERN.fullmatch(profile_id):
            raise ValueError(f"Profile factory {value!r} did not create a profile with a valid profile_id")
        return _ProfileFactory(profile_id=profile_id, factory=value)

    profile_id = getattr(value, "profile_id", None)
    if not isinstance(profile_id, str) or not EXTENSION_KEY_PATTERN.fullmatch(profile_id):
        raise ValueError(f"Profile export {value!r} does not declare a valid profile_id")
    profile_type = value.__class__

    def create() -> GameProfile:
        return profile_type()

    return _ProfileFactory(profile_id=profile_id, factory=create)


def _factory_and_probe_from_export(value: Any) -> tuple[_ProfileFactory, GameProfile]:
    """Normalize one export and return the single instance used for inspection."""

    if isinstance(value, type):
        profile_id = getattr(value, "profile_id", None)
        if not isinstance(profile_id, str) or not EXTENSION_KEY_PATTERN.fullmatch(profile_id):
            raise ValueError(f"Profile class {value!r} does not declare a valid profile_id")
        return _ProfileFactory(profile_id=profile_id, factory=value), value()

    if callable(value) and not hasattr(value, "matches"):
        profile = value()
        profile_id = getattr(profile, "profile_id", None)
        if not isinstance(profile_id, str) or not EXTENSION_KEY_PATTERN.fullmatch(profile_id):
            raise ValueError(f"Profile factory {value!r} did not create a profile with a valid profile_id")
        return _ProfileFactory(profile_id=profile_id, factory=value), profile

    profile_id = getattr(value, "profile_id", None)
    if not isinstance(profile_id, str) or not EXTENSION_KEY_PATTERN.fullmatch(profile_id):
        raise ValueError(f"Profile export {value!r} does not declare a valid profile_id")
    profile_type = value.__class__

    def create() -> GameProfile:
        return profile_type()

    return _ProfileFactory(profile_id=profile_id, factory=create), value


def _candidate_from_profile(profile: GameProfile) -> _ProfileCandidate:
    """Validate an explicit profile and capture exactly one manifest snapshot."""

    manifest = _validate_profile_instance(profile, validate_manifest=True)
    if manifest is None:  # pragma: no cover - validate_manifest guarantees a snapshot.
        raise ValueError(f"Profile {getattr(profile, 'profile_id', '<unknown>')} did not produce a manifest")
    return _ProfileCandidate(profile=profile, manifest=manifest)


def _validate_profile_instance(
    profile: Any,
    *,
    validate_manifest: bool = False,
) -> ProfileExtensionManifest | None:
    """Validate the public profile protocol before core can select it."""

    profile_id = getattr(profile, "profile_id", None)
    if not isinstance(profile_id, str) or not EXTENSION_KEY_PATTERN.fullmatch(profile_id):
        raise ValueError("Profile declares an invalid profile_id")
    api_version = getattr(profile, "api_version", None)
    if api_version != PROFILE_API_VERSION:
        raise ValueError(
            f"Profile {profile_id} targets API version {api_version!r}; core requires {PROFILE_API_VERSION}"
        )
    name = getattr(profile, "name", None)
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"Profile {profile_id} does not declare name")
    supported_games = getattr(profile, "supported_games", None)
    if not isinstance(supported_games, tuple) or not all(
        isinstance(game, str) and game.strip() for game in supported_games
    ):
        raise ValueError(f"Profile {profile_id} does not declare valid supported_games")
    if not isinstance(getattr(profile, "idle_shutdown_supported", None), bool):
        raise ValueError(f"Profile {profile_id} declares invalid idle_shutdown_supported")
    required_methods = (
        "matches",
        "enrich_status",
        "suggest_display_name",
        "can_start",
        "can_stop",
        "idle_shutdown_capability",
        "extra_entities",
        "editable_files",
        "resources",
        "actions",
        "surfaces",
        "lifecycle_hooks",
        "validators",
    )
    missing = tuple(name for name in required_methods if not callable(getattr(profile, name, None)))
    if missing:
        raise ValueError(f"Profile {profile_id} is missing required methods: {', '.join(missing)}")
    for name in (
        "matches",
        "idle_shutdown_capability",
        "extra_entities",
        "editable_files",
        "resources",
        "actions",
        "surfaces",
        "lifecycle_hooks",
        "validators",
    ):
        if inspect.iscoroutinefunction(getattr(profile, name)):
            raise ValueError(f"Profile {profile_id} declares async {name}; this contract is synchronous")
    if validate_manifest:
        return profile_extension_manifest(profile)
    return None


def _dedupe_factories(factories: Iterable[_ProfileFactory]) -> tuple[_ProfileFactory, ...]:
    """Dedupe profile factories by profile ID while preserving discovery order."""

    seen: set[str] = set()
    result: list[_ProfileFactory] = []
    for factory in factories:
        if factory.profile_id in seen:
            continue
        seen.add(factory.profile_id)
        result.append(factory)
    return tuple(result)
