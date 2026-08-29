"""Config flow skeleton for Nitrado Game Server."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

try:
    from homeassistant.helpers import selector
except ModuleNotFoundError:  # pragma: no cover - local tests do not load HA.
    selector = None  # type: ignore[assignment]

from .api.nitrado import (
    NitradoApiError,
    NitradoAuthError,
    NitradoClient,
    fallback_account_identity,
)
from .const import (
    CONF_ACCOUNT_ID,
    CONF_ACCOUNT_UUID,
    CONF_API_TOKEN,
    CONF_DISCOVERY_INTERVAL,
    CONF_DISCOVERY_MODE,
    CONF_IGNORED_SERVICE_IDS,
    CONF_IMPORTED_SERVICE_IDS,
    CONF_MISSING_SERVICE_THRESHOLD,
    CONF_SETTLE_SECONDS,
    CONF_STATUS_INTERVAL,
    DEFAULT_DISCOVERY_INTERVAL,
    DEFAULT_DISCOVERY_MODE,
    DEFAULT_MISSING_SERVICE_THRESHOLD,
    DEFAULT_SETTLE_SECONDS,
    DEFAULT_STATUS_INTERVAL,
    DISCOVERY_MODES,
    DOMAIN,
)
from .discovery import (
    DISCOVERY_ENTRY_ID,
    DISCOVERY_GAME,
    DISCOVERY_SERVICE_ID,
    DISCOVERY_TITLE,
)
from .models import ManagedServiceState
from .naming import service_display_name, service_metadata_display_name
from .operation_journal import OperationIntent
from .plugins.base import (
    ProfileManifestError,
    ProfileOptionDeclaration,
    ProfileOptionType,
    ProfileStatus,
    async_invoke_profile,
    profile_read_transport,
)
from .registry_cleanup import (
    async_clear_service_issue,
    async_remove_service_registry_entries,
)
from .repairs import async_update_repair_issues
from .runtime import ServiceRuntime, runtime_profile_extension_manifest
from .service_options import (
    default_service_options,
    entry_option_update_lock,
    normalized_service_options,
    profile_option_acknowledged,
    profile_option_acknowledgements_map,
    profile_option_is_configured,
    profile_option_value,
    profile_options_map,
    service_area_id_map,
    service_id_set,
    updated_idle_toggle_options,
    updated_profile_option_acknowledgement_options,
    updated_profile_option_options,
    updated_service_options,
)

FIELD_ACTION = "action"
FIELD_AREA_ID = "area_id"
FIELD_DISPLAY_NAME = "display_name"
FIELD_ENABLED = "enabled"
FIELD_CONFIRM = "confirm"
FIELD_PROFILE_SETTING = "profile_setting"
FIELD_SERVICE_ACTION = "service_action"


@dataclass(frozen=True, slots=True)
class ValidatedAccount:
    """Validated account identity plus non-secret duplicate-detection evidence."""

    account_id: str
    visible_service_ids: frozenset[str]
    fallback_identity: bool = False


class NitradoGameServerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a Nitrado Game Server config flow."""

    VERSION = 2
    _discovered_service: dict[str, Any] | None = None
    _reauth_entry_id: str | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Handle initial setup."""

        errors: dict[str, str] = {}
        if user_input is not None:
            token = user_input[CONF_API_TOKEN].strip()
            try:
                account = await _async_validate_account(self.hass, token)
            except NitradoAuthError:
                errors["base"] = "invalid_auth"
            except NitradoApiError:
                errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "unknown"
            else:
                account_id = account.account_id
                if _account_evidence_overlaps_existing_entry(
                    self.hass,
                    account.visible_service_ids,
                ):
                    return self.async_abort(reason="already_configured")
                await self.async_set_unique_id(f"account:{account_id}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="Nitrado Game Server",
                    data={
                        CONF_API_TOKEN: token,
                        CONF_ACCOUNT_ID: account_id,
                        CONF_ACCOUNT_UUID: str(uuid.uuid4()),
                    },
                    options=default_service_options(),
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_API_TOKEN): _token_selector()}),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]):
        """Start token replacement after Home Assistant reports auth failure."""

        self._reauth_entry_id = str(self.context.get("entry_id") or "")
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None):
        """Validate and store a replacement token for the same account."""

        return await self._async_token_update_step("reauth_confirm", user_input, abort_reason="reauth_successful")

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None):
        """Allow deliberate token replacement from the integration UI."""

        self._reauth_entry_id = str(self.context.get("entry_id") or "")
        return await self._async_token_update_step("reconfigure", user_input, abort_reason="reconfigure_successful")

    async def _async_token_update_step(
        self,
        step_id: str,
        user_input: dict[str, Any] | None,
        *,
        abort_reason: str,
    ):
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(self._reauth_entry_id)
        if entry is None:
            return self.async_abort(reason="not_loaded")
        if user_input is not None:
            token = str(user_input[CONF_API_TOKEN]).strip()
            try:
                account = await _async_validate_account(self.hass, token)
            except NitradoAuthError:
                errors["base"] = "invalid_auth"
            except NitradoApiError:
                errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "unknown"
            else:
                account_id = account.account_id
                expected = entry.data.get(CONF_ACCOUNT_ID)
                expected_id = str(expected or "")
                identities_differ = bool(expected_id and expected_id != account_id)
                transition_involves_fallback = expected_id.startswith("fallback-") or account.fallback_identity
                if identities_differ:
                    if not transition_involves_fallback or not _account_evidence_matches_entry(
                        self.hass,
                        entry,
                        account.visible_service_ids,
                    ):
                        errors["base"] = "wrong_account"
                        return self.async_show_form(
                            step_id=step_id,
                            data_schema=vol.Schema({vol.Required(CONF_API_TOKEN): _token_selector()}),
                            errors=errors,
                        )
                    if expected_id.startswith("fallback-") and account.fallback_identity:
                        account_id = expected_id
                    elif not expected_id.startswith("fallback-") and account.fallback_identity:
                        # Never downgrade a previously verified stable identity
                        # merely because a replacement token cannot read /account.
                        account_id = expected_id

                if _account_evidence_overlaps_existing_entry(
                    self.hass,
                    account.visible_service_ids,
                    exclude_entry_id=entry.entry_id,
                ):
                    errors["base"] = "wrong_account"
                else:
                    data = dict(entry.data)
                    data[CONF_API_TOKEN] = token
                    data[CONF_ACCOUNT_ID] = account_id
                    self.hass.config_entries.async_update_entry(
                        entry,
                        data=data,
                        unique_id=f"account:{account_id}",
                    )
                    await self.hass.config_entries.async_reload(entry.entry_id)
                    return self.async_abort(reason=abort_reason)
        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema({vol.Required(CONF_API_TOKEN): _token_selector()}),
            errors=errors,
        )

    async def async_step_integration_discovery(self, discovery_info: dict[str, Any]):
        """Handle a service found by this integration's account discovery."""

        return await self.async_step_discovery(discovery_info)

    async def async_step_discovery(self, discovery_info: dict[str, Any]):
        """Handle a discovered unmanaged Nitrado service."""

        self._discovered_service = dict(discovery_info)
        entry_id = str(discovery_info[DISCOVERY_ENTRY_ID])
        service_id = str(discovery_info[DISCOVERY_SERVICE_ID])
        await self.async_set_unique_id(f"{entry_id}:service:{service_id}")

        coordinator = self.hass.data.get(DOMAIN, {}).get(entry_id)
        if coordinator is None:
            return self.async_abort(reason="not_loaded")
        state = coordinator.known.get(service_id)
        if state is None:
            return self.async_abort(reason="not_found")
        if state.ignored:
            return self.async_abort(reason="service_ignored")
        if not state.pending_discovery:
            return self.async_abort(reason="already_configured")

        display_name = await _async_suggested_service_name(coordinator, service_id)
        flow_title = str(discovery_info.get(DISCOVERY_TITLE) or display_name)
        self.context["title_placeholders"] = {"name": flow_title}

        return self.async_show_form(
            step_id="discovered_service",
            description_placeholders={
                "service_name": display_name,
                "game": str(discovery_info.get(DISCOVERY_GAME) or "Unknown game"),
                "service_id": service_id,
            },
            data_schema=vol.Schema(
                {
                    vol.Required(FIELD_ACTION, default="import"): vol.In(
                        {
                            "import": "Import service",
                            "ignore": "Ignore service",
                        }
                    ),
                    vol.Optional(FIELD_DISPLAY_NAME, default=display_name): str,
                    vol.Optional(FIELD_AREA_ID): _area_selector(),
                }
            ),
        )

    async def async_step_discovered_service(self, user_input: dict[str, Any] | None = None):
        """Import or ignore a discovered unmanaged Nitrado service."""

        if self._discovered_service is None:
            return self.async_abort(reason="not_found")
        if user_input is None:
            return await self.async_step_discovery(self._discovered_service)

        entry_id = str(self._discovered_service[DISCOVERY_ENTRY_ID])
        service_id = str(self._discovered_service[DISCOVERY_SERVICE_ID])
        entry = _entry_by_id(self.hass, entry_id)
        coordinator = self.hass.data.get(DOMAIN, {}).get(entry_id)
        if entry is None or coordinator is None:
            return self.async_abort(reason="not_loaded")

        action = user_input[FIELD_ACTION]
        if action == "import":
            await coordinator.import_service(service_id)
            _persist_service_choice(
                self.hass,
                entry,
                import_service_id=service_id,
                service_area_id=user_input.get(FIELD_AREA_ID),
                service_display_name=user_input.get(FIELD_DISPLAY_NAME),
            )
            reason = "service_imported"
        else:
            cancelled = coordinator.ignore_service(service_id)
            if cancelled:
                await asyncio.gather(*cancelled, return_exceptions=True)
            _persist_service_choice(self.hass, entry, ignore_service_id=service_id)
            reason = "service_ignored"

        await async_update_repair_issues(self.hass, entry, coordinator)
        coordinator.active_discovery_flow_ids.discard(f"{entry_id}:service:{service_id}")
        await self.hass.config_entries.async_reload(entry.entry_id)
        if action == "ignore":
            async_remove_service_registry_entries(self.hass, service_id, entry_id)
            async_clear_service_issue(self.hass, entry, service_id)
        return self.async_abort(reason=reason)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Return the options flow."""

        return NitradoGameServerOptionsFlow()


class NitradoGameServerOptionsFlow(config_entries.OptionsFlow):
    """Handle Nitrado Game Server options."""

    _service_action: tuple[str, str] | None = None
    _profile_setting_action: tuple[str, str] | None = None
    _pending_import: tuple[str, str | None, str | None] | None = None
    _onboarding_options: dict[str, Any] | None = None
    _onboarding_queue: list[tuple[str, str, ProfileOptionDeclaration]] | None = None

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        """Show the options menu."""

        return self.async_show_menu(
            step_id="init",
            menu_options=["settings", "services", "profile_settings"],
        )

    async def async_step_profile_settings(self, user_input: dict[str, Any] | None = None):
        """Expose profile settings through Home Assistant's standard options flow."""

        coordinator = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        if coordinator is None:
            return self.async_abort(reason="not_loaded")
        choices = _standard_profile_option_choices(coordinator)
        if not choices:
            return self.async_abort(reason="no_profile_settings")
        if user_input is not None:
            raw = str(user_input[FIELD_PROFILE_SETTING])
            self._profile_setting_action = _split_profile_setting(raw)
            return await self.async_step_profile_option()
        return self.async_show_form(
            step_id="profile_settings",
            data_schema=vol.Schema({vol.Required(FIELD_PROFILE_SETTING, default=next(iter(choices))): vol.In(choices)}),
        )

    async def async_step_profile_option(self, user_input: dict[str, Any] | None = None):
        """Edit one administrator-managed profile option with explicit confirmation."""

        coordinator = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        if coordinator is None:
            return self.async_abort(reason="not_loaded")
        target = _resolve_profile_option_target(coordinator, self._profile_setting_action)
        if target is None:
            return self.async_abort(reason="profile_setting_unavailable")
        service_id, profile_id, declaration = target
        options = normalized_service_options(dict(self.config_entry.options))
        current = profile_option_value(options, profile_id, declaration.key, service_id, declaration.default)
        acknowledged = _profile_option_is_acknowledged(options, profile_id, declaration, service_id)
        errors: dict[str, str] = {}
        if user_input is not None:
            enabled = bool(user_input[FIELD_ENABLED])
            changing = enabled != current or not acknowledged
            if declaration.confirmation_required and changing and user_input.get(FIELD_CONFIRM) is not True:
                errors[FIELD_CONFIRM] = "confirmation_required"
            else:
                updated = await _async_commit_standard_profile_option(
                    self.hass,
                    self.config_entry,
                    coordinator,
                    profile_id,
                    declaration,
                    service_id,
                    enabled,
                )
                return self.async_create_entry(title="", data=updated)
        return self.async_show_form(
            step_id="profile_option",
            description_placeholders={
                "service_name": _service_label(coordinator, service_id),
                "option_name": declaration.name,
                "option_description": declaration.description,
                "idle_shutdown_impact": (
                    "Disabling this also turns Auto Shutdown off. Re-enabling it does not re-arm Auto Shutdown."
                    if declaration.idle_shutdown_required
                    else ""
                ),
            },
            data_schema=vol.Schema(
                {
                    vol.Required(FIELD_ENABLED, default=bool(current)): bool,
                    vol.Optional(FIELD_CONFIRM, default=False): bool,
                }
            ),
            errors=errors,
        )

    async def async_step_settings(self, user_input: dict[str, Any] | None = None):
        """Manage account polling/discovery settings."""

        if user_input is not None:
            data = normalized_service_options(dict(self.config_entry.options))
            data.update(user_input)
            return self.async_create_entry(title="", data=normalized_service_options(data))

        current = self.config_entry.options
        return self.async_show_form(
            step_id="settings",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_DISCOVERY_MODE,
                        default=current.get(CONF_DISCOVERY_MODE, DEFAULT_DISCOVERY_MODE),
                    ): vol.In(sorted(DISCOVERY_MODES)),
                    vol.Optional(
                        CONF_DISCOVERY_INTERVAL,
                        default=current.get(CONF_DISCOVERY_INTERVAL, DEFAULT_DISCOVERY_INTERVAL),
                    ): vol.All(int, vol.Range(min=60)),
                    vol.Optional(
                        CONF_STATUS_INTERVAL,
                        default=current.get(CONF_STATUS_INTERVAL, DEFAULT_STATUS_INTERVAL),
                    ): vol.All(int, vol.Range(min=30)),
                    vol.Optional(
                        CONF_MISSING_SERVICE_THRESHOLD,
                        default=current.get(CONF_MISSING_SERVICE_THRESHOLD, DEFAULT_MISSING_SERVICE_THRESHOLD),
                    ): vol.All(int, vol.Range(min=1)),
                    vol.Optional(
                        CONF_SETTLE_SECONDS,
                        default=current.get(CONF_SETTLE_SECONDS, DEFAULT_SETTLE_SECONDS),
                    ): vol.All(int, vol.Range(min=0)),
                }
            ),
        )

    async def async_step_services(self, user_input: dict[str, Any] | None = None):
        """Manage discovered/missing services."""

        coordinator = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        if coordinator is None:
            return self.async_abort(reason="not_loaded")

        errors: dict[str, str] = {}
        if user_input is not None:
            options = normalized_service_options(dict(self.config_entry.options))
            action = ""
            service_id = ""
            service_action = user_input.get(FIELD_SERVICE_ACTION)
            if service_action:
                action, service_id = _split_service_action(str(service_action))
                if action == "import":
                    self._service_action = (action, service_id)
                    return await self.async_step_import_service()
                elif action == "ignore":
                    cancelled = coordinator.ignore_service(service_id)
                    if cancelled:
                        await asyncio.gather(*cancelled, return_exceptions=True)
                    options = updated_service_options(dict(self.config_entry.options), ignore_service_id=service_id)
                elif action == "remove":
                    cancelled = coordinator.remove_service(service_id)
                    if cancelled:
                        await asyncio.gather(*cancelled, return_exceptions=True)
                    options = updated_service_options(dict(self.config_entry.options), remove_service_id=service_id)
                elif action == "refresh":
                    await coordinator.async_refresh_discovery()
                    return await self.async_step_services()

            self.hass.config_entries.async_update_entry(self.config_entry, options=options)
            await async_update_repair_issues(self.hass, self.config_entry, coordinator)
            await self.hass.config_entries.async_reload(self.config_entry.entry_id)
            if action in {"ignore", "remove"}:
                async_remove_service_registry_entries(self.hass, service_id, self.config_entry.entry_id)
                async_clear_service_issue(self.hass, self.config_entry, service_id)
            return self.async_create_entry(title="", data=options)

        try:
            await coordinator.async_refresh_discovery()
        except NitradoApiError:
            errors["base"] = "cannot_connect"
        except Exception:
            errors["base"] = "unknown"

        pending = {
            service_id: _service_label(coordinator, service_id)
            for service_id, state in coordinator.known.items()
            if state.pending_discovery and not state.ignored
        }
        missing = {
            service_id: _service_label(coordinator, service_id)
            for service_id, state in coordinator.known.items()
            if not state.available and not state.pending_discovery and not state.ignored
        }
        managed = {
            service_id: _service_label(coordinator, service_id)
            for service_id, state in coordinator.known.items()
            if state.available and not state.pending_discovery and not state.ignored
        }
        ignored = {
            service_id: _service_label(coordinator, service_id)
            for service_id, state in coordinator.known.items()
            if state.ignored
        }

        choices: dict[str, str] = {}
        for service_id, label in pending.items():
            choices[f"import:{service_id}"] = f"Import {label}"
            choices[f"ignore:{service_id}"] = f"Ignore {label}"
        for service_id, label in missing.items():
            choices[f"remove:{service_id}"] = f"Remove {label}"
        for service_id, label in managed.items():
            choices[f"ignore:{service_id}"] = f"Stop managing {label}"
        for service_id, label in ignored.items():
            choices[f"import:{service_id}"] = f"Manage {label} again"
        choices["refresh:"] = "Refresh service discovery"

        return self.async_show_form(
            step_id="services",
            data_schema=vol.Schema({vol.Required(FIELD_SERVICE_ACTION, default=next(iter(choices))): vol.In(choices)}),
            errors=errors,
        )

    async def async_step_import_service(self, user_input: dict[str, Any] | None = None):
        """Collect name/area before importing a discovered service."""

        if self._service_action is None:
            return await self.async_step_services()
        _, service_id = self._service_action
        coordinator = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        if coordinator is None:
            return self.async_abort(reason="not_loaded")

        if user_input is not None:
            options = updated_service_options(
                dict(self.config_entry.options),
                import_service_id=service_id,
                service_area_id=user_input.get(FIELD_AREA_ID),
                service_display_name=user_input.get(FIELD_DISPLAY_NAME),
            )
            onboarding = await _async_onboarding_profile_options(coordinator, service_id)
            if onboarding:
                self._pending_import = (
                    service_id,
                    user_input.get(FIELD_AREA_ID),
                    user_input.get(FIELD_DISPLAY_NAME),
                )
                self._onboarding_options = options
                self._onboarding_queue = onboarding
                return await self.async_step_profile_onboarding()
            return await self._async_finalize_import(options)

        display_name = await _async_suggested_service_name(coordinator, service_id)
        area_ids = service_area_id_map(dict(self.config_entry.options))
        return self.async_show_form(
            step_id="import_service",
            description_placeholders={
                "service_name": _service_label(coordinator, service_id),
                "service_id": service_id,
            },
            data_schema=vol.Schema(
                {
                    vol.Optional(FIELD_DISPLAY_NAME, default=display_name): str,
                    vol.Optional(FIELD_AREA_ID, default=area_ids.get(service_id, "")): _area_selector(),
                }
            ),
        )

    async def async_step_profile_onboarding(self, user_input: dict[str, Any] | None = None):
        """Resolve profile security decisions while importing a game service."""

        coordinator = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        if coordinator is None or not self._onboarding_queue or self._onboarding_options is None:
            return self.async_abort(reason="profile_setting_unavailable")
        service_id, profile_id, declaration = self._onboarding_queue[0]
        errors: dict[str, str] = {}
        if user_input is not None:
            if declaration.confirmation_required and user_input.get(FIELD_CONFIRM) is not True:
                errors[FIELD_CONFIRM] = "confirmation_required"
            else:
                self._onboarding_options = _updated_standard_profile_option(
                    self._onboarding_options,
                    profile_id,
                    declaration,
                    service_id,
                    bool(user_input[FIELD_ENABLED]),
                )
                self._onboarding_queue.pop(0)
                if self._onboarding_queue:
                    return await self.async_step_profile_onboarding()
                return await self._async_finalize_import(self._onboarding_options)
        return self.async_show_form(
            step_id="profile_onboarding",
            description_placeholders={
                "service_name": _service_label(coordinator, service_id),
                "option_name": declaration.name,
                "option_description": declaration.description,
                "idle_shutdown_impact": (
                    "Declining leaves player reporting and Auto Shutdown unavailable."
                    if declaration.idle_shutdown_required
                    else ""
                ),
            },
            data_schema=vol.Schema(
                {
                    vol.Required(FIELD_ENABLED, default=bool(declaration.default)): bool,
                    vol.Optional(FIELD_CONFIRM, default=False): bool,
                }
            ),
            errors=errors,
        )

    async def _async_finalize_import(self, options: dict[str, Any]):
        """Persist a fully resolved service import and reload the entry."""

        if self._service_action is None:
            return self.async_abort(reason="profile_setting_unavailable")
        _, service_id = self._service_action
        coordinator = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        if coordinator is None:
            return self.async_abort(reason="not_loaded")
        await coordinator.import_service(service_id)
        self.hass.config_entries.async_update_entry(self.config_entry, options=options)
        await async_update_repair_issues(self.hass, self.config_entry, coordinator)
        await self.hass.config_entries.async_reload(self.config_entry.entry_id)
        return self.async_create_entry(title="", data=options)


