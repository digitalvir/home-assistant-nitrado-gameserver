"""Tests for safe editable-file plumbing."""

from __future__ import annotations

import asyncio
import sys
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
from custom_components.nitrado_gameserver.editable_files import (
    async_apply_editable_file,
    async_preview_editable_file,
    async_read_editable_file,
    async_rollback_editable_file,
)
from custom_components.nitrado_gameserver.extensions import ProfileExtensionError
from custom_components.nitrado_gameserver.models import ManagedServiceState
from custom_components.nitrado_gameserver.plugins.base import (
    SUPPORTED,
    CapabilityState,
    EditableFileDeclaration,
    LifecycleEvent,
    LifecycleHookDeclaration,
    MatchResult,
    blocked,
)
from custom_components.nitrado_gameserver.plugins.generic import GenericProfile
from custom_components.nitrado_gameserver.plugins.registry import register_profile
from custom_components.nitrado_gameserver.runtime import ServiceRuntime


def service_fixture() -> NitradoService:
    """Build a service fixture."""

    return NitradoService(
        service_id="123456",
        name="Editable Server",
        game="editable",
        game_human="Editable",
        folder_short="editable",
        type_human="Gameserver",
        raw_redacted={},
    )


def server_fixture(status: str = "stopped") -> ParsedServer:
    """Build a server fixture."""

    return ParsedServer(
        service_id="123456",
        raw_status=status,
        server_name="Editable Server",
        address="203.0.113.10:12345",
        game_short="editable",
        game_human="Editable",
        player_count=0,
        player_max=10,
        player_names=(),
        query_valid=True,
        player_source="profile",
        raw_redacted={},
    )


class FakeClient:
    """Small client double for editable-file tests."""

    def __init__(self) -> None:
        self.downloads = {
            "/game/settings.ini": "Password=old-secret\ndifficulty=1\n",
            "/game/settings.ini.nitrado_gameserver.backup.100": "Password=backup-secret\ndifficulty=2\n",
        }
        self.uploads: list[tuple[str, str, str]] = []

    async def download_file(self, service_id: str, path: str) -> str:
        """Return configured file content."""

        return self.downloads[path]

    async def upload_text_file(self, service_id: str, path: str, content: str) -> None:
        """Record upload writes and make later downloads see them."""

        self.uploads.append((service_id, path, content))
        self.downloads[path] = content


class CorruptTargetClient(FakeClient):
    """Client that corrupts the proposed target write but permits recovery."""

    async def upload_text_file(self, service_id: str, path: str, content: str) -> None:
        await super().upload_text_file(service_id, path, content)
        if path == "/game/settings.ini" and "new-secret" in content:
            self.downloads[path] = "corrupted"


class CorruptRollbackClient(FakeClient):
    """Client that corrupts a rollback target but permits restoring current content."""

    async def upload_text_file(self, service_id: str, path: str, content: str) -> None:
        await super().upload_text_file(service_id, path, content)
        if path == "/game/settings.ini" and "backup-secret" in content:
            self.downloads[path] = "corrupted"


class SlowClient(FakeClient):
    """Client that records concurrent uploads."""

    def __init__(self) -> None:
        super().__init__()
        self.active_uploads = 0
        self.max_active_uploads = 0

    async def upload_text_file(self, service_id: str, path: str, content: str) -> None:
        self.active_uploads += 1
        self.max_active_uploads = max(self.max_active_uploads, self.active_uploads)
        await asyncio.sleep(0.005)
        await super().upload_text_file(service_id, path, content)
        self.active_uploads -= 1


class CancelAfterTargetWriteClient(FakeClient):
    """Pause after mutating the target so cancellation tests rollback."""

    def __init__(self) -> None:
        super().__init__()
        self.target_written = asyncio.Event()

    async def upload_text_file(self, service_id: str, path: str, content: str) -> None:
        await super().upload_text_file(service_id, path, content)
        if path == "/game/settings.ini" and "new-secret" in content:
            self.target_written.set()
            await asyncio.Event().wait()


class RepeatedCancelRecoveryClient(CancelAfterTargetWriteClient):
    """Pause recovery so a second cancellation can attack cleanup."""

    def __init__(self) -> None:
        super().__init__()
        self.rollback_started = asyncio.Event()
        self.release_rollback = asyncio.Event()

    async def upload_text_file(self, service_id: str, path: str, content: str) -> None:
        if path == "/game/settings.ini" and "old-secret" in content:
            self.rollback_started.set()
            await self.release_rollback.wait()
        await super().upload_text_file(service_id, path, content)


class ProfileChangingClient(FakeClient):
    """Client that changes profile selection during the target upload."""

    def __init__(self, runtime: ServiceRuntime) -> None:
        super().__init__()
        self.runtime = runtime

    async def upload_text_file(self, service_id: str, path: str, content: str) -> None:
        await super().upload_text_file(service_id, path, content)
        if path == "/game/settings.ini" and "new-secret" in content:
            self.runtime.profile = EditableProfile()
            self.runtime.profile_generation += 1


class ChangeDuringBackupClient(FakeClient):
    """Simulate an external edit while a regular backup is verified."""

    async def upload_text_file(self, service_id: str, path: str, content: str) -> None:
        await super().upload_text_file(service_id, path, content)
        if ".nitrado_gameserver.backup." in path:
            self.downloads["/game/settings.ini"] = "Password=external\ndifficulty=3\n"


class RegistryBumpProfile(GenericProfile):
    """Nonmatching profile used to change the whole registry generation."""

    profile_id = "registry_bump_editable_files"


class RegistryChangeDuringBackupClient(FakeClient):
    """Register another profile during backup creation before target mutation."""

    def __init__(self) -> None:
        super().__init__()
        self.unregister = None

    async def upload_text_file(self, service_id: str, path: str, content: str) -> None:
        await super().upload_text_file(service_id, path, content)
        if ".nitrado_gameserver.backup." in path and self.unregister is None:
            self.unregister = register_profile(RegistryBumpProfile)


