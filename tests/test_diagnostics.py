"""Tests for diagnostics redaction."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from custom_components.nitrado_gameserver.api.nitrado import (
    NitradoRequestContext,
    NitradoRequestFailure,
    NitradoService,
    ParsedServer,
    redact_payload,
)
from custom_components.nitrado_gameserver.const import CONF_PROFILE_OPTIONS, DOMAIN
from custom_components.nitrado_gameserver.coordinator import NitradoAccountCoordinator
from custom_components.nitrado_gameserver.diagnostics import (
    async_get_config_entry_diagnostics,
    coordinator_diagnostics,
)
from custom_components.nitrado_gameserver.models import (
    DiscoveryReconcileResult,
    ManagedServiceState,
)
from custom_components.nitrado_gameserver.plugins.base import (
    ActionDeclaration,
    CapabilityState,
    CapabilityVerdict,
    ProfileExtensionManifest,
    ProfileOptionDeclaration,
    ProfileOptionType,
    ProfileStatus,
)
from custom_components.nitrado_gameserver.plugins.palworld import PalworldProfile
from custom_components.nitrado_gameserver.runtime import ServiceRuntime


class FakeConfigEntry:
    """Minimal config entry fixture."""

    data = {
        "api_token": "secret-token",
        "safe": "value",
    }
    options = {
        "download_url": "https://example.invalid/file",
        "status_interval": 60,
    }


class DiagnosticsTests(unittest.TestCase):
    """Diagnostics tests."""

    def test_diagnostics_redacts_secret_fields(self) -> None:
        async def run() -> None:
            diagnostics = await async_get_config_entry_diagnostics(None, FakeConfigEntry())

            self.assertEqual(diagnostics["entry"]["data"]["api_token"], "[redacted]")
            self.assertEqual(diagnostics["entry"]["data"]["safe"], "value")
            self.assertEqual(diagnostics["entry"]["options"]["download_url"], "[redacted]")
            self.assertEqual(diagnostics["entry"]["options"]["status_interval"], 60)

        asyncio.run(run())

    def test_request_failure_diagnostics_keep_context_without_credentials(self) -> None:
        secret = "credential-that-must-not-leak"

        class Client:
            last_request_failure = NitradoRequestFailure(
                reason="endpoint_authorization_failure",
                request=NitradoRequestContext(
                    method="POST",
                    path="/services/{service_id}/gameservers/stop",
                    service_id="123456",
                    status=403,
                    provider_request_id="request-abc",
                    response_summary='{"message":"password=[redacted]"}',
                ),
                token_probe=NitradoRequestContext(
                    method="GET",
                    path="/services",
                    status=200,
                ),
            )
            bearer_token = secret
            authorization = f"Bearer {secret}"

        diagnostics = coordinator_diagnostics(NitradoAccountCoordinator(client=Client()))  # type: ignore[arg-type]
        failure = diagnostics["last_request_failure"]

        self.assertEqual(failure["reason"], "endpoint_authorization_failure")
        self.assertEqual(failure["request"]["method"], "POST")
        self.assertEqual(failure["request"]["path"], "/services/{service_id}/gameservers/stop")
        self.assertEqual(failure["request"]["service_id"], "123456")
        self.assertEqual(failure["request"]["status"], 403)
        self.assertEqual(failure["request"]["provider_request_id"], "request-abc")
        self.assertEqual(failure["token_probe"]["status"], 200)
        self.assertNotIn(secret, repr(diagnostics))

    def test_profile_secret_default_is_never_exposed_in_diagnostics(self) -> None:
        class SecretDefaultProfile(PalworldProfile):
            def profile_options(self):
                return (
                    ProfileOptionDeclaration(
                        key="admin_password",
                        name="Administrator password",
                        option_type=ProfileOptionType.SECRET,
                        default="must-never-leak",
                    ),
                )

        runtime = ServiceRuntime(ManagedServiceState("123456"))
        runtime.profile = SecretDefaultProfile()
        runtime.profile_manifest = ProfileExtensionManifest(
            profile_id="palworld",
            profile_options=SecretDefaultProfile().profile_options(),
        )
        coordinator = NitradoAccountCoordinator(client=object())
        coordinator.services["123456"] = runtime

        payload = coordinator_diagnostics(coordinator)
        encoded = str(payload)

        self.assertNotIn("must-never-leak", encoded)
        self.assertEqual(
            payload["services"]["123456"]["profile"]["extensions"]["profile_options"][0]["default"],
            "[redacted]",
        )

    def test_config_entry_diagnostics_redact_arbitrary_declared_secret_option_keys(self) -> None:
        class SecretProfile(PalworldProfile):
            profile_id = "secret_profile"

            def profile_options(self):
                return (
                    ProfileOptionDeclaration(
                        key="rcon_credential",
                        name="RCON credential",
                        option_type=ProfileOptionType.SECRET,
                        default="",
                    ),
                    ProfileOptionDeclaration(
                        key="query_enabled",
                        name="Query enabled",
                        option_type=ProfileOptionType.BOOLEAN,
                        default=False,
                    ),
                )

        runtime = ServiceRuntime(ManagedServiceState("123456"))
        runtime.profile = SecretProfile()
        runtime.profile_manifest = ProfileExtensionManifest(
            profile_id="secret_profile",
            profile_options=SecretProfile().profile_options(),
        )
        coordinator = NitradoAccountCoordinator(client=object())
        coordinator.services["123456"] = runtime

        class Entry:
            entry_id = "entry"
            data = {}
            options = {
                CONF_PROFILE_OPTIONS: {
                    "secret_profile": {
                        "rcon_credential": {"123456": "not-name-guessable"},
                        "query_enabled": {"123456": True},
                        "undeclared_future_secret": {"123456": "fail-closed"},
                    }
                }
            }

        class Hass:
            data = {DOMAIN: {"entry": coordinator}}

        diagnostics = asyncio.run(async_get_config_entry_diagnostics(Hass(), Entry()))
        values = diagnostics["entry"]["options"][CONF_PROFILE_OPTIONS]["secret_profile"]
        self.assertEqual(values["rcon_credential"]["123456"], "[redacted]")
        self.assertIs(values["query_enabled"]["123456"], True)
        self.assertEqual(values["undeclared_future_secret"]["123456"], "[redacted]")

    def test_conflicting_profile_option_declarations_fail_closed(self) -> None:
        class DeclaredProfile(PalworldProfile):
            profile_id = "conflicting_profile"

        coordinator = NitradoAccountCoordinator(client=object())
        for service_id, option_type in (
            ("one", ProfileOptionType.SECRET),
            ("two", ProfileOptionType.BOOLEAN),
        ):
            runtime = ServiceRuntime(ManagedServiceState(service_id))
            runtime.profile = DeclaredProfile()
            runtime.profile_manifest = ProfileExtensionManifest(
                profile_id=DeclaredProfile.profile_id,
                profile_options=(
                    ProfileOptionDeclaration(
                        key="shared_option",
                        name="Shared option",
                        option_type=option_type,
                        default="" if option_type == ProfileOptionType.SECRET else False,
                    ),
                ),
            )
            coordinator.services[service_id] = runtime

        class Entry:
            entry_id = "entry"
            data = {}
            options = {
                CONF_PROFILE_OPTIONS: {
                    "conflicting_profile": {
                        "shared_option": {"one": "secret-one", "two": "secret-two"},
                    }
                }
            }

        class Hass:
            data = {DOMAIN: {"entry": coordinator}}

        diagnostics = asyncio.run(async_get_config_entry_diagnostics(Hass(), Entry()))
        values = diagnostics["entry"]["options"][CONF_PROFILE_OPTIONS]["conflicting_profile"]
        self.assertEqual(values["shared_option"], {"one": "[redacted]", "two": "[redacted]"})

    def test_redaction_serializes_dataclasses_and_sets(self) -> None:
        payload = redact_payload(
            {
                "result": DiscoveryReconcileResult(newly_discovered={"b", "a"}),
                "token": "secret",
            }
        )

        self.assertEqual(payload["token"], "[redacted]")
        self.assertEqual(payload["result"]["newly_discovered"], ["a", "b"])

    def test_redaction_covers_nested_profile_secret_conventions_and_mixed_sets(self) -> None:
        payload = redact_payload(
            {
                "profile_status": {
                    "extra": {
                        "server_password": "server-secret",
                        "client_secret": "client-secret",
                        "access_token": "access-secret",
                        "secret": "plain-secret",
                    }
                },
                "mixed": frozenset({1, "a"}),
            }
        )

        self.assertEqual(
            payload["profile_status"]["extra"],
            {
                "server_password": "[redacted]",
                "client_secret": "[redacted]",
                "access_token": "[redacted]",
                "secret": "[redacted]",
            },
        )
        self.assertCountEqual(payload["mixed"], [1, "a"])

    def test_coordinator_diagnostics_include_runtime_sources_and_verdicts(self) -> None:
        coordinator = NitradoAccountCoordinator(client=object())  # type: ignore[arg-type]
        runtime = ServiceRuntime(ManagedServiceState("123456"))
        runtime.update_service(
            NitradoService(
                service_id="123456",
                name="Palworld Xbox",
                game="palworldxb",
                game_human="Palworld",
                folder_short="palworldxb",
                type_human="Gameserver",
                raw_redacted={},
            )
        )
        runtime.profile = PalworldProfile()
        runtime.server = ParsedServer(
            service_id="123456",
            raw_status="started",
            server_name="Example Palworld Server",
            address="31.214.203.101:17100",
            game_short="palworldxb",
            game_human="Palworld",
            player_count=1,
            player_max=10,
            player_names=("ExamplePlayer",),
            query_valid=True,
            player_source="palworld_rest",
            raw_redacted={
                "gameserver": {
                    "query": {
                        "player_current": 0,
                        "player_max": 10,
                        "server_name": "Example Palworld Server",
                        "players": [],
                    }
                }
            },
        )
        runtime.status_fresh = True
        runtime.refreshed_at = 1234
        runtime.last_transition_at = 1200
        runtime.idle_started_at = 1210
        runtime.shutdown_pending = True
        runtime.last_reset_reason = "Idle timer started after valid zero-player poll"
        runtime.last_start_verdict = CapabilityVerdict(
            CapabilityState.BLOCKED,
            reason="Server status is started; start was not sent.",
            overridable=True,
        )
        runtime.last_stop_verdict = CapabilityVerdict(CapabilityState.SUPPORTED)
        runtime.last_lifecycle_hook_results["after_start"] = (
            {
                "key": "report_start",
                "event": "after_start",
                "result_type": "RuntimeError",
                "verdict": {
                    "state": "blocked",
                    "reason": "after hook failed",
                    "source": "profile",
                    "overridable": False,
                },
            },
        )
        runtime.extra["profile_status"] = ProfileStatus(
            player_count=1,
            player_max=10,
            player_names=("ExamplePlayer",),
            player_source="palworld_rest",
            query_valid=True,
            extra={
                "palworld_rest_enabled": True,
                "nitrado_query_trustworthy": False,
                "admin_password": "should-redact",
            },
        )
        coordinator.services["123456"] = runtime

        diagnostics = redact_payload(coordinator_diagnostics(coordinator))
        service = diagnostics["services"]["123456"]

        self.assertEqual(service["profile"]["profile_id"], "palworld")
        self.assertEqual(
            [item["key"] for item in service["profile"]["extensions"]["entities"]],
            ["player_source"],
        )
        self.assertEqual([item["key"] for item in service["profile"]["extensions"]["editable_files"]], ["settings"])
        self.assertEqual(service["profile"]["extensions"]["lifecycle_hooks"], [])
        self.assertEqual(service["players"]["final"]["player_count"], 1)
        self.assertEqual(service["players"]["final"]["player_names_count"], 1)
        self.assertEqual(service["players"]["final"]["player_source"], "palworld_rest")
        self.assertEqual(service["players"]["nitrado_query"]["player_current"], 0)
        self.assertFalse(service["players"]["profile_override"]["extra"]["nitrado_query_trustworthy"])
        self.assertEqual(service["players"]["profile_override"]["extra"]["admin_password"], "[redacted]")
        self.assertTrue(service["profile"]["idle_shutdown_supported"])
        self.assertFalse(service["profile"]["extensions"]["invalid"])
        self.assertTrue(service["idle_shutdown"]["shutdown_pending"])
        self.assertEqual(
            service["idle_shutdown"]["last_reset_reason"], "Idle timer started after valid zero-player poll"
        )
        self.assertEqual(service["controls"]["start"]["state"], "blocked")
        self.assertTrue(service["controls"]["start"]["overridable"])
        self.assertEqual(service["controls"]["stop"]["state"], "supported")
        self.assertEqual(service["lifecycle_hooks"]["after_start"][0]["key"], "report_start")
        self.assertEqual(service["lifecycle_hooks"]["after_start"][0]["verdict"]["reason"], "after hook failed")
        self.assertNotIn("ExamplePlayer", str(diagnostics))

    def test_diagnostics_report_invalid_profile_manifest_without_crashing(self) -> None:
        class InvalidProfile(PalworldProfile):
            profile_id = "invalid"

            def actions(self):
                return (
                    ActionDeclaration("duplicate", "Duplicate", action_fn=lambda context: None),
                    ActionDeclaration("duplicate", "Duplicate Again", action_fn=lambda context: None),
                )

        coordinator = NitradoAccountCoordinator(client=object())  # type: ignore[arg-type]
        runtime = ServiceRuntime(ManagedServiceState("123456"))
        runtime.profile = InvalidProfile()
        coordinator.services["123456"] = runtime

        diagnostics = coordinator_diagnostics(coordinator)
        extensions = diagnostics["services"]["123456"]["profile"]["extensions"]

        self.assertTrue(extensions["invalid"])
        self.assertIn("details were logged", extensions["error"])
        self.assertEqual(extensions["actions"], [])

    def test_diagnostics_report_profile_declaration_failures_without_crashing(self) -> None:
        class BrokenProfile(PalworldProfile):
            profile_id = "broken"

            def actions(self):
                raise RuntimeError("action declarations exploded")

        coordinator = NitradoAccountCoordinator(client=object())  # type: ignore[arg-type]
        runtime = ServiceRuntime(ManagedServiceState("123456"))
        runtime.profile = BrokenProfile()
        coordinator.services["123456"] = runtime

        diagnostics = coordinator_diagnostics(coordinator)
        extensions = diagnostics["services"]["123456"]["profile"]["extensions"]

        self.assertTrue(extensions["invalid"])
        self.assertIn("details were logged", extensions["error"])
        self.assertEqual(extensions["actions"], [])


if __name__ == "__main__":
    unittest.main()
