"""Home Assistant discovered-item support for pending Nitrado services."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .api.nitrado import NitradoApiError
from .const import DOMAIN
from .coordinator import NitradoAccountCoordinator
from .models import ManagedServiceState
from .naming import service_display_name, service_metadata_display_name
from .plugins.base import ProfileStatus, async_invoke_profile, profile_read_transport
from .runtime import ServiceRuntime

DISCOVERY_ENTRY_ID = "entry_id"
DISCOVERY_SERVICE_ID = "service_id"
DISCOVERY_SERVICE_NAME = "service_name"
DISCOVERY_GAME = "game"
DISCOVERY_TITLE = "title"


@dataclass(slots=True, frozen=True)
class DiscoveryFlowRequest:
    """A desired Home Assistant discovery flow for one pending service."""

    flow_key: str
    entry_id: str
    service_id: str
    service_name: str
    game: str
    title: str


def build_discovery_flow_requests(
    entry: Any,
    coordinator: NitradoAccountCoordinator,
) -> tuple[DiscoveryFlowRequest, ...]:
    """Return pending services that should appear as HA discovered items."""

    requests: list[DiscoveryFlowRequest] = []
    for service_id, state in sorted(coordinator.known.items()):
        if not state.pending_discovery or state.ignored:
            continue
        service = coordinator.discovered_services.get(service_id)
        service_name = service_metadata_display_name(service, fallback_id=service_id)
        game = service.game_human or service.game if service else ""
        title = discovery_title(service_name=service_name, game=game, service_id=service_id)
        flow_key = f"{entry.entry_id}:service:{service_id}"
        requests.append(
            DiscoveryFlowRequest(
                flow_key=flow_key,
                entry_id=entry.entry_id,
                service_id=service_id,
                service_name=service_name,
                game=game,
                title=title,
            )
        )
    return tuple(requests)


async def async_update_discovery_flows(hass: Any, entry: Any, coordinator: NitradoAccountCoordinator) -> None:
    """Create Home Assistant discovered items for pending unmanaged services."""

    try:
        from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY
    except ModuleNotFoundError:  # pragma: no cover - local tests do not load HA.
        return

    if not _entry_coordinator_active(hass, entry, coordinator):
        return
    desired = build_discovery_flow_requests(entry, coordinator)
    desired_keys = {request.flow_key for request in desired}
    coordinator.active_discovery_flow_ids.intersection_update(desired_keys)

    for request in desired:
        if request.flow_key in coordinator.active_discovery_flow_ids:
            continue
        title = await _async_enriched_discovery_title(coordinator, request)
        if not _entry_coordinator_active(hass, entry, coordinator):
            return
        current_keys = {item.flow_key for item in build_discovery_flow_requests(entry, coordinator)}
        if request.flow_key not in current_keys:
            continue
        await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": SOURCE_INTEGRATION_DISCOVERY,
                "entry_id": request.entry_id,
                "title_placeholders": {"name": title},
            },
            data={
                DISCOVERY_ENTRY_ID: request.entry_id,
                DISCOVERY_SERVICE_ID: request.service_id,
                DISCOVERY_SERVICE_NAME: request.service_name,
                DISCOVERY_GAME: request.game,
                DISCOVERY_TITLE: title,
            },
        )
        coordinator.active_discovery_flow_ids.add(request.flow_key)


async def async_abort_entry_discovery_flows(hass: Any, entry_id: str) -> None:
    """Abort only pending discovery flows owned by one removed account."""

    from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY

    flows = tuple(
        hass.config_entries.flow.async_progress_by_handler(
            DOMAIN,
            include_uninitialized=True,
        )
    )
    for flow in flows:
        context = flow.get("context") if isinstance(flow, dict) else None
        data = flow.get("data") if isinstance(flow, dict) else None
        if not isinstance(context, dict) or context.get("source") != SOURCE_INTEGRATION_DISCOVERY:
            continue
        owner = context.get("entry_id") if isinstance(context, dict) else None
        if owner is None and isinstance(data, dict):
            owner = data.get(DISCOVERY_ENTRY_ID)
        if str(owner or "") == str(entry_id) and (flow_id := flow.get("flow_id")):
            hass.config_entries.flow.async_abort(flow_id)


def _entry_coordinator_active(hass: Any, entry: Any, coordinator: NitradoAccountCoordinator) -> bool:
    """Return whether discovery work still belongs to a live config entry."""

    return hass.data.get(DOMAIN, {}).get(entry.entry_id) is coordinator and not coordinator.shutting_down


def discovery_title(*, service_name: str, game: str, service_id: str) -> str:
    """Return a compact one-line title for Home Assistant discovered cards."""

    parts: list[str] = []
    if service_name:
        parts.append(service_name)
    if game and game not in service_name:
        parts.append(game)
    if service_id:
        parts.append(f"ID {service_id}")
    return " - ".join(parts) if parts else "Nitrado service"


async def _async_enriched_discovery_title(
    coordinator: NitradoAccountCoordinator,
    request: DiscoveryFlowRequest,
) -> str:
    """Return the best title available before HA renders a discovered item."""

    service = coordinator.discovered_services.get(request.service_id)
    if service is None:
        return request.title

    try:
        server = await coordinator.client.fetch_server(request.service_id)
    except NitradoApiError:
        return request.title

    runtime = ServiceRuntime(ManagedServiceState(request.service_id))
    runtime.update_service(service, server, select_profile_now=False)
    runtime.update_server(server, observed_at=coordinator.now_fn(), select_profile_now=False)

    await runtime.async_select_profile(service, server)
    try:
        suggested = await async_invoke_profile(
            runtime.profile.suggest_display_name,
            profile_read_transport(
                coordinator.client,
                request.service_id,
                file_transport=coordinator.profile_transport,
            ),
            service,
            server,
        )
    except (AttributeError, NitradoApiError, TimeoutError):
        suggested = None
    if type(suggested) is str and suggested:
        runtime.extra["profile_status"] = ProfileStatus(display_name=suggested)

    service_name = service_display_name(runtime, coordinator.options.service_display_names)
    game = server.game_human or service.game_human or service.game or request.game
    return discovery_title(service_name=service_name, game=game or "", service_id=request.service_id)
