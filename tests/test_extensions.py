"""Tests for generic profile extension dispatch."""

from __future__ import annotations

import asyncio
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from custom_components.nitrado_gameserver.api.nitrado import (
    NitradoService,
    ParsedServer,
)
from custom_components.nitrado_gameserver.coordinator import NitradoAccountCoordinator
from custom_components.nitrado_gameserver.extensions import (
    MAX_RESOURCE_CACHE_BYTES,
    ProfileExtensionError,
    _resource_cache_bytes,
    async_fetch_profile_resource,
    async_fetch_profile_surface_resources,
    async_run_lifecycle_hooks,
    async_run_profile_action,
    async_validate_profile,
    profile_action_context,
    profile_resource_descriptors,
    profile_surface_descriptors,
    resource_content_family,
)
from custom_components.nitrado_gameserver.models import ManagedServiceState
from custom_components.nitrado_gameserver.plugins.base import (
    SUPPORTED,
    ActionDeclaration,
    ActionInputDeclaration,
    ActionInputType,
    CapabilityState,
    EntityDeclaration,
    LifecycleEvent,
    LifecycleHookDeclaration,
    MatchResult,
    ResourceContentFamily,
    ResourceDeclaration,
    SurfaceDeclaration,
    ValidatorDeclaration,
    ValidatorDomain,
    ValidatorTarget,
    async_invoke_profile,
    blocked,
)
from custom_components.nitrado_gameserver.plugins.generic import GenericProfile
from custom_components.nitrado_gameserver.plugins.registry import register_profile
from custom_components.nitrado_gameserver.runtime import ServiceRuntime


def async_callback(callback):
    """Wrap a compact test callback in the required async mutation contract."""

    async def wrapped(*args, **kwargs):
        return callback(*args, **kwargs)

    return wrapped


def service_fixture() -> NitradoService:
    """Build a service fixture."""

    return NitradoService(
        service_id="123456",
        name="Extensible Server",
        game="extensible",
        game_human="Extensible",
        folder_short="extensible",
        type_human="Gameserver",
        raw_redacted={},
    )


def server_fixture() -> ParsedServer:
    """Build a server fixture."""

    return ParsedServer(
        service_id="123456",
        raw_status="started",
        server_name="Extensible Server",
        address="203.0.113.10:12345",
        game_short="extensible",
        game_human="Extensible",
        player_count=0,
        player_max=10,
        player_names=(),
        query_valid=True,
        player_source="profile",
        raw_redacted={},
    )


class RegistryBumpProfile(GenericProfile):
    """Nonmatching profile used to change the whole registry generation."""

    profile_id = "registry_bump_extensions"


class FakeClient:
    """Small client double for profile context tests."""

    def __init__(self) -> None:
        self.file_lists = {None: {"entries": [{"name": "settings.ini", "type": "file"}]}}
        self.downloads = {"/game/settings.ini": "difficulty=2"}

    async def list_files(self, service_id: str, directory: str | None = None) -> dict:
        """Return fake file listing."""

        return self.file_lists[directory]

    async def download_file(self, service_id: str, path: str) -> str:
        """Return fake file content."""

        return self.downloads[path]


class DispatchProfile:
    """Profile fixture with resources/actions/hooks/validators."""

    profile_id = "dispatch"
    name = "Dispatch"
    supported_games = ("dispatch",)
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
        return (
            EntityDeclaration(
                platform="sensor",
                key="profile_status",
                name="Profile Status",
                value_fn=lambda runtime: "ok",
            ),
        )

    def editable_files(self):
        return ()

    def resources(self):
        return (
            ResourceDeclaration(
                key="world_visual",
                name="World Visual",
                content_type="image/png",
                fetch_fn=lambda context: b"png",
                cache_seconds=60,
            ),
            ResourceDeclaration(
                key="world_state",
                name="World State",
                content_type="application/json",
                fetch_fn=self._fetch_world_state,
                cache_seconds=10,
                validators=("resource_ready",),
            ),
        )

    def actions(self):
        async def blocked_action(context):
            context.extra.setdefault("should_not_run", True)

        async def confirmed_action(context):
            return context.extra.setdefault("confirmed_action_ran", True)

        return (
            ActionDeclaration(
                key="record_payload",
                name="Record Payload",
                action_fn=self._record_payload,
                validators=("action_ready",),
            ),
            ActionDeclaration(
                key="blocked_action",
                name="Blocked Action",
                action_fn=blocked_action,
                validators=("always_block",),
            ),
            ActionDeclaration(
                key="confirmed_action",
                name="Confirmed Action",
                action_fn=confirmed_action,
                requires_confirmation=True,
            ),
        )

    def surfaces(self):
        return (
            SurfaceDeclaration(
                key="world_panel",
                name="World Panel",
                resources=("world_visual", "world_state"),
                actions=("record_payload",),
                controls=("profile_status",),
                renderer_hint="image_with_overlay",
                validators=("surface_ready",),
            ),
        )

    def lifecycle_hooks(self):
        async def second(context):
            context.extra.setdefault("hook_order", []).append("second")

        async def first(context):
            context.extra.setdefault("hook_order", []).append("first")

        async def blocked_hook(context):
            return blocked("Hook said no")

        async def overridable_block(context):
            return blocked("Forced hook can bypass this", overridable=True)

        async def exploding_after(context):
            raise RuntimeError("after boom")

        return (
            LifecycleHookDeclaration(
                key="second",
                event=LifecycleEvent.BEFORE_START,
                hook_fn=second,
                order=20,
            ),
            LifecycleHookDeclaration(
                key="first",
                event=LifecycleEvent.BEFORE_START,
                hook_fn=first,
                order=10,
            ),
            LifecycleHookDeclaration(
                key="blocked",
                event=LifecycleEvent.AFTER_STOP,
                hook_fn=blocked_hook,
                blocking=True,
            ),
            LifecycleHookDeclaration(
                key="overridable_block",
                event=LifecycleEvent.AFTER_START,
                hook_fn=overridable_block,
                blocking=True,
            ),
            LifecycleHookDeclaration(
                key="exploding_after",
                event=LifecycleEvent.AFTER_RESTORE,
                hook_fn=exploding_after,
                blocking=True,
            ),
        )

    def validators(self):
        return (
            ValidatorDeclaration(
                key="resource_ready",
                name="Resource Ready",
                target=ValidatorTarget.RESOURCE,
                validate_fn=lambda context, payload=None: SUPPORTED if context.server else blocked("No server"),
            ),
            ValidatorDeclaration(
                key="action_ready",
                name="Action Ready",
                target=ValidatorTarget.ACTION,
                validate_fn=lambda context, payload=None: SUPPORTED if context.server else blocked("No server"),
            ),
            ValidatorDeclaration(
                key="always_block",
                name="Always Block",
                target=ValidatorTarget.ACTION,
                validate_fn=lambda context, payload=None: blocked("Blocked by validator"),
            ),
            ValidatorDeclaration(
                key="surface_ready",
                name="Surface Ready",
                target=ValidatorTarget.SURFACE,
                validate_fn=lambda context, payload=None: SUPPORTED,
            ),
            ValidatorDeclaration(
                key="save_ready",
                name="Save Ready",
                target=ValidatorTarget.LIFECYCLE_HOOK,
                domains=(ValidatorDomain.SAVE,),
                validate_fn=lambda context, payload=None: SUPPORTED,
            ),
        )

    def _fetch_world_state(self, context):
        count = context.extra.get("resource_fetch_count", 0) + 1
        context.extra["resource_fetch_count"] = count
        return {"count": count, "payload": context.payload}

    async def _record_payload(self, context):
        context.extra["last_action"] = {
            "payload": context.payload,
            "now": context.now,
            "service_id": context.service_id,
        }
        return {"ok": True}


