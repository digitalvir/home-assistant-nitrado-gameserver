"""Real aiohttp contracts for profile-owned cockpit delivery."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nitrado_gameserver.cockpit import CockpitAssetRecord, register_cockpit_assets
from custom_components.nitrado_gameserver.const import DOMAIN
from custom_components.nitrado_gameserver.models import ServiceIdentity
from custom_components.nitrado_gameserver.views import (
    CockpitAssetView,
    _core_entity_ids_payload,
    _json_error,
    _surface_descriptors_payload,
)


@pytest.mark.asyncio
async def test_digest_asset_is_native_import_compatible_without_authentication(hass) -> None:
    content = b"export const cockpitMetadata = Object.freeze({});\n"
    record = CockpitAssetRecord(
        owner_domain="fixture_owner",
        profile_id="fixture_profile",
        asset_key="fixture_cockpit",
        digest="a" * 64,
        content=content,
    )
    register_cockpit_assets(hass, (record,))

    view = CockpitAssetView()
    assert view.requires_auth is False
    response = await view.get(
        SimpleNamespace(app={"hass": hass}),
        record.owner_domain,
        record.asset_key,
        record.digest,
    )

    assert response.body == content
    assert response.content_type == "text/javascript"
    assert response.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.parametrize(
    ("status", "expected_type"),
    ((409, "HTTPConflict"), (410, "HTTPGone")),
)
def test_structured_cockpit_errors_preserve_http_status(status: int, expected_type: str) -> None:
    error = _json_error("bounded failure", code="test_code", status=status)

    assert error.status == status
    assert type(error).__name__ == expected_type
    assert json.loads(error.text) == {"error": {"code": "test_code", "message": "bounded failure"}}


def test_cockpit_entity_resolution_uses_composite_account_identity(hass) -> None:
    registry = er.async_get(hass)
    identity = ServiceIdentity("123", "entry-a")
    entry = MockConfigEntry(domain=DOMAIN, entry_id="entry-a", data={})
    entry.add_to_hass(hass)
    core = registry.async_get_or_create(
        "switch",
        DOMAIN,
        identity.entity_unique_id("auto_shutdown"),
        config_entry=entry,
    )
    profile = registry.async_get_or_create(
        "button",
        DOMAIN,
        identity.entity_unique_id("save", profile_id="fixture"),
        config_entry=entry,
    )

    core_payload = _core_entity_ids_payload(hass, "123", "entry-a")
    surfaces = _surface_descriptors_payload(
        hass,
        "123",
        "entry-a",
        SimpleNamespace(profile_id="fixture"),
        ({"control_entities": [{"entity_key": "save", "platform": "button"}]},),
    )

    assert core_payload["auto_shutdown"] == core.entity_id
    assert surfaces[0]["control_entities"][0]["entity_id"] == profile.entity_id
    assert _core_entity_ids_payload(hass, "123", "entry-b") == {}
