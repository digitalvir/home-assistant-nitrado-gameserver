"""Tests for game profile helpers."""

from __future__ import annotations

import asyncio
import json
import sys
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from custom_components.nitrado_gameserver.api.nitrado import (
    NitradoApiError,
    NitradoService,
    ParsedServer,
)
from custom_components.nitrado_gameserver.models import ManagedServiceState
from custom_components.nitrado_gameserver.plugins.api import (
    PROFILE_API_VERSION as PUBLIC_PROFILE_API_VERSION,
)
from custom_components.nitrado_gameserver.plugins.base import (
    COCKPIT_API_VERSION,
    PROFILE_API_VERSION,
    SUPPORTED,
    ActionDeclaration,
    ActionInputDeclaration,
    ActionInputType,
    CapabilityState,
    CockpitDeclaration,
    ControlContext,
    EditableFileDeclaration,
    EntityDeclaration,
    LifecycleEvent,
    LifecycleHookDeclaration,
    MatchResult,
    ProfileActionContext,
    ProfileManifestError,
    ProfileOptionDeclaration,
    ProfileOptionType,
    ProfileStatus,
    ResourceContentFamily,
    ResourceDeclaration,
    SaveBundleDeclaration,
    SurfaceDeclaration,
    ValidatorDeclaration,
    ValidatorDomain,
    ValidatorTarget,
    profile_extension_manifest,
    validate_action_payload,
    validate_profile_option_value,
)
from custom_components.nitrado_gameserver.plugins.generic import GenericProfile
from custom_components.nitrado_gameserver.plugins.palworld import (
    PalworldProfile,
    find_palworld_game_dir,
    palworld_save_root,
    palworld_settings_editor_model,
    parse_palworld_rest_config,
    parse_palworld_rest_players,
    parse_palworld_rest_status,
    parse_palworld_settings_server_name,
    patch_palworld_settings_text,
    validate_palworld_settings_text,
)
from custom_components.nitrado_gameserver.plugins.registry import (
    _factory_from_export,
    _module_profile_factories,
    _ProfileCandidate,
    async_select_profile,
    async_select_profile_candidate,
    clear_discovered_profile_cache,
    discover_profiles,
    register_profile,
    select_profile,
)
from custom_components.nitrado_gameserver.runtime import ServiceRuntime, runtime_profile_extension_manifest


def async_callback(callback):
    """Wrap a compact test callback in the required async mutation contract."""

    async def wrapped(*args, **kwargs):
        return callback(*args, **kwargs)

    return wrapped


def service(**kwargs) -> NitradoService:
    """Build a service fixture."""

    defaults = {
        "service_id": "123456",
        "name": "Palworld Xbox",
        "game": "palworldxb",
        "game_human": "Palworld",
        "folder_short": "palworldxb",
        "type_human": "Gameserver",
        "raw_redacted": {},
    }
    defaults.update(kwargs)
    return NitradoService(**defaults)


def server(status: str) -> ParsedServer:
    """Build a server fixture."""

    return ParsedServer(
        service_id="123456",
        raw_status=status,
        server_name="Example Server",
        address="85.190.158.243:17130",
        game_short="palworldxb",
        game_human="Palworld",
        player_count=None,
        player_max=10,
        player_names=(),
        query_valid=False,
        player_source=None,
        raw_redacted={},
    )