def _service_label(coordinator, service_id: str) -> str:
    service = coordinator.discovered_services.get(service_id)
    runtime = coordinator.services.get(service_id)
    if runtime:
        return f"{service_display_name(runtime, coordinator.options.service_display_names)} ({service_id})"
    if service:
        return f"{service_metadata_display_name(service, fallback_id=service_id)} ({service_id})"
    return str(service_id)


def _standard_profile_option_choices(coordinator: Any) -> dict[str, str]:
    """Return standard Options-flow profile settings for managed services."""

    choices: dict[str, str] = {}
    for service_id in sorted(coordinator.services):
        runtime = coordinator.get_runtime(service_id)
        if runtime.profile is None:
            continue
        try:
            declarations = runtime_profile_extension_manifest(runtime).profile_options
        except ProfileManifestError:
            continue
        for declaration in declarations:
            if declaration.standard_options and declaration.option_type == ProfileOptionType.BOOLEAN:
                profile_name = str(runtime.profile.name or runtime.profile.profile_id)
                choices[f"{service_id}:{declaration.key}"] = (
                    f"{_service_label(coordinator, service_id)} → {profile_name} → {declaration.name}"
                )
    return choices


def _split_profile_setting(value: str) -> tuple[str, str]:
    """Split one internal standard-profile-setting selector value."""

    service_id, separator, option_key = value.partition(":")
    if not separator or not service_id.isdigit() or not option_key:
        raise vol.Invalid("invalid profile setting")
    return service_id, option_key


