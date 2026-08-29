"""Tests for generic profile extension HTTP view helpers."""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from custom_components.nitrado_gameserver.api.nitrado import NitradoApiError, NitradoWebinterfaceLogin
from custom_components.nitrado_gameserver.const import (
    CONF_ALLOW_PLAINTEXT_FTP_SERVICE_IDS,
    DOMAIN,
)
from custom_components.nitrado_gameserver.coordinator import AccountCoordinatorOptions
from custom_components.nitrado_gameserver.plugins.base import (
    COCKPIT_API_VERSION,
    SUPPORTED,
    ActionDeclaration,
    CapabilityState,
    CapabilityVerdict,
    CockpitDeclaration,
    CockpitSnapshotContext,
    EditableFileDeclaration,
    EntityDeclaration,
    ExtensionAccess,
    LifecycleEvent,
    LifecycleHookDeclaration,
    MatchResult,
    ProfileOptionDeclaration,
    ProfileOptionType,
    ResourceDeclaration,
    SurfaceDeclaration,
    ValidatorDeclaration,
    ValidatorDomain,
    ValidatorTarget,
    blocked,
)
from custom_components.nitrado_gameserver.plugins.palworld import PalworldProfile
from custom_components.nitrado_gameserver.service_options import entry_option_update_lock
from custom_components.nitrado_gameserver.views import (
    FilesystemSettingsView,
    NitradoWebinterfaceLoginView,
    ProfileActionView,
    ProfileEditableFileApplyView,
    ProfileEditableFileReadView,
    ProfileExtensionServicesView,
    ProfileExtensionsView,
    _account_title,
    _cockpit_compatibility,
    _editable_snapshot_payload,
    _filesystem_status_payload,
    _find_coordinator,
    _find_entry_coordinator,
    _find_exact_coordinator,
    _http_blocked,
    _json_body,
    _json_safe,
    _manifest_payload,
    _optional_bool,
    _optional_extension_payload,
    _public_profile_option_values,
    _required_edit_value,
    async_register_extension_views,
)


def async_callback(callback):
    """Wrap a compact test callback in the required async mutation contract."""

    async def wrapped(*args, **kwargs):
        return callback(*args, **kwargs)

    return wrapped


class FakeHttp:
    """Small HA HTTP fixture."""

    def __init__(self) -> None:
        self.views: list[object] = []

    def register_view(self, view: object) -> None:
        self.views.append(view)


class FakeHass:
    """Small HA fixture."""

    def __init__(self) -> None:
        self.data = {DOMAIN: {}}
        self.http = FakeHttp()


class FakeConfigEntry:
    """Small mutable config-entry fixture."""

    def __init__(self, entry_id: str = "entry", title: str = "Nitrado account") -> None:
        self.entry_id = entry_id
        self.title = title
        self.options: dict[str, object] = {}


class FakeConfigEntries:
    """Small config-entry manager fixture."""

    def __init__(self, entry: FakeConfigEntry) -> None:
        self.entry = entry
        self.updates: list[dict[str, object]] = []

    def async_get_entry(self, entry_id: str) -> FakeConfigEntry | None:
        return self.entry if entry_id == self.entry.entry_id else None

    def async_update_entry(self, entry: FakeConfigEntry, *, options: dict[str, object]) -> None:
        entry.options = options
        self.updates.append(options)


class FakeRequest:
    """Tiny aiohttp request double."""

    def __init__(
        self,
        body: object | Exception,
        hass: object | None = None,
        *,
        is_admin: bool = True,
    ) -> None:
        self._body = body
        self.app = {"hass": hass or FakeHass()}
        self._user = FakeUser(is_admin=is_admin)

    def get(self, key: str, default: object | None = None) -> object | None:
        """Return aiohttp-style request metadata."""

        if key == "hass_user":
            return self._user
        return default

    async def json(self) -> object:
        """Return or raise the configured JSON body."""

        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def response_payload(value: object) -> dict[str, object]:
    """Decode either a direct fallback value or an aiohttp response/error."""

    if isinstance(value, dict):
        return value
    text = getattr(value, "text", None)
    if not isinstance(text, str):
        text = str(value)
    return json.loads(text)


@dataclass(slots=True)
class FakeUser:
    """Minimal authenticated Home Assistant user fixture."""

    is_admin: bool