class ChangeDuringPreRollbackBackupClient(FakeClient):
    """Simulate an external edit while a pre-rollback backup is verified."""

    async def upload_text_file(self, service_id: str, path: str, content: str) -> None:
        await super().upload_text_file(service_id, path, content)
        if ".nitrado_gameserver.pre-rollback." in path:
            self.downloads["/game/settings.ini"] = "Password=external\ndifficulty=3\n"


class EditableProfile:
    """Profile fixture declaring one editable settings file."""

    profile_id = "editable"
    name = "Editable"
    supported_games = ("editable",)
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
        return ()

    def editable_files(self):
        return (
            EditableFileDeclaration(
                key="settings",
                name="Settings",
                path_fn=lambda context: "/game/settings.ini",
                parser=parse_settings,
                serializer=serialize_settings,
                validator=validate_settings,
                redactor=redact_settings,
                requires_restart=True,
                requires_stopped=True,
                create_backup=True,
            ),
        )

    def resources(self):
        return ()

    def actions(self):
        return ()

    def surfaces(self):
        return ()

    def lifecycle_hooks(self):
        async def before_file_write(context):
            context.extra.setdefault("file_hooks", []).append("before")

        async def after_file_write(context):
            context.extra.setdefault("file_hooks", []).append("after")

        async def before_restore(context):
            context.extra.setdefault("restore_hooks", []).append("before")

        async def after_restore(context):
            context.extra.setdefault("restore_hooks", []).append("after")

        return (
            LifecycleHookDeclaration(
                key="before_file_write",
                event=LifecycleEvent.BEFORE_FILE_WRITE,
                hook_fn=before_file_write,
                order=10,
            ),
            LifecycleHookDeclaration(
                key="after_file_write",
                event=LifecycleEvent.AFTER_FILE_WRITE,
                hook_fn=after_file_write,
                order=10,
            ),
            LifecycleHookDeclaration(
                key="before_restore",
                event=LifecycleEvent.BEFORE_RESTORE,
                hook_fn=before_restore,
                order=10,
            ),
            LifecycleHookDeclaration(
                key="after_restore",
                event=LifecycleEvent.AFTER_RESTORE,
                hook_fn=after_restore,
                order=10,
            ),
        )

    def validators(self):
        return ()


class StructuredEditableProfile(EditableProfile):
    """Editable fixture exposing a browser-safe model and exact operation patcher."""

    profile_id = "structured_editable"

    def editable_files(self):
        def modeler(text):
            parsed = parse_settings(text)
            return {
                "settings": [
                    {"key": key, "raw_value": None if key == "Password" else value, "sensitive": key == "Password"}
                    for key, value in parsed.items()
                ]
            }

        def patcher(text, operations):
            result = text
            for operation in operations:
                key = operation["key"]
                old = next(line for line in result.splitlines() if line.startswith(f"{key}="))
                result = result.replace(old, f"{key}={operation['raw_value']}")
            return result

        declaration = super().editable_files()[0]
        return (
            EditableFileDeclaration(
                key=declaration.key,
                name=declaration.name,
                path_fn=declaration.path_fn,
                parser=declaration.parser,
                serializer=declaration.serializer,
                validator=declaration.validator,
                redactor=declaration.redactor,
                editor_modeler=modeler,
                editor_patcher=patcher,
                requires_restart=True,
                requires_stopped=True,
                create_backup=True,
            ),
        )

    def validators(self):
        return ()


class BadParserProfile(EditableProfile):
    """Profile fixture with a broken parser."""

    def editable_files(self):
        return (
            EditableFileDeclaration(
                key="settings",
                name="Settings",
                path_fn=lambda context: "/game/settings.ini",
                parser=lambda text: (_ for _ in ()).throw(ValueError("bad syntax")),
                serializer=serialize_settings,
                requires_stopped=True,
            ),
        )


class BadSerializerProfile(EditableProfile):
    """Profile fixture with a broken serializer."""

    def editable_files(self):
        return (
            EditableFileDeclaration(
                key="settings",
                name="Settings",
                path_fn=lambda context: "/game/settings.ini",
                parser=parse_settings,
                serializer=lambda parsed: (_ for _ in ()).throw(ValueError("bad value")),
                requires_stopped=True,
            ),
        )


class NonTextSerializerProfile(EditableProfile):
    """Profile fixture with a serializer returning the wrong type."""

    def editable_files(self):
        return (
            EditableFileDeclaration(
                key="settings",
                name="Settings",
                path_fn=lambda context: "/game/settings.ini",
                parser=parse_settings,
                serializer=lambda parsed: 123,  # type: ignore[return-value]
                requires_stopped=True,
            ),
        )


class ExplodingValidatorProfile(EditableProfile):
    """Profile fixture with a validator that raises."""

    def editable_files(self):
        return (
            EditableFileDeclaration(
                key="settings",
                name="Settings",
                path_fn=lambda context: "/game/settings.ini",
                parser=parse_settings,
                serializer=serialize_settings,
                validator=lambda parsed: (_ for _ in ()).throw(RuntimeError("validator boom")),
                requires_stopped=True,
            ),
        )


class ExplodingPathProfile(EditableProfile):
    """Profile fixture with a path resolver that raises."""

    def editable_files(self):
        return (
            EditableFileDeclaration(
                key="settings",
                name="Settings",
                path_fn=lambda context: (_ for _ in ()).throw(RuntimeError("path boom")),
                parser=parse_settings,
                serializer=serialize_settings,
                requires_stopped=True,
            ),
        )


class ExplodingRedactorProfile(EditableProfile):
    """Profile fixture with a redactor that raises."""

    def editable_files(self):
        return (
            EditableFileDeclaration(
                key="settings",
                name="Settings",
                path_fn=lambda context: "/game/settings.ini",
                parser=parse_settings,
                serializer=serialize_settings,
                redactor=lambda text: (_ for _ in ()).throw(RuntimeError("redactor boom")),
                requires_stopped=True,
            ),
        )


