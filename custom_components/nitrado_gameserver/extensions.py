"""Generic profile extension dispatch helpers."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any

from .api.nitrado import NitradoClient
from .plugins.base import (
    SUPPORTED,
    CapabilityState,
    CapabilityVerdict,
    DataSource,
    ExtensionAccess,
    GameProfile,
    LifecycleEvent,
    ProfileActionContext,
    ProfileEntityContext,
    ProfileManifestError,
    ResourceContentFamily,
    ResourceDeclaration,
    SurfaceDeclaration,
    ValidatorDeclaration,
    ValidatorDomain,
    ValidatorTarget,
    async_invoke_profile,
    blocked,
    profile_read_transport,
    valid_capability_verdict,
    validate_action_payload,
)
from .plugins.registry import profile_registry_generation
from .profile_logging import log_profile_failure
from .profile_workers import async_run_profile_sync
from .runtime import ServiceRuntime, runtime_profile_extension_manifest

_LOGGER = logging.getLogger(__name__)


class ProfileExtensionError(Exception):
    """Raised when a profile extension cannot be dispatched safely."""

    def __init__(self, verdict: CapabilityVerdict) -> None:
        super().__init__(verdict.reason or verdict.state.value)
        self.verdict = verdict


MAX_RESOURCE_CACHE_ENTRIES = 64
MAX_RESOURCE_CACHE_BYTES = 33_554_432
MAX_EXTENSION_RESULT_BYTES = 1_048_576
MAX_BINARY_RESOURCE_BYTES = 16_777_216
MAX_SURFACE_RESOURCE_BYTES = 16_777_216


@dataclass(slots=True, frozen=True)
class ResourceResult:
    """Result returned by a profile resource fetch."""

    key: str
    name: str
    content_type: str
    content_family: ResourceContentFamily
    data: Any
    fetched_at: int
    cached: bool = False
    cache_seconds: int = 0
    attributes: dict[str, Any] | None = None
    cache_bytes: int = 0


@dataclass(slots=True, frozen=True)
class ResourceDescriptor:
    """Serializable-ish metadata for one profile-declared resource."""

    key: str
    name: str
    content_type: str
    content_family: ResourceContentFamily
    description: str = ""
    cache_seconds: int = 0
    validators: tuple[str, ...] = ()
    access: ExtensionAccess = ExtensionAccess.AUTHENTICATED
    attributes: dict[str, Any] | None = None


@dataclass(slots=True, frozen=True)
class SurfaceControlDescriptor:
    """Resolved metadata for one profile-declared HA control referenced by a surface."""

    key: str
    entity_key: str
    platform: str
    name: str
    kind: str | None = None
    attributes: dict[str, Any] | None = None


@dataclass(slots=True, frozen=True)
class SurfaceDescriptor:
    """Metadata for a profile-declared surface composition."""

    key: str
    name: str
    description: str = ""
    resources: tuple[ResourceDescriptor, ...] = ()
    actions: tuple[str, ...] = ()
    controls: tuple[str, ...] = ()
    control_entities: tuple[SurfaceControlDescriptor, ...] = ()
    editable_files: tuple[str, ...] = ()
    renderer_hint: str | None = None
    validators: tuple[str, ...] = ()
    access: ExtensionAccess = ExtensionAccess.AUTHENTICATED
    attributes: dict[str, Any] | None = None


@dataclass(slots=True, frozen=True)
class SurfaceResourceBundle:
    """Fetched resources for one declared surface."""

    surface: SurfaceDescriptor
    resources: tuple[ResourceResult, ...]


@dataclass(slots=True, frozen=True)
class LifecycleHookResult:
    """Result returned by one lifecycle hook dispatch."""

    key: str
    event: LifecycleEvent
    result: Any
    verdict: CapabilityVerdict | None = None


def profile_action_context(
    client: NitradoClient,
    runtime: ServiceRuntime,
    *,
    force: bool = False,
    now: int | None = None,
    payload: Any | None = None,
) -> ProfileActionContext:
    """Build a controlled profile handler context from current runtime state."""

    service_id = runtime.state.identity.service_id
    return ProfileActionContext(
        client=profile_read_transport(client, service_id),
        service=runtime.service,
        server=runtime.server,
        status_fresh=runtime.status_fresh,
        using_cached_data=runtime.using_cached_data,
        force=force,
        now=now,
        payload=payload,
        extra=runtime.profile_extra(),
        options=MappingProxyType(dict(runtime.extra.get("_persisted_profile_options", {}))),
    )


def profile_entity_context(runtime: ServiceRuntime) -> ProfileEntityContext:
    """Build a passive profile entity context from current runtime state."""

    return ProfileEntityContext(
        service=runtime.service,
        server=runtime.server,
        status_fresh=runtime.status_fresh,
        using_cached_data=runtime.using_cached_data,
        extra=MappingProxyType(runtime.profile_extra()),
        options=MappingProxyType(dict(runtime.extra.get("_persisted_profile_options", {}))),
    )


async def async_validate_profile(
    runtime: ServiceRuntime,
    client: NitradoClient,
    *,
    target: ValidatorTarget,
    validator_keys: tuple[str, ...] | None = None,
    domains: tuple[ValidatorDomain, ...] | None = None,
    payload: Any | None = None,
    force: bool = False,
    now: int | None = None,
) -> CapabilityVerdict:
    """Run profile validators and return the first blocking verdict."""

    profile = runtime.profile
    if profile is None:
        return SUPPORTED
    snapshot = (runtime.profile_generation, runtime.profile_registry_generation_seen)
    manifest = _manifest_or_raise(runtime)
    validators = manifest.validators
    if validator_keys is not None:
        wanted = set(validator_keys)
        validators = tuple(validator for validator in validators if validator.key in wanted)
        missing = wanted - {validator.key for validator in validators}
        if missing:
            return blocked(
                f"Profile validator not found: {', '.join(sorted(missing))}",
                source=DataSource.PROFILE,
            )
        wrong_target = tuple(validator for validator in validators if not _validator_applies_to(validator, target))
        if wrong_target:
            details = ", ".join(
                f"{validator.key} is {validator.target.value}"
                for validator in sorted(wrong_target, key=lambda item: item.key)
            )
            return blocked(
                f"Profile validator target mismatch for {target.value}: {details}",
                source=DataSource.PROFILE,
            )
    else:
        validators = tuple(validator for validator in validators if _validator_applies_to(validator, target))
        if domains is not None:
            validators = tuple(validator for validator in validators if _validator_matches_domains(validator, domains))

    context = profile_action_context(client, runtime, force=force, now=now, payload=payload)
    for validator in validators:
        if not _validator_applies_to(validator, target) and validator_keys is None:
            continue
        _require_profile_snapshot(runtime, profile, snapshot)
        try:
            verdict = await async_invoke_profile(validator.validate_fn, context, payload)
        except Exception as err:  # noqa: BLE001 - isolate arbitrary profile validator failures.
            log_profile_failure(_LOGGER, "validator", err, profile_id=manifest.profile_id, key=validator.key)
            return blocked(f"Profile validator {validator.key} failed safely; details were logged.")
        _require_profile_snapshot(runtime, profile, snapshot)
        if not valid_capability_verdict(verdict):
            return blocked(f"Profile validator {validator.key} returned an invalid verdict")
        if not verdict.allowed:
            if force and verdict.overridable:
                continue
            return verdict
    _require_profile_snapshot(runtime, profile, snapshot)
    return SUPPORTED


async def async_run_profile_action(
    runtime: ServiceRuntime,
    client: NitradoClient,
    action_key: str,
    *,
    payload: Any | None = None,
    confirmed: bool = False,
    now: int | None = None,
) -> Any:
    """Dispatch a profile-declared action by key."""

    profile, generation = _profile_snapshot(runtime)
    manifest = _manifest_or_raise(runtime)
    action = next((action for action in manifest.actions if action.key == action_key), None)
    if action is None:
        raise ProfileExtensionError(blocked(f"Profile action not found: {action_key}"))
    if action.requires_confirmation and not confirmed:
        raise ProfileExtensionError(blocked(f"Profile action requires explicit confirmation: {action_key}"))
    try:
        payload = validate_action_payload(action, payload)
    except ValueError as err:
        raise ProfileExtensionError(blocked(f"Invalid payload for profile action {action_key}: {err}")) from None

    validation = await async_validate_profile(
        runtime,
        client,
        target=ValidatorTarget.ACTION,
        validator_keys=action.validators,
        payload=payload,
        now=now,
    )
    if not validation.allowed:
        raise ProfileExtensionError(validation)
    _require_profile_snapshot(runtime, profile, generation)

    context = profile_action_context(client, runtime, now=now, payload=payload)
    try:
        result = await async_invoke_profile(action.action_fn, context, require_async=True)
    except ProfileExtensionError:
        raise
    except Exception as err:  # noqa: BLE001 - untrusted profile boundary
        log_profile_failure(_LOGGER, "action", err, profile_id=manifest.profile_id, key=action.key)
        raise ProfileExtensionError(
            blocked(f"Profile action {action.key} failed safely; details were logged.", source=DataSource.PROFILE)
        ) from None
    if isinstance(result, CapabilityVerdict) and not valid_capability_verdict(result):
        raise ProfileExtensionError(blocked(f"Profile action {action.key} returned an invalid verdict"))
    if isinstance(result, CapabilityVerdict) and not result.allowed:
        raise ProfileExtensionError(result)
    if isinstance(result, CapabilityVerdict):
        result = _capability_verdict_payload(result)
    result = await async_run_profile_sync(
        ("action_normalize", manifest.profile_id, *generation, action.key),
        _normalize_json_result,
        result,
        f"Profile action {action.key} result",
        timeout=15.0,
    )
    _require_profile_snapshot(runtime, profile, generation)
    return result


async def async_fetch_profile_resource(
    runtime: ServiceRuntime,
    client: NitradoClient,
    resource_key: str,
    *,
    payload: Any | None = None,
    now: int | None = None,
    use_cache: bool = True,
) -> ResourceResult:
    """Fetch a profile-declared resource with simple per-runtime caching."""

    profile, generation = _profile_snapshot(runtime)
    manifest = _manifest_or_raise(runtime)
    resource = next((resource for resource in manifest.resources if resource.key == resource_key), None)
    if resource is None:
        raise ProfileExtensionError(blocked(f"Profile resource not found: {resource_key}"))

    effective_now = int(now or 0)
    validation = await async_validate_profile(
        runtime,
        client,
        target=ValidatorTarget.RESOURCE,
        validator_keys=resource.validators,
        payload=payload,
        now=now,
    )
    if not validation.allowed:
        raise ProfileExtensionError(validation)
    _require_profile_snapshot(runtime, profile, generation)

    cache_key = _resource_cache_key(manifest.profile_id, generation[0], resource.key, payload)
    cache = _resource_cache(runtime)
    cached = cache.get(cache_key)
    if use_cache and cached is not None and _cache_valid(cached, effective_now):
        cache.pop(cache_key, None)
        cache[cache_key] = cached
        _require_profile_snapshot(runtime, profile, generation)
        return replace(cached, cached=True)
    if cached is not None:
        cache.pop(cache_key, None)

    context = profile_action_context(client, runtime, now=now, payload=payload)
    try:
        data = await async_invoke_profile(resource.fetch_fn, context)
    except ProfileExtensionError:
        raise
    except Exception as err:  # noqa: BLE001 - untrusted profile boundary
        log_profile_failure(_LOGGER, "resource", err, profile_id=manifest.profile_id, key=resource.key)
        raise ProfileExtensionError(
            blocked(f"Profile resource {resource.key} failed safely; details were logged.", source=DataSource.PROFILE)
        ) from None
    _require_profile_snapshot(runtime, profile, generation)
    data = await async_run_profile_sync(
        ("resource_normalize", manifest.profile_id, *generation, resource.key),
        _normalize_resource_data,
        resource,
        data,
        timeout=15.0,
    )
    result = ResourceResult(
        key=resource.key,
        name=resource.name,
        content_type=resource.content_type,
        content_family=resource_content_family(resource),
        data=data,
        fetched_at=effective_now,
        cached=False,
        cache_seconds=max(0, int(resource.cache_seconds)),
        attributes=dict(resource.attributes),
    )
    if result.cache_seconds > 0:
        result = replace(
            result,
            cache_bytes=await async_run_profile_sync(
                ("resource_cache_size", manifest.profile_id, *generation, resource.key),
                _resource_cache_entry_bytes,
                cache_key,
                result,
                timeout=15.0,
            ),
        )
        cache[cache_key] = result
        _trim_resource_cache(cache)
    return result


def _profile_snapshot(runtime: ServiceRuntime) -> tuple[GameProfile, tuple[int, int]]:
    """Capture profile identity plus runtime and whole-registry generations."""

    profile = _profile_or_raise(runtime)
    return profile, (runtime.profile_generation, runtime.profile_registry_generation_seen)


def _require_profile_snapshot(
    runtime: ServiceRuntime,
    profile: GameProfile,
    generation: tuple[int, int],
) -> None:
    """Abort when profile selection changed during one extension operation."""

    profile_generation, registry_generation = generation
    if (
        runtime.profile is not profile
        or runtime.profile_generation != profile_generation
        or runtime.profile_registry_generation_seen != registry_generation
        or registry_generation != profile_registry_generation()
    ):
        raise ProfileExtensionError(
            blocked("The selected game profile changed during the operation; retry against the current profile.")
        )


def _normalize_resource_data(resource: ResourceDeclaration, data: Any) -> Any:
    """Validate and detach resource data outside Home Assistant's event loop."""

    family = resource_content_family(resource)
    valid = True
    if family == ResourceContentFamily.JSON:
        try:
            encoded = json.dumps(data, allow_nan=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError, OverflowError):
            valid = False
        else:
            valid = len(encoded) <= MAX_EXTENSION_RESULT_BYTES
            if valid:
                data = json.loads(encoded)
    elif family == ResourceContentFamily.TEXT:
        valid = type(data) is str
        if valid:
            valid = len(data.encode("utf-8")) <= MAX_EXTENSION_RESULT_BYTES
    elif family in {ResourceContentFamily.IMAGE, ResourceContentFamily.BINARY}:
        valid = type(data) in {bytes, bytearray, memoryview}
        if valid:
            valid = len(data) <= MAX_BINARY_RESOURCE_BYTES
            if valid:
                data = bytes(data)
    elif family == ResourceContentFamily.STREAM:
        valid = hasattr(data, "__aiter__")
    elif family == ResourceContentFamily.UNKNOWN:
        try:
            _require_bounded_json(data, f"Profile resource {resource.key} result")
        except ProfileExtensionError:
            valid = False
        else:
            data = json.loads(json.dumps(data, allow_nan=False, separators=(",", ":")))
    if not valid:
        raise ProfileExtensionError(
            blocked(f"Profile resource {resource.key} returned data incompatible with {family.value} content.")
        )
    return data