class FakeCoordinator:
    """Coordinator double for mutation-route guard tests."""

    def __init__(self) -> None:
        self.runtime = FakeRuntime()
        self.services = {"123": self.runtime}
        self.apply_calls = 0
        self.read_calls = 0
        self.read_error: Exception | None = None
        self.descriptor_error: Exception | None = None

    def get_runtime(self, service_id: str) -> object:
        """Return a fake runtime."""

        return self.services[service_id]

    def profile_dispatch_token(self, service_id: str) -> tuple[int, str, int, int, int]:
        """Return a stable fake authorization snapshot."""

        return (id(self.services[service_id]), "", 0, 0, 0)

    def profile_resource_descriptors(self, service_id: str) -> tuple:
        """Return fake resource descriptors."""

        if self.descriptor_error is not None:
            raise self.descriptor_error
        return ()

    def profile_surface_descriptors(self, service_id: str) -> tuple:
        """Return fake surface descriptors."""

        if self.descriptor_error is not None:
            raise self.descriptor_error
        return ()

    async def async_read_editable_file(self, *args: object, **kwargs: object) -> object:
        """Raise configured read errors."""

        if self.read_error is not None:
            raise self.read_error
        self.read_calls += 1
        return SimpleNamespace(
            key="settings",
            name="Settings",
            path="/game/settings.ini",
            text="password=secret",
            redacted_text="password=***",
            parsed={"password": "secret"},
            requires_restart=True,
        )

    async def async_apply_editable_file(self, *args: object, **kwargs: object) -> object:
        """Record that the mutating backend would have been called."""

        self.apply_calls += 1
        return object()


class FakeRuntime:
    """Runtime double with an optional selected profile."""

    def __init__(self) -> None:
        self.profile = None


class ExtensionProfile:
    """Profile fixture for descriptor payload tests."""

    profile_id = "fixture"
    name = "Fixture"
    supported_games = ("fixture",)
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
        return (EntityDeclaration(platform="select", key="mode", name="Mode"),)

    def editable_files(self):
        return (
            EditableFileDeclaration(
                key="settings",
                name="Settings",
                path_fn=lambda context: "/game/settings.ini",
                requires_stopped=True,
            ),
        )

    def resources(self):
        return (
            ResourceDeclaration(key="visual", name="Visual", content_type="image/png", fetch_fn=lambda context: b""),
        )

    def actions(self):
        return (
            ActionDeclaration(
                key="danger",
                name="Danger",
                action_fn=async_callback(lambda context: {"ok": True}),
                requires_confirmation=True,
            ),
        )

    def surfaces(self):
        return (
            SurfaceDeclaration(
                key="panel",
                name="Panel",
                resources=("visual",),
                actions=("danger",),
                controls=("mode",),
                editable_files=("settings",),
            ),
        )

    def lifecycle_hooks(self):
        return (
            LifecycleHookDeclaration(
                key="before_start",
                event=LifecycleEvent.BEFORE_START,
                hook_fn=async_callback(lambda context: SUPPORTED),
            ),
        )

    def validators(self):
        return (
            ValidatorDeclaration(
                key="ok",
                name="OK",
                target=ValidatorTarget.ACTION,
                domains=(ValidatorDomain.SERVER_STATE,),
                validate_fn=lambda context, payload=None: SUPPORTED,
            ),
        )


@dataclass(slots=True, frozen=True)
class Payload:
    """Fixture dataclass for JSON helper tests."""

    verdict: CapabilityVerdict
    raw: bytes


