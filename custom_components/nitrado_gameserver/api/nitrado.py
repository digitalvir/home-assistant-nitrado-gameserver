"""Generic Nitrado API client and parsers."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import re
import socket
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit, urlunsplit

try:
    from aiohttp import ClientError, ClientSession
except ModuleNotFoundError:  # pragma: no cover - local parser tests can run without HA/aiohttp installed.

    class ClientError(Exception):
        """Fallback ClientError when aiohttp is unavailable in local tests."""

    ClientSession = Any  # type: ignore[misc, assignment]

API_BASE = "https://api.nitrado.net"
MAX_API_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_EDITABLE_FILE_BYTES = 4 * 1024 * 1024
MAX_UPLOAD_RESPONSE_BYTES = 64 * 1024
MAX_STOP_REASON_CHARS = 512
MAX_STATUS_CHARS = 64
MAX_TEXT_FIELD_CHARS = 512
MAX_PLAYER_NAME_CHARS = 128
MAX_PLAYER_NAMES = 1024
MAX_FAILURE_RESPONSE_SUMMARY_CHARS = 512

AUTH_CANDIDATE = "auth_candidate"
AUTH_CANDIDATE_UNVERIFIED = "auth_candidate_unverified"
ENDPOINT_AUTHORIZATION_FAILURE = "endpoint_authorization_failure"
# This is a diagnostic classification label, never a credential.
TOKEN_REJECTED = "token_rejected"  # nosec B105

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class NitradoRequestContext:
    """Bounded, secret-free context for one provider request."""

    method: str
    path: str
    service_id: str | None = None
    status: int | None = None
    provider_request_id: str | None = None
    response_summary: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return diagnostics-safe primitives."""

        return {
            "method": self.method,
            "path": self.path,
            "service_id": self.service_id,
            "status": self.status,
            "provider_request_id": self.provider_request_id,
            "response_summary": self.response_summary,
        }


