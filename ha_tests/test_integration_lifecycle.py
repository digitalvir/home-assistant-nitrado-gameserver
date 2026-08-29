"""Real Home Assistant lifecycle, persistence, permission, and action tests."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import shutil
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import Context, CoreState
from homeassistant.exceptions import HomeAssistantError, Unauthorized
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nitrado_gameserver.api.nitrado import (
    NitradoAuthError,
    NitradoEndpointAuthorizationError,
    NitradoRequestContext,
    NitradoRequestFailure,
)
from custom_components.nitrado_gameserver.cockpit import (
    _update_cockpit_asset_issue,
    cockpit_state,
    issue_save_bundle_preview_grant,
)
from custom_components.nitrado_gameserver.const import (
    CONF_ACCOUNT_ID,
    CONF_ACCOUNT_UUID,
    CONF_API_TOKEN,
    CONF_DISCOVERY_INTERVAL,
    CONF_IDLE_SHUTDOWN_SERVICE_IDS,
    CONF_IMPORTED_SERVICE_IDS,
    CONF_PROFILE_OPTION_ACKNOWLEDGEMENTS,
    CONF_PROFILE_OPTIONS,
    CONF_SETTLE_SECONDS,
    CONF_STATUS_INTERVAL,
    DOMAIN,
)
from custom_components.nitrado_gameserver.diagnostics import async_get_config_entry_diagnostics
from custom_components.nitrado_gameserver.extensions import ProfileExtensionError
from custom_components.nitrado_gameserver.filesystem import NitradoFilesystemService
from custom_components.nitrado_gameserver.ha_filesystem_storage import HomeAssistantRecoveryBlobStore
from custom_components.nitrado_gameserver.panel import (
    PANEL_ASSET_VERSION,
    PANEL_ELEMENT,
    PANEL_MODULE_URL,
    PANEL_STATIC_URL,
    PANEL_URL_PATH,
    async_register_extension_panel,
    async_unregister_extension_panel,
)
from custom_components.nitrado_gameserver.plugins.api import (
    PROFILE_API_VERSION,
    SUPPORTED,
    ActionDeclaration,
    BaseGameProfile,
    EditableFileDeclaration,
    EntityDeclaration,
    MatchResult,
    NitradoApiError,
    NitradoService,
    ParsedServer,
    ProfileOptionDeclaration,
    ProfileOptionType,
    ProfileStatus,
    ResourceContentFamily,
    ResourceDeclaration,
    _profile_registry_transition_lock,
    async_register_profile,
)
from custom_components.nitrado_gameserver.plugins.registry import profile_registration_generation
from custom_components.nitrado_gameserver.provider_api import (
    PROVIDER_CONNECTOR_API_VERSION,
    ProviderAuthorizationError,
    ProviderFileReadRequest,
    ProviderGrant,
    ProviderLeaseRevokedError,
    ProviderPlanError,
    ProviderScope,
    ProviderServiceRef,
    ProviderTreeFile,
    ProviderTreeManifest,
    ProviderTreeReplaceRequest,
    async_register_provider_consumer,
)
from custom_components.nitrado_gameserver.provider_runtime import (
    HomeAssistantProviderAuthority,
    get_provider_runtime,
)
from custom_components.nitrado_gameserver.repairs import (
    RepairIssue,
    RepairIssuePlan,
    async_create_fix_flow,
    async_update_repair_issues,
)
from custom_components.nitrado_gameserver.service_options import default_service_options, entry_option_update_lock

SERVICE_ID = "900001"


class HarnessProfile(BaseGameProfile):
    """Predictable external profile used to exercise the real HA boundary."""

    api_version = PROFILE_API_VERSION
    profile_id = "harness_game"
    name = "Harness Game"
    supported_games = ("harnessgame",)
    idle_shutdown_supported = True

    def matches(self, service, server=None):
        return MatchResult(service.game == "harnessgame", confidence=1.0)

    async def enrich_status(self, client, service, server, context):
        return ProfileStatus(
            player_count=server.player_count,
            player_max=server.player_max,
            player_names=server.player_names,
            player_source=server.player_source,
            query_valid=server.query_valid,
        )

    async def suggest_display_name(self, client, service, server=None):
        return server.server_name if server and server.server_name else service.name

    async def can_start(self, context):
        return SUPPORTED

    async def can_stop(self, context):
        return SUPPORTED

    def idle_shutdown_capability(self, context):
        return SUPPORTED

    def editable_files(self):
        async def settings_path(context):
            registry_change = context.extra.get("_registry_reentrant_file_test")
            if callable(registry_change):
                await registry_change()
            return "game/settings.ini"

        return (
            EditableFileDeclaration(
                key="settings",
                name="Settings",
                path_fn=settings_path,
                parser=lambda text: text,
                serializer=str,
                redactor=lambda text: text.replace("secret-value", "[redacted]"),
            ),
        )

    def actions(self):
        async def record(context):
            registry_change = context.extra.get("_registry_reentrant_test")
            if callable(registry_change):
                await registry_change()
            blocking = context.extra.get("_blocking_action_test")
            if isinstance(blocking, dict):
                blocking["started"].set()
                await blocking["release"].wait()
                blocking["effects"].append("ran")
            context.extra["harness_action_payload"] = context.payload
            return {"ok": True}

        return (ActionDeclaration("record", "Record", record),)

    def resources(self):
        async def stream(context):
            context.extra["stream_closed"] = False

            async def chunks():
                try:
                    blocking = context.extra.get("_blocking_stream_test")
                    if isinstance(blocking, dict):
                        blocking["started"].set()
                        await blocking["release"].wait()
                        blocking["effects"].append("yielded")
                    yield b"first\n"
                    yield bytearray(b"second\n")
                finally:
                    context.extra["stream_closed"] = True
                    blocking = context.extra.get("_blocking_stream_test")
                    if isinstance(blocking, dict):
                        blocking["closed"].set()

            return chunks()

        async def binary(context):
            blocking = context.extra.get("_blocking_resource_test")
            if isinstance(blocking, dict):
                blocking["started"].set()
                await blocking["release"].wait()
                registry_change = blocking.get("registry_change")
                if callable(registry_change):
                    await registry_change()
            return bytearray(b"binary")

        class BrokenStream:
            def __aiter__(self):
                raise RuntimeError("broken stream iterator")

        async def broken_stream(context):
            return BrokenStream()

        return (
            ResourceDeclaration(
                key="event_stream",
                name="Event stream",
                content_type="text/event-stream",
                content_family=ResourceContentFamily.STREAM,
                fetch_fn=stream,
            ),
            ResourceDeclaration(
                key="binary_blob",
                name="Binary",
                content_type="application/octet-stream",
                content_family=ResourceContentFamily.BINARY,
                fetch_fn=binary,
            ),
            ResourceDeclaration(
                key="broken_stream",
                name="Broken stream",
                content_type="text/event-stream",
                content_family=ResourceContentFamily.STREAM,
                fetch_fn=broken_stream,
            ),
        )

    def extra_entities(self):
        return (
            EntityDeclaration(
                platform="switch",
                key="enhanced_mode",
                name="Enhanced Mode",
                attributes={"option_key": "enhanced_mode"},
            ),
        )

    def profile_options(self):
        return (
            ProfileOptionDeclaration(
                key="allow_insecure_transport",
                name="Allow insecure transport",
                option_type=ProfileOptionType.BOOLEAN,
            ),
            ProfileOptionDeclaration(
                key="strict_validation",
                name="Strict validation",
                option_type=ProfileOptionType.BOOLEAN,
                default=True,
            ),
            ProfileOptionDeclaration(
                key="admin_password",
                name="Admin password",
                option_type=ProfileOptionType.SECRET,
                default="",
            ),
        )


class StructuredHarnessProfile(HarnessProfile):
    """Higher-confidence fixture for the structured cockpit HTTP contract."""

    profile_id = "structured_harness"
    name = "Structured Harness"

    def matches(self, service, server=None):
        return MatchResult(service.game == "harnessgame", confidence=1.0)

    def editable_files(self):
        async def settings_path(context):
            return "game/settings.ini"

        def model(text):
            difficulty = text.split("difficulty=", 1)[1].splitlines()[0]
            return {
                "schema_revision": "structured-harness-1",
                "settings": [
                    {"key": "AdminPassword", "raw_value": None, "sensitive": True, "configured": True},
                    {"key": "difficulty", "raw_value": difficulty, "sensitive": False, "configured": None},
                ],
                "redacted_source": text.replace("secret-value", "[redacted]"),
            }

        def patch_settings(text, operations):
            assert operations == [{"op": "set", "key": "difficulty", "raw_value": "2"}]
            return text.replace("difficulty=1", "difficulty=2")

        return (
            EditableFileDeclaration(
                key="settings",
                name="Settings",
                path_fn=settings_path,
                parser=lambda text: text,
                serializer=str,
                redactor=lambda text: text.replace("secret-value", "[redacted]"),
                editor_modeler=model,
                editor_patcher=patch_settings,
                requires_stopped=True,
            ),
        )


class ConsentHarnessProfile(HarnessProfile):
    """Harness profile whose transport consent exercises the public requirement contract."""

    def profile_options(self):
        return (
            ProfileOptionDeclaration(
                key="allow_insecure_transport",
                name="Allow insecure transport",
                description="Allow authenticated game status over plaintext HTTP.",
                option_type=ProfileOptionType.BOOLEAN,
                standard_options=True,
                onboarding=True,
                confirmation_required=True,
                acknowledgement_revision=1,
                idle_shutdown_required=True,
                repair_if_unacknowledged=True,
            ),
            ProfileOptionDeclaration(
                key="strict_validation",
                name="Strict validation",
                option_type=ProfileOptionType.BOOLEAN,
                default=True,
            ),
            ProfileOptionDeclaration(
                key="admin_password",
                name="Admin password",
                option_type=ProfileOptionType.SECRET,
                default="",
            ),
        )


class NonMatchingProfile(BaseGameProfile):
    """External registry entry used to exercise hot-change quiescing."""

    api_version = PROFILE_API_VERSION
    profile_id = "nonmatching_harness"
    name = "Nonmatching Harness"

    def matches(self, service, server=None):
        return MatchResult(False)


class SecondNonMatchingProfile(BaseGameProfile):
    """Second external registry entry used to test serialized transitions."""

    api_version = PROFILE_API_VERSION
    profile_id = "second_nonmatching_harness"
    name = "Second Nonmatching Harness"

    def matches(self, service, server=None):
        return MatchResult(False)


class InvalidManifestProfile(BaseGameProfile):
    """Externally supplied profile whose declarations cannot be dispatched safely."""

    api_version = PROFILE_API_VERSION
    profile_id = "invalid_manifest_harness"
    name = "Invalid Manifest Harness"

    def matches(self, service, server=None):
        return MatchResult(True, confidence=100.0)

    def actions(self):
        async def noop(context):
            return None

        return (
            ActionDeclaration("duplicate", "Duplicate", noop),
            ActionDeclaration("duplicate", "Duplicate Again", noop),
        )


class FakeNitradoClient:
    """Network-free Nitrado client for the actual HA setup machinery."""

    def __init__(
        self,
        *,
        status: str = "started",
        account_id: str = "harness-account",
        service_id: str = SERVICE_ID,
    ) -> None:
        self.status = status
        self.account_id = account_id
        self.service_id = service_id
        self.stop_calls: list[tuple[str, str]] = []
        self.start_calls: list[tuple[str, str]] = []
        self.files = {
            "/game/settings.ini": 'AdminPassword="secret-value"',
            "/games/ni900_001/ftproot/game/settings.ini": 'AdminPassword="secret-value"',
        }

    async def account_identity(self) -> str:
        return self.account_id

    async def service_list(self):
        return [
            NitradoService(
                service_id=self.service_id,
                name="Harness Server",
                game="harnessgame",
                game_human="Harness Game",
                folder_short="harnessgame",
                type_human="Gameserver",
                raw_redacted={},
            )
        ]

    async def fetch_server(self, service_id: str):
        return ParsedServer(
            service_id=service_id,
            raw_status=self.status,
            server_name="Harness Server",
            address="gameserver.example.invalid:8211",
            game_short="harnessgame",
            game_human="Harness Game",
            player_count=0,
            player_max=10,
            player_names=(),
            query_valid=True,
            player_source="harness",
            raw_redacted={},
        )

    async def start_server(self, service_id: str, game_short: str) -> None:
        self.start_calls.append((service_id, game_short))

    async def stop_server(self, service_id: str, reason: str) -> None:
        self.stop_calls.append((service_id, reason))

    async def download_file(self, service_id: str, path: str) -> str:
        return self.files[path]

    async def list_files(self, service_id: str, directory: str | None = None):
        del service_id, directory
        return {
            "entries": [
                {
                    "type": "dir",
                    "name": "game",
                    "path": "/games/ni900_001/ftproot/game",
                }
            ]
        }

    async def upload_text_file(self, service_id: str, path: str, content: str) -> None:
        self.files[path] = content


class BlockingUploadClient(FakeNitradoClient):
    """Client that lets unload race a tracked editable-file mutation."""

    def __init__(self) -> None:
        super().__init__(status="stopped")
        self.upload_started = asyncio.Event()
        self.release_upload = asyncio.Event()

    async def upload_text_file(self, service_id: str, path: str, content: str) -> None:
        # Let the verified backup complete, then hold the destructive target
        # replacement so unload must drain an operation that crossed the
        # mutation boundary.
        if ".nitrado_gameserver.backup." not in path:
            self.upload_started.set()
            await self.release_upload.wait()
        await super().upload_text_file(service_id, path, content)


class FallbackIdentityClient(FakeNitradoClient):
    """Client whose token can list services but cannot read account identity."""

    async def account_identity(self) -> str:
        raise NitradoApiError("account endpoint unavailable")


def _options(*, auto_shutdown: bool = True, service_id: str = SERVICE_ID) -> dict:
    options = default_service_options()
    options[CONF_IMPORTED_SERVICE_IDS] = [service_id]
    options[CONF_IDLE_SHUTDOWN_SERVICE_IDS] = [service_id] if auto_shutdown else []
    options[CONF_SETTLE_SECONDS] = 0
    return options


def _entry(
    *,
    version: int = 2,
    options: dict | None = None,
    account_id: str = "harness-account",
    service_id: str = SERVICE_ID,
    api_token: str = "test-token",
) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Nitrado Account",
        data={
            CONF_API_TOKEN: api_token,
            CONF_ACCOUNT_ID: account_id,
            CONF_ACCOUNT_UUID: f"{account_id}-uuid",
        },
        options=options if options is not None else _options(service_id=service_id),
        unique_id=f"account:{account_id}",
        version=version,
    )


@pytest.fixture(autouse=True)
async def harness_profile(hass):
    """Register the external profile for the duration of each HA test."""

    unregister = await async_register_profile(hass, HarnessProfile)
    yield unregister
    await unregister()


@pytest.fixture(autouse=True)
async def cleanup_config_entries(hass, harness_profile):
    """Unload integration entries so interval callbacks cannot leak between tests."""

    yield
    for entry in tuple(hass.config_entries.async_entries(DOMAIN)):
        if entry.state.recoverable:
            await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@contextmanager
def _patched_client(fake_client: FakeNitradoClient):
    with patch(
        "custom_components.nitrado_gameserver.NitradoClient",
        return_value=fake_client,
    ):
        yield


async def _setup(hass, entry: MockConfigEntry, fake_client: FakeNitradoClient) -> None:
    entry.add_to_hass(hass)
    with _patched_client(fake_client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_platform_setup_failure_rolls_back_and_retry_rebinds_durable_runtime(
    hass,
    enable_custom_integrations,
) -> None:
    fake_client = FakeNitradoClient()
    entry = _entry()
    entry.add_to_hass(hass)

    with (
        _patched_client(fake_client),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            side_effect=RuntimeError("platform setup failed"),
        ),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.entry_id not in hass.data.get(DOMAIN, {})
    assert get_provider_runtime(hass) is None

    assert await hass.config_entries.async_unload(entry.entry_id)
    with _patched_client(fake_client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][entry.entry_id]
    provider_runtime = get_provider_runtime(hass)
    assert provider_runtime is not None
    assert coordinator.setup_committed
    assert coordinator._operation_journal_durable_bound
    assert provider_runtime._operation_journals[entry.entry_id] is coordinator.operation_journal


@pytest.mark.asyncio
async def test_final_setup_failure_removes_orphan_panel(hass, enable_custom_integrations) -> None:
    fake_client = FakeNitradoClient()
    entry = _entry()
    entry.add_to_hass(hass)

    with (
        _patched_client(fake_client),
        patch(
            "custom_components.nitrado_gameserver.async_register_extension_panel",
            new=AsyncMock(),
        ) as register_panel,
        patch(
            "custom_components.nitrado_gameserver.async_unregister_extension_panel",
        ) as unregister_panel,
        patch.object(entry, "add_update_listener", side_effect=RuntimeError("listener registration failed")),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    register_panel.assert_awaited_once_with(hass)
    unregister_panel.assert_called_once_with(hass)
    assert entry.entry_id not in hass.data.get(DOMAIN, {})


def _entity_id(hass, unique_id: str, platform: str = "switch") -> str:
    registry = er.async_get(hass)
    registry_entry = registry.async_get_entity_id(platform, DOMAIN, unique_id)
    if registry_entry is None and unique_id.startswith("service:"):
        _, service_id, suffix = unique_id.split(":", 2)
        matching_entries = [
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if service_id in entry.options.get(CONF_IMPORTED_SERVICE_IDS, ())
        ]
        assert len(matching_entries) == 1
        composite_id = f"account:{matching_entries[0].entry_id}:service:{service_id}:{suffix}"
        registry_entry = registry.async_get_entity_id(platform, DOMAIN, composite_id)
    assert registry_entry is not None
    return registry_entry


@pytest.mark.asyncio
async def test_auto_shutdown_survives_real_unload_and_setup(hass, enable_custom_integrations) -> None:
    fake_client = FakeNitradoClient()
    entry = _entry(options=_options(auto_shutdown=False))
    await _setup(hass, entry, fake_client)

    entity_id = _entity_id(hass, f"service:{SERVICE_ID}:auto_shutdown")
    assert hass.states.get(entity_id).state != STATE_ON
    await hass.services.async_call(
        "switch",
        "turn_on",
        {"entity_id": entity_id},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == STATE_ON
    assert set(default_service_options()).issubset(entry.options)
    assert entry.options[CONF_IDLE_SHUTDOWN_SERVICE_IDS] == [SERVICE_ID]
    assert SERVICE_ID in hass.data[DOMAIN][entry.entry_id].options.idle_shutdown_service_ids


@pytest.mark.asyncio
async def test_setup_migrates_legacy_registry_identity_without_recreating_objects(
    hass,
    enable_custom_integrations,
) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    entity_registry = er.async_get(hass)
    legacy_entity = entity_registry.async_get_or_create(
        "switch",
        DOMAIN,
        f"service:{SERVICE_ID}:auto_shutdown",
        config_entry=entry,
    )
    device_registry = dr.async_get(hass)
    legacy_device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"service:{SERVICE_ID}")},
    )

    fake_client = FakeNitradoClient()
    with _patched_client(fake_client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    migrated_entity = entity_registry.async_get(legacy_entity.entity_id)
    assert migrated_entity is not None
    assert migrated_entity.unique_id == f"account:{entry.entry_id}:service:{SERVICE_ID}:auto_shutdown"
    assert (
        entity_registry.async_get_entity_id(
            "switch",
            DOMAIN,
            f"service:{SERVICE_ID}:auto_shutdown",
        )
        is None
    )
    migrated_device = device_registry.async_get(legacy_device.id)
    assert migrated_device is not None
    assert migrated_device.identifiers == {(DOMAIN, f"account:{entry.entry_id}:service:{SERVICE_ID}")}


@pytest.mark.asyncio
async def test_stopped_server_exposes_zero_players_without_query_availability_warning(
    hass,
    enable_custom_integrations,
) -> None:
    fake_client = FakeNitradoClient(status="stopped")
    entry = _entry()
    await _setup(hass, entry, fake_client)

    player_count = _entity_id(hass, f"service:{SERVICE_ID}:player_count", platform="sensor")
    online_players = _entity_id(hass, f"service:{SERVICE_ID}:online_players", platform="sensor")
    player_data_valid = _entity_id(
        hass,
        f"service:{SERVICE_ID}:player_data_valid",
        platform="binary_sensor",
    )
    idle_minutes = _entity_id(hass, f"service:{SERVICE_ID}:idle_minutes", platform="sensor")
    idle_remaining = _entity_id(
        hass,
        f"service:{SERVICE_ID}:idle_time_remaining",
        platform="sensor",
    )
    auto_shutdown_status = _entity_id(
        hass,
        f"service:{SERVICE_ID}:auto_shutdown_status",
        platform="sensor",
    )
    runtime = hass.data[DOMAIN][entry.entry_id].get_runtime(SERVICE_ID)

    assert hass.states.get(player_count).state == "0"
    assert hass.states.get(online_players).state == "None"
    assert hass.states.get(player_data_valid).state == STATE_ON
    assert hass.states.get(idle_minutes).state == "0.0"
    assert hass.states.get(idle_remaining).state == "0.0"
    assert hass.states.get(auto_shutdown_status).state == "Inactive — server stopped"
    assert runtime.server.player_source == "server_status"
    assert runtime.server.query_valid is False
    assert "status is stopped" in runtime.last_reset_reason
    assert "player" not in runtime.last_reset_reason.lower()


@pytest.mark.asyncio
async def test_transitioning_server_keeps_player_entities_unavailable_without_player_complaint(
    hass,
    enable_custom_integrations,
) -> None:
    fake_client = FakeNitradoClient(status="stopping")
    entry = _entry()
    await _setup(hass, entry, fake_client)

    player_count = _entity_id(hass, f"service:{SERVICE_ID}:player_count", platform="sensor")
    online_players = _entity_id(hass, f"service:{SERVICE_ID}:online_players", platform="sensor")
    player_data_valid = _entity_id(
        hass,
        f"service:{SERVICE_ID}:player_data_valid",
        platform="binary_sensor",
    )
    idle_minutes = _entity_id(hass, f"service:{SERVICE_ID}:idle_minutes", platform="sensor")
    idle_remaining = _entity_id(
        hass,
        f"service:{SERVICE_ID}:idle_time_remaining",
        platform="sensor",
    )
    auto_shutdown_status = _entity_id(
        hass,
        f"service:{SERVICE_ID}:auto_shutdown_status",
        platform="sensor",
    )
    runtime = hass.data[DOMAIN][entry.entry_id].get_runtime(SERVICE_ID)

    assert hass.states.get(player_count).state == STATE_UNAVAILABLE
    assert hass.states.get(online_players).state == STATE_UNAVAILABLE
    assert hass.states.get(player_data_valid).state != STATE_ON
    assert hass.states.get(idle_minutes).state == "0.0"
    assert hass.states.get(idle_remaining).state == "0.0"
    assert hass.states.get(auto_shutdown_status).state == "Waiting for stable server status"
    assert "status is stopping" in runtime.last_reset_reason
    assert "player" not in runtime.last_reset_reason.lower()


@pytest.mark.asyncio
async def test_running_server_without_trusted_count_is_unavailable_and_complains(
    hass,
    enable_custom_integrations,
) -> None:
    fake_client = FakeNitradoClient(status="started")
    fake_client.fetch_server = AsyncMock(
        return_value=ParsedServer(
            service_id=SERVICE_ID,
            raw_status="started",
            server_name="Harness Server",
            address="gameserver.example.invalid:8211",
            game_short="harnessgame",
            game_human="Harness Game",
            player_count=None,
            player_max=10,
            player_names=(),
            query_valid=False,
            player_source=None,
            raw_redacted={},
        )
    )
    entry = _entry()
    await _setup(hass, entry, fake_client)

    player_count = _entity_id(hass, f"service:{SERVICE_ID}:player_count", platform="sensor")
    player_data_valid = _entity_id(
        hass,
        f"service:{SERVICE_ID}:player_data_valid",
        platform="binary_sensor",
    )
    coordinator = hass.data[DOMAIN][entry.entry_id]
    runtime = coordinator.get_runtime(SERVICE_ID)
    runtime.startup_cooldown_started_at = coordinator.now_fn() - 3600
    await coordinator._async_handle_idle_shutdown(runtime)

    assert hass.states.get(player_count).state == STATE_UNAVAILABLE
    assert hass.states.get(player_data_valid).state != STATE_ON
    assert "player count" in runtime.last_reset_reason.lower()


@pytest.mark.asyncio
async def test_external_public_profile_unregisters_to_generic_in_real_ha(
    hass,
    enable_custom_integrations,
    harness_profile,
) -> None:
    """A companion can unload without leaving core bound to private profile state."""

    fake_client = FakeNitradoClient()
    entry = _entry()
    await _setup(hass, entry, fake_client)
    runtime = hass.data[DOMAIN][entry.entry_id].get_runtime(SERVICE_ID)
    assert runtime.profile.profile_id == HarnessProfile.profile_id

    with _patched_client(fake_client):
        await harness_profile()
        await hass.async_block_till_done()

    assert entry.state.recoverable
    runtime = hass.data[DOMAIN][entry.entry_id].get_runtime(SERVICE_ID)
    assert runtime.profile.profile_id == "generic"
    assert runtime.server.query_valid is True
    assert runtime.server.player_source == "harness"


@pytest.mark.asyncio
async def test_external_public_profile_registers_into_loaded_core_in_real_ha(
    hass,
    enable_custom_integrations,
    harness_profile,
) -> None:
    """A companion can register after core is loaded and take effect immediately."""

    await harness_profile()
    fake_client = FakeNitradoClient()
    entry = _entry()
    await _setup(hass, entry, fake_client)
    assert hass.data[DOMAIN][entry.entry_id].get_runtime(SERVICE_ID).profile.profile_id == "generic"

    with _patched_client(fake_client):
        unregister = await async_register_profile(hass, HarnessProfile)
        await hass.async_block_till_done()
    try:
        runtime = hass.data[DOMAIN][entry.entry_id].get_runtime(SERVICE_ID)
        assert runtime.profile.profile_id == HarnessProfile.profile_id
    finally:
        with _patched_client(fake_client):
            await unregister()
            await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_separately_packaged_companion_setup_and_unload_uses_public_profile_lifecycle(
    hass,
    enable_custom_integrations,
    hass_client,
    harness_profile,
    request,
) -> None:
    """A real companion config entry can register, take over, unload, and fall back."""

    fake_client = FakeNitradoClient()
    nitrado_entry = _entry()
    await _setup(hass, nitrado_entry, fake_client)
    runtime = hass.data[DOMAIN][nitrado_entry.entry_id].get_runtime(SERVICE_ID)
    assert runtime.profile.profile_id == HarnessProfile.profile_id
    with _patched_client(fake_client):
        await harness_profile()
        await hass.async_block_till_done()
    assert hass.data[DOMAIN][nitrado_entry.entry_id].get_runtime(SERVICE_ID).profile.profile_id == "generic"

    import custom_components

    fixture = Path(__file__).parent / "fixtures" / "nitrado_companion"
    target = Path(next(iter(custom_components.__path__))) / "nitrado_companion"
    shutil.copytree(fixture, target, dirs_exist_ok=True)
    request.addfinalizer(lambda: shutil.rmtree(target, ignore_errors=True))
    from homeassistant import loader

    importlib.invalidate_caches()
    hass.data.pop(loader.DATA_CUSTOM_COMPONENTS, None)
    hass.data[loader.DATA_INTEGRATIONS].pop("nitrado_companion", None)
    assert "nitrado_companion" in await loader.async_get_custom_components(hass)
    companion = MockConfigEntry(
        domain="nitrado_companion",
        title="Nitrado Companion Test",
        data={
            "account_entry_id": nitrado_entry.entry_id,
            "service_id": SERVICE_ID,
            "root": "game",
        },
    )
    companion.add_to_hass(hass)
    provider_runtime = get_provider_runtime(hass)
    assert provider_runtime is not None
    await provider_runtime.authority.async_put_grant(
        ProviderGrant(
            "fixture-read-grant",
            "nitrado_companion",
            companion.entry_id,
            ProviderServiceRef(nitrado_entry.entry_id, SERVICE_ID),
            frozenset({ProviderScope.FILESYSTEM_READ}),
            permitted_roots=frozenset({"game"}),
        )
    )

    with (
        _patched_client(fake_client),
        patch.object(
            NitradoFilesystemService,
            "read_bytes",
            new=AsyncMock(return_value=b'AdminPassword="secret-value"'),
        ),
        patch(
            "custom_components.nitrado_gameserver.filesystem.NitradoFtpTransport.capabilities",
            new=AsyncMock(return_value=None),
        ),
    ):
        assert await hass.config_entries.async_setup(companion.entry_id)
        await hass.async_block_till_done()
    runtime = hass.data[DOMAIN][nitrado_entry.entry_id].get_runtime(SERVICE_ID)
    assert runtime.profile.profile_id == "companion_harness"
    assert profile_registration_generation("companion_harness") > 0
    admin = await hass_client()
    bootstrap_url = (
        f"/api/{DOMAIN}/accounts/{nitrado_entry.entry_id}/services/{SERVICE_ID}"
        "/cockpit/bootstrap?mount_epoch=companion-fixture"
    )
    bootstrap_response = await admin.get(bootstrap_url)
    assert bootstrap_response.status == 200
    bootstrap = await bootstrap_response.json()
    assert bootstrap["cockpit"]["key"] == "companion_harness"
    assert bootstrap["cockpit"]["asset_url"].startswith(
        f"/api/{DOMAIN}/cockpit-assets/nitrado_companion/companion_cockpit/"
    )
    asset_url = bootstrap["cockpit"]["asset_url"]
    asset_response = await admin.get(asset_url)
    assert asset_response.status == 200
    assert await asset_response.read() == (target / "frontend" / "companion-cockpit.js").read_bytes()
    assert asset_response.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    companion_state = hass.data["nitrado_companion"][companion.entry_id]
    connector = companion_state["connector"]
    assert companion_state["content"].content == b'AdminPassword="secret-value"'
    assert connector.active

    with _patched_client(fake_client):
        assert await hass.config_entries.async_unload(companion.entry_id)
        await hass.async_block_till_done()
    runtime = hass.data[DOMAIN][nitrado_entry.entry_id].get_runtime(SERVICE_ID)
    assert runtime.profile.profile_id == "generic"
    assert profile_registration_generation("companion_harness") == 0
    fallback_response = await admin.get(bootstrap_url)
    assert fallback_response.status == 200
    assert (await fallback_response.json())["cockpit"] is None
    assert (await admin.get(asset_url)).status == 404
    assert not connector.active
    with pytest.raises(ProviderLeaseRevokedError):
        await connector.async_read_file(ProviderFileReadRequest("game/settings.ini", max_bytes=4096))

    companion_module = importlib.import_module("custom_components.nitrado_companion")
    calls = []

    async def old_register(*args, **kwargs):
        calls.append((args, kwargs))
        return lambda: None

    old_api = SimpleNamespace(async_register_profile=old_register)
    with patch.object(companion_module, "profile_api", old_api):
        assert companion_module.CompanionHarnessProfile().cockpit() is None
        await companion_module.async_register_companion_profile(hass, old_api)
    assert calls == [((hass, companion_module.CompanionHarnessProfile), {})]


@pytest.mark.asyncio
async def test_provider_grants_are_exact_persistent_and_cancellation_drained(
    hass,
    enable_custom_integrations,
) -> None:
    entry = _entry()
    await _setup(hass, entry, FakeNitradoClient())
    provider_runtime = get_provider_runtime(hass)
    assert provider_runtime is not None
    authority = provider_runtime.authority
    service_ref = ProviderServiceRef(entry.entry_id, SERVICE_ID)

    with pytest.raises(ProviderAuthorizationError):
        await authority.async_put_grant(
            ProviderGrant(
                "missing-root",
                "save_monitor",
                "consumer-entry",
                service_ref,
                frozenset({ProviderScope.FILESYSTEM_READ}),
            )
        )
    with pytest.raises(ProviderAuthorizationError):
        await authority.async_put_grant(
            ProviderGrant(
                "backup-with-root",
                "save_monitor",
                "consumer-entry",
                service_ref,
                frozenset({ProviderScope.NATIVE_BACKUP_READ}),
                permitted_roots=frozenset({"world"}),
            )
        )

    save_started = asyncio.Event()
    release_save = asyncio.Event()
    original_save = authority._store.async_save

    async def delayed_save(payload):
        save_started.set()
        await release_save.wait()
        await original_save(payload)

    authority._store.async_save = delayed_save
    grant = ProviderGrant(
        "persistent-exact-grant",
        "save_monitor",
        "consumer-entry",
        service_ref,
        frozenset({ProviderScope.FILESYSTEM_READ}),
        permitted_roots=frozenset({"world"}),
    )
    update = asyncio.create_task(authority.async_put_grant(grant))
    await save_started.wait()
    update.cancel()
    release_save.set()
    await update
    assert tuple(item.grant_id for item in authority.grants()) == (grant.grant_id,)

    reloaded = HomeAssistantProviderAuthority(hass)
    await reloaded.async_initialize()
    assert tuple(item.grant_id for item in reloaded.grants()) == (grant.grant_id,)


@pytest.mark.asyncio
async def test_corrupt_provider_grant_store_fails_closed_and_surfaces_repair(
    hass,
    enable_custom_integrations,
) -> None:
    from homeassistant.helpers.storage import Store

    await Store(hass, 1, f"{DOMAIN}.provider_grants").async_save({"grants": "not-a-list"})
    entry = _entry()
    await _setup(hass, entry, FakeNitradoClient())
    provider_runtime = get_provider_runtime(hass)
    assert provider_runtime is not None
    assert provider_runtime.authority.storage_error is not None
    assert provider_runtime.authority.grants() == ()
    issue_id = f"{entry.entry_id}_provider_grant_store_corrupt"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["provider_connector"] == {
        "grant_store_valid": False,
        "grant_store_error": provider_runtime.authority.storage_error,
        "grant_count": 0,
        "native_backup_restore": {
            "schema_version": 1,
            "integrity_blocked": False,
            "record_count": 0,
            "unresolved_count": 0,
            "records": [],
        },
    }


@pytest.mark.asyncio
async def test_provider_grant_removal_preserves_other_accounts_and_removes_empty_or_corrupt_store(
    hass,
    enable_custom_integrations,
) -> None:
    from homeassistant.helpers.storage import Store

    authority = HomeAssistantProviderAuthority(hass)
    await authority.async_initialize()
    for grant_id, account_id in (("grant-a", "account-a"), ("grant-b", "account-b")):
        await authority.async_put_grant(
            ProviderGrant(
                grant_id,
                "consumer",
                f"consumer-{grant_id}",
                ProviderServiceRef(account_id, SERVICE_ID),
                frozenset({ProviderScope.FILESYSTEM_READ}),
                permitted_roots=frozenset({"game"}),
            )
        )

    await authority.async_delete_account_grants("account-a")
    reloaded = HomeAssistantProviderAuthority(hass)
    await reloaded.async_initialize()
    assert tuple(grant.grant_id for grant in reloaded.grants()) == ("grant-b",)

    await reloaded.async_delete_account_grants("account-b")
    assert await Store(hass, 1, f"{DOMAIN}.provider_grants").async_load() is None

    await Store(hass, 1, f"{DOMAIN}.provider_grants").async_save({"grants": "corrupt"})
    corrupt = HomeAssistantProviderAuthority(hass)
    await corrupt.async_initialize()
    assert corrupt.storage_error is not None
    await corrupt.async_delete_account_grants("any-account")
    assert await Store(hass, 1, f"{DOMAIN}.provider_grants").async_load() is None


@pytest.mark.asyncio
async def test_repair_issue_classes_use_expected_real_registry_severity(
    hass,
    enable_custom_integrations,
) -> None:
    entry = _entry()
    await _setup(hass, entry, FakeNitradoClient())
    coordinator = hass.data[DOMAIN][entry.entry_id]
    error_keys = (
        "missing_service",
        "ftp_plaintext_consent_required",
        "ftp_credentials_unavailable",
        "ftp_transport_unavailable",
        "filesystem_recovery_required",
        "filesystem_recovery_blob_missing",
        "filesystem_recovery_blob_corrupt",
        "filesystem_journal_corrupt",
        "provider_grant_store_corrupt",
        "native_backup_restore_review_required",
        "native_backup_journal_corrupt",
    )
    desired = {
        f"{entry.entry_id}_{translation_key}": RepairIssue(
            issue_id=f"{entry.entry_id}_{translation_key}",
            translation_key=translation_key,
            translation_placeholders={},
        )
        for translation_key in error_keys
    }
    warning_id = f"{entry.entry_id}_profile_option_acknowledgement_required"
    desired[warning_id] = RepairIssue(
        issue_id=warning_id,
        translation_key="profile_option_acknowledgement_required",
        translation_placeholders={},
        severity="warning",
    )

    with patch(
        "custom_components.nitrado_gameserver.repairs.build_repair_issue_plan",
        return_value=RepairIssuePlan(desired=desired, stale_issue_ids=()),
    ):
        await async_update_repair_issues(hass, entry, coordinator)

    registry = ir.async_get(hass)
    for translation_key in error_keys:
        issue = registry.issues[(DOMAIN, f"{entry.entry_id}_{translation_key}")]
        assert issue.translation_key == translation_key
        assert issue.severity is ir.IssueSeverity.ERROR
    assert registry.issues[(DOMAIN, warning_id)].severity is ir.IssueSeverity.WARNING

    _update_cockpit_asset_issue(hass, "broken-profile", "DigestMismatch")
    cockpit_issue = registry.issues[(DOMAIN, "cockpit_asset_" + hashlib.sha256(b"broken-profile").hexdigest()[:16])]
    assert cockpit_issue.translation_key == "cockpit_asset_unavailable"
    assert cockpit_issue.severity is ir.IssueSeverity.ERROR


@pytest.mark.asyncio
async def test_permanent_service_removal_deletes_exact_provider_grants(
    hass,
    enable_custom_integrations,
) -> None:
    entry = _entry()
    fake_client = FakeNitradoClient()
    await _setup(hass, entry, fake_client)
    provider_runtime = get_provider_runtime(hass)
    assert provider_runtime is not None
    await provider_runtime.authority.async_put_grant(
        ProviderGrant(
            "remove-with-service",
            "save_monitor",
            "consumer-entry",
            ProviderServiceRef(entry.entry_id, SERVICE_ID),
            frozenset({ProviderScope.FILESYSTEM_READ}),
            permitted_roots=frozenset({"world"}),
        )
    )

    with _patched_client(fake_client):
        await hass.services.async_call(
            DOMAIN,
            "remove_service",
            {"service_id": SERVICE_ID},
            blocking=True,
        )
        await hass.async_block_till_done()

    current = get_provider_runtime(hass)
    assert current is not None
    assert current.authority.grants() == ()


@pytest.mark.asyncio
async def test_external_profile_factory_is_vetted_once_without_blocking_the_event_loop(hass) -> None:
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def factory():
        nonlocal calls
        calls += 1
        started.set()
        release.wait(timeout=2)
        return NonMatchingProfile()

    registration = asyncio.create_task(async_register_profile(hass, factory))
    assert await asyncio.to_thread(started.wait, 1)
    await asyncio.sleep(0)
    assert not registration.done()
    release.set()
    unregister = await asyncio.wait_for(registration, timeout=1)
    try:
        assert calls == 1
    finally:
        await unregister()


@pytest.mark.asyncio
async def test_external_profile_factory_timeout_does_not_commit_late(hass) -> None:
    finished = threading.Event()

    def factory():
        time.sleep(0.05)
        finished.set()
        return NonMatchingProfile()

    with (
        patch(
            "custom_components.nitrado_gameserver.plugins.api.PROFILE_REGISTRATION_TIMEOUT_SECONDS",
            0.005,
        ),
        pytest.raises(TimeoutError, match="execution budget"),
    ):
        await async_register_profile(hass, factory)

    assert await asyncio.to_thread(finished.wait, 1)
    assert profile_registration_generation(NonMatchingProfile.profile_id) == 0


@pytest.mark.asyncio
async def test_hung_registration_factory_cannot_exhaust_home_assistant_executor(hass) -> None:
    started = threading.Event()
    release = threading.Event()

    def factory():
        started.set()
        release.wait(timeout=2)
        return NonMatchingProfile()

    try:
        with (
            patch(
                "custom_components.nitrado_gameserver.plugins.api.PROFILE_REGISTRATION_TIMEOUT_SECONDS",
                0.005,
            ),
            pytest.raises(TimeoutError, match="execution budget"),
        ):
            await async_register_profile(hass, factory)

        assert started.is_set()
        with pytest.raises(RuntimeError, match="still running"):
            await async_register_profile(hass, NonMatchingProfile)
        assert await asyncio.wait_for(asyncio.to_thread(lambda: "executor-ok"), timeout=0.2) == "executor-ok"
    finally:
        release.set()


@pytest.mark.asyncio
async def test_ordinary_entry_setup_waits_for_registry_transition(
    hass,
    enable_custom_integrations,
) -> None:
    fake_client = FakeNitradoClient()
    entry = _entry()
    entry.add_to_hass(hass)
    lock = _profile_registry_transition_lock(hass)
    await lock.acquire()
    try:
        with _patched_client(fake_client):
            setup_task = asyncio.create_task(hass.config_entries.async_setup(entry.entry_id))
            await asyncio.sleep(0)
            assert not setup_task.done()
            lock.release()
            assert await asyncio.wait_for(setup_task, timeout=1)
            await hass.async_block_till_done()
    finally:
        if lock.locked():
            lock.release()


@pytest.mark.asyncio
async def test_invalid_external_manifest_is_rejected_before_it_can_shadow_valid_profiles(hass) -> None:
    with pytest.raises(ValueError, match="duplicate"):
        await async_register_profile(hass, InvalidManifestProfile)

    assert profile_registration_generation(InvalidManifestProfile.profile_id) == 0


@pytest.mark.asyncio
async def test_public_registry_change_quiesces_active_profile_mutation_before_generation_change(
    hass,
    enable_custom_integrations,
) -> None:
    """Hot registration cancels old mutation code before changing the registry."""

    fake_client = FakeNitradoClient()
    entry = _entry()
    await _setup(hass, entry, fake_client)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    runtime = coordinator.get_runtime(SERVICE_ID)
    started = asyncio.Event()
    release = asyncio.Event()
    effects: list[str] = []
    runtime.profile_extra()["_blocking_action_test"] = {
        "started": started,
        "release": release,
        "effects": effects,
    }

    action_task = asyncio.create_task(coordinator.async_run_profile_action(SERVICE_ID, "record"))
    await started.wait()

    with _patched_client(fake_client):
        register_task = asyncio.create_task(async_register_profile(hass, NonMatchingProfile))
        with pytest.raises(asyncio.CancelledError):
            await action_task
        unregister = await register_task
        await hass.async_block_till_done()
    try:
        assert effects == []
        assert hass.data[DOMAIN][entry.entry_id].get_runtime(SERVICE_ID).profile.profile_id == HarnessProfile.profile_id
    finally:
        with _patched_client(fake_client):
            await unregister()
            await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_failed_public_profile_registration_resumes_loaded_coordinator(
    hass,
    enable_custom_integrations,
) -> None:
    """A rejected duplicate registration must not strand core in quiescent state."""

    fake_client = FakeNitradoClient()
    entry = _entry()
    await _setup(hass, entry, fake_client)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    with pytest.raises(ValueError, match="already registered"):
        await async_register_profile(hass, HarnessProfile)

    assert not coordinator.shutting_down
    assert await coordinator.async_run_profile_action(SERVICE_ID, "record") == {"ok": True}


@pytest.mark.asyncio
async def test_cockpit_asset_collision_is_rejected_before_profile_generation_commit(
    hass,
    enable_custom_integrations,
) -> None:
    """Asset preflight cannot leave loaded consumers on a stale generation."""

    fake_client = FakeNitradoClient()
    entry = _entry()
    await _setup(hass, entry, fake_client)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    with (
        patch(
            "custom_components.nitrado_gameserver.plugins.api.ensure_cockpit_assets_registerable",
            side_effect=RuntimeError("asset collision"),
        ),
        pytest.raises(RuntimeError, match="asset collision"),
    ):
        await async_register_profile(hass, NonMatchingProfile)

    assert profile_registration_generation(NonMatchingProfile.profile_id) == 0
    assert not coordinator.shutting_down
    assert await coordinator.async_run_profile_action(SERVICE_ID, "record") == {"ok": True}


@pytest.mark.asyncio
async def test_post_commit_asset_failure_reloads_consumers_after_generation_rollback(
    hass,
    enable_custom_integrations,
) -> None:
    """An unexpected commit failure cannot resume generation-stale consumers."""

    fake_client = FakeNitradoClient()
    entry = _entry()
    await _setup(hass, entry, fake_client)
    original_coordinator = hass.data[DOMAIN][entry.entry_id]

    with (
        _patched_client(fake_client),
        patch(
            "custom_components.nitrado_gameserver.plugins.api.register_cockpit_assets",
            side_effect=RuntimeError("unexpected asset commit failure"),
        ),
        pytest.raises(RuntimeError, match="unexpected asset commit failure"),
    ):
        await async_register_profile(hass, NonMatchingProfile)
    await hass.async_block_till_done()

    current = hass.data[DOMAIN][entry.entry_id]
    assert current is not original_coordinator
    assert profile_registration_generation(NonMatchingProfile.profile_id) == 0
    assert not current.shutting_down
    assert await current.async_run_profile_action(SERVICE_ID, "record") == {"ok": True}


@pytest.mark.asyncio
async def test_public_registry_change_cancels_and_closes_active_profile_stream(
    hass,
    enable_custom_integrations,
) -> None:
    """Old companion stream code is closed before registry mutation commits."""

    fake_client = FakeNitradoClient()
    entry = _entry()
    await _setup(hass, entry, fake_client)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    runtime = coordinator.get_runtime(SERVICE_ID)
    started = asyncio.Event()
    release = asyncio.Event()
    closed = asyncio.Event()
    effects: list[str] = []
    runtime.profile_extra()["_blocking_stream_test"] = {
        "started": started,
        "release": release,
        "closed": closed,
        "effects": effects,
    }

    result = await coordinator.async_fetch_profile_resource(SERVICE_ID, "event_stream", use_cache=False)
    consume_task = asyncio.create_task(anext(result.data))
    await started.wait()

    with _patched_client(fake_client):
        register_task = asyncio.create_task(async_register_profile(hass, NonMatchingProfile))
        with pytest.raises(asyncio.CancelledError):
            await consume_task
        unregister = await register_task
        await hass.async_block_till_done()
    try:
        assert closed.is_set()
        assert effects == []
    finally:
        with _patched_client(fake_client):
            await unregister()
            await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_profile_callback_cannot_reenter_public_registry_transition(
    hass,
    enable_custom_integrations,
) -> None:
    """A callback cannot deadlock quiescing against its own profile lease."""

    fake_client = FakeNitradoClient()
    entry = _entry()
    await _setup(hass, entry, fake_client)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    runtime = coordinator.get_runtime(SERVICE_ID)

    async def reenter() -> None:
        await async_register_profile(hass, NonMatchingProfile)

    runtime.profile_extra()["_registry_reentrant_test"] = reenter
    with pytest.raises(ProfileExtensionError) as err:
        await asyncio.wait_for(coordinator.async_run_profile_action(SERVICE_ID, "record"), timeout=1.0)

    assert err.value.__cause__ is None
    assert "failed safely" in str(err.value)
    assert not coordinator.shutting_down


@pytest.mark.asyncio
async def test_editable_file_callback_cannot_reenter_public_registry_transition(
    hass,
    enable_custom_integrations,
) -> None:
    fake_client = FakeNitradoClient(status="stopped")
    entry = _entry()
    await _setup(hass, entry, fake_client)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    runtime = coordinator.get_runtime(SERVICE_ID)

    async def reenter() -> None:
        await async_register_profile(hass, NonMatchingProfile)

    runtime.profile_extra()["_registry_reentrant_file_test"] = reenter
    with pytest.raises(ProfileExtensionError) as err:
        await asyncio.wait_for(
            coordinator.async_apply_editable_file(
                SERVICE_ID,
                "settings",
                'AdminPassword="changed"',
            ),
            timeout=1,
        )

    assert err.value.__cause__ is None
    assert "failed safely" in str(err.value)
    assert profile_registration_generation(NonMatchingProfile.profile_id) == 0


@pytest.mark.asyncio
async def test_contended_profile_callback_reentry_rejects_before_transition_lock(
    hass,
    enable_custom_integrations,
) -> None:
    """Callback reentry cannot deadlock behind a transition waiting for its lease."""

    fake_client = FakeNitradoClient()
    entry = _entry()
    await _setup(hass, entry, fake_client)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    runtime = coordinator.get_runtime(SERVICE_ID)
    started = asyncio.Event()
    release = asyncio.Event()

    async def reenter() -> None:
        await async_register_profile(hass, SecondNonMatchingProfile)

    runtime.profile_extra()["_blocking_resource_test"] = {
        "started": started,
        "release": release,
        "registry_change": reenter,
    }
    resource_task = asyncio.create_task(
        coordinator.async_fetch_profile_resource(SERVICE_ID, "binary_blob", use_cache=False)
    )
    await started.wait()
    with _patched_client(fake_client):
        outer_transition = asyncio.create_task(async_register_profile(hass, NonMatchingProfile))
        for _ in range(100):
            if coordinator.shutting_down:
                break
            await asyncio.sleep(0)
        assert coordinator.shutting_down
        release.set()
        with pytest.raises(ProfileExtensionError):
            await asyncio.wait_for(resource_task, timeout=1.0)
        unregister = await asyncio.wait_for(outer_transition, timeout=1.0)
        await hass.async_block_till_done()
    with _patched_client(fake_client):
        await unregister()
        await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_slow_profile_stream_does_not_block_status_refresh(
    hass,
    enable_custom_integrations,
) -> None:
    """Waiting for the next stream event does not monopolize profile status."""

    fake_client = FakeNitradoClient()
    entry = _entry()
    await _setup(hass, entry, fake_client)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    runtime = coordinator.get_runtime(SERVICE_ID)
    started = asyncio.Event()
    release = asyncio.Event()
    closed = asyncio.Event()
    runtime.profile_extra()["_blocking_stream_test"] = {
        "started": started,
        "release": release,
        "closed": closed,
        "effects": [],
    }

    result = await coordinator.async_fetch_profile_resource(SERVICE_ID, "event_stream", use_cache=False)
    consume_task = asyncio.create_task(anext(result.data))
    await started.wait()
    refreshed = await asyncio.wait_for(
        coordinator.async_refresh_service(SERVICE_ID, handle_idle_shutdown=False),
        timeout=1.0,
    )
    assert refreshed.server is not None
    assert not consume_task.done()
    release.set()
    assert await consume_task == b"first\n"
    await result.data.aclose()
    assert closed.is_set()


@pytest.mark.asyncio
async def test_broken_stream_iterator_releases_all_profile_task_ownership(
    hass,
    enable_custom_integrations,
) -> None:
    """Malformed ``__aiter__`` cannot leak teardown-owned task registrations."""

    fake_client = FakeNitradoClient()
    entry = _entry()
    await _setup(hass, entry, fake_client)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    runtime = coordinator.get_runtime(SERVICE_ID)
    result = await coordinator.async_fetch_profile_resource(SERVICE_ID, "broken_stream", use_cache=False)

    with pytest.raises(RuntimeError, match="broken stream iterator"):
        await anext(result.data)

    assert coordinator._active_profile_mutation_tasks == {}
    assert runtime.active_profile_stream_tasks == set()


@pytest.mark.asyncio
async def test_cancelled_registry_quiesce_resumes_coordinator_without_mutation(
    hass,
    enable_custom_integrations,
) -> None:
    """Cancellation while waiting for a profile lease cannot strand core."""

    fake_client = FakeNitradoClient()
    entry = _entry()
    await _setup(hass, entry, fake_client)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    runtime = coordinator.get_runtime(SERVICE_ID)
    started = asyncio.Event()
    release = asyncio.Event()
    runtime.profile_extra()["_blocking_resource_test"] = {"started": started, "release": release}
    resource_task = asyncio.create_task(
        coordinator.async_fetch_profile_resource(SERVICE_ID, "binary_blob", use_cache=False)
    )
    await started.wait()

    register_task = asyncio.create_task(async_register_profile(hass, NonMatchingProfile))
    for _ in range(100):
        if coordinator.shutting_down:
            break
        await asyncio.sleep(0)
    assert coordinator.shutting_down
    register_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await register_task

    assert not coordinator.shutting_down
    release.set()
    assert bytes((await resource_task).data) == b"binary"
    with _patched_client(fake_client):
        unregister = await async_register_profile(hass, NonMatchingProfile)
        await unregister()


@pytest.mark.asyncio
async def test_cancelled_registry_reload_rolls_back_and_resumes_coordinator(
    hass,
    enable_custom_integrations,
) -> None:
    """Cancellation after registry mutation restores the pre-call state."""

    fake_client = FakeNitradoClient()
    entry = _entry()
    await _setup(hass, entry, fake_client)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    reload_started = asyncio.Event()
    calls = 0

    async def interrupted_reload(entry_id: str) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            reload_started.set()
            await asyncio.Event().wait()
        return True

    with patch.object(hass.config_entries, "async_reload", side_effect=interrupted_reload):
        register_task = asyncio.create_task(async_register_profile(hass, NonMatchingProfile))
        await reload_started.wait()
        register_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await register_task

    assert calls == 2
    assert not coordinator.shutting_down
    assert profile_registration_generation(NonMatchingProfile.profile_id) == 0


@pytest.mark.asyncio
async def test_partial_multi_account_registry_reload_restores_every_captured_entry(
    hass,
    enable_custom_integrations,
) -> None:
    """A later account reload failure cannot strand it unloaded after rollback."""

    first_client = FakeNitradoClient(account_id="first-account", service_id="900001")
    second_client = FakeNitradoClient(account_id="second-account", service_id="900002")
    first = _entry(account_id="first-account", service_id="900001", api_token="first-token")
    second = _entry(account_id="second-account", service_id="900002", api_token="second-token")
    await _setup(hass, first, first_client)
    await _setup(hass, second, second_client)

    clients = {"first-token": first_client, "second-token": second_client}
    original_reload = hass.config_entries.async_reload
    second_attempts = 0

    async def reload_with_one_partial_failure(entry_id: str) -> bool:
        nonlocal second_attempts
        if entry_id == second.entry_id:
            second_attempts += 1
            if second_attempts == 1:
                assert await hass.config_entries.async_unload(entry_id)
                return False
            if second.state.name != "LOADED":
                return await hass.config_entries.async_setup(entry_id)
        return await original_reload(entry_id)

    with (
        patch(
            "custom_components.nitrado_gameserver.NitradoClient",
            side_effect=lambda _session, token: clients[token],
        ),
        patch.object(hass.config_entries, "async_reload", side_effect=reload_with_one_partial_failure),
        pytest.raises(RuntimeError, match="Failed to reload"),
    ):
        await async_register_profile(hass, NonMatchingProfile)

    assert first.state.name == "LOADED"
    assert second.state.name == "LOADED"
    assert first.entry_id in hass.data[DOMAIN]
    assert second.entry_id in hass.data[DOMAIN]
    assert profile_registration_generation(NonMatchingProfile.profile_id) == 0


@pytest.mark.asyncio
async def test_failed_unregister_restores_exact_vetted_factory(
    hass,
    enable_custom_integrations,
) -> None:
    """Rollback never re-invokes arbitrary factory code that has become broken."""

    fake_client = FakeNitradoClient()
    entry = _entry()
    await _setup(hass, entry, fake_client)
    state = {"calls": 0, "fail": False}

    def stateful_factory():
        state["calls"] += 1
        if state["fail"]:
            raise RuntimeError("factory unavailable")
        return SecondNonMatchingProfile()

    with _patched_client(fake_client):
        unregister = await async_register_profile(hass, stateful_factory)
        await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN][entry.entry_id]
    calls_before_failure = state["calls"]
    state["fail"] = True

    try:
        with (
            patch.object(hass.config_entries, "async_reload", new=AsyncMock(side_effect=[False, True])),
            pytest.raises(RuntimeError, match="Failed to reload"),
        ):
            await unregister()

        assert state["calls"] == calls_before_failure
        assert not coordinator.shutting_down
        assert profile_registration_generation(SecondNonMatchingProfile.profile_id) > 0
    finally:
        with _patched_client(fake_client):
            await unregister()
            await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_concurrent_public_registry_transitions_are_serialized(
    hass,
    enable_custom_integrations,
) -> None:
    """Two companion setups cannot interleave quiesce/mutate/reload phases."""

    fake_client = FakeNitradoClient()
    entry = _entry()
    await _setup(hass, entry, fake_client)
    with _patched_client(fake_client):
        first_task = asyncio.create_task(async_register_profile(hass, NonMatchingProfile))
        second_task = asyncio.create_task(async_register_profile(hass, SecondNonMatchingProfile))
        first, second = await asyncio.gather(first_task, second_task)
        await hass.async_block_till_done()
    try:
        assert profile_registration_generation(NonMatchingProfile.profile_id) > 0
        assert profile_registration_generation(SecondNonMatchingProfile.profile_id) > 0
    finally:
        with _patched_client(fake_client):
            await asyncio.gather(first(), second())
            await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_profile_local_option_survives_real_unload_and_setup(hass, enable_custom_integrations) -> None:
    fake_client = FakeNitradoClient()
    entry = _entry()
    await _setup(hass, entry, fake_client)

    entity_id = _entity_id(hass, f"service:{SERVICE_ID}:profile:harness_game:harness_game_enhanced_mode")
    assert hass.states.get(entity_id).state != STATE_ON
    await hass.services.async_call("switch", "turn_on", {"entity_id": entity_id}, blocking=True)
    await hass.async_block_till_done()

    assert entry.options[CONF_PROFILE_OPTIONS] == {"harness_game": {"enhanced_mode": {SERVICE_ID: True}}}
    runtime = hass.data[DOMAIN][entry.entry_id].get_runtime(SERVICE_ID)
    assert runtime.extra["_persisted_profile_options"]["enhanced_mode"] is True

    with _patched_client(fake_client):
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert hass.states.get(entity_id).state == STATE_ON
    runtime = hass.data[DOMAIN][entry.entry_id].get_runtime(SERVICE_ID)
    assert runtime.extra["_persisted_profile_options"]["enhanced_mode"] is True

    with _patched_client(fake_client):
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.options[CONF_IDLE_SHUTDOWN_SERVICE_IDS] == [SERVICE_ID]
    assert hass.states.get(entity_id).state == STATE_ON
    assert SERVICE_ID in hass.data[DOMAIN][entry.entry_id].options.idle_shutdown_service_ids


@pytest.mark.asyncio
async def test_profile_option_default_and_explicit_false_survive_real_reload(
    hass,
    enable_custom_integrations,
    hass_client,
) -> None:
    fake_client = FakeNitradoClient()
    entry = _entry()
    await _setup(hass, entry, fake_client)

    runtime = hass.data[DOMAIN][entry.entry_id].get_runtime(SERVICE_ID)
    assert runtime.extra["_persisted_profile_options"]["strict_validation"] is True

    admin = await hass_client()
    response = await admin.post(
        f"/api/{DOMAIN}/services/{SERVICE_ID}/profile-options/strict_validation",
        json={"value": False},
    )
    assert response.status == 200
    assert entry.options[CONF_PROFILE_OPTIONS]["harness_game"]["strict_validation"][SERVICE_ID] is False
    runtime = hass.data[DOMAIN][entry.entry_id].get_runtime(SERVICE_ID)
    assert runtime.server.player_count is None
    assert runtime.server.player_names == ()
    assert runtime.server.query_valid is False
    assert runtime.server.player_source is None

    with _patched_client(fake_client):
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    runtime = hass.data[DOMAIN][entry.entry_id].get_runtime(SERVICE_ID)
    assert runtime.extra["_persisted_profile_options"]["strict_validation"] is False


@pytest.mark.asyncio
async def test_consent_requirement_api_toggle_disarms_shutdown_and_never_rearms_it(
    hass,
    enable_custom_integrations,
    hass_client,
    harness_profile,
) -> None:
    """The advanced API and standard contract share one safe persisted setting."""

    await harness_profile()
    unregister = await async_register_profile(hass, ConsentHarnessProfile)
    options = _options(auto_shutdown=True)
    options[CONF_PROFILE_OPTIONS] = {"harness_game": {"allow_insecure_transport": {SERVICE_ID: True}}}
    options[CONF_PROFILE_OPTION_ACKNOWLEDGEMENTS] = {"harness_game": {"allow_insecure_transport": {SERVICE_ID: 1}}}
    entry = _entry(options=options)
    fake_client = FakeNitradoClient()
    try:
        await _setup(hass, entry, fake_client)
        coordinator = hass.data[DOMAIN][entry.entry_id]
        assert coordinator.idle_shutdown_configurable(SERVICE_ID)
        assert SERVICE_ID in entry.options[CONF_IDLE_SHUTDOWN_SERVICE_IDS]

        admin = await hass_client()
        index = await admin.get(f"/api/{DOMAIN}/extensions")
        manifest_option = (await index.json())["services"][0]["manifest"]["profile_options"][0]
        assert manifest_option["standard_options"] is True
        assert manifest_option["onboarding"] is True
        assert manifest_option["confirmation_required"] is True
        assert manifest_option["acknowledgement_revision"] == 1
        assert manifest_option["idle_shutdown_required"] is True

        option_url = f"/api/{DOMAIN}/services/{SERVICE_ID}/profile-options/allow_insecure_transport"
        unconfirmed = await admin.post(option_url, json={"value": False})
        assert unconfirmed.status == 400

        disabled = await admin.post(option_url, json={"value": False, "confirm": True})
        assert disabled.status == 200
        assert entry.options[CONF_IDLE_SHUTDOWN_SERVICE_IDS] == []
        assert entry.options[CONF_PROFILE_OPTIONS]["harness_game"]["allow_insecure_transport"][SERVICE_ID] is False
        assert (
            entry.options[CONF_PROFILE_OPTION_ACKNOWLEDGEMENTS]["harness_game"]["allow_insecure_transport"][SERVICE_ID]
            == 1
        )
        runtime = coordinator.get_runtime(SERVICE_ID)
        assert runtime.server.query_valid is False
        assert runtime.server.player_count is None
        assert runtime.idle_started_at is None
        assert runtime.shutdown_pending is False
        assert not coordinator.idle_shutdown_configurable(SERVICE_ID)

        enabled = await admin.post(option_url, json={"value": True, "confirm": True})
        assert enabled.status == 200
        assert entry.options[CONF_IDLE_SHUTDOWN_SERVICE_IDS] == []
        assert coordinator.idle_shutdown_configurable(SERVICE_ID)
    finally:
        with _patched_client(fake_client):
            if entry.state.recoverable:
                await hass.config_entries.async_unload(entry.entry_id)
            await unregister()
            await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_standard_options_flow_requires_confirmation_and_preserves_manual_rearming(
    hass,
    enable_custom_integrations,
    harness_profile,
) -> None:
    """Consent remains reachable in Options without silently rearming shutdown."""

    await harness_profile()
    unregister = await async_register_profile(hass, ConsentHarnessProfile)
    options = _options(auto_shutdown=True)
    options[CONF_PROFILE_OPTIONS] = {"harness_game": {"allow_insecure_transport": {SERVICE_ID: True}}}
    options[CONF_PROFILE_OPTION_ACKNOWLEDGEMENTS] = {"harness_game": {"allow_insecure_transport": {SERVICE_ID: 1}}}
    entry = _entry(options=options)
    fake_client = FakeNitradoClient()
    try:
        await _setup(hass, entry, fake_client)
        started = await hass.config_entries.options.async_init(entry.entry_id)
        assert started["type"] == "menu"
        assert "profile_settings" in started["menu_options"]

        selected = await hass.config_entries.options.async_configure(
            started["flow_id"],
            user_input={"next_step_id": "profile_settings"},
        )
        assert selected["type"] == "form"
        assert selected["step_id"] == "profile_settings"
        choice_validator = next(iter(selected["data_schema"].schema.values()))
        choice_label = choice_validator.container[f"{SERVICE_ID}:allow_insecure_transport"]
        assert "→ Harness Game → Allow insecure transport" in choice_label

        selected = await hass.config_entries.options.async_configure(
            selected["flow_id"],
            user_input={"profile_setting": f"{SERVICE_ID}:allow_insecure_transport"},
        )
        assert selected["type"] == "form"
        assert selected["step_id"] == "profile_option"

        rejected = await hass.config_entries.options.async_configure(
            selected["flow_id"],
            user_input={"enabled": False, "confirm": False},
        )
        assert rejected["type"] == "form"
        assert rejected["errors"] == {"confirm": "confirmation_required"}

        with _patched_client(fake_client):
            completed = await hass.config_entries.options.async_configure(
                rejected["flow_id"],
                user_input={"enabled": False, "confirm": True},
            )
            await hass.async_block_till_done()
        assert completed["type"] == "create_entry"
        assert entry.options[CONF_IDLE_SHUTDOWN_SERVICE_IDS] == []
        coordinator = hass.data[DOMAIN][entry.entry_id]
        assert coordinator.options.idle_shutdown_service_ids == frozenset()
        runtime = coordinator.get_runtime(SERVICE_ID)
        assert runtime.server.query_valid is False
        assert runtime.server.player_count is None
        assert runtime.shutdown_pending is False

        started = await hass.config_entries.options.async_init(entry.entry_id)
        selected = await hass.config_entries.options.async_configure(
            started["flow_id"],
            user_input={"next_step_id": "profile_settings"},
        )
        assert selected["step_id"] == "profile_settings"
        selected = await hass.config_entries.options.async_configure(
            selected["flow_id"],
            user_input={"profile_setting": f"{SERVICE_ID}:allow_insecure_transport"},
        )
        assert selected["step_id"] == "profile_option"
        with _patched_client(fake_client):
            completed = await hass.config_entries.options.async_configure(
                selected["flow_id"],
                user_input={"enabled": True, "confirm": True},
            )
            await hass.async_block_till_done()
        assert completed["type"] == "create_entry"
        assert entry.options[CONF_IDLE_SHUTDOWN_SERVICE_IDS] == []
        assert coordinator.options.idle_shutdown_service_ids == frozenset()
        assert entry.options[CONF_PROFILE_OPTIONS]["harness_game"]["allow_insecure_transport"][SERVICE_ID] is True
    finally:
        with _patched_client(fake_client):
            if entry.state.recoverable:
                await hass.config_entries.async_unload(entry.entry_id)
            await unregister()
            await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_service_import_onboarding_requires_explicit_profile_security_choice(
    hass,
    enable_custom_integrations,
    harness_profile,
) -> None:
    """A new service cannot finish onboarding with an unresolved security decision."""

    await harness_profile()
    unregister = await async_register_profile(hass, ConsentHarnessProfile)
    options = default_service_options()
    options[CONF_SETTLE_SECONDS] = 0
    entry = _entry(options=options)
    fake_client = FakeNitradoClient()
    try:
        await _setup(hass, entry, fake_client)
        assert entry.options[CONF_IMPORTED_SERVICE_IDS] == []

        started = await hass.config_entries.options.async_init(entry.entry_id)
        services = await hass.config_entries.options.async_configure(
            started["flow_id"],
            user_input={"next_step_id": "services"},
        )
        assert services["type"] == "form"
        assert services["step_id"] == "services"

        import_form = await hass.config_entries.options.async_configure(
            services["flow_id"],
            user_input={"service_action": f"import:{SERVICE_ID}"},
        )
        assert import_form["type"] == "form"
        assert import_form["step_id"] == "import_service"

        onboarding = await hass.config_entries.options.async_configure(
            import_form["flow_id"],
            user_input={"display_name": "Harness Server", "area_id": ""},
        )
        assert onboarding["type"] == "form"
        assert onboarding["step_id"] == "profile_onboarding"

        rejected = await hass.config_entries.options.async_configure(
            onboarding["flow_id"],
            user_input={"enabled": False, "confirm": False},
        )
        assert rejected["type"] == "form"
        assert rejected["errors"] == {"confirm": "confirmation_required"}

        with _patched_client(fake_client):
            completed = await hass.config_entries.options.async_configure(
                rejected["flow_id"],
                user_input={"enabled": False, "confirm": True},
            )
            await hass.async_block_till_done()
        assert completed["type"] == "create_entry"
        assert entry.options[CONF_IMPORTED_SERVICE_IDS] == [SERVICE_ID]
        assert entry.options[CONF_IDLE_SHUTDOWN_SERVICE_IDS] == []
        assert entry.options[CONF_PROFILE_OPTIONS]["harness_game"]["allow_insecure_transport"][SERVICE_ID] is False
        assert (
            entry.options[CONF_PROFILE_OPTION_ACKNOWLEDGEMENTS]["harness_game"]["allow_insecure_transport"][SERVICE_ID]
            == 1
        )
    finally:
        with _patched_client(fake_client):
            if entry.state.recoverable:
                await hass.config_entries.async_unload(entry.entry_id)
            await unregister()
            await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_upgrade_with_unresolved_consent_disarms_shutdown_and_creates_fix_flow(
    hass,
    enable_custom_integrations,
    harness_profile,
) -> None:
    """An upgrade gap is loud, fixable, and never leaves shutdown apparently armed."""

    await harness_profile()
    unregister = await async_register_profile(hass, ConsentHarnessProfile)
    entry = _entry(options=_options(auto_shutdown=True))
    fake_client = FakeNitradoClient()
    try:
        await _setup(hass, entry, fake_client)
        assert entry.options[CONF_IDLE_SHUTDOWN_SERVICE_IDS] == []
        coordinator = hass.data[DOMAIN][entry.entry_id]
        assert not coordinator.idle_shutdown_configurable(SERVICE_ID)

        issue_id = f"{entry.entry_id}_profile_option_unacknowledged_{SERVICE_ID}_allow_insecure_transport"
        issue = ir.async_get(hass).issues[(DOMAIN, issue_id)]
        assert issue.is_fixable
        assert issue.translation_key == "profile_option_acknowledgement_required"

        flow = await async_create_fix_flow(hass, issue_id, issue.data)
        flow.hass = hass
        form = await flow.async_step_init()
        assert form["type"] == "form"
        assert form["step_id"] == "profile_option"

        rejected = await flow.async_step_profile_option({"enabled": True, "confirm": False})
        assert rejected["errors"] == {"confirm": "confirmation_required"}
        completed = await flow.async_step_profile_option({"enabled": True, "confirm": True})
        assert completed["type"] == "create_entry"
        assert entry.options[CONF_IDLE_SHUTDOWN_SERVICE_IDS] == []
        assert coordinator.idle_shutdown_configurable(SERVICE_ID)
        assert (
            entry.options[CONF_PROFILE_OPTION_ACKNOWLEDGEMENTS]["harness_game"]["allow_insecure_transport"][SERVICE_ID]
            == 1
        )
        assert (DOMAIN, issue_id) not in ir.async_get(hass).issues
    finally:
        with _patched_client(fake_client):
            if entry.state.recoverable:
                await hass.config_entries.async_unload(entry.entry_id)
            await unregister()
            await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_real_migration_repairs_skinny_options_without_losing_intent(hass, enable_custom_integrations) -> None:
    entry = _entry(
        version=1,
        options={
            CONF_IMPORTED_SERVICE_IDS: [SERVICE_ID],
            CONF_IDLE_SHUTDOWN_SERVICE_IDS: [SERVICE_ID],
            CONF_STATUS_INTERVAL: "broken",
        },
    )
    await _setup(hass, entry, FakeNitradoClient())

    assert entry.version == 2
    assert entry.options[CONF_IDLE_SHUTDOWN_SERVICE_IDS] == [SERVICE_ID]
    assert isinstance(entry.options[CONF_STATUS_INTERVAL], int)
    assert set(default_service_options()).issubset(entry.options)


@pytest.mark.asyncio
async def test_real_options_update_reloads_polling_settings_and_preserves_shutdown(
    hass, enable_custom_integrations
) -> None:
    fake_client = FakeNitradoClient()
    entry = _entry()
    await _setup(hass, entry, fake_client)

    updated = dict(entry.options)
    updated[CONF_STATUS_INTERVAL] = 120
    with _patched_client(fake_client):
        hass.config_entries.async_update_entry(entry, options=updated)
        await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.options.status_interval == 120
    assert SERVICE_ID in coordinator.options.idle_shutdown_service_ids
    assert entry.options[CONF_IDLE_SHUTDOWN_SERVICE_IDS] == [SERVICE_ID]


@pytest.mark.asyncio
async def test_real_http_permissions_and_redaction(
    hass,
    enable_custom_integrations,
    hass_client,
    hass_read_only_access_token,
) -> None:
    entry = _entry()
    await _setup(hass, entry, FakeNitradoClient())
    read_url = f"/api/{DOMAIN}/services/{SERVICE_ID}/editable-files/settings/read"
    action_url = f"/api/{DOMAIN}/services/{SERVICE_ID}/actions/record"
    option_url = f"/api/{DOMAIN}/services/{SERVICE_ID}/profile-options/allow_insecure_transport"
    secret_option_url = f"/api/{DOMAIN}/services/{SERVICE_ID}/profile-options/admin_password"

    read_only = await hass_client(hass_read_only_access_token)
    denied_read = await read_only.post(read_url, json={"include_raw": False})
    denied_action = await read_only.post(action_url, json={"confirm": False})
    denied_index = await read_only.get(f"/api/{DOMAIN}/extensions")
    denied_option = await read_only.post(option_url, json={"value": True})
    assert denied_read.status == 403
    assert denied_action.status == 403
    assert denied_index.status == 403
    assert denied_option.status == 403

    admin = await hass_client()
    with patch.object(
        NitradoFilesystemService,
        "read_bytes",
        new=AsyncMock(return_value=b'AdminPassword="secret-value"'),
    ):
        response = await admin.post(read_url, json={"include_raw": False})
    assert response.status == 200
    body = await response.json()
    serialized = str(body)
    assert "secret-value" not in serialized
    assert "parsed" not in body
    assert "[redacted]" in serialized

    index = await admin.get(f"/api/{DOMAIN}/extensions")
    assert index.status == 200
    index_body = await index.json()
    assert index_body["services"][0]["service_id"] == SERVICE_ID
    assert index_body["services"][0]["account_entry_id"] == entry.entry_id
    assert index_body["services"][0]["account_title"] == "Nitrado Account"
    assert index_body["services"][0]["manifest"]["editable_files"][0]["key"] == "settings"
    assert index_body["services"][0]["manifest"]["profile_options"][0]["key"] == "allow_insecure_transport"

    secret_saved = await admin.post(secret_option_url, json={"value": "never-return-this"})
    assert secret_saved.status == 200
    secret_body = await secret_saved.json()
    assert secret_body == {"key": "admin_password", "configured": True}
    refreshed_index = await admin.get(f"/api/{DOMAIN}/extensions")
    refreshed_body = await refreshed_index.json()
    assert refreshed_body["services"][0]["profile_option_configured"]["admin_password"] is True
    assert "never-return-this" not in json.dumps(refreshed_body)

    secret_cleared = await admin.post(secret_option_url, json={"clear": True})
    assert secret_cleared.status == 200
    assert await secret_cleared.json() == {"key": "admin_password", "configured": False}
    cleared_index = await admin.get(f"/api/{DOMAIN}/extensions")
    cleared_body = await cleared_index.json()
    assert cleared_body["services"][0]["profile_option_configured"]["admin_password"] is False
    assert "admin_password" not in entry.options.get(CONF_PROFILE_OPTIONS, {}).get("harness_game", {})

    stream = await admin.post(
        f"/api/{DOMAIN}/services/{SERVICE_ID}/resources/event_stream",
        json={"payload": None, "use_cache": False},
    )
    assert stream.status == 200
    assert await stream.text() == "first\nsecond\n"
    runtime = hass.data[DOMAIN][entry.entry_id].get_runtime(SERVICE_ID)
    assert runtime.profile_extra()["stream_closed"] is True
    binary = await admin.post(
        f"/api/{DOMAIN}/services/{SERVICE_ID}/resources/binary_blob",
        json={"payload": None, "use_cache": False},
    )
    assert binary.status == 200
    assert await binary.read() == b"binary"

    saved = await admin.post(option_url, json={"value": True})
    assert saved.status == 200
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.options[CONF_PROFILE_OPTIONS]["harness_game"]["allow_insecure_transport"][SERVICE_ID] is True


@pytest.mark.asyncio
async def test_real_cockpit_structured_read_and_preview_contract(
    hass,
    enable_custom_integrations,
    hass_client,
    harness_profile,
) -> None:
    """Operations plus source revision traverse the authenticated real-HA route."""

    await harness_profile()
    unregister = await async_register_profile(hass, StructuredHarnessProfile)
    entry = _entry()
    fake_client = FakeNitradoClient(status="stopped")
    source = 'AdminPassword="secret-value"\ndifficulty=1\n'
    fake_client.files = {path: source for path in fake_client.files}
    try:
        await _setup(hass, entry, fake_client)
        admin = await hass_client()
        bootstrap = await admin.get(
            f"/api/{DOMAIN}/accounts/{entry.entry_id}/services/{SERVICE_ID}/cockpit/bootstrap?mount_epoch=structured-test"
        )
        assert bootstrap.status == 200
        bootstrap_body = await bootstrap.json()
        lease = bootstrap_body["lease"]
        assert bootstrap_body["snapshot"]["profile"]["editor_mutations_enabled"] is False
        assert bootstrap_body["snapshot"]["profile"]["save_bundle_mutations_enabled"] is False
        capability_url = f"/api/{DOMAIN}/accounts/{entry.entry_id}/services/{SERVICE_ID}/cockpit/capabilities"
        read = await admin.post(
            f"{capability_url}/editable-read",
            json={"lease": lease, "file_key": "settings"},
        )
        assert read.status == 200
        read_body = await read.json()
        assert "secret-value" not in json.dumps(read_body)
        assert read_body["file"]["model"]["settings"][0]["raw_value"] is None
        revision = read_body["file"]["revision"]

        preview = await admin.post(
            f"{capability_url}/editable-preview",
            json={
                "lease": lease,
                "file_key": "settings",
                "source_revision": revision,
                "operations": [{"op": "set", "key": "difficulty", "raw_value": "2"}],
            },
        )
        assert preview.status == 200
        preview_body = await preview.json()
        assert preview_body["preview"]["changed"] is True
        assert preview_body["preview"]["verdict"]["state"] == "supported"
        assert "difficulty=2" in preview_body["preview"]["redacted_diff"]
        assert "secret-value" not in json.dumps(preview_body)
        assert isinstance(preview_body["preview_token"], str)

        coordinator = hass.data[DOMAIN][entry.entry_id]
        recovery = await coordinator.editable_file_journal.async_record(
            account_entry_id=entry.entry_id,
            service_id=SERVICE_ID,
            file_key="settings",
            declared_path=read_body["file"]["path"],
            backup_path=f"{read_body['file']['path']}.nitrado_gameserver.backup.test",
            backup_kind="apply",
            source_revision="5" * 64,
            resulting_revision="6" * 64,
            operation_id="operation-test",
        )
        original_files = dict(fake_client.files)
        original_operations = await coordinator.async_operation_records(SERVICE_ID)
        original_recovery_records = await coordinator.editable_file_journal.async_records(SERVICE_ID, "settings")
        apply_editable = AsyncMock()
        with patch.object(type(coordinator), "async_apply_editable_file", new=apply_editable):
            rejected_apply = await admin.post(
                f"{capability_url}/editable-apply",
                json={
                    "lease": lease,
                    "confirm": True,
                    "file_key": "settings",
                    "source_revision": revision,
                    "operations": [{"op": "set", "key": "difficulty", "raw_value": "2"}],
                    "preview_token": preview_body["preview_token"],
                },
            )
        assert rejected_apply.status == 400
        apply_editable.assert_not_awaited()
        assert preview_body["preview_token"] in cockpit_state(hass).editable_previews
        rejected_rollback = await admin.post(
            f"{capability_url}/editable-rollback",
            json={
                "lease": lease,
                "confirm": True,
                "file_key": "settings",
                "recovery_id": recovery.recovery_id,
            },
        )
        assert rejected_rollback.status == 400
        assert (
            await coordinator.editable_file_journal.async_require(
                SERVICE_ID,
                "settings",
                recovery.recovery_id,
            )
            == recovery
        )

        cockpit_lease = cockpit_state(hass).leases[lease]
        private_preview = SimpleNamespace(key="world", target_root="/save/world", closed=False)
        private_preview.close = lambda: setattr(private_preview, "closed", True)
        save_grant = issue_save_bundle_preview_grant(
            hass,
            lease=cockpit_lease,
            preview=private_preview,
            expected_current_digest="3" * 64,
            proposed_digest="4" * 64,
        )
        apply_save = AsyncMock()
        with patch.object(type(coordinator), "async_apply_save_bundle", new=apply_save):
            rejected_restore = await admin.post(
                f"{capability_url}/save-bundle-apply",
                json={
                    "lease": lease,
                    "confirm": True,
                    "bundle_key": "world",
                    "preview_token": save_grant.token,
                },
            )
        assert rejected_restore.status == 400
        apply_save.assert_not_awaited()
        assert cockpit_state(hass).save_bundle_previews[save_grant.token] is save_grant
        assert private_preview.closed is False

        for route in ("apply", "rollback"):
            legacy = await admin.post(
                f"/api/{DOMAIN}/services/{SERVICE_ID}/editable-files/settings/{route}",
                json={"confirm": True},
            )
            assert legacy.status == 400

        assert fake_client.files == original_files
        assert await coordinator.async_operation_records(SERVICE_ID) == original_operations
        assert (
            await coordinator.editable_file_journal.async_records(
                SERVICE_ID,
                "settings",
            )
            == original_recovery_records
        )
    finally:
        if entry.state.recoverable:
            await hass.config_entries.async_unload(entry.entry_id)
        await unregister()
        await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_core_admin_panel_refuses_approval_but_allows_stale_plan_rejection(
    hass,
    enable_custom_integrations,
    hass_client,
    hass_read_only_access_token,
) -> None:
    nitrado_entry = _entry()
    fake_client = FakeNitradoClient(status="stopped")
    await _setup(hass, nitrado_entry, fake_client)
    coordinator = hass.data[DOMAIN][nitrado_entry.entry_id]
    original_files = dict(fake_client.files)
    original_operations = await coordinator.async_operation_records(SERVICE_ID)
    companion = MockConfigEntry(domain="nitrado_companion", title="Companion", data={})
    companion.add_to_hass(hass)
    runtime = get_provider_runtime(hass)
    assert runtime is not None
    await runtime.authority.async_put_grant(
        ProviderGrant(
            "mutation-ui-grant",
            companion.domain,
            companion.entry_id,
            ProviderServiceRef(nitrado_entry.entry_id, SERVICE_ID),
            frozenset({ProviderScope.FILESYSTEM_REPLACE}),
            permitted_roots=frozenset({"game"}),
        )
    )
    lease = await async_register_provider_consumer(
        hass,
        api_version=PROVIDER_CONNECTOR_API_VERSION,
        consumer_domain=companion.domain,
        consumer_entry_id=companion.entry_id,
    )
    # Simulate a plan created before the Release A capability gate took
    # effect. The live route must never approve it, but administrators still
    # need a way to reject and clear it safely.
    with (
        patch(
            "custom_components.nitrado_gameserver.filesystem.NitradoFtpTransport.capabilities",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "custom_components.nitrado_gameserver.provider_runtime._DISABLED_PROVIDER_SCOPES",
            frozenset(),
        ),
        patch.object(runtime.registry, "_disabled_scopes", frozenset()),
    ):
        connector = await lease.async_get_connector(
            ProviderServiceRef(nitrado_entry.entry_id, SERVICE_ID),
            scopes={ProviderScope.FILESYSTEM_REPLACE},
            roots={"game"},
        )
        current = ProviderTreeManifest.from_files((ProviderTreeFile.from_bytes("old.txt", b"old"),))
        request = ProviderTreeReplaceRequest(
            "game",
            (ProviderTreeFile.from_bytes("new.txt", b"new"),),
            current,
        )
        plan = connector.plan_tree_replace(request)
        waiting = asyncio.create_task(connector.async_wait_for_approval(plan))
        await asyncio.sleep(0)
    url = f"/api/{DOMAIN}/provider-mutation-approvals"

    read_only = await hass_client(hass_read_only_access_token)
    assert (await read_only.get(url)).status == 403

    admin = await hass_client()
    pending = await admin.get(url)
    assert pending.status == 200
    pending_body = await pending.json()
    assert pending_body["plans"][0]["plan_id"] == plan.plan_id
    assert pending_body["plans"][0]["payload_digest"] == plan.payload_digest
    approved = await admin.post(
        url,
        json={
            "plan_id": plan.plan_id,
            "payload_digest": plan.payload_digest,
            "decision": "approve",
            "confirm": True,
        },
    )
    assert approved.status == 400
    rejected = await admin.post(
        url,
        json={
            "plan_id": plan.plan_id,
            "payload_digest": plan.payload_digest,
            "decision": "reject",
            "confirm": True,
        },
    )
    assert rejected.status == 200
    with pytest.raises(ProviderPlanError, match="administrator rejected"):
        await waiting
    assert fake_client.files == original_files
    assert await coordinator.async_operation_records(SERVICE_ID) == original_operations
    await lease.async_close()


@pytest.mark.asyncio
async def test_concurrent_profile_option_requests_are_serialized_without_lost_updates(
    hass,
    enable_custom_integrations,
    hass_client,
) -> None:
    entry = _entry()
    await _setup(hass, entry, FakeNitradoClient())
    coordinator = hass.data[DOMAIN][entry.entry_id]
    original = coordinator.async_profile_options_changed
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    calls = 0
    reservation_lock_facts = []
    original_operation = type(coordinator).async_operation

    @asynccontextmanager
    async def checked_operation(self, service_id, intent, **kwargs):
        assert self is coordinator
        reservation_lock_facts.append(entry_option_update_lock(hass, entry.entry_id).locked())
        async with original_operation(self, service_id, intent, **kwargs) as reservation:
            yield reservation

    async def hold_first(self, runtime, reason):
        nonlocal calls
        assert self is coordinator
        calls += 1
        if calls == 1:
            first_started.set()
            await release_first.wait()
        await original(runtime, reason)

    admin = await hass_client()
    first_url = f"/api/{DOMAIN}/services/{SERVICE_ID}/profile-options/allow_insecure_transport"
    second_url = f"/api/{DOMAIN}/services/{SERVICE_ID}/profile-options/strict_validation"
    with (
        patch.object(type(coordinator), "async_profile_options_changed", new=hold_first),
        patch.object(type(coordinator), "async_operation", new=checked_operation),
    ):
        first_request = asyncio.create_task(admin.post(first_url, json={"value": True}))
        await first_started.wait()
        second_request = asyncio.create_task(admin.post(second_url, json={"value": False}))
        await asyncio.sleep(0.02)
        assert not second_request.done()
        release_first.set()
        first_response, second_response = await asyncio.gather(first_request, second_request)

    assert first_response.status == 200
    assert second_response.status == 200
    values = entry.options[CONF_PROFILE_OPTIONS]["harness_game"]
    assert values["allow_insecure_transport"][SERVICE_ID] is True
    assert values["strict_validation"][SERVICE_ID] is False
    assert reservation_lock_facts == [True, True]


@pytest.mark.asyncio
async def test_frontend_panel_registration_is_admin_only_visible_and_reversible(hass) -> None:
    from homeassistant.components import frontend

    hass.config.components.add("frontend")
    http = MagicMock()
    http.async_register_static_paths = AsyncMock()
    hass.http = http
    with (
        patch.object(frontend, "async_register_built_in_panel") as register_panel,
        patch.object(frontend, "async_remove_panel") as remove_panel,
    ):
        await async_register_extension_panel(hass)
        await async_register_extension_panel(hass)

        http.async_register_static_paths.assert_awaited_once()
        register_panel.assert_called_once()
        kwargs = register_panel.call_args.kwargs
        assert kwargs["frontend_url_path"] == PANEL_URL_PATH
        assert kwargs["require_admin"] is True
        assert kwargs["sidebar_title"] == "Nitrado Servers"
        assert kwargs["show_in_sidebar"] is True
        assert "config_panel_domain" not in kwargs
        panel_config = kwargs["config"]["_panel_custom"]
        assert panel_config["name"] == PANEL_ELEMENT
        assert panel_config["module_url"] == PANEL_MODULE_URL
        assert panel_config["module_url"] == f"{PANEL_STATIC_URL}?v={PANEL_ASSET_VERSION}"
        static_config = http.async_register_static_paths.await_args.args[0][0]
        assert static_config.url_path == PANEL_STATIC_URL

        async_unregister_extension_panel(hass)
        remove_panel.assert_called_once_with(hass, PANEL_URL_PATH)


@pytest.mark.asyncio
async def test_real_device_target_service_schema_and_profile_action(
    hass,
    enable_custom_integrations,
    hass_read_only_user,
) -> None:
    fake_client = FakeNitradoClient(status="stopped")
    entry = _entry()
    await _setup(hass, entry, fake_client)
    device = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, f"account:{entry.entry_id}:service:{SERVICE_ID}")}
    )
    assert device is not None
    assert device.configuration_url == (f"homeassistant://nitrado-game-servers/{entry.entry_id}/{SERVICE_ID}")

    restricted_context = Context(user_id=hass_read_only_user.id)
    for action, data in (
        ("start", {"device_id": device.id}),
        ("start", {"device_id": device.id, "force": True}),
        ("stop", {"device_id": device.id}),
        ("cancel_pending_shutdown", {"device_id": device.id}),
        ("ignore_service", {"service_id": SERVICE_ID}),
        ("remove_service", {"service_id": SERVICE_ID}),
    ):
        with pytest.raises(Unauthorized):
            await hass.services.async_call(
                DOMAIN,
                action,
                data,
                blocking=True,
                context=restricted_context,
            )

    await hass.services.async_call(
        DOMAIN,
        "start",
        {"device_id": device.id, "force": True},
        blocking=True,
    )
    # Provider acknowledgement is not completion evidence.  Observe a fresh
    # started state before dispatching the next service mutation.
    fake_client.status = "started"
    await hass.data[DOMAIN][entry.entry_id].async_refresh_service(
        SERVICE_ID,
        handle_idle_shutdown=False,
    )
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            "profile_action",
            {"device_id": device.id, "action_key": "record", "payload": {"source": "denied"}},
            blocking=True,
            context=restricted_context,
        )
    await hass.services.async_call(
        DOMAIN,
        "profile_action",
        {"device_id": device.id, "action_key": "record", "payload": {"source": "ha"}},
        blocking=True,
    )
    for invalid_payload in ({"value": float("nan")}, "x" * 70_000):
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                DOMAIN,
                "profile_action",
                {"device_id": device.id, "action_key": "record", "payload": invalid_payload},
                blocking=True,
            )

    assert fake_client.start_calls == [(SERVICE_ID, "harnessgame")]
    runtime = hass.data[DOMAIN][entry.entry_id].get_runtime(SERVICE_ID)
    assert runtime.profile_extra()["harness_action_payload"] == {"source": "ha"}


@pytest.mark.asyncio
async def test_duplicate_service_ids_are_isolated_by_account_entry(
    hass,
    enable_custom_integrations,
) -> None:
    """Never resolve an account-ambiguous service ID by first match."""

    first_client = FakeNitradoClient(status="stopped", account_id="account-one")
    second_client = FakeNitradoClient(status="stopped", account_id="account-two")
    first_entry = _entry(account_id="account-one", api_token="first-token")
    second_entry = _entry(account_id="account-two", api_token="second-token")
    await _setup(hass, first_entry, first_client)
    await _setup(hass, second_entry, second_client)

    registry = dr.async_get(hass)
    first_device = registry.async_get_device(
        identifiers={(DOMAIN, f"account:{first_entry.entry_id}:service:{SERVICE_ID}")}
    )
    second_device = registry.async_get_device(
        identifiers={(DOMAIN, f"account:{second_entry.entry_id}:service:{SERVICE_ID}")}
    )
    assert first_device is not None
    assert second_device is not None
    assert first_device.id != second_device.id

    entity_registry = er.async_get(hass)
    first_start = entity_registry.async_get_entity_id(
        "button",
        DOMAIN,
        f"account:{first_entry.entry_id}:service:{SERVICE_ID}:start",
    )
    second_start = entity_registry.async_get_entity_id(
        "button",
        DOMAIN,
        f"account:{second_entry.entry_id}:service:{SERVICE_ID}:start",
    )
    assert first_start is not None
    assert second_start is not None
    assert first_start != second_start

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            "start",
            {"service_id": SERVICE_ID},
            blocking=True,
        )
    assert first_client.start_calls == []
    assert second_client.start_calls == []

    await hass.services.async_call(
        DOMAIN,
        "start",
        {"device_id": first_device.id},
        blocking=True,
    )
    assert first_client.start_calls == [(SERVICE_ID, "harnessgame")]
    assert second_client.start_calls == []


@pytest.mark.asyncio
async def test_real_service_permissions_are_action_specific(hass, enable_custom_integrations) -> None:
    fake_client = FakeNitradoClient(status="stopped")
    entry = _entry()
    await _setup(hass, entry, fake_client)
    start_entity = _entity_id(hass, f"service:{SERVICE_ID}:start", platform="button")
    refresh_entity = _entity_id(hass, f"service:{SERVICE_ID}:refresh", platform="button")

    user = MagicMock(is_admin=False)
    user.permissions.check_entity.side_effect = lambda entity_id, _policy: entity_id == refresh_entity
    context = Context(user_id="partial-control-user")
    with patch.object(hass.auth, "async_get_user", new=AsyncMock(return_value=user)):
        with pytest.raises(Unauthorized):
            await hass.services.async_call(
                DOMAIN,
                "start",
                {"service_id": SERVICE_ID},
                blocking=True,
                context=context,
            )

        user.permissions.check_entity.side_effect = lambda entity_id, _policy: entity_id == start_entity
        await hass.services.async_call(
            DOMAIN,
            "start",
            {"service_id": SERVICE_ID},
            blocking=True,
            context=context,
        )

        with pytest.raises(Unauthorized):
            await hass.services.async_call(
                DOMAIN,
                "profile_action",
                {"service_id": SERVICE_ID, "action_key": "record"},
                blocking=True,
                context=context,
            )


@pytest.mark.asyncio
async def test_real_reauth_and_duplicate_account_flow(hass, enable_custom_integrations) -> None:
    fake_client = FakeNitradoClient()
    entry = _entry()
    await _setup(hass, entry, fake_client)

    with (
        patch("custom_components.nitrado_gameserver.config_flow.NitradoClient", return_value=fake_client),
        _patched_client(fake_client),
    ):
        duplicate = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "user"},
            data={CONF_API_TOKEN: "duplicate-token"},
        )
        assert duplicate["type"] == "abort"
        assert duplicate["reason"] == "already_configured"

        started = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "reauth", "entry_id": entry.entry_id},
            data=dict(entry.data),
        )
        assert started["type"] == "form"
        assert started["step_id"] == "reauth_confirm"
        completed = await hass.config_entries.flow.async_configure(
            started["flow_id"],
            user_input={CONF_API_TOKEN: "replacement-token"},
        )
        await hass.async_block_till_done()

    assert completed["type"] == "abort"
    assert completed["reason"] == "reauth_successful"
    assert entry.data[CONF_API_TOKEN] == "replacement-token"
    assert entry.options[CONF_IDLE_SHUTDOWN_SERVICE_IDS] == [SERVICE_ID]


@pytest.mark.asyncio
async def test_real_fallback_identity_rejects_overlapping_duplicate_and_allows_reauth(
    hass,
    enable_custom_integrations,
) -> None:
    fake_client = FallbackIdentityClient()
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Nitrado Account",
        data={
            CONF_API_TOKEN: "old-fallback-token",
            CONF_ACCOUNT_ID: "fallback-existing-identity",
            CONF_ACCOUNT_UUID: "fallback-account-uuid",
        },
        options=_options(),
        unique_id="account:fallback-existing-identity",
        version=2,
    )
    await _setup(hass, entry, fake_client)

    with (
        patch("custom_components.nitrado_gameserver.config_flow.NitradoClient", return_value=fake_client),
        _patched_client(fake_client),
    ):
        duplicate = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "user"},
            data={CONF_API_TOKEN: "different-token-same-account"},
        )
        assert duplicate["type"] == "abort"
        assert duplicate["reason"] == "already_configured"

        started = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "reauth", "entry_id": entry.entry_id},
            data=dict(entry.data),
        )
        completed = await hass.config_entries.flow.async_configure(
            started["flow_id"],
            user_input={CONF_API_TOKEN: "replacement-fallback-token"},
        )
        await hass.async_block_till_done()

    assert completed["type"] == "abort"
    assert completed["reason"] == "reauth_successful"
    assert entry.data[CONF_API_TOKEN] == "replacement-fallback-token"
    assert entry.data[CONF_ACCOUNT_ID] == "fallback-existing-identity"
    assert entry.unique_id == "account:fallback-existing-identity"


@pytest.mark.asyncio
async def test_fallback_identity_upgrades_to_stable_on_setup(hass, enable_custom_integrations) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Nitrado Account",
        data={
            CONF_API_TOKEN: "existing-token",
            CONF_ACCOUNT_ID: "fallback-existing-identity",
            CONF_ACCOUNT_UUID: "fallback-account-uuid",
        },
        options=_options(),
        unique_id="account:fallback-existing-identity",
        version=2,
    )
    await _setup(hass, entry, FakeNitradoClient(account_id="stable-account-id"))

    assert entry.data[CONF_ACCOUNT_ID] == "stable-account-id"
    assert entry.unique_id == "account:stable-account-id"


@pytest.mark.asyncio
async def test_fallback_to_stable_reauth_uses_service_overlap_evidence(
    hass,
    enable_custom_integrations,
) -> None:
    fallback_client = FallbackIdentityClient()
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Nitrado Account",
        data={
            CONF_API_TOKEN: "old-fallback-token",
            CONF_ACCOUNT_ID: "fallback-existing-identity",
            CONF_ACCOUNT_UUID: "fallback-account-uuid",
        },
        options=_options(),
        unique_id="account:fallback-existing-identity",
        version=2,
    )
    await _setup(hass, entry, fallback_client)
    stable_client = FakeNitradoClient(account_id="stable-account-id")

    with (
        patch("custom_components.nitrado_gameserver.config_flow.NitradoClient", return_value=stable_client),
        _patched_client(stable_client),
    ):
        duplicate = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "user"},
            data={CONF_API_TOKEN: "same-account-stable-token"},
        )
        assert duplicate["type"] == "abort"
        assert duplicate["reason"] == "already_configured"

        started = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "reauth", "entry_id": entry.entry_id},
            data=dict(entry.data),
        )
        completed = await hass.config_entries.flow.async_configure(
            started["flow_id"],
            user_input={CONF_API_TOKEN: "same-account-stable-token"},
        )
        await hass.async_block_till_done()

    assert completed["type"] == "abort"
    assert completed["reason"] == "reauth_successful"
    assert entry.data[CONF_ACCOUNT_ID] == "stable-account-id"
    assert entry.unique_id == "account:stable-account-id"


@pytest.mark.asyncio
async def test_stable_to_fallback_reauth_preserves_verified_identity(
    hass,
    enable_custom_integrations,
) -> None:
    stable_client = FakeNitradoClient(account_id="stable-account-id")
    entry = _entry()
    await _setup(hass, entry, stable_client)
    fallback_client = FallbackIdentityClient()

    with (
        patch("custom_components.nitrado_gameserver.config_flow.NitradoClient", return_value=fallback_client),
        _patched_client(fallback_client),
    ):
        started = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "reauth", "entry_id": entry.entry_id},
            data=dict(entry.data),
        )
        completed = await hass.config_entries.flow.async_configure(
            started["flow_id"],
            user_input={CONF_API_TOKEN: "reduced-scope-token"},
        )
        await hass.async_block_till_done()

    assert completed["type"] == "abort"
    assert completed["reason"] == "reauth_successful"
    assert entry.data[CONF_ACCOUNT_ID] == "stable-account-id"
    assert entry.unique_id == "account:stable-account-id"


@pytest.mark.asyncio
async def test_setup_reconciles_stale_repairs_from_before_restart(hass, enable_custom_integrations) -> None:
    fake_client = FakeNitradoClient()
    entry = _entry()
    stale_issue_id = f"{entry.entry_id}_ftp_transport_unavailable_{SERVICE_ID}"
    hass.set_state(CoreState.starting)
    ir.async_create_issue(
        hass,
        DOMAIN,
        stale_issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="ftp_transport_unavailable",
        translation_placeholders={"service_id": SERVICE_ID, "service_name": "Harness Server"},
    )
    assert (DOMAIN, stale_issue_id) in ir.async_get(hass).issues

    await _setup(hass, entry, fake_client)

    assert (DOMAIN, stale_issue_id) not in ir.async_get(hass).issues
    # Model the real boot order: the durable issue registry can load an old
    # issue after this config entry's initial reconciliation has already run.
    ir.async_create_issue(
        hass,
        DOMAIN,
        stale_issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="ftp_transport_unavailable",
        translation_placeholders={"service_id": SERVICE_ID, "service_name": "Harness Server"},
    )
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    assert (DOMAIN, stale_issue_id) not in ir.async_get(hass).issues


@pytest.mark.asyncio
async def test_real_remove_and_readd_cleans_and_recreates_registry(hass, enable_custom_integrations) -> None:
    fake_client = FakeNitradoClient()
    entry = _entry()
    await _setup(hass, entry, fake_client)
    unique_id = f"service:{SERVICE_ID}:auto_shutdown"
    assert _entity_id(hass, unique_id)
    orphan_issue_id = f"{entry.entry_id}_orphaned_test_issue"
    ir.async_create_issue(
        hass,
        DOMAIN,
        orphan_issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="missing_service",
        translation_placeholders={"service_name": "Harness Server"},
    )
    assert (DOMAIN, orphan_issue_id) in ir.async_get(hass).issues

    assert await hass.config_entries.async_remove(entry.entry_id) == {"require_restart": False}
    await hass.async_block_till_done()
    assert er.async_get(hass).async_get_entity_id("switch", DOMAIN, unique_id) is None
    assert (DOMAIN, orphan_issue_id) not in ir.async_get(hass).issues

    replacement = _entry()
    await _setup(hass, replacement, fake_client)
    assert _entity_id(hass, unique_id)


@pytest.mark.asyncio
async def test_permanent_removal_purges_exact_local_state_and_preserves_sibling(
    hass,
    enable_custom_integrations,
) -> None:
    from homeassistant.helpers.storage import Store

    fake_client = FakeNitradoClient()
    entry = _entry()
    await _setup(hass, entry, fake_client)
    sibling_id = "sibling-entry"
    keys = (
        "filesystem_journal",
        "operation_journal",
        "editable_file_history",
        "native_backup_journal",
        "native_backup_journal_quarantine",
    )
    for suffix in keys:
        await Store(hass, 1, f"{DOMAIN}.{suffix}.{entry.entry_id}").async_save({"owner": entry.entry_id})
        await Store(hass, 1, f"{DOMAIN}.{suffix}.{sibling_id}").async_save({"owner": sibling_id})
    blobs = HomeAssistantRecoveryBlobStore(hass, entry.entry_id)
    sibling_blobs = HomeAssistantRecoveryBlobStore(hass, sibling_id)
    await blobs.async_remove_all()
    await sibling_blobs.async_remove_all()
    await blobs.async_write("recovery-owned", b"owned secret")
    await sibling_blobs.async_write("recovery-sibling", b"preserved")
    entry_option_update_lock(hass, entry.entry_id)
    cockpit_state(hass).account_epochs[entry.entry_id] = 7
    cockpit_state(hass).account_epochs[sibling_id] = 9

    assert await hass.config_entries.async_remove(entry.entry_id) == {"require_restart": False}
    await hass.async_block_till_done()

    for suffix in keys:
        assert await Store(hass, 1, f"{DOMAIN}.{suffix}.{entry.entry_id}").async_load() is None
        assert await Store(hass, 1, f"{DOMAIN}.{suffix}.{sibling_id}").async_load() == {"owner": sibling_id}
    assert not Path(hass.config.path(".storage", DOMAIN, "recovery", entry.entry_id)).exists()
    assert await sibling_blobs.async_read("recovery-sibling") == b"preserved"
    assert entry.entry_id not in hass.data.get("nitrado_gameserver_entry_option_locks", {})
    assert entry.entry_id not in cockpit_state(hass).account_epochs
    assert cockpit_state(hass).account_epochs[sibling_id] == 9

    replacement = _entry()
    await _setup(hass, replacement, fake_client)
    assert _entity_id(hass, f"service:{SERVICE_ID}:auto_shutdown")


@pytest.mark.asyncio
async def test_real_unload_cancels_pending_shutdown(hass, enable_custom_integrations) -> None:
    fake_client = FakeNitradoClient(status="started")
    entry = _entry()
    await _setup(hass, entry, fake_client)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    runtime = coordinator.get_runtime(SERVICE_ID)
    runtime.startup_cooldown_started_at = None
    runtime.idle_started_at = coordinator.now_fn() - 3600

    refresh_task = asyncio.create_task(coordinator.async_refresh_service(SERVICE_ID))
    for _ in range(100):
        if runtime.shutdown_pending:
            break
        await asyncio.sleep(0)
    assert runtime.shutdown_pending

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    await asyncio.gather(refresh_task, return_exceptions=True)
    assert fake_client.stop_calls == []


@pytest.mark.asyncio
async def test_real_unload_drains_active_editable_file_mutation(hass, enable_custom_integrations) -> None:
    fake_client = BlockingUploadClient()
    entry = _entry()
    await _setup(hass, entry, fake_client)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    settings_path = "/games/ni900_001/ftproot/game/settings.ini"
    original = fake_client.files[settings_path]

    mutation = asyncio.create_task(
        coordinator.async_apply_editable_file(
            SERVICE_ID,
            "settings",
            'AdminPassword="new-secret"',
        )
    )
    try:
        await asyncio.wait_for(fake_client.upload_started.wait(), timeout=2)
    except TimeoutError as err:
        if mutation.done():
            await mutation
        raise AssertionError("editable mutation did not reach the target upload boundary") from err

    unload = asyncio.create_task(hass.config_entries.async_unload(entry.entry_id))
    await asyncio.sleep(0)
    assert not unload.done()
    fake_client.release_upload.set()
    assert await unload
    await hass.async_block_till_done()
    result = await mutation

    assert result.wrote
    assert fake_client.files[settings_path] != original


@pytest.mark.asyncio
async def test_periodic_auth_failure_starts_reauth_flow(hass, enable_custom_integrations) -> None:
    entry = _entry()
    fake_client = FakeNitradoClient()
    scheduled: list[tuple[object, timedelta]] = []

    def capture_interval(_hass, callback, interval):
        scheduled.append((callback, interval))
        return lambda: None

    with patch("homeassistant.helpers.event.async_track_time_interval", side_effect=capture_interval):
        await _setup(hass, entry, fake_client)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    can_stop_entity_id = _entity_id(
        hass,
        f"service:{SERVICE_ID}:can_stop",
        platform="binary_sensor",
    )
    assert hass.states.get(can_stop_entity_id).state != STATE_UNAVAILABLE
    with (
        patch.object(fake_client, "fetch_server", new=AsyncMock(side_effect=NitradoAuthError("expired token"))),
        patch.object(entry, "async_start_reauth_if_available") as start_reauth,
    ):
        status_callback = next(
            callback
            for callback, interval in scheduled
            if interval == timedelta(seconds=entry.options[CONF_STATUS_INTERVAL])
        )
        await status_callback(dt_util.utcnow())

    start_reauth.assert_called_once_with(hass)
    assert coordinator.get_runtime(SERVICE_ID).status_fresh is False
    assert hass.states.get(can_stop_entity_id).state == STATE_UNAVAILABLE


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.asyncio
async def test_periodic_endpoint_auth_failure_with_successful_probe_does_not_reauth(
    hass,
    enable_custom_integrations,
    status,
) -> None:
    entry = _entry()
    fake_client = FakeNitradoClient()
    scheduled: list[tuple[object, timedelta]] = []

    def capture_interval(_hass, callback, interval):
        scheduled.append((callback, interval))
        return lambda: None

    with patch("homeassistant.helpers.event.async_track_time_interval", side_effect=capture_interval):
        await _setup(hass, entry, fake_client)

    failure = NitradoRequestFailure(
        reason="endpoint_authorization_failure",
        request=NitradoRequestContext(
            method="GET",
            path="/services/{service_id}/gameservers",
            service_id=SERVICE_ID,
            status=status,
            provider_request_id="provider-request-123",
            response_summary='{"message":"operation unavailable"}',
        ),
        token_probe=NitradoRequestContext(method="GET", path="/services", status=200),
    )
    endpoint_error = NitradoEndpointAuthorizationError(
        "endpoint_authorization_failure: account token remains valid",
        failure=failure,
    )
    fake_client.last_request_failure = failure
    coordinator = hass.data[DOMAIN][entry.entry_id]

    with (
        patch.object(fake_client, "fetch_server", new=AsyncMock(side_effect=endpoint_error)),
        patch.object(entry, "async_start_reauth_if_available") as start_reauth,
    ):
        status_callback = next(
            callback
            for callback, interval in scheduled
            if interval == timedelta(seconds=entry.options[CONF_STATUS_INTERVAL])
        )
        await status_callback(dt_util.utcnow())

    start_reauth.assert_not_called()
    assert entry.state.name == "LOADED"
    assert coordinator.get_runtime(SERVICE_ID).status_fresh is False
    assert "endpoint_authorization_failure" in coordinator.last_refresh_error
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["runtime"]["last_request_failure"]["reason"] == "endpoint_authorization_failure"
    assert diagnostics["runtime"]["last_request_failure"]["request"]["status"] == status


@pytest.mark.asyncio
async def test_verified_discovery_clears_stale_reauth_flow_and_issue(
    hass,
    enable_custom_integrations,
) -> None:
    entry = _entry()
    fake_client = FakeNitradoClient()
    scheduled: list[tuple[object, timedelta]] = []

    def capture_interval(_hass, callback, interval):
        scheduled.append((callback, interval))
        return lambda: None

    with patch("homeassistant.helpers.event.async_track_time_interval", side_effect=capture_interval):
        await _setup(hass, entry, fake_client)

    entry.async_start_reauth_if_available(hass)
    await hass.async_block_till_done()
    issue_id = f"config_entry_reauth_{DOMAIN}_{entry.entry_id}"
    assert ir.async_get(hass).async_get_issue("homeassistant", issue_id) is not None
    assert tuple(entry.async_get_active_flows(hass, {SOURCE_REAUTH}))

    discovery_callback = next(
        callback
        for callback, interval in scheduled
        if interval == timedelta(seconds=entry.options[CONF_DISCOVERY_INTERVAL])
    )
    await discovery_callback(dt_util.utcnow())
    await hass.async_block_till_done()

    assert entry.state.name == "LOADED"
    assert not tuple(entry.async_get_active_flows(hass, {SOURCE_REAUTH}))
    assert ir.async_get(hass).async_get_issue("homeassistant", issue_id) is None


@pytest.mark.asyncio
async def test_verified_setup_clears_orphaned_stale_reauth_issue(
    hass,
    enable_custom_integrations,
) -> None:
    entry = _entry()
    issue_id = f"config_entry_reauth_{DOMAIN}_{entry.entry_id}"
    ir.async_create_issue(
        hass,
        "homeassistant",
        issue_id,
        is_fixable=False,
        issue_domain=DOMAIN,
        severity=ir.IssueSeverity.ERROR,
        translation_key="config_entry_reauth",
        translation_placeholders={"name": entry.title},
    )
    assert ir.async_get(hass).async_get_issue("homeassistant", issue_id) is not None

    await _setup(hass, entry, FakeNitradoClient())

    assert entry.state.name == "LOADED"
    assert ir.async_get(hass).async_get_issue("homeassistant", issue_id) is None


@pytest.mark.asyncio
async def test_discovery_auth_failure_stales_entities_and_starts_reauth(
    hass,
    enable_custom_integrations,
) -> None:
    options = _options()
    options[CONF_DISCOVERY_INTERVAL] = 60
    options[CONF_STATUS_INTERVAL] = 600
    entry = _entry(options=options)
    fake_client = FakeNitradoClient()
    scheduled: list[tuple[object, timedelta]] = []

    def capture_interval(_hass, callback, interval):
        scheduled.append((callback, interval))
        return lambda: None

    with patch("homeassistant.helpers.event.async_track_time_interval", side_effect=capture_interval):
        await _setup(hass, entry, fake_client)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    runtime = coordinator.get_runtime(SERVICE_ID)
    runtime.idle_started_at = 100
    runtime.shutdown_pending = True
    can_stop_entity_id = _entity_id(
        hass,
        f"service:{SERVICE_ID}:can_stop",
        platform="binary_sensor",
    )
    assert hass.states.get(can_stop_entity_id).state != STATE_UNAVAILABLE

    with (
        patch.object(fake_client, "service_list", new=AsyncMock(side_effect=NitradoAuthError("expired token"))),
        patch.object(entry, "async_start_reauth_if_available") as start_reauth,
    ):
        discovery_callback = next(
            callback
            for callback, interval in scheduled
            if interval == timedelta(seconds=entry.options[CONF_DISCOVERY_INTERVAL])
        )
        await discovery_callback(dt_util.utcnow())

    start_reauth.assert_called_once_with(hass)
    assert runtime.status_fresh is False
    assert runtime.idle_started_at is None
    assert runtime.shutdown_pending is False
    assert hass.states.get(can_stop_entity_id).state == STATE_UNAVAILABLE


@pytest.mark.asyncio
async def test_admin_can_durably_stop_managing_active_service(hass, enable_custom_integrations) -> None:
    entry = _entry()
    fake_client = FakeNitradoClient()
    await _setup(hass, entry, fake_client)
    unique_id = f"service:{SERVICE_ID}:auto_shutdown"
    assert _entity_id(hass, unique_id)

    with _patched_client(fake_client):
        await hass.services.async_call(
            DOMAIN,
            "ignore_service",
            {"service_id": SERVICE_ID},
            blocking=True,
        )
        await hass.async_block_till_done()

    assert entry.options[CONF_IMPORTED_SERVICE_IDS] == []
    assert entry.options["ignored_service_ids"] == [SERVICE_ID]
    assert entry.options[CONF_IDLE_SHUTDOWN_SERVICE_IDS] == []
    assert er.async_get(hass).async_get_entity_id("switch", DOMAIN, unique_id) is None
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert SERVICE_ID not in coordinator.services