class ViewTests(unittest.TestCase):
    """HTTP view helper tests."""

    def test_register_extension_views_once_without_polluting_domain_coordinators(self) -> None:
        hass = FakeHass()

        async_register_extension_views(hass)
        async_register_extension_views(hass)

        self.assertEqual(len(hass.http.views), 21)
        self.assertEqual(hass.data[DOMAIN], {})

    def test_account_title_uses_config_entry_title_with_entry_id_fallback(self) -> None:
        titled = FakeConfigEntry("entry-a", "Family servers")
        hass = FakeHass()
        hass.config_entries = FakeConfigEntries(titled)

        self.assertEqual(_account_title(hass, "entry-a"), "Family servers")
        self.assertEqual(_account_title(hass, "missing-entry"), "missing-entry")

        titled.title = "  "
        self.assertEqual(_account_title(hass, "entry-a"), "entry-a")

    def test_extension_services_payload_exposes_account_title(self) -> None:
        async def run() -> None:
            entry = FakeConfigEntry("entry-a", "Family servers")
            coordinator = FakeCoordinator()
            coordinator.account_entry_id = entry.entry_id
            coordinator.options = SimpleNamespace(service_display_names={})
            hass = FakeHass()
            hass.config_entries = FakeConfigEntries(entry)
            hass.data[DOMAIN] = {entry.entry_id: coordinator}
            request = FakeRequest({}, hass)

            with (
                patch("custom_components.nitrado_gameserver.views._manifest_payload", return_value={}),
                patch("custom_components.nitrado_gameserver.views._core_entity_ids_payload", return_value={}),
                patch("custom_components.nitrado_gameserver.views._filesystem_status_payload", return_value={}),
                patch("custom_components.nitrado_gameserver.views._surface_descriptors_payload", return_value=[]),
                patch("custom_components.nitrado_gameserver.views._public_profile_option_values", return_value={}),
                patch("custom_components.nitrado_gameserver.views.service_display_name", return_value="Palworld"),
            ):
                body = response_payload(await ProfileExtensionServicesView().get(request))

            self.assertEqual(body["services"][0]["account_entry_id"], "entry-a")
            self.assertEqual(body["services"][0]["account_title"], "Family servers")

        asyncio.run(run())

    def test_cockpit_identity_never_falls_through_duplicate_service_ids(self) -> None:
        hass = FakeHass()
        first = SimpleNamespace(services={"123": object()})
        second = SimpleNamespace(services={"123": object()})
        hass.data[DOMAIN] = {"entry-a": first, "entry-b": second}

        self.assertIs(_find_exact_coordinator(hass, "entry-a", "123"), first)
        self.assertIs(_find_exact_coordinator(hass, "entry-b", "123"), second)
        with self.assertRaises(Exception):
            _find_coordinator(hass, "123")
        with self.assertRaises(Exception):
            _find_entry_coordinator(hass, "123")
        with self.assertRaises(Exception):
            _find_exact_coordinator(hass, "missing", "123")
        with self.assertRaises(Exception):
            _find_exact_coordinator(hass, "entry-a", "999")

    def test_unsupported_cockpit_api_degrades_only_the_host_ui(self) -> None:
        declaration = CockpitDeclaration(
            key="future_cockpit",
            name="Future Cockpit",
            cockpit_api_version=COCKPIT_API_VERSION + 1,
            asset_key="future_cockpit",
            frontend_revision=f"sha256:{'a' * 64}",
            default_route="overview",
            route_keys=("overview",),
        )

        compatible, degraded_code = _cockpit_compatibility(declaration)

        self.assertIsNone(compatible)
        self.assertEqual(degraded_code, "cockpit_api_unsupported")

    def test_webinterface_login_is_admin_only_and_returns_ephemeral_url(self) -> None:
        class LoginClient:
            def __init__(self) -> None:
                self.calls: list[str] = []

            async def webinterface_login(self, service_id: str) -> NitradoWebinterfaceLogin:
                self.calls.append(service_id)
                return NitradoWebinterfaceLogin(
                    url=("https://webinterface.nitrado.net/?access_token=ephemeral&service_id=123&lable=ni"),
                    expires_at=2_000_000_000,
                )

        async def run() -> None:
            hass = FakeHass()
            coordinator = FakeCoordinator()
            coordinator.client = LoginClient()
            hass.data[DOMAIN]["entry"] = coordinator
            view = NitradoWebinterfaceLoginView()

            with self.assertRaises(Exception) as denied:
                await view.post(FakeRequest({}, hass, is_admin=False), "123")
            self.assertEqual(response_payload(denied.exception)["error"]["code"], "forbidden")
            self.assertEqual(coordinator.client.calls, [])

            with self.assertRaises(Exception) as stale:
                await view.post(FakeRequest({}, hass), "123")
            self.assertEqual(response_payload(stale.exception)["error"]["code"], "reload_required")
            self.assertNotIn("access_token", json.dumps(response_payload(stale.exception)))
            self.assertEqual(coordinator.client.calls, [])

        asyncio.run(run())

    def test_json_safe_serializes_dataclasses_enums_and_bytes(self) -> None:
        payload = _json_safe(Payload(CapabilityVerdict(CapabilityState.BLOCKED, reason="no"), b"abc"))

        self.assertEqual(payload["verdict"]["state"], "blocked")
        self.assertEqual(payload["verdict"]["reason"], "no")
        self.assertEqual(payload["raw"]["encoding"], "base64")
        self.assertEqual(payload["raw"]["data"], "YWJj")

    def test_json_safe_preserves_extension_resource_urls(self) -> None:
        payload = _json_safe(
            {
                "url": "https://example.invalid/resource.png",
                "download_url": "https://example.invalid/download.bin",
            }
        )

        self.assertEqual(payload["url"], "https://example.invalid/resource.png")
        self.assertEqual(payload["download_url"], "https://example.invalid/download.bin")

    def test_json_safe_handles_odd_profile_payload_shapes(self) -> None:
        payload = _json_safe(
            {
                ("tuple", "key"): {1, "a"},
                7: "numeric key",
            }
        )

        self.assertEqual(payload["('tuple', 'key')"], ["a", 1])
        self.assertEqual(payload["7"], "numeric key")
        json.dumps(payload)

    def test_extension_payload_must_be_bounded_finite_json(self) -> None:
        self.assertEqual(_optional_extension_payload({"payload": {"mode": "safe"}}), {"mode": "safe"})
        with self.assertRaises(Exception) as invalid_number:
            _optional_extension_payload({"payload": {"value": float("nan")}})
        self.assertIn("finite JSON", response_payload(invalid_number.exception)["error"]["message"])
        with self.assertRaises(Exception) as oversized:
            _optional_extension_payload({"payload": "x" * 70_000})
        self.assertIn("exceeds 65536 bytes", response_payload(oversized.exception)["error"]["message"])

    def test_json_body_rejects_malformed_or_non_object_requests(self) -> None:
        async def run() -> None:
            with self.assertRaises(Exception) as malformed:
                await _json_body(FakeRequest(ValueError("bad")))
            self.assertEqual(
                response_payload(malformed.exception)["error"]["message"], "Request body must be a valid JSON object."
            )

            with self.assertRaises(Exception) as wrong_shape:
                await _json_body(FakeRequest([]))
            self.assertEqual(
                response_payload(wrong_shape.exception)["error"]["message"], "Request body must be a JSON object."
            )

        asyncio.run(run())

    def test_boolean_and_edit_value_helpers_are_strict(self) -> None:
        self.assertTrue(_optional_bool({"confirm": True}, "confirm", default=False))
        with self.assertRaises(Exception):
            _optional_bool({"confirm": "true"}, "confirm", default=False)
        with self.assertRaises(Exception):
            _required_edit_value({}, value_is_parsed=False)
        with self.assertRaises(Exception):
            _required_edit_value({"value": {"bad": "shape"}}, value_is_parsed=False)
        self.assertEqual(_required_edit_value({"value": {"ok": True}}, value_is_parsed=True), {"ok": True})

    def test_blocked_errors_are_structured_json(self) -> None:
        error = _http_blocked(ExceptionWithVerdict(blocked("Nope")))  # type: ignore[arg-type]
        payload = response_payload(error)

        self.assertEqual(payload["error"]["message"], "Nope")
        self.assertEqual(payload["error"]["verdict"]["state"], "blocked")

    def test_manifest_payload_exposes_full_extension_metadata(self) -> None:
        manifest = _json_safe(_manifest_payload(SimpleNamespace(profile=ExtensionProfile(), profile_manifest=None)))

        self.assertEqual(manifest["entities"][0]["key"], "mode")
        self.assertEqual(manifest["editable_files"][0]["key"], "settings")
        self.assertTrue(manifest["editable_files"][0]["requires_stopped"])
        self.assertEqual(manifest["editable_files"][0]["access"], "admin")
        self.assertEqual(manifest["resources"][0]["key"], "visual")
        self.assertEqual(manifest["resources"][0]["content_family"], "image")
        self.assertTrue(manifest["actions"][0]["requires_confirmation"])
        self.assertEqual(manifest["actions"][0]["access"], "admin")
        self.assertEqual(manifest["surfaces"][0]["editable_files"], ["settings"])
        self.assertEqual(manifest["lifecycle_hooks"][0]["event"], "before_start")
        self.assertEqual(manifest["validators"][0]["target"], "action")
        self.assertEqual(manifest["validators"][0]["domains"], ["server_state"])

    def test_manifest_payload_handles_missing_profile(self) -> None:
        manifest = _manifest_payload(None)

        self.assertEqual(manifest["entities"], [])
        self.assertEqual(manifest["editable_files"], [])
        self.assertEqual(manifest["resources"], [])
        self.assertEqual(manifest["actions"], [])
        self.assertEqual(manifest["surfaces"], [])
        self.assertEqual(manifest["lifecycle_hooks"], [])
        self.assertEqual(manifest["validators"], [])

    def test_public_profile_option_values_never_expose_secret_text(self) -> None:
        class SecretProfile(ExtensionProfile):
            def profile_options(self):
                return (
                    ProfileOptionDeclaration("mode", "Mode", ProfileOptionType.TEXT, default="safe"),
                    ProfileOptionDeclaration("password", "Password", ProfileOptionType.SECRET, default=""),
                )

        runtime = SimpleNamespace(
            profile=SecretProfile(),
            profile_manifest=None,
            extra={"_persisted_profile_options": {"mode": "fast", "password": "do-not-return"}},
        )

        payload = _public_profile_option_values(runtime)

        self.assertEqual(payload["profile_option_values"], {"mode": "fast"})
        self.assertEqual(payload["profile_option_configured"], {"password": True})
        self.assertNotIn("do-not-return", json.dumps(payload))

    def test_player_reporting_contract_preserves_unresolved_and_legacy_disabled_truth(self) -> None:
        runtime = SimpleNamespace(
            profile=PalworldProfile(),
            profile_manifest=None,
            state=SimpleNamespace(identity=SimpleNamespace(service_id="123")),
            server=SimpleNamespace(
                raw_status="stopped",
                query_valid=False,
                player_source="server_status",
                address="127.0.0.1:8211",
            ),
            status_fresh=True,
            using_cached_data=False,
        )

        unresolved = PalworldProfile().cockpit_snapshot(
            CockpitSnapshotContext(
                service_id="123",
                server=runtime.server,
                status_fresh=True,
                using_cached_data=False,
            )
        )["player_reporting"]
        self.assertEqual(unresolved["consent"]["status"], "unresolved")
        self.assertEqual(unresolved["verification"]["status"], "not_testable")

        legacy_disabled = PalworldProfile().cockpit_snapshot(
            CockpitSnapshotContext(
                service_id="123",
                server=runtime.server,
                status_fresh=True,
                using_cached_data=False,
                public_options={"allow_insecure_rest": False},
                configured_options=frozenset({"allow_insecure_rest"}),
            )
        )["player_reporting"]
        self.assertEqual(legacy_disabled["consent"]["status"], "disabled")
        self.assertEqual(legacy_disabled["verification"]["status"], "not_testable")

    def test_player_reporting_contract_separates_stopped_zero_from_rest_verification(self) -> None:
        runtime = SimpleNamespace(
            profile=PalworldProfile(),
            profile_manifest=None,
            state=SimpleNamespace(identity=SimpleNamespace(service_id="123")),
            server=SimpleNamespace(
                raw_status="stopped",
                query_valid=False,
                player_source="server_status",
                address="127.0.0.1:8211",
            ),
            status_fresh=True,
            using_cached_data=False,
        )

        def reporting() -> dict[str, object]:
            return PalworldProfile().cockpit_snapshot(
                CockpitSnapshotContext(
                    service_id="123",
                    server=runtime.server,
                    status_fresh=runtime.status_fresh,
                    using_cached_data=runtime.using_cached_data,
                    public_options={"allow_insecure_rest": True},
                    configured_options=frozenset({"allow_insecure_rest"}),
                    option_acknowledgements={"allow_insecure_rest": 1},
                )
            )["player_reporting"]

        stopped = reporting()
        self.assertEqual(stopped["consent"]["status"], "enabled")
        self.assertEqual(stopped["verification"]["status"], "not_testable")

        runtime.server.raw_status = "started"
        running_unverified = reporting()
        self.assertEqual(running_unverified["verification"]["status"], "unverified")

        runtime.server.query_valid = True
        runtime.server.player_source = "palworld_rest"
        verified = reporting()
        self.assertEqual(verified["verification"]["status"], "verified")

        runtime.status_fresh = False
        unavailable = reporting()
        self.assertEqual(unavailable["verification"]["status"], "unavailable")

    def test_player_reporting_contract_fails_malformed_truth_closed_and_ignores_other_profiles(self) -> None:
        malformed = PalworldProfile().cockpit_snapshot(
            CockpitSnapshotContext(
                service_id="123",
                server=None,
                status_fresh=False,
                using_cached_data=False,
                public_options={"allow_insecure_rest": "yes"},
                configured_options=frozenset({"allow_insecure_rest"}),
            )
        )["player_reporting"]
        self.assertEqual(malformed["consent"]["status"], "unavailable")

        class RevisionTwoPalworld(PalworldProfile):
            def profile_options(self):
                option = super().profile_options()[0]
                return (
                    ProfileOptionDeclaration(
                        key=option.key,
                        name=option.name,
                        option_type=option.option_type,
                        description=option.description,
                        default=option.default,
                        standard_options=option.standard_options,
                        onboarding=option.onboarding,
                        confirmation_required=option.confirmation_required,
                        acknowledgement_revision=2,
                        idle_shutdown_required=option.idle_shutdown_required,
                        repair_if_unacknowledged=option.repair_if_unacknowledged,
                    ),
                )

        self.assertEqual(RevisionTwoPalworld().profile_options()[0].acknowledgement_revision, 2)

    def test_filesystem_status_is_cached_whitelisted_and_secret_free(self) -> None:
        class Filesystem:
            def observed_status(self, service_id: str) -> dict[str, object]:
                self.service_id = service_id
                return {
                    "http": {
                        "root_mapping_proven": True,
                        "root_prefix": "/games/private/account-root",
                    },
                    "ftp": {
                        "observed_transport": "ftps",
                        "secure_transport_observed": True,
                        "active_operations": 2,
                        "closed": False,
                        "host": "private.example.invalid",
                        "username": "ftp-user",
                        "password": "do-not-return",
                    },
                }

        coordinator = SimpleNamespace(
            filesystem=Filesystem(),
            options=AccountCoordinatorOptions(
                allow_plaintext_ftp_service_ids=frozenset({"123"}),
            ),
        )

        payload = _filesystem_status_payload(
            coordinator,
            "123",
            {"editable_files": []},
        )

        self.assertEqual(
            payload,
            {
                "relevant": True,
                "allow_plaintext_ftp": True,
                "http_root_mapping_proven": True,
                "observed_transport": "ftps",
                "secure_transport_observed": True,
                "plaintext_consent_visible": True,
                "plaintext_required": False,
                "active_operations": 2,
                "closed": False,
            },
        )
        serialized = json.dumps(payload)
        self.assertNotIn("private", serialized)
        self.assertNotIn("ftp-user", serialized)
        self.assertNotIn("do-not-return", serialized)

    def test_filesystem_status_is_hidden_until_relevant(self) -> None:
        filesystem = SimpleNamespace(
            observed_status=lambda service_id: {
                "http": {"root_mapping_proven": False},
                "ftp": {
                    "observed_transport": None,
                    "active_operations": 0,
                    "closed": False,
                },
            }
        )
        coordinator = SimpleNamespace(
            filesystem=filesystem,
            options=AccountCoordinatorOptions(),
        )

        hidden = _filesystem_status_payload(coordinator, "123", {"editable_files": []})
        shown = _filesystem_status_payload(coordinator, "123", {"editable_files": [{"key": "settings"}]})

        self.assertFalse(hidden["relevant"])
        self.assertTrue(shown["relevant"])
        self.assertFalse(shown["plaintext_consent_visible"])

    def test_plaintext_consent_is_visible_only_when_required_observed_or_enabled(self) -> None:
        def payload(*, transport=None, required=False, consent=False):
            coordinator = SimpleNamespace(
                filesystem=SimpleNamespace(
                    observed_status=lambda service_id: {
                        "http": {"root_mapping_proven": False},
                        "ftp": {
                            "observed_transport": transport,
                            "plaintext_required": required,
                        },
                    }
                ),
                options=AccountCoordinatorOptions(
                    allow_plaintext_ftp_service_ids=frozenset({"123"}) if consent else frozenset(),
                ),
            )
            return _filesystem_status_payload(
                coordinator,
                "123",
                {"editable_files": [{"key": "settings"}]},
            )

        self.assertFalse(payload(transport="ftps")["plaintext_consent_visible"])
        self.assertTrue(payload(required=True)["plaintext_consent_visible"])
        self.assertTrue(payload(transport="ftp")["plaintext_consent_visible"])
        self.assertTrue(payload(consent=True)["plaintext_consent_visible"])

    def test_plaintext_ftp_consent_is_admin_only_persisted_and_applied_live(self) -> None:
        async def run() -> None:
            class Filesystem:
                def __init__(self) -> None:
                    self.updates: list[set[str]] = []

                def update_plaintext_ftp_consent(self, service_ids: set[str]) -> None:
                    self.updates.append(service_ids)

            hass = FakeHass()
            entry = FakeConfigEntry()
            hass.config_entries = FakeConfigEntries(entry)
            filesystem = Filesystem()

            @asynccontextmanager
            async def operation(service_id, intent):
                del service_id, intent
                self.assertTrue(entry_option_update_lock(hass, entry.entry_id).locked())

                class Reservation:
                    async def async_mark_dispatched(self) -> None:
                        return None

                    async def async_mark_verifying(self) -> None:
                        return None

                yield Reservation()

            coordinator = SimpleNamespace(
                services={"123": FakeRuntime()},
                options=AccountCoordinatorOptions(),
                filesystem=filesystem,
                async_operation=operation,
            )
            hass.data[DOMAIN][entry.entry_id] = coordinator
            view = FilesystemSettingsView()

            with self.assertRaises(Exception) as denied:
                await view.post(
                    FakeRequest({"allow_plaintext_ftp": True}, hass, is_admin=False),
                    "123",
                )
            self.assertEqual(response_payload(denied.exception)["error"]["code"], "forbidden")
            self.assertEqual(hass.config_entries.updates, [])

            enabled = await view.post(
                FakeRequest({"allow_plaintext_ftp": True}, hass),
                "123",
            )
            self.assertEqual(response_payload(enabled), {"allow_plaintext_ftp": True})
            self.assertEqual(entry.options[CONF_ALLOW_PLAINTEXT_FTP_SERVICE_IDS], ["123"])
            self.assertEqual(coordinator.options.allow_plaintext_ftp_service_ids, frozenset({"123"}))
            self.assertEqual(filesystem.updates[-1], {"123"})

            disabled = await view.post(
                FakeRequest({"allow_plaintext_ftp": False}, hass),
                "123",
            )
            self.assertEqual(response_payload(disabled), {"allow_plaintext_ftp": False})
            self.assertEqual(entry.options[CONF_ALLOW_PLAINTEXT_FTP_SERVICE_IDS], [])
            self.assertEqual(filesystem.updates[-1], set())

        asyncio.run(run())

    def test_plaintext_ftp_consent_requires_a_real_boolean(self) -> None:
        async def run() -> None:
            hass = FakeHass()
            coordinator = SimpleNamespace(services={"123": FakeRuntime()})
            hass.data[DOMAIN]["entry"] = coordinator

            with self.assertRaises(Exception) as caught:
                await FilesystemSettingsView().post(
                    FakeRequest({"allow_plaintext_ftp": "true"}, hass),
                    "123",
                )
            self.assertIn("must be a boolean", response_payload(caught.exception)["error"]["message"])
            with self.assertRaises(Exception) as missing:
                await FilesystemSettingsView().post(FakeRequest({}, hass), "123")
            self.assertIn("is required", response_payload(missing.exception)["error"]["message"])

        asyncio.run(run())

    def test_manifest_payload_reports_invalid_profile_contracts(self) -> None:
        class InvalidProfile(ExtensionProfile):
            def actions(self):
                return (
                    ActionDeclaration("duplicate", "Duplicate", action_fn=async_callback(lambda context: None)),
                    ActionDeclaration("duplicate", "Duplicate Again", action_fn=async_callback(lambda context: None)),
                )

        manifest = _manifest_payload(SimpleNamespace(profile=InvalidProfile(), profile_manifest=None))

        self.assertTrue(manifest["invalid"])
        self.assertIn("details were logged", manifest["error"])
        self.assertEqual(manifest["actions"], [])

    def test_descriptor_route_handles_missing_profile(self) -> None:
        async def run() -> None:
            coordinator = FakeCoordinator()
            hass = FakeHass()
            hass.data[DOMAIN]["entry"] = coordinator
            payload = response_payload(await ProfileExtensionsView().get(FakeRequest({}, hass), "123"))

            self.assertEqual(payload["profile"], {"profile_id": None, "name": None})
            self.assertEqual(payload["manifest"]["actions"], [])

        asyncio.run(run())

    def test_descriptor_route_reports_invalid_manifest_without_descriptor_dispatch(self) -> None:
        class InvalidProfile(ExtensionProfile):
            def actions(self):
                return (
                    ActionDeclaration("duplicate", "Duplicate", action_fn=async_callback(lambda context: None)),
                    ActionDeclaration("duplicate", "Duplicate Again", action_fn=async_callback(lambda context: None)),
                )

        async def run() -> None:
            coordinator = FakeCoordinator()
            coordinator.runtime.profile = InvalidProfile()
            coordinator.descriptor_error = AssertionError("descriptor dispatch should not run")
            hass = FakeHass()
            hass.data[DOMAIN]["entry"] = coordinator
            payload = response_payload(await ProfileExtensionsView().get(FakeRequest({}, hass), "123"))

            self.assertTrue(payload["manifest"]["invalid"])
            self.assertIn("details were logged", payload["manifest"]["error"])
            self.assertEqual(payload["resources"], [])
            self.assertEqual(payload["surfaces"], [])

        asyncio.run(run())

    def test_editable_file_apply_route_is_backend_disabled_before_mutating(self) -> None:
        async def run() -> None:
            coordinator = FakeCoordinator()
            hass = FakeHass()
            hass.data[DOMAIN]["entry"] = coordinator
            request = FakeRequest({"value": "difficulty=3"}, hass)

            with self.assertRaises(Exception) as caught:
                await ProfileEditableFileApplyView().post(request, "123", "settings")

            self.assertIn("disabled in this release", response_payload(caught.exception)["error"]["message"])
            self.assertEqual(coordinator.apply_calls, 0)

        asyncio.run(run())

    def test_admin_only_routes_reject_non_admin_users_before_dispatch(self) -> None:
        async def run() -> None:
            coordinator = FakeCoordinator()
            coordinator.runtime.profile = ExtensionProfile()
            hass = FakeHass()
            hass.data[DOMAIN]["entry"] = coordinator

            with self.assertRaises(Exception) as edit_error:
                await ProfileEditableFileApplyView().post(
                    FakeRequest({"confirm": True, "value": "difficulty=3"}, hass, is_admin=False),
                    "123",
                    "settings",
                )
            self.assertEqual(response_payload(edit_error.exception)["error"]["code"], "forbidden")
            self.assertEqual(coordinator.apply_calls, 0)

            with self.assertRaises(Exception) as action_error:
                await ProfileActionView().post(
                    FakeRequest({"confirm": True}, hass, is_admin=False),
                    "123",
                    "danger",
                )
            self.assertEqual(response_payload(action_error.exception)["error"]["code"], "forbidden")

        asyncio.run(run())

    def test_redacted_editable_snapshot_omits_unredacted_parsed_data(self) -> None:
        snapshot = type(
            "Snapshot",
            (),
            {
                "key": "settings",
                "name": "Settings",
                "path": "/game/settings.ini",
                "text": "password=secret",
                "redacted_text": "password=***",
                "parsed": {"password": "secret"},
                "requires_restart": True,
            },
        )()

        redacted = _editable_snapshot_payload(snapshot, include_raw=False)
        raw = _editable_snapshot_payload(snapshot, include_raw=True)

        self.assertNotIn("parsed", redacted)
        self.assertNotIn("secret", json.dumps(redacted))
        self.assertEqual(raw["parsed"], {"password": "secret"})

    def test_structured_editable_snapshot_returns_model_without_raw_source(self) -> None:
        snapshot = SimpleNamespace(
            key="settings",
            name="Settings",
            path="/game/settings.ini",
            text="Password=secret\ndifficulty=1\n",
            redacted_text="Password=[redacted]\ndifficulty=1\n",
            parsed={"Password": "secret", "difficulty": "1"},
            editor_model={
                "settings": [
                    {"key": "Password", "raw_value": None, "sensitive": True, "configured": True},
                    {"key": "difficulty", "raw_value": "1", "sensitive": False},
                ]
            },
            requires_restart=True,
            revision="revision",
        )

        payload = _editable_snapshot_payload(snapshot, include_raw=False)

        self.assertEqual(payload["text"], "Password=[redacted]\ndifficulty=1\n")
        self.assertNotIn("parsed", payload)
        self.assertNotIn("secret", json.dumps(payload))
        self.assertTrue(payload["model"]["settings"][0]["configured"])

    def test_authenticated_editable_read_requires_admin_only_for_raw_output(self) -> None:
        async def run() -> None:
            class AuthenticatedEditableProfile(ExtensionProfile):
                def editable_files(self):
                    return (
                        EditableFileDeclaration(
                            key="settings",
                            name="Settings",
                            path_fn=lambda context: "/game/settings.ini",
                            access=ExtensionAccess.AUTHENTICATED,
                        ),
                    )

            coordinator = FakeCoordinator()
            coordinator.runtime.profile = AuthenticatedEditableProfile()
            hass = FakeHass()
            hass.data[DOMAIN]["entry"] = coordinator

            redacted = response_payload(
                await ProfileEditableFileReadView().post(
                    FakeRequest({}, hass, is_admin=False),
                    "123",
                    "settings",
                )
            )
            self.assertEqual(redacted["text"], "password=***")
            self.assertNotIn("parsed", redacted)
            self.assertEqual(coordinator.read_calls, 1)

            with self.assertRaises(Exception) as caught:
                await ProfileEditableFileReadView().post(
                    FakeRequest({"include_raw": True}, hass, is_admin=False),
                    "123",
                    "settings",
                )
            self.assertEqual(response_payload(caught.exception)["error"]["code"], "forbidden")
            self.assertEqual(coordinator.read_calls, 1)

        asyncio.run(run())

    def test_routes_return_structured_api_and_handler_errors(self) -> None:
        async def run() -> None:
            coordinator = FakeCoordinator()
            hass = FakeHass()
            hass.data[DOMAIN]["entry"] = coordinator

            coordinator.read_error = NitradoApiError("Nitrado fell over")
            with self.assertRaises(Exception) as api_error:
                await ProfileEditableFileReadView().post(FakeRequest({}, hass), "123", "settings")
            api_payload = response_payload(api_error.exception)
            self.assertEqual(api_payload["error"]["code"], "nitrado_api_error")
            self.assertNotIn("fell over", api_payload["error"]["message"])

            coordinator.read_error = RuntimeError("profile handler exploded")
            with self.assertRaises(Exception) as handler_error:
                await ProfileEditableFileReadView().post(FakeRequest({}, hass), "123", "settings")
            payload = response_payload(handler_error.exception)
            self.assertEqual(payload["error"]["code"], "extension_handler_error")
            self.assertNotIn("profile handler exploded", payload["error"]["message"])

        asyncio.run(run())


class ExceptionWithVerdict(Exception):
    """Exception double with a ProfileExtensionError-like verdict attribute."""

    def __init__(self, verdict: CapabilityVerdict) -> None:
        super().__init__(verdict.reason)
        self.verdict = verdict


if __name__ == "__main__":
    unittest.main()