@dataclass(slots=True, frozen=True)
class NitradoRequestFailure:
    """Secret-free classification and evidence for one failed request."""

    reason: str
    request: NitradoRequestContext
    token_probe: NitradoRequestContext | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return diagnostics-safe primitives."""

        return {
            "reason": self.reason,
            "request": self.request.as_dict(),
            "token_probe": self.token_probe.as_dict() if self.token_probe is not None else None,
        }


class NitradoApiError(Exception):
    """Raised when Nitrado cannot be queried safely."""

    def __init__(self, message: str, *, failure: NitradoRequestFailure | None = None) -> None:
        super().__init__(message)
        self.failure = failure


class NitradoAuthError(NitradoApiError):
    """Raised only after the account probe rejects API credentials."""


class NitradoEndpointAuthorizationError(NitradoApiError):
    """Raised when one endpoint rejects a still-valid account token."""


@dataclass(slots=True, frozen=True)
class NitradoService:
    """A service from the Nitrado account service list."""

    service_id: str
    name: str | None
    game: str | None
    game_human: str | None
    folder_short: str | None
    type_human: str | None
    raw_redacted: dict[str, Any]


@dataclass(slots=True, frozen=True)
class ParsedServer:
    """Parsed Nitrado gameserver details."""

    service_id: str
    raw_status: str
    server_name: str | None
    address: str | None
    game_short: str | None
    game_human: str | None
    player_count: int | None
    player_max: int | None
    player_names: tuple[str, ...]
    query_valid: bool
    player_source: str | None
    raw_redacted: dict[str, Any]


@dataclass(slots=True, frozen=True)
class NitradoFtpCredentials:
    """Ephemeral FTP credentials returned for one Nitrado gameserver."""

    hostname: str
    port: int
    username: str
    password: str = field(repr=False)
    secure_hint: bool | None = None


@dataclass(slots=True, frozen=True)
class NitradoWebinterfaceLogin:
    """Ephemeral authenticated Nitrado web-interface destination."""

    url: str = field(repr=False)
    expires_at: int


class NitradoClient:
    """Minimal async client for the official Nitrado API."""

    def __init__(self, session: ClientSession, token: str) -> None:
        self._session = session
        self._token = token
        self._auth_probe_lock = asyncio.Lock()
        self.last_request_failure: NitradoRequestFailure | None = None
        self.verified_account_probe_count = 0

    async def _request(
        self,
        method: str,
        path: str,
        *,
        _account_probe: bool = False,
        _optional_not_found: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        payload, _context = await self._request_with_context(
            method,
            path,
            _account_probe=_account_probe,
            _optional_not_found=_optional_not_found,
            **kwargs,
        )
        return payload

    async def _request_with_context(
        self,
        method: str,
        path: str,
        *,
        _account_probe: bool = False,
        _optional_not_found: bool = False,
        **kwargs: Any,
    ) -> tuple[dict[str, Any], NitradoRequestContext]:
        method = str(method).upper()
        headers = kwargs.pop("headers", {})
        headers = dict(headers)
        headers["Authorization"] = f"Bearer {self._token}"
        headers.setdefault("Accept", "application/json")
        if method != "GET":
            headers.setdefault("Content-Type", "application/json")
        url = f"{API_BASE}{path}"
        response = None
        try:
            async with asyncio.timeout(20):
                response = await self._session.request(
                    method,
                    url,
                    headers=headers,
                    allow_redirects=False,
                    **kwargs,
                )
                text = await _read_text_limited(response, MAX_API_RESPONSE_BYTES)
        except (TimeoutError, ClientError) as err:
            context = _request_context(method, path)
            failure = NitradoRequestFailure("transport_failure", context)
            self._record_failure(failure)
            raise NitradoApiError("Nitrado request failed before receiving a response", failure=failure) from err
        except NitradoApiError as err:
            context = _request_context(
                method,
                path,
                status=getattr(response, "status", None),
                headers=getattr(response, "headers", None),
            )
            failure = err.failure or NitradoRequestFailure("response_read_failure", context)
            self._record_failure(failure)
            raise NitradoApiError("Nitrado response could not be read safely", failure=failure) from err

        context = _request_context(
            method,
            path,
            status=response.status,
            headers=getattr(response, "headers", None),
            response_text=text if response.status >= 400 else None,
        )

        if response.status in {401, 403}:
            candidate = NitradoRequestFailure(AUTH_CANDIDATE, context)
            self._record_failure(candidate)
            if _account_probe:
                rejected = NitradoRequestFailure(TOKEN_REJECTED, context)
                self._record_failure(rejected)
                raise NitradoAuthError(
                    f"{TOKEN_REJECTED}: Nitrado account probe returned HTTP {response.status}",
                    failure=rejected,
                )
            await self._raise_classified_auth_candidate(candidate)
        if response.status >= 400:
            failure = NitradoRequestFailure("provider_http_failure", context)
            self._record_failure(
                failure,
                warning=not (_optional_not_found and response.status == 404),
            )
            raise NitradoApiError(
                f"provider_http_failure: Nitrado {method} {context.path} returned HTTP {response.status}",
                failure=failure,
            )

        try:
            payload = json.loads(text)
        except Exception as err:
            failure = NitradoRequestFailure("invalid_provider_response", context)
            self._record_failure(failure)
            raise NitradoApiError("invalid_provider_response: Nitrado returned non-JSON", failure=failure) from err

        if not isinstance(payload, dict) or payload.get("status") != "success":
            message = payload.get("message") if isinstance(payload, dict) else None
            failure_context = _request_context(
                method,
                path,
                status=response.status,
                headers=getattr(response, "headers", None),
                response_text=text,
            )
            failure = NitradoRequestFailure("provider_unsuccessful_payload", failure_context)
            self._record_failure(failure)
            raise NitradoApiError(
                f"provider_unsuccessful_payload: Nitrado rejected the request ({_safe_message(message)})",
                failure=failure,
            )

        if _account_probe:
            self.verified_account_probe_count += 1
        _LOGGER.debug(
            "Nitrado request succeeded method=%s path=%s status=%s request_id=%s",
            context.method,
            context.path,
            context.status,
            context.provider_request_id,
        )
        return payload, context

    async def _raise_classified_auth_candidate(self, candidate: NitradoRequestFailure) -> None:
        """Probe account access once and classify a service-endpoint 401/403."""

        probe_context: NitradoRequestContext | None = None
        try:
            async with self._auth_probe_lock:
                _payload, probe_context = await self._request_with_context(
                    "GET",
                    "/services",
                    _account_probe=True,
                )
        except NitradoAuthError as err:
            probe_context = err.failure.request if err.failure is not None else None
            rejected = NitradoRequestFailure(TOKEN_REJECTED, candidate.request, probe_context)
            self._record_failure(rejected)
            raise NitradoAuthError(
                f"{TOKEN_REJECTED}: independent Nitrado account probe rejected the token",
                failure=rejected,
            ) from err
        except NitradoApiError as err:
            if err.failure is not None:
                probe_context = err.failure.request
            unverified = NitradoRequestFailure(
                AUTH_CANDIDATE_UNVERIFIED,
                candidate.request,
                probe_context,
            )
            self._record_failure(unverified)
            raise NitradoEndpointAuthorizationError(
                f"{AUTH_CANDIDATE_UNVERIFIED}: endpoint rejected access and the account probe was inconclusive",
                failure=unverified,
            ) from err

        endpoint_failure = NitradoRequestFailure(
            ENDPOINT_AUTHORIZATION_FAILURE,
            candidate.request,
            probe_context,
        )
        self._record_failure(endpoint_failure)
        raise NitradoEndpointAuthorizationError(
            f"{ENDPOINT_AUTHORIZATION_FAILURE}: Nitrado {candidate.request.method} "
            f"{candidate.request.path} returned HTTP {candidate.request.status}; account token remains valid",
            failure=endpoint_failure,
        )

    def _record_failure(self, failure: NitradoRequestFailure, *, warning: bool = True) -> None:
        """Retain only the latest bounded, secret-free request failure."""

        self.last_request_failure = failure
        if warning:
            _LOGGER.warning("Nitrado request failed: %s", failure.as_dict())
        else:
            _LOGGER.debug("Optional Nitrado endpoint was unavailable: %s", failure.as_dict())

    async def service_list(self) -> list[NitradoService]:
        """Return services visible to the API token."""

        payload = await self._request("GET", "/services", _account_probe=True)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise NitradoApiError("Nitrado services payload did not contain a data object")
        services = data.get("services")
        if not isinstance(services, list):
            raise NitradoApiError("Nitrado services payload did not contain a service list")
        parsed: list[NitradoService] = []
        invalid = 0
        for service in services:
            if not isinstance(service, dict):
                invalid += 1
                continue
            try:
                parsed.append(parse_service(service))
            except NitradoApiError as err:
                invalid += 1
                _LOGGER.warning("Skipping malformed Nitrado service entry: %s", err)
        if services and not parsed:
            raise NitradoApiError(f"Nitrado service list contained no valid entries ({invalid} malformed)")
        return parsed

    async def account_identity(self) -> str:
        """Return a stable Nitrado account identity for entry deduplication."""

        payload = await self._request("GET", "/account", _optional_not_found=True)
        identity = parse_account_identity(payload)
        if identity is None:
            raise NitradoApiError("Nitrado account payload did not contain a stable account ID")
        return identity

    async def webinterface_login(self, service_id: str) -> NitradoWebinterfaceLogin:
        """Return a validated short-lived login URL for one managed service."""

        service_id = _validate_service_id(service_id)
        payload = await self._request("GET", f"/services/{service_id}/webinterface_login")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise NitradoApiError("Nitrado web-interface login payload did not contain a data object")
        url = data.get("url")
        expires_at = data.get("expires_at")
        if not isinstance(url, str) or not url:
            raise NitradoApiError("Nitrado web-interface login payload did not contain a URL")
        if isinstance(expires_at, bool) or not isinstance(expires_at, int) or expires_at <= 0:
            raise NitradoApiError("Nitrado web-interface login payload did not contain a valid expiration")
        _validate_webinterface_login_url(url, service_id)
        return NitradoWebinterfaceLogin(url=url, expires_at=expires_at)

    async def fetch_server(self, service_id: str) -> ParsedServer:
        """Fetch generic gameserver status for one service."""

        service_id, gameserver = await self._fetch_gameserver(service_id)
        return parse_gameserver(service_id, gameserver)

    async def fetch_ftp_credentials(self, service_id: str) -> NitradoFtpCredentials:
        """Return service FTP credentials without persisting or logging them."""

        _, gameserver = await self._fetch_gameserver(service_id)
        credentials = gameserver.get("credentials")
        ftp = credentials.get("ftp") if isinstance(credentials, dict) else None
        if not isinstance(ftp, dict):
            raise NitradoApiError("Nitrado gameserver payload did not contain FTP credentials")
        hostname = first_string(ftp.get("hostname"), ftp.get("host"))
        port = parse_int(ftp.get("port"))
        username = first_string(ftp.get("username"), ftp.get("user"))
        password = ftp.get("password")
        if not hostname or port is None or not username or not isinstance(password, str) or not password:
            raise NitradoApiError("Nitrado returned incomplete FTP credentials")
        if not 1 <= port <= 65535:
            raise NitradoApiError("Nitrado returned an invalid FTP port")
        secure_hint = _parse_secure_ftp_hint(ftp)
        return NitradoFtpCredentials(hostname, port, username, password, secure_hint)

    async def _fetch_gameserver(self, service_id: str) -> tuple[str, dict[str, Any]]:
        """Return one validated raw gameserver object for internal parsers."""

        service_id = _validate_service_id(service_id)
        payload = await self._request("GET", f"/services/{service_id}/gameservers")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise NitradoApiError("Nitrado gameserver payload did not contain a data object")
        gameserver = data.get("gameserver")
        if not isinstance(gameserver, dict):
            raise NitradoApiError("Nitrado gameserver payload did not contain data.gameserver")
        return service_id, gameserver

    async def fetch_players(self, service_id: str) -> tuple[str, ...]:
        """Fetch Nitrado's explicit player list for one service."""

        service_id = _validate_service_id(service_id)
        payload = await self._request("GET", f"/services/{service_id}/gameservers/games/players")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise NitradoApiError("Nitrado players payload did not contain a data object")
        players = data.get("players")
        if not isinstance(players, list):
            raise NitradoApiError("Nitrado players payload did not contain data.players")
        return parse_player_list(players)

    async def start_server(self, service_id: str, game_short: str) -> None:
        """Send Nitrado's game start command."""

        service_id = _validate_service_id(service_id)
        await self._request(
            "POST",
            f"/services/{service_id}/gameservers/games/start",
            json={"game": game_short},
        )

    async def stop_server(self, service_id: str, reason: str) -> None:
        """Send Nitrado's gameserver stop command."""

        service_id = _validate_service_id(service_id)
        reason = str(reason)[:MAX_STOP_REASON_CHARS]
        await self._request(
            "POST",
            f"/services/{service_id}/gameservers/stop",
            json={
                "message": reason,
                "stop_message": reason,
            },
        )

    async def list_native_backups(self, service_id: str) -> dict[str, Any]:
        """Return the provider-native backup inventory for one gameserver.

        This deliberately returns the bounded ``data`` object rather than
        interpreting backup age, size, or game health in the transport layer.
        """

        service_id = _validate_service_id(service_id)
        payload = await self._request("GET", f"/services/{service_id}/gameservers/backups")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise NitradoApiError("Nitrado backup inventory did not contain a data object")
        return data

    async def native_backup_restore_possible(self, service_id: str) -> bool:
        """Return Nitrado's documented provider restore-availability fact."""

        service_id = _validate_service_id(service_id)
        payload = await self._request(
            "GET",
            f"/services/{service_id}/gameservers/backups/restore_possible",
        )
        data = payload.get("data")
        possible = data.get("restore_possible") if isinstance(data, dict) else None
        if not isinstance(possible, bool):
            raise NitradoApiError("Nitrado restore availability did not contain a boolean fact")
        return possible

    async def restore_native_backup(self, service_id: str, folder: str, backup_id: str) -> dict[str, Any]:
        """Request restoration of one exact provider-native backup.

        Provider acceptance is returned as facts only.  It is not evidence
        that the intended game world is healthy or even that restore work has
        completed.
        """

        service_id = _validate_service_id(service_id)
        folder = _validate_backup_selector(folder, "folder")
        backup_id = _validate_backup_selector(backup_id, "backup ID")
        if (
            not folder[0].isalnum()
            or any(not (char.isalnum() or char in "_.-") for char in folder)
            or folder in {".", ".."}
        ):
            raise NitradoApiError("Nitrado backup folder is invalid")
        if not backup_id.isdigit():
            raise NitradoApiError("Nitrado backup ID is invalid")
        payload = await self._request(
            "POST",
            f"/services/{service_id}/gameservers/backups/gameserver",
            json={"folder": folder, "backup": backup_id},
        )
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    async def list_files(self, service_id: str, directory: str | None = None) -> dict[str, Any]:
        """List files through Nitrado's gameserver file browser."""

        service_id = _validate_service_id(service_id)
        params: dict[str, str] = {}
        if directory:
            params["dir"] = directory
        payload = await self._request("GET", f"/services/{service_id}/gameservers/file_server/list", params=params)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise NitradoApiError("Nitrado file list payload did not contain data object")
        return data

    async def download_file(self, service_id: str, path: str) -> str:
        """Download a text file through Nitrado's file browser."""

        service_id = _validate_service_id(service_id)
        payload = await self._request(
            "GET",
            f"/services/{service_id}/gameservers/file_server/download",
            params={"file": path},
        )
        url = find_url(payload)
        if not url:
            raise NitradoApiError("Nitrado file download payload did not include a download URL")
        try:
            async with asyncio.timeout(20):
                response = await _async_pinned_request(
                    self._session,
                    "GET",
                    url,
                    allow_http=False,
                    allow_redirects=False,
                )
                text = await _read_text_limited(response, MAX_EDITABLE_FILE_BYTES)
        except (TimeoutError, ClientError) as err:
            raise NitradoApiError(f"Nitrado file download failed: {err}") from err
        if response.status >= 400:
            raise NitradoApiError(f"Nitrado file download returned HTTP {response.status}")
        return text

    async def upload_text_file(self, service_id: str, path: str, content: str) -> None:
        """Upload a text file through Nitrado's file browser."""

        service_id = _validate_service_id(service_id)
        if len(content.encode("utf-8")) > MAX_EDITABLE_FILE_BYTES:
            raise NitradoApiError("Editable file upload exceeds the configured safety limit")
        directory, filename = split_remote_path(path)
        payload = await self._request(
            "POST",
            f"/services/{service_id}/gameservers/file_server/upload",
            json={"path": directory, "file": filename},
        )
        token = find_upload_token(payload)
        url = find_url(payload)
        if not token or not url:
            raise NitradoApiError("Nitrado file upload payload did not include an upload URL and token")
        try:
            async with asyncio.timeout(20):
                response = await _async_pinned_request(
                    self._session,
                    "POST",
                    url,
                    allow_http=False,
                    data=content.encode("utf-8"),
                    headers={
                        "content-type": "application/binary",
                        "token": token,
                    },
                    allow_redirects=False,
                )
                await _read_text_limited(response, MAX_UPLOAD_RESPONSE_BYTES)
        except (TimeoutError, ClientError) as err:
            raise NitradoApiError(f"Nitrado file upload failed: {err}") from err
        if response.status >= 400:
            raise NitradoApiError(f"Nitrado file upload returned HTTP {response.status}")

    async def fetch_external_json(
        self,
        url: str,
        *,
        allowed_hosts: Iterable[str],
        headers: dict[str, str] | None = None,
        timeout_seconds: int = 10,
    ) -> dict[str, Any]:
        """Fetch a game-native JSON endpoint using the shared HTTP session."""

        try:
            async with asyncio.timeout(timeout_seconds):
                response = await _async_pinned_request(
                    self._session,
                    "GET",
                    url,
                    allow_http=True,
                    allowed_hosts=allowed_hosts,
                    headers=headers or {},
                    allow_redirects=False,
                )
                text = await _read_text_limited(response, MAX_API_RESPONSE_BYTES)
        except (TimeoutError, ClientError) as err:
            raise NitradoApiError(f"External game request failed: {err}") from err

        if response.status >= 400:
            raise NitradoApiError(f"External game endpoint returned HTTP {response.status}")

        try:
            payload = json.loads(text)
        except Exception as err:
            raise NitradoApiError("External game endpoint returned non-JSON response") from err
        if not isinstance(payload, dict):
            raise NitradoApiError("External game endpoint returned malformed JSON")
        return payload