class NonTextRedactorProfile(EditableProfile):
    """Profile fixture with a redactor returning the wrong type."""

    def editable_files(self):
        return (
            EditableFileDeclaration(
                key="settings",
                name="Settings",
                path_fn=lambda context: "/game/settings.ini",
                parser=parse_settings,
                serializer=serialize_settings,
                redactor=lambda text: 123,  # type: ignore[return-value]
                requires_stopped=True,
            ),
        )


class DuplicateEditableFileProfile(EditableProfile):
    """Profile fixture with an invalid duplicate editable-file declaration."""

    def editable_files(self):
        return (
            EditableFileDeclaration(
                key="settings",
                name="Settings",
                path_fn=lambda context: "/game/settings.ini",
                requires_stopped=True,
            ),
            EditableFileDeclaration(
                key="settings",
                name="Settings Again",
                path_fn=lambda context: "/game/settings-again.ini",
                requires_stopped=True,
            ),
        )


class SamePathEditableFileProfile(EditableProfile):
    """Two distinct declarations that intentionally resolve to one remote path."""

    def editable_files(self):
        common = {
            "path_fn": lambda context: "/game/settings.ini",
            "parser": parse_settings,
            "serializer": serialize_settings,
            "validator": validate_settings,
            "redactor": redact_settings,
            "requires_stopped": True,
            "create_backup": True,
        }
        return (
            EditableFileDeclaration(key="basic_settings", name="Basic Settings", **common),
            EditableFileDeclaration(key="advanced_settings", name="Advanced Settings", **common),
        )


class BlockingBeforeFileWriteProfile(EditableProfile):
    """Profile fixture that blocks before any remote file mutation."""

    def lifecycle_hooks(self):
        async def block_write(context):
            return blocked("No writes today")

        return (
            LifecycleHookDeclaration(
                key="before_file_write",
                event=LifecycleEvent.BEFORE_FILE_WRITE,
                hook_fn=block_write,
                order=10,
            ),
        )


class UploadOrderProfile(EditableProfile):
    """Profile fixture that records upload count when the before-write hook runs."""

    def __init__(self, observed_client: FakeClient) -> None:
        self.observed_client = observed_client

    def lifecycle_hooks(self):
        async def record_count(context):
            context.extra.setdefault("upload_counts", []).append(len(self.observed_client.uploads))

        return (
            LifecycleHookDeclaration(
                key="before_file_write",
                event=LifecycleEvent.BEFORE_FILE_WRITE,
                hook_fn=record_count,
                order=10,
            ),
            LifecycleHookDeclaration(
                key="after_file_write",
                event=LifecycleEvent.AFTER_FILE_WRITE,
                hook_fn=record_count,
                order=10,
            ),
        )


class ConcurrentFileChangeProfile(EditableProfile):
    """Profile fixture simulating another actor changing the file after preview."""

    def __init__(self, observed_client: FakeClient) -> None:
        self.observed_client = observed_client

    def lifecycle_hooks(self):
        async def change_file(context):
            self.observed_client.downloads["/game/settings.ini"] = "Password=someone-else\ndifficulty=3\n"

        return (
            LifecycleHookDeclaration(
                key="concurrent_change",
                event=LifecycleEvent.BEFORE_FILE_WRITE,
                hook_fn=change_file,
                order=10,
            ),
        )


def parse_settings(text: str) -> dict[str, str]:
    """Parse tiny key=value fixture settings."""

    parsed: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            parsed[key] = value
    return parsed


def serialize_settings(parsed: dict[str, str]) -> str:
    """Serialize tiny key=value fixture settings."""

    return "".join(f"{key}={value}\n" for key, value in parsed.items())


def validate_settings(parsed: dict[str, str]):
    """Block invalid difficulty values."""

    if parsed.get("difficulty") not in {"1", "2", "3"}:
        return blocked("Difficulty must be 1, 2, or 3")
    return SUPPORTED


def redact_settings(text: str) -> str:
    """Redact fixture password."""

    return (
        text.replace("old-secret", "[redacted]")
        .replace("new-secret", "[redacted]")
        .replace("backup-secret", "[redacted]")
    )


def runtime_fixture(status: str = "stopped") -> ServiceRuntime:
    """Build a runtime with editable profile selected."""

    runtime = ServiceRuntime(ManagedServiceState("123456"))
    runtime.update_service(service_fixture())
    runtime.update_server(server_fixture(status), observed_at=100, status_fresh=True)
    runtime.profile = EditableProfile()
    runtime.state.profile_id = "editable"
    return runtime


def runtime_with_profile(profile: object, status: str = "stopped") -> ServiceRuntime:
    """Build a runtime with a custom editable profile fixture."""

    runtime = runtime_fixture(status)
    runtime.profile = profile  # type: ignore[assignment]
    runtime.state.profile_id = getattr(profile, "profile_id", None)
    return runtime