def _normalize_json_result(result: Any, label: str) -> Any:
    """Validate and detach one bounded action result outside HA's loop."""

    try:
        encoded = json.dumps(result, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as err:
        raise ProfileExtensionError(blocked(f"{label} was not JSON serializable")) from err
    if len(encoded) > MAX_EXTENSION_RESULT_BYTES:
        raise ProfileExtensionError(blocked(f"{label} exceeded the supported size"))
    return json.loads(encoded)


def _resource_result_size(result: ResourceResult) -> int:
    """Return a conservative serialized size for aggregate surface limits."""

    data = result.data
    if isinstance(data, (bytes, bytearray, memoryview)):
        return len(data)
    if hasattr(data, "__aiter__"):
        raise ProfileExtensionError(
            blocked(
                f"Stream resource {result.key} cannot be bundled into a surface response.",
                source=DataSource.PROFILE,
            )
        )
    try:
        return len(json.dumps(data, allow_nan=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError, OverflowError):
        raise ProfileExtensionError(
            blocked(f"Profile resource {result.key} cannot be serialized safely.", source=DataSource.PROFILE)
        ) from None


def _require_bounded_json(value: Any, label: str) -> None:
    """Require finite, bounded JSON at an extension output boundary."""

    try:
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        raise ProfileExtensionError(blocked(f"{label} was not finite JSON data.")) from None
    if len(encoded) > MAX_EXTENSION_RESULT_BYTES:
        raise ProfileExtensionError(blocked(f"{label} exceeded the 1 MiB output limit."))


def _capability_verdict_payload(verdict: CapabilityVerdict) -> dict[str, Any]:
    """Normalize an allowed action verdict into stable JSON-safe output."""

    return {
        "state": verdict.state.value,
        "reason": verdict.reason,
        "source": verdict.source.value,
        "overridable": verdict.overridable,
    }


async def async_fetch_profile_surface_resources(
    runtime: ServiceRuntime,
    client: NitradoClient,
    surface_key: str,
    *,
    payload: Any | None = None,
    now: int | None = None,
    use_cache: bool = True,
) -> SurfaceResourceBundle:
    """Fetch all resources composed by a profile-declared surface."""

    profile, generation = _profile_snapshot(runtime)
    manifest = _manifest_or_raise(runtime)
    declaration = next((surface for surface in manifest.surfaces if surface.key == surface_key), None)
    if declaration is None:
        raise ProfileExtensionError(blocked(f"Profile surface not found: {surface_key}"))
    surface = _surface_descriptor(declaration, manifest)

    validation = await async_validate_profile(
        runtime,
        client,
        target=ValidatorTarget.SURFACE,
        validator_keys=declaration.validators,
        payload=payload,
        now=now,
    )
    if not validation.allowed:
        raise ProfileExtensionError(validation)
    _require_profile_snapshot(runtime, profile, generation)

    resources: list[ResourceResult] = []
    aggregate_bytes = 0
    for resource in surface.resources:
        result = await async_fetch_profile_resource(
            runtime,
            client,
            resource.key,
            payload=payload,
            now=now,
            use_cache=use_cache,
        )
        aggregate_bytes += _resource_result_size(result)
        if aggregate_bytes > MAX_SURFACE_RESOURCE_BYTES:
            raise ProfileExtensionError(
                blocked(
                    f"Profile surface {surface.key} resources exceed the aggregate output limit.",
                    source=DataSource.PROFILE,
                )
            )
        resources.append(result)
    _require_profile_snapshot(runtime, profile, generation)
    return SurfaceResourceBundle(surface=surface, resources=tuple(resources))


async def async_run_lifecycle_hooks(
    runtime: ServiceRuntime,
    client: NitradoClient,
    event: LifecycleEvent,
    *,
    payload: Any | None = None,
    force: bool = False,
    raise_blocking: bool = True,
    now: int | None = None,
) -> tuple[LifecycleHookResult, ...]:
    """Run profile-declared lifecycle hooks for an event."""

    profile = runtime.profile
    if profile is None:
        return ()
    generation = (runtime.profile_generation, runtime.profile_registry_generation_seen)
    manifest = _manifest_or_raise(runtime)
    hooks = sorted(
        (hook for hook in manifest.lifecycle_hooks if hook.event == event),
        key=lambda hook: (hook.order, hook.key),
    )
    results: list[LifecycleHookResult] = []

    def require_current_profile(hook_key: str) -> bool:
        """Keep pre-mutation hooks strict and contain stale post-hook results."""

        try:
            _require_profile_snapshot(runtime, profile, generation)
        except ProfileExtensionError as err:
            if raise_blocking:
                raise
            results.append(LifecycleHookResult(hook_key, event, err, err.verdict))
            _record_lifecycle_results(runtime, event, tuple(results))
            _LOGGER.warning(
                "Ignoring stale nonblocking %s lifecycle result after the selected profile changed",
                event.value,
            )
            return False
        return True

    for hook in hooks:
        try:
            validation = await async_validate_profile(
                runtime,
                client,
                target=ValidatorTarget.LIFECYCLE_HOOK,
                validator_keys=hook.validators,
                payload=payload,
                force=force,
                now=now,
            )
        except Exception as err:  # noqa: BLE001 - untrusted profile boundary
            verdict = _hook_exception_verdict(hook.key, err)
            log_profile_failure(
                _LOGGER,
                "lifecycle_validation",
                err,
                profile_id=manifest.profile_id,
                key=hook.key,
            )
            if hook.blocking and raise_blocking:
                results.append(LifecycleHookResult(hook.key, event, None, verdict))
                _record_lifecycle_results(runtime, event, tuple(results))
                raise ProfileExtensionError(verdict) from None
            results.append(LifecycleHookResult(hook.key, event, None, verdict))
            continue
        if not require_current_profile(hook.key):
            break
        if not validation.allowed:
            if hook.blocking and raise_blocking:
                results.append(LifecycleHookResult(hook.key, event, validation, validation))
                _record_lifecycle_results(runtime, event, tuple(results))
                raise ProfileExtensionError(validation)
            _LOGGER.warning(
                "Non-fatal profile lifecycle validation block profile=%s key=%s state=%s",
                manifest.profile_id,
                hook.key,
                validation.state.value,
            )
            results.append(LifecycleHookResult(hook.key, event, validation, validation))
            continue
        context = profile_action_context(client, runtime, force=force, now=now, payload=payload)
        try:
            result = await async_invoke_profile(hook.hook_fn, context, require_async=True)
        except Exception as err:  # noqa: BLE001 - untrusted profile boundary
            verdict = _hook_exception_verdict(hook.key, err)
            log_profile_failure(_LOGGER, "lifecycle", err, profile_id=manifest.profile_id, key=hook.key)
            if hook.blocking and raise_blocking:
                results.append(LifecycleHookResult(hook.key, event, None, verdict))
                _record_lifecycle_results(runtime, event, tuple(results))
                raise ProfileExtensionError(verdict) from None
            results.append(LifecycleHookResult(hook.key, event, None, verdict))
            continue
        if not require_current_profile(hook.key):
            break
        if isinstance(result, CapabilityVerdict) and not valid_capability_verdict(result):
            verdict = blocked(f"Profile lifecycle hook {hook.key} returned an invalid verdict")
        else:
            verdict = result if isinstance(result, CapabilityVerdict) else None
        if (
            verdict is not None
            and not verdict.allowed
            and hook.blocking
            and raise_blocking
            and not (force and verdict.overridable)
        ):
            results.append(LifecycleHookResult(hook.key, event, result, verdict))
            _record_lifecycle_results(runtime, event, tuple(results))
            raise ProfileExtensionError(verdict)
        if verdict is not None and not verdict.allowed and hook.blocking and not raise_blocking:
            _LOGGER.warning(
                "Non-fatal profile lifecycle hook block profile=%s key=%s state=%s",
                manifest.profile_id,
                hook.key,
                verdict.state.value,
            )
        results.append(LifecycleHookResult(hook.key, event, result, verdict))
    _record_lifecycle_results(runtime, event, tuple(results))
    return tuple(results)


def profile_resource_descriptors(runtime: ServiceRuntime) -> tuple[ResourceDescriptor, ...]:
    """Return generic metadata for resources exposed by the selected profile."""

    profile = runtime.profile
    if profile is None:
        return ()
    manifest = _manifest_or_raise(runtime)
    return tuple(resource_descriptor(resource) for resource in manifest.resources)


def profile_surface_descriptors(runtime: ServiceRuntime) -> tuple[SurfaceDescriptor, ...]:
    """Return generic metadata for surfaces exposed by the selected profile."""

    profile = runtime.profile
    if profile is None:
        return ()
    manifest = _manifest_or_raise(runtime)
    return tuple(_surface_descriptor(surface, manifest) for surface in manifest.surfaces)


def profile_surface_descriptor(runtime: ServiceRuntime, surface_key: str) -> SurfaceDescriptor:
    """Return one profile-declared surface descriptor by key."""

    _profile_or_raise(runtime)
    manifest = _manifest_or_raise(runtime)
    surface = next((surface for surface in manifest.surfaces if surface.key == surface_key), None)
    if surface is None:
        raise ProfileExtensionError(blocked(f"Profile surface not found: {surface_key}"))
    return _surface_descriptor(surface, manifest)


def resource_descriptor(resource: ResourceDeclaration) -> ResourceDescriptor:
    """Return generic metadata for one resource declaration."""

    return ResourceDescriptor(
        key=resource.key,
        name=resource.name,
        content_type=resource.content_type,
        content_family=resource_content_family(resource),
        description=resource.description,
        cache_seconds=max(0, int(resource.cache_seconds)),
        validators=tuple(resource.validators),
        access=resource.access,
        attributes=dict(resource.attributes),
    )


def resource_content_family(resource: ResourceDeclaration | str) -> ResourceContentFamily:
    """Classify a content type into the broad families generic core supports."""

    if isinstance(resource, ResourceDeclaration):
        if resource.content_family is not None:
            return resource.content_family
        content_type = resource.content_type
    else:
        content_type = resource

    media_type = (content_type or "").split(";", 1)[0].strip().lower()
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


def _profile_or_raise(runtime: ServiceRuntime):
    profile = runtime.profile
    if profile is None:
        raise ProfileExtensionError(
            CapabilityVerdict(
                CapabilityState.UNSUPPORTED,
                reason="No game profile is selected for this service.",
                source=DataSource.PROFILE,
            )
        )
    return profile


def _resource_cache(runtime: ServiceRuntime) -> dict[tuple[str, int, str, str], ResourceResult]:
    cache = runtime.extra.setdefault("_profile_resource_cache", {})
    if not isinstance(cache, dict):
        cache = {}
        runtime.extra["_profile_resource_cache"] = cache
    return cache


def _manifest_or_raise(runtime: ServiceRuntime) -> Any:
    """Return a valid profile extension manifest or raise a structured extension error."""

    try:
        return runtime_profile_extension_manifest(runtime)
    except ProfileManifestError as err:
        log_profile_failure(_LOGGER, "manifest", err)
        raise ProfileExtensionError(
            blocked(
                "The selected game profile manifest is invalid; details were logged.",
                source=DataSource.PROFILE,
            )
        ) from None


def _validator_applies_to(validator: ValidatorDeclaration, target: ValidatorTarget) -> bool:
    """Return whether a validator may be used for one dispatch target."""

    return validator.target == target or target in validator.applies_to


def _validator_matches_domains(
    validator: ValidatorDeclaration,
    domains: tuple[ValidatorDomain, ...],
) -> bool:
    """Return whether a validator belongs to one requested semantic domain."""

    wanted = set(domains)
    return bool(wanted.intersection(validator.domains))


def _hook_exception_verdict(hook_key: str, err: Exception) -> CapabilityVerdict:
    """Convert a profile lifecycle exception into a structured hook verdict."""

    return blocked(
        f"Profile lifecycle hook {hook_key} failed safely; details were logged.",
        source=DataSource.PROFILE,
    )


def _record_lifecycle_results(
    runtime: ServiceRuntime,
    event: LifecycleEvent,
    results: tuple[LifecycleHookResult, ...],
) -> None:
    """Store the last lifecycle hook results as diagnostics-friendly data."""

    runtime.last_lifecycle_hook_results[event.value] = tuple(_lifecycle_hook_report(result) for result in results)


def _lifecycle_hook_report(result: LifecycleHookResult) -> dict[str, Any]:
    """Return a serializable summary for one lifecycle hook result."""

    verdict = result.verdict
    return {
        "key": result.key,
        "event": result.event.value,
        "result_type": result.result.__class__.__name__ if result.result is not None else None,
        "verdict": None
        if verdict is None
        else {
            "state": verdict.state.value,
            "reason": verdict.reason,
            "source": verdict.source.value,
            "overridable": verdict.overridable,
        },
    }


def _cache_valid(result: ResourceResult, now: int) -> bool:
    if result.cache_seconds <= 0:
        return False
    return now - result.fetched_at < result.cache_seconds


def _resource_cache_key(
    profile_id: str,
    profile_generation: int,
    resource_key: str,
    payload: Any | None,
) -> tuple[str, int, str, str]:
    """Return a stable cache key for a resource request variant."""

    if payload is None:
        return (profile_id, profile_generation, resource_key, "")
    try:
        token = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=repr)
    except (TypeError, ValueError):
        token = repr(payload)
    return (profile_id, profile_generation, resource_key, token)


def _trim_resource_cache(cache: dict[tuple[str, int, str, str], ResourceResult]) -> None:
    """Keep per-runtime resource caches bounded."""

    while len(cache) > MAX_RESOURCE_CACHE_ENTRIES or _resource_cache_bytes(cache) > MAX_RESOURCE_CACHE_BYTES:
        oldest_key = next(iter(cache))
        cache.pop(oldest_key, None)


def _resource_cache_bytes(cache: dict[tuple[str, int, str, str], ResourceResult]) -> int:
    """Return a conservative serialized-size estimate for one runtime cache."""

    return sum(result.cache_bytes or _resource_cache_entry_bytes(key, result) for key, result in cache.items())


def _resource_cache_entry_bytes(key: tuple[str, int, str, str], result: ResourceResult) -> int:
    """Compute one detached cache entry's byte cost outside the event loop."""

    total = sum(len(str(part).encode("utf-8")) for part in key)
    total += len(json.dumps(result.attributes, sort_keys=True, default=repr).encode("utf-8"))
    if isinstance(result.data, str):
        total += len(result.data.encode("utf-8"))
    elif isinstance(result.data, bytes):
        total += len(result.data)
    else:
        total += len(json.dumps(result.data, sort_keys=True, default=repr).encode("utf-8"))
    return total


def _surface_descriptor(surface: SurfaceDeclaration, manifest) -> SurfaceDescriptor:
    resources_by_key = {resource.key: resource for resource in manifest.resources}
    controls_by_key = {entity.key: entity for entity in manifest.entities}
    resources = tuple(
        resource_descriptor(resources_by_key[key]) for key in surface.resources if key in resources_by_key
    )
    return SurfaceDescriptor(
        key=surface.key,
        name=surface.name,
        description=surface.description,
        resources=resources,
        actions=tuple(surface.actions),
        controls=tuple(surface.controls),
        control_entities=tuple(
            _surface_control_descriptor(manifest.profile_id, controls_by_key[key])
            for key in surface.controls
            if key in controls_by_key
        ),
        editable_files=tuple(surface.editable_files),
        renderer_hint=surface.renderer_hint,
        validators=tuple(surface.validators),
        access=surface.access,
        attributes=dict(surface.attributes),
    )


def _surface_control_descriptor(profile_id: str, declaration) -> SurfaceControlDescriptor:
    entity_key = declaration.key
    if not entity_key.startswith(f"{profile_id}_"):
        entity_key = f"{profile_id}_{entity_key}"
    return SurfaceControlDescriptor(
        key=declaration.key,
        entity_key=entity_key,
        platform=declaration.platform,
        name=declaration.name,
        kind=declaration.kind,
        attributes=dict(declaration.attributes),
    )