def _validate_outbound_url(
    url: str,
    *,
    allow_http: bool,
    allowed_hosts: Iterable[str] | None = None,
) -> None:
    """Reject unsafe or unexpected outbound URLs before using the HA session."""

    if not isinstance(url, str) or not url:
        raise NitradoApiError("Outbound URL is missing")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as err:
        raise NitradoApiError("Outbound URL is malformed") from err
    allowed_schemes = {"https", "http"} if allow_http else {"https"}
    if parsed.scheme.lower() not in allowed_schemes:
        raise NitradoApiError(f"Outbound URL scheme is not allowed: {parsed.scheme or 'missing'}")
    if parsed.username is not None or parsed.password is not None:
        raise NitradoApiError("Outbound URL must not contain credentials")
    if parsed.fragment:
        raise NitradoApiError("Outbound URL must not contain a fragment")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise NitradoApiError("Outbound URL host is missing")
    if port is not None and not 1 <= port <= 65535:
        raise NitradoApiError("Outbound URL port is invalid")

    if allowed_hosts is not None:
        normalized_allowed = {str(item).lower().strip().rstrip(".") for item in allowed_hosts}
        if host not in normalized_allowed:
            raise NitradoApiError(f"Outbound URL host is not declared by the selected profile: {host}")

    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".home.arpa")):
        raise NitradoApiError("Outbound URL resolves to a local-only hostname")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return
    if not address.is_global:
        raise NitradoApiError("Outbound URL uses a non-public IP address")