class EditableFileTests(unittest.TestCase):
    """Editable-file workflow tests."""

    def test_read_editable_file_resolves_path_parses_and_redacts(self) -> None:
        async def run() -> None:
            snapshot = await async_read_editable_file(runtime_fixture(), FakeClient(), "settings")  # type: ignore[arg-type]

            self.assertEqual(snapshot.path, "/game/settings.ini")
            self.assertEqual(snapshot.parsed, {"Password": "old-secret", "difficulty": "1"})
            self.assertIn("[redacted]", snapshot.redacted_text)
            self.assertNotIn("old-secret", snapshot.redacted_text)

        asyncio.run(run())

    def test_private_parser_objects_reach_profile_serializer_and_validator(self) -> None:
        class ParsedSettings:
            def __init__(self, text: str) -> None:
                self.text = text

        validated: list[ParsedSettings] = []

        def validate(parsed: ParsedSettings):
            validated.append(parsed)
            return SUPPORTED

        class DomainObjectProfile(EditableProfile):
            profile_id = "editable_domain_object"

            def editable_files(self):
                return (
                    EditableFileDeclaration(
                        key="settings",
                        name="Settings",
                        path_fn=lambda context: "/game/settings.ini",
                        parser=ParsedSettings,
                        serializer=lambda parsed: parsed.text,
                        validator=validate,
                        requires_stopped=True,
                    ),
                )

        async def run() -> None:
            runtime = runtime_with_profile(DomainObjectProfile())
            snapshot = await async_read_editable_file(runtime, FakeClient(), "settings")  # type: ignore[arg-type]
            self.assertIsNone(snapshot.parsed)

            preview = await async_preview_editable_file(
                runtime,
                FakeClient(),  # type: ignore[arg-type]
                "settings",
                "Password=changed\ndifficulty=2\n",
            )
            self.assertTrue(preview.verdict.allowed)
            self.assertIsNone(preview.parsed)
            self.assertEqual(preview.proposed_text, "Password=changed\ndifficulty=2\n")
            self.assertIsInstance(validated[-1], ParsedSettings)

        asyncio.run(run())

    def test_read_editable_file_reports_invalid_profile_manifest(self) -> None:
        async def run() -> None:
            with self.assertRaises(ProfileExtensionError) as caught:
                await async_read_editable_file(
                    runtime_with_profile(DuplicateEditableFileProfile()),
                    FakeClient(),  # type: ignore[arg-type]
                    "settings",
                )

            self.assertIn("manifest is invalid", caught.exception.verdict.reason)
            self.assertNotIn("duplicate", caught.exception.verdict.reason)

        asyncio.run(run())

    def test_structured_model_and_operations_stay_server_side(self) -> None:
        async def run() -> None:
            runtime = runtime_with_profile(StructuredEditableProfile())
            client = FakeClient()
            snapshot = await async_read_editable_file(runtime, client, "settings")  # type: ignore[arg-type]
            self.assertEqual(snapshot.editor_model["settings"][0]["raw_value"], None)
            self.assertNotIn("old-secret", str(snapshot.editor_model))

            preview = await async_preview_editable_file(
                runtime,
                client,  # type: ignore[arg-type]
                "settings",
                [{"op": "set", "key": "difficulty", "raw_value": "2"}],
                value_is_parsed=True,
                expected_source_revision=snapshot.revision,
            )
            self.assertTrue(preview.verdict.allowed)
            self.assertEqual(preview.proposed_text, "Password=old-secret\ndifficulty=2\n")

            client.downloads["/game/settings.ini"] = "Password=external\ndifficulty=1\n"
            with self.assertRaises(ProfileExtensionError) as caught:
                await async_preview_editable_file(
                    runtime,
                    client,  # type: ignore[arg-type]
                    "settings",
                    [{"op": "set", "key": "difficulty", "raw_value": "3"}],
                    value_is_parsed=True,
                    expected_source_revision=snapshot.revision,
                )
            self.assertIn("changed after this editor was opened", caught.exception.verdict.reason)

        asyncio.run(run())

    def test_preview_builds_redacted_diff_and_blocks_invalid_settings(self) -> None:
        async def run() -> None:
            runtime = runtime_fixture()
            client = FakeClient()
            preview = await async_preview_editable_file(
                runtime,
                client,  # type: ignore[arg-type]
                "settings",
                {"Password": "new-secret", "difficulty": "2"},
                value_is_parsed=True,
            )

            self.assertTrue(preview.verdict.allowed)
            self.assertTrue(preview.changed)
            self.assertEqual(len(preview.source_revision), 64)
            self.assertEqual(len(preview.proposed_revision), 64)
            self.assertIn("-difficulty=1", preview.diff)
            self.assertIn("+difficulty=2", preview.diff)
            self.assertNotIn("old-secret", preview.redacted_diff)
            self.assertNotIn("new-secret", preview.redacted_diff)

            blocked_preview = await async_preview_editable_file(
                runtime,
                client,  # type: ignore[arg-type]
                "settings",
                {"Password": "new-secret", "difficulty": "9"},
                value_is_parsed=True,
            )

            self.assertEqual(blocked_preview.verdict.state, CapabilityState.BLOCKED)
            self.assertEqual(blocked_preview.verdict.reason, "Difficulty must be 1, 2, or 3")

        asyncio.run(run())

    def test_apply_requires_the_exact_previewed_source_and_proposal(self) -> None:
        async def run() -> None:
            runtime = runtime_fixture()
            client = FakeClient()
            preview = await async_preview_editable_file(
                runtime,
                client,  # type: ignore[arg-type]
                "settings",
                {"Password": "new-secret", "difficulty": "2"},
                value_is_parsed=True,
            )

            with self.assertRaises(ProfileExtensionError) as source_error:
                await async_apply_editable_file(
                    runtime,
                    client,  # type: ignore[arg-type]
                    "settings",
                    {"Password": "new-secret", "difficulty": "2"},
                    value_is_parsed=True,
                    expected_source_revision="0" * 64,
                    expected_proposed_revision=preview.proposed_revision,
                )
            self.assertIn("changed after preview", source_error.exception.verdict.reason)
            self.assertEqual(client.uploads, [])

            with self.assertRaises(ProfileExtensionError) as proposal_error:
                await async_apply_editable_file(
                    runtime,
                    client,  # type: ignore[arg-type]
                    "settings",
                    {"Password": "new-secret", "difficulty": "2"},
                    value_is_parsed=True,
                    expected_source_revision=preview.source_revision,
                    expected_proposed_revision="f" * 64,
                )
            self.assertIn("no longer matches", proposal_error.exception.verdict.reason)
            self.assertEqual(client.uploads, [])

            with self.assertRaises(ProfileExtensionError) as path_error:
                await async_apply_editable_file(
                    runtime,
                    client,  # type: ignore[arg-type]
                    "settings",
                    {"Password": "new-secret", "difficulty": "2"},
                    value_is_parsed=True,
                    expected_source_revision=preview.source_revision,
                    expected_proposed_revision=preview.proposed_revision,
                    expected_path="/different/settings.ini",
                )
            self.assertIn("path changed after preview", path_error.exception.verdict.reason)
            self.assertEqual(client.uploads, [])

        asyncio.run(run())

    def test_parser_and_serializer_failures_are_blocked_verdicts(self) -> None:
        async def run() -> None:
            with self.assertRaises(ProfileExtensionError) as parser_error:
                await async_preview_editable_file(
                    runtime_with_profile(BadParserProfile()),
                    FakeClient(),  # type: ignore[arg-type]
                    "settings",
                    "this is not valid",
                )

            self.assertEqual(
                parser_error.exception.verdict.reason,
                "Settings parser failed safely; details were logged.",
            )
            self.assertEqual(parser_error.exception.verdict.state, CapabilityState.BLOCKED)

            with self.assertRaises(ProfileExtensionError) as serializer_error:
                await async_preview_editable_file(
                    runtime_with_profile(BadSerializerProfile()),
                    FakeClient(),  # type: ignore[arg-type]
                    "settings",
                    {"difficulty": "2"},
                    value_is_parsed=True,
                )

            self.assertEqual(
                serializer_error.exception.verdict.reason,
                "Settings serializer failed safely; details were logged.",
            )
            self.assertEqual(serializer_error.exception.verdict.state, CapabilityState.BLOCKED)

            with self.assertRaises(ProfileExtensionError) as non_text_error:
                await async_preview_editable_file(
                    runtime_with_profile(NonTextSerializerProfile()),
                    FakeClient(),  # type: ignore[arg-type]
                    "settings",
                    {"difficulty": "2"},
                    value_is_parsed=True,
                )

            self.assertIn("serializer returned non-text content", non_text_error.exception.verdict.reason)
            self.assertEqual(non_text_error.exception.verdict.state, CapabilityState.BLOCKED)

        asyncio.run(run())

    def test_editable_file_validator_exceptions_are_blocked_verdicts(self) -> None:
        async def run() -> None:
            preview = await async_preview_editable_file(
                runtime_with_profile(ExplodingValidatorProfile()),
                FakeClient(),  # type: ignore[arg-type]
                "settings",
                {"difficulty": "2"},
                value_is_parsed=True,
            )

            self.assertEqual(preview.verdict.state, CapabilityState.BLOCKED)
            self.assertEqual(
                preview.verdict.reason,
                "Profile editable-file validator settings failed safely; details were logged.",
            )
            self.assertNotIn("validator boom", preview.verdict.reason)

        asyncio.run(run())

    def test_path_and_redactor_failures_are_structured_extension_errors(self) -> None:
        async def run() -> None:
            with self.assertRaises(ProfileExtensionError) as path_error:
                await async_read_editable_file(
                    runtime_with_profile(ExplodingPathProfile()),
                    FakeClient(),  # type: ignore[arg-type]
                    "settings",
                )

            self.assertEqual(path_error.exception.verdict.state, CapabilityState.BLOCKED)
            self.assertEqual(
                path_error.exception.verdict.reason,
                "Settings path resolver failed safely; details were logged.",
            )
            self.assertNotIn("path boom", path_error.exception.verdict.reason)

            with self.assertRaises(ProfileExtensionError) as redactor_error:
                await async_read_editable_file(
                    runtime_with_profile(ExplodingRedactorProfile()),
                    FakeClient(),  # type: ignore[arg-type]
                    "settings",
                )

            self.assertEqual(redactor_error.exception.verdict.state, CapabilityState.BLOCKED)
            self.assertEqual(
                redactor_error.exception.verdict.reason,
                "Settings redactor failed safely; details were logged.",
            )
            self.assertNotIn("redactor boom", redactor_error.exception.verdict.reason)

            with self.assertRaises(ProfileExtensionError) as non_text_redactor:
                await async_read_editable_file(
                    runtime_with_profile(NonTextRedactorProfile()),
                    FakeClient(),  # type: ignore[arg-type]
                    "settings",
                )

            self.assertEqual(non_text_redactor.exception.verdict.state, CapabilityState.BLOCKED)
            self.assertIn("Settings redactor returned non-text content", non_text_redactor.exception.verdict.reason)

        asyncio.run(run())

    def test_apply_writes_backup_before_target_and_runs_hooks(self) -> None:
        async def run() -> None:
            client = FakeClient()
            runtime = runtime_with_profile(UploadOrderProfile(client))
            with patch("custom_components.nitrado_gameserver.editable_files.secrets.token_hex", return_value="a" * 16):
                result = await async_apply_editable_file(
                    runtime,
                    client,  # type: ignore[arg-type]
                    "settings",
                    {"Password": "new-secret", "difficulty": "2"},
                    value_is_parsed=True,
                    now=100,
                )

            self.assertTrue(result.wrote)
            self.assertEqual(result.backup_path, "/game/settings.ini.nitrado_gameserver.backup.100-aaaaaaaaaaaaaaaa")
            self.assertEqual(
                client.uploads,
                [
                    ("123456", result.backup_path, "Password=old-secret\ndifficulty=1\n"),
                    ("123456", "/game/settings.ini", "Password=new-secret\ndifficulty=2\n"),
                ],
            )
            self.assertEqual(runtime.profile_extra()["upload_counts"], [0, 2])

        asyncio.run(run())

    def test_apply_without_injected_time_uses_real_unique_backup_stamp(self) -> None:
        async def run() -> None:
            client = FakeClient()
            runtime = runtime_with_profile(UploadOrderProfile(client))

            with (
                patch("custom_components.nitrado_gameserver.editable_files.time.time", return_value=123456789),
                patch("custom_components.nitrado_gameserver.editable_files.secrets.token_hex", return_value="b" * 16),
            ):
                result = await async_apply_editable_file(
                    runtime,
                    client,  # type: ignore[arg-type]
                    "settings",
                    {"Password": "new-secret", "difficulty": "2"},
                    value_is_parsed=True,
                )

            self.assertEqual(
                result.backup_path,
                "/game/settings.ini.nitrado_gameserver.backup.123456789-bbbbbbbbbbbbbbbb",
            )
            self.assertNotIn(".backup.0", result.backup_path or "")

        asyncio.run(run())

    def test_apply_aborts_if_target_changes_while_backup_is_created(self) -> None:
        async def run() -> None:
            client = ChangeDuringBackupClient()
            with self.assertRaises(ProfileExtensionError) as caught:
                await async_apply_editable_file(
                    runtime_fixture(),
                    client,  # type: ignore[arg-type]
                    "settings",
                    {"Password": "new-secret", "difficulty": "2"},
                    value_is_parsed=True,
                    now=100,
                )

            self.assertIn("changed while its backup was created", caught.exception.verdict.reason)
            self.assertEqual(client.downloads["/game/settings.ini"], "Password=external\ndifficulty=3\n")
            self.assertEqual(len(client.uploads), 1)

        asyncio.run(run())

    def test_apply_aborts_before_target_if_registry_changes_during_backup(self) -> None:
        async def run() -> None:
            client = RegistryChangeDuringBackupClient()
            try:
                with self.assertRaises(ProfileExtensionError) as caught:
                    await async_apply_editable_file(
                        runtime_fixture(),
                        client,  # type: ignore[arg-type]
                        "settings",
                        {"Password": "new-secret", "difficulty": "2"},
                        value_is_parsed=True,
                        now=100,
                    )

                self.assertIn("profile changed", caught.exception.verdict.reason)
                self.assertEqual(client.downloads["/game/settings.ini"], "Password=old-secret\ndifficulty=1\n")
                self.assertEqual(len(client.uploads), 1)
            finally:
                if client.unregister is not None:
                    client.unregister()

        asyncio.run(run())

    def test_apply_before_hook_blocks_before_any_upload(self) -> None:
        async def run() -> None:
            runtime = runtime_with_profile(BlockingBeforeFileWriteProfile())
            client = FakeClient()

            with self.assertRaises(ProfileExtensionError) as caught:
                await async_apply_editable_file(
                    runtime,
                    client,  # type: ignore[arg-type]
                    "settings",
                    {"Password": "new-secret", "difficulty": "2"},
                    value_is_parsed=True,
                    now=100,
                )

            self.assertEqual(caught.exception.verdict.reason, "No writes today")
            self.assertEqual(client.uploads, [])

        asyncio.run(run())

    def test_apply_does_not_write_when_unchanged_or_blocked(self) -> None:
        async def run() -> None:
            runtime = runtime_fixture()
            client = FakeClient()
            unchanged = await async_apply_editable_file(
                runtime,
                client,  # type: ignore[arg-type]
                "settings",
                {"Password": "old-secret", "difficulty": "1"},
                value_is_parsed=True,
            )

            self.assertFalse(unchanged.wrote)
            self.assertEqual(client.uploads, [])

            with self.assertRaises(ProfileExtensionError):
                await async_apply_editable_file(
                    runtime,
                    client,  # type: ignore[arg-type]
                    "settings",
                    {"Password": "new-secret", "difficulty": "9"},
                    value_is_parsed=True,
                )

            self.assertEqual(client.uploads, [])

        asyncio.run(run())

    def test_apply_blocks_when_state_prerequisite_fails(self) -> None:
        async def run() -> None:
            with self.assertRaises(ProfileExtensionError) as caught:
                await async_apply_editable_file(
                    runtime_fixture(status="started"),
                    FakeClient(),  # type: ignore[arg-type]
                    "settings",
                    {"Password": "new-secret", "difficulty": "2"},
                    value_is_parsed=True,
                )

            self.assertIn("can only be edited while the server is stopped", caught.exception.verdict.reason)

        asyncio.run(run())

    def test_apply_fails_closed_when_status_is_stale(self) -> None:
        async def run() -> None:
            runtime = runtime_fixture()
            runtime.status_fresh = False
            runtime.using_cached_data = True
            client = FakeClient()

            with self.assertRaises(ProfileExtensionError) as caught:
                await async_apply_editable_file(
                    runtime,
                    client,  # type: ignore[arg-type]
                    "settings",
                    {"Password": "new-secret", "difficulty": "2"},
                    value_is_parsed=True,
                )

            self.assertIn("fresh, non-cached server status", caught.exception.verdict.reason)
            self.assertEqual(client.uploads, [])

        asyncio.run(run())

    def test_apply_rechecks_live_state_immediately_before_upload(self) -> None:
        async def run() -> None:
            runtime = runtime_fixture()
            client = FakeClient()
            refreshes = 0

            async def refresh() -> None:
                nonlocal refreshes
                refreshes += 1
                runtime.status_fresh = True
                runtime.using_cached_data = False
                if refreshes == 2:
                    runtime.server = server_fixture("started")

            with self.assertRaises(ProfileExtensionError) as caught:
                await async_apply_editable_file(
                    runtime,
                    client,  # type: ignore[arg-type]
                    "settings",
                    {"Password": "new-secret", "difficulty": "2"},
                    value_is_parsed=True,
                    refresh_status=refresh,
                )

            self.assertIn("only be edited while the server is stopped", caught.exception.verdict.reason)
            self.assertEqual(client.uploads, [])

        asyncio.run(run())

    def test_apply_aborts_if_file_changes_after_preview(self) -> None:
        async def run() -> None:
            client = FakeClient()
            with self.assertRaises(ProfileExtensionError) as caught:
                await async_apply_editable_file(
                    runtime_with_profile(ConcurrentFileChangeProfile(client)),
                    client,  # type: ignore[arg-type]
                    "settings",
                    {"Password": "new-secret", "difficulty": "2"},
                    value_is_parsed=True,
                )

            self.assertIn("changed after preview", caught.exception.verdict.reason)
            self.assertEqual(client.uploads, [])
            self.assertIn("someone-else", client.downloads["/game/settings.ini"])

        asyncio.run(run())

    def test_apply_verification_failure_restores_original_content(self) -> None:
        async def run() -> None:
            runtime = runtime_fixture()
            client = CorruptTargetClient()

            with self.assertRaises(ProfileExtensionError) as caught:
                await async_apply_editable_file(
                    runtime,
                    client,  # type: ignore[arg-type]
                    "settings",
                    {"Password": "new-secret", "difficulty": "2"},
                    value_is_parsed=True,
                    now=100,
                )

            self.assertIn("write verification failed", caught.exception.verdict.reason)
            self.assertIn("automatic rollback verified", caught.exception.verdict.reason)
            self.assertEqual(client.downloads["/game/settings.ini"], "Password=old-secret\ndifficulty=1\n")

        asyncio.run(run())

    def test_apply_restores_original_if_profile_changes_during_target_upload(self) -> None:
        async def run() -> None:
            runtime = runtime_fixture()
            client = ProfileChangingClient(runtime)

            with self.assertRaises(ProfileExtensionError) as caught:
                await async_apply_editable_file(
                    runtime,
                    client,  # type: ignore[arg-type]
                    "settings",
                    {"Password": "new-secret", "difficulty": "2"},
                    value_is_parsed=True,
                    now=100,
                )

            self.assertIn("profile changed", caught.exception.verdict.reason)
            self.assertIn("automatic rollback verified", caught.exception.verdict.reason)
            self.assertEqual(client.downloads["/game/settings.ini"], "Password=old-secret\ndifficulty=1\n")

        asyncio.run(run())

    def test_coordinator_serializes_same_file_mutations(self) -> None:
        async def run() -> None:
            runtime = runtime_fixture()
            client = SlowClient()
            coordinator = NitradoAccountCoordinator(client, services={"123456": runtime}, now_fn=lambda: 300)  # type: ignore[arg-type]

            async def refresh(_coordinator, service_id: str, **kwargs) -> ServiceRuntime:
                runtime.status_fresh = True
                runtime.using_cached_data = False
                return runtime

            with patch.object(NitradoAccountCoordinator, "async_refresh_service", new=refresh):
                await asyncio.gather(
                    coordinator.async_apply_editable_file(
                        "123456",
                        "settings",
                        {"Password": "first", "difficulty": "2"},
                        value_is_parsed=True,
                    ),
                    coordinator.async_apply_editable_file(
                        "123456",
                        "settings",
                        {"Password": "second", "difficulty": "3"},
                        value_is_parsed=True,
                    ),
                )

            self.assertEqual(client.max_active_uploads, 1)
            self.assertEqual(client.downloads["/game/settings.ini"], "Password=second\ndifficulty=3\n")

        asyncio.run(run())

    def test_coordinator_serializes_distinct_declarations_resolving_to_same_path(self) -> None:
        async def run() -> None:
            runtime = runtime_with_profile(SamePathEditableFileProfile())
            client = SlowClient()
            coordinator = NitradoAccountCoordinator(client, services={"123456": runtime}, now_fn=lambda: 300)  # type: ignore[arg-type]

            async def refresh(_coordinator, service_id: str, **kwargs) -> ServiceRuntime:
                runtime.status_fresh = True
                runtime.using_cached_data = False
                return runtime

            with patch.object(NitradoAccountCoordinator, "async_refresh_service", new=refresh):
                await asyncio.gather(
                    coordinator.async_apply_editable_file(
                        "123456",
                        "basic_settings",
                        {"Password": "first", "difficulty": "2"},
                        value_is_parsed=True,
                    ),
                    coordinator.async_apply_editable_file(
                        "123456",
                        "advanced_settings",
                        {"Password": "second", "difficulty": "3"},
                        value_is_parsed=True,
                    ),
                )

            self.assertEqual(client.max_active_uploads, 1)
            self.assertEqual(client.downloads["/game/settings.ini"], "Password=second\ndifficulty=3\n")

        asyncio.run(run())

    def test_rollback_restores_backup_and_saves_current_first(self) -> None:
        async def run() -> None:
            runtime = runtime_fixture()
            client = FakeClient()
            with patch("custom_components.nitrado_gameserver.editable_files.secrets.token_hex", return_value="c" * 16):
                result = await async_rollback_editable_file(
                    runtime,
                    client,  # type: ignore[arg-type]
                    "settings",
                    "/game/settings.ini.nitrado_gameserver.backup.100",
                    now=200,
                )

            self.assertTrue(result.wrote)
            self.assertEqual(
                result.pre_rollback_backup_path,
                "/game/settings.ini.nitrado_gameserver.pre-rollback.200-cccccccccccccccc",
            )
            self.assertEqual(
                client.uploads,
                [
                    ("123456", result.pre_rollback_backup_path, "Password=old-secret\ndifficulty=1\n"),
                    ("123456", "/game/settings.ini", "Password=backup-secret\ndifficulty=2\n"),
                ],
            )
            self.assertEqual(runtime.profile_extra()["restore_hooks"], ["before", "after"])

        asyncio.run(run())

    def test_rollback_aborts_if_target_changes_while_prebackup_is_created(self) -> None:
        async def run() -> None:
            client = ChangeDuringPreRollbackBackupClient()
            with self.assertRaises(ProfileExtensionError) as caught:
                await async_rollback_editable_file(
                    runtime_fixture(),
                    client,  # type: ignore[arg-type]
                    "settings",
                    "/game/settings.ini.nitrado_gameserver.backup.100",
                    now=200,
                )

            self.assertIn("changed while its pre-rollback backup was created", caught.exception.verdict.reason)
            self.assertEqual(client.downloads["/game/settings.ini"], "Password=external\ndifficulty=3\n")
            self.assertEqual(len(client.uploads), 1)

        asyncio.run(run())

    def test_cancel_after_target_write_rolls_back_before_propagating_cancel(self) -> None:
        async def run() -> None:
            runtime = runtime_fixture()
            client = CancelAfterTargetWriteClient()
            original = client.downloads["/game/settings.ini"]
            task = asyncio.create_task(
                async_apply_editable_file(
                    runtime,
                    client,  # type: ignore[arg-type]
                    "settings",
                    {"Password": "new-secret", "difficulty": "2"},
                    value_is_parsed=True,
                    now=200,
                )
            )
            await client.target_written.wait()
            task.cancel()

            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(client.downloads["/game/settings.ini"], original)

        asyncio.run(run())

    def test_repeated_cancel_cannot_interrupt_verified_recovery(self) -> None:
        async def run() -> None:
            runtime = runtime_fixture()
            client = RepeatedCancelRecoveryClient()
            original = client.downloads["/game/settings.ini"]
            task = asyncio.create_task(
                async_apply_editable_file(
                    runtime,
                    client,  # type: ignore[arg-type]
                    "settings",
                    {"Password": "new-secret", "difficulty": "2"},
                    value_is_parsed=True,
                    now=200,
                )
            )
            await client.target_written.wait()
            task.cancel()
            await client.rollback_started.wait()
            task.cancel()
            client.release_rollback.set()

            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(client.downloads["/game/settings.ini"], original)

        asyncio.run(run())

    def test_cancel_during_after_write_hook_restores_original_content(self) -> None:
        async def run() -> None:
            hook_started = asyncio.Event()

            class SlowAfterWriteProfile(EditableProfile):
                def lifecycle_hooks(self):
                    async def wait_after_write(context):
                        hook_started.set()
                        await asyncio.Event().wait()

                    return (
                        LifecycleHookDeclaration(
                            key="after_write",
                            event=LifecycleEvent.AFTER_FILE_WRITE,
                            hook_fn=wait_after_write,
                        ),
                    )

            runtime = runtime_with_profile(SlowAfterWriteProfile())
            client = FakeClient()
            original = client.downloads["/game/settings.ini"]
            task = asyncio.create_task(
                async_apply_editable_file(
                    runtime,
                    client,  # type: ignore[arg-type]
                    "settings",
                    {"Password": "new-secret", "difficulty": "2"},
                    value_is_parsed=True,
                    now=200,
                )
            )
            await hook_started.wait()
            task.cancel()

            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(client.downloads["/game/settings.ini"], original)

        asyncio.run(run())

    def test_cancel_during_after_restore_hook_restores_pre_rollback_content(self) -> None:
        async def run() -> None:
            hook_started = asyncio.Event()

            class SlowAfterRestoreProfile(EditableProfile):
                def lifecycle_hooks(self):
                    async def wait_after_restore(context):
                        hook_started.set()
                        await asyncio.Event().wait()

                    return (
                        LifecycleHookDeclaration(
                            key="after_restore",
                            event=LifecycleEvent.AFTER_RESTORE,
                            hook_fn=wait_after_restore,
                        ),
                    )

            runtime = runtime_with_profile(SlowAfterRestoreProfile())
            client = FakeClient()
            original = client.downloads["/game/settings.ini"]
            task = asyncio.create_task(
                async_rollback_editable_file(
                    runtime,
                    client,  # type: ignore[arg-type]
                    "settings",
                    "/game/settings.ini.nitrado_gameserver.backup.100",
                    now=200,
                )
            )
            await hook_started.wait()
            task.cancel()

            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(client.downloads["/game/settings.ini"], original)

        asyncio.run(run())

    def test_rollback_preserves_content_changed_during_before_restore_hook(self) -> None:
        async def run() -> None:
            runtime = runtime_fixture()
            client = FakeClient()
            profile = runtime.profile

            async def external_change(context):
                client.downloads["/game/settings.ini"] = "Password=external\ndifficulty=9\n"

            profile.lifecycle_hooks = lambda: (  # type: ignore[method-assign]
                LifecycleHookDeclaration(
                    key="external_change",
                    event=LifecycleEvent.BEFORE_RESTORE,
                    hook_fn=external_change,
                ),
            )
            result = await async_rollback_editable_file(
                runtime,
                client,  # type: ignore[arg-type]
                "settings",
                "/game/settings.ini.nitrado_gameserver.backup.100",
                now=200,
            )

            self.assertEqual(
                client.downloads[result.pre_rollback_backup_path],
                "Password=external\ndifficulty=9\n",
            )

        asyncio.run(run())

    def test_rollback_rejects_backup_path_for_different_target_file(self) -> None:
        async def run() -> None:
            runtime = runtime_fixture()
            client = FakeClient()

            with self.assertRaises(ProfileExtensionError) as caught:
                await async_rollback_editable_file(
                    runtime,
                    client,  # type: ignore[arg-type]
                    "settings",
                    "/game/other.ini.nitrado_gameserver.backup.100",
                    now=200,
                )

            self.assertIn("not a generated backup for this file", caught.exception.verdict.reason)
            self.assertEqual(client.uploads, [])

        asyncio.run(run())

    def test_rollback_verification_failure_restores_original_content(self) -> None:
        async def run() -> None:
            runtime = runtime_fixture()
            client = CorruptRollbackClient()

            with self.assertRaises(ProfileExtensionError) as caught:
                await async_rollback_editable_file(
                    runtime,
                    client,  # type: ignore[arg-type]
                    "settings",
                    "/game/settings.ini.nitrado_gameserver.backup.100",
                    now=200,
                )

            self.assertIn("rollback verification failed", caught.exception.verdict.reason)
            self.assertIn("original content restored and verified", caught.exception.verdict.reason)
            self.assertEqual(client.downloads["/game/settings.ini"], "Password=old-secret\ndifficulty=1\n")

        asyncio.run(run())

    def test_coordinator_exposes_editable_file_methods(self) -> None:
        async def run() -> None:
            runtime = runtime_fixture()
            client = FakeClient()
            coordinator = NitradoAccountCoordinator(client, services={"123456": runtime}, now_fn=lambda: 300)  # type: ignore[arg-type]

            async def refresh(_coordinator, service_id: str, **kwargs) -> ServiceRuntime:
                runtime.status_fresh = True
                runtime.using_cached_data = False
                return runtime

            with patch.object(NitradoAccountCoordinator, "async_refresh_service", new=refresh):
                snapshot = await coordinator.async_read_editable_file("123456", "settings")
                preview = await coordinator.async_preview_editable_file(
                    "123456",
                    "settings",
                    {"Password": "new-secret", "difficulty": "2"},
                    value_is_parsed=True,
                )
                result = await coordinator.async_apply_editable_file(
                    "123456",
                    "settings",
                    {"Password": "new-secret", "difficulty": "2"},
                    value_is_parsed=True,
                )

            self.assertEqual(snapshot.path, "/game/settings.ini")
            self.assertTrue(preview.verdict.allowed)
            self.assertTrue(result.backup_path.startswith("/game/settings.ini.nitrado_gameserver.backup.300-"))

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