def _resolve_profile_option_target(
    coordinator: Any,
    action: tuple[str, str] | None,
) -> tuple[str, str, ProfileOptionDeclaration] | None:
    """Resolve one current profile option without trusting stale flow state."""

    if action is None:
        return None
    service_id, option_key = action
    try:
        runtime = coordinator.get_runtime(service_id)
    except Exception:
        return None
    if runtime.profile is None:
        return None
    try:
        declaration = next(
            (
                item
                for item in runtime_profile_extension_manifest(runtime).profile_options
                if item.key == option_key and item.standard_options and item.option_type == ProfileOptionType.BOOLEAN
            ),
            None,
        )
    except ProfileManifestError:
        return None
    if declaration is None:
        return None
    return service_id, str(runtime.profile.profile_id), declaration


async def _async_onboarding_profile_options(
    coordinator: Any,
    service_id: str,
) -> list[tuple[str, str, ProfileOptionDeclaration]]:
    """Select a pending service profile and return its onboarding decisions."""

    service = coordinator.discovered_services.get(str(service_id))
    if service is None:
        return []
    server = None
    # Service metadata can still identify bundled profiles. If richer metadata
    # is temporarily unavailable, a later profile change creates the same
    # unresolved-requirement Repair and remains fail-closed.
    with suppress(NitradoApiError):
        server = await coordinator.client.fetch_server(str(service_id))
    runtime = ServiceRuntime(ManagedServiceState(str(service_id)))
    runtime.update_service(service, server, select_profile_now=False)
    if server is not None:
        runtime.update_server(
            server,
            observed_at=coordinator.now_fn(),
            select_profile_now=False,
        )
    await runtime.async_select_profile(service, server)
    if runtime.profile is None:
        return []
    try:
        declarations = runtime_profile_extension_manifest(runtime).profile_options
    except ProfileManifestError:
        return []
    profile_id = str(runtime.profile.profile_id)
    return [
        (str(service_id), profile_id, declaration)
        for declaration in declarations
        if declaration.onboarding and declaration.option_type == ProfileOptionType.BOOLEAN
    ]


