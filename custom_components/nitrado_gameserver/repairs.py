"""Home Assistant Repairs support for Nitrado Game Server."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Literal

from .const import DOMAIN
from .coordinator import NitradoAccountCoordinator
from .filesystem_journal import JournalIssue
from .native_backup_journal import NativeRestoreFact, NativeRestoreIssue
from .operation_journal import OperationIntent
from .provider_runtime import get_provider_runtime
from .runtime import runtime_profile_extension_manifest
from .service_options import (
    entry_option_update_lock,
    profile_option_acknowledgements_map,
    profile_options_map,
    service_id_set,
    updated_idle_toggle_options,
    updated_profile_option_acknowledgement_options,
    updated_profile_option_options,
)


@dataclass(slots=True, frozen=True)
class RepairIssue:
    """A desired Home Assistant repair issue."""

    issue_id: str
    translation_key: str
    translation_placeholders: dict[str, str]
    severity: Literal["error", "warning"] = "error"
    is_fixable: bool = False
    data: dict[str, str] | None = None


@dataclass(slots=True, frozen=True)
class RepairIssuePlan:
    """Desired repair issue changes."""

    desired: dict[str, RepairIssue]
    stale_issue_ids: tuple[str, ...]


def build_repair_issue_plan(
    coordinator: NitradoAccountCoordinator,
    *,
    known_issue_ids: set[str] | None = None,
    issue_prefix: str = "",
    provider_grant_store_error: str | None = None,
    native_backup_facts: tuple[NativeRestoreFact, ...] = (),
) -> RepairIssuePlan:
    """Return desired repair issues for coordinator discovery state."""

    desired: dict[str, RepairIssue] = {}
    for service_id in _missing_services(coordinator):
        issue_id = f"{issue_prefix}missing_service_{service_id}"
        desired[issue_id] = RepairIssue(
            issue_id=issue_id,
            translation_key="missing_service",
            translation_placeholders={
                "service_id": service_id,
                "service_name": _service_label(coordinator, service_id),
            },
        )

    for service_id in sorted(coordinator.services):
        for declaration in coordinator.unacknowledged_profile_options(service_id):
            runtime = coordinator.get_runtime(service_id)
            profile_id = str(runtime.profile.profile_id) if runtime.profile is not None else "unknown"
            issue_id = f"{issue_prefix}profile_option_unacknowledged_{service_id}_{declaration.key}"
            desired[issue_id] = RepairIssue(
                issue_id=issue_id,
                translation_key="profile_option_acknowledgement_required",
                translation_placeholders={
                    "service_id": service_id,
                    "service_name": _service_label(coordinator, service_id),
                    "option_name": declaration.name,
                    "option_description": declaration.description,
                },
                severity="warning",
                is_fixable=True,
                data={
                    "action": "profile_option_acknowledgement",
                    "entry_id": issue_prefix.removesuffix("_"),
                    "service_id": service_id,
                    "profile_id": profile_id,
                    "option_key": declaration.key,
                    "option_name": declaration.name,
                    "option_description": declaration.description,
                },
            )

    filesystem = getattr(coordinator, "profile_transport", None)
    observed_status = getattr(filesystem, "observed_status", None)
    if callable(observed_status):
        for service_id in sorted(getattr(coordinator, "services", {})):
            ftp = observed_status(service_id).get("ftp", {})
            failure_count = int(ftp.get("failure_count") or 0)
            failure_code = str(ftp.get("last_failure_code") or "")
            if failure_count < 2:
                continue
            if failure_code == "plaintext_consent_required":
                translation_key = "ftp_plaintext_consent_required"
            elif failure_code in {"credentials_rejected", "credentials_or_endpoint_unavailable"}:
                translation_key = "ftp_credentials_unavailable"
            else:
                translation_key = "ftp_transport_unavailable"
            issue_id = f"{issue_prefix}{translation_key}_{service_id}"
            desired[issue_id] = RepairIssue(
                issue_id=issue_id,
                translation_key=translation_key,
                translation_placeholders={
                    "service_id": service_id,
                    "service_name": _service_label(coordinator, service_id),
                },
            )

    for fact in coordinator.filesystem_recovery_facts:
        service_id = fact.service.service_id if fact.service is not None else "unknown"
        issue_id = f"{issue_prefix}filesystem_{fact.issue.value}_{fact.transaction_id}"
        desired[issue_id] = RepairIssue(
            issue_id=issue_id,
            translation_key=_filesystem_translation_key(fact.issue),
            translation_placeholders={
                "service_id": service_id,
                "transaction_id": fact.transaction_id,
                "operation": fact.operation.value if fact.operation is not None else "unknown",
                "state": fact.state.value if fact.state is not None else "unknown",
            },
            is_fixable=fact.issue is not JournalIssue.CORRUPT_METADATA,
            data=(
                {
                    "action": (
                        "filesystem_retry" if fact.issue is JournalIssue.RECOVERY_REQUIRED else "filesystem_acknowledge"
                    ),
                    "entry_id": issue_prefix.removesuffix("_"),
                    "service_id": service_id,
                    "transaction_id": fact.transaction_id,
                }
                if fact.issue is not JournalIssue.CORRUPT_METADATA
                else None
            ),
        )

    if provider_grant_store_error:
        issue_id = f"{issue_prefix}provider_grant_store_corrupt"
        desired[issue_id] = RepairIssue(
            issue_id=issue_id,
            translation_key="provider_grant_store_corrupt",
            translation_placeholders={},
        )

    for fact in native_backup_facts:
        issue_id = f"{issue_prefix}native_backup_{fact.issue.value}_{fact.operation_id}"
        target = fact.target
        desired[issue_id] = RepairIssue(
            issue_id=issue_id,
            translation_key=(
                "native_backup_journal_corrupt"
                if fact.issue is NativeRestoreIssue.CORRUPT_METADATA
                else "native_backup_restore_review_required"
            ),
            translation_placeholders={
                "service_id": target.service_id if target is not None else "unknown",
                "operation_id": fact.operation_id,
                "state": fact.state.value if fact.state is not None else "unknown",
                "failure_code": fact.failure_code.value if fact.failure_code is not None else "unknown",
                "folder": target.folder if target is not None else "unknown",
                "backup_id": target.backup_id if target is not None else "unknown",
            },
            is_fixable=True,
            data={
                "action": (
                    "native_backup_reset_corrupt"
                    if fact.issue is NativeRestoreIssue.CORRUPT_METADATA
                    else "native_backup_acknowledge"
                ),
                "entry_id": issue_prefix.removesuffix("_"),
                "service_id": target.service_id if target is not None else "",
                "operation_id": fact.operation_id,
            },
        )

    stale = tuple(sorted((known_issue_ids or set()) - set(desired)))
    return RepairIssuePlan(desired=desired, stale_issue_ids=stale)


async def async_update_repair_issues(hass: Any, entry: Any, coordinator: NitradoAccountCoordinator) -> None:
    """Create/update/clear Home Assistant Repairs issues for one config entry."""

    try:
        from homeassistant.helpers import issue_registry as ir
    except ModuleNotFoundError:  # pragma: no cover - local tests do not load HA.
        return

    issue_prefix = f"{entry.entry_id}_"
    registry = ir.async_get(hass)
    durable_issue_ids = {
        issue_id for domain, issue_id in registry.issues if domain == DOMAIN and issue_id.startswith(issue_prefix)
    }
    known_issue_ids = set(getattr(coordinator, "active_repair_issue_ids", set())) | durable_issue_ids
    provider_runtime = get_provider_runtime(hass)
    native_backup_facts = (
        await provider_runtime.async_native_backup_facts(entry.entry_id) if provider_runtime is not None else ()
    )
    plan = build_repair_issue_plan(
        coordinator,
        known_issue_ids=known_issue_ids,
        issue_prefix=issue_prefix,
        provider_grant_store_error=(provider_runtime.authority.storage_error if provider_runtime is not None else None),
        native_backup_facts=native_backup_facts,
    )

    ir.async_delete_issue(hass, DOMAIN, f"{issue_prefix}discovered_services")

    for issue_id in plan.stale_issue_ids:
        ir.async_delete_issue(hass, DOMAIN, issue_id)

    for issue in plan.desired.values():
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue.issue_id,
            is_fixable=issue.is_fixable,
            severity=ir.IssueSeverity(issue.severity),
            translation_key=issue.translation_key,
            translation_placeholders=issue.translation_placeholders,
            data=issue.data,
        )

    coordinator.active_repair_issue_ids = set(plan.desired)


def _missing_services(coordinator: NitradoAccountCoordinator) -> tuple[str, ...]:
    return tuple(
        sorted(
            service_id
            for service_id, state in coordinator.known.items()
            if not state.available and not state.pending_discovery and not state.ignored
        )
    )


def _service_label(coordinator: NitradoAccountCoordinator, service_id: str) -> str:
    service = coordinator.discovered_services.get(service_id)
    runtime = coordinator.services.get(service_id)
    if service and service.name:
        return f"{service.name} ({service_id})"
    if runtime and runtime.service and runtime.service.name:
        return f"{runtime.service.name} ({service_id})"
    if runtime and runtime.server and runtime.server.server_name:
        return f"{runtime.server.server_name} ({service_id})"
    return service_id


def _filesystem_translation_key(issue: JournalIssue) -> str:
    return {
        JournalIssue.RECOVERY_REQUIRED: "filesystem_recovery_required",
        JournalIssue.RECOVERY_BLOB_MISSING: "filesystem_recovery_blob_missing",
        JournalIssue.RECOVERY_BLOB_CORRUPT: "filesystem_recovery_blob_corrupt",
        JournalIssue.CORRUPT_METADATA: "filesystem_journal_corrupt",
    }[issue]


try:
    from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
except ModuleNotFoundError:  # pragma: no cover - pure tests do not load HA.

    class RepairsFlow:  # type: ignore[no-redef]
        """Import fallback for source-only tests."""

    RepairsFlowResult = Any  # type: ignore[misc,assignment]


class NitradoProviderRepairFlow(RepairsFlow):
    """Execute one explicit provider recovery or truthful incident closure."""

    def __init__(self, repair_data: dict[str, str]) -> None:
        self._repair_data = repair_data

    async def async_step_init(self, user_input: dict[str, str] | None = None) -> RepairsFlowResult:
        return await self.async_step_confirm(user_input)

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None) -> RepairsFlowResult:
        import voluptuous as vol

        if self._repair_data.get("action") == "profile_option_acknowledgement":
            return await self.async_step_profile_option(user_input)

        if user_input is None:
            return self.async_show_form(step_id="confirm", data_schema=vol.Schema({}))
        try:
            await _async_execute_provider_repair(self.hass, self._repair_data)
        except Exception:  # HA presents a retryable form; details stay in logs.
            import logging

            logging.getLogger(__name__).exception("Nitrado provider repair action failed")
            return self.async_show_form(
                step_id="confirm",
                data_schema=vol.Schema({}),
                errors={"base": "repair_failed"},
            )
        return self.async_create_entry(data={})

    async def async_step_profile_option(self, user_input: dict[str, Any] | None = None) -> RepairsFlowResult:
        """Resolve one explicit profile security decision."""

        import voluptuous as vol

        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get("confirm") is not True:
                errors["confirm"] = "confirmation_required"
            else:
                repair_data = dict(self._repair_data)
                repair_data["enabled"] = "true" if user_input.get("enabled") is True else "false"
                try:
                    await _async_execute_provider_repair(self.hass, repair_data)
                except Exception:
                    import logging

                    logging.getLogger(__name__).exception("Nitrado profile-option repair failed")
                    errors["base"] = "repair_failed"
                else:
                    return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="profile_option",
            description_placeholders={
                "service_name": self._repair_data.get("service_id", "unknown"),
                "option_name": self._repair_data.get("option_name", "Profile setting"),
                "option_description": self._repair_data.get("option_description", ""),
            },
            data_schema=vol.Schema(
                {
                    vol.Required("enabled", default=False): bool,
                    vol.Optional("confirm", default=False): bool,
                }
            ),
            errors=errors,
        )


async def async_create_fix_flow(
    hass: Any,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create an exact, issue-bound provider repair flow."""

    del hass, issue_id
    if not isinstance(data, dict):
        raise ValueError("Nitrado repair issue is missing its exact action data")
    normalized = {str(key): str(value) for key, value in data.items() if value is not None}
    return NitradoProviderRepairFlow(normalized)


