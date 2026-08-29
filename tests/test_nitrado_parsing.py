"""Tests for generic Nitrado parser behavior."""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from custom_components.nitrado_gameserver.api.nitrado import (
    ENDPOINT_AUTHORIZATION_FAILURE,
    MAX_API_RESPONSE_BYTES,
    MAX_EDITABLE_FILE_BYTES,
    MAX_PLAYER_NAME_CHARS,
    MAX_PLAYER_NAMES,
    MAX_STATUS_CHARS,
    MAX_STOP_REASON_CHARS,
    NitradoApiError,
    NitradoAuthError,
    NitradoClient,
    NitradoEndpointAuthorizationError,
    NitradoService,
    _async_pinned_request,
    _async_validate_outbound_url,
    _read_text_limited,
    _validate_outbound_url,
    fallback_account_identity,
    find_upload_token,
    parse_account_identity,
    parse_gameserver,
    parse_player_list,
    parse_service,
    redact_payload,
    split_remote_path,
)


class FakeResponse:
    """Minimal aiohttp-like response fixture."""

    def __init__(
        self,
        status: int,
        payload: dict | None = None,
        *,
        raw_text: str | None = None,
        headers: dict | None = None,
    ) -> None:
        self.status = status
        self._payload = payload or {}
        self._raw_text = raw_text
        self.headers = headers or {}
        self.released = False

    def release(self) -> None:
        self.released = True

    async def text(self) -> str:
        """Return response text."""

        return self._raw_text if self._raw_text is not None else json.dumps(self._payload)

    async def json(self, content_type=None) -> dict:
        """Return response JSON."""

        return self._payload


class FakeSession:
    """Minimal aiohttp-like session fixture."""

    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.requests = []
        self.posts = []
        self.gets = []

    async def request(self, method: str, url: str, **kwargs):
        """Return configured response."""

        self.requests.append((method, url, kwargs))
        return self.response

    async def post(self, url: str, **kwargs):
        """Return configured upload response."""

        self.posts.append((url, kwargs))
        return FakeResponse(200, {"status": "success"})

    async def get(self, url: str, **kwargs):
        """Return configured download/external response."""

        self.gets.append((url, kwargs))
        return self.response