def _profile_option_is_acknowledged(
    options: dict[str, Any],
    profile_id: str,
    declaration: ProfileOptionDeclaration,
    service_id: str,
) -> bool:
    """Return whether the active acknowledgement revision is resolved."""

    revision = declaration.acknowledgement_revision
    if revision is None:
        return profile_option_is_configured(options, profile_id, declaration.key, service_id)
    return profile_option_acknowledged(options, profile_id, declaration.key, service_id, revision)


def _updated_standard_profile_option(
    options: dict[str, Any],
    profile_id: str,
    declaration: ProfileOptionDeclaration,
    service_id: str,
    enabled: bool,
) -> dict[str, Any]:
    """Persist one confirmed setting and its safety side effects."""

    updated = updated_profile_option_options(
        options,
        profile_id,
        declaration.key,
        service_id,
        enabled,
    )
    if declaration.acknowledgement_revision is not None:
        updated = updated_profile_option_acknowledgement_options(
            updated,
            profile_id,
            declaration.key,
            service_id,
            declaration.acknowledgement_revision,
        )
    if declaration.idle_shutdown_required and not enabled:
        updated = updated_idle_toggle_options(
            updated,
            service_id,
            "idle_shutdown_service_ids",
            False,
        )
    return normalized_service_options(updated)


