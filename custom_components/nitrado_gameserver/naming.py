"""Human-facing naming helpers for Nitrado services."""

from __future__ import annotations

from typing import Any

from .api.nitrado import NitradoService, first_string
from .runtime import ServiceRuntime

BAD_SERVER_NAMES = {
    "palserver hosted by nitrado.net",
    "server hosted by nitrado.net",
}


def service_display_name(runtime: ServiceRuntime, explicit_names: dict[str, str] | None = None) -> str:
    """Return the best human-facing name for a service runtime."""

    service_id = runtime.state.service_id
    explicit = explicit_names.get(service_id) if explicit_names else None
    if useful_name(explicit):
        return explicit.strip()

    profile_status = runtime.extra.get("profile_status") if isinstance(runtime.extra, dict) else None
    profile_name = getattr(profile_status, "display_name", None)
    if useful_name(profile_name):
        return profile_name.strip()

    server = runtime.server
    service = runtime.service
    for candidate in (
        server.server_name if server else None,
        service.name if service else None,
    ):
        if useful_name(candidate):
            return candidate.strip()

    game_human = first_string(
        server.game_human if server else None,
        service.game_human if service else None,
    )
    if useful_name(game_human):
        return f"{game_human.strip()} Server"

    game_short = first_string(
        server.game_short if server else None,
        service.game if service else None,
        service.folder_short if service else None,
    )
    if useful_name(game_short):
        return f"{game_short.strip()} Server"

    return f"Nitrado service {service_id}"


def service_metadata_display_name(service: NitradoService | None, *, fallback_id: str | None = None) -> str:
    """Return the best human-facing name from service-list metadata alone."""

    if service is not None:
        for candidate in (service.name, service.game_human):
            if useful_name(candidate):
                return f"{candidate.strip()} Server" if candidate == service.game_human else candidate.strip()

        for candidate in (service.game, service.folder_short, service.type_human):
            if useful_name(candidate):
                return f"{candidate.strip()} Server"

    if fallback_id:
        return f"Nitrado service {fallback_id}"
    return "Nitrado service"


def useful_name(value: Any) -> bool:
    """Return true when a candidate name is useful to humans."""

    if not isinstance(value, str):
        return False
    cleaned = value.strip()
    if len(cleaned) <= 2:
        return False
    lowered = cleaned.lower()
    return lowered not in BAD_SERVER_NAMES
