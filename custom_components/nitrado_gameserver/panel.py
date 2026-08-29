"""Home Assistant frontend host for generic profile extension surfaces."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .const import DOMAIN

PANEL_URL_PATH = "nitrado-game-servers"
PANEL_ASSET_VERSION = "2026-8-29-20145"
PANEL_ELEMENT = f"nitrado-game-server-panel-{PANEL_ASSET_VERSION}"
PANEL_STATIC_URL = f"/{DOMAIN}_static/nitrado-game-server-panel.js"
PANEL_MODULE_URL = f"{PANEL_STATIC_URL}?v={PANEL_ASSET_VERSION}"
_PANEL_REGISTERED = f"{DOMAIN}_panel_registered"
_STATIC_REGISTERED = f"{DOMAIN}_panel_static_registered"


def device_configuration_url(service_id: str, account_entry_id: str | None = None) -> str:
    """Return the HA deep link that opens the selected service cockpit."""

    if account_entry_id:
        return f"homeassistant://{PANEL_URL_PATH}/{account_entry_id}/{service_id}"
    return f"homeassistant://{PANEL_URL_PATH}/{service_id}"


async def async_register_extension_panel(hass: Any) -> None:
    """Register the administrator server-management panel and its static module."""

    # Home Assistant can run without the optional frontend integration. Core
    # server control must remain fully functional in that supported shape.
    if "frontend" not in hass.config.components:
        return

    from homeassistant.components import frontend
    from homeassistant.components.http import StaticPathConfig

    if not hass.data.get(_STATIC_REGISTERED):
        module_path = Path(__file__).parent / "frontend" / "nitrado-game-server-panel.js"
        await hass.http.async_register_static_paths([StaticPathConfig(PANEL_STATIC_URL, str(module_path), False)])
        hass.data[_STATIC_REGISTERED] = True

    if hass.data.get(_PANEL_REGISTERED):
        return
    frontend.async_register_built_in_panel(
        hass,
        component_name="custom",
        sidebar_title="Nitrado Servers",
        sidebar_icon="mdi:server",
        frontend_url_path=PANEL_URL_PATH,
        config={
            "_panel_custom": {
                "name": PANEL_ELEMENT,
                "module_url": PANEL_MODULE_URL,
                "embed_iframe": False,
                "trust_external": False,
                "handle_safe_area": False,
            }
        },
        require_admin=True,
        show_in_sidebar=True,
    )
    hass.data[_PANEL_REGISTERED] = True


def async_unregister_extension_panel(hass: Any) -> None:
    """Remove the panel when the final account entry unloads."""

    if not hass.data.pop(_PANEL_REGISTERED, False):
        return
    if "frontend" not in hass.config.components:
        return
    from homeassistant.components import frontend

    frontend.async_remove_panel(hass, PANEL_URL_PATH)