async def _async_commit_standard_profile_option(
    hass: Any,
    entry: Any,
    coordinator: Any,
    profile_id: str,
    declaration: ProfileOptionDeclaration,
    service_id: str,
    enabled: bool,
) -> dict[str, Any]:
    """Commit an Options-flow profile setting through durable arbitration."""

    intent = OperationIntent(
        "profile-option",
        declaration.key,
        generation=f"{profile_id}-{coordinator.get_runtime(service_id).profile_generation}",
        expected_evidence="Home Assistant config-entry options contain the confirmed profile value",
    )
    async with (
        entry_option_update_lock(hass, entry.entry_id),
        coordinator.async_operation(service_id, intent) as reservation,
    ):
        runtime = coordinator.get_runtime(service_id)
        if runtime.profile is None or str(runtime.profile.profile_id) != profile_id:
            raise ValueError("The selected game profile changed; reopen the options flow")
        updated = _updated_standard_profile_option(
            normalized_service_options(dict(entry.options)),
            profile_id,
            declaration,
            service_id,
            enabled,
        )
        await reservation.async_mark_dispatched()
        hass.config_entries.async_update_entry(entry, options=updated)
        coordinator.options = replace(
            coordinator.options,
            profile_options=profile_options_map(updated),
            profile_option_acknowledgements=profile_option_acknowledgements_map(updated),
            idle_shutdown_service_ids=frozenset(service_id_set(updated, "idle_shutdown_service_ids")),
        )
        await coordinator.async_profile_options_changed(
            runtime,
            "Profile security setting changed; pending shutdown revoked",
        )
        await async_update_repair_issues(hass, entry, coordinator)
        await reservation.async_mark_verifying()
        return updated