class ProfileTests(unittest.TestCase):
    """Profile contract tests."""

    def test_external_profile_registration_is_fresh_and_reversible(self) -> None:
        created = 0

        class ExternalProfile(GenericProfile):
            profile_id = "external_example"
            name = "External Example"

            def __init__(self) -> None:
                nonlocal created
                created += 1

            def matches(self, service, server=None):
                return MatchResult(service.game == "external", confidence=1.0)

        unregister = register_profile(ExternalProfile)
        try:
            first = discover_profiles()
            second = discover_profiles()
            self.assertIn("external_example", {profile.profile_id for profile in first})
            first_profile = next(profile for profile in first if profile.profile_id == "external_example")
            second_profile = next(profile for profile in second if profile.profile_id == "external_example")
            self.assertIsNot(first_profile, second_profile)
            selected = select_profile(service(game="external", game_human="External", folder_short="external"))
            self.assertEqual(selected.profile_id, "external_example")
        finally:
            unregister()
        self.assertNotIn("external_example", {profile.profile_id for profile in discover_profiles()})
        self.assertGreaterEqual(created, 4)

    def test_reregistered_external_profile_replaces_same_id_runtime_instance(self) -> None:
        class ExternalProfileV1(GenericProfile):
            profile_id = "reloadable_external"
            name = "External V1"

            def matches(self, service, server=None):
                return MatchResult(service.game == "external", confidence=1.0)

        class ExternalProfileV2(ExternalProfileV1):
            name = "External V2"

        runtime = ServiceRuntime(ManagedServiceState("123456"))
        target = service(game="external", game_human="External", folder_short="external")
        unregister_v1 = register_profile(ExternalProfileV1)
        try:
            runtime.update_service(target)
            first = runtime.profile
        finally:
            unregister_v1()

        unregister_v2 = register_profile(ExternalProfileV2)
        try:
            runtime.update_service(target)
            self.assertIsNot(runtime.profile, first)
            self.assertEqual(runtime.profile.name, "External V2")
        finally:
            unregister_v2()

    def test_profile_manifest_does_not_mutate_profile_instance_for_caching(self) -> None:
        class CountingProfile(GenericProfile):
            profile_id = "counting"
            calls = 0

            def resources(self):
                self.calls += 1
                return ()

        profile = CountingProfile()
        first = profile_extension_manifest(profile)
        second = profile_extension_manifest(profile)

        self.assertIsNot(first, second)
        self.assertEqual(profile.calls, 2)

        class SlottedProfile(GenericProfile):
            __slots__ = ()
            profile_id = "slotted"

        manifest = profile_extension_manifest(SlottedProfile())
        self.assertEqual(manifest.profile_id, "slotted")

    def test_runtime_owns_one_immutable_manifest_snapshot(self) -> None:
        class StatefulProfile(GenericProfile):
            profile_id = "stateful_manifest"

            def __init__(self) -> None:
                self.calls = 0

            def actions(self):
                self.calls += 1
                return (
                    ActionDeclaration(
                        key="run",
                        name="Run",
                        action_fn=async_callback(lambda context: {"ok": True}),
                    ),
                )

        runtime = ServiceRuntime(ManagedServiceState("123456"))
        profile = StatefulProfile()
        runtime._apply_selected_profile(profile)

        first = runtime_profile_extension_manifest(runtime)
        second = runtime_profile_extension_manifest(runtime)

        self.assertIs(first, second)
        self.assertEqual(profile.calls, 1)

    def test_profile_replacement_invalidates_prior_profile_derived_status(self) -> None:
        class FirstProfile(GenericProfile):
            profile_id = "first_profile"

        class SecondProfile(GenericProfile):
            profile_id = "second_profile"

        runtime = ServiceRuntime(ManagedServiceState("123456"))
        runtime._apply_selected_profile(FirstProfile())
        runtime.server = replace(
            server("started"),
            player_count=0,
            query_valid=True,
            player_source="first",
        )
        runtime.status_fresh = True
        runtime.using_cached_data = False
        runtime.extra["profile_status"] = ProfileStatus(
            player_count=0,
            query_valid=True,
            player_source="first",
        )

        runtime._apply_selected_profile(SecondProfile())

        self.assertNotIn("profile_status", runtime.extra)
        self.assertFalse(runtime.status_fresh)
        self.assertTrue(runtime.using_cached_data)
        self.assertIsNone(runtime.server.player_count)
        self.assertFalse(runtime.server.query_valid)
        self.assertFalse(runtime.last_idle_shutdown_verdict.allowed)

    def test_manifest_snapshot_detaches_and_freezes_profile_owned_containers(self) -> None:
        validator_refs: list[str] = []
        nested_options = ["safe"]
        attributes = {"ui": {"options": nested_options}}

        class MutableProfile(GenericProfile):
            profile_id = "mutable_manifest"

            def actions(self):
                return (
                    ActionDeclaration(
                        key="run",
                        name="Run",
                        action_fn=async_callback(lambda context: {"ok": True}),
                        validators=validator_refs,  # type: ignore[arg-type]
                        attributes=attributes,
                    ),
                )

        manifest = profile_extension_manifest(MutableProfile())
        validator_refs.append("late_gate")
        nested_options.append("unsafe")
        attributes["new"] = True

        action = manifest.actions[0]
        self.assertEqual(action.validators, ())
        self.assertEqual(tuple(action.attributes["ui"]["options"]), ("safe",))
        self.assertNotIn("new", action.attributes)
        with self.assertRaises(TypeError):
            action.attributes["new"] = True

    def test_runtime_uses_the_manifest_snapshot_validated_during_selection(self) -> None:
        class DriftingManifestProfile(GenericProfile):
            profile_id = "instance_manifest_drift"
            calls = 0

            def actions(self):
                type(self).calls += 1
                if type(self).calls == 1:
                    return ()
                action = ActionDeclaration(
                    key="duplicate",
                    name="Duplicate",
                    action_fn=async_callback(lambda *_args, **_kwargs: None),
                )
                return (action, action)

        async def run() -> None:
            runtime = ServiceRuntime(ManagedServiceState("123456"))
            target = service(game="unknown")
            profile = DriftingManifestProfile()
            candidate = _ProfileCandidate(profile, profile_extension_manifest(profile))
            with patch(
                "custom_components.nitrado_gameserver.runtime.async_select_profile_candidate",
                new=AsyncMock(return_value=candidate),
            ):
                await runtime.async_select_profile(target, None)

            self.assertIs(runtime.profile, profile)
            self.assertIs(runtime.profile_manifest, candidate.manifest)
            self.assertEqual(DriftingManifestProfile.calls, 1)

        asyncio.run(run())

    def test_manifest_rejects_synchronous_mutation_callbacks(self) -> None:
        class InvalidEntityProfile(GenericProfile):
            profile_id = "sync_entity_mutation"

            def extra_entities(self):
                return (
                    EntityDeclaration(
                        platform="button",
                        key="button",
                        name="Button",
                        action_fn=lambda context: None,
                    ),
                )

        class InvalidActionProfile(GenericProfile):
            profile_id = "sync_action_mutation"

            def actions(self):
                return (ActionDeclaration(key="action", name="Action", action_fn=lambda context: None),)

        class InvalidHookProfile(GenericProfile):
            profile_id = "sync_hook_mutation"

            def lifecycle_hooks(self):
                return (
                    LifecycleHookDeclaration(
                        key="hook",
                        event=LifecycleEvent.BEFORE_START,
                        hook_fn=lambda context: SUPPORTED,
                    ),
                )

        for profile_type in (InvalidEntityProfile, InvalidActionProfile, InvalidHookProfile):
            with (
                self.subTest(profile=profile_type.profile_id),
                self.assertRaisesRegex(ProfileManifestError, "mutation callbacks must be async"),
            ):
                profile_extension_manifest(profile_type())

    def test_async_selection_retries_if_external_registration_changes_mid_selection(self) -> None:
        class ExternalProfileV1(GenericProfile):
            profile_id = "racing_external"
            name = "External V1"

            def matches(self, service, server=None):
                return MatchResult(service.game == "external", confidence=1.0)

        class ExternalProfileV2(ExternalProfileV1):
            name = "External V2"

        async def run() -> None:
            runtime = ServiceRuntime(ManagedServiceState("123456"))
            target = service(game="external", game_human="External", folder_short="external")
            unregister_v1 = register_profile(ExternalProfileV1)
            unregister_v2 = None
            original_select = async_select_profile_candidate
            calls = 0

            async def racing_select(service_value, server_value=None):
                nonlocal calls, unregister_v2
                calls += 1
                selected = await original_select(service_value, server_value)
                if calls == 1:
                    unregister_v1()
                    unregister_v2 = register_profile(ExternalProfileV2)
                return selected

            try:
                with patch(
                    "custom_components.nitrado_gameserver.runtime.async_select_profile_candidate",
                    side_effect=racing_select,
                ):
                    await runtime.async_select_profile(target, None)
                self.assertGreaterEqual(calls, 2)
                self.assertEqual(runtime.profile.name, "External V2")
            finally:
                unregister_v1()
                if unregister_v2 is not None:
                    unregister_v2()

        asyncio.run(run())

    def test_async_profile_selection_quarantines_a_hung_matcher(self) -> None:
        class HangingProfile(GenericProfile):
            profile_id = "hanging"

            def matches(self, service, server=None):
                time.sleep(0.1)
                return MatchResult(True, confidence=1)

        async def run() -> None:
            started = time.monotonic()
            with patch(
                "custom_components.nitrado_gameserver.plugins.registry.PROFILE_MATCH_TIMEOUT_SECONDS",
                0.001,
            ):
                selected = await async_select_profile(service(), profiles=(HangingProfile(), GenericProfile()))
            elapsed = time.monotonic() - started

            self.assertEqual(selected.profile_id, "generic")
            self.assertLess(elapsed, 0.05)

        try:
            asyncio.run(run())
        finally:
            clear_discovered_profile_cache()

    def test_async_selection_isolates_factory_that_hangs_after_registration(self) -> None:
        class FactoryProfile(GenericProfile):
            profile_id = "later_hanging_factory"

        calls = 0

        def factory():
            nonlocal calls
            calls += 1
            if calls > 1:
                time.sleep(0.1)
            return FactoryProfile()

        unregister = register_profile(factory)

        async def run() -> None:
            started = time.monotonic()
            with patch(
                "custom_components.nitrado_gameserver.plugins.registry.PROFILE_DISCOVERY_TIMEOUT_SECONDS",
                0.001,
            ):
                selected = await async_select_profile(service())
            self.assertEqual(selected.profile_id, "palworld")
            self.assertLess(time.monotonic() - started, 0.05)

        try:
            asyncio.run(run())
        finally:
            unregister()
            clear_discovered_profile_cache()

    def test_external_profile_registration_rejects_conflicts(self) -> None:
        with self.assertRaisesRegex(ValueError, "conflicts"):
            register_profile(PalworldProfile)
        with self.assertRaisesRegex(ValueError, "reserved"):
            register_profile(GenericProfile)

    def test_external_profile_registration_requires_exact_api_version(self) -> None:
        class MissingVersionProfile(GenericProfile):
            profile_id = "missing_api_version"
            api_version = None

        class FutureVersionProfile(GenericProfile):
            profile_id = "future_api_version"
            api_version = PROFILE_API_VERSION + 1

        with self.assertRaisesRegex(ValueError, "targets API version None"):
            register_profile(MissingVersionProfile)
        with self.assertRaisesRegex(ValueError, "core requires"):
            register_profile(FutureVersionProfile)

    def test_public_profile_api_exports_current_contract_version(self) -> None:
        self.assertEqual(PUBLIC_PROFILE_API_VERSION, PROFILE_API_VERSION)
        self.assertEqual(PROFILE_API_VERSION, 2)

    def test_unsupported_cockpit_api_preserves_backend_profile(self) -> None:
        class FutureCockpitProfile(GenericProfile):
            profile_id = "future_cockpit"
            name = "Future Cockpit"

            def matches(self, service, server=None):
                del service, server
                return MatchResult(True, confidence=1.0)

            def cockpit(self):
                return CockpitDeclaration(
                    key="future_cockpit",
                    name="Future Cockpit",
                    cockpit_api_version=COCKPIT_API_VERSION + 1,
                    asset_key="future_cockpit",
                    frontend_revision=f"sha256:{'a' * 64}",
                    default_route="overview",
                    route_keys=("overview",),
                )

        manifest = profile_extension_manifest(FutureCockpitProfile())
        self.assertEqual(manifest.cockpit.cockpit_api_version, COCKPIT_API_VERSION + 1)
        selected = select_profile(service(), profiles=(FutureCockpitProfile(), GenericProfile()))
        self.assertEqual(selected.profile_id, "future_cockpit")

    def test_profile_extension_manifest_collects_all_contract_families(self) -> None:
        class FullContractProfile:
            profile_id = "full_contract"
            name = "Full Contract"
            supported_games = ("fullcontract",)
            idle_shutdown_supported = False

            def matches(self, service, server=None):
                return MatchResult(True, confidence=1)

            async def enrich_status(self, client, service, server, context):
                raise AssertionError("not used")

            async def suggest_display_name(self, client, service, server):
                return None

            async def can_start(self, context):
                return SUPPORTED

            async def can_stop(self, context):
                return SUPPORTED

            def idle_shutdown_capability(self, context):
                return SUPPORTED

            def extra_entities(self):
                return (EntityDeclaration("sensor", "profile_status", "Profile Status", value_fn=lambda runtime: "ok"),)

            def editable_files(self):
                return (
                    EditableFileDeclaration(
                        key="settings",
                        name="Settings",
                        path_fn=lambda context: "/game/settings.ini",
                        parser=lambda text: {"raw": text},
                        serializer=lambda parsed: str(parsed["raw"]),
                    ),
                )

            def resources(self):
                return (
                    ResourceDeclaration(
                        key="world_visual",
                        name="World Visual",
                        content_type="image/png",
                        fetch_fn=lambda context: b"png",
                        cache_seconds=300,
                    ),
                    ResourceDeclaration(
                        key="world_state",
                        name="World State",
                        content_type="application/json",
                        fetch_fn=lambda context: {"players": []},
                    ),
                )

            def actions(self):
                return (
                    ActionDeclaration(
                        key="open_editor",
                        name="Open Editor",
                        action_fn=async_callback(lambda context: context.extra.setdefault("opened_editor", True)),
                        validators=("server_stopped",),
                    ),
                )

            def surfaces(self):
                return (
                    SurfaceDeclaration(
                        key="world_view",
                        name="World View",
                        resources=("world_visual", "world_state"),
                        actions=("open_editor",),
                        controls=("profile_status",),
                        editable_files=("settings",),
                        renderer_hint="image_with_overlay",
                    ),
                )

            def lifecycle_hooks(self):
                return (
                    LifecycleHookDeclaration(
                        key="check_save_before_start",
                        event=LifecycleEvent.BEFORE_START,
                        hook_fn=async_callback(lambda context: SUPPORTED),
                        validators=("save_tree",),
                    ),
                )

            def validators(self):
                return (
                    ValidatorDeclaration(
                        key="server_stopped",
                        name="Server Stopped",
                        target=ValidatorTarget.ACTION,
                        validate_fn=lambda context, payload=None: SUPPORTED,
                    ),
                    ValidatorDeclaration(
                        key="save_tree",
                        name="Save Tree",
                        target=ValidatorTarget.LIFECYCLE_HOOK,
                        domains=(ValidatorDomain.SAVE,),
                        validate_fn=lambda context, payload=None: SUPPORTED,
                    ),
                )

        manifest = profile_extension_manifest(FullContractProfile())

        self.assertEqual(manifest.profile_id, "full_contract")
        self.assertEqual([entity.key for entity in manifest.entities], ["profile_status"])
        self.assertEqual([file.key for file in manifest.editable_files], ["settings"])
        self.assertEqual([resource.key for resource in manifest.resources], ["world_visual", "world_state"])
        self.assertEqual([action.key for action in manifest.actions], ["open_editor"])
        self.assertEqual([surface.key for surface in manifest.surfaces], ["world_view"])
        self.assertEqual([hook.event for hook in manifest.lifecycle_hooks], [LifecycleEvent.BEFORE_START])
        self.assertEqual([validator.key for validator in manifest.validators], ["server_stopped", "save_tree"])
        self.assertEqual(manifest.validators[1].domains, (ValidatorDomain.SAVE,))
        self.assertEqual(manifest.surfaces[0].resources, ("world_visual", "world_state"))

    def test_profile_extension_manifest_rejects_duplicate_keys(self) -> None:
        class DuplicateActionProfile(GenericProfile):
            profile_id = "duplicate"
            name = "Duplicate"

            def actions(self):
                return (
                    ActionDeclaration("open_editor", "Open Editor", action_fn=async_callback(lambda context: None)),
                    ActionDeclaration(
                        "open_editor", "Open Editor Again", action_fn=async_callback(lambda context: None)
                    ),
                )

        with self.assertRaises(ValueError) as caught:
            profile_extension_manifest(DuplicateActionProfile())

        self.assertIn("duplicate action keys", str(caught.exception))

    def test_profile_extension_manifest_rejects_duplicate_normalized_entity_keys(self) -> None:
        class DuplicateEntityProfile(GenericProfile):
            profile_id = "duplicate"
            name = "Duplicate"

            def extra_entities(self):
                return (
                    EntityDeclaration("sensor", "status", "Status"),
                    EntityDeclaration("sensor", "duplicate_status", "Prefixed Status"),
                )

        with self.assertRaises(ValueError) as caught:
            profile_extension_manifest(DuplicateEntityProfile())

        self.assertIn("duplicate normalized entity keys", str(caught.exception))
        self.assertIn("duplicate_status", str(caught.exception))

    def test_profile_extension_manifest_rejects_invalid_entity_contracts(self) -> None:
        class InvalidEntityProfile(GenericProfile):
            profile_id = "invalid_entity"
            name = "Invalid Entity"

            def extra_entities(self):
                return (EntityDeclaration("buton", "open_editor", "Open Editor"),)

        with self.assertRaises(ValueError) as bad_platform:
            profile_extension_manifest(InvalidEntityProfile())

        self.assertIn("unsupported entity platform", str(bad_platform.exception))
        self.assertIn("buton", str(bad_platform.exception))

        async def async_value(_context):
            return "bad"

        class AsyncPassiveEntityProfile(GenericProfile):
            profile_id = "async_passive_entity"
            name = "Async Passive Entity"

            def extra_entities(self):
                return (EntityDeclaration("sensor", "status", "Status", value_fn=async_value),)

        with self.assertRaises(ValueError) as async_entity:
            profile_extension_manifest(AsyncPassiveEntityProfile())

        self.assertIn("async entity value_fn", str(async_entity.exception))
        self.assertIn("passive entity callbacks must be synchronous", str(async_entity.exception))

        class AsyncCallableValue:
            async def __call__(self, _context):
                return "bad"

        class AsyncCallablePassiveEntityProfile(GenericProfile):
            profile_id = "async_callable_passive_entity"
            name = "Async Callable Passive Entity"

            def extra_entities(self):
                return (EntityDeclaration("sensor", "status", "Status", value_fn=AsyncCallableValue()),)

        with self.assertRaises(ValueError) as async_callable_entity:
            profile_extension_manifest(AsyncCallablePassiveEntityProfile())

        self.assertIn("async entity value_fn", str(async_callable_entity.exception))
        self.assertIn("passive entity callbacks must be synchronous", str(async_callable_entity.exception))

        class InvalidEntityKeyProfile(GenericProfile):
            profile_id = "invalid_entity_key"
            name = "Invalid Entity Key"

            def extra_entities(self):
                return (
                    EntityDeclaration("sensor", 123, "Bad Key"),  # type: ignore[arg-type]
                )

        with self.assertRaises(ValueError) as bad_key:
            profile_extension_manifest(InvalidEntityKeyProfile())

        self.assertIn("entity with an invalid key", str(bad_key.exception))

        class InvalidEntityAttributeProfile(GenericProfile):
            profile_id = "invalid_entity_attribute"
            name = "Invalid Entity Attribute"

            def extra_entities(self):
                return (
                    EntityDeclaration(
                        "number",
                        "difficulty",
                        "Difficulty",
                        attributes={
                            "entity_category": "surprise",
                            "native_min_value": "1",
                            "native_step": 0,
                        },
                    ),
                )

        with self.assertRaises(ValueError) as bad_attribute:
            profile_extension_manifest(InvalidEntityAttributeProfile())

        self.assertIn("invalid entity_category", str(bad_attribute.exception))

        class InvalidSelectOptionsProfile(GenericProfile):
            profile_id = "invalid_select_options"
            name = "Invalid Select Options"

            def extra_entities(self):
                return (
                    EntityDeclaration(
                        "select",
                        "slot",
                        "Slot",
                        attributes={"options": ("main", 123)},
                    ),
                )

        with self.assertRaises(ValueError) as bad_options:
            profile_extension_manifest(InvalidSelectOptionsProfile())

        self.assertIn("select entity slot options", str(bad_options.exception))

    def test_profile_extension_manifest_rejects_invalid_declaration_keys(self) -> None:
        class InvalidActionKeyProfile(GenericProfile):
            profile_id = "invalid_action_key"
            name = "Invalid Action Key"

            def actions(self):
                return (ActionDeclaration("", "Bad Action", action_fn=async_callback(lambda context: None)),)

        with self.assertRaises(ValueError) as caught:
            profile_extension_manifest(InvalidActionKeyProfile())

        self.assertIn("action with an invalid key", str(caught.exception))

    def test_profile_extension_manifest_wraps_declaration_failures(self) -> None:
        class BrokenProfile(GenericProfile):
            profile_id = "broken"
            name = "Broken"

            def actions(self):
                raise RuntimeError("action declarations exploded")

        with self.assertRaises(ValueError) as caught:
            profile_extension_manifest(BrokenProfile())

        self.assertIn("Profile broken extension declarations failed", str(caught.exception))
        self.assertIn("failed with RuntimeError", str(caught.exception))
        self.assertNotIn("action declarations exploded", str(caught.exception))

    def test_profile_entity_cannot_reuse_administrator_option_key(self) -> None:
        class CollisionProfile(GenericProfile):
            profile_id = "option_collision"

            def profile_options(self):
                return (
                    ProfileOptionDeclaration(
                        key="allow_insecure_transport",
                        name="Allow insecure transport",
                        option_type=ProfileOptionType.BOOLEAN,
                        default=False,
                        confirmation_required=True,
                        acknowledgement_revision=1,
                    ),
                )

            def extra_entities(self):
                return (
                    EntityDeclaration(
                        platform="switch",
                        key="insecure",
                        name="Insecure",
                        attributes={"option_key": "allow_insecure_transport"},
                    ),
                )

        with self.assertRaisesRegex(ProfileManifestError, "administrator profile option"):
            profile_extension_manifest(CollisionProfile())

    def test_save_bundle_paths_are_rejected_at_manifest_registration(self) -> None:
        class UnsafeSaveProfile(GenericProfile):
            profile_id = "unsafe_save"

            def save_bundles(self):
                return (
                    SaveBundleDeclaration(
                        key="world",
                        name="World",
                        root_fn=lambda _context: "world",
                        allowed_suffixes=(".dat",),
                        required_files=("../level.dat",),
                        editor_root_files=("level.dat",),
                    ),
                )

        with self.assertRaisesRegex(ProfileManifestError, "invalid required_files"):
            profile_extension_manifest(UnsafeSaveProfile())

    def test_profile_manifest_rejects_retained_primitive_subclasses(self) -> None:
        class HostileText(str):
            pass

        class HostileSuffixProfile(GenericProfile):
            profile_id = "hostile_suffix"

            def save_bundles(self):
                return (
                    SaveBundleDeclaration(
                        key="world",
                        name="World",
                        root_fn=lambda _context: "world",
                        allowed_suffixes=(HostileText(".dat"),),
                        editor_root_files=("level.dat",),
                    ),
                )

        with self.assertRaisesRegex(ProfileManifestError, "invalid suffix"):
            profile_extension_manifest(HostileSuffixProfile())

        class HostileCockpitProfile(GenericProfile):
            profile_id = "hostile_cockpit"

            def cockpit(self):
                return CockpitDeclaration(
                    key="hostile_cockpit",
                    name="Hostile Cockpit",
                    cockpit_api_version=COCKPIT_API_VERSION,
                    asset_key="hostile_cockpit",
                    frontend_revision=f"sha256:{'a' * 64}",
                    default_route=HostileText("overview"),
                    route_keys=("overview",),
                )

        with self.assertRaisesRegex(ProfileManifestError, "default route"):
            profile_extension_manifest(HostileCockpitProfile())

        class HostileAttributeProfile(GenericProfile):
            profile_id = "hostile_attribute"

            def extra_entities(self):
                return (
                    EntityDeclaration(
                        platform="sensor",
                        key="status",
                        name="Status",
                        attributes={HostileText("icon"): "mdi:test"},
                    ),
                )

        with self.assertRaisesRegex(ProfileManifestError, "attribute key"):
            profile_extension_manifest(HostileAttributeProfile())

    def test_profile_extension_manifest_rejects_invalid_non_key_fields(self) -> None:
        class InvalidResourceProfile(GenericProfile):
            profile_id = "invalid_resource"
            name = "Invalid Resource"

            def resources(self):
                return (
                    ResourceDeclaration(
                        key="world",
                        name="World",
                        content_type="png",
                        fetch_fn=lambda context: b"",
                        content_family=ResourceContentFamily.IMAGE,
                    ),
                )

        with self.assertRaises(ValueError) as bad_resource:
            profile_extension_manifest(InvalidResourceProfile())

        self.assertIn("invalid content_type", str(bad_resource.exception))

        class CachedInferredStreamProfile(GenericProfile):
            profile_id = "cached_inferred_stream"
            name = "Cached Inferred Stream"

            def resources(self):
                return (
                    ResourceDeclaration(
                        key="events",
                        name="Events",
                        content_type="text/event-stream",
                        fetch_fn=lambda context: None,
                        cache_seconds=10,
                    ),
                )

        with self.assertRaises(ValueError) as cached_stream:
            profile_extension_manifest(CachedInferredStreamProfile())

        self.assertIn("stream resource events cannot be cached", str(cached_stream.exception))

        class InvalidActionProfile(GenericProfile):
            profile_id = "invalid_action"
            name = "Invalid Action"

            def actions(self):
                return (
                    ActionDeclaration(
                        key="danger",
                        name="Danger",
                        action_fn=async_callback(lambda context: None),
                        requires_confirmation="yes",  # type: ignore[arg-type]
                    ),
                )

        with self.assertRaises(ValueError) as bad_action:
            profile_extension_manifest(InvalidActionProfile())

        self.assertIn("requires_confirmation", str(bad_action.exception))

    def test_profile_extension_manifest_rejects_invalid_hook_and_validator_fields(self) -> None:
        class InvalidHookProfile(GenericProfile):
            profile_id = "invalid_hook"
            name = "Invalid Hook"

            def lifecycle_hooks(self):
                return (
                    LifecycleHookDeclaration(
                        key="before_wobble",
                        event="before_wobble",  # type: ignore[arg-type]
                        hook_fn=async_callback(lambda context: SUPPORTED),
                    ),
                )

        with self.assertRaises(ValueError) as bad_hook:
            profile_extension_manifest(InvalidHookProfile())

        self.assertIn("invalid event", str(bad_hook.exception))

        class InvalidValidatorProfile(GenericProfile):
            profile_id = "invalid_validator"
            name = "Invalid Validator"

            def validators(self):
                return (
                    ValidatorDeclaration(
                        key="ready",
                        name="Ready",
                        target="action",  # type: ignore[arg-type]
                        validate_fn=lambda context, payload=None: SUPPORTED,
                    ),
                )

        with self.assertRaises(ValueError) as bad_validator:
            profile_extension_manifest(InvalidValidatorProfile())

        self.assertIn("invalid target", str(bad_validator.exception))

    def test_profile_extension_manifest_rejects_invalid_cross_references(self) -> None:
        class MissingValidatorProfile(GenericProfile):
            profile_id = "missing_validator"
            name = "Missing Validator"

            def actions(self):
                return (
                    ActionDeclaration(
                        "open_editor",
                        "Open Editor",
                        action_fn=async_callback(lambda context: None),
                        validators=("gone",),
                    ),
                )

        with self.assertRaises(ValueError) as missing_validator:
            profile_extension_manifest(MissingValidatorProfile())

        self.assertIn("unresolved action open_editor validators", str(missing_validator.exception))

        class WrongValidatorTargetProfile(GenericProfile):
            profile_id = "wrong_validator_target"
            name = "Wrong Validator Target"

            def actions(self):
                return (
                    ActionDeclaration(
                        "open_editor",
                        "Open Editor",
                        action_fn=async_callback(lambda context: None),
                        validators=("ready",),
                    ),
                )

            def validators(self):
                return (
                    ValidatorDeclaration(
                        key="ready",
                        name="Ready",
                        target=ValidatorTarget.RESOURCE,
                        validate_fn=lambda context, payload=None: SUPPORTED,
                    ),
                )

        with self.assertRaises(ValueError) as wrong_target:
            profile_extension_manifest(WrongValidatorTargetProfile())

        self.assertIn("wrong target", str(wrong_target.exception))
        self.assertIn("ready is resource", str(wrong_target.exception))

        class MissingSurfaceRefProfile(GenericProfile):
            profile_id = "missing_surface_ref"
            name = "Missing Surface Ref"

            def surfaces(self):
                return (SurfaceDeclaration("panel", "Panel", resources=("map",)),)

        with self.assertRaises(ValueError) as missing_surface_ref:
            profile_extension_manifest(MissingSurfaceRefProfile())

        self.assertIn("unresolved surface panel resources", str(missing_surface_ref.exception))

    def test_builtin_profiles_expose_current_contract_families(self) -> None:
        generic_manifest = profile_extension_manifest(GenericProfile())
        palworld_manifest = profile_extension_manifest(PalworldProfile())

        self.assertEqual(generic_manifest.resources, ())
        self.assertEqual(generic_manifest.actions, ())
        self.assertEqual(generic_manifest.surfaces, ())
        self.assertEqual(generic_manifest.lifecycle_hooks, ())
        self.assertEqual(generic_manifest.validators, ())
        self.assertEqual(palworld_manifest.resources, ())
        self.assertEqual(palworld_manifest.actions, ())
        self.assertEqual(palworld_manifest.surfaces, ())
        self.assertEqual(palworld_manifest.lifecycle_hooks, ())
        self.assertEqual(palworld_manifest.validators, ())
        self.assertEqual([file.key for file in palworld_manifest.editable_files], ["settings"])
        self.assertEqual([bundle.key for bundle in palworld_manifest.save_bundles], ["world"])
        self.assertEqual(palworld_manifest.save_bundles[0].excluded_paths, ("backup",))

    def test_palworld_profile_matches_service_metadata(self) -> None:
        result = PalworldProfile().matches(service())

        self.assertTrue(result.matched)
        self.assertGreater(result.confidence, 0.9)

    def test_palworld_profile_prefers_production_machine_ids_over_human_service_game(self) -> None:
        result = PalworldProfile().matches(
            service(
                name="Gameserver - 10 Slots",
                game="Palworld Xbox",
                game_human=None,
                folder_short="palworldxb",
            ),
            server("stopped"),
        )

        self.assertTrue(result.matched)
        self.assertGreater(result.confidence, 0.9)

    def test_palworld_profile_rejects_misleading_name_when_machine_ids_are_unrelated(self) -> None:
        unrelated_server = replace(server("stopped"), game_short="arksa", game_human="ARK")
        result = PalworldProfile().matches(
            service(name="Palworld Prank", game="ARK", game_human="ARK", folder_short="arksa"),
            unrelated_server,
        )

        self.assertFalse(result.matched)

    def test_palworld_profile_does_not_match_unrelated_service(self) -> None:
        result = PalworldProfile().matches(service(name="ARK", game="arksa", game_human="ARK", folder_short="arksa"))

        self.assertFalse(result.matched)

    def test_registry_selects_palworld_before_generic(self) -> None:
        self.assertEqual(select_profile(service()).profile_id, "palworld")

    def test_registry_falls_back_to_generic(self) -> None:
        selected = select_profile(service(name="ARK", game="arksa", game_human="ARK", folder_short="arksa"))

        self.assertEqual(selected.profile_id, "generic")

    def test_registry_discovers_profiles_from_plugin_modules(self) -> None:
        clear_discovered_profile_cache()
        profile_ids = [profile.profile_id for profile in discover_profiles()]

        self.assertIn("palworld", profile_ids)
        self.assertEqual(profile_ids[-1], "generic")

    def test_registry_caches_discovered_profiles_until_refresh(self) -> None:
        clear_discovered_profile_cache()

        first = discover_profiles()
        second = discover_profiles()
        refreshed = discover_profiles(refresh=True)

        self.assertIsNot(first, second)
        self.assertIsNot(first, refreshed)
        self.assertEqual([profile.profile_id for profile in first], [profile.profile_id for profile in refreshed])
        for before, after in zip(first, second, strict=True):
            self.assertEqual(before.profile_id, after.profile_id)
            self.assertIsNot(before, after)

    def test_registry_returns_fresh_profile_instances(self) -> None:
        clear_discovered_profile_cache()

        first = select_profile(service())
        second = select_profile(service())

        self.assertEqual(first.profile_id, "palworld")
        self.assertEqual(second.profile_id, "palworld")
        self.assertIsNot(first, second)

    def test_registry_accepts_profile_factory_exports(self) -> None:
        class FactoryProfile(GenericProfile):
            profile_id = "factory"
            name = "Factory"

        def make_profile():
            return FactoryProfile()

        factory = _factory_from_export(make_profile)
        first = factory.create()
        second = factory.create()

        self.assertEqual(factory.profile_id, "factory")
        self.assertIsInstance(first, FactoryProfile)
        self.assertIsInstance(second, FactoryProfile)
        self.assertIsNot(first, second)

    def test_registered_factory_identity_drift_is_skipped(self) -> None:
        class ProbeProfile(GenericProfile):
            profile_id = "stable_probe"
            name = "Stable probe"

        calls = 0

        def drifting_factory():
            nonlocal calls
            calls += 1
            return ProbeProfile() if calls == 1 else PalworldProfile()

        unregister = register_profile(drifting_factory)
        try:
            clear_discovered_profile_cache()
            profile_ids = [profile.profile_id for profile in discover_profiles()]
            self.assertNotIn("stable_probe", profile_ids)
            self.assertEqual(profile_ids.count("palworld"), 1)
        finally:
            unregister()
            clear_discovered_profile_cache()

    def test_registered_factory_manifest_drift_is_skipped(self) -> None:
        class InitiallyValidProfile(GenericProfile):
            profile_id = "manifest_probe"
            name = "Manifest probe"

        class LaterInvalidProfile(InitiallyValidProfile):
            def actions(self):
                action = ActionDeclaration(
                    key="duplicate",
                    name="Duplicate",
                    action_fn=async_callback(lambda *_args, **_kwargs: None),
                )
                return (action, action)

        calls = 0

        def drifting_factory():
            nonlocal calls
            calls += 1
            return InitiallyValidProfile() if calls == 1 else LaterInvalidProfile()

        unregister = register_profile(drifting_factory)
        try:
            clear_discovered_profile_cache()
            self.assertNotIn("manifest_probe", [profile.profile_id for profile in discover_profiles()])
        finally:
            unregister()
            clear_discovered_profile_cache()

    def test_registry_select_profile_skips_match_failures(self) -> None:
        class BrokenProfile(GenericProfile):
            profile_id = "broken"
            name = "Broken"

            def matches(self, service, server=None):
                raise RuntimeError("match exploded")

        class GoodProfile(GenericProfile):
            profile_id = "good"
            name = "Good"

            def matches(self, service, server=None):
                return MatchResult(True, confidence=1)

        selected = select_profile(service(), profiles=(BrokenProfile(), GoodProfile()))

        self.assertEqual(selected.profile_id, "good")

    def test_registry_skips_invalid_module_profile_exports(self) -> None:
        class ValidProfile(GenericProfile):
            profile_id = "valid"
            name = "Valid"

        module = type("Module", (), {"__name__": "fake_profiles", "PROFILES": (object(), ValidProfile)})()

        factories = _module_profile_factories(module)

        self.assertEqual([factory.profile_id for factory in factories], ["valid"])

    def test_parse_palworld_rest_config_derives_current_nitrado_endpoint(self) -> None:
        config = parse_palworld_rest_config(
            'RESTAPIEnabled=True,PublicIP="31.214.203.66",PublicPort=8211,RESTAPIPort=8212,AdminPassword="secret")',
            "85.190.158.243:17130",
        )

        self.assertTrue(config.enabled)
        self.assertEqual(config.endpoints[0], ("85.190.158.243", 17131))
        self.assertIn(("31.214.203.66", 8212), config.endpoints)
        self.assertEqual(config.admin_password, "secret")

    def test_parse_palworld_settings_server_name(self) -> None:
        self.assertEqual(
            parse_palworld_settings_server_name(
                'OptionSettings=(ServerName="Example Palworld Server",RESTAPIEnabled=True)'
            ),
            "Example Palworld Server",
        )
        self.assertEqual(
            parse_palworld_settings_server_name(
                '[/Script/Pal.PalGameWorldSettings]\nOptionSettings=(ServerName="Example, \\"Quoted\\" World",RESTAPIEnabled=True)\n'
            ),
            'Example, "Quoted" World',
        )

    def test_palworld_profile_suggests_name_from_settings_file(self) -> None:
        class DummyClient:
            async def list_files(self, directory: str | None = None):
                return {"entries": [{"type": "dir", "name": "palworldxb", "path": "/palworldxb"}]}

            async def download_file(self, path: str):
                return 'OptionSettings=(ServerName="Example Palworld Server",RESTAPIEnabled=True)'

        async def run() -> None:
            suggested = await PalworldProfile().suggest_display_name(DummyClient(), service(), server("stopped"))  # type: ignore[arg-type]

            self.assertEqual(suggested, "Example Palworld Server")

        asyncio.run(run())

    def test_palworld_profile_declares_settings_editor_contract(self) -> None:
        declaration = PalworldProfile().editable_files()[0]

        self.assertEqual(declaration.key, "settings")
        self.assertEqual(declaration.name, "PalWorldSettings.ini")
        self.assertTrue(declaration.requires_restart)
        self.assertTrue(declaration.requires_stopped)
        self.assertTrue(declaration.create_backup)
        self.assertEqual(declaration.parser("raw settings"), "raw settings")
        self.assertEqual(declaration.serializer("raw settings"), "raw settings")
        self.assertEqual(
            declaration.redactor(
                'AdminPassword="admin-secret",ServerPassword="join-secret",ServerName="Example Server"'
            ),
            'AdminPassword=[redacted],ServerPassword=[redacted],ServerName="Example Server"',
        )
        self.assertEqual(
            declaration.redactor(r'AdminPassword="abc\"def",ServerPassword="ghi\"jkl"'),
            "AdminPassword=[redacted],ServerPassword=[redacted]",
        )

    def test_palworld_settings_validation_is_lossless_and_structural(self) -> None:
        valid = (
            "[/Script/Pal.PalGameWorldSettings]\n"
            'OptionSettings=(ServerName="Example, World",AdminPassword="secret",RESTAPIEnabled=True,FutureKey=(A=1,B=2))\n'
        )
        declaration = PalworldProfile().editable_files()[0]

        parsed = declaration.parser(valid)
        self.assertEqual(declaration.serializer(parsed), valid)
        self.assertTrue(validate_palworld_settings_text(parsed).allowed)
        self.assertIn(
            "duplicate setting RESTAPIEnabled",
            validate_palworld_settings_text(valid.replace("FutureKey=(A=1,B=2)", "RESTAPIEnabled=False")).reason,
        )
        self.assertIn(
            "quoted OptionSettings value",
            validate_palworld_settings_text(
                valid.replace('ServerName="Example, World"', 'ServerName="Example, World')
            ).reason,
        )

    def test_palworld_structured_editor_is_secret_safe_and_lossless(self) -> None:
        source = (
            "; lead\r\n"
            "[/Script/Pal.PalGameWorldSettings] ; target\r\n"
            'OptionSettings = (  ServerName = "Goblin, \\"Box\\"" ,FutureKey=(A=1,B=(C="x,y")),'
            'DayTimeSpeedRate=1.000000,AdminPassword = "s3cr,et",ServerPassword\t=\t"join",FutureToken="opaque" ) #tail\r\n'
            "[Other]\r\nKeep = exactly\r\n"
        )

        model = palworld_settings_editor_model(source)

        self.assertEqual(model["schema_revision"], "palworld-1.0-2026-08")
        by_key = {item["key"]: item for item in model["settings"]}
        self.assertEqual(by_key["ServerName"]["raw_value"], '"Goblin, \\"Box\\""')
        self.assertEqual(by_key["FutureKey"]["raw_value"], '(A=1,B=(C="x,y"))')
        self.assertIsNone(by_key["AdminPassword"]["raw_value"])
        self.assertTrue(by_key["AdminPassword"]["configured"])
        self.assertNotIn("s3cr,et", model["redacted_source"])
        self.assertNotIn('"join"', model["redacted_source"])
        self.assertNotIn('"opaque"', model["redacted_source"])
        self.assertIsNone(by_key["FutureToken"]["raw_value"])

        changed = patch_palworld_settings_text(
            source,
            [
                {"op": "set", "key": "DayTimeSpeedRate", "raw_value": "2.500000"},
                {"op": "set", "key": "ServerName", "raw_value": '"New, World"'},
            ],
        )
        expected = source.replace('"Goblin, \\"Box\\""', '"New, World"').replace(
            "DayTimeSpeedRate=1.000000", "DayTimeSpeedRate=2.500000"
        )
        self.assertEqual(changed, expected)
        self.assertIn('FutureKey=(A=1,B=(C="x,y"))', changed)
        self.assertTrue(changed.endswith("[Other]\r\nKeep = exactly\r\n"))

    def test_palworld_structured_editor_accepts_plain_crlf_section_headers(self) -> None:
        source = (
            "[/Script/Pal.PalGameWorldSettings]\r\n"
            'OptionSettings=(ServerName="CRLF world",DayTimeSpeedRate=1.000000)\r\n'
            "[Other]\r\n"
            "Keep = exactly\r\n"
        )

        self.assertTrue(validate_palworld_settings_text(source).allowed)
        model = palworld_settings_editor_model(source)
        by_key = {item["key"]: item for item in model["settings"]}
        self.assertEqual(by_key["ServerName"]["raw_value"], '"CRLF world"')
        changed = patch_palworld_settings_text(
            source,
            [{"op": "set", "key": "DayTimeSpeedRate", "raw_value": "2.000000"}],
        )
        self.assertEqual(
            changed,
            source.replace("DayTimeSpeedRate=1.000000", "DayTimeSpeedRate=2.000000"),
        )
        self.assertTrue(changed.endswith("[Other]\r\nKeep = exactly\r\n"))

    def test_palworld_structured_editor_rejects_wrong_section_and_bad_operations(self) -> None:
        wrong = '[/Script/Pal.PalGameWorldSettings]\nOther=1\n[Other]\nOptionSettings=(ServerName="Wrong section")\n'
        self.assertFalse(validate_palworld_settings_text(wrong).allowed)

        valid = '[/Script/Pal.PalGameWorldSettings]\nOptionSettings=(ServerName="Right",DenyTechnologyList=)\n'
        self.assertTrue(validate_palworld_settings_text(valid).allowed)
        with self.assertRaisesRegex(ValueError, "not present"):
            patch_palworld_settings_text(valid, [{"op": "set", "key": "Missing", "raw_value": "1"}])
        with self.assertRaisesRegex(ValueError, "duplicate operation"):
            patch_palworld_settings_text(
                valid,
                [
                    {"op": "set", "key": "ServerName", "raw_value": '"One"'},
                    {"op": "set", "key": "servername", "raw_value": '"Two"'},
                ],
            )
        with self.assertRaisesRegex(ValueError, "unsupported value"):
            patch_palworld_settings_text(valid, [{"op": "set", "key": "ServerName", "raw_value": '"bad\nvalue"'}])

    def test_palworld_structured_editor_never_crosses_section_boundaries(self) -> None:
        malformed = "[/Script/Pal.PalGameWorldSettings]\nOptionSettings=(ServerName=x\n[Other]\nFoo=1)\n"

        self.assertFalse(validate_palworld_settings_text(malformed).allowed)
        with self.assertRaisesRegex(ValueError, "parenthesized value|closing parenthesis"):
            palworld_settings_editor_model(malformed)
        with self.assertRaisesRegex(ValueError, "parenthesized value|closing parenthesis"):
            patch_palworld_settings_text(
                malformed,
                [{"op": "set", "key": "ServerName", "raw_value": '"safe"'}],
            )

    def test_palworld_structured_editor_fails_closed_for_secret_like_keys_and_placeholders(self) -> None:
        source = (
            "[/Script/Pal.PalGameWorldSettings]\n"
            'OptionSettings=(ApiKey="api-leak",PrivateKey="private-leak",AuthKey="auth-leak",'
            'FutureAuth=(ApiToken="nested-leak",User="x"),FutureKey=(AdminPassword = "nested-known"),'
            'ServerName="ok")\n'
            "[Other]\n"
            'AdminPassword="outside-known"\n'
            'ApiToken="outside-token"\n'
            '; ClientKey="comment-leak"\n'
        )

        model = palworld_settings_editor_model(source)
        by_key = {item["key"]: item for item in model["settings"]}
        for key in ("ApiKey", "PrivateKey", "AuthKey", "FutureAuth", "FutureKey"):
            self.assertTrue(by_key[key]["sensitive"])
            self.assertIsNone(by_key[key]["raw_value"])
        for secret in (
            "api-leak",
            "private-leak",
            "auth-leak",
            "nested-leak",
            "nested-known",
            "outside-known",
            "outside-token",
            "comment-leak",
        ):
            self.assertNotIn(secret, model["redacted_source"])

        for placeholder in ("[redacted]", '"[redacted]"', "***"):
            with self.assertRaisesRegex(ValueError, "redaction placeholder"):
                patch_palworld_settings_text(
                    source,
                    [{"op": "set", "key": "ApiKey", "raw_value": placeholder}],
                )

    def test_palworld_settings_editor_contract_resolves_active_path(self) -> None:
        class DummyClient:
            async def list_files(self, directory: str | None = None):
                return {"entries": [{"type": "dir", "name": "palworldxb", "path": "/palworldxb"}]}

        async def run() -> None:
            declaration = PalworldProfile().editable_files()[0]
            path = await declaration.path_fn(
                ProfileActionContext(
                    client=DummyClient(),  # type: ignore[arg-type]
                    service=service(),
                    server=server("stopped"),
                    status_fresh=True,
                    using_cached_data=False,
                )
            )

            self.assertEqual(path, "/palworldxb/Pal/Saved/Config/WindowsServer/PalWorldSettings.ini")

        asyncio.run(run())

    def test_parse_palworld_rest_players_uses_display_names_only(self) -> None:
        players = parse_palworld_rest_players(
            [
                {"name": "Alex", "playerId": "do-not-use"},
                {"accountName": "Jordan", "ip": "do-not-use"},
                {},
            ]
        )

        self.assertEqual(players, ("Alex", "Jordan", "Player 3"))

    def test_parse_palworld_rest_status_uses_metrics_and_names(self) -> None:
        status = parse_palworld_rest_status(
            info={"servername": "Example Palworld Live"},
            metrics={"currentplayernum": 2, "maxplayernum": 12},
            players_payload={"players": [{"name": "Alex"}, {"accountName": "Jordan"}]},
            fallback=server("started"),
        )

        self.assertEqual(status.player_count, 2)
        self.assertEqual(status.player_max, 12)
        self.assertEqual(status.player_names, ("Alex", "Jordan"))
        self.assertEqual(status.player_source, "palworld_rest")
        self.assertTrue(status.query_valid)
        self.assertEqual(status.display_name, "Example Palworld Live")

    def test_profile_can_match_from_server_metadata_after_generic_service_metadata(self) -> None:
        class ServerOnlyProfile(GenericProfile):
            profile_id = "server_only"
            name = "Server Only"
            supported_games = ("serveronly",)
            idle_shutdown_supported = False

            def matches(self, service: NitradoService, server: ParsedServer | None = None) -> MatchResult:
                if server and server.game_short == "serveronly":
                    return MatchResult(True, confidence=0.9)
                return MatchResult(False)

        generic_service = service(name="Mystery", game=None, game_human=None, folder_short=None)
        selected_without_server = select_profile(generic_service, profiles=(ServerOnlyProfile(), GenericProfile()))
        selected_with_server = select_profile(
            generic_service,
            replace(server("started"), game_short="serveronly", game_human="Server Only"),
            profiles=(ServerOnlyProfile(), GenericProfile()),
        )

        self.assertEqual(selected_without_server.profile_id, "generic")
        self.assertEqual(selected_with_server.profile_id, "server_only")

    def test_parse_palworld_rest_status_rejects_contradictory_zero(self) -> None:
        with self.assertRaises(Exception):
            parse_palworld_rest_status(
                info={},
                metrics={"currentplayernum": 0},
                players_payload={"players": [{"name": "Alex"}]},
                fallback=server("started"),
            )

    def test_find_palworld_game_dir_prefers_current_game_metadata(self) -> None:
        found = find_palworld_game_dir(
            {
                "entries": [
                    {"type": "dir", "name": "palworld-old", "path": "/palworld-old"},
                    {"type": "dir", "name": "palworldxb", "path": "/palworldxb"},
                ]
            },
            service=service(),
            server=server("started"),
        )

        self.assertEqual(found, "/palworldxb")

    def test_palworld_name_fallback_cannot_override_contradictory_machine_metadata(self) -> None:
        ark_service = service(
            name="Palworld refugees",
            game="arksa",
            game_human="ARK: Survival Ascended",
            folder_short="arksa",
        )
        ark_server = replace(server("started"), game_short="arksa", game_human="ARK: Survival Ascended")

        selected = select_profile(ark_service, ark_server)

        self.assertEqual(selected.profile_id, "generic")

    def test_palworld_save_root_proves_one_world_with_level_save(self) -> None:
        async def run() -> None:
            client = AsyncMock()

            async def list_files(path=None):
                if path is None:
                    return {"entries": [{"type": "dir", "name": "palworldxb", "path": "palworldxb"}]}
                if path == "palworldxb/Pal/Saved/SaveGames/0":
                    return {
                        "entries": [
                            {"type": "dir", "name": "noise", "path": f"{path}/noise"},
                            {"type": "dir", "name": "world-id", "path": f"{path}/world-id"},
                        ]
                    }
                if path.endswith("/world-id"):
                    return {"entries": [{"type": "file", "name": "Level.sav", "path": f"{path}/Level.sav"}]}
                return {"entries": [{"type": "file", "name": "readme.txt", "path": f"{path}/readme.txt"}]}

            client.list_files.side_effect = list_files
            context = ProfileActionContext(
                client=client,
                service=service(),
                server=server("stopped"),
                status_fresh=True,
                using_cached_data=False,
            )
            self.assertEqual(
                await palworld_save_root(context),
                "palworldxb/Pal/Saved/SaveGames/0/world-id",
            )

        asyncio.run(run())

    def test_palworld_save_root_does_not_hide_authoritative_settings_read_failure(self) -> None:
        async def run() -> None:
            client = AsyncMock()

            async def list_files(path=None):
                if path is None:
                    return {"entries": [{"type": "dir", "name": "palworldxb", "path": "palworldxb"}]}
                if path == "palworldxb/Pal/Saved/Config/WindowsServer":
                    return {
                        "entries": [
                            {
                                "type": "file",
                                "name": "GameUserSettings.ini",
                                "path": f"{path}/GameUserSettings.ini",
                            }
                        ]
                    }
                return {"entries": []}

            client.list_files.side_effect = list_files
            client.download_file.side_effect = NitradoApiError("transport unavailable")
            context = ProfileActionContext(
                client=client,
                service=service(),
                server=server("stopped"),
                status_fresh=True,
                using_cached_data=False,
            )

            with self.assertRaisesRegex(NitradoApiError, "transport unavailable"):
                await palworld_save_root(context)

        asyncio.run(run())

    def test_palworld_save_root_uses_authoritative_dedicated_server_name(self) -> None:
        async def run() -> None:
            client = AsyncMock()

            async def list_files(path=None):
                if path is None:
                    return {"entries": [{"type": "dir", "name": "palworldxb", "path": "palworldxb"}]}
                if path == "palworldxb/Pal/Saved/Config/WindowsServer":
                    return {
                        "entries": [
                            {
                                "type": "file",
                                "name": "GameUserSettings.ini",
                                "path": f"{path}/GameUserSettings.ini",
                            }
                        ]
                    }
                if path == "palworldxb/Pal/Saved/SaveGames/0":
                    return {
                        "entries": [
                            {"type": "dir", "name": "old-world", "path": f"{path}/old-world"},
                            {"type": "dir", "name": "world-id", "path": f"{path}/world-id"},
                        ]
                    }
                if path.endswith(("/old-world", "/world-id")):
                    return {"entries": [{"type": "file", "name": "Level.sav", "path": f"{path}/Level.sav"}]}
                return {"entries": []}

            client.list_files.side_effect = list_files
            client.download_file.return_value = "[ServerSettings]\nDedicatedServerName=world-id\n"
            context = ProfileActionContext(
                client=client,
                service=service(),
                server=server("stopped"),
                status_fresh=True,
                using_cached_data=False,
            )

            self.assertEqual(
                await palworld_save_root(context),
                "palworldxb/Pal/Saved/SaveGames/0/world-id",
            )

        asyncio.run(run())

    def test_generic_safe_start_requires_fresh_stopped_status(self) -> None:
        async def run() -> None:
            profile = GenericProfile()
            verdict = await profile.can_start(
                ControlContext(service=service(), server=server("stopped"), status_fresh=True, using_cached_data=False)
            )

            self.assertEqual(verdict.state, CapabilityState.SUPPORTED)

        asyncio.run(run())

    def test_match_result_requires_real_boolean_matched_field(self) -> None:
        class MalformedProfile(GenericProfile):
            profile_id = "malformed"

            def matches(self, service, server=None):
                return MatchResult("false", confidence=1.0)  # type: ignore[arg-type]

        selected = select_profile(service(), profiles=(MalformedProfile(), GenericProfile()))

        self.assertEqual(selected.profile_id, "generic")

    def test_generic_safe_start_blocks_transition_but_force_allows(self) -> None:
        async def run() -> None:
            profile = GenericProfile()
            context = ControlContext(
                service=service(), server=server("updating"), status_fresh=True, using_cached_data=False
            )

            blocked = await profile.can_start(context)
            forced = await profile.can_start(
                ControlContext(
                    service=service(), server=server("updating"), status_fresh=True, using_cached_data=False, force=True
                )
            )

            self.assertEqual(blocked.state, CapabilityState.BLOCKED)
            self.assertTrue(blocked.overridable)
            self.assertEqual(forced.state, CapabilityState.SUPPORTED)

        asyncio.run(run())

    def test_palworld_profile_blocks_transition_but_force_allows(self) -> None:
        async def run() -> None:
            profile = PalworldProfile()

            blocked = await profile.can_start(
                ControlContext(
                    service=service(), server=server("gs_installation"), status_fresh=True, using_cached_data=False
                )
            )
            forced = await profile.can_start(
                ControlContext(
                    service=service(),
                    server=server("gs_installation"),
                    status_fresh=True,
                    using_cached_data=False,
                    force=True,
                )
            )

            self.assertEqual(blocked.state, CapabilityState.BLOCKED)
            self.assertTrue(blocked.overridable)
            self.assertEqual(forced.state, CapabilityState.SUPPORTED)

        asyncio.run(run())

    def test_palworld_insecure_rest_consent_is_admin_profile_option_not_entity(self) -> None:
        manifest = profile_extension_manifest(PalworldProfile())

        self.assertNotIn("allow_insecure_rest", {entity.key for entity in manifest.entities})
        option = next(item for item in manifest.profile_options if item.key == "allow_insecure_rest")
        self.assertFalse(option.default)
        self.assertEqual(option.option_type.value, "boolean")
        self.assertTrue(option.standard_options)
        self.assertTrue(option.onboarding)
        self.assertTrue(option.confirmation_required)
        self.assertEqual(option.acknowledgement_revision, 1)
        self.assertTrue(option.idle_shutdown_required)
        self.assertTrue(option.repair_if_unacknowledged)

    def test_palworld_idle_shutdown_requires_current_rest_consent(self) -> None:
        verdict = PalworldProfile().idle_shutdown_capability(
            ControlContext(
                service=service(),
                server=replace(
                    server("started"),
                    player_count=0,
                    player_names=(),
                    query_valid=True,
                    player_source="palworld_rest",
                ),
                status_fresh=True,
                using_cached_data=False,
                options={"allow_insecure_rest": False},
            )
        )

        self.assertEqual(verdict.state, CapabilityState.BLOCKED)
        self.assertIn("approved", verdict.reason)

    def test_palworld_idle_shutdown_does_not_complain_when_not_running(self) -> None:
        profile = PalworldProfile()
        for status in ("stopped", "starting", "stopping"):
            with self.subTest(status=status):
                verdict = profile.idle_shutdown_capability(
                    ControlContext(
                        service=service(),
                        server=replace(
                            server(status),
                            player_count=None,
                            player_names=(),
                            query_valid=False,
                            player_source=None,
                        ),
                        status_fresh=True,
                        using_cached_data=False,
                        options={"allow_insecure_rest": False},
                    )
                )
                self.assertEqual(verdict.state, CapabilityState.SUPPORTED)

    def test_palworld_idle_shutdown_defers_to_core_when_running_status_is_stale(self) -> None:
        verdict = PalworldProfile().idle_shutdown_capability(
            ControlContext(
                service=service(),
                server=replace(
                    server("started"),
                    player_count=None,
                    player_names=(),
                    query_valid=False,
                    player_source=None,
                ),
                status_fresh=False,
                using_cached_data=True,
                options={"allow_insecure_rest": False},
            )
        )

        self.assertEqual(verdict.state, CapabilityState.SUPPORTED)

    def test_typed_profile_options_validate_defaults_and_runtime_values(self) -> None:
        declarations = (
            ProfileOptionDeclaration("enabled", "Enabled", ProfileOptionType.BOOLEAN, default=False),
            ProfileOptionDeclaration(
                "limit",
                "Limit",
                ProfileOptionType.NUMBER,
                default=5,
                attributes={"min": 1, "max": 10, "step": 1},
            ),
            ProfileOptionDeclaration(
                "label", "Label", ProfileOptionType.TEXT, default="", attributes={"max_length": 8}
            ),
            ProfileOptionDeclaration(
                "mode",
                "Mode",
                ProfileOptionType.SELECT,
                default="safe",
                attributes={"options": ["safe", "fast"]},
            ),
            ProfileOptionDeclaration("password", "Password", ProfileOptionType.SECRET, default=""),
        )

        self.assertEqual(validate_profile_option_value(declarations[1], 7), 7)
        self.assertEqual(validate_profile_option_value(declarations[3], "fast"), "fast")
        with self.assertRaisesRegex(ValueError, "at most"):
            validate_profile_option_value(declarations[1], 11)
        with self.assertRaisesRegex(ValueError, "declared options"):
            validate_profile_option_value(declarations[3], "reckless")

        class TypedOptionsProfile(GenericProfile):
            profile_id = "typed_options"

            def profile_options(self):
                return declarations

        manifest = profile_extension_manifest(TypedOptionsProfile())
        self.assertEqual(len(manifest.profile_options), 5)

    def test_typed_action_inputs_reject_unknown_missing_and_malformed_fields(self) -> None:
        action = ActionDeclaration(
            key="broadcast",
            name="Broadcast",
            action_fn=async_callback(lambda context: None),
            inputs=(
                ActionInputDeclaration("message", "Message", ActionInputType.TEXT, required=True),
                ActionInputDeclaration(
                    "channel",
                    "Channel",
                    ActionInputType.SELECT,
                    default="global",
                    attributes={"options": ["global", "admin"]},
                ),
                ActionInputDeclaration("metadata", "Metadata", ActionInputType.JSON),
            ),
        )

        self.assertEqual(
            validate_action_payload(action, {"message": "hello"}),
            {"message": "hello", "channel": "global"},
        )
        with self.assertRaisesRegex(ValueError, "required"):
            validate_action_payload(action, {})
        with self.assertRaisesRegex(ValueError, "unknown fields"):
            validate_action_payload(action, {"message": "hello", "surprise": True})
        with self.assertRaisesRegex(ValueError, "field names must be text"):
            validate_action_payload(action, {1: "hello"})
        with self.assertRaisesRegex(ValueError, "finite JSON"):
            validate_action_payload(action, {"message": "hello", "metadata": {"bad": float("nan")}})

    def test_frozen_json_action_default_is_thawed_for_handler_payload(self) -> None:
        class DefaultProfile(GenericProfile):
            profile_id = "default_payload"

            def actions(self):
                return (
                    ActionDeclaration(
                        key="run",
                        name="Run",
                        action_fn=async_callback(lambda context: None),
                        inputs=(
                            ActionInputDeclaration(
                                "metadata",
                                "Metadata",
                                ActionInputType.JSON,
                                default={"modes": ["safe"]},
                            ),
                        ),
                    ),
                )

        action = profile_extension_manifest(DefaultProfile()).actions[0]
        payload = validate_action_payload(action, {})

        self.assertEqual(payload, {"metadata": {"modes": ["safe"]}})
        self.assertIsInstance(payload["metadata"], dict)
        self.assertIsInstance(payload["metadata"]["modes"], list)
        json.dumps(payload)


if __name__ == "__main__":
    unittest.main()