class SequencedSession(FakeSession):
    """Return a distinct response for the failed endpoint and account probe."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        super().__init__(responses[-1])
        self.responses = list(responses)

    async def request(self, method: str, url: str, **kwargs):
        """Return the next configured response."""

        self.requests.append((method, url, kwargs))
        return self.responses.pop(0)


class NitradoParsingTests(unittest.TestCase):
    """Generic Nitrado parser tests."""

    def test_outbound_url_policy_rejects_unsafe_targets(self) -> None:
        _validate_outbound_url("https://upload.example.invalid/file", allow_http=False)
        _validate_outbound_url(
            "http://gameserver.example.invalid:8212/v1/api/info",
            allow_http=True,
            allowed_hosts=("gameserver.example.invalid",),
        )

        for unsafe in (
            "http://upload.example.invalid/file",
            "file:///etc/passwd",
            "https://user:secret@example.invalid/file",
            "https://127.0.0.1/file",
            "https://169.254.169.254/latest/meta-data",
            "https://server.local/file",
        ):
            with self.subTest(url=unsafe), self.assertRaises(NitradoApiError):
                _validate_outbound_url(unsafe, allow_http=False)

        with self.assertRaisesRegex(NitradoApiError, "not declared"):
            _validate_outbound_url(
                "https://other.example.invalid/data",
                allow_http=True,
                allowed_hosts=("gameserver.example.invalid",),
            )

    def test_outbound_dns_policy_rejects_private_resolution(self) -> None:
        class FakeLoop:
            async def getaddrinfo(self, *args, **kwargs):
                return [(None, None, None, None, ("192.168.1.10", 443))]

        async def run() -> None:
            original = asyncio.get_running_loop
            try:
                asyncio.get_running_loop = lambda: FakeLoop()  # type: ignore[assignment]
                with self.assertRaisesRegex(NitradoApiError, "non-public"):
                    await _async_validate_outbound_url(
                        "https://signed-download.example.invalid/file",
                        allow_http=False,
                    )
            finally:
                asyncio.get_running_loop = original  # type: ignore[assignment]

        asyncio.run(run())

    def test_outbound_request_pins_validated_dns_address(self) -> None:
        class FakeLoop:
            async def getaddrinfo(self, *args, **kwargs):
                return [(None, None, None, None, ("93.184.216.34", 443))]

        async def run() -> None:
            original = asyncio.get_running_loop
            session = FakeSession(FakeResponse(200, {"ok": True}))
            try:
                asyncio.get_running_loop = lambda: FakeLoop()  # type: ignore[assignment]
                await _async_pinned_request(
                    session,
                    "GET",
                    "https://download.example.invalid:8443/file?token=signed",
                    allow_http=False,
                    allow_redirects=False,
                )
            finally:
                asyncio.get_running_loop = original  # type: ignore[assignment]

            self.assertEqual(
                session.gets[0][0],
                "https://93.184.216.34:8443/file?token=signed",
            )
            self.assertEqual(session.gets[0][1]["headers"]["Host"], "download.example.invalid:8443")
            self.assertEqual(session.gets[0][1]["server_hostname"], "download.example.invalid")
            self.assertFalse(session.gets[0][1]["allow_redirects"])

        asyncio.run(run())

    def test_limited_reader_rejects_declared_and_actual_oversize_bodies(self) -> None:
        async def run() -> None:
            declared = FakeResponse(
                200,
                raw_text="small",
                headers={"Content-Length": str(MAX_API_RESPONSE_BYTES + 1)},
            )
            with self.assertRaisesRegex(NitradoApiError, "safety limit"):
                await _read_text_limited(declared, MAX_API_RESPONSE_BYTES)
            self.assertTrue(declared.released)

            actual = FakeResponse(200, raw_text="x" * (MAX_EDITABLE_FILE_BYTES + 1))
            with self.assertRaisesRegex(NitradoApiError, "safety limit"):
                await _read_text_limited(actual, MAX_EDITABLE_FILE_BYTES)
            self.assertTrue(actual.released)

        asyncio.run(run())

    def test_parse_service_extracts_identity_and_game_metadata(self) -> None:
        service = parse_service(
            {
                "id": 123456,
                "comment": "Family server",
                "type_human": "Gameserver",
                "details": {
                    "name": "Palworld Xbox",
                    "game": "palworldxb",
                    "game_human": "Palworld",
                    "folder_short": "palworldxb",
                },
            }
        )

        self.assertEqual(service.service_id, "123456")
        self.assertEqual(service.name, "Palworld Xbox")
        self.assertEqual(service.game, "palworldxb")
        self.assertEqual(service.game_human, "Palworld")
        self.assertEqual(service.folder_short, "palworldxb")

    def test_parse_account_identity_accepts_supported_payload_shapes(self) -> None:
        self.assertEqual(
            parse_account_identity({"data": {"user": {"id": 42}}}),
            "42",
        )
        self.assertEqual(
            parse_account_identity({"data": {"account": {"account_id": "acct-7"}}}),
            "acct-7",
        )
        self.assertIsNone(parse_account_identity({"data": {"user": {}}}))

    def test_fallback_account_identity_is_stable_and_secret_safe(self) -> None:
        service = NitradoService("123", None, None, None, None, None, {})

        first = fallback_account_identity([service], "one-token")
        second = fallback_account_identity([service], "different-token")

        same_token_new_inventory = fallback_account_identity([], "one-token")

        self.assertNotEqual(first, second)
        self.assertEqual(first, same_token_new_inventory)
        self.assertTrue(first.startswith("fallback-"))
        self.assertNotIn("token", first)

    def test_parse_service_requires_id(self) -> None:
        with self.assertRaises(NitradoApiError):
            parse_service({"details": {"name": "Nope"}})

        with self.assertRaises(NitradoApiError):
            parse_service({"id": "../../oops", "details": {"name": "Nope"}})

    def test_parse_gameserver_preserves_valid_query_data(self) -> None:
        server = parse_gameserver(
            "123456",
            {
                "status": "started",
                "label": "Family Palworld",
                "game": "palworldxb",
                "ip": "203.0.113.10",
                "port": 8211,
                "slots": 10,
                "query": {
                    "server_name": "Example Server",
                    "connect_ip": "203.0.113.10:8211",
                    "player_current": "2",
                    "player_max": "10",
                    "players": [{"name": "Alex"}, "Jordan"],
                },
            },
        )

        self.assertEqual(server.service_id, "123456")
        self.assertEqual(server.raw_status, "started")
        self.assertEqual(server.server_name, "Example Server")
        self.assertEqual(server.address, "203.0.113.10:8211")
        self.assertEqual(server.game_short, "palworldxb")
        self.assertEqual(server.player_count, 2)
        self.assertEqual(server.player_max, 10)
        self.assertEqual(server.player_names, ("Alex", "Jordan"))
        self.assertTrue(server.query_valid)
        self.assertEqual(server.player_source, "nitrado_query")

    def test_parse_gameserver_does_not_invent_zero_from_malformed_query(self) -> None:
        server = parse_gameserver(
            "123456",
            {
                "status": "started",
                "label": "Broken Query",
                "game": "palworldxb",
                "slots": 10,
                "query": {
                    "player_current": None,
                    "player_max": "10",
                    "players": [],
                },
            },
        )

        self.assertFalse(server.query_valid)
        self.assertIsNone(server.player_count)
        self.assertIsNone(server.player_source)
        self.assertEqual(server.player_max, 10)

    def test_parse_gameserver_requires_status(self) -> None:
        with self.assertRaises(NitradoApiError):
            parse_gameserver("123456", {"label": "No status"})

        with self.assertRaisesRegex(NitradoApiError, "field length"):
            parse_gameserver("123456", {"status": "x" * (MAX_STATUS_CHARS + 1)})

    def test_player_fields_are_bounded_before_reaching_ha_state(self) -> None:
        long_name = "x" * (MAX_PLAYER_NAME_CHARS + 20)
        names = parse_player_list([long_name] * (MAX_PLAYER_NAMES + 50))

        self.assertEqual(len(names), MAX_PLAYER_NAMES)
        self.assertTrue(all(len(name) == MAX_PLAYER_NAME_CHARS for name in names))

    def test_stop_reason_is_bounded_before_transport(self) -> None:
        async def run() -> None:
            response = FakeResponse(200, raw_text='{"status":"success"}')
            session = FakeSession(response)
            client = NitradoClient(session, "token")  # type: ignore[arg-type]

            await client.stop_server("123456", "r" * (MAX_STOP_REASON_CHARS + 100))

            payload = session.requests[0][2]["json"]
            self.assertEqual(len(payload["message"]), MAX_STOP_REASON_CHARS)
            self.assertEqual(payload["message"], payload["stop_message"])

        asyncio.run(run())

    def test_parse_player_list_accepts_common_shapes(self) -> None:
        self.assertEqual(
            parse_player_list(
                [
                    "PlainName",
                    {"username": "UserName"},
                    {"display_name": "DisplayName"},
                    {},
                    42,
                ]
            ),
            ("PlainName", "UserName", "DisplayName", "Player 4", "Player 5"),
        )

    def test_redact_payload_removes_secret_fields_recursively(self) -> None:
        payload = {
            "token": "secret",
            "nested": {
                "Authorization": "Bearer nope",
                "download_url": "https://example.invalid/file",
                "safe": "value",
            },
        }

        self.assertEqual(
            redact_payload(payload),
            {
                "token": "[redacted]",
                "nested": {
                    "Authorization": "[redacted]",
                    "download_url": "[redacted]",
                    "safe": "value",
                },
            },
        )

    def test_client_raises_auth_error_for_rejected_token(self) -> None:
        async def run() -> None:
            client = NitradoClient(FakeSession(FakeResponse(401, {"status": "error"})), "bad-token")

            with self.assertRaises(NitradoAuthError):
                await client.service_list()

        asyncio.run(run())

    def test_success_logging_is_allowlisted_and_optional_account_404_is_not_warning(self) -> None:
        async def successful() -> None:
            secret = "unknown-success-field-must-not-be-logged"
            session = FakeSession(
                FakeResponse(
                    200,
                    {
                        "status": "success",
                        "data": {"services": [], "future_private_field": secret},
                    },
                )
            )
            client = NitradoClient(session, "token")  # type: ignore[arg-type]
            with self.assertLogs("custom_components.nitrado_gameserver.api.nitrado", level="DEBUG") as captured:
                self.assertEqual(await client.service_list(), [])
            self.assertNotIn(secret, "\n".join(captured.output))

        async def optional_not_found() -> None:
            client = NitradoClient(FakeSession(FakeResponse(404, {"status": "error"})), "token")
            with (
                self.assertLogs("custom_components.nitrado_gameserver.api.nitrado", level="DEBUG") as captured,
                self.assertRaises(NitradoApiError),
            ):
                await client.account_identity()
            self.assertTrue(any("Optional Nitrado endpoint" in line for line in captured.output))
            self.assertFalse(any(":WARNING:" in line for line in captured.output))

        asyncio.run(successful())
        asyncio.run(optional_not_found())

    def test_service_401_or_403_with_successful_account_probe_is_not_token_rejection(self) -> None:
        async def run(status: int) -> None:
            secret = "this-must-never-appear-in-diagnostics"
            session = SequencedSession(
                [
                    FakeResponse(
                        status,
                        {
                            "status": "error",
                            "message": f"password={secret} Bearer {secret}",
                            "credentials": {"token": secret},
                        },
                        headers={"X-Correlation-ID": "provider-request-123"},
                    ),
                    FakeResponse(200, {"status": "success", "data": {"services": []}}),
                ]
            )
            client = NitradoClient(session, "long-lived-secret-token")  # type: ignore[arg-type]

            with self.assertRaises(NitradoEndpointAuthorizationError) as raised:
                await client.fetch_server("123456")

            failure = raised.exception.failure
            self.assertIsNotNone(failure)
            assert failure is not None
            self.assertEqual(failure.reason, ENDPOINT_AUTHORIZATION_FAILURE)
            self.assertEqual(failure.request.method, "GET")
            self.assertEqual(failure.request.path, "/services/{service_id}/gameservers")
            self.assertEqual(failure.request.service_id, "123456")
            self.assertEqual(failure.request.status, status)
            self.assertEqual(failure.request.provider_request_id, "provider-request-123")
            self.assertEqual(failure.token_probe.status, 200)
            self.assertEqual(failure.token_probe.path, "/services")
            self.assertNotIn(secret, repr(failure.as_dict()))
            self.assertNotIn("long-lived-secret-token", repr(failure.as_dict()))
            self.assertEqual(client.verified_account_probe_count, 1)
            self.assertEqual(
                [request[1] for request in session.requests],
                [
                    "https://api.nitrado.net/services/123456/gameservers",
                    "https://api.nitrado.net/services",
                ],
            )

        for status in (401, 403):
            with self.subTest(status=status):
                asyncio.run(run(status))

    def test_service_auth_candidate_becomes_token_rejected_only_when_probe_rejects(self) -> None:
        async def run(probe_status: int) -> None:
            session = SequencedSession(
                [
                    FakeResponse(403, {"status": "error", "message": "operation unavailable"}),
                    FakeResponse(probe_status, {"status": "error", "message": "unauthorized"}),
                ]
            )
            client = NitradoClient(session, "rejected-token")  # type: ignore[arg-type]

            with self.assertRaises(NitradoAuthError) as raised:
                await client.fetch_server("123456")

            failure = raised.exception.failure
            self.assertIsNotNone(failure)
            assert failure is not None
            self.assertEqual(failure.reason, "token_rejected")
            self.assertEqual(failure.request.status, 403)
            self.assertEqual(failure.token_probe.status, probe_status)
            self.assertEqual(failure.token_probe.path, "/services")
            self.assertEqual(client.verified_account_probe_count, 0)

        for probe_status in (401, 403):
            with self.subTest(probe_status=probe_status):
                asyncio.run(run(probe_status))

    def test_webinterface_login_is_ephemeral_service_bound_and_host_validated(self) -> None:
        async def run() -> None:
            url = "https://webinterface.nitrado.net/?access_token=short-lived-secret&service_id=123&lable=ni"
            session = FakeSession(
                FakeResponse(
                    200,
                    {"status": "success", "data": {"url": url, "expires_at": 2_000_000_000}},
                )
            )
            login = await NitradoClient(session, "token").webinterface_login("123")

            self.assertEqual(login.url, url)
            self.assertEqual(login.expires_at, 2_000_000_000)
            self.assertNotIn("short-lived-secret", repr(login))
            self.assertEqual(
                session.requests[0][1],
                "https://api.nitrado.net/services/123/webinterface_login",
            )

            for unsafe_url in (
                "http://webinterface.nitrado.net/?access_token=x&service_id=123",
                "https://evil.example/?access_token=x&service_id=123",
                "https://webinterface.nitrado.net/?access_token=x&service_id=999",
                "https://webinterface.nitrado.net/?service_id=123",
            ):
                client = NitradoClient(
                    FakeSession(
                        FakeResponse(
                            200,
                            {
                                "status": "success",
                                "data": {"url": unsafe_url, "expires_at": 2_000_000_000},
                            },
                        )
                    ),
                    "token",
                )
                with self.subTest(url=unsafe_url), self.assertRaises(NitradoApiError):
                    await client.webinterface_login("123")

        asyncio.run(run())

    def test_service_list_skips_one_malformed_entry(self) -> None:
        async def run() -> None:
            client = NitradoClient(
                FakeSession(
                    FakeResponse(
                        200,
                        {
                            "status": "success",
                            "data": {
                                "services": [
                                    {"details": {"name": "missing id"}},
                                    {"id": 123, "details": {"name": "valid"}},
                                ]
                            },
                        },
                    )
                ),
                "token",
            )

            services = await client.service_list()

            self.assertEqual([service.service_id for service in services], ["123"])

        asyncio.run(run())

    def test_success_payload_with_non_object_data_is_bounded(self) -> None:
        async def run() -> None:
            operations = (
                lambda client: client.service_list(),
                lambda client: client.fetch_server("123456"),
                lambda client: client.fetch_players("123456"),
            )
            for operation in operations:
                client = NitradoClient(
                    FakeSession(FakeResponse(200, {"status": "success", "data": []})),
                    "token",
                )
                with self.subTest(operation=operation), self.assertRaises(NitradoApiError):
                    await operation(client)

        asyncio.run(run())

    def test_upload_text_file_requests_token_then_posts_upload(self) -> None:
        async def run() -> None:
            session = FakeSession(
                FakeResponse(
                    200,
                    {
                        "status": "success",
                        "data": {
                            "token": {
                                "url": "https://93.184.216.34/",
                                "token": "upload-token",
                            }
                        },
                    },
                )
            )
            client = NitradoClient(session, "token")

            await client.upload_text_file("123456", "/game/server.properties", "difficulty=2")

            self.assertEqual(session.requests[0][0], "POST")
            self.assertIn("/services/123456/gameservers/file_server/upload", session.requests[0][1])
            self.assertEqual(session.requests[0][2]["json"], {"path": "/game", "file": "server.properties"})
            self.assertEqual(session.posts[0][0], "https://93.184.216.34/")
            self.assertEqual(session.posts[0][1]["data"], b"difficulty=2")
            self.assertEqual(
                session.posts[0][1]["headers"],
                {
                    "content-type": "application/binary",
                    "token": "upload-token",
                    "Host": "93.184.216.34",
                },
            )
            self.assertEqual(session.posts[0][1]["server_hostname"], "93.184.216.34")
            self.assertFalse(session.posts[0][1]["allow_redirects"])

        asyncio.run(run())

    def test_upload_rejects_oversized_content_before_request(self) -> None:
        async def run() -> None:
            session = FakeSession(FakeResponse(200, {"status": "success"}))
            client = NitradoClient(session, "token")

            with self.assertRaisesRegex(NitradoApiError, "upload exceeds"):
                await client.upload_text_file(
                    "123456",
                    "/game/server.properties",
                    "x" * (MAX_EDITABLE_FILE_BYTES + 1),
                )

            self.assertEqual(session.requests, [])

        asyncio.run(run())

    def test_upload_failure_does_not_echo_upstream_body(self) -> None:
        class FailingUploadSession(FakeSession):
            async def post(self, url: str, **kwargs):
                self.posts.append((url, kwargs))
                return FakeResponse(500, raw_text="upstream-secret-body")

        async def run() -> None:
            session = FailingUploadSession(
                FakeResponse(
                    200,
                    {
                        "status": "success",
                        "data": {"token": {"url": "https://93.184.216.34/", "token": "upload-token"}},
                    },
                )
            )
            client = NitradoClient(session, "token")  # type: ignore[arg-type]

            with self.assertRaises(NitradoApiError) as caught:
                await client.upload_text_file("123456", "/game/server.properties", "difficulty=2")

            self.assertNotIn("upstream-secret-body", str(caught.exception))
            self.assertIn("HTTP 500", str(caught.exception))

        asyncio.run(run())

    def test_upload_helpers_parse_token_and_split_paths(self) -> None:
        self.assertEqual(
            find_upload_token({"data": {"token": {"url": "https://example.invalid", "token": "abc"}}}),
            "abc",
        )
        self.assertEqual(split_remote_path("/game/server.properties"), ("/game", "server.properties"))

        with self.assertRaises(NitradoApiError):
            split_remote_path("server.properties")


if __name__ == "__main__":
    unittest.main()