def _suggested_service_name(coordinator, service_id: str) -> str:
    """Return the default user-facing service name for import forms."""

    if explicit := coordinator.options.service_display_names.get(service_id):
        return explicit
    runtime = coordinator.services.get(service_id)
    if runtime:
        return service_display_name(runtime, coordinator.options.service_display_names)
    return service_metadata_display_name(coordinator.discovered_services.get(service_id), fallback_id=service_id)


async def _async_suggested_service_name(coordinator, service_id: str) -> str:
    """Return the best available suggested service name for import forms."""

    fallback = _suggested_service_name(coordinator, service_id)
    service = coordinator.discovered_services.get(service_id)
    if service is None:
        return fallback

    server = None
    try:
        server = await coordinator.client.fetch_server(service_id)
    except NitradoApiError:
        return fallback

    runtime = ServiceRuntime(ManagedServiceState(service_id))
    runtime.update_service(service, server, select_profile_now=False)
    runtime.update_server(server, observed_at=coordinator.now_fn(), select_profile_now=False)

    await runtime.async_select_profile(service, server)
    try:
        suggested = await async_invoke_profile(
            runtime.profile.suggest_display_name,
            profile_read_transport(
                coordinator.client,
                service_id,
                file_transport=coordinator.profile_transport,
            ),
            service,
            server,
        )
    except (AttributeError, NitradoApiError, TimeoutError):
        suggested = None
    if type(suggested) is str and suggested:
        runtime.extra["profile_status"] = ProfileStatus(display_name=suggested)

    return service_display_name(runtime, coordinator.options.service_display_names)