def _validate_webinterface_login_url(url: str, service_id: str) -> None:
    """Accept only Nitrado's documented service-bound HTTPS login destination."""

    if len(url) > 8192:
        raise NitradoApiError("Nitrado web-interface login URL is too long")
    try:
        parsed = urlsplit(url)
        port = parsed.port
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as err:
        raise NitradoApiError("Nitrado returned an invalid web-interface login URL") from err
    if parsed.scheme != "https" or parsed.hostname != "webinterface.nitrado.net":
        raise NitradoApiError("Nitrado returned an untrusted web-interface login destination")
    if parsed.username is not None or parsed.password is not None or port not in {None, 443} or parsed.fragment:
        raise NitradoApiError("Nitrado returned an unsafe web-interface login destination")
    tokens = query.get("access_token", [])
    service_ids = query.get("service_id", [])
    if len(tokens) != 1 or not tokens[0] or len(tokens[0]) > 4096:
        raise NitradoApiError("Nitrado web-interface login URL did not contain one access token")
    if service_ids != [service_id]:
        raise NitradoApiError("Nitrado web-interface login URL targeted a different service")


async def _async_validate_outbound_url(
    url: str,
    *,
    allow_http: bool,
    allowed_hosts: Iterable[str] | None = None,
) -> tuple[str, ...]:
    """Validate syntax and return every public address for request pinning."""

    _validate_outbound_url(url, allow_http=allow_http, allowed_hosts=allowed_hosts)
    parsed = urlsplit(url)
    host = str(parsed.hostname or "")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return (str(address),)
    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(
            host,
            parsed.port or (80 if parsed.scheme == "http" else 443),
            type=socket.SOCK_STREAM,
        )
    except OSError as err:
        raise NitradoApiError("Outbound URL host could not be resolved safely") from err
    resolved = {item[4][0] for item in addresses if item and len(item) > 4 and item[4]}
    if not resolved:
        raise NitradoApiError("Outbound URL host did not resolve to an address")
    for value in resolved:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as err:
            raise NitradoApiError("Outbound URL resolved to a malformed address") from err
        if not address.is_global:
            raise NitradoApiError("Outbound URL resolved to a non-public IP address")
    return tuple(sorted(resolved, key=lambda value: (":" in value, value)))