async def _async_execute_provider_repair(hass: Any, data: dict[str, str]) -> None:
    action = data.get("action")
    entry_id = data.get("entry_id", "")
    coordinator = hass.data.get(DOMAIN, {}).get(entry_id)
    entry = hass.config_entries.async_get_entry(entry_id)
    if coordinator is None or entry is None:
        raise ValueError("The exact Nitrado account is no longer loaded")

    if action == "filesystem_retry":
        service_id = data.get("service_id", "")
        await coordinator.async_recover_filesystem_transactions(service_id)
    elif action == "filesystem_acknowledge":
        service_id = data.get("service_id", "")
        transaction_id = data.get("transaction_id", "")
        fact = next(
            (
                item
                for item in coordinator.filesystem_recovery_facts
                if item.transaction_id == transaction_id
                and item.service is not None
                and item.service.service_id == service_id
                and item.issue in {JournalIssue.RECOVERY_BLOB_MISSING, JournalIssue.RECOVERY_BLOB_CORRUPT}
            ),
            None,
        )
        if fact is None or coordinator.filesystem_journal is None:
            raise ValueError("The exact irrecoverable filesystem transaction is no longer active")
        intent = OperationIntent(
            "filesystem-acknowledge",
            transaction_id,
            expected_evidence="filesystem journal records the explicit administrator acknowledgement",
        )
        async with coordinator.async_operation(service_id, intent) as reservation:
            await reservation.async_mark_dispatched()
            await coordinator.filesystem_journal.async_acknowledge_unresolved(transaction_id)
            await coordinator.async_refresh_filesystem_recovery_facts()
            await reservation.async_mark_verifying()
    elif action in {"native_backup_acknowledge", "native_backup_reset_corrupt"}:
        provider_runtime = get_provider_runtime(hass)
        if provider_runtime is None:
            raise ValueError("The Nitrado provider connector is unavailable")
        if action == "native_backup_acknowledge":
            await provider_runtime.async_acknowledge_native_backup_unknown(
                entry_id,
                data.get("operation_id", ""),
            )
        else:
            await provider_runtime.async_reset_corrupt_native_backup_journal(entry_id)
    elif action == "profile_option_acknowledgement":
        service_id = data.get("service_id", "")
        profile_id = data.get("profile_id", "")
        option_key = data.get("option_key", "")
        runtime = coordinator.get_runtime(service_id)
        if runtime.profile is None or str(runtime.profile.profile_id) != profile_id:
            raise ValueError("The profile requiring acknowledgement is no longer active")
        declaration = next(
            (item for item in runtime_profile_extension_manifest(runtime).profile_options if item.key == option_key),
            None,
        )
        if declaration is None or declaration.acknowledgement_revision is None:
            raise ValueError("The profile requirement is no longer active")
        enabled = data.get("enabled") == "true"
        intent = OperationIntent(
            "profile-option",
            option_key,
            generation=f"{profile_id}-{runtime.profile_generation}",
            expected_evidence="Home Assistant config-entry options contain the repaired acknowledgement",
        )
        async with (
            entry_option_update_lock(hass, entry_id),
            coordinator.async_operation(service_id, intent) as reservation,
        ):
            options = updated_profile_option_options(
                dict(entry.options),
                profile_id,
                option_key,
                service_id,
                enabled,
            )
            options = updated_profile_option_acknowledgement_options(
                options,
                profile_id,
                option_key,
                service_id,
                declaration.acknowledgement_revision,
            )
            if declaration.idle_shutdown_required and not enabled:
                options = updated_idle_toggle_options(
                    options,
                    service_id,
                    "idle_shutdown_service_ids",
                    False,
                )
            await reservation.async_mark_dispatched()
            coordinator.options = replace(
                coordinator.options,
                profile_options=profile_options_map(options),
                profile_option_acknowledgements=profile_option_acknowledgements_map(options),
                idle_shutdown_service_ids=frozenset(service_id_set(options, "idle_shutdown_service_ids")),
            )
            hass.config_entries.async_update_entry(entry, options=options)
            await coordinator.async_profile_options_changed(
                runtime,
                "Profile security setting changed; pending shutdown revoked",
            )
            await reservation.async_mark_verifying()
    else:
        raise ValueError("Unsupported Nitrado repair action")
    await async_update_repair_issues(hass, entry, coordinator)