def _area_selector() -> Any:
    """Return a Home Assistant area selector, or a local-test fallback."""

    if selector is None:
        return str
    return selector.AreaSelector()


def _token_selector() -> Any:
    """Return a password-style token selector without exposing stored secrets."""

    if selector is None:
        return str
    return selector.TextSelector(selector.TextSelectorConfig(type="password"))


async def _async_validate_account(hass: Any, token: str) -> ValidatedAccount:
    """Validate a token and return identity plus non-secret account evidence."""

    client = NitradoClient(async_get_clientsession(hass), token)
    services = await client.service_list()
    visible_service_ids = frozenset(service.service_id for service in services)
    try:
        return ValidatedAccount(
            account_id=await client.account_identity(),
            visible_service_ids=visible_service_ids,
        )
    except NitradoAuthError:
        raise
    except NitradoApiError:
        # Some Nitrado token scopes may list services but not expose /account.
        # The fallback deliberately fingerprints the token rather than the
        # mutable visible-service inventory.
        return ValidatedAccount(
            account_id=fallback_account_identity(services, token),
            visible_service_ids=visible_service_ids,
            fallback_identity=True,
        )


def _account_evidence_overlaps_existing_entry(
    hass: Any,
    visible_service_ids: frozenset[str],
    *,
    exclude_entry_id: str | None = None,
) -> bool:
    """Return whether visible services overlap an existing account entry.

    Service IDs are globally unique. Overlap is therefore strong same-account
    evidence across stable/fallback identity transitions and prevents two
    coordinators from controlling the same service.
    """

    if not visible_service_ids:
        return False
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.entry_id == exclude_entry_id:
            continue
        if _entry_visible_service_ids(hass, entry) & visible_service_ids:
            return True
    return False