async def _async_pinned_request(
    session: Any,
    method: str,
    url: str,
    *,
    allow_http: bool,
    allowed_hosts: Iterable[str] | None = None,
    headers: dict[str, str] | None = None,
    **kwargs: Any,
) -> Any:
    """Connect only to an address validated for this exact request.

    The URL host is replaced with a validated IP while the original Host header
    and TLS server name are retained. This closes the DNS-rebinding gap between
    policy validation and aiohttp's connection-time resolution.
    """

    addresses = await _async_validate_outbound_url(
        url,
        allow_http=allow_http,
        allowed_hosts=allowed_hosts,
    )
    parsed = urlsplit(url)
    host = str(parsed.hostname or "")
    port = parsed.port
    default_port = 80 if parsed.scheme.lower() == "http" else 443
    host_header = host if port in {None, default_port} else f"{host}:{port}"
    request_headers = dict(headers or {})
    request_headers["Host"] = host_header
    last_error: ClientError | None = None
    request_fn = getattr(session, method.lower())
    for address in addresses:
        address_host = f"[{address}]" if ":" in address else address
        netloc = address_host if port is None else f"{address_host}:{port}"
        pinned_url = urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, ""))
        request_kwargs = {**kwargs, "headers": request_headers}
        if parsed.scheme.lower() == "https":
            request_kwargs["server_hostname"] = host
        try:
            return await request_fn(pinned_url, **request_kwargs)
        except ClientError as err:
            last_error = err
    if last_error is not None:
        raise last_error
    raise NitradoApiError("Outbound URL did not have a validated address")


