"""Test-only separately packaged companion integration."""

from custom_components.nitrado_gameserver.plugins import api as profile_api
from custom_components.nitrado_gameserver.provider_api import (
    PROVIDER_CONNECTOR_API_VERSION,
    ProviderFileReadRequest,
    ProviderScope,
    ProviderServiceRef,
    async_register_provider_consumer,
)

COCKPIT_DIGEST = "bd060781562d7d8a13b1380265a2262379ccdfe9c19f6fc0dbf4de5ddefcfd4b"


class CompanionHarnessProfile(profile_api.BaseGameProfile):
    """Profile registered through the public API from a separate package."""

    api_version = profile_api.PROFILE_API_VERSION
    profile_id = "companion_harness"
    name = "Companion Harness"
    supported_games = ("harnessgame",)

    def matches(self, service, server=None):
        return profile_api.MatchResult(service.game == "harnessgame", confidence=1.0)

    def cockpit(self):
        """Declare a cockpit only when the installed core exposes that API."""

        if not all(hasattr(profile_api, name) for name in ("COCKPIT_API_VERSION", "CockpitDeclaration")):
            return None
        return profile_api.CockpitDeclaration(
            key="companion_harness",
            name="Companion Harness",
            cockpit_api_version=profile_api.COCKPIT_API_VERSION,
            asset_key="companion_cockpit",
            frontend_revision=f"sha256:{COCKPIT_DIGEST}",
            default_route="overview",
            route_keys=("overview",),
        )


def profile_registration_kwargs(api=profile_api):
    """Return additive cockpit kwargs only when the installed core supports them."""

    if not all(hasattr(api, name) for name in ("COCKPIT_API_VERSION", "CockpitDeclaration", "LocalCockpitAsset")):
        return {}
    return {
        "owner_domain": "nitrado_companion",
        "cockpit_assets": {
            "companion_cockpit": api.LocalCockpitAsset(
                package_resource="frontend/companion-cockpit.js",
                sha256=COCKPIT_DIGEST,
            )
        },
    }


async def async_register_companion_profile(hass, api=profile_api):
    """Register with cockpit support or fall back to the unchanged v1 call."""

    return await api.async_register_profile(
        hass,
        CompanionHarnessProfile,
        **profile_registration_kwargs(api),
    )


async def async_setup_entry(hass, entry):
    """Register the companion and bind its cleanup to this config entry."""

    unregister = await async_register_companion_profile(hass)
    entry.async_on_unload(unregister)
    account_entry_id = entry.data.get("account_entry_id")
    service_id = entry.data.get("service_id")
    root = entry.data.get("root")
    if account_entry_id and service_id and isinstance(root, str):
        lease = await async_register_provider_consumer(
            hass,
            api_version=PROVIDER_CONNECTOR_API_VERSION,
            consumer_domain=entry.domain,
            consumer_entry_id=entry.entry_id,
        )
        entry.async_on_unload(lease.async_close)
        connector = await lease.async_get_connector(
            ProviderServiceRef(str(account_entry_id), str(service_id)),
            scopes={ProviderScope.FILESYSTEM_READ},
            roots={root},
        )
        content = await connector.async_read_file(ProviderFileReadRequest(f"{root}/settings.ini", max_bytes=4096))
        hass.data.setdefault(entry.domain, {})[entry.entry_id] = {
            "lease": lease,
            "connector": connector,
            "content": content,
        }
    return True


async def async_unload_entry(hass, entry):
    """Let Home Assistant execute registered unload callbacks."""

    hass.data.get(entry.domain, {}).pop(entry.entry_id, None)
    return True