def _account_evidence_matches_entry(hass: Any, entry: Any, visible_service_ids: frozenset[str]) -> bool:
    """Return whether replacement-token evidence matches the configured entry."""

    return bool(_entry_visible_service_ids(hass, entry) & visible_service_ids)


def _entry_visible_service_ids(hass: Any, entry: Any) -> set[str]:
    """Return persisted and runtime service IDs known for one account entry."""

    known_service_ids = service_id_set(dict(entry.options), CONF_IMPORTED_SERVICE_IDS)
    known_service_ids.update(service_id_set(dict(entry.options), CONF_IGNORED_SERVICE_IDS))
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is not None:
        known_service_ids.update(str(service_id) for service_id in coordinator.known)
        known_service_ids.update(str(service_id) for service_id in coordinator.discovered_services)
    return known_service_ids


def _split_service_action(value: str) -> tuple[str, str]:
    """Split a services-options action value into action and service ID."""

    action, _, service_id = value.partition(":")
    return action, service_id


def _entry_by_id(hass: Any, entry_id: str) -> Any | None:
    """Find a config entry by ID."""

    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.entry_id == entry_id:
            return entry
    return None


def _persist_service_choice(
    hass: Any,
    entry: Any,
    *,
    import_service_id: str | None = None,
    ignore_service_id: str | None = None,
    service_area_id: str | None = None,
    service_display_name: str | None = None,
) -> None:
    """Persist service import/ignore choices in config entry options."""

    hass.config_entries.async_update_entry(
        entry,
        options=updated_service_options(
            dict(entry.options),
            import_service_id=import_service_id,
            ignore_service_id=ignore_service_id,
            service_area_id=service_area_id,
            service_display_name=service_display_name,
        ),
    )