async def _read_text_limited(response: Any, limit: int) -> str:
    """Read an HTTP response without accepting unbounded bodies."""

    try:
        headers = getattr(response, "headers", {}) or {}
        content_length = headers.get("Content-Length") or headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > limit:
                    raise NitradoApiError("HTTP response exceeded the configured safety limit")
            except ValueError:
                pass
        content = getattr(response, "content", None)
        if content is not None and callable(getattr(content, "read", None)):
            body = await content.read(limit + 1)
            if len(body) > limit:
                raise NitradoApiError("HTTP response exceeded the configured safety limit")
            charset = getattr(response, "charset", None) or "utf-8"
            try:
                return body.decode(charset)
            except (LookupError, UnicodeDecodeError) as err:
                raise NitradoApiError("HTTP response was not valid text") from err
        text = await response.text()
        if len(text.encode("utf-8")) > limit:
            raise NitradoApiError("HTTP response exceeded the configured safety limit")
        return text
    finally:
        release = getattr(response, "release", None)
        if callable(release):
            release()


def _request_context(
    method: str,
    path: str,
    *,
    status: int | None = None,
    headers: Mapping[str, Any] | None = None,
    response_text: str | None = None,
) -> NitradoRequestContext:
    """Build bounded context without retaining credentials, queries, or raw bodies."""

    normalized_path, service_id = _normalize_request_path(path)
    return NitradoRequestContext(
        method=str(method).upper()[:16],
        path=normalized_path,
        service_id=service_id,
        status=status,
        provider_request_id=_provider_request_id(headers),
        response_summary=_bounded_response_summary(response_text),
    )


def _normalize_request_path(path: str) -> tuple[str, str | None]:
    """Remove queries and separate a numeric service ID from a route template."""

    try:
        clean_path = urlsplit(str(path)).path
    except ValueError:
        clean_path = "/invalid"
    clean_path = clean_path if clean_path.startswith("/") else f"/{clean_path}"
    clean_path = re.sub(r"/{2,}", "/", clean_path)[:512]
    match = re.match(r"^/services/(\d+)(?=/|$)", clean_path)
    if match is None:
        return clean_path or "/", None
    service_id = match.group(1)
    normalized = f"/services/{{service_id}}{clean_path[match.end() :]}"
    return normalized, service_id


def _provider_request_id(headers: Mapping[str, Any] | None) -> str | None:
    """Return one bounded provider correlation identifier from an allowlist."""

    if not headers:
        return None
    lowered = {str(key).lower(): value for key, value in headers.items()}
    for key in ("x-request-id", "x-correlation-id", "request-id", "traceparent", "cf-ray"):
        value = lowered.get(key)
        if not isinstance(value, str):
            continue
        value = "".join(char for char in value.strip() if 32 <= ord(char) < 127)
        if value:
            return value[:128]
    return None