def runtime_fixture() -> ServiceRuntime:
    """Build a runtime with the dispatch profile selected."""

    runtime = ServiceRuntime(ManagedServiceState("123456"))
    runtime.update_service(service_fixture())
    runtime.update_server(server_fixture(), observed_at=100, status_fresh=True)
    runtime.profile = DispatchProfile()
    runtime.state.profile_id = "dispatch"
    return runtime


class ExtensionDispatchTests(unittest.TestCase):
    """Generic extension dispatcher tests."""

    def test_resource_content_type_classification(self) -> None:
        self.assertEqual(resource_content_family("image/png"), ResourceContentFamily.IMAGE)
        self.assertEqual(resource_content_family("application/json; charset=utf-8"), ResourceContentFamily.JSON)
        self.assertEqual(resource_content_family("application/vnd.example+json"), ResourceContentFamily.JSON)
        self.assertEqual(resource_content_family("text/plain"), ResourceContentFamily.TEXT)
        self.assertEqual(resource_content_family("application/octet-stream"), ResourceContentFamily.BINARY)
        self.assertEqual(resource_content_family("text/event-stream"), ResourceContentFamily.STREAM)

    def test_profile_action_context_exposes_safe_file_helpers(self) -> None:
        async def run() -> None:
            runtime = runtime_fixture()
            runtime.extra["core_only"] = "hidden"
            context = profile_action_context(FakeClient(), runtime, now=1000, payload={"x": 1})  # type: ignore[arg-type]

            self.assertEqual(context.service_id, "123456")
            self.assertEqual(context.now, 1000)
            self.assertEqual(context.payload, {"x": 1})
            self.assertIs(context.extra, runtime.profile_extra())
            self.assertNotIn("core_only", context.extra)
            self.assertEqual(await context.list_files(), {"entries": [{"name": "settings.ini", "type": "file"}]})
            self.assertEqual(await context.download_text_file("/game/settings.ini"), "difficulty=2")
            self.assertFalse(hasattr(context.client, "start_server"))
            self.assertFalse(hasattr(context.client, "stop_server"))
            self.assertFalse(hasattr(context.client, "upload_text_file"))

        asyncio.run(run())

    def test_resource_fetch_uses_validator_and_payload_aware_cache(self) -> None:
        async def run() -> None:
            runtime = runtime_fixture()
            client = FakeClient()

            first = await async_fetch_profile_resource(
                runtime,
                client,  # type: ignore[arg-type]
                "world_state",
                payload={"view": "a"},
                now=100,
            )
            second = await async_fetch_profile_resource(
                runtime,
                client,  # type: ignore[arg-type]
                "world_state",
                payload={"view": "b"},
                now=105,
            )
            third = await async_fetch_profile_resource(
                runtime,
                client,  # type: ignore[arg-type]
                "world_state",
                payload={"view": "b"},
                now=106,
            )
            fourth = await async_fetch_profile_resource(
                runtime,
                client,  # type: ignore[arg-type]
                "world_state",
                payload={"view": "c"},
                now=111,
            )

            self.assertEqual(first.data, {"count": 1, "payload": {"view": "a"}})
            self.assertEqual(first.content_family, ResourceContentFamily.JSON)
            self.assertFalse(second.cached)
            self.assertEqual(second.data, {"count": 2, "payload": {"view": "b"}})
            self.assertTrue(third.cached)
            self.assertEqual(third.data, second.data)
            self.assertFalse(fourth.cached)
            self.assertEqual(fourth.data, {"count": 3, "payload": {"view": "c"}})

        asyncio.run(run())

    def test_resource_and_surface_descriptors_are_generic_metadata(self) -> None:
        runtime = runtime_fixture()

        resources = {resource.key: resource for resource in profile_resource_descriptors(runtime)}
        surfaces = {surface.key: surface for surface in profile_surface_descriptors(runtime)}

        self.assertEqual(resources["world_visual"].content_family, ResourceContentFamily.IMAGE)
        self.assertEqual(resources["world_state"].content_family, ResourceContentFamily.JSON)
        self.assertIn("world_panel", surfaces)
        self.assertEqual(
            [resource.key for resource in surfaces["world_panel"].resources], ["world_visual", "world_state"]
        )
        self.assertEqual([control.key for control in surfaces["world_panel"].control_entities], ["profile_status"])
        self.assertEqual(
            [control.entity_key for control in surfaces["world_panel"].control_entities], ["dispatch_profile_status"]
        )
        self.assertEqual(surfaces["world_panel"].renderer_hint, "image_with_overlay")
        self.assertFalse(hasattr(surfaces["world_panel"], "missing_resources"))

    def test_cached_resource_still_runs_validators_and_generation_guard(self) -> None:
        class StatefulValidatorProfile(DispatchProfile):
            def validators(self):
                return (
                    ValidatorDeclaration(
                        key="resource_ready",
                        name="Resource Ready",
                        target=ValidatorTarget.RESOURCE,
                        validate_fn=lambda context, payload=None: (
                            SUPPORTED if context.extra.get("allow_resource", True) else blocked("Resource revoked")
                        ),
                    ),
                    *super().validators()[1:],
                )

        async def run() -> None:
            runtime = runtime_fixture()
            runtime.profile = StatefulValidatorProfile()
            client = FakeClient()
            first = await async_fetch_profile_resource(
                runtime,
                client,  # type: ignore[arg-type]
                "world_state",
                now=100,
            )
            self.assertFalse(first.cached)

            runtime.profile_extra()["allow_resource"] = False
            with self.assertRaises(ProfileExtensionError) as caught:
                await async_fetch_profile_resource(
                    runtime,
                    client,  # type: ignore[arg-type]
                    "world_state",
                    now=101,
                )

            self.assertEqual(caught.exception.verdict.reason, "Resource revoked")

        asyncio.run(run())

    def test_surface_resource_fetch_composes_resources_without_game_semantics(self) -> None:
        async def run() -> None:
            runtime = runtime_fixture()
            client = FakeClient()

            bundle = await async_fetch_profile_surface_resources(
                runtime,
                client,  # type: ignore[arg-type]
                "world_panel",
                payload={"view": "surface"},
                now=125,
            )

            self.assertEqual(bundle.surface.key, "world_panel")
            self.assertEqual([resource.key for resource in bundle.resources], ["world_visual", "world_state"])
            self.assertEqual(bundle.resources[0].content_family, ResourceContentFamily.IMAGE)
            self.assertEqual(bundle.resources[0].data, b"png")
            self.assertEqual(bundle.resources[1].content_family, ResourceContentFamily.JSON)
            self.assertEqual(bundle.resources[1].data["payload"], {"view": "surface"})

            with self.assertRaises(ProfileExtensionError) as caught:
                await async_fetch_profile_surface_resources(
                    runtime,
                    client,  # type: ignore[arg-type]
                    "missing_panel",
                    now=130,
                )

            self.assertIn("not found", caught.exception.verdict.reason)

        asyncio.run(run())

    def test_surface_resource_bundle_has_aggregate_output_limit(self) -> None:
        async def run() -> None:
            runtime = runtime_fixture()
            with (
                patch("custom_components.nitrado_gameserver.extensions.MAX_SURFACE_RESOURCE_BYTES", 2),
                self.assertRaises(ProfileExtensionError) as caught,
            ):
                await async_fetch_profile_surface_resources(
                    runtime,
                    FakeClient(),  # type: ignore[arg-type]
                    "world_panel",
                )
            self.assertIn("aggregate output limit", caught.exception.verdict.reason)

        asyncio.run(run())

    def test_action_dispatch_runs_validators_and_uses_context_payload(self) -> None:
        async def run() -> None:
            runtime = runtime_fixture()
            client = FakeClient()

            result = await async_run_profile_action(
                runtime,
                client,  # type: ignore[arg-type]
                "record_payload",
                payload={"difficulty": 3},
                now=200,
            )

            self.assertEqual(result, {"ok": True})
            self.assertEqual(
                runtime.profile_extra()["last_action"],
                {"payload": {"difficulty": 3}, "now": 200, "service_id": "123456"},
            )

            with self.assertRaises(ProfileExtensionError) as caught:
                await async_run_profile_action(runtime, client, "blocked_action")  # type: ignore[arg-type]

            self.assertEqual(caught.exception.verdict.reason, "Blocked by validator")
            self.assertNotIn("should_not_run", runtime.profile_extra())

        asyncio.run(run())

    def test_action_dispatch_enforces_confirmation(self) -> None:
        async def run() -> None:
            runtime = runtime_fixture()
            client = FakeClient()

            with self.assertRaises(ProfileExtensionError) as caught:
                await async_run_profile_action(runtime, client, "confirmed_action")  # type: ignore[arg-type]

            self.assertIn("requires explicit confirmation", caught.exception.verdict.reason)
            self.assertNotIn("confirmed_action_ran", runtime.profile_extra())

            result = await async_run_profile_action(
                runtime,
                client,  # type: ignore[arg-type]
                "confirmed_action",
                confirmed=True,
            )

            self.assertTrue(result)
            self.assertTrue(runtime.profile_extra()["confirmed_action_ran"])

        asyncio.run(run())

    def test_action_handler_exceptions_are_structured_extension_errors(self) -> None:
        class ExplodingActionProfile(DispatchProfile):
            def actions(self):
                return (
                    ActionDeclaration(
                        key="explode",
                        name="Explode",
                        action_fn=async_callback(lambda context: (_ for _ in ()).throw(RuntimeError("action boom"))),
                    ),
                )

            def surfaces(self):
                return ()

        async def run() -> None:
            runtime = runtime_fixture()
            runtime.profile = ExplodingActionProfile()
            client = FakeClient()

            with self.assertRaises(ProfileExtensionError) as caught:
                await async_run_profile_action(runtime, client, "explode")  # type: ignore[arg-type]

            self.assertEqual(caught.exception.verdict.state, CapabilityState.BLOCKED)
            self.assertEqual(
                caught.exception.verdict.reason,
                "Profile action explode failed safely; details were logged.",
            )
            self.assertNotIn("action boom", caught.exception.verdict.reason)

        asyncio.run(run())

    def test_action_results_must_be_bounded_finite_json(self) -> None:
        class BadOutputProfile(DispatchProfile):
            def actions(self):
                return (
                    ActionDeclaration("object_result", "Object", action_fn=async_callback(lambda context: object())),
                    ActionDeclaration(
                        "nan_result",
                        "NaN",
                        action_fn=async_callback(lambda context: {"value": float("nan")}),
                    ),
                    ActionDeclaration("huge_result", "Huge", action_fn=async_callback(lambda context: "x" * 1_100_000)),
                )

        async def run() -> None:
            runtime = runtime_fixture()
            runtime.profile = BadOutputProfile()
            for key in ("object_result", "nan_result", "huge_result"):
                with self.assertRaises(ProfileExtensionError):
                    await async_run_profile_action(runtime, FakeClient(), key)  # type: ignore[arg-type]

        asyncio.run(run())

    def test_action_result_is_detached_without_blocking_the_event_loop(self) -> None:
        serializing = threading.Event()
        from custom_components.nitrado_gameserver import extensions as extensions_module

        real_normalize = extensions_module._normalize_json_result

        def slow_normalize(*args, **kwargs):
            serializing.set()
            time.sleep(0.05)
            return real_normalize(*args, **kwargs)

        class SlowOutputProfile(DispatchProfile):
            def actions(self):
                async def slow_output(_context):
                    return {"ok": True}

                return (*super().actions(), ActionDeclaration("slow_output", "Slow Output", action_fn=slow_output))

        async def run() -> None:
            runtime = runtime_fixture()
            runtime.profile = SlowOutputProfile()
            with patch(
                "custom_components.nitrado_gameserver.extensions._normalize_json_result",
                side_effect=slow_normalize,
            ):
                action = asyncio.create_task(
                    async_run_profile_action(runtime, FakeClient(), "slow_output")  # type: ignore[arg-type]
                )
                for _ in range(100):
                    if serializing.is_set():
                        break
                    await asyncio.sleep(0)
                self.assertTrue(serializing.is_set())
                await asyncio.wait_for(asyncio.sleep(0.005), timeout=0.02)
                result = await action
            self.assertIs(type(result), dict)
            self.assertEqual(result, {"ok": True})

        asyncio.run(run())

    def test_allowed_action_verdict_is_normalized_to_json(self) -> None:
        class VerdictActionProfile(DispatchProfile):
            def actions(self):
                async def verdict(context):
                    return SUPPORTED

                return (ActionDeclaration("verdict", "Verdict", action_fn=verdict),)

            def surfaces(self):
                return ()

        async def run() -> None:
            runtime = runtime_fixture()
            runtime.profile = VerdictActionProfile()
            result = await async_run_profile_action(runtime, FakeClient(), "verdict")  # type: ignore[arg-type]

            self.assertEqual(
                result,
                {
                    "state": "supported",
                    "reason": "",
                    "source": "profile",
                    "overridable": False,
                },
            )

        asyncio.run(run())

    def test_action_aborts_if_selected_profile_changes_during_handler(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        class SlowActionProfile(DispatchProfile):
            async def _slow(self, context):
                started.set()
                await release.wait()
                return {"ok": True}

            def actions(self):
                return (ActionDeclaration(key="slow", name="Slow", action_fn=self._slow),)

            def surfaces(self):
                return ()

        async def run() -> None:
            runtime = runtime_fixture()
            runtime.profile = SlowActionProfile()
            generation = runtime.profile_generation
            task = asyncio.create_task(async_run_profile_action(runtime, FakeClient(), "slow"))  # type: ignore[arg-type]
            await started.wait()
            runtime.profile = DispatchProfile()
            runtime.profile_generation = generation + 1
            release.set()

            with self.assertRaises(ProfileExtensionError) as caught:
                await task

            self.assertIn("profile changed", caught.exception.verdict.reason)

        asyncio.run(run())

    def test_action_rejects_result_if_registry_changes_during_handler(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        class SlowActionProfile(DispatchProfile):
            async def _slow(self, context):
                started.set()
                await release.wait()
                return {"ok": True}

            def actions(self):
                return (ActionDeclaration(key="slow", name="Slow", action_fn=self._slow),)

            def surfaces(self):
                return ()

        async def run() -> None:
            runtime = runtime_fixture()
            runtime.profile = SlowActionProfile()
            task = asyncio.create_task(async_run_profile_action(runtime, FakeClient(), "slow"))  # type: ignore[arg-type]
            await started.wait()
            unregister = register_profile(RegistryBumpProfile)
            try:
                release.set()
                with self.assertRaises(ProfileExtensionError) as caught:
                    await task
                self.assertIn("profile changed", caught.exception.verdict.reason)
            finally:
                unregister()

        asyncio.run(run())

    def test_async_profile_action_has_core_owned_timeout(self) -> None:
        class HangingActionProfile(DispatchProfile):
            async def _hang(self, context):
                await asyncio.sleep(60)

            def actions(self):
                return (ActionDeclaration(key="hang", name="Hang", action_fn=self._hang),)

            def surfaces(self):
                return ()

        async def run() -> None:
            runtime = runtime_fixture()
            runtime.profile = HangingActionProfile()
            with (
                patch("custom_components.nitrado_gameserver.plugins.base.PROFILE_HANDLER_TIMEOUT_SECONDS", 0.001),
                self.assertRaises(ProfileExtensionError) as caught,
            ):
                await async_run_profile_action(runtime, FakeClient(), "hang")  # type: ignore[arg-type]

            self.assertEqual(
                caught.exception.verdict.reason,
                "Profile action hang failed safely; details were logged.",
            )

        asyncio.run(run())

    def test_sync_side_effect_free_callback_has_core_owned_timeout(self) -> None:
        async def run() -> None:
            with (
                patch("custom_components.nitrado_gameserver.plugins.base.PROFILE_HANDLER_TIMEOUT_SECONDS", 0.001),
                self.assertRaises(TimeoutError),
            ):
                await async_invoke_profile(lambda: time.sleep(0.05))

        asyncio.run(run())

    def test_typed_action_inputs_are_validated_before_handler(self) -> None:
        class TypedActionProfile(DispatchProfile):
            def actions(self):
                async def handler(context):
                    return context.payload

                return (
                    ActionDeclaration(
                        key="typed",
                        name="Typed",
                        action_fn=handler,
                        inputs=(
                            ActionInputDeclaration(
                                "message",
                                "Message",
                                ActionInputType.TEXT,
                                required=True,
                            ),
                        ),
                    ),
                )

            def surfaces(self):
                return ()

        async def run() -> None:
            runtime = runtime_fixture()
            runtime.profile = TypedActionProfile()
            result = await async_run_profile_action(
                runtime,
                FakeClient(),  # type: ignore[arg-type]
                "typed",
                payload={"message": "hello"},
            )
            self.assertEqual(result, {"message": "hello"})
            with self.assertRaises(ProfileExtensionError) as caught:
                await async_run_profile_action(
                    runtime,
                    FakeClient(),  # type: ignore[arg-type]
                    "typed",
                    payload={"unexpected": True},
                )
            self.assertIn("unknown fields", caught.exception.verdict.reason)

        asyncio.run(run())

    def test_resource_cache_is_bounded(self) -> None:
        async def run() -> None:
            runtime = runtime_fixture()
            client = FakeClient()

            for index in range(70):
                await async_fetch_profile_resource(
                    runtime,
                    client,  # type: ignore[arg-type]
                    "world_state",
                    payload={"index": index},
                    now=100 + index,
                )

            cache = runtime.extra["_profile_resource_cache"]
            self.assertLessEqual(len(cache), 64)
            self.assertNotIn(("dispatch", 1, "world_state", '{"index":0}'), cache)
            self.assertIn(("dispatch", 1, "world_state", '{"index":69}'), cache)

        asyncio.run(run())

    def test_binary_resource_cache_is_bounded_by_aggregate_bytes(self) -> None:
        class BinaryCacheProfile(DispatchProfile):
            def resources(self):
                return (
                    ResourceDeclaration(
                        key="blob",
                        name="Blob",
                        content_type="application/octet-stream",
                        fetch_fn=lambda context: b"x" * (8 * 1024 * 1024),
                        cache_seconds=60,
                    ),
                )

            def surfaces(self):
                return ()

        async def run() -> None:
            runtime = runtime_fixture()
            runtime.profile = BinaryCacheProfile()
            for index in range(6):
                await async_fetch_profile_resource(
                    runtime,
                    FakeClient(),  # type: ignore[arg-type]
                    "blob",
                    payload={"variant": index},
                    now=100 + index,
                )

            cache = runtime.extra["_profile_resource_cache"]
            self.assertLessEqual(_resource_cache_bytes(cache), MAX_RESOURCE_CACHE_BYTES)
            self.assertLessEqual(len(cache), 3)

        asyncio.run(run())

    def test_resource_handler_exceptions_are_structured_extension_errors(self) -> None:
        class ExplodingResourceProfile(DispatchProfile):
            def resources(self):
                return (
                    ResourceDeclaration(
                        key="explode",
                        name="Explode",
                        content_type="application/json",
                        fetch_fn=lambda context: (_ for _ in ()).throw(RuntimeError("resource boom")),
                    ),
                )

            def surfaces(self):
                return ()

        async def run() -> None:
            runtime = runtime_fixture()
            runtime.profile = ExplodingResourceProfile()
            client = FakeClient()

            with self.assertRaises(ProfileExtensionError) as caught:
                await async_fetch_profile_resource(runtime, client, "explode")  # type: ignore[arg-type]

            self.assertEqual(caught.exception.verdict.state, CapabilityState.BLOCKED)
            self.assertEqual(
                caught.exception.verdict.reason,
                "Profile resource explode failed safely; details were logged.",
            )
            self.assertNotIn("resource boom", caught.exception.verdict.reason)

        asyncio.run(run())

    def test_resource_result_must_match_declared_content_family(self) -> None:
        class BadContentProfile(DispatchProfile):
            def resources(self):
                return (
                    ResourceDeclaration(
                        key="json_as_bytes",
                        name="JSON As Bytes",
                        content_type="application/json",
                        fetch_fn=lambda context: b"not-json-data",
                    ),
                    ResourceDeclaration(
                        key="image_as_text",
                        name="Image As Text",
                        content_type="image/png",
                        fetch_fn=lambda context: "not-bytes",
                    ),
                )

            def surfaces(self):
                return ()

        async def run() -> None:
            runtime = runtime_fixture()
            runtime.profile = BadContentProfile()
            client = FakeClient()

            for key, family in (("json_as_bytes", "json"), ("image_as_text", "image")):
                with self.assertRaises(ProfileExtensionError) as caught:
                    await async_fetch_profile_resource(runtime, client, key)  # type: ignore[arg-type]
                self.assertIn(f"incompatible with {family} content", caught.exception.verdict.reason)

        asyncio.run(run())

    def test_json_resource_rejects_nonfinite_and_oversized_results(self) -> None:
        class BadResourceProfile(DispatchProfile):
            def resources(self):
                return (
                    ResourceDeclaration(
                        key="nan_json",
                        name="NaN",
                        content_type="application/json",
                        fetch_fn=lambda context: {"value": float("nan")},
                    ),
                    ResourceDeclaration(
                        key="huge_json",
                        name="Huge",
                        content_type="application/json",
                        fetch_fn=lambda context: {"value": "x" * 1_100_000},
                    ),
                )

        async def run() -> None:
            runtime = runtime_fixture()
            runtime.profile = BadResourceProfile()
            for key in ("nan_json", "huge_json"):
                with self.assertRaises(ProfileExtensionError):
                    await async_fetch_profile_resource(runtime, FakeClient(), key)  # type: ignore[arg-type]

        asyncio.run(run())

    def test_text_unknown_and_stream_resources_enforce_transport_contracts(self) -> None:
        class BadResourceProfile(DispatchProfile):
            def resources(self):
                return (
                    ResourceDeclaration(
                        key="huge_text",
                        name="Huge Text",
                        content_type="text/plain",
                        fetch_fn=lambda context: "x" * 1_100_000,
                    ),
                    ResourceDeclaration(
                        key="unknown_object",
                        name="Unknown Object",
                        content_type="application/x-example",
                        fetch_fn=lambda context: object(),
                    ),
                    ResourceDeclaration(
                        key="scalar_stream",
                        name="Scalar Stream",
                        content_type="text/event-stream",
                        fetch_fn=lambda context: "not a stream iterator",
                    ),
                )

            def surfaces(self):
                return ()

        async def run() -> None:
            runtime = runtime_fixture()
            runtime.profile = BadResourceProfile()
            for key in ("huge_text", "unknown_object", "scalar_stream"):
                with self.assertRaises(ProfileExtensionError):
                    await async_fetch_profile_resource(runtime, FakeClient(), key)  # type: ignore[arg-type]

        asyncio.run(run())

    def test_resource_aborts_if_selected_profile_changes_during_fetch(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        class SlowResourceProfile(DispatchProfile):
            async def _slow(self, context):
                started.set()
                await release.wait()
                return {"ok": True}

            def resources(self):
                return (
                    ResourceDeclaration(
                        key="slow",
                        name="Slow",
                        content_type="application/json",
                        fetch_fn=self._slow,
                    ),
                )

            def surfaces(self):
                return ()

        async def run() -> None:
            runtime = runtime_fixture()
            runtime.profile = SlowResourceProfile()
            generation = runtime.profile_generation
            task = asyncio.create_task(async_fetch_profile_resource(runtime, FakeClient(), "slow"))  # type: ignore[arg-type]
            await started.wait()
            runtime.profile = DispatchProfile()
            runtime.profile_generation = generation + 1
            release.set()

            with self.assertRaises(ProfileExtensionError) as caught:
                await task

            self.assertIn("profile changed", caught.exception.verdict.reason)

        asyncio.run(run())

    def test_resource_rejects_result_if_registry_changes_during_fetch(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        class SlowResourceProfile(DispatchProfile):
            async def _slow(self, context):
                started.set()
                await release.wait()
                return {"ok": True}

            def resources(self):
                return (
                    ResourceDeclaration(
                        key="slow",
                        name="Slow",
                        content_type="application/json",
                        fetch_fn=self._slow,
                    ),
                )

            def surfaces(self):
                return ()

        async def run() -> None:
            runtime = runtime_fixture()
            runtime.profile = SlowResourceProfile()
            task = asyncio.create_task(async_fetch_profile_resource(runtime, FakeClient(), "slow"))  # type: ignore[arg-type]
            await started.wait()
            unregister = register_profile(RegistryBumpProfile)
            try:
                release.set()
                with self.assertRaises(ProfileExtensionError) as caught:
                    await task
                self.assertIn("profile changed", caught.exception.verdict.reason)
            finally:
                unregister()

        asyncio.run(run())

    def test_resource_cache_is_profile_aware(self) -> None:
        class OtherProfile(DispatchProfile):
            profile_id = "other"
            name = "Other"

            def resources(self):
                return (
                    ResourceDeclaration(
                        key="world_visual",
                        name="World Visual",
                        content_type="image/png",
                        fetch_fn=lambda context: b"other",
                    ),
                    ResourceDeclaration(
                        key="world_state",
                        name="World State",
                        content_type="application/json",
                        fetch_fn=lambda context: {"profile": "other", "payload": context.payload},
                        cache_seconds=60,
                        validators=("resource_ready",),
                    ),
                )

        async def run() -> None:
            runtime = runtime_fixture()
            client = FakeClient()
            payload = {"same": True}

            first = await async_fetch_profile_resource(
                runtime,
                client,  # type: ignore[arg-type]
                "world_state",
                payload=payload,
                now=100,
            )
            runtime.profile = OtherProfile()
            runtime.state.profile_id = "other"
            second = await async_fetch_profile_resource(
                runtime,
                client,  # type: ignore[arg-type]
                "world_state",
                payload=payload,
                now=101,
            )

            self.assertEqual(first.data["count"], 1)
            self.assertEqual(second.data, {"profile": "other", "payload": payload})

        asyncio.run(run())

    def test_validator_runner_can_run_target_named_or_domain_filtered_validators(self) -> None:
        async def run() -> None:
            runtime = runtime_fixture()
            client = FakeClient()

            self.assertTrue(
                (
                    await async_validate_profile(
                        runtime,
                        client,  # type: ignore[arg-type]
                        target=ValidatorTarget.LIFECYCLE_HOOK,
                        domains=(ValidatorDomain.SAVE,),
                    )
                ).allowed
            )
            self.assertTrue(
                (
                    await async_validate_profile(
                        runtime,
                        client,  # type: ignore[arg-type]
                        target=ValidatorTarget.LIFECYCLE_HOOK,
                        validator_keys=("save_ready",),
                    )
                ).allowed
            )
            missing = await async_validate_profile(
                runtime,
                client,  # type: ignore[arg-type]
                target=ValidatorTarget.ACTION,
                validator_keys=("missing",),
            )

            self.assertEqual(missing.state, CapabilityState.BLOCKED)
            self.assertIn("missing", missing.reason)

        asyncio.run(run())

    def test_named_validators_must_match_requested_target(self) -> None:
        async def run() -> None:
            runtime = runtime_fixture()
            client = FakeClient()

            verdict = await async_validate_profile(
                runtime,
                client,  # type: ignore[arg-type]
                target=ValidatorTarget.ACTION,
                validator_keys=("surface_ready",),
            )

            self.assertEqual(verdict.state, CapabilityState.BLOCKED)
            self.assertIn("target mismatch", verdict.reason)
            self.assertIn("surface_ready is surface", verdict.reason)

        asyncio.run(run())

    def test_validator_exceptions_fail_closed_as_blocked_verdicts(self) -> None:
        class ExplodingValidatorProfile(DispatchProfile):
            def resources(self):
                return ()

            def actions(self):
                return ()

            def surfaces(self):
                return ()

            def lifecycle_hooks(self):
                return ()

            def validators(self):
                return (
                    ValidatorDeclaration(
                        key="exploding",
                        name="Exploding",
                        target=ValidatorTarget.ACTION,
                        validate_fn=lambda context, payload=None: (_ for _ in ()).throw(RuntimeError("validator boom")),
                    ),
                )

        async def run() -> None:
            runtime = runtime_fixture()
            runtime.profile = ExplodingValidatorProfile()
            client = FakeClient()

            verdict = await async_validate_profile(
                runtime,
                client,  # type: ignore[arg-type]
                target=ValidatorTarget.ACTION,
                validator_keys=("exploding",),
            )

            self.assertEqual(verdict.state, CapabilityState.BLOCKED)
            self.assertEqual(
                verdict.reason,
                "Profile validator exploding failed safely; details were logged.",
            )
            self.assertNotIn("validator boom", verdict.reason)

        asyncio.run(run())

    def test_lifecycle_hooks_run_in_order_and_block_when_configured(self) -> None:
        async def run() -> None:
            runtime = runtime_fixture()
            client = FakeClient()

            results = await async_run_lifecycle_hooks(
                runtime,
                client,  # type: ignore[arg-type]
                LifecycleEvent.BEFORE_START,
                now=300,
            )

            self.assertEqual([result.key for result in results], ["first", "second"])
            self.assertEqual(runtime.profile_extra()["hook_order"], ["first", "second"])

            with self.assertRaises(ProfileExtensionError) as caught:
                await async_run_lifecycle_hooks(runtime, client, LifecycleEvent.AFTER_STOP)  # type: ignore[arg-type]

            self.assertEqual(caught.exception.verdict.reason, "Hook said no")
            self.assertEqual(
                runtime.last_lifecycle_hook_results["after_stop"][0]["verdict"]["reason"],
                "Hook said no",
            )

        asyncio.run(run())

    def test_lifecycle_hook_aborts_if_selected_profile_changes(self) -> None:
        runtime = runtime_fixture()

        class ProfileChangingHook(DispatchProfile):
            def lifecycle_hooks(self):
                async def change_profile(context):
                    runtime.profile = DispatchProfile()
                    runtime.profile_generation += 1

                return (
                    LifecycleHookDeclaration(
                        key="change_profile",
                        event=LifecycleEvent.BEFORE_START,
                        hook_fn=change_profile,
                    ),
                )

        async def run() -> None:
            runtime.profile = ProfileChangingHook()

            with self.assertRaises(ProfileExtensionError) as caught:
                await async_run_lifecycle_hooks(
                    runtime,
                    FakeClient(),  # type: ignore[arg-type]
                    LifecycleEvent.BEFORE_START,
                )

            self.assertIn("profile changed", caught.exception.verdict.reason)

        asyncio.run(run())

    def test_lifecycle_hook_rejects_result_if_registry_changes_while_awaiting(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        class SlowHookProfile(DispatchProfile):
            def lifecycle_hooks(self):
                async def slow_hook(context):
                    started.set()
                    await release.wait()
                    return SUPPORTED

                return (
                    LifecycleHookDeclaration(
                        key="slow_hook",
                        event=LifecycleEvent.BEFORE_START,
                        hook_fn=slow_hook,
                    ),
                )

        async def run() -> None:
            runtime = runtime_fixture()
            runtime.profile = SlowHookProfile()
            task = asyncio.create_task(
                async_run_lifecycle_hooks(
                    runtime,
                    FakeClient(),  # type: ignore[arg-type]
                    LifecycleEvent.BEFORE_START,
                )
            )
            await started.wait()
            unregister = register_profile(RegistryBumpProfile)
            try:
                release.set()
                with self.assertRaises(ProfileExtensionError) as caught:
                    await task
                self.assertIn("profile changed", caught.exception.verdict.reason)
            finally:
                unregister()

        asyncio.run(run())

    def test_lifecycle_hooks_can_report_blocked_after_events_without_raising(self) -> None:
        async def run() -> None:
            runtime = runtime_fixture()
            client = FakeClient()

            results = await async_run_lifecycle_hooks(
                runtime,
                client,  # type: ignore[arg-type]
                LifecycleEvent.AFTER_STOP,
                raise_blocking=False,
            )

            self.assertEqual([result.key for result in results], ["blocked"])
            self.assertEqual(results[0].verdict.reason, "Hook said no")

        asyncio.run(run())

    def test_after_lifecycle_hook_exceptions_are_reported_without_raising_when_requested(self) -> None:
        async def run() -> None:
            runtime = runtime_fixture()
            client = FakeClient()

            results = await async_run_lifecycle_hooks(
                runtime,
                client,  # type: ignore[arg-type]
                LifecycleEvent.AFTER_RESTORE,
                raise_blocking=False,
            )

            self.assertEqual([result.key for result in results], ["exploding_after"])
            self.assertEqual(results[0].verdict.state, CapabilityState.BLOCKED)
            self.assertIn("exploding_after failed", results[0].verdict.reason)

        asyncio.run(run())

    def test_nonblocking_after_hook_contains_profile_generation_change(self) -> None:
        async def run() -> None:
            runtime = runtime_fixture()
            original = runtime.profile

            async def change_profile(context):
                runtime.profile = DispatchProfile()
                runtime.profile_generation += 1
                return SUPPORTED

            original.lifecycle_hooks = lambda: (  # type: ignore[method-assign]
                LifecycleHookDeclaration(
                    key="profile_change",
                    event=LifecycleEvent.AFTER_START,
                    hook_fn=change_profile,
                ),
            )

            results = await async_run_lifecycle_hooks(
                runtime,
                FakeClient(),  # type: ignore[arg-type]
                LifecycleEvent.AFTER_START,
                raise_blocking=False,
            )

            self.assertEqual(results[-1].verdict.state, CapabilityState.BLOCKED)
            self.assertIn("profile changed", results[-1].verdict.reason)

        asyncio.run(run())

    def test_lifecycle_hooks_allow_force_to_bypass_overridable_blocks(self) -> None:
        async def run() -> None:
            runtime = runtime_fixture()
            client = FakeClient()

            with self.assertRaises(ProfileExtensionError) as caught:
                await async_run_lifecycle_hooks(
                    runtime,
                    client,  # type: ignore[arg-type]
                    LifecycleEvent.AFTER_START,
                )

            self.assertEqual(caught.exception.verdict.reason, "Forced hook can bypass this")

            results = await async_run_lifecycle_hooks(
                runtime,
                client,  # type: ignore[arg-type]
                LifecycleEvent.AFTER_START,
                force=True,
            )

            self.assertEqual([result.key for result in results], ["overridable_block"])
            self.assertFalse(results[0].verdict.allowed)
            self.assertTrue(results[0].verdict.overridable)

        asyncio.run(run())

    def test_coordinator_exposes_extension_dispatch_methods(self) -> None:
        async def run() -> None:
            runtime = runtime_fixture()
            coordinator = NitradoAccountCoordinator(FakeClient(), services={"123456": runtime}, now_fn=lambda: 500)  # type: ignore[arg-type]

            action_result = await coordinator.async_run_profile_action(
                "123456",
                "record_payload",
                payload={"from": "coordinator"},
            )
            resource = await coordinator.async_fetch_profile_resource("123456", "world_state")
            bundle = await coordinator.async_fetch_profile_surface_resources("123456", "world_panel")
            verdict = await coordinator.async_validate_profile(
                "123456",
                ValidatorTarget.LIFECYCLE_HOOK,
                domains=(ValidatorDomain.SAVE,),
            )
            hooks = await coordinator.async_run_lifecycle_hooks("123456", LifecycleEvent.BEFORE_START)
            surface = coordinator.profile_surface_descriptor("123456", "world_panel")

            self.assertEqual(action_result, {"ok": True})
            self.assertEqual(runtime.profile_extra()["last_action"]["now"], 500)
            self.assertEqual(resource.content_type, "application/json")
            self.assertEqual([item.key for item in bundle.resources], ["world_visual", "world_state"])
            self.assertEqual(
                [item.key for item in coordinator.profile_resource_descriptors("123456")],
                ["world_visual", "world_state"],
            )
            self.assertEqual([item.key for item in coordinator.profile_surface_descriptors("123456")], ["world_panel"])
            self.assertEqual(surface.resources[0].content_family, ResourceContentFamily.IMAGE)
            self.assertTrue(verdict.allowed)
            self.assertEqual([hook.key for hook in hooks], ["first", "second"])

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