def _bounded_response_summary(text: str | None) -> str | None:
    """Summarize only allowlisted response fields and never retain a raw body."""

    if text is None:
        return None
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return f"non-json response ({len(str(text).encode('utf-8'))} bytes)"
    if not isinstance(payload, Mapping):
        return f"json {type(payload).__name__} response"
    allowed = {str(key): payload[key] for key in ("status", "code", "message", "error", "errors") if key in payload}
    if not allowed:
        return "json object response"
    try:
        rendered = json.dumps(redact_payload(allowed), ensure_ascii=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return "json error response"
    return _redact_summary_text(rendered)[:MAX_FAILURE_RESPONSE_SUMMARY_CHARS]


def _redact_summary_text(value: str) -> str:
    """Aggressively remove credentials and opaque values from provider messages."""

    value = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", value)
    value = re.sub(
        r"(?i)(authorization|api[_ -]?token|access[_ -]?token|password|secret)\s*[:=]\s*[^\s,;\"']+",
        r"\1=[redacted]",
        value,
    )
    value = re.sub(r"https?://[^\s\"']+", "[redacted-url]", value)
    value = re.sub(r"\b[A-Za-z0-9_-]{32,}\b", "[redacted-opaque]", value)
    return " ".join(value.split())


def _safe_message(value: Any) -> str:
    """Return a bounded provider message safe enough for an exception string."""

    if not isinstance(value, str) or not value.strip():
        return "no safe provider message"
    return _redact_summary_text(value)[:160]


def _validate_service_id(service_id: Any) -> str:
    """Return a numeric Nitrado service ID or fail before URL construction."""

    value = str(service_id).strip()
    if not value.isdigit():
        raise NitradoApiError("Nitrado service ID must contain only digits")
    return value


def _validate_backup_selector(value: Any, label: str) -> str:
    """Return a bounded provider backup selector without control characters."""

    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise NitradoApiError(f"Nitrado backup {label} is invalid")
    normalized = str(value).strip()
    if not normalized or len(normalized) > 128:
        raise NitradoApiError(f"Nitrado backup {label} is invalid")
    if any(ord(char) < 32 or ord(char) == 127 for char in normalized):
        raise NitradoApiError(f"Nitrado backup {label} is invalid")
    return normalized


def fallback_account_identity(services: list[NitradoService], token: str) -> str:
    """Return a token-stable one-way fallback when /account is unavailable.

    The result intentionally does not depend on the token's current service
    inventory. Inventory and token scope can change without changing accounts.
    """

    del services
    seed = "token:" + token
    return "fallback-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def parse_account_identity(payload: dict[str, Any]) -> str | None:
    """Extract a stable account/user ID from supported Nitrado payload shapes."""

    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    candidates: list[Any] = [data.get("id"), data.get("account_id"), data.get("user_id")]
    for container_key in ("user", "account"):
        container = data.get(container_key)
        if isinstance(container, dict):
            candidates.extend((container.get("id"), container.get("account_id"), container.get("user_id")))
    for value in candidates:
        if isinstance(value, bool) or value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def parse_service(service: dict[str, Any]) -> NitradoService:
    """Parse one account service entry."""

    service_id = service.get("id")
    if service_id is None:
        raise NitradoApiError("Nitrado service entry did not contain an id")
    service_id = _validate_service_id(service_id)
    details = service.get("details") if isinstance(service.get("details"), dict) else {}
    return NitradoService(
        service_id=service_id,
        name=first_string(details.get("name"), service.get("comment"), service.get("type_human")),
        game=first_string(details.get("game")),
        game_human=first_string(details.get("game_human")),
        folder_short=first_string(details.get("folder_short"), details.get("portlist_short")),
        type_human=first_string(service.get("type_human")),
        raw_redacted=redact_payload(service),
    )


def parse_gameserver(service_id: str, gameserver: dict[str, Any]) -> ParsedServer:
    """Parse details without ever guessing unknown player data as zero."""

    status = gameserver.get("status")
    if not isinstance(status, str) or not status:
        raise NitradoApiError("Gameserver status was missing or malformed")
    if len(status) > MAX_STATUS_CHARS:
        raise NitradoApiError("Gameserver status exceeded the supported field length")

    query = gameserver.get("query")
    query_valid = isinstance(query, dict)
    player_count: int | None = None
    player_max: int | None = None
    player_names: list[str] = []

    if query_valid:
        current = query.get("player_current")
        maximum = query.get("player_max")
        if isinstance(current, int) and current >= 0:
            player_count = current
        elif isinstance(current, str) and current.isdigit():
            player_count = int(current)
        else:
            query_valid = False

        if isinstance(maximum, int) and maximum >= 0:
            player_max = maximum
        elif isinstance(maximum, str) and maximum.isdigit():
            player_max = int(maximum)

        players = query.get("players")
        if isinstance(players, list):
            for player in players[:MAX_PLAYER_NAMES]:
                if isinstance(player, dict) and isinstance(player.get("name"), str):
                    player_names.append(player["name"][:MAX_PLAYER_NAME_CHARS])
                elif isinstance(player, str) and player:
                    player_names.append(player[:MAX_PLAYER_NAME_CHARS])

    game_specific = gameserver.get("game_specific") if isinstance(gameserver.get("game_specific"), dict) else {}
    query_dict = query if isinstance(query, dict) else {}
    slots = gameserver.get("slots")

    return ParsedServer(
        service_id=str(service_id),
        raw_status=status,
        server_name=first_string(query_dict.get("server_name"), gameserver.get("label"), gameserver.get("game_human")),
        address=first_string(
            query_dict.get("connect_ip"), join_host_port(gameserver.get("ip"), gameserver.get("port"))
        ),
        game_short=first_string(gameserver.get("game"), game_specific.get("folder_short")),
        game_human=first_string(gameserver.get("game_human")),
        player_count=player_count,
        player_max=player_max if player_max is not None else slots if isinstance(slots, int) else None,
        player_names=tuple(player_names),
        query_valid=query_valid,
        player_source="nitrado_query" if query_valid else None,
        raw_redacted=redact_payload({"gameserver": gameserver}),
    )


def parse_player_list(players: list[Any]) -> tuple[str, ...]:
    """Parse Nitrado's explicit player list without inventing hidden facts."""

    names: list[str] = []
    for index, player in enumerate(players[:MAX_PLAYER_NAMES], start=1):
        if isinstance(player, str) and player:
            names.append(player[:MAX_PLAYER_NAME_CHARS])
            continue
        if isinstance(player, dict):
            name = first_string(
                player.get("name"),
                player.get("username"),
                player.get("player_name"),
                player.get("nickname"),
                player.get("display_name"),
            )
            names.append((name or f"Player {index}")[:MAX_PLAYER_NAME_CHARS])
            continue
        names.append(f"Player {index}")
    return tuple(names)


def first_string(*values: Any) -> str | None:
    """Return the first non-empty string."""

    for value in values:
        if isinstance(value, str) and value:
            return value[:MAX_TEXT_FIELD_CHARS]
    return None


def parse_int(value: Any) -> int | None:
    """Parse a non-negative integer from common API shapes."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _parse_secure_ftp_hint(ftp: Mapping[str, Any]) -> bool | None:
    """Return an explicit provider secure-FTP hint when present."""

    for key in ("secure", "ssl", "tls", "ftps"):
        value = ftp.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in {0, 1}:
            return bool(value)
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"true", "yes", "1", "ftps", "tls"}:
                return True
            if lowered in {"false", "no", "0", "ftp", "plain"}:
                return False
    protocol = ftp.get("protocol")
    if isinstance(protocol, str):
        lowered = protocol.strip().lower()
        if lowered in {"ftps", "ftp+tls"}:
            return True
        if lowered == "ftp":
            return False
    return None


def join_host_port(host: Any, port: Any) -> str | None:
    """Join host/port API fields."""

    parsed_port = parse_int(port)
    if isinstance(host, str) and host and parsed_port is not None:
        return f"{host}:{parsed_port}"
    return None


def find_url(value: Any) -> str | None:
    """Find a download URL in a nested Nitrado payload."""

    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in {"url", "download_url"} and isinstance(child, str) and child.startswith("http"):
                return child
            found = find_url(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_url(child)
            if found:
                return found
    return None


def find_upload_token(value: Any) -> str | None:
    """Find an upload token in a nested Nitrado payload."""

    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() == "token" and isinstance(child, str) and child:
                return child
            found = find_upload_token(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_upload_token(child)
            if found:
                return found
    return None


def split_remote_path(path: str) -> tuple[str, str]:
    """Split an absolute Nitrado file-browser path into directory and filename."""

    normalized = str(path or "").strip().replace("\\", "/")
    if not normalized or normalized.endswith("/") or "/" not in normalized:
        raise NitradoApiError("Nitrado file upload requires a full target file path")
    directory, filename = normalized.rsplit("/", 1)
    if not filename:
        raise NitradoApiError("Nitrado file upload requires a target file name")
    return directory or "/", filename


def redact_payload(value: Any) -> Any:
    """Remove secrets and bulky credentials before logging/debugging."""

    secret_keys = {
        "authorization",
        "apitoken",
        "accesstoken",
        "refreshtoken",
        "clientsecret",
        "serverpassword",
        "password",
        "adminpassword",
        "secret",
        "token",
        "websockettoken",
        "url",
        "downloadurl",
        "ftp",
        "mysql",
        "credentials",
    }
    if is_dataclass(value) and not isinstance(value, type):
        return redact_payload({field.name: getattr(value, field.name) for field in fields(value)})
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, child in value.items():
            normalized_key = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized_key in secret_keys:
                redacted[key] = "[redacted]"
            else:
                redacted[key] = redact_payload(child)
        return redacted
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    if isinstance(value, tuple):
        return [redact_payload(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [redact_payload(item) for item in sorted(value, key=repr)]
    return value
