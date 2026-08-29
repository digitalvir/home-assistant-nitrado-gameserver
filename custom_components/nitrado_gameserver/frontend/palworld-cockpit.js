const digestFromUrl = () => {
  const match = new URL(import.meta.url).pathname.match(/\/([0-9a-f]{64})\.js$/);
  return match ? `sha256:${match[1]}` : "development";
};

export const cockpitMetadata = Object.freeze({
  cockpitApiVersion: 1,
  frontendRevision: digestFromUrl(),
  key: "palworld",
});

const escapeHtml = (value) => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

const formatBytes = (value, labels) => {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes < 0) return labels.unavailable;
  if (bytes < 1024) return `${bytes} ${labels.bytes}`;
  const units = labels.units;
  let amount = bytes;
  let unit = -1;
  do { amount /= 1024; unit += 1; } while (amount >= 1024 && unit < units.length - 1);
  return `${amount.toFixed(amount >= 10 ? 1 : 2)} ${units[unit]}`;
};

const ROUTES = Object.freeze([
  ["overview", "Overview"],
  ["auto-shutdown", "Auto Shutdown"],
  ["save-games", "Save games"],
  ["game-settings", "Game settings"],
]);
const ROUTE_KEYS = new Set(ROUTES.map(([key]) => key));
const PALWORLD_TRANSLATIONS = Object.freeze({
  en: Object.freeze({
    "route.overview": "Overview",
    "route.auto-shutdown": "Auto Shutdown",
    "route.save-games": "Save games",
    "route.game-settings": "Game settings",
    "navigation.section": "Palworld section",
    "category.all": "All Settings",
    "category.favorites": "Essentials",
    "category.world": "World & progression",
    "category.players": "Players",
    "category.pals": "Pals",
    "category.combat": "Combat & death",
    "category.bases": "Bases & building",
    "category.multiplayer": "Multiplayer & guilds",
    "category.server": "Server & access",
    "category.performance": "Performance",
    "category.other": "Other & new settings",
    "editor.title": "Palworld settings",
    "editor.search": "Search settings",
    "editor.category": "Setting category",
    "editor.close": "Close",
    "save.title": "Save-game bundles",
    "reporting.title": "Player reporting connection",
    "common.unavailable": "Unavailable", "common.enabled": "Enabled", "common.disabled": "Disabled", "common.on": "ON", "common.off": "OFF", "common.yes": "Yes", "common.no": "No", "common.value": "Value", "common.save": "Save", "common.retry": "Retry", "common.processing": "Processing…", "common.settings": "Settings", "common.unknown_time": "Unknown time",
    "unit.bytes": "B", "unit.kibibytes": "KiB", "unit.mebibytes": "MiB", "unit.gibibytes": "GiB", "unit.ticks_per_second": "ticks/sec", "unit.pals": "Pals", "unit.bases": "bases", "unit.centimeters": "cm", "unit.port": "port", "unit.hours": "hours", "unit.minutes": "minutes",
    "shell.title": "Palworld", "shell.sections": "Palworld server sections",
    "reporting.reason_missing": "The saved reporting decision has no current explanation.", "reporting.decision_unavailable": "The saved reporting decision is unavailable.", "reporting.verification_missing": "No current verification detail is available.", "reporting.verification_unavailable": "Player reporting verification is unavailable.",
    "reporting.off": "Off", "reporting.action_needed": "Action needed", "reporting.status_unavailable": "Status unavailable", "reporting.check_setup": "Check setup", "reporting.a11y_off": "Game settings, player reporting off", "reporting.a11y_action": "Game settings, action needed for player reporting", "reporting.a11y_unavailable": "Game settings, player reporting status unavailable", "reporting.a11y_attention": "Game settings, player reporting needs attention", "reporting.off_detail": "Plaintext HTTP is off; live player reporting and Auto Shutdown cannot work.", "reporting.action_detail": "Choose whether to allow Palworld's plaintext HTTP connection.",
    "progress.files_total": "{done} of {total} files", "progress.files_done": "{done} files complete", "progress.bytes_total": "{done} of {total}", "progress.bytes_read": "{done} read", "progress.elapsed": "{seconds}s elapsed", "progress.prepare": "Preparing the save package…", "progress.milestone": "{message} {percent}% complete.",
    "feedback.invalid_timing": "Enter a valid timing value before saving.", "feedback.update_auto": "Updating Auto Shutdown…", "feedback.cancel_shutdown": "Cancelling pending shutdown…", "feedback.save_timing": "Saving timing…", "feedback.accepted": "Home Assistant accepted the request; current state will confirm the result.", "feedback.failed": "The request failed safely.",
    "clipboard.unavailable": "Clipboard access is unavailable.", "clipboard.copied": "Address copied.", "clipboard.manual": "Could not copy automatically. Select the address and copy it manually.",
    "reporting.saving": "Saving the player reporting decision…", "reporting.saved": "Player reporting decision saved. Verification will follow current server state.", "reporting.refresh_unavailable": "Player reporting decision saved, but its refreshed status is unavailable. Reload this page before making another change.", "reporting.not_saved": "The decision was not saved; the confirmed setting is unchanged.",
    "prerequisite.refresh": "Refresh the server status first.", "prerequisite.stop_save": "Stop Palworld first so every save file belongs to one consistent moment.", "prerequisite.operation": "Resolve the active or uncertain provider operation first.", "prerequisite.refresh_editor": "Refresh the server status before previewing changes.", "prerequisite.stop_editor": "Stop Palworld before previewing or applying changes.", "prerequisite.operation_editor": "Resolve the active or uncertain provider operation before previewing.",
    "save.starting": "Starting the live-save snapshot…", "save.downloaded": "Downloaded {filename}.", "save.default_filename": "the Palworld save bundle", "save.download_failed": "The save bundle was not downloaded safely.", "save.checking": "Checking the uploaded ZIP…", "save.inspecting": "Inspecting the ZIP and comparing it with the stopped server…", "save.no_review": "Home Assistant did not return a save-bundle review.", "save.review_changed": "ZIP inspected. Review the exact save-file overlay below.", "save.review_identical": "ZIP inspected. It does not change the current save.", "save.inspect_failed": "The ZIP could not be inspected safely.", "save.restore_title": "Restore this reviewed save?", "save.restore_help": "{replaced} file(s) will be replaced and {added} added. A verified full rollback snapshot will be kept first.", "save.restore_confirm": "Restore reviewed save", "save.restoring": "Restoring and verifying the reviewed save tree…", "save.restored": "Save restored and verified. Audit {audit}.", "save.audit_recorded": "recorded", "save.restore_failed": "The save bundle was not restored safely.",
    "draft.too_large": "This draft is too large for browser-session recovery. Keep this window open until you finish.", "draft.unavailable": "Browser-session draft recovery is unavailable. Keep this window open until you finish.", "draft.invalid": "An invalid saved draft was discarded safely.", "draft.recovered": "Recovered {count} unsaved setting change{suffix} from this browser session.{detail}", "draft.partial": "Recovery was partial because sensitive or changed-schema fields are never restored from browser storage.",
    "editor.loading_operation": "Loading current settings and recovery history…", "editor.no_model": "Home Assistant did not return the safe structured settings model.", "editor.history_unavailable": "Recovery history is unavailable.", "editor.open_failed": "The settings file could not be opened safely.", "editor.loaded": "Settings loaded.", "editor.retrying": "Retrying settings load…", "editor.discard_title": "Discard unsaved Palworld settings?", "editor.discard_help": "The current draft will be removed from this browser session.", "editor.discard_confirm": "Discard changes", "editor.validating": "Validating the current draft…", "editor.no_preview": "Home Assistant did not return a settings preview.", "editor.review_ready": "Review ready.", "editor.preview_blocked": "Preview blocked. Resolve the listed issues.", "editor.preview_failed": "The settings preview failed safely.", "editor.apply_title": "Apply validated Palworld settings?", "editor.apply_help": "A verified backup will be created first. Restart Palworld before expecting the changes to take effect.", "editor.apply_confirm": "Apply settings", "editor.applying": "Applying and verifying the validated settings…", "editor.applied_restart": "Settings applied with a verified backup. Restart Palworld before expecting the changes.", "editor.applied": "Settings applied with a verified backup.", "editor.apply_failed": "The settings were not applied safely.", "editor.rollback_title": "Restore this verified settings backup?", "editor.rollback_help": "The current file will be backed up first. Restart Palworld before expecting the restored settings.", "editor.rollback_confirm": "Restore settings backup", "editor.rolling_back": "Restoring and verifying the selected settings backup…", "editor.rolled_back": "The verified backup was restored. Restart Palworld before expecting the restored settings.", "editor.rollback_failed": "The rollback failed safely.", "editor.undo_title": "Undo all settings changes?", "editor.undo_help": "{count} unsaved change{suffix} will be discarded.", "editor.undo_confirm": "Undo all changes",
    "editor.description": "Human controls for PalWorldSettings.ini · {schema}", "editor.loading_schema": "loading schema", "editor.close_a11y": "Close Palworld settings editor", "editor.loading": "Loading the current settings and recovery history…", "editor.unavailable": "Editor unavailable", "editor.find": "Find and filter settings", "editor.search_placeholder": "Name, key, effect, or category", "editor.scope": "Setting scope", "editor.scope_all": "All", "editor.scope_changed": "Changed", "editor.scope_nondefault": "Non-default", "editor.scope_issues": "Issues", "editor.undo_all": "Undo all", "editor.categories": "Setting categories", "editor.count": "{count} setting{suffix} shown · unknown keys are preserved and remain editable.", "editor.no_results": "No settings found", "editor.no_results_help": "The filters have successfully hidden absolutely everything. Magnificent. Useless, but magnificent.", "editor.clear_filters": "Clear search and filters",
    "setting.changed": "Changed", "setting.nondefault": "Non-default", "setting.shipped": "Shipped, undocumented", "setting.provider": "Provider or new", "setting.performance": "Performance", "setting.review": "Review carefully", "setting.issue": "Issue", "setting.existing_hidden": "Existing value hidden", "setting.not_configured": "Not configured", "setting.cleared": "Cleared", "setting.new_hidden": "New value hidden", "setting.empty": "empty", "setting.saved_proposed": "Saved → proposed: {saved} → {proposed} · {impact}", "setting.impact_performance": "May affect performance", "setting.impact_gameplay": "Gameplay or server behavior", "setting.default": "Palworld 1.0 default: {value}", "setting.no_default": "No documented default", "setting.undo": "Undo", "setting.use_default": "Use default", "setting.fix_pvp": "Enable all required PvP settings", "setting.stored": "Stored value is {state}", "setting.configured": "configured", "setting.empty_state": "empty", "setting.keep": "Keep existing value", "setting.replace": "Replace value", "setting.clear": "Clear value", "setting.new_value": "New value", "setting.unrecognized": "{value} (current, unrecognized)",
    "validation.secret": "Enter a replacement value or choose Clear instead.", "validation.number": "Enter a finite number.", "validation.minimum": "Minimum allowed here is {min}.", "validation.maximum": "Maximum allowed here is {max}.", "validation.pvp": "Pocketpair's trial PvP mode also requires player damage and defense against other guilds.", "validation.fast_travel": "Base-only fast travel cannot work while fast travel is disabled.",
    "overview.players": "{current} of {maximum} players online", "overview.player_unavailable": "Player data unavailable", "overview.online": "Online: {players}", "overview.server": "Palworld server", "overview.direct": "Direct connect", "overview.address": "Address", "overview.address_help": "Use this address from Palworld's multiplayer connection screen.", "overview.copy": "Copy address", "overview.manage_auto": "Manage Auto Shutdown",
    "auto.title": "Auto Shutdown", "auto.operations_blocked": "Operations blocked for recovery", "auto.operations_help": "Review Nitrado tools before changing this server.", "auto.current": "Current activity", "auto.reporting": "Player reporting", "auto.remaining": "Idle time remaining", "auto.not_counting": "Not counting down", "auto.enable": "Enable Auto Shutdown", "auto.disable": "Disable Auto Shutdown", "auto.review": "Review connection", "auto.cancel": "Cancel pending shutdown", "auto.idle": "Idle timing", "auto.idle_label": "Minutes at zero trusted players", "auto.cooldown": "Startup cooldown", "auto.cooldown_label": "Minutes before shutdown may begin", "auto.history": "Timer history", "auto.last_shutdown": "Last shutdown reason", "auto.last_reset": "Last reset reason", "auto.none": "None recorded",
    "reporting.blocked_decision": "Blocked — player reporting decision required", "reporting.blocked_off": "Blocked — player reporting is off", "reporting.blocked_status": "Blocked — player reporting status unavailable", "reporting.blocked_unavailable": "Blocked — player reporting unavailable", "reporting.admin_required": "Administrator decision required", "reporting.review_game": "Off — review Game settings", "reporting.verified": "Verified", "reporting.resume": "Enabled — verification resumes when the server runs", "reporting.blocked_reason": "Blocked — {reason}",
    "reporting.callout_off": "Live player reporting is off", "reporting.callout_off_help": "Player counts while the server runs and Auto Shutdown are unavailable.", "reporting.callout_unresolved": "Player reporting needs a decision", "reporting.callout_unresolved_help": "Review the plaintext connection warning before live player counts or Auto Shutdown can be used.", "reporting.callout_status": "Player reporting status cannot be confirmed", "reporting.callout_status_help": "Auto Shutdown fails closed until the saved administrator decision can be verified.", "reporting.callout_unverified": "Player reporting is enabled but not verified", "reporting.callout_unavailable": "Player reporting verification is unavailable", "reporting.review_connection": "Review connection",
    "auto.enablement_required": "{blocked} — required before enablement",
    "game.confirmed": "Confirmed status", "game.choice": "Connection choice", "game.keep_off": "Keep live player reporting off", "game.allow_plaintext": "Allow the plaintext connection", "game.settings": "Palworld server settings", "game.settings_help": "Searchable, categorized controls for the live file with documented defaults, secret-safe editing, lossless preservation of future settings, validated review, verified backup-before-write, and durable recovery.", "game.inspect": "Open and inspect", "game.preview_apply": "Preview and apply", "game.after": "After a change", "game.restart": "Restart Palworld", "game.open": "Open settings workspace", "game.read_only": "Applying settings and restoring saves are read-only in this release.", "game.ready_any": "Ready in any server state", "game.ready_stopped": "Ready — server stopped",
    "game.endpoint": "the Palworld REST endpoint configured in PalWorldSettings.ini", "game.operation_blocked": "Resolve the active or uncertain provider operation before opening settings.",
    "security.why": "Why this connection needs caution", "security.risk": "Palworld sends the administrator credential to {endpoint} using HTTP Basic authentication over unencrypted HTTP. Anyone able to observe that network path could read it. Use a unique REST password.", "security.approved": "Plaintext connection approved", "security.approved_help": "The acknowledged connection is active for player reporting and Auto Shutdown.", "security.off": "Player reporting is off", "security.off_help": "No administrator credential is sent over HTTP. Live player counts and Auto Shutdown remain unavailable.", "security.unavailable": "Saved decision unavailable", "security.unavailable_help": "The integration cannot confirm the administrator's choice, so changes are disabled.", "security.decision": "Decision required", "security.decision_help": "Choose whether to allow the plaintext connection before player reporting or Auto Shutdown can run.",
    "review.sensitive": "Sensitive setting changed — values hidden.", "review.consequential": "Review carefully; this can have security or permanent gameplay consequences.", "review.performance": "May increase or reduce server load.", "review.gameplay": "Changes gameplay or server behavior after restart.", "review.ready": "Review ready", "review.blocked": "Preview blocked", "review.failed": "Preview failed", "review.valid": "Valid", "review.invalid": "Blocked", "review.changed": "Changed", "review.unchanged": "Unchanged", "review.restart": "Restart required", "review.proposed": "Proposed changes", "review.diff": "Redacted INI diff",
    "recovery.before_rollback": "Before rollback", "recovery.before_apply": "Before apply", "recovery.audit": "Audit {audit}", "recovery.audit_unavailable": "unavailable", "recovery.restore": "Restore this backup", "recovery.unavailable": "Unavailable — {reason}", "recovery.unverified": "backup could not be verified", "recovery.none": "No editor-created recovery backups yet.", "recovery.title": "Source and recovery", "recovery.help": "Read-only, credential-redacted source. Structured edits replace only exact value spans; untouched bytes, order, quoting, and future keys survive.", "recovery.source_unavailable": "Source unavailable", "recovery.history": "Recovery history",
    "footer.make_change": "Make a change to start review.", "footer.resolve_issues": "Resolve {count} issue{suffix} before review.", "footer.read_only": "Applying changes is read-only in this release.", "footer.review_first": "Review the current draft before Apply.", "footer.ready_apply": "Validated and ready to apply.", "footer.unsaved": "{count} unsaved change{suffix}", "footer.clean": "No unsaved changes", "footer.ready_validate": "Ready to validate · restart required after apply", "footer.review": "Review changes", "footer.apply": "Apply validated changes",
    "save.no_changes": "No file content changes were found.", "save.review_title": "Restore review", "save.world_manifest": "World {world} · Portable bundle manifest recognized", "save.world_editor": "World {world} · Editor ZIP detected", "save.active_world": "active", "save.changes": "CHANGES", "save.identical": "IDENTICAL", "save.replace": "Replace", "save.add": "Add", "save.preserve": "Preserve", "save.delete": "Delete", "save.current": "Current save", "save.proposed": "Proposed save", "save.file_count": "{count} files · {bytes}", "save.files_change": "Files supplied by the ZIP that will change", "save.no_deletions": "No implicit deletions.", "save.no_deletions_help": "Files missing from an editor ZIP stay exactly as they are on the server.", "save.truncated": "The on-screen file list is truncated; counts and hashes still cover the complete save.", "save.read_only": "Restore is read-only in this release. Download and ZIP inspection are available.", "save.progress_a11y": "Save-package progress", "save.stop_first": "Stop first, then snapshot", "save.stop_help": "{prerequisite} A freshly confirmed stop prevents a ZIP assembled from files written at different moments.", "save.ready_title": "Ready for a consistent snapshot", "save.ready_help": "The server is freshly confirmed stopped.", "save.current_readiness": "Current readiness", "save.intro": "Download the live editor-facing save, edit it, then upload the ZIP for exact review.", "save.safety": "ZIP contents and safety checks", "save.safety_help": "Palworld's rolling backup/ history, provider paths, settings, credentials, and Nitrado metadata are excluded. Uploads are checked for unsafe paths, links, collisions, encryption, backup history, and decompression bombs.", "save.download_title": "1. Download", "save.download_help": "Creates a portable ZIP containing the current world and player files plus a hash manifest.", "save.download_button": "Download editor save ZIP", "save.upload_title": "2. Upload and review", "save.upload_help": "Accepts this bundle or a ZIP produced by a save editor. Nothing is written during upload or review.", "save.upload_label": "Palworld save ZIP", "save.upload_max": "Maximum upload: 256 MiB.",
    "status.enabled_verified": "Enabled and verified", "status.enabled_resume": "Enabled — verification resumes when the server runs", "status.enabled_unverified": "Enabled — not verified", "status.decision_required": "Decision required", "status.decision_unavailable": "Unavailable — saved decision cannot be confirmed", "status.unavailable_reason": "Unavailable — {reason}",
    "editor.search_results": "{category}: search results for “{query}”", "editor.filter_status": "{category}. {count} {scope} shown{query}.", "editor.matching": " matching “{query}”", "editor.scope_one": "setting", "editor.scope_many": "settings", "editor.changed_one": "changed setting", "editor.changed_many": "changed settings", "editor.nondefault_one": "non-default setting", "editor.nondefault_many": "non-default settings", "editor.issue_one": "setting with issues", "editor.issue_many": "settings with issues",
    "setting.generic_help_number": "{label}. Palworld stores this as a numeric value.", "setting.generic_help_boolean": "{label}. Palworld stores this as an on/off value.", "setting.generic_help_other": "{label}. Palworld stores this as an additional setting.",
  }),
  "en-XA": Object.freeze({
    "route.overview": "[ Overview and current server activity ]",
    "route.auto-shutdown": "[ Automatic shutdown timing and safeguards ]",
    "route.save-games": "[ Save-game download, upload, and review ]",
    "route.game-settings": "[ Palworld game settings and player reporting ]",
    "navigation.section": "[ Choose a Palworld server section ]",
    "category.all": "[ All Palworld settings across every category ]",
    "category.favorites": "[ Essential settings most people change ]",
    "category.server": "[ Server identity, access, and connections ]",
  }),
});
const EDITOR_DRAFT_STORAGE_PREFIX = "nitrado_gameserver:palworld-editor-draft:v1:";
const EDITOR_DRAFT_STORAGE_VERSION = 1;
const EDITOR_DRAFT_MAX_BYTES = 256 * 1024;
const EDITOR_DRAFT_MAX_RECORDS = 8;

const CATEGORY_ORDER = Object.freeze([
  ["all", "All Settings"],
  ["favorites", "Essentials"],
  ["world", "World & progression"],
  ["players", "Players"],
  ["pals", "Pals"],
  ["combat", "Combat & death"],
  ["bases", "Bases & building"],
  ["multiplayer", "Multiplayer & guilds"],
  ["server", "Server & access"],
  ["performance", "Performance"],
  ["other", "Other & new settings"],
]);

const DEFAULTS = Object.freeze({
  Difficulty: "None", RandomizerType: "None", RandomizerSeed: '""', bIsRandomizerPalLevelRandom: "False",
  DayTimeSpeedRate: "1.000000", NightTimeSpeedRate: "1.000000", ExpRate: "1.000000", PalCaptureRate: "1.000000",
  PalSpawnNumRate: "1.000000", PalDamageRateAttack: "1.000000", PalDamageRateDefense: "1.000000",
  PlayerDamageRateAttack: "1.000000", PlayerDamageRateDefense: "1.000000", PlayerStomachDecreaceRate: "1.000000",
  PlayerStaminaDecreaceRate: "1.000000", PlayerAutoHPRegeneRate: "1.000000", PlayerAutoHpRegeneRateInSleep: "1.000000",
  PalStomachDecreaceRate: "1.000000", PalStaminaDecreaceRate: "1.000000", PalAutoHPRegeneRate: "1.000000",
  PalAutoHpRegeneRateInSleep: "1.000000", BuildObjectHpRate: "1.000000", BuildObjectDamageRate: "1.000000",
  BuildObjectDeteriorationDamageRate: "1.000000", CollectionDropRate: "1.000000", CollectionObjectHpRate: "1.000000",
  CollectionObjectRespawnSpeedRate: "1.000000", EnemyDropItemRate: "1.000000", DeathPenalty: "Item",
  bEnablePlayerToPlayerDamage: "False", bEnableFriendlyFire: "False", bEnableInvaderEnemy: "True", bActiveUNKO: "False",
  bEnableAimAssistPad: "True", bEnableAimAssistKeyboard: "False", DropItemMaxNum: "3000",
  PhysicsActiveDropItemMaxNum: "-1", DropItemMaxNum_UNKO: "100", BaseCampMaxNum: "128", BaseCampWorkerMaxNum: "15",
  DropItemAliveMaxHours: "1.000000", bAutoResetGuildNoOnlinePlayers: "False", AutoResetGuildTimeNoOnlinePlayers: "72.000000",
  GuildPlayerMaxNum: "20", BaseCampMaxNumInGuild: "4", PalEggDefaultHatchingTime: "1.000000", WorkSpeedRate: "1.000000",
  AutoSaveSpan: "30.000000", bIsMultiplay: "False", bIsPvP: "False", bHardcore: "False", bPalLost: "False",
  bCharacterRecreateInHardcore: "False", bCanPickupOtherGuildDeathPenaltyDrop: "False", bEnableNonLoginPenalty: "True",
  bEnableFastTravel: "True", bEnableFastTravelOnlyBaseCamp: "False", bIsStartLocationSelectByMap: "False",
  bExistPlayerAfterLogout: "False", bEnableDefenseOtherGuildPlayer: "False", bInvisibleOtherGuildBaseCampAreaFX: "False",
  bBuildAreaLimit: "False", ItemWeightRate: "1.000000", CoopPlayerMaxNum: "4", ServerPlayerMaxNum: "32",
  ServerName: '"Default Palworld Server"', ServerDescription: '""', AdminPassword: '""', ServerPassword: '""',
  bAllowClientMod: "True", PublicPort: "8211", PublicIP: '""', RCONEnabled: "False", RCONPort: "25575", Region: '""',
  bUseAuth: "True", BanListURL: '"https://b.palworldgame.com/api/banlist.txt"', RESTAPIEnabled: "False", RESTAPIPort: "8212",
  bShowPlayerList: "False", ChatPostLimitPerMinute: "30", CrossplayPlatforms: "(Steam,Xbox,PS5,Mac)",
  bIsUseBackupSaveData: "True", LogFormatType: "Text", bIsShowJoinLeftMessage: "True", SupplyDropSpan: "180",
  EnablePredatorBossPal: "True", MaxBuildingLimitNum: "0", ServerReplicatePawnCullDistance: "15000.000000",
  bAllowGlobalPalboxExport: "True", bAllowGlobalPalboxImport: "False", EquipmentDurabilityDamageRate: "1.000000",
  ItemContainerForceMarkDirtyInterval: "1.000000", PlayerDataPalStorageUpdateCheckTickInterval: "1.000000",
  ItemCorruptionMultiplier: "1.000000", MonsterFarmActionSpeedRate: "1.000000", DenyTechnologyList: "",
  GuildRejoinCooldownMinutes: "0", AutoTransferMasterCheckIntervalSeconds: "3600.000000",
  AutoTransferMasterThresholdDays: "14", MaxGuildsPerFrame: "10", BlockRespawnTime: "5.000000",
  RespawnPenaltyDurationThreshold: "0.000000", RespawnPenaltyTimeScale: "2.000000",
  bDisplayPvPItemNumOnWorldMap_BaseCamp: "False", bDisplayPvPItemNumOnWorldMap_Player: "False",
  AdditionalDropItemWhenPlayerKillingInPvPMode: '"PlayerDropItem"', AdditionalDropItemNumWhenPlayerKillingInPvPMode: "1",
  bAdditionalDropItemWhenPlayerKillingInPvPMode: "False", bEnableVoiceChat: "False",
  VoiceChatMaxVolumeDistance: "3000.000000", VoiceChatZeroVolumeDistance: "15000.000000",
  bAllowEnhanceStat_Health: "True", bAllowEnhanceStat_Attack: "True", bAllowEnhanceStat_Stamina: "True",
  bAllowEnhanceStat_Weight: "True", bAllowEnhanceStat_WorkSpeed: "True", bEnableBuildingPlayerUIdDisplay: "False",
  BuildingNameDisplayCacheTTLSeconds: "60", bAllowEnemyCampSpawnNearBaseCamp: "False",
});

const STRING_KEYS = new Set(["RandomizerSeed", "ServerName", "ServerDescription", "AdminPassword", "ServerPassword", "PublicIP", "Region", "BanListURL", "AdditionalDropItemWhenPlayerKillingInPvPMode"]);
const RAW_KEYS = new Set(["CrossplayPlatforms", "DenyTechnologyList"]);
const ENUMS = Object.freeze({
  RandomizerType: [["None", "Off"], ["Region", "Randomize by region"], ["All", "Fully random"]],
  DeathPenalty: [["None", "Keep everything"], ["Item", "Drop items"], ["ItemAndEquipment", "Drop items and equipment"], ["All", "Drop items, equipment, and team Pals"]],
  LogFormatType: [["Text", "Text"], ["Json", "JSON"]],
});
const SECRET_KEYS = new Set(["AdminPassword", "ServerPassword"]);
const ESSENTIAL_KEYS = new Set(["ServerName", "ServerDescription", "ServerPassword", "AdminPassword", "ServerPlayerMaxNum", "Difficulty", "ExpRate", "PalCaptureRate", "PalSpawnNumRate", "PalEggDefaultHatchingTime", "DeathPenalty", "bIsPvP", "bHardcore", "BaseCampWorkerMaxNum", "RESTAPIEnabled"]);
const PERFORMANCE_KEYS = new Set(["NetServerMaxTickRate", "PalSpawnNumRate", "DropItemMaxNum", "PhysicsActiveDropItemMaxNum", "DropItemMaxNum_UNKO", "BaseCampMaxNum", "BaseCampWorkerMaxNum", "AutoSaveSpan", "MaxBuildingLimitNum", "ServerReplicatePawnCullDistance", "ItemContainerForceMarkDirtyInterval", "PlayerDataPalStorageUpdateCheckTickInterval", "AutoTransferMasterCheckIntervalSeconds", "MaxGuildsPerFrame"]);
const UNDOCUMENTED_KEYS = new Set(["AutoSaveSpan", "AutoTransferMasterCheckIntervalSeconds", "AutoTransferMasterThresholdDays", "BanListURL", "BuildObjectHpRate", "BuildingNameDisplayCacheTTLSeconds", "CoopPlayerMaxNum", "Difficulty", "DropItemAliveMaxHours", "DropItemMaxNum", "DropItemMaxNum_UNKO", "EnablePredatorBossPal", "MaxGuildsPerFrame", "PlayerDataPalStorageUpdateCheckTickInterval", "Region", "WorkSpeedRate", "bActiveUNKO", "bCanPickupOtherGuildDeathPenaltyDrop", "bEnableAimAssistKeyboard", "bEnableAimAssistPad", "bEnableDefenseOtherGuildPlayer", "bEnableFriendlyFire", "bEnableNonLoginPenalty", "bEnablePlayerToPlayerDamage", "bIsMultiplay", "bUseAuth"]);

const LABELS = Object.freeze({
  ExpRate: "Experience rate", PalCaptureRate: "Capture rate", PalSpawnNumRate: "Pal spawn rate",
  DayTimeSpeedRate: "Day length speed", NightTimeSpeedRate: "Night length speed", PalEggDefaultHatchingTime: "Huge egg hatch time",
  PlayerStomachDecreaceRate: "Player hunger drain", PlayerStaminaDecreaceRate: "Player stamina drain",
  PalStomachDecreaceRate: "Pal hunger drain", PalStaminaDecreaceRate: "Pal stamina drain",
  PlayerAutoHPRegeneRate: "Player health regeneration", PlayerAutoHpRegeneRateInSleep: "Player sleep regeneration",
  PalAutoHPRegeneRate: "Pal health regeneration", PalAutoHpRegeneRateInSleep: "Palbox health regeneration",
  PalDamageRateAttack: "Damage dealt by Pals", PalDamageRateDefense: "Damage taken by Pals",
  PlayerDamageRateAttack: "Damage dealt by players", PlayerDamageRateDefense: "Damage taken by players",
  BuildObjectDamageRate: "Damage taken by structures", BuildObjectDeteriorationDamageRate: "Structure deterioration",
  CollectionDropRate: "Gathered resource yield", CollectionObjectHpRate: "Gatherable object health",
  CollectionObjectRespawnSpeedRate: "Resource respawn interval", EnemyDropItemRate: "Enemy drop yield",
  BaseCampMaxNum: "Total bases on server", BaseCampMaxNumInGuild: "Bases per guild", BaseCampWorkerMaxNum: "Working Pals per base",
  ServerPlayerMaxNum: "Maximum players", GuildPlayerMaxNum: "Players per guild", CoopPlayerMaxNum: "Co-op party size",
  RESTAPIEnabled: "Palworld REST API", RESTAPIPort: "REST API port", RCONEnabled: "RCON (deprecated)",
  bIsPvP: "PvP mode", bHardcore: "Hardcore mode", bPalLost: "Permanent Pal loss on death",
  bEnableInvaderEnemy: "Base raid events", bEnableFastTravel: "Fast travel", bEnableFastTravelOnlyBaseCamp: "Base-to-base fast travel only",
  bEnableNonLoginPenalty: "Offline guild decay", bIsUseBackupSaveData: "Palworld world backups",
  bAllowGlobalPalboxExport: "Export to Global Palbox", bAllowGlobalPalboxImport: "Import from Global Palbox",
  bAllowClientMod: "Allow modded clients", bShowPlayerList: "In-game player list", bIsShowJoinLeftMessage: "Join and leave messages",
  bAllowEnemyCampSpawnNearBaseCamp: "Allow enemy camps near bases",
  NetServerMaxTickRate: "Server tick rate", ServerReplicatePawnCullDistance: "Pal synchronization distance",
});

const DESCRIPTIONS = Object.freeze({
  Difficulty: "Undocumented in Pocketpair's current configuration guide. The shipped 1.0.3 default is None; preserve provider-specific values unless you know the hosted build supports them.",
  ExpRate: "Multiplier for experience gained. Higher values reduce leveling grind.",
  PalCaptureRate: "Multiplier for capture chance. Higher values make Pals easier to catch.",
  PalSpawnNumRate: "Multiplier for wild Pal population. Higher values also increase server load.",
  PalEggDefaultHatchingTime: "Hours required for a Huge Egg; smaller eggs scale from this value.",
  DayTimeSpeedRate: "World daytime clock speed. Lower values make daytime last longer.",
  NightTimeSpeedRate: "World nighttime clock speed. Lower values make nighttime last longer.",
  DeathPenalty: "Choose what a player drops when they die.",
  bHardcore: "Death prevents normal respawning. This is a consequential world rule.",
  bPalLost: "Pals can be permanently lost on death. Yes, this switch has teeth.",
  BaseCampWorkerMaxNum: "Maximum working Pals at each base. Pocketpair documents a maximum of 50; higher counts cost performance.",
  BaseCampMaxNumInGuild: "Maximum bases owned by one guild. Pocketpair documents a maximum of 10.",
  ServerPlayerMaxNum: "Maximum concurrent players accepted by the server.",
  ServerName: "Name shown to players and in server listings.",
  ServerDescription: "Description shown with the server listing.",
  ServerPassword: "Password players need to join. Preview and diff output redact it.",
  AdminPassword: "Administrator password used by REST and administrative commands. Preview and diff output redact it.",
  RESTAPIEnabled: "Required for trusted live player reporting and this integration's Auto Shutdown logic.",
  RCONEnabled: "Enables legacy RCON. Pocketpair has deprecated this setting; prefer the REST API.",
  CrossplayPlatforms: "Platforms allowed to connect, stored as an Unreal tuple such as (Steam,Xbox,PS5,Mac).",
  bIsUseBackupSaveData: "Enables Palworld's own rotating world backups. This is separate from editor recovery backups.",
  bAllowGlobalPalboxExport: "Allows players to save Pals to the Global Palbox.",
  bAllowGlobalPalboxImport: "Allows players to load Pals from the Global Palbox.",
  bAllowEnemyCampSpawnNearBaseCamp: "Allows hostile camps to appear near player bases. Pocketpair documents this as disabled by default.",
  NetServerMaxTickRate: "Provider-level server simulation tick rate. This is not in Pocketpair's current documented INI set; higher values are more CPU-intensive.",
  ServerReplicatePawnCullDistance: "Distance in centimeters at which Pals synchronize to players. Documented range: 5,000–15,000.",
  DenyTechnologyList: "Unreal tuple of technology IDs that the server disables.",
});

export const palworldTranslations = Object.freeze({
  en: Object.freeze({
    ...PALWORLD_TRANSLATIONS.en,
    ...Object.fromEntries(Object.entries(LABELS).map(([key, value]) => [`setting.${key}.label`, value])),
    ...Object.fromEntries(Object.entries(DESCRIPTIONS).map(([key, value]) => [`setting.${key}.description`, value])),
    ...Object.fromEntries(Object.entries(ENUMS).flatMap(([key, values]) => values.map(([value, label]) => [`enum.${key}.${value}`, label]))),
  }),
  "en-XA": PALWORLD_TRANSLATIONS["en-XA"],
});

const humanizeKey = (key) => (LABELS[key] || String(key)
  .replace(/^b(?=[A-Z])/, "")
  .replaceAll("_", " ")
  .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
  .replace(/\bNum\b/g, "number")
  .replace(/\bMax\b/g, "maximum")
  .replace(/\bRegene\b/g, "regeneration")
  .replace(/\bHp\b|\bHP\b/g, "health")
  .replace(/\bPv P\b/g, "PvP")
  .replace(/\bU Id\b/g, "UID")
  .replace(/\bApi\b/g, "API")
  .replace(/\bRcon\b/g, "RCON")
  .replace(/\bUnko\b/g, "UNKO")
  .replace(/^./, (value) => value.toUpperCase()));

const categoryForKey = (key) => {
  if (PERFORMANCE_KEYS.has(key)) return "performance";
  if (/^(Server|Admin|Public|REST|RCON|Region|BanList|LogFormat|ChatPost|Crossplay|bUseAuth|bAllowClientMod|bShowPlayerList|bIsShowJoinLeft)/.test(key)) return "server";
  if (/(BaseCamp|Build|Building|CollectionObject|ItemContainer)/.test(key)) return "bases";
  if (/(Guild|Multiplay|PvP|PlayerToPlayer|FriendlyFire|OtherGuild|VoiceChat|Coop)/.test(key)) return "multiplayer";
  if (/(Damage|Death|Penalty|Hardcore|PalLost|Respawn|AdditionalDrop)/.test(key)) return "combat";
  if (/^Pal|Predator|Randomizer|Invader/.test(key) || /Palbox/.test(key)) return "pals";
  if (/^Player|AimAssist|EnhanceStat|FastTravel|StartLocation|ItemWeight|ExistPlayer/.test(key)) return "players";
  if (/(TimeSpeed|ExpRate|WorkSpeed|DropRate|DropItem|Hatching|AutoSave|SupplyDrop|Corruption|FarmAction)/.test(key)) return "world";
  return "other";
};

const decodeQuoted = (raw) => {
  const value = String(raw ?? "").trim();
  if (!(value.startsWith('"') && value.endsWith('"'))) return value;
  return value.slice(1, -1).replace(/\\([\\"])/g, "$1");
};
const encodeQuoted = (value) => `"${String(value ?? "").replaceAll("\\", "\\\\").replaceAll('"', '\\"')}"`;

const _findClosingParen = (text, open) => {
  let depth = 0; let quote = false; let escaped = false;
  for (let index = open; index < text.length; index += 1) {
    const char = text[index];
    if (quote) {
      if (escaped) escaped = false;
      else if (char === "\\") escaped = true;
      else if (char === '"') quote = false;
      continue;
    }
    if (char === '"') quote = true;
    else if (char === "(") depth += 1;
    else if (char === ")" && --depth === 0) return index;
  }
  return -1;
};

export const parsePalworldSettingsDocument = (source) => {
  const text = String(source ?? "");
  const assignments = [...text.matchAll(/^[ \t]*OptionSettings[ \t]*=/gm)];
  if (assignments.length !== 1) return { text, entries: [], byKey: new Map(), error: "Expected exactly one OptionSettings assignment." };
  let open = assignments[0].index + assignments[0][0].length;
  while (/[ \t]/.test(text[open] || "")) open += 1;
  if (text[open] !== "(") return { text, entries: [], byKey: new Map(), error: "OptionSettings must use a parenthesized value." };
  const close = _findClosingParen(text, open);
  if (close < 0) return { text, entries: [], byKey: new Map(), error: "OptionSettings has no safe closing parenthesis." };
  const entries = []; const byKey = new Map(); let partStart = open + 1; let quote = false; let escaped = false; let depth = 0;
  const addPart = (partEnd) => {
    const part = text.slice(partStart, partEnd);
    const match = part.match(/^([ \t]*)([A-Za-z][A-Za-z0-9_]*)([ \t]*)=([ \t]*)([\s\S]*?)([ \t]*)$/);
    if (!match) throw new Error(`Could not safely parse setting ${entries.length + 1}.`);
    const key = match[2];
    if (byKey.has(key.toLowerCase())) throw new Error(`Duplicate setting ${key}.`);
    const leadingLength = match[1].length + key.length + match[3].length + 1 + match[4].length;
    const rawValue = match[5];
    const entry = { key, rawValue, valueStart: partStart + leadingLength, valueEnd: partStart + leadingLength + rawValue.length };
    entries.push(entry); byKey.set(key.toLowerCase(), entry);
  };
  try {
    for (let index = open + 1; index <= close; index += 1) {
      const char = text[index];
      if (quote) {
        if (escaped) escaped = false;
        else if (char === "\\") escaped = true;
        else if (char === '"') quote = false;
      } else if (char === '"') quote = true;
      else if (char === "(") depth += 1;
      else if (char === ")" && depth > 0) depth -= 1;
      else if ((char === "," && depth === 0) || index === close) { addPart(index); partStart = index + 1; }
    }
  } catch (error) {
    return { text, entries: [], byKey: new Map(), error: error.message };
  }
  return { text, entries, byKey, open, close, error: "" };
};

export const updatePalworldSetting = (source, key, rawValue) => {
  const document = parsePalworldSettingsDocument(source);
  if (document.error) throw new Error(document.error);
  const entry = document.byKey.get(String(key).toLowerCase());
  if (!entry) throw new Error(`Setting ${key} is not present in this file.`);
  return `${document.text.slice(0, entry.valueStart)}${rawValue}${document.text.slice(entry.valueEnd)}`;
};

const settingType = (key, rawValue) => {
  if (ENUMS[key]) return "enum";
  if (STRING_KEYS.has(key)) return SECRET_KEYS.has(key) ? "secret" : "string";
  if (RAW_KEYS.has(key)) return "raw";
  const sample = DEFAULTS[key] ?? rawValue;
  if (/^(true|false)$/i.test(String(sample).trim())) return "boolean";
  if (/^-?\d+(?:\.\d+)?$/.test(String(sample).trim())) return "number";
  return "raw";
};

const settingMeta = (entry) => {
  const key = entry.key; const type = settingType(key, entry.rawValue); const defaultRaw = DEFAULTS[key];
  const limits = {
    NetServerMaxTickRate: [null, null, 1, "ticks/sec"], BaseCampWorkerMaxNum: [null, 50, 1, "Pals"],
    BaseCampMaxNumInGuild: [null, 10, 1, "bases"], ServerReplicatePawnCullDistance: [5000, 15000, 100, "cm"],
    PublicPort: [1, 65535, 1, "port"], RCONPort: [1, 65535, 1, "port"], RESTAPIPort: [1, 65535, 1, "port"],
    PalEggDefaultHatchingTime: [0, null, 0.1, "hours"], SupplyDropSpan: [null, null, 1, "minutes"],
    AutoResetGuildTimeNoOnlinePlayers: [null, null, 1, ""], GuildRejoinCooldownMinutes: [0, null, 1, "minutes"],
  }[key] || (type === "number" ? [null, null, entry.rawValue.includes(".") ? 0.1 : 1, ""] : []);
  return {
    key, type, label: humanizeKey(key), category: categoryForKey(key), defaultRaw,
    description: DESCRIPTIONS[key] || `${humanizeKey(key)}. Palworld stores this as ${type === "number" ? "a numeric value" : type === "boolean" ? "an on/off value" : "an advanced setting"}.`,
    min: limits[0], max: limits[1], step: limits[2], unit: limits[3] || "",
    performance: PERFORMANCE_KEYS.has(key), consequential: /Hardcore|PalLost|PvP|DeathPenalty|Password|RCON|GlobalPalboxImport/.test(key),
    confidence: DEFAULTS[key] === undefined ? "provider" : UNDOCUMENTED_KEYS.has(key) ? "shipped" : "documented",
  };
};

const entity = (snapshot, key) => snapshot?.entities?.[key] || null;
const state = (snapshot, key, fallback = "") => {
  const value = entity(snapshot, key)?.state;
  return value === undefined || value === null || ["unknown", "unavailable"].includes(String(value).toLowerCase())
    ? fallback
    : String(value);
};
const truth = (snapshot, key) => {
  const value = String(entity(snapshot, key)?.state || "").toLowerCase();
  return value === "on" ? true : value === "off" ? false : null;
};

const styles = `
  :host { display:block; color:var(--primary-text-color); --nitrado-readable-accent:color-mix(in srgb,var(--primary-color,#006b8f) 55%,var(--primary-text-color,#212121)); --nitrado-readable-warning:color-mix(in srgb,var(--warning-color,#b36b00) 60%,var(--primary-text-color,#212121)); --nitrado-readable-error:color-mix(in srgb,var(--error-color,#b3261e) 62%,var(--primary-text-color,#212121)); }
  * { box-sizing:border-box; }
  button, input, select, textarea { font:inherit; max-width:100%; }
  button { min-height:44px; border:1px solid color-mix(in srgb,var(--primary-color) 45%,var(--divider-color)); border-radius:9px; padding:9px 14px; background:var(--nitrado-readable-accent); color:var(--primary-background-color,#fff); font-weight:700; cursor:pointer; }
  input[type="number"], input[type="text"], input[type="search"], input[type="password"], select { min-height:44px; border:1px solid var(--divider-color); border-radius:9px; padding:8px 10px; background:var(--card-background-color); color:var(--primary-text-color); }
  button.secondary { background:var(--card-background-color); color:var(--primary-text-color); }
  button.danger { background:var(--nitrado-readable-error); border-color:var(--nitrado-readable-error); color:var(--primary-background-color,#fff); }
  button:disabled, button[aria-disabled="true"] { opacity:.58; cursor:not-allowed; }
  button:focus-visible, input:focus-visible, select:focus-visible, textarea:focus-visible, [tabindex]:focus-visible { outline:3px solid var(--primary-color); outline-offset:3px; }
  .tabs { display:flex; gap:6px; overflow-x:auto; padding:2px 2px 8px; scrollbar-width:thin; }
  .tabs button { flex:0 0 auto; display:inline-flex; align-items:center; gap:8px; background:transparent; color:var(--secondary-text-color); border-color:transparent; border-bottom:3px solid transparent; border-radius:7px 7px 0 0; }
  .tabs button[aria-selected="true"] { color:var(--primary-text-color); border-bottom-color:var(--primary-color); font-weight:800; }
  .tab-badge { display:inline-flex; align-items:center; min-height:24px; padding:3px 8px; border-radius:999px; color:var(--text-primary-color,#fff); font-size:11px; font-weight:850; line-height:1; letter-spacing:.01em; }
  .tab-badge.off { background:#9a3412; }
  .tab-badge.action { background:#b3261e; }
  .tab-badge.unavailable { background:#5c4b51; }
  .tab-badge.check { background:#7a3e00; }
  .panel { min-width:0; }
  .stack { display:grid; gap:16px; }
  .grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px; }
  .card { min-width:0; padding:18px; border:1px solid var(--divider-color); border-radius:13px; background:var(--card-background-color); box-shadow:0 2px 7px rgba(0,0,0,.08); }
  h1 { margin:0 0 13px; font-size:25px; }
  h2 { margin:0 0 10px; font-size:19px; }
  h3 { margin:0; font-size:16px; }
  p { margin:7px 0 0; line-height:1.45; overflow-wrap:anywhere; }
  .muted { color:var(--secondary-text-color); }
  .sr-only { position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden; clip:rect(0,0,0,0); white-space:nowrap; border:0; }
  .row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin-top:12px; }
  .stat { display:flex; justify-content:space-between; gap:15px; padding:10px 0; border-top:1px solid var(--divider-color); }
  .stat:first-of-type { border-top:0; }
  .stat span { text-align:right; overflow-wrap:anywhere; }
  .pill { display:inline-flex; align-items:center; min-height:28px; padding:4px 9px; border-radius:999px; border:1px solid var(--divider-color); font-weight:800; font-size:12px; }
  .pill.on { background:color-mix(in srgb,var(--success-color,#2e7d32) 14%,var(--card-background-color)); }
  .callout,.notice { padding:14px; border-left:4px solid var(--warning-color,#f0a800); border-radius:9px; background:color-mix(in srgb,var(--warning-color,#f0a800) 10%,var(--secondary-background-color)); }
  .warning,.error-notice { padding:14px; border-left:4px solid var(--error-color,#db4437); border-radius:9px; background:color-mix(in srgb,var(--error-color,#db4437) 8%,var(--secondary-background-color)); }
  .security-note { padding:12px 14px; border-left:4px solid var(--warning-color,#f0a800); border-radius:9px; background:color-mix(in srgb,var(--warning-color,#f0a800) 6%,var(--secondary-background-color)); }
  .success-note { padding:12px 14px; border-left:4px solid var(--success-color,#2e7d32); border-radius:9px; background:color-mix(in srgb,var(--success-color,#2e7d32) 7%,var(--secondary-background-color)); }
  .good { padding:12px; border-left:4px solid var(--success-color,#2e7d32); border-radius:8px; background:var(--secondary-background-color); }
  .choice { display:grid; gap:8px; margin:14px 0 0; padding:0; border:0; }
  .choice[aria-disabled="true"] { opacity:.7; }
  .choice legend { margin-bottom:4px; font-weight:800; }
  .choice label { display:flex; align-items:center; min-height:44px; padding:9px 11px; border:1px solid var(--divider-color); border-radius:9px; background:var(--secondary-background-color); cursor:pointer; }
  .choice input { width:auto; margin:0 10px 0 0; }
  details > summary { display:flex; align-items:center; min-height:44px; cursor:pointer; }
  .feedback { min-height:22px; margin-top:9px; font-weight:650; }
  .player-list { margin:7px 0 0; padding-left:20px; }
  .editor-dialog { width:min(1240px,calc(100vw - 24px)); max-width:1240px; height:min(900px,calc(100vh - 24px)); max-height:calc(100vh - 24px); padding:0; border:1px solid var(--divider-color); border-radius:16px; background:var(--primary-background-color,var(--card-background-color)); color:var(--primary-text-color); overflow:hidden; }
  .editor-dialog::backdrop { background:rgba(0,0,0,.58); }
  .editor-body { display:grid; grid-template-rows:auto auto minmax(0,1fr) auto; height:100%; min-height:0; }
  #editor-stage { display:contents; }
  .editor-operation { min-height:1.45em; padding:8px 20px; background:var(--card-background-color); outline:none; }
  .editor-operation[hidden] { display:none; }
  .editor-header { display:flex; align-items:flex-start; justify-content:space-between; gap:12px; padding:18px 20px 14px; border-bottom:1px solid var(--divider-color); background:var(--card-background-color); }
  .editor-header h2 { margin:0; }
  .editor-actions { display:flex; gap:9px; flex-wrap:wrap; }
  .editor-toolbar { display:flex; align-items:center; gap:10px; padding:12px 20px; border-bottom:1px solid var(--divider-color); background:var(--card-background-color); flex-wrap:wrap; }
  .editor-search { flex:1 1 280px; }
  .editor-filter { display:inline-flex; flex-wrap:wrap; gap:4px; max-width:100%; padding:3px; border:1px solid var(--divider-color); border-radius:10px; background:var(--secondary-background-color); }
  .editor-filter button { min-height:44px; padding:9px 12px; border:0; background:transparent; color:var(--secondary-text-color); }
  .editor-filter button[aria-pressed="true"] { background:var(--card-background-color); color:var(--primary-text-color); box-shadow:0 1px 4px rgba(0,0,0,.14); }
  .editor-workspace { display:grid; grid-template-columns:230px minmax(0,1fr); min-height:0; overflow:hidden; }
  .editor-rail { padding:14px 10px; border-right:1px solid var(--divider-color); background:var(--card-background-color); overflow:auto; }
  .editor-rail button { display:flex; width:100%; justify-content:space-between; gap:8px; margin:2px 0; padding:9px 10px; min-height:44px; border:0; background:transparent; color:var(--primary-text-color); text-align:left; font-weight:650; }
  .editor-category-select,.route-select { display:none; }
  .editor-rail button[aria-current="true"] { background:color-mix(in srgb,var(--primary-color) 14%,var(--card-background-color)); color:var(--nitrado-readable-accent); }
  .rail-count { min-width:25px; text-align:center; border-radius:999px; padding:2px 6px; background:var(--secondary-background-color); font-size:12px; }
  .editor-content { padding:18px 20px 28px; overflow:auto; min-width:0; }
  .editor-content-header { display:flex; align-items:flex-start; justify-content:space-between; gap:12px; margin-bottom:14px; }
  .settings-list { display:grid; gap:10px; }
  .setting-card { display:grid; grid-template-columns:minmax(0,1fr) minmax(220px,310px); gap:18px; align-items:center; padding:15px 16px; border:1px solid var(--divider-color); border-radius:12px; background:var(--card-background-color); }
  .setting-card.changed { border-left:4px solid var(--primary-color); padding-left:13px; }
  .setting-card.issue { border-color:var(--warning-color,#f0a800); }
  .setting-title { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
  .setting-title h3 { font-size:15px; }
  .setting-key { margin-top:4px; color:var(--secondary-text-color); font-family:ui-monospace,SFMono-Regular,Consolas,monospace; font-size:11px; overflow-wrap:anywhere; }
  .setting-control { min-width:0; }
  .setting-control input:not([type="checkbox"]), .setting-control select { width:100%; }
  .setting-control-row { display:flex; align-items:center; gap:8px; }
  .setting-control-row input { flex:1; }
  .setting-meta { display:flex; align-items:center; gap:7px; min-height:24px; margin-top:6px; color:var(--secondary-text-color); font-size:12px; flex-wrap:wrap; }
  .setting-change { margin-top:8px; padding:7px 9px; border-radius:8px; background:var(--secondary-background-color); font-size:12px; overflow-wrap:anywhere; }
  .tag { display:inline-flex; align-items:center; min-height:23px; padding:3px 7px; border-radius:999px; background:var(--secondary-background-color); font-size:11px; font-weight:800; }
  .tag.changed { color:var(--nitrado-readable-accent); }
  .tag.risk { color:var(--nitrado-readable-warning); }
  .setting-undo { min-height:44px; padding:8px 10px; font-size:12px; }
  .switch-control { display:flex; align-items:center; justify-content:space-between; gap:12px; min-height:48px; padding:8px 10px; border:1px solid var(--divider-color); border-radius:10px; background:var(--secondary-background-color); cursor:pointer; }
  .switch-control input { width:22px; height:22px; accent-color:var(--primary-color); }
  .empty-state { padding:28px; text-align:center; border:1px dashed var(--divider-color); border-radius:12px; }
  .editor-footer { display:flex; align-items:center; justify-content:space-between; gap:12px; padding:12px 20px; border-top:1px solid var(--divider-color); background:var(--card-background-color); box-shadow:0 -4px 14px rgba(0,0,0,.07); }
  .change-summary { font-weight:800; }
  .review-list { display:grid; gap:8px; margin:10px 0; padding:0; list-style:none; }
  .review-item { padding:10px 12px; border:1px solid var(--divider-color); border-radius:9px; background:var(--card-background-color); }
  .review-item p { margin:4px 0 0; }
  .raw-panel { margin-top:18px; padding:13px; border:1px solid var(--divider-color); border-radius:11px; background:var(--card-background-color); }
  .raw-panel summary { font-weight:800; }
  textarea { width:100%; min-height:300px; resize:vertical; padding:12px; border:1px solid var(--divider-color); border-radius:9px; background:var(--secondary-background-color); color:var(--primary-text-color); font-family:ui-monospace,SFMono-Regular,Consolas,monospace; font-size:13px; line-height:1.45; tab-size:2; }
  pre { max-width:100%; max-height:280px; margin:8px 0 0; padding:12px; overflow:auto; border:1px solid var(--divider-color); border-radius:8px; background:var(--secondary-background-color); white-space:pre-wrap; overflow-wrap:anywhere; font-size:12px; }
  .history { display:grid; gap:8px; margin-top:10px; }
  .history-item { padding:10px; border:1px solid var(--divider-color); border-radius:9px; }
  .history-item .row { margin-top:7px; }
  .danger-text { color:var(--error-color,#db4437); font-weight:750; }
  .file-picker { display:grid; gap:7px; margin-top:12px; }
  .file-picker input[type="file"] { min-height:44px; max-width:100%; padding:8px; border:1px solid var(--divider-color); border-radius:9px; background:var(--secondary-background-color); color:var(--primary-text-color); }
  .bundle-review { display:grid; gap:12px; }
  .bundle-counts { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:8px; }
  .bundle-counts .stat { display:grid; gap:3px; padding:10px; border:1px solid var(--divider-color); border-radius:9px; }
  .bundle-counts .stat span { text-align:left; font-size:20px; font-weight:800; }
  .bundle-files { max-height:260px; margin:0; padding-left:22px; overflow:auto; }
  .bundle-files li { margin:5px 0; overflow-wrap:anywhere; }
  .bundle-progress { display:grid; gap:8px; width:100%; padding:12px; border:1px solid var(--info-color,#2980b9); border-radius:9px; background:color-mix(in srgb,var(--info-color,#2980b9) 9%,transparent); }
  .bundle-progress[hidden] { display:none; }
  .bundle-progress progress { width:100%; min-height:10px; accent-color:var(--primary-color); }
  .bundle-progress-detail { min-height:1.4em; overflow-wrap:anywhere; }
  @media (max-width:680px) { .grid { grid-template-columns:1fr; } .card { padding:15px; } .bundle-counts { grid-template-columns:repeat(2,minmax(0,1fr)); } .editor-dialog { width:100vw; height:100dvh; max-height:100dvh; border:0; border-radius:0; } .editor-body { grid-template-rows:auto auto minmax(0,1fr) auto; } .editor-header { padding:13px 14px; } .editor-toolbar { align-items:stretch; padding:10px 12px; } .editor-search { flex-basis:100%; } .editor-filter { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); width:100%; } .editor-filter button { min-width:0; overflow-wrap:anywhere; } .editor-category-select { display:grid; gap:5px; width:100%; } .editor-category-select select { min-height:44px; width:100%; } .editor-workspace { display:block; min-height:0; overflow:auto; } .editor-rail { display:none; } .editor-content { overflow:visible; padding:14px 12px; } .setting-card { grid-template-columns:1fr; gap:11px; padding:14px; } .editor-footer { position:static; align-items:stretch; flex-wrap:wrap; padding:9px 12px; } .editor-footer > div:first-child { min-width:0; } .editor-actions { width:100%; } .editor-actions button { flex:1 1 130px; min-width:0; } }
  .narrow .tabs { display:none; }
  .narrow .route-select { display:grid; gap:5px; margin-bottom:12px; }
  .narrow .route-select select { width:100%; min-height:44px; }
  .narrow .editor-category-select { display:grid; flex-basis:100%; gap:5px; width:100%; }
  .narrow .editor-category-select select { width:100%; min-height:44px; }
  .narrow .editor-workspace { display:block; min-height:0; overflow:auto; }
  .narrow .editor-rail { display:none; }
  .narrow .editor-content { overflow:visible; padding:14px 12px; }
  @media (prefers-reduced-motion:reduce) { *, *::before, *::after { scroll-behavior:auto !important; transition:none !important; } }
`;

class PalworldCockpit {
  constructor() {
    this.root = null;
    this.context = null;
    this.snapshot = null;
    this.pending = null;
    this.feedback = null;
    this.focusTarget = null;
    this.editor = null;
    this.saveBundle = { preview: null, previewToken: null, error: "" };
    this.saveBundleProgress = null;
    this.saveBundleResult = { kind: "idle", text: "" };
    this._saveAnnouncementKey = "";
    this._saveAnnouncementBucket = -1;
    this._secretDrafts = new Map();
    this._generation = 0;
    this._disposed = true;
    this._beforeUnload = (event) => {
      if (!this.editor?.dirty) return;
      event.preventDefault();
      event.returnValue = "";
    };
  }

  async mount(root, context) {
    this._generation += 1;
    this._disposed = false;
    this.root = root;
    this.context = context;
    this.snapshot = context.snapshot;
    this._ensureShell();
    this._publishState();
    this.render();
  }

  async ready() {}

  async update(snapshot) {
    const previousRoute = this.route;
    this.snapshot = snapshot;
    if (this.route === previousRoute && this.editor?.open && !this.editor.loading && this.root?.querySelector("#palworld-settings-editor")) {
      this._syncEditorChrome();
      return;
    }
    this.render();
  }

  _saveBundleOperationActive() {
    return Boolean(this.pending?.startsWith("save-") && this.saveBundleProgress);
  }

  _saveBundleProgressDetail(progress = this.saveBundleProgress) {
    const parts = [];
    if (Number.isInteger(progress?.files_done)) {
      parts.push(Number.isInteger(progress.files_total)
        ? this._t("progress.files_total", { done: progress.files_done, total: progress.files_total })
        : this._t("progress.files_done", { done: progress.files_done }));
    }
    if (Number.isInteger(progress?.bytes_done)) {
      parts.push(Number.isInteger(progress.bytes_total)
        ? this._t("progress.bytes_total", { done: this._formatBytes(progress.bytes_done), total: this._formatBytes(progress.bytes_total) })
        : this._t("progress.bytes_read", { done: this._formatBytes(progress.bytes_done) }));
    }
    if (typeof progress?.current_file === "string" && progress.current_file) parts.push(progress.current_file);
    if (Number.isInteger(progress?.elapsed_seconds)) parts.push(this._t("progress.elapsed", { seconds: progress.elapsed_seconds }));
    return parts.join(" · ") || this._t("common.processing");
  }

  _syncSaveBundleOperationArea() {
    const area = this.root?.querySelector("#save-bundle-progress");
    if (!area) return false;
    const active = this._saveBundleOperationActive();
    area.hidden = !active;
    this.root.querySelector("#save-bundle-card")?.setAttribute("aria-busy", String(active));
    const result = this.root.querySelector("#save-bundle-operation-result");
    if (result) {
      result.dataset.kind = this.saveBundleResult.kind || "idle";
      result.textContent = this.saveBundleResult.text || "";
    }
    if (!active) return true;
    const message = area.querySelector("#save-bundle-progress-message");
    const detail = area.querySelector("#save-bundle-progress-detail");
    if (message) message.textContent = this.saveBundleProgress.message || this._t("progress.prepare");
    if (detail) detail.textContent = this._saveBundleProgressDetail();
    this.root.querySelectorAll("#download-save-bundle, #save-bundle-upload, #apply-save-bundle")
      .forEach((control) => { control.disabled = true; });
    return true;
  }

  async navigationGuard() {
    if (this.pending) return { decision: "block", messageCode: "mutation_in_progress" };
    if (this.editor?.dirty) return { decision: "prompt", messageCode: "unsaved_settings" };
    return { decision: "allow" };
  }

  async dispose() {
    this._generation += 1;
    this._disposed = true;
    globalThis.removeEventListener?.("beforeunload", this._beforeUnload);
    this._secretDrafts.clear();
    if (this.root) this.root.replaceChildren();
    this.root = null;
    this.context = null;
  }

  _captureGeneration() { return this._generation; }

  _ownsGeneration(generation) {
    return !this._disposed && generation === this._generation && Boolean(this.root && this.context);
  }

  _isNarrow() {
    const supplied = this.snapshot?.ui?.narrow ?? this.context?.ui?.narrow;
    return typeof supplied === "boolean" ? supplied : globalThis.matchMedia?.("(max-width: 680px)")?.matches === true;
  }

  _t(key, values = {}) {
    const translate = this.context?.ui?.translate;
    if (typeof translate === "function") return translate(palworldTranslations, key, values);
    const template = palworldTranslations.en[key] || key;
    const interpolate = this.context?.ui?.interpolate;
    return typeof interpolate === "function" ? interpolate(template, values) : template;
  }

  _formatBytes(value) {
    return formatBytes(value, {
      unavailable: this._t("common.unavailable"),
      bytes: this._t("unit.bytes"),
      units: [this._t("unit.kibibytes"), this._t("unit.mebibytes"), this._t("unit.gibibytes")],
    });
  }

  _categoryLabel(key) {
    const fallback = CATEGORY_ORDER.find(([category]) => category === key)?.[1] || this._t("common.settings");
    const translated = this._t(`category.${key}`);
    return translated === `category.${key}` ? fallback : translated;
  }

  _settingMeta(entry) {
    const meta = settingMeta(entry);
    const labelKey = `setting.${meta.key}.label`;
    const descriptionKey = `setting.${meta.key}.description`;
    const translatedLabel = this._t(labelKey);
    const label = translatedLabel === labelKey ? meta.label : translatedLabel;
    const translatedDescription = this._t(descriptionKey);
    const description = translatedDescription === descriptionKey
      ? this._t(meta.type === "number" ? "setting.generic_help_number" : meta.type === "boolean" ? "setting.generic_help_boolean" : "setting.generic_help_other", { label })
      : translatedDescription;
    const unitKey = ({
      "ticks/sec": "unit.ticks_per_second",
      Pals: "unit.pals",
      bases: "unit.bases",
      cm: "unit.centimeters",
      port: "unit.port",
      hours: "unit.hours",
      minutes: "unit.minutes",
    })[meta.unit];
    return { ...meta, label, description, unit: unitKey ? this._t(unitKey) : "" };
  }

  _publishState() {
    if (this.editor?.dirty) globalThis.addEventListener?.("beforeunload", this._beforeUnload);
    else globalThis.removeEventListener?.("beforeunload", this._beforeUnload);
    this.context?.host?.publishNavigationState({
      dirty: Boolean(this.editor?.dirty),
      mutationPhase: this.pending ? "pending" : "idle",
      messageCode: this.pending ? "mutation_in_progress" : "clean",
    });
  }

  get route() {
    const route = this.snapshot?.navigation?.current_route;
    return ROUTE_KEYS.has(route) ? route : "overview";
  }

  reporting() {
    const raw = this.snapshot?.profile?.public_state?.player_reporting;
    const consent = new Set(["enabled", "disabled", "unresolved", "unavailable"]).has(raw?.consent?.status)
      ? { ...raw.consent, reason: raw.consent.reason || this._t("reporting.reason_missing") }
      : { status: "unavailable", reason: this._t("reporting.decision_unavailable") };
    const verification = new Set(["verified", "unverified", "not_testable", "unavailable"]).has(raw?.verification?.status)
      ? { ...raw.verification, reason: raw.verification.reason || this._t("reporting.verification_missing") }
      : { status: "unavailable", reason: this._t("reporting.verification_unavailable") };
    return { consent, verification };
  }

  _reportingTabIndicator(reporting = this.reporting()) {
    if (reporting.consent.status === "disabled") {
      return {
        label: this._t("reporting.off"),
        kind: "off",
        accessibleName: this._t("reporting.a11y_off"),
        detail: this._t("reporting.off_detail"),
      };
    }
    if (reporting.consent.status === "unresolved") {
      return {
        label: this._t("reporting.action_needed"),
        kind: "action",
        accessibleName: this._t("reporting.a11y_action"),
        detail: this._t("reporting.action_detail"),
      };
    }
    if (reporting.consent.status === "unavailable") {
      return {
        label: this._t("reporting.status_unavailable"),
        kind: "unavailable",
        accessibleName: this._t("reporting.a11y_unavailable"),
        detail: reporting.consent.reason,
      };
    }
    if (["unverified", "unavailable"].includes(reporting.verification.status)) {
      return {
        label: this._t("reporting.check_setup"),
        kind: "check",
        accessibleName: this._t("reporting.a11y_attention"),
        detail: reporting.verification.reason,
      };
    }
    return null;
  }

  _ensureShell() {
    if (this.root?.querySelector("#palworld-main")) return;
    this.root.innerHTML = `<style>${styles}</style><main id="palworld-main" aria-labelledby="palworld-title"><h1 id="palworld-title">${escapeHtml(this._t("shell.title"))}</h1><div class="tabs" role="tablist" aria-label="${escapeHtml(this._t("shell.sections"))}"></div><label class="route-select" for="palworld-route-select"><span>${escapeHtml(this._t("navigation.section"))}</span><select id="palworld-route-select"></select></label><section class="panel" role="tabpanel" tabindex="0"></section><div class="feedback" aria-live="off"></div></main>`;
  }

  _setFeedback(kind, text, code, announce = true) {
    this.feedback = { kind, text };
    const node = this.root?.querySelector(".feedback");
    if (node) {
      node.dataset.kind = kind;
      node.textContent = text;
    }
    if (announce) this.context?.host?.announce?.(code, { message: text });
  }

  _announceSave(text, key, force = false) {
    if (!force && key === this._saveAnnouncementKey) return;
    this._saveAnnouncementKey = key;
    const node = this.root?.querySelector("#save-bundle-announcement");
    if (node) node.textContent = node.textContent === text ? `${text} ` : text;
  }

  _setSaveFeedback(kind, text, code, announce = true) {
    this.feedback = { kind, text };
    this.saveBundleResult = { kind, text };
    if (announce) this._announceSave(text, code, true);
  }

  _updateSaveBundleProgress(progress) {
    if (!progress || typeof progress.message !== "string") return;
    this.saveBundleProgress = progress;
    this.feedback = { kind: "status", text: progress.message };
    const message = this.root?.querySelector("#save-bundle-progress-message");
    const detail = this.root?.querySelector("#save-bundle-progress-detail");
    const feedback = this.root?.querySelector(".feedback");
    if (message) message.textContent = progress.message;
    if (detail) detail.textContent = this._saveBundleProgressDetail(progress);
    if (feedback) {
      feedback.dataset.kind = "status";
      feedback.textContent = progress.message;
    }
    const stage = String(progress.stage || progress.message || "progress");
    const done = Number.isFinite(Number(progress.bytes_done)) ? Number(progress.bytes_done) : Number(progress.files_done);
    const total = Number.isFinite(Number(progress.bytes_total)) && Number(progress.bytes_total) > 0
      ? Number(progress.bytes_total)
      : Number(progress.files_total);
    const bucket = Number.isFinite(done) && Number.isFinite(total) && total > 0
      ? Math.min(4, Math.floor((done / total) * 4))
      : -1;
    const key = `${stage}:${bucket}`;
    this._announceSave(bucket > 0 ? this._t("progress.milestone", { message: progress.message, percent: bucket * 25 }) : progress.message, key);
  }

  render() {
    if (!this.root || !this.snapshot) return;
    this._ensureShell();
    // A native indeterminate <progress> animation restarts whenever its DOM
    // node is replaced. Snapshot polling can request renders between backend
    // stages, so freeze the Save Games panel for the lifetime of the visible
    // operation area. Progress callbacks update only its two text nodes.
    if (this.route === "save-games" && this._saveBundleOperationActive()) {
      const area = this.root.querySelector("#save-bundle-progress");
      if (area && !area.hidden) {
        this._syncSaveBundleOperationArea();
        return;
      }
    }
    const active = this.root.getRootNode()?.activeElement;
    const restoreId = active && this.root.contains(active) ? active.id : "";
    const route = this.route;
    const main = this.root.querySelector("#palworld-main");
    main?.classList.toggle("narrow", this._isNarrow());
    const content = route === "overview"
      ? this._overview()
      : route === "auto-shutdown"
        ? this._autoShutdown()
        : route === "save-games" ? this._saveGames() : this._gameSettings();
    const reportingIndicator = this._reportingTabIndicator();
    this.root.querySelector(".tabs").innerHTML = ROUTES.map(([key, fallbackLabel]) => {
      const label = this._t(`route.${key}`) || fallbackLabel;
      const indicator = key === "game-settings" ? reportingIndicator : null;
      const accessibleName = indicator ? ` aria-label="${escapeHtml(indicator.accessibleName)}"` : "";
      const badge = indicator
        ? `<span class="tab-badge ${escapeHtml(indicator.kind)}" aria-hidden="true" title="${escapeHtml(indicator.detail)}">${escapeHtml(indicator.label)}</span>`
        : "";
      return `<button id="palworld-tab-${key}" type="button" role="tab" aria-selected="${route === key}" aria-controls="palworld-panel-${key}" tabindex="${route === key ? 0 : -1}" data-route="${key}"${accessibleName}><span>${escapeHtml(label)}</span>${badge}</button>`;
    }).join("");
    const routeSelect = this.root.querySelector("#palworld-route-select");
    if (routeSelect) routeSelect.innerHTML = ROUTES.map(([key, fallbackLabel]) => `<option value="${escapeHtml(key)}" ${route === key ? "selected" : ""}>${escapeHtml(this._t(`route.${key}`) || fallbackLabel)}</option>`).join("");
    const panel = this.root.querySelector(".panel");
    panel.id = `palworld-panel-${route}`;
    panel.setAttribute("aria-labelledby", `palworld-tab-${route}`);
    panel.innerHTML = content;
    panel.querySelector("h2")?.setAttribute("tabindex", "-1");
    const feedback = this.root.querySelector(".feedback");
    feedback.dataset.kind = this.feedback?.kind || "idle";
    feedback.textContent = this.feedback?.text || "";
    this._bind();
    const dialog = this.root.querySelector("#palworld-settings-editor");
    if (dialog && !dialog.open) dialog.showModal();
    this.root.querySelector('[role="tab"][aria-selected="true"]')?.scrollIntoView?.({ block: "nearest", inline: "nearest", behavior: "auto" });
    const explicitFocus = this.focusTarget;
    this.focusTarget = null;
    if (explicitFocus || restoreId) {
      const target = explicitFocus
        ? this.root.querySelector(explicitFocus)
        : [...this.root.querySelectorAll("[id]")].find((item) => item.id === restoreId);
      target?.focus();
      if (explicitFocus) target?.scrollIntoView?.({ block: "center", behavior: "auto" });
    }
  }

  _tabsKeydown(event) {
    const tabs = [...this.root.querySelectorAll('[role="tab"]')];
    const index = tabs.indexOf(event.currentTarget);
    let next = null;
    if (["ArrowRight", "ArrowDown"].includes(event.key)) next = (index + 1) % tabs.length;
    if (["ArrowLeft", "ArrowUp"].includes(event.key)) next = (index - 1 + tabs.length) % tabs.length;
    if (event.key === "Home") next = 0;
    if (event.key === "End") next = tabs.length - 1;
    if (next === null) return;
    event.preventDefault();
    const route = tabs[next].dataset.route;
    this.context.navigation.navigate(route, { focus: `#palworld-tab-${route}` });
  }

  _bind() {
    this.root.querySelector("#palworld-route-select")?.addEventListener("change", (event) => {
      this.context.navigation.navigate(event.target.value, { focus: "#palworld-route-select" });
    });
    this.root.querySelectorAll("[data-route]").forEach((button) => {
      button.addEventListener("click", () => this.context.navigation.navigate(button.dataset.route, { focus: `#palworld-tab-${button.dataset.route}` }));
      if (button.getAttribute("role") === "tab") button.addEventListener("keydown", (event) => this._tabsKeydown(event));
    });
    this.root.querySelector("#review-player-reporting")?.addEventListener("click", () => {
      this.focusTarget = "#player-reporting-title";
      this.context.navigation.navigate("game-settings", { focus: "#player-reporting-title" });
    });
    this.root.querySelector("#auto-toggle")?.addEventListener("click", () => this._runEntity(
      "auto_shutdown",
      truth(this.snapshot, "auto_shutdown") === true ? "turn_off" : "turn_on",
      this._t("feedback.update_auto"),
    ));
    this.root.querySelector("#cancel-shutdown")?.addEventListener("click", () => this._runEntity(
      "cancel_pending_shutdown",
      "press",
      this._t("feedback.cancel_shutdown"),
    ));
    this.root.querySelectorAll("[data-number-action]").forEach((button) => button.addEventListener("click", () => {
      const input = this.root.querySelector(`#${button.dataset.numberAction}`);
      if (!input) return;
      const value = Number(input.value);
      if (!String(input.value).trim() || !input.checkValidity() || !Number.isFinite(value)) {
        input.reportValidity();
        this._setFeedback("error", this._t("feedback.invalid_timing"), "invalid_timing_value");
        return;
      }
      this._runEntity(button.dataset.numberAction, "set_value", this._t("feedback.save_timing"), value);
    }));
    this.root.querySelectorAll('input[name="player-reporting-choice"]').forEach((input) => {
      input.addEventListener("click", (event) => { if (this.pending) event.preventDefault(); });
      input.addEventListener("change", () => this._saveReporting(input.value === "enabled"));
    });
    this.root.querySelector("#open-settings-editor")?.addEventListener("click", () => this._openSettingsEditor());
    this.root.querySelector("#download-save-bundle")?.addEventListener("click", () => this._downloadSaveBundle());
    this.root.querySelector("#save-bundle-upload")?.addEventListener("change", (event) => {
      const file = event.target.files?.[0];
      if (file) this._inspectSaveBundle(file);
    });
    this.root.querySelector("#apply-save-bundle")?.addEventListener("click", () => this._applySaveBundle());
    this.root.querySelector("#copy-address")?.addEventListener("click", () => this._copyAddress());
    this._bindEditorShell();
  }

  async _copyAddress() {
    if (this.pending) return;
    const generation = this._captureGeneration();
    const address = state(this.snapshot, "address", "");
    const result = this.root?.querySelector("#copy-address-result");
    try {
      if (!address || !globalThis.navigator?.clipboard?.writeText) throw new Error(this._t("clipboard.unavailable"));
      await globalThis.navigator.clipboard.writeText(address);
      if (!this._ownsGeneration(generation)) return;
      if (result) result.textContent = this._t("clipboard.copied");
    } catch (_error) {
      if (!this._ownsGeneration(generation)) return;
      if (result) result.textContent = this._t("clipboard.manual");
    }
    result?.focus();
  }

  _bindEditorShell() {
    const bind = (element, eventName, handler) => {
      if (!element || element.dataset.editorShellBound === "true") return;
      element.dataset.editorShellBound = "true";
      element.addEventListener(eventName, handler);
    };
    bind(this.root.querySelector("#editor-close"), "click", () => this._closeSettingsEditor());
    bind(this.root.querySelector("#settings-search"), "input", (event) => {
      if (!this.editor) return;
      const filtersWereActive = this._editorFiltersActive();
      this.editor.query = event.target.value;
      if (!filtersWereActive && this._editorFiltersActive()) this.editor.category = "all";
      this._renderEditorResults();
    });
    this.root.querySelectorAll("[data-editor-scope]").forEach((button) => bind(button, "click", () => {
      const filtersWereActive = this._editorFiltersActive();
      this.editor.scope = button.dataset.editorScope;
      if (!filtersWereActive && this._editorFiltersActive()) this.editor.category = "all";
      this._renderEditorResults();
    }));
    bind(this.root.querySelector("#editor-undo-all"), "click", () => this._undoAllSettings());
    bind(this.root.querySelector("#editor-category-select"), "change", (event) => {
      this.editor.category = event.target.value;
      this._renderEditorResults();
      this.root.querySelector("#editor-category-select")?.focus();
    });
    bind(this.root.querySelector("#editor-retry"), "click", () => this._retrySettingsEditor());
    bind(this.root.querySelector("#palworld-settings-editor"), "cancel", (event) => {
      event.preventDefault();
      this._closeSettingsEditor();
    });
    this._bindEditorResults();
  }

  _bindEditorResults() {
    const bind = (element, eventName, handler) => {
      if (!element || element.dataset.editorBound === "true") return;
      element.dataset.editorBound = "true";
      element.addEventListener(eventName, handler);
    };
    bind(this.root.querySelector("#editor-preview"), "click", () => this._previewSettings());
    bind(this.root.querySelector("#editor-apply"), "click", () => this._applySettings());
    this.root.querySelectorAll("[data-editor-category]").forEach((button) => bind(button, "click", () => {
      this.editor.category = button.dataset.editorCategory;
      this._renderEditorResults();
      this.root.querySelector(`[data-editor-category="${button.dataset.editorCategory}"]`)?.focus();
    }));
    this.root.querySelectorAll("[data-setting-key]").forEach((control) => bind(control, "change", () => {
      this._changeSetting(control.dataset.settingKey, control);
    }));
    this.root.querySelectorAll("[data-secret-action]").forEach((control) => bind(control, "change", () => {
      this._changeSecretAction(control.dataset.secretAction, control.value);
    }));
    this.root.querySelectorAll("[data-secret-value]").forEach((control) => bind(control, "input", () => {
      this._changeSecretValue(control.dataset.secretValue, control.value);
    }));
    this.root.querySelectorAll("[data-undo-setting]").forEach((button) => bind(button, "click", () => this._undoSetting(button.dataset.undoSetting)));
    this.root.querySelectorAll("[data-default-setting]").forEach((button) => bind(button, "click", () => this._defaultSetting(button.dataset.defaultSetting)));
    bind(this.root.querySelector("#fix-pvp-requirements"), "click", () => this._fixPvpRequirements());
    bind(this.root.querySelector("#editor-clear-filters"), "click", () => {
      this.editor.query = "";
      this.editor.scope = "all";
      const search = this.root.querySelector("#settings-search");
      if (search) search.value = "";
      this.root.querySelectorAll("[data-editor-scope]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.editorScope === "all")));
      this._renderEditorResults();
      search?.focus();
    });
    this.root.querySelectorAll("[data-recovery-id]").forEach((button) => bind(button, "click", () => this._rollbackSettings(button.dataset.recoveryId)));
    this._restoreSecretControls();
  }

  async _runEntity(key, action, pendingText, value = undefined) {
    if (this.pending || this._operationBlocked()) return;
    const generation = this._captureGeneration();
    const active = this.root?.getRootNode()?.activeElement;
    if (active?.id && this.root.contains(active)) this.focusTarget = `#${active.id}`;
    this.pending = key;
    this._setFeedback("status", pendingText, "mutation_pending");
    this._publishState();
    this.render();
    try {
      await this.context.operations.entityAction(key, action, value);
      if (!this._ownsGeneration(generation)) return;
      this._setFeedback("success", this._t("feedback.accepted"), "mutation_accepted");
    } catch (error) {
      if (!this._ownsGeneration(generation)) return;
      this._setFeedback("error", error?.message || this._t("feedback.failed"), "mutation_failed");
    } finally {
      if (!this._ownsGeneration(generation)) return;
      this.pending = null;
      this._publishState();
      this.render();
    }
  }

  async _saveReporting(value) {
    if (this.pending || this._operationBlocked()) return;
    const generation = this._captureGeneration();
    const active = this.root?.getRootNode()?.activeElement;
    const radioId = active?.id || `player-reporting-${value ? "enabled" : "disabled"}`;
    this.focusTarget = `#${radioId}`;
    this.pending = "allow_insecure_rest";
    this._setFeedback("status", this._t("reporting.saving"), "reporting_decision_pending");
    this._publishState();
    this.render();
    try {
      await this.context.extensions.updateProfileOption("allow_insecure_rest", value, { confirmed: true });
      if (!this._ownsGeneration(generation)) return;
      this._setFeedback("success", this._t("reporting.saved"), "reporting_decision_saved");
      try {
        await this.context.operations.refreshSnapshot();
        if (!this._ownsGeneration(generation)) return;
      } catch (_error) {
        if (!this._ownsGeneration(generation)) return;
        this._setFeedback("success", this._t("reporting.refresh_unavailable"), "reporting_refresh_unavailable");
      }
    } catch (error) {
      if (!this._ownsGeneration(generation)) return;
      this._setFeedback("error", error?.message || this._t("reporting.not_saved"), "reporting_decision_failed");
    } finally {
      if (!this._ownsGeneration(generation)) return;
      this.pending = null;
      this._publishState();
      this.focusTarget = `#${radioId}`;
      this.render();
    }
  }

  _saveBundlePrerequisite() {
    const provider = this.snapshot?.provider || {};
    if (!provider.status_fresh || provider.using_cached_data) return this._t("prerequisite.refresh");
    if (provider.status !== "stopped") return this._t("prerequisite.stop_save");
    if (this._operationBlocked()) return this._t("prerequisite.operation");
    return "";
  }

  async _downloadSaveBundle() {
    if (this.pending || this._saveBundlePrerequisite()) return;
    const generation = this._captureGeneration();
    this.pending = "save-download";
    this._saveAnnouncementKey = "";
    this.saveBundleProgress = { message: this._t("save.starting"), stage: "starting" };
    this._setSaveFeedback("status", this._t("save.starting"), "save_bundle_download_pending");
    this._publishState();
    if (!this._syncSaveBundleOperationArea()) this.render();
    let completionCode = "save_bundle_downloaded";
    try {
      const result = await this.context.extensions.downloadSaveBundle(
        "world",
        (progress) => { if (this._ownsGeneration(generation)) this._updateSaveBundleProgress(progress); },
      );
      if (!this._ownsGeneration(generation)) return;
      this._setSaveFeedback("success", this._t("save.downloaded", { filename: result?.filename || this._t("save.default_filename") }), completionCode, false);
    } catch (error) {
      if (!this._ownsGeneration(generation)) return;
      completionCode = "save_bundle_download_failed";
      this._setSaveFeedback("error", error?.message || this._t("save.download_failed"), completionCode, false);
    } finally {
      if (!this._ownsGeneration(generation)) return;
      this.pending = null;
      this.saveBundleProgress = null;
      this._syncSaveBundleOperationArea();
      this._publishState();
      this.focusTarget = "#download-save-bundle";
      this.render();
      this._announceSave(this.saveBundleResult.text, completionCode, true);
    }
  }

  async _inspectSaveBundle(file) {
    if (this.pending || this._saveBundlePrerequisite()) return;
    const generation = this._captureGeneration();
    this.pending = "save-inspect";
    this._saveAnnouncementKey = "";
    this.saveBundle = { preview: null, previewToken: null, error: "" };
    this.saveBundleProgress = { message: this._t("save.checking"), stage: "checking_zip" };
    this._setSaveFeedback("status", this._t("save.inspecting"), "save_bundle_review_pending");
    this._publishState();
    if (!this._syncSaveBundleOperationArea()) this.render();
    try {
      const result = await this.context.extensions.inspectSaveBundle(
        "world",
        file,
        (progress) => { if (this._ownsGeneration(generation)) this._updateSaveBundleProgress(progress); },
      );
      if (!this._ownsGeneration(generation)) return;
      if (!result?.preview) throw new Error(this._t("save.no_review"));
      this.saveBundle = {
        preview: result.preview,
        previewToken: typeof result.preview_token === "string" ? result.preview_token : null,
        restoreEnabled: result.restore_enabled === true,
        error: "",
      };
      this._setSaveFeedback(
        "success",
        result.preview.changed ? this._t("save.review_changed") : this._t("save.review_identical"),
        "save_bundle_review_ready",
        false,
      );
    } catch (error) {
      if (!this._ownsGeneration(generation)) return;
      this.saveBundle = { preview: null, previewToken: null, error: error?.message || this._t("save.inspect_failed") };
      this._setSaveFeedback("error", this.saveBundle.error, "save_bundle_review_failed", false);
    } finally {
      if (!this._ownsGeneration(generation)) return;
      this.pending = null;
      this.saveBundleProgress = null;
      this._syncSaveBundleOperationArea();
      this._publishState();
      this.focusTarget = this.saveBundle.preview ? "#save-bundle-review" : "#save-bundle-operation-result";
      this.render();
    }
  }

  async _applySaveBundle() {
    if (this.pending || !this.saveBundle.previewToken || !this.saveBundle.restoreEnabled || this._saveBundlePrerequisite()) return;
    const generation = this._captureGeneration();
    const previewToken = this.saveBundle.previewToken;
    const counts = this.saveBundle.preview?.counts || {};
    const accepted = await this.context.host.confirm({
      title: this._t("save.restore_title"),
      explanation: this._t("save.restore_help", { replaced: counts.replaced || 0, added: counts.added || 0 }),
      confirmLabel: this._t("save.restore_confirm"),
      destructive: true,
    });
    if (!accepted || !this._ownsGeneration(generation) || this.pending
      || previewToken !== this.saveBundle.previewToken
      || !this.saveBundle.restoreEnabled || this._saveBundlePrerequisite()) return;
    this.pending = "save-apply";
    this._saveAnnouncementKey = "";
    this.saveBundleResult = { kind: "status", text: this._t("save.restoring") };
    this._setSaveFeedback("status", this._t("save.restoring"), "save_bundle_restore_pending");
    this._publishState();
    this.render();
    try {
      const result = await this.context.extensions.applySaveBundle("world", previewToken);
      if (!this._ownsGeneration(generation)) return;
      this.saveBundle = { preview: null, previewToken: null, error: "" };
      this.saveBundleResult = { kind: "success", text: this._t("save.restored", { audit: result?.operation_id || this._t("save.audit_recorded") }) };
      this._setSaveFeedback("success", this.saveBundleResult.text, "save_bundle_restored", false);
      await this.context.operations.refreshSnapshot();
      if (!this._ownsGeneration(generation)) return;
    } catch (error) {
      if (!this._ownsGeneration(generation)) return;
      this.saveBundle.error = error?.message || this._t("save.restore_failed");
      this.saveBundleResult = { kind: "error", text: this.saveBundle.error };
      this._setSaveFeedback("error", this.saveBundle.error, "save_bundle_restore_failed", false);
    } finally {
      if (!this._ownsGeneration(generation)) return;
      this.pending = null;
      this._publishState();
      this.focusTarget = "#save-bundle-operation-result";
      this.render();
    }
  }

  _editorWritePrerequisite() {
    const provider = this.snapshot?.provider || {};
    if (!provider.status_fresh || provider.using_cached_data) return this._t("prerequisite.refresh_editor");
    if (provider.status !== "stopped") return this._t("prerequisite.stop_editor");
    if (this._operationBlocked()) return this._t("prerequisite.operation_editor");
    return "";
  }

  _editorMutationsEnabled() {
    return this.snapshot?.profile?.editor_mutations_enabled === true;
  }

  _draftStorageIdentity() {
    const identity = this.snapshot?.identity || this.context?.identity || {};
    return [
      identity.account_entry_id,
      identity.service_id,
      identity.profile_id || cockpitMetadata.key,
      "settings",
    ].map((part) => encodeURIComponent(String(part || "unknown"))).join(":");
  }

  _draftStorageKey() {
    if (!this.editor?.schemaRevision || !this.editor?.sourceRevision) return "";
    return `${EDITOR_DRAFT_STORAGE_PREFIX}${this._draftStorageIdentity()}:${encodeURIComponent(this.editor.schemaRevision)}:${encodeURIComponent(this.editor.sourceRevision)}`;
  }

  _draftStorage() {
    try { return globalThis.sessionStorage || null; } catch (_error) { return null; }
  }

  _clearEditorDraftsForIdentity() {
    const storage = this._draftStorage();
    if (!storage) return;
    const prefix = `${EDITOR_DRAFT_STORAGE_PREFIX}${this._draftStorageIdentity()}:`;
    try {
      for (let index = storage.length - 1; index >= 0; index -= 1) {
        const key = storage.key(index);
        if (key?.startsWith(prefix)) storage.removeItem(key);
      }
    } catch (_error) { /* Browser storage is optional; drafts still remain in memory. */ }
  }

  _clearStaleEditorDrafts(currentKey) {
    const storage = this._draftStorage();
    if (!storage) return;
    const prefix = `${EDITOR_DRAFT_STORAGE_PREFIX}${this._draftStorageIdentity()}:`;
    try {
      for (let index = storage.length - 1; index >= 0; index -= 1) {
        const key = storage.key(index);
        if (key?.startsWith(prefix) && key !== currentKey) storage.removeItem(key);
      }
    } catch (_error) { /* Optional storage. */ }
  }

  _pruneEditorDrafts(currentKey = "") {
    const storage = this._draftStorage();
    if (!storage) return;
    try {
      const records = [];
      for (let index = 0; index < storage.length; index += 1) {
        const key = storage.key(index);
        if (!key?.startsWith(EDITOR_DRAFT_STORAGE_PREFIX) || key === currentKey) continue;
        let createdAt = 0;
        try { createdAt = Number(JSON.parse(storage.getItem(key) || "null")?.createdAt) || 0; } catch (_error) { createdAt = 0; }
        records.push({ key, createdAt });
      }
      records.sort((left, right) => right.createdAt - left.createdAt);
      records.slice(Math.max(EDITOR_DRAFT_MAX_RECORDS - (currentKey ? 1 : 0), 0)).forEach(({ key }) => storage.removeItem(key));
    } catch (_error) { /* Quota/security failures must not break the editor. */ }
  }

  _persistEditorDraft() {
    const storage = this._draftStorage();
    const key = this._draftStorageKey();
    if (!storage || !key || !this.editor) return;
    if (!this.editor.dirty) {
      try { storage.removeItem(key); } catch (_error) { /* Optional storage. */ }
      return;
    }
    const changed = this.editor.fields.filter((field) => this._fieldChanged(field));
    const record = {
      version: EDITOR_DRAFT_STORAGE_VERSION,
      createdAt: Date.now(),
      schemaRevision: this.editor.schemaRevision,
      sourceRevision: this.editor.sourceRevision,
      changes: changed.filter((field) => !field.sensitive).map((field) => ({ key: field.key, draftRaw: field.draftRaw })),
      omittedSensitive: changed.some((field) => field.sensitive),
    };
    const serialized = JSON.stringify(record);
    if (serialized.length > EDITOR_DRAFT_MAX_BYTES) {
      this.editor.recoveryWarning = this._t("draft.too_large");
      return;
    }
    try {
      storage.setItem(key, serialized);
      this._pruneEditorDrafts(key);
    } catch (_error) {
      this.editor.recoveryWarning = this._t("draft.unavailable");
    }
  }

  _restoreEditorDraft() {
    const storage = this._draftStorage();
    const key = this._draftStorageKey();
    if (!storage || !key || !this.editor) return false;
    this._clearStaleEditorDrafts(key);
    let parsed;
    try {
      const serialized = storage.getItem(key);
      if (!serialized) { this._pruneEditorDrafts(); return false; }
      if (serialized.length > EDITOR_DRAFT_MAX_BYTES) throw new Error("oversized");
      parsed = JSON.parse(serialized);
      if (parsed?.version !== EDITOR_DRAFT_STORAGE_VERSION
        || parsed.schemaRevision !== this.editor.schemaRevision
        || parsed.sourceRevision !== this.editor.sourceRevision
        || !Array.isArray(parsed.changes)) throw new Error("mismatch");
    } catch (_error) {
      try { storage.removeItem(key); } catch (_storageError) { /* Optional storage. */ }
      this.editor.recoveryWarning = this._t("draft.invalid");
      return false;
    }
    let restored = 0;
    let partial = parsed.omittedSensitive === true;
    const seen = new Set();
    for (const item of parsed.changes) {
      if (!item || typeof item.key !== "string" || typeof item.draftRaw !== "string"
        || item.draftRaw.length > 32768 || seen.has(item.key)) { partial = true; continue; }
      seen.add(item.key);
      const field = this._editorField(item.key);
      if (!field || field.sensitive) { partial = true; continue; }
      field.draftRaw = item.draftRaw;
      restored += 1;
    }
    this.editor.dirty = this.editor.fields.some((field) => this._fieldChanged(field));
    if (this.editor.dirty) this.editor.draftEpoch = 1;
    if (restored || partial) {
      const suffix = partial ? " Sensitive or no-longer-safe fields were intentionally omitted." : "";
      this.editor.operation = { kind: "success", text: this._t("draft.recovered", { count: restored, suffix: restored === 1 ? "" : "s", detail: suffix }), recovered: true };
      this.editor.recoveryWarning = partial ? this._t("draft.partial") : "";
    }
    this._pruneEditorDrafts(key);
    return restored > 0;
  }

  async _openSettingsEditor() {
    if (this.pending || this._operationBlocked()) return;
    const generation = this._captureGeneration();
    this._secretDrafts.clear();
    this.pending = "settings-load";
    this.editor = { open: true, loading: true, fields: [], dirty: false, draftEpoch: 0, preview: null, previewToken: null, history: [], error: "", query: "", scope: "all", category: "favorites", operation: { kind: "status", text: this._t("editor.loading_operation") } };
    this._publishState();
    this.render();
    try {
      const loaded = await this.context.extensions.readEditableFile("settings");
      if (!this._ownsGeneration(generation) || !this.editor?.open) return;
      const model = loaded?.file?.model;
      if (!model || !Array.isArray(model.settings)) throw new Error(this._t("editor.no_model"));
      this.editor.sourceRevision = loaded.file.revision;
      this.editor.schemaRevision = model.schema_revision || "unknown";
      this.editor.redactedSource = model.redacted_source || "";
      this.editor.fields = model.settings.map((item) => ({
        key: item.key,
        sensitive: item.sensitive === true,
        configured: item.configured === true,
        originalRaw: item.sensitive ? null : item.raw_value,
        draftRaw: item.sensitive ? null : item.raw_value,
        secretAction: "keep",
      }));
      this._restoreEditorDraft();
      try {
        const history = await this.context.extensions.editableFileHistory("settings");
        if (!this._ownsGeneration(generation) || !this.editor?.open) return;
        this.editor.history = Array.isArray(history?.history) ? history.history : [];
      } catch (error) {
        if (!this._ownsGeneration(generation) || !this.editor?.open) return;
        this.editor.historyError = error?.message || this._t("editor.history_unavailable");
      }
    } catch (error) {
      if (!this._ownsGeneration(generation) || !this.editor?.open) return;
      this.editor.error = error?.message || this._t("editor.open_failed");
      this.editor.operation = { kind: "error", text: this.editor.error };
    } finally {
      if (!this._ownsGeneration(generation) || !this.editor?.open) return;
      this.pending = null;
      this.editor.loading = false;
      if (!this.editor.error && !this.editor.operation?.recovered) this.editor.operation = { kind: "success", text: this._t("editor.loaded") };
      this._publishState();
      this._syncEditorDialog();
      this.root.querySelector(this.editor.error ? "#editor-retry" : "#settings-search")?.focus();
    }
  }

  async _retrySettingsEditor() {
    if (this.pending || !this.editor?.open) return;
    const dialog = this.root?.querySelector("#palworld-settings-editor");
    if (!dialog) return;
    const generation = this._captureGeneration();
    this.pending = "settings-load";
    this.editor.loading = true;
    this.editor.error = "";
    this.editor.fields = [];
    this.editor.operation = { kind: "status", text: this._t("editor.retrying") };
    this._publishState();
    this._syncEditorDialog();
    try {
      const loaded = await this.context.extensions.readEditableFile("settings");
      if (!this._ownsGeneration(generation) || !this.editor?.open) return;
      this._replaceEditorModel(loaded?.file);
      const history = await this.context.extensions.editableFileHistory("settings");
      if (!this._ownsGeneration(generation) || !this.editor?.open) return;
      this.editor.history = Array.isArray(history?.history) ? history.history : [];
      this.editor.operation = { kind: "success", text: this._t("editor.loaded") };
    } catch (error) {
      if (!this._ownsGeneration(generation) || !this.editor?.open) return;
      this.editor.error = error?.message || this._t("editor.open_failed");
      this.editor.operation = { kind: "error", text: this.editor.error };
    } finally {
      if (!this._ownsGeneration(generation) || !this.editor?.open) return;
      this.pending = null;
      this.editor.loading = false;
      this._publishState();
      this._syncEditorDialog();
      this.root.querySelector(this.editor.error ? "#editor-retry" : "#settings-search")?.focus();
    }
  }

  async _closeSettingsEditor() {
    if (!this.editor || this.pending) return;
    const generation = this._captureGeneration();
    const draftEpoch = this.editor.draftEpoch;
    if (this.editor.dirty) {
      const accepted = await this.context.host.confirm({
        title: this._t("editor.discard_title"),
        explanation: this._t("editor.discard_help"),
        confirmLabel: this._t("editor.discard_confirm"),
        destructive: true,
      });
      if (!accepted || !this._ownsGeneration(generation) || this.pending
        || !this.editor?.dirty || draftEpoch !== this.editor.draftEpoch) return;
    }
    this._clearEditorDraftsForIdentity();
    this._secretDrafts.clear();
    this.editor = null;
    this._publishState();
    this.focusTarget = "#open-settings-editor";
    this.render();
  }

  _syncEditorButtons() {
    const preview = this.root?.querySelector("#editor-preview");
    const apply = this.root?.querySelector("#editor-apply");
    if (preview) preview.disabled = Boolean(this.pending) || !this.editor?.dirty || Boolean(this._editorWritePrerequisite()) || Boolean(this._editorIssues().size);
    if (apply) apply.disabled = Boolean(this.pending)
      || !this._editorMutationsEnabled()
      || Boolean(this._editorWritePrerequisite())
      || !this.editor?.previewToken
      || this.editor?.preview?.verdict?.state !== "supported";
  }

  async _previewSettings() {
    if (this.pending || !this.editor?.dirty) return;
    const generation = this._captureGeneration();
    if (this._editorWritePrerequisite()) return;
    if (this._editorIssues().size) return;
    const proposal = this._editorProposal();
    const draftEpoch = this.editor.draftEpoch;
    this.pending = "settings-preview";
    this.editor.operation = { kind: "status", text: this._t("editor.validating") };
    this.editor.error = "";
    this.editor.preview = null;
    this.editor.previewToken = null;
    this._publishState();
    this._syncEditorChrome();
    try {
      const result = await this.context.extensions.previewEditableFile("settings", proposal);
      if (!this._ownsGeneration(generation) || !this.editor || this.editor.draftEpoch !== draftEpoch) return;
      this.editor.preview = result?.preview || null;
      this.editor.previewToken = typeof result?.preview_token === "string" ? result.preview_token : null;
      if (!this.editor.preview) throw new Error(this._t("editor.no_preview"));
      this.editor.operation = this.editor.preview?.verdict?.state === "supported"
        ? { kind: "success", text: this._t("editor.review_ready") }
        : { kind: "error", text: this._t("editor.preview_blocked") };
    } catch (error) {
      if (this._ownsGeneration(generation) && this.editor?.draftEpoch === draftEpoch) {
        this.editor.error = error?.message || this._t("editor.preview_failed");
        this.editor.operation = { kind: "error", text: this.editor.error };
      }
    } finally {
      if (!this._ownsGeneration(generation) || !this.editor) return;
      this.pending = null;
      this._publishState();
      this._syncEditorChrome();
      if (this.editor.draftEpoch === draftEpoch) this.root.querySelector("#editor-preview")?.focus();
    }
  }

  async _applySettings() {
    if (this.pending || !this.editor?.previewToken || !this._editorMutationsEnabled()) return;
    if (this._editorWritePrerequisite()) return;
    const generation = this._captureGeneration();
    const draftEpoch = this.editor.draftEpoch;
    const previewToken = this.editor.previewToken;
    const accepted = await this.context.host.confirm({
      title: this._t("editor.apply_title"),
      explanation: this._t("editor.apply_help"),
      confirmLabel: this._t("editor.apply_confirm"),
      destructive: true,
    });
    if (!accepted || !this._ownsGeneration(generation) || this.pending
      || !this.editor || this.editor.draftEpoch !== draftEpoch
      || this.editor.previewToken !== previewToken
      || !this._editorMutationsEnabled() || this._editorWritePrerequisite()) return;
    const proposal = this._editorProposal();
    this.pending = "settings-apply";
    this.editor.operation = { kind: "status", text: this._t("editor.applying") };
    this._publishState();
    this._syncEditorChrome();
    try {
      const result = await this.context.extensions.applyEditableFile("settings", proposal, previewToken);
      if (!this._ownsGeneration(generation) || !this.editor) return;
      const loaded = await this.context.extensions.readEditableFile("settings");
      if (!this._ownsGeneration(generation) || !this.editor) return;
      this._replaceEditorModel(loaded?.file);
      this.editor.operation = { kind: "success", text: result?.requires_restart ? this._t("editor.applied_restart") : this._t("editor.applied") };
      this._setFeedback("success", this.editor.operation.text, "settings_applied", false);
      const history = await this.context.extensions.editableFileHistory("settings");
      if (!this._ownsGeneration(generation) || !this.editor) return;
      this.editor.history = Array.isArray(history?.history) ? history.history : [];
    } catch (error) {
      if (!this._ownsGeneration(generation) || !this.editor) return;
      this.editor.error = error?.message || this._t("editor.apply_failed");
      this.editor.operation = { kind: "error", text: this.editor.error };
    } finally {
      if (!this._ownsGeneration(generation) || !this.editor) return;
      this.pending = null;
      this._publishState();
      this._syncEditorChrome();
      this.root.querySelector("#editor-close")?.focus();
    }
  }

  async _rollbackSettings(recoveryId) {
    if (this.pending || !this._editorMutationsEnabled() || !recoveryId) return;
    const generation = this._captureGeneration();
    const accepted = await this.context.host.confirm({
      title: this._t("editor.rollback_title"),
      explanation: this._t("editor.rollback_help"),
      confirmLabel: this._t("editor.rollback_confirm"),
      destructive: true,
    });
    if (!accepted || !this._ownsGeneration(generation) || this.pending
      || !this._editorMutationsEnabled() || !this.editor?.history?.some((item) => item.recovery_id === recoveryId)) return;
    this.pending = "settings-rollback";
    this.editor.operation = { kind: "status", text: this._t("editor.rolling_back") };
    this._publishState();
    this._syncEditorDialog();
    try {
      await this.context.extensions.rollbackEditableFile("settings", recoveryId);
      if (!this._ownsGeneration(generation) || !this.editor) return;
      const loaded = await this.context.extensions.readEditableFile("settings");
      if (!this._ownsGeneration(generation) || !this.editor) return;
      this._replaceEditorModel(loaded?.file);
      const history = await this.context.extensions.editableFileHistory("settings");
      if (!this._ownsGeneration(generation) || !this.editor) return;
      this.editor.history = Array.isArray(history?.history) ? history.history : [];
      this.editor.operation = { kind: "success", text: this._t("editor.rolled_back") };
      this._setFeedback("success", this.editor.operation.text, "settings_rolled_back", false);
    } catch (error) {
      if (!this._ownsGeneration(generation) || !this.editor) return;
      this.editor.error = error?.message || this._t("editor.rollback_failed");
      this.editor.operation = { kind: "error", text: this.editor.error };
    } finally {
      if (!this._ownsGeneration(generation) || !this.editor) return;
      this.pending = null;
      this._publishState();
      this._syncEditorDialog();
      this.root.querySelector("#editor-close")?.focus();
    }
  }

  _replaceEditorModel(file) {
    const model = file?.model;
    if (!model || !Array.isArray(model.settings)) throw new Error(this._t("editor.no_model"));
    this.editor.sourceRevision = file.revision;
    this.editor.schemaRevision = model.schema_revision || "unknown";
    this.editor.redactedSource = model.redacted_source || "";
    this.editor.fields = model.settings.map((item) => ({
      key: item.key, sensitive: item.sensitive === true, configured: item.configured === true,
      originalRaw: item.sensitive ? null : item.raw_value, draftRaw: item.sensitive ? null : item.raw_value, secretAction: "keep",
    }));
    this.editor.dirty = false;
    this.editor.draftEpoch = 0;
    this.editor.preview = null;
    this.editor.previewToken = null;
    this._clearEditorDraftsForIdentity();
    this._secretDrafts.clear();
  }

  _editorField(key) {
    return this.editor?.fields.find((field) => field.key === key) || null;
  }

  _fieldChanged(field) {
    return field.sensitive ? field.secretAction !== "keep" : field.draftRaw !== field.originalRaw;
  }

  _secretReplacementRaw(key) {
    return encodeQuoted(this._secretDrafts.get(key) || "");
  }

  _restoreSecretControls() {
    for (const [key, value] of this._secretDrafts) {
      const control = this.root?.querySelector(`[data-secret-value="${key}"]`);
      if (control instanceof HTMLInputElement) control.value = value;
    }
  }

  _editorOperations(includeSecretValues = false) {
    return (this.editor?.fields || []).filter((field) => this._fieldChanged(field)).map((field) => ({
      op: "set",
      key: field.key,
      raw_value: field.sensitive
        ? field.secretAction === "clear" ? '""' : includeSecretValues ? this._secretReplacementRaw(field.key) : "[hidden]"
        : field.draftRaw,
    }));
  }

  _editorProposal() {
    return { operations: this._editorOperations(true), sourceRevision: this.editor.sourceRevision };
  }

  _invalidateEditorPreview() {
    if (!this.editor) return;
    this.editor.dirty = this.editor.fields.some((field) => this._fieldChanged(field));
    this.editor.preview = null;
    this.editor.previewToken = null;
    this.editor.error = "";
    this.editor.draftEpoch = (this.editor.draftEpoch || 0) + 1;
    this._persistEditorDraft();
    this._publishState();
  }

  _changeSetting(key, control) {
    const field = this._editorField(key);
    if (!field || field.sensitive) return;
    const previousKeys = this._filteredEditorFields().map((item) => item.key);
    const previousIndex = previousKeys.indexOf(key);
    const hadFocus = this.root?.activeElement === control || this.root?.getRootNode()?.activeElement === control;
    const meta = this._settingMeta({ key, rawValue: field.draftRaw });
    if (meta.type === "boolean") field.draftRaw = control.checked ? "True" : "False";
    else if (meta.type === "string") field.draftRaw = encodeQuoted(control.value);
    else field.draftRaw = control.value;
    this._invalidateEditorPreview();
    this._refreshEditorPresentation(key);
    if (hadFocus && !control.isConnected) this._focusFilteredEditorFallback(previousIndex);
  }

  _changeSecretAction(key, action) {
    const field = this._editorField(key);
    if (!field?.sensitive || !["keep", "replace", "clear"].includes(action)) return;
    const previousKeys = this._filteredEditorFields().map((item) => item.key);
    const previousIndex = previousKeys.indexOf(key);
    field.secretAction = action;
    field.draftRaw = null;
    if (action === "replace") {
      if (!this._secretDrafts.has(key)) this._secretDrafts.set(key, "");
    } else {
      this._secretDrafts.delete(key);
    }
    this._invalidateEditorPreview();
    if (this._filteredEditorFields().some((candidate) => candidate.key === key)) {
      this._replaceSettingCard(key);
      this.root.querySelector(action === "replace" ? `#setting-${key}` : `#setting-${key}-action`)?.focus();
    } else {
      this._renderEditorResults();
      this._focusFilteredEditorFallback(previousIndex);
    }
  }

  _changeSecretValue(key, value) {
    const field = this._editorField(key);
    if (!field?.sensitive || field.secretAction !== "replace") return;
    this._secretDrafts.set(key, value);
    this._invalidateEditorPreview();
    this._refreshEditorPresentation(key, { preserveSecretControl: true });
  }

  _undoSetting(key) {
    const field = this._editorField(key);
    if (!field) return;
    const previousKeys = this._filteredEditorFields().map((item) => item.key);
    const previousIndex = previousKeys.indexOf(key);
    if (field.sensitive) { field.secretAction = "keep"; field.draftRaw = null; this._secretDrafts.delete(key); }
    else field.draftRaw = field.originalRaw;
    this._invalidateEditorPreview();
    if (field.sensitive) this._replaceSettingCard(key);
    else this._refreshEditorPresentation(key);
    const target = this.root.querySelector(`#setting-${key}${field.sensitive ? "-action" : ""}`);
    if (target) target.focus();
    else this._focusFilteredEditorFallback(previousIndex);
  }

  _defaultSetting(key) {
    const field = this._editorField(key);
    if (!field || field.sensitive || DEFAULTS[key] === undefined) return;
    const previousKeys = this._filteredEditorFields().map((item) => item.key);
    const previousIndex = previousKeys.indexOf(key);
    field.draftRaw = DEFAULTS[key];
    this._invalidateEditorPreview();
    this._refreshEditorPresentation(key);
    const target = this.root.querySelector(`#setting-${key}`);
    if (target) target.focus();
    else this._focusFilteredEditorFallback(previousIndex);
  }

  _focusFilteredEditorFallback(previousIndex) {
    const visible = this._filteredEditorFields();
    const candidate = visible[Math.min(Math.max(previousIndex, 0), Math.max(visible.length - 1, 0))]
      || visible[Math.max(previousIndex - 1, 0)];
    const target = candidate
      ? this.root.querySelector(`#setting-${candidate.key}, #setting-${candidate.key}-action`)
      : this.root.querySelector("#editor-category-title");
    target?.focus();
  }

  async _undoAllSettings() {
    const changedFields = (this.editor?.fields || []).filter((field) => this._fieldChanged(field));
    if (!changedFields.length || this.pending) return;
    const generation = this._captureGeneration();
    const draftEpoch = this.editor.draftEpoch;
    if (changedFields.length > 1 || changedFields.some((field) => field.sensitive)) {
      const accepted = await this.context.host.confirm({
        title: this._t("editor.undo_title"),
        explanation: this._t("editor.undo_help", { count: changedFields.length, suffix: changedFields.length === 1 ? "" : "s" }),
        confirmLabel: this._t("editor.undo_confirm"),
        destructive: true,
      });
      if (!accepted || !this._ownsGeneration(generation) || this.pending
        || !this.editor || this.editor.draftEpoch !== draftEpoch) return;
    }
    for (const field of this.editor?.fields || []) {
      if (field.sensitive) { field.secretAction = "keep"; field.draftRaw = null; }
      else field.draftRaw = field.originalRaw;
    }
    this._secretDrafts.clear();
    this._invalidateEditorPreview();
    this._clearEditorDraftsForIdentity();
    this._renderEditorResults();
    const undoAll = this.root.querySelector("#editor-undo-all");
    (undoAll && !undoAll.disabled ? undoAll : this.root.querySelector("#editor-category-title"))?.focus();
  }

  async discardPendingNavigation() {
    this._clearEditorDraftsForIdentity();
    this._secretDrafts.clear();
    this.editor = null;
    this._publishState();
  }

  _fixPvpRequirements() {
    const previousKeys = this._filteredEditorFields().map((item) => item.key);
    const previousIndex = previousKeys.indexOf("bIsPvP");
    for (const key of ["bIsPvP", "bEnablePlayerToPlayerDamage", "bEnableDefenseOtherGuildPlayer"]) {
      const field = this._editorField(key);
      if (field && !field.sensitive) field.draftRaw = "True";
    }
    this._invalidateEditorPreview();
    this._renderEditorResults();
    const target = this.root.querySelector("#setting-bIsPvP");
    if (target) target.focus();
    else this._focusFilteredEditorFallback(previousIndex);
  }

  _replaceSettingCard(key) {
    const card = this.root?.querySelector(`[data-setting-card="${key}"]`);
    const field = this._editorField(key);
    if (!card || !field) return this._renderEditorResults();
    card.outerHTML = this._settingCardMarkup(field);
    this._syncEditorChrome();
    this._bindEditorResults();
  }

  _refreshEditorPresentation(key, { preserveSecretControl = false } = {}) {
    const field = this._editorField(key);
    const card = this.root?.querySelector(`[data-setting-card="${key}"]`);
    const remainsVisible = this._filteredEditorFields().some((candidate) => candidate.key === key);
    if (card && !remainsVisible) {
      this._renderEditorResults();
      return;
    }
    if (!field || !card) {
      this._syncEditorChrome();
      return;
    }
    const meta = this._settingMeta({ key: field.key, rawValue: field.draftRaw ?? "" });
    const changed = this._fieldChanged(field);
    const issue = this._editorIssues().get(field.key) || "";
    card.classList.toggle("changed", changed);
    card.classList.toggle("issue", Boolean(issue));
    const tags = card.querySelector("[data-setting-tags]");
    if (tags) tags.innerHTML = this._settingTagsMarkup(field, meta, changed, issue);
    const change = card.querySelector("[data-setting-change]");
    if (change) change.innerHTML = this._settingChangeMarkup(field, meta);
    const issueNode = card.querySelector("[data-setting-issue]");
    if (issueNode) { issueNode.hidden = !issue; issueNode.textContent = issue; }
    const control = card.querySelector("[data-setting-key], [data-secret-action], [data-secret-value]");
    if (control) {
      control.toggleAttribute("aria-invalid", Boolean(issue));
      control.setAttribute("aria-describedby", this._settingDescribedBy(field, meta, issue));
    }
    if (!preserveSecretControl && meta.type === "boolean") {
      const label = card.querySelector(".switch-control span");
      if (label) label.textContent = /^true$/i.test(field.draftRaw) ? this._t("common.enabled") : this._t("common.disabled");
    }
    const metaNode = card.querySelector("[data-setting-meta]");
    if (metaNode) metaNode.innerHTML = this._settingMetaMarkup(field, meta, changed);
    const pvpFix = card.querySelector("#fix-pvp-requirements");
    if (pvpFix) pvpFix.hidden = !(field.key === "bIsPvP" && issue);
    this._syncEditorChrome();
    this._bindEditorResults();
  }

  _syncEditorChrome() {
    const changed = this._editorOperations().length;
    const issues = this._editorIssues().size;
    const changedLabel = this.root?.querySelector("[data-scope-count=changed]");
    const issueLabel = this.root?.querySelector("[data-scope-count=issues]");
    if (changedLabel) changedLabel.textContent = changed ? ` (${changed})` : "";
    if (issueLabel) issueLabel.textContent = issues ? ` (${issues})` : "";
    const undoAll = this.root?.querySelector("#editor-undo-all");
    if (undoAll) undoAll.disabled = !changed;
    const operationStatus = this.root?.querySelector("#editor-operation-status");
    const operation = this.editor?.operation || { kind: "idle", text: "" };
    if (operationStatus) {
      operationStatus.hidden = !operation.text;
      operationStatus.className = `editor-operation ${operation.kind || "idle"}`;
      operationStatus.textContent = operation.text || "";
    }
    const reviewRegion = this.root?.querySelector("#editor-review-region");
    if (reviewRegion) reviewRegion.innerHTML = this._editorReviewMarkup();
    const footer = this.root?.querySelector(".editor-footer");
    if (footer) footer.outerHTML = this._editorFooterMarkup();
    this._bindEditorResults();
  }

  _renderEditorResults() {
    const workspace = this.root?.querySelector(".editor-workspace");
    const footer = this.root?.querySelector(".editor-footer");
    if (!workspace || !footer) return;
    const workspaceScrollTop = workspace.scrollTop;
    const contentScrollTop = this.root.querySelector(".editor-content")?.scrollTop || 0;
    workspace.outerHTML = this._editorWorkspaceMarkup();
    footer.outerHTML = this._editorFooterMarkup();
    const filterStatus = this.root.querySelector("#editor-filter-status");
    if (filterStatus) filterStatus.textContent = this._editorFilterStatus();
    this.root.querySelectorAll("[data-editor-scope]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.editorScope === this.editor.scope)));
    const categorySelect = this.root.querySelector("#editor-category-select");
    if (categorySelect) categorySelect.value = this.editor.category;
    this._syncEditorChrome();
    this._bindEditorResults();
    const nextWorkspace = this.root.querySelector(".editor-workspace");
    const content = this.root.querySelector(".editor-content");
    if (nextWorkspace) nextWorkspace.scrollTop = workspaceScrollTop;
    if (content) content.scrollTop = contentScrollTop;
  }

  _reportingCallout(reporting) {
    let title = "";
    let text = "";
    if (reporting.consent.status === "unresolved") {
      title = this._t("reporting.callout_unresolved");
      text = this._t("reporting.callout_unresolved_help");
    } else if (reporting.consent.status === "disabled") {
      title = this._t("reporting.callout_off");
      text = this._t("reporting.callout_off_help");
    } else if (reporting.consent.status === "unavailable") {
      title = this._t("reporting.callout_status");
      text = this._t("reporting.callout_status_help");
    } else if (reporting.verification.status === "unverified") {
      title = this._t("reporting.callout_unverified");
      text = reporting.verification.reason;
    } else if (reporting.verification.status === "unavailable") {
      title = this._t("reporting.callout_unavailable");
      text = reporting.verification.reason;
    } else return "";
    return `<section class="callout" aria-labelledby="reporting-callout-title"><h2 id="reporting-callout-title">${escapeHtml(title)}</h2><p>${escapeHtml(text)}</p><div class="row"><button id="review-player-reporting" type="button" class="secondary">${escapeHtml(this._t("reporting.review_connection"))}</button></div></section>`;
  }

  _overview() {
    const reporting = this.reporting();
    const playerValid = truth(this.snapshot, "player_data_valid");
    const players = playerValid === true
      ? this._t("overview.players", { current: state(this.snapshot, "player_count", "0"), maximum: state(this.snapshot, "player_max", "?") })
      : this._t("overview.player_unavailable");
    const online = state(this.snapshot, "online_players", "");
    const auto = truth(this.snapshot, "auto_shutdown");
    const blocked = this._reportingBlocked(reporting);
    return `<div class="stack">
      <section class="card" aria-labelledby="palworld-status-title"><h2 id="palworld-status-title">${escapeHtml(this._t("overview.server"))}</h2><p><strong>${escapeHtml(players)}</strong></p>${online && online !== "None" ? `<p class="muted">${escapeHtml(this._t("overview.online", { players: online }))}</p>` : ""}${this._reportingCallout(reporting)}</section>
      <div class="grid"><section class="card" aria-labelledby="access-title"><h2 id="access-title">${escapeHtml(this._t("overview.direct"))}</h2><div class="stat"><strong>${escapeHtml(this._t("overview.address"))}</strong><span>${escapeHtml(state(this.snapshot, "address", this._t("common.unavailable")))}</span></div><p class="muted">${escapeHtml(this._t("overview.address_help"))}</p><div class="row"><button id="copy-address" type="button" class="secondary">${escapeHtml(this._t("overview.copy"))}</button></div><p id="copy-address-result" class="muted" tabindex="-1" aria-live="off"></p></section>
      <section class="card" aria-labelledby="auto-summary-title"><div class="row"><h2 id="auto-summary-title">${escapeHtml(this._t("auto.title"))}</h2><span class="pill ${auto === true ? "on" : ""}">${escapeHtml(auto === true ? this._t("common.on") : auto === false ? this._t("common.off") : this._t("common.unavailable"))}</span></div><p><strong>${escapeHtml(blocked && auto === true ? blocked : state(this.snapshot, "auto_shutdown_status", this._t("common.unavailable")))}</strong></p><div class="row"><button type="button" class="secondary" data-route="auto-shutdown">${escapeHtml(this._t("overview.manage_auto"))}</button></div></section></div>
    </div>`;
  }

  _reportingBlocked(reporting) {
    if (reporting.consent.status === "unresolved") return this._t("reporting.blocked_decision");
    if (reporting.consent.status === "disabled") return this._t("reporting.blocked_off");
    if (reporting.consent.status === "unavailable") return this._t("reporting.blocked_status");
    if (["unverified", "unavailable"].includes(reporting.verification.status)) return this._t("reporting.blocked_unavailable");
    return "";
  }

  _readiness(reporting) {
    if (reporting.consent.status === "unresolved") return this._t("reporting.admin_required");
    if (reporting.consent.status === "disabled") return this._t("reporting.review_game");
    if (reporting.consent.status === "unavailable") return this._t("status.unavailable_reason", { reason: reporting.consent.reason });
    if (reporting.verification.status === "verified") return this._t("reporting.verified");
    if (reporting.verification.status === "not_testable") return this._t("reporting.resume");
    return this._t("reporting.blocked_reason", { reason: reporting.verification.reason });
  }

  _autoShutdown() {
    const reporting = this.reporting();
    const enabled = truth(this.snapshot, "auto_shutdown");
    const pending = truth(this.snapshot, "shutdown_pending");
    const blocked = this._operationBlocked();
    const reportingBlocked = this._reportingBlocked(reporting);
    const enableBlocked = enabled === false && Boolean(reportingBlocked);
    const providerRunning = ["started", "running"].includes(String(this.snapshot?.provider?.status || "").toLowerCase());
    const autoStatus = state(this.snapshot, "auto_shutdown_status", this._t("common.unavailable"));
    const countdownActive = autoStatus === "Starting countdown" || /^\d+(?:\.\d+)?\s+min remaining$/i.test(autoStatus);
    const countdown = enabled === true && providerRunning && !reportingBlocked && countdownActive
      ? state(this.snapshot, "idle_time_remaining", this._t("auto.not_counting"))
      : this._t("auto.not_counting");
    const currentActivity = reportingBlocked && enabled !== null
      ? enabled === true ? reportingBlocked : this._t("auto.enablement_required", { blocked: reportingBlocked })
      : autoStatus;
    return `<div class="stack">${blocked ? `<section class="callout" role="status"><h2>${escapeHtml(this._t("auto.operations_blocked"))}</h2><p>${escapeHtml(this._t("auto.operations_help"))}</p></section>` : ""}<section class="card" aria-labelledby="auto-title"><div class="row"><h2 id="auto-title">${escapeHtml(this._t("auto.title"))}</h2><span class="pill ${enabled === true ? "on" : ""}">${escapeHtml(enabled === true ? this._t("common.enabled") : enabled === false ? this._t("common.disabled") : this._t("common.unavailable"))}</span></div><div class="stat"><strong>${escapeHtml(this._t("auto.current"))}</strong><span>${escapeHtml(currentActivity)}</span></div><div class="stat"><strong>${escapeHtml(this._t("auto.reporting"))}</strong><span>${escapeHtml(this._readiness(reporting))}</span></div><div class="stat"><strong>${escapeHtml(this._t("auto.remaining"))}</strong><span>${escapeHtml(countdown)}</span></div><div class="row"><button id="auto-toggle" class="${enabled === true ? "secondary" : ""}" type="button" ${enabled === null || blocked || enableBlocked ? "disabled" : ""} aria-disabled="${Boolean(this.pending)}">${escapeHtml(enabled === true ? this._t("auto.disable") : this._t("auto.enable"))}</button>${enableBlocked ? `<button id="review-player-reporting" type="button" class="secondary">${escapeHtml(this._t("auto.review"))}</button>` : ""}${pending === true ? `<button id="cancel-shutdown" class="secondary" type="button" ${blocked ? "disabled" : ""} aria-disabled="${Boolean(this.pending)}">${escapeHtml(this._t("auto.cancel"))}</button>` : ""}</div></section>
      <div class="grid"><section class="card" aria-labelledby="idle-timing-title"><h2 id="idle-timing-title">${escapeHtml(this._t("auto.idle"))}</h2><label for="idle_shutdown_minutes">${escapeHtml(this._t("auto.idle_label"))}</label><div class="row"><input id="idle_shutdown_minutes" type="number" min="1" step="1" value="${escapeHtml(state(this.snapshot, "idle_shutdown_minutes", "20"))}" ${blocked || this.pending ? "disabled" : ""}><button type="button" data-number-action="idle_shutdown_minutes" ${blocked ? "disabled" : ""} aria-disabled="${Boolean(this.pending)}">${escapeHtml(this._t("common.save"))}</button></div></section><section class="card" aria-labelledby="cooldown-title"><h2 id="cooldown-title">${escapeHtml(this._t("auto.cooldown"))}</h2><label for="startup_cooldown_minutes">${escapeHtml(this._t("auto.cooldown_label"))}</label><div class="row"><input id="startup_cooldown_minutes" type="number" min="0" step="1" value="${escapeHtml(state(this.snapshot, "startup_cooldown_minutes", "10"))}" ${blocked || this.pending ? "disabled" : ""}><button type="button" data-number-action="startup_cooldown_minutes" ${blocked ? "disabled" : ""} aria-disabled="${Boolean(this.pending)}">${escapeHtml(this._t("common.save"))}</button></div></section></div>
      <section class="card" aria-labelledby="history-title"><h2 id="history-title">${escapeHtml(this._t("auto.history"))}</h2><div class="stat"><strong>${escapeHtml(this._t("auto.last_shutdown"))}</strong><span>${escapeHtml(state(this.snapshot, "last_shutdown_reason", this._t("auto.none")))}</span></div><div class="stat"><strong>${escapeHtml(this._t("auto.last_reset"))}</strong><span>${escapeHtml(state(this.snapshot, "last_reset_reason", this._t("auto.none")))}</span></div></section></div>`;
  }

  _editorMarkup() {
    if (!this.editor?.open) return "";
    const editor = this.editor;
    const operation = editor.operation || { kind: "idle", text: "" };
    return `<dialog id="palworld-settings-editor" class="editor-dialog" aria-labelledby="editor-title" aria-describedby="editor-description" aria-busy="${Boolean(this.pending)}"><div class="editor-body"><header class="editor-header"><div><h2 id="editor-title">${escapeHtml(this._t("editor.title"))}</h2><p id="editor-description" class="muted">${escapeHtml(this._t("editor.description", { schema: editor.schemaRevision || this._t("editor.loading_schema") }))}</p></div><button id="editor-close" type="button" class="secondary" ${this.pending ? "disabled" : ""} aria-label="${escapeHtml(this._t("editor.close_a11y"))}">${escapeHtml(this._t("editor.close"))}</button></header><div id="editor-operation-status" class="editor-operation ${escapeHtml(operation.kind || "idle")}" tabindex="-1" aria-live="polite" ${operation.text ? "" : "hidden"}>${escapeHtml(operation.text || "")}</div><div id="editor-stage" data-mode="${this._editorStageMode()}">${this._editorStageMarkup()}</div></div></dialog>`;
  }

  _editorStageMode() {
    if (this.editor?.loading) return "loading";
    if (this.editor?.error && !this.editor.fields.length) return "error";
    return "loaded";
  }

  _editorStageMarkup() {
    if (this.editor?.loading) return `<div class="editor-content"><p>${escapeHtml(this._t("editor.loading"))}</p></div>`;
    if (this.editor?.error && !this.editor.fields.length) return `<div class="editor-content"><div class="warning"><strong>${escapeHtml(this._t("editor.unavailable"))}</strong><p>${escapeHtml(this.editor.error)}</p><p><button id="editor-retry" type="button">${escapeHtml(this._t("common.retry"))}</button></p></div></div>`;
    return `${this._editorToolbarMarkup()}${this._editorWorkspaceMarkup()}${this._editorFooterMarkup()}`;
  }

  _syncEditorDialog() {
    const dialog = this.root?.querySelector("#palworld-settings-editor");
    if (!dialog || !this.editor?.open) return false;
    dialog.setAttribute("aria-busy", String(Boolean(this.pending)));
    const close = dialog.querySelector("#editor-close");
    if (close) close.disabled = Boolean(this.pending);
    const description = dialog.querySelector("#editor-description");
    if (description) description.textContent = this._t("editor.description", { schema: this.editor.schemaRevision || this._t("editor.loading_schema") });
    const status = dialog.querySelector("#editor-operation-status");
    const operation = this.editor.operation || { kind: "idle", text: "" };
    if (status) {
      status.hidden = !operation.text;
      status.className = `editor-operation ${operation.kind || "idle"}`;
      status.textContent = operation.text || "";
    }
    const stage = dialog.querySelector("#editor-stage");
    const mode = this._editorStageMode();
    if (stage?.dataset.mode !== mode) {
      stage.dataset.mode = mode;
      stage.innerHTML = this._editorStageMarkup();
      this._bindEditorShell();
    } else if (mode === "loaded") {
      this._renderEditorResults();
    }
    return true;
  }

  _editorToolbarMarkup() {
    const changed = this._editorOperations().length;
    const issues = this._editorIssues().size;
    const categories = CATEGORY_ORDER.map(([key]) => `<option value="${escapeHtml(key)}" ${this.editor.category === key ? "selected" : ""}>${escapeHtml(this._categoryLabel(key))}</option>`).join("");
    return `<section class="editor-toolbar" aria-label="${escapeHtml(this._t("editor.find"))}"><label class="editor-search" for="settings-search"><span class="muted">${escapeHtml(this._t("editor.search"))}</span><input id="settings-search" type="search" value="${escapeHtml(this.editor.query)}" placeholder="${escapeHtml(this._t("editor.search_placeholder"))}" autocomplete="off"></label><label class="editor-category-select" for="editor-category-select"><span class="muted">${escapeHtml(this._t("editor.category"))}</span><select id="editor-category-select">${categories}</select></label><div class="editor-filter" role="group" aria-label="${escapeHtml(this._t("editor.scope"))}"><button type="button" data-editor-scope="all" aria-pressed="${this.editor.scope === "all"}">${escapeHtml(this._t("editor.scope_all"))}</button><button type="button" data-editor-scope="changed" aria-pressed="${this.editor.scope === "changed"}">${escapeHtml(this._t("editor.scope_changed"))}<span data-scope-count="changed">${changed ? ` (${changed})` : ""}</span></button><button type="button" data-editor-scope="nondefault" aria-pressed="${this.editor.scope === "nondefault"}">${escapeHtml(this._t("editor.scope_nondefault"))}</button><button type="button" data-editor-scope="issues" aria-pressed="${this.editor.scope === "issues"}">${escapeHtml(this._t("editor.scope_issues"))}<span data-scope-count="issues">${issues ? ` (${issues})` : ""}</span></button></div><button id="editor-undo-all" type="button" class="secondary" ${changed ? "" : "disabled"}>${escapeHtml(this._t("editor.undo_all"))}</button><p id="editor-filter-status" class="sr-only" role="status" aria-live="polite" aria-atomic="true">${escapeHtml(this._editorFilterStatus())}</p></section>`;
  }

  _editorIssues() {
    const issues = new Map();
    for (const field of this.editor?.fields || []) {
      const meta = this._settingMeta({ key: field.key, rawValue: field.draftRaw ?? "" });
      if (field.sensitive && field.secretAction === "replace" && decodeQuoted(this._secretReplacementRaw(field.key)) === "") {
        issues.set(field.key, this._t("validation.secret"));
      } else if (meta.type === "number") {
        const value = Number(field.draftRaw);
        if (!Number.isFinite(value)) issues.set(field.key, this._t("validation.number"));
        else if (meta.min !== null && meta.min !== undefined && value < meta.min) issues.set(field.key, this._t("validation.minimum", { min: meta.min }));
        else if (meta.max !== null && meta.max !== undefined && value > meta.max) issues.set(field.key, this._t("validation.maximum", { max: meta.max }));
      }
    }
    const enabled = (key) => /^true$/i.test(this._editorField(key)?.draftRaw || "");
    if (enabled("bIsPvP") && (!enabled("bEnablePlayerToPlayerDamage") || !enabled("bEnableDefenseOtherGuildPlayer"))) {
      issues.set("bIsPvP", this._t("validation.pvp"));
    }
    if (enabled("bEnableFastTravelOnlyBaseCamp") && !enabled("bEnableFastTravel")) {
      issues.set("bEnableFastTravelOnlyBaseCamp", this._t("validation.fast_travel"));
    }
    return issues;
  }

  _isNonDefault(field) {
    const defaultRaw = DEFAULTS[field.key];
    if (defaultRaw === undefined || field.sensitive) return false;
    const current = String(field.draftRaw ?? "").trim();
    if (/^-?\d+(?:\.\d+)?$/.test(current) && /^-?\d+(?:\.\d+)?$/.test(defaultRaw)) return Number(current) !== Number(defaultRaw);
    if (/^(true|false)$/i.test(current)) return current.toLowerCase() !== defaultRaw.toLowerCase();
    if (STRING_KEYS.has(field.key)) return decodeQuoted(current) !== decodeQuoted(defaultRaw);
    return current !== defaultRaw;
  }

  _editorFiltersActive(query = this.editor?.query, scope = this.editor?.scope) {
    return Boolean(String(query || "").trim()) || scope !== "all";
  }

  _editorFilterStatus() {
    const categoryLabel = this._categoryLabel(this.editor.category);
    const count = this._filteredEditorFields().length;
    const scopeLabel = ({
      all: [this._t("editor.scope_one"), this._t("editor.scope_many")],
      changed: [this._t("editor.changed_one"), this._t("editor.changed_many")],
      nondefault: [this._t("editor.nondefault_one"), this._t("editor.nondefault_many")],
      issues: [this._t("editor.issue_one"), this._t("editor.issue_many")],
    }[this.editor.scope] || [this._t("editor.scope_one"), this._t("editor.scope_many")])[count === 1 ? 0 : 1];
    const query = this.editor.query.trim();
    return this._t("editor.filter_status", { category: categoryLabel, count, scope: scopeLabel, query: query ? this._t("editor.matching", { query }) : "" });
  }

  _filteredEditorFields(category = this.editor.category) {
    const query = this.editor.query.trim().toLowerCase();
    return this.editor.fields.filter((field) => {
      const meta = this._settingMeta({ key: field.key, rawValue: field.draftRaw ?? "" });
      if (category === "favorites" && !ESSENTIAL_KEYS.has(field.key)) return false;
      if (category !== "all" && category !== "favorites" && meta.category !== category) return false;
      if (this.editor.scope === "changed" && !this._fieldChanged(field)) return false;
      if (this.editor.scope === "nondefault" && !this._isNonDefault(field)) return false;
      if (this.editor.scope === "issues" && !this._editorIssues().has(field.key)) return false;
      if (!query) return true;
      const fieldCategoryLabel = this._categoryLabel(meta.category);
      return `${meta.label} ${meta.key} ${meta.description} ${fieldCategoryLabel}`.toLowerCase().includes(query);
    });
  }

  _editorWorkspaceMarkup() {
    const fields = this._filteredEditorFields();
    const counts = Object.fromEntries(CATEGORY_ORDER.map(([category]) => [category, this._filteredEditorFields(category).length]));
    const categoryLabel = this._categoryLabel(this.editor.category);
    const activeLabel = this.editor.query ? this._t("editor.search_results", { category: categoryLabel, query: this.editor.query.trim() }) : categoryLabel;
    const cards = fields.length ? fields.map((field) => this._settingCardMarkup(field)).join("") : `<div class="empty-state"><h3>${escapeHtml(this._t("editor.no_results"))}</h3><p class="muted">${escapeHtml(this._t("editor.no_results_help"))}</p><div class="row"><button id="editor-clear-filters" type="button" class="secondary">${escapeHtml(this._t("editor.clear_filters"))}</button></div></div>`;
    return `<div class="editor-workspace"><nav class="editor-rail" aria-label="${escapeHtml(this._t("editor.categories"))}">${CATEGORY_ORDER.map(([key]) => `<button id="editor-category-${key}" type="button" data-editor-category="${key}" aria-current="${this.editor.category === key}"><span>${escapeHtml(this._categoryLabel(key))}</span><span class="rail-count">${counts[key] || 0}</span></button>`).join("")}</nav><section class="editor-content" aria-labelledby="editor-category-title"><div class="editor-content-header"><div><h2 id="editor-category-title" tabindex="-1">${escapeHtml(activeLabel)}</h2><p class="muted">${escapeHtml(this._t("editor.count", { count: fields.length, suffix: fields.length === 1 ? "" : "s" }))}</p></div></div><div id="editor-review-region">${this._editorReviewMarkup()}</div><div class="settings-list">${cards}</div>${this._expertAndRecoveryMarkup()}</section></div>`;
  }

  _settingTagsMarkup(field, meta, changed = this._fieldChanged(field), issue = this._editorIssues().get(field.key) || "") {
    const nonDefault = this._isNonDefault(field);
    return [changed ? `<span class="tag changed">${escapeHtml(this._t("setting.changed"))}</span>` : "", nonDefault ? `<span class="tag">${escapeHtml(this._t("setting.nondefault"))}</span>` : "", meta.confidence === "shipped" ? `<span class="tag">${escapeHtml(this._t("setting.shipped"))}</span>` : meta.confidence === "provider" ? `<span class="tag">${escapeHtml(this._t("setting.provider"))}</span>` : "", meta.performance ? `<span class="tag risk">${escapeHtml(this._t("setting.performance"))}</span>` : "", meta.consequential ? `<span class="tag risk">${escapeHtml(this._t("setting.review"))}</span>` : "", issue ? `<span class="tag risk">${escapeHtml(this._t("setting.issue"))}</span>` : ""].join("");
  }

  _displaySettingValue(field, rawValue, proposed = false) {
    if (field.sensitive) {
      if (!proposed) return field.configured ? this._t("setting.existing_hidden") : this._t("setting.not_configured");
      return field.secretAction === "clear" ? this._t("setting.cleared") : this._t("setting.new_hidden");
    }
    const meta = this._settingMeta({ key: field.key, rawValue: rawValue ?? "" });
    if (meta.type === "string") return decodeQuoted(rawValue);
    if (meta.type === "boolean") return /^true$/i.test(String(rawValue)) ? this._t("common.enabled") : this._t("common.disabled");
    return String(rawValue ?? this._t("setting.empty")) || this._t("setting.empty");
  }

  _settingChangeMarkup(field, meta = this._settingMeta({ key: field.key, rawValue: field.draftRaw ?? "" })) {
    if (!this._fieldChanged(field)) return "";
    const saved = this._displaySettingValue(field, field.originalRaw, false);
    const proposed = this._displaySettingValue(field, field.draftRaw, true);
    const impact = meta.consequential ? this._t("setting.review") : meta.performance ? this._t("setting.impact_performance") : this._t("setting.impact_gameplay");
    return `<div class="setting-change">${escapeHtml(this._t("setting.saved_proposed", { saved, proposed, impact }))}</div>`;
  }

  _settingMetaMarkup(field, meta, changed = this._fieldChanged(field)) {
    return `${meta.unit ? `<span id="unit-${escapeHtml(field.key)}">${escapeHtml(meta.unit)}</span>` : ""}${meta.defaultRaw !== undefined && !field.sensitive ? `<span>${escapeHtml(this._t("setting.default", { value: meta.defaultRaw || this._t("setting.empty") }))}</span>` : `<span>${escapeHtml(this._t("setting.no_default"))}</span>`}${changed ? `<button type="button" class="secondary setting-undo" data-undo-setting="${escapeHtml(field.key)}">${escapeHtml(this._t("setting.undo"))}</button>` : ""}${meta.defaultRaw !== undefined && !field.sensitive && field.draftRaw !== meta.defaultRaw ? `<button type="button" class="secondary setting-undo" data-default-setting="${escapeHtml(field.key)}">${escapeHtml(this._t("setting.use_default"))}</button>` : ""}`;
  }

  _settingCardMarkup(field) {
    const meta = this._settingMeta({ key: field.key, rawValue: field.draftRaw ?? "" });
    const changed = this._fieldChanged(field);
    const issue = this._editorIssues().get(field.key) || "";
    return `<article data-setting-card="${escapeHtml(field.key)}" class="setting-card ${changed ? "changed" : ""} ${issue ? "issue" : ""}" aria-labelledby="label-${escapeHtml(field.key)}"><div><div class="setting-title"><h3 id="label-${escapeHtml(field.key)}">${escapeHtml(meta.label)}</h3><span data-setting-tags>${this._settingTagsMarkup(field, meta, changed, issue)}</span></div><p id="help-${escapeHtml(field.key)}">${escapeHtml(meta.description)}</p><div class="setting-key">${escapeHtml(field.key)}</div><div id="change-${escapeHtml(field.key)}" data-setting-change>${this._settingChangeMarkup(field, meta)}</div></div><div class="setting-control">${this._settingControlMarkup(field, meta, issue)}<p id="issue-${escapeHtml(field.key)}" data-setting-issue class="danger-text" role="alert" ${issue ? "" : "hidden"}>${escapeHtml(issue)}</p>${field.key === "bIsPvP" ? `<button id="fix-pvp-requirements" type="button" class="secondary" ${issue ? "" : "hidden"}>${escapeHtml(this._t("setting.fix_pvp"))}</button>` : ""}<div class="setting-meta" data-setting-meta>${this._settingMetaMarkup(field, meta, changed)}</div></div></article>`;
  }

  _settingDescribedBy(field, meta, issue = "") {
    return [`help-${field.key}`, meta.unit ? `unit-${field.key}` : "", `change-${field.key}`, issue ? `issue-${field.key}` : ""].filter(Boolean).join(" ");
  }

  _settingControlMarkup(field, meta, issue = "") {
    const disabled = this.pending ? "disabled" : "";
    const invalid = issue ? `aria-invalid="true"` : "";
    const describedBy = escapeHtml(this._settingDescribedBy(field, meta, issue));
    const labelledBy = `label-${escapeHtml(field.key)}`;
    const id = `setting-${field.key}`;
    if (field.sensitive) {
      const action = field.secretAction;
      return `<label id="qualifier-${escapeHtml(field.key)}-action" for="${id}-action" class="muted">${escapeHtml(this._t("setting.stored", { state: field.configured ? this._t("setting.configured") : this._t("setting.empty_state") }))}</label><select id="${id}-action" data-secret-action="${escapeHtml(field.key)}" aria-labelledby="${labelledBy} qualifier-${escapeHtml(field.key)}-action" aria-describedby="${describedBy}" ${disabled}><option value="keep" ${action === "keep" ? "selected" : ""}>${escapeHtml(this._t("setting.keep"))}</option><option value="replace" ${action === "replace" ? "selected" : ""}>${escapeHtml(this._t("setting.replace"))}</option><option value="clear" ${action === "clear" ? "selected" : ""}>${escapeHtml(this._t("setting.clear"))}</option></select>${action === "replace" ? `<label id="qualifier-${escapeHtml(field.key)}-value" for="${id}" class="muted">${escapeHtml(this._t("setting.new_value"))}</label><input id="${id}" data-secret-value="${escapeHtml(field.key)}" type="password" autocomplete="new-password" aria-labelledby="${labelledBy} qualifier-${escapeHtml(field.key)}-value" aria-describedby="${describedBy}" ${invalid} ${disabled}>` : ""}`;
    }
    if (meta.type === "boolean") return `<label class="switch-control" for="${id}"><span id="qualifier-${escapeHtml(field.key)}">${escapeHtml(/^true$/i.test(field.draftRaw) ? this._t("common.enabled") : this._t("common.disabled"))}</span><input id="${id}" data-setting-key="${escapeHtml(field.key)}" type="checkbox" aria-labelledby="${labelledBy} qualifier-${escapeHtml(field.key)}" aria-describedby="${describedBy}" ${/^true$/i.test(field.draftRaw) ? "checked" : ""} ${invalid} ${disabled}></label>`;
    if (meta.type === "enum") {
      const options = [...ENUMS[field.key]];
      if (!options.some(([value]) => value === field.draftRaw)) options.unshift([field.draftRaw, this._t("setting.unrecognized", { value: field.draftRaw })]);
      return `<label id="qualifier-${escapeHtml(field.key)}" class="muted" for="${id}">${escapeHtml(this._t("common.value"))}</label><select id="${id}" data-setting-key="${escapeHtml(field.key)}" aria-labelledby="${labelledBy} qualifier-${escapeHtml(field.key)}" aria-describedby="${describedBy}" ${invalid} ${disabled}>${options.map(([value, label]) => { const key = `enum.${field.key}.${value}`; const translated = this._t(key); return `<option value="${escapeHtml(value)}" ${value === field.draftRaw ? "selected" : ""}>${escapeHtml(translated === key ? label : translated)}</option>`; }).join("")}</select>`;
    }
    if (meta.type === "number") return `<label id="qualifier-${escapeHtml(field.key)}" class="muted" for="${id}">${escapeHtml(this._t("common.value"))}${meta.unit ? ` (${escapeHtml(meta.unit)})` : ""}</label><input id="${id}" data-setting-key="${escapeHtml(field.key)}" type="number" value="${escapeHtml(field.draftRaw)}" aria-labelledby="${labelledBy} qualifier-${escapeHtml(field.key)}" aria-describedby="${describedBy}" ${meta.min !== null && meta.min !== undefined ? `min="${meta.min}"` : ""} ${meta.max !== null && meta.max !== undefined ? `max="${meta.max}"` : ""} step="${meta.step || "any"}" ${invalid} ${disabled}>`;
    const value = meta.type === "string" ? decodeQuoted(field.draftRaw) : field.draftRaw;
    return `<label id="qualifier-${escapeHtml(field.key)}" class="muted" for="${id}">${escapeHtml(this._t("common.value"))}</label><input id="${id}" data-setting-key="${escapeHtml(field.key)}" type="text" value="${escapeHtml(value)}" spellcheck="false" aria-labelledby="${labelledBy} qualifier-${escapeHtml(field.key)}" aria-describedby="${describedBy}" ${invalid} ${disabled}>`;
  }

  _editorReviewMarkup() {
    const preview = this.editor.preview;
    if (!preview && !this.editor.error) return "";
    const verdict = preview?.verdict;
    const valid = verdict?.state === "supported";
    const previewText = preview?.redacted_changes_hidden ? this._t("review.sensitive") : preview?.redacted_diff || "";
    const summaries = this.editor.fields.filter((field) => this._fieldChanged(field)).map((field) => {
      const meta = this._settingMeta({ key: field.key, rawValue: field.draftRaw ?? "" });
      const category = this._categoryLabel(meta.category);
      const impact = meta.consequential ? this._t("review.consequential") : meta.performance ? this._t("review.performance") : this._t("review.gameplay");
      return `<li class="review-item"><strong>${escapeHtml(meta.label)}</strong> <span class="tag">${escapeHtml(category)}</span><p><code>${escapeHtml(field.key)}</code></p><p><strong>${escapeHtml(this._displaySettingValue(field, field.originalRaw, false))}</strong> → <strong>${escapeHtml(this._displaySettingValue(field, field.draftRaw, true))}</strong></p><p class="muted">${escapeHtml(impact)}</p></li>`;
    }).join("");
    return `<section id="editor-preview-status" class="${valid ? "good" : "warning"}" tabindex="-1" aria-live="off"><h3>${escapeHtml(preview ? valid ? this._t("review.ready") : this._t("review.blocked") : this._t("review.failed"))}</h3>${verdict ? `<p><strong>${escapeHtml(valid ? this._t("review.valid") : this._t("review.invalid"))}</strong>${verdict.reason ? ` — ${escapeHtml(verdict.reason)}` : ""} · ${escapeHtml(preview.changed ? this._t("review.changed") : this._t("review.unchanged"))}${preview.requires_restart && preview.changed ? ` · ${escapeHtml(this._t("review.restart"))}` : ""}</p>` : ""}${this.editor.error ? `<p class="danger-text">${escapeHtml(this.editor.error)}</p>` : ""}${summaries ? `<h4>${escapeHtml(this._t("review.proposed"))}</h4><ul class="review-list">${summaries}</ul>` : ""}${previewText ? `<details><summary>${escapeHtml(this._t("review.diff"))}</summary><pre>${escapeHtml(previewText)}</pre></details>` : ""}</section>`;
  }

  _expertAndRecoveryMarkup() {
    const history = this.editor.history.length ? this.editor.history.map((item) => {
      const when = Number.isFinite(item.created_at) ? new Date(item.created_at * 1000).toLocaleString(this.context?.ui?.locale || undefined) : this._t("common.unknown_time");
      return `<div class="history-item"><strong>${escapeHtml(item.backup_kind === "pre_rollback" ? this._t("recovery.before_rollback") : this._t("recovery.before_apply"))}</strong><p class="muted">${escapeHtml(when)} · ${escapeHtml(this._t("recovery.audit", { audit: item.operation_id || this._t("recovery.audit_unavailable") }))}</p>${item.available ? `<button type="button" class="secondary" data-recovery-id="${escapeHtml(item.recovery_id)}" ${this._editorMutationsEnabled() && !this.pending ? "" : "disabled"}>${escapeHtml(this._t("recovery.restore"))}</button>` : `<p class="danger-text">${escapeHtml(this._t("recovery.unavailable", { reason: item.reason || this._t("recovery.unverified") }))}</p>`}</div>`;
    }).join("") : `<p class="muted">${escapeHtml(this._t("recovery.none"))}</p>`;
    return `<details class="raw-panel"><summary>${escapeHtml(this._t("recovery.title"))}</summary><p class="muted">${escapeHtml(this._t("recovery.help"))}</p><pre>${escapeHtml(this.editor.redactedSource || this._t("recovery.source_unavailable"))}</pre><h3>${escapeHtml(this._t("recovery.history"))}</h3>${this.editor.historyError ? `<p class="danger-text">${escapeHtml(this.editor.historyError)}</p>` : ""}<div class="history">${history}</div></details>`;
  }

  _editorFooterMarkup() {
    const changed = this._editorOperations().length;
    const writeBlocked = this._editorWritePrerequisite();
    const valid = this.editor.preview?.verdict?.state === "supported";
    const issues = this._editorIssues().size;
    const reviewReason = !changed ? this._t("footer.make_change") : issues ? this._t("footer.resolve_issues", { count: issues, suffix: issues === 1 ? "" : "s" }) : writeBlocked;
    const applyReason = !this._editorMutationsEnabled() ? this._t("footer.read_only") : writeBlocked || (!this.editor.previewToken || !valid ? this._t("footer.review_first") : this._t("footer.ready_apply"));
    return `<footer class="editor-footer"><div><div class="change-summary">${escapeHtml(changed ? this._t("footer.unsaved", { count: changed, suffix: changed === 1 ? "" : "s" }) : this._t("footer.clean"))}</div><div id="editor-review-reason" class="muted">${escapeHtml(reviewReason || this._t("footer.ready_validate"))}</div><div id="editor-apply-reason" class="muted">${escapeHtml(applyReason)}</div>${this.editor.recoveryWarning ? `<div class="notice">${escapeHtml(this.editor.recoveryWarning)}</div>` : ""}</div><div class="editor-actions"><button id="editor-preview" type="button" ${this.pending || reviewReason ? "disabled" : ""} aria-describedby="editor-review-reason">${escapeHtml(this._t("footer.review"))}</button><button id="editor-apply" type="button" ${this.pending || !this._editorMutationsEnabled() || writeBlocked || !this.editor.previewToken || !valid ? "disabled" : ""} aria-describedby="editor-apply-reason">${escapeHtml(this._t("footer.apply"))}</button></div></footer>`;
  }

  _saveGames() {
    const prerequisite = this._saveBundlePrerequisite();
    const preview = this.saveBundle.preview;
    const counts = preview?.counts || {};
    const changedFiles = [...(preview?.replaced || []), ...(preview?.added || [])];
    const fileMarkup = changedFiles.length
      ? `<ul class="bundle-files">${changedFiles.map((item) => `<li><code>${escapeHtml(item.path)}</code> · ${escapeHtml(this._formatBytes(item.size))}</li>`).join("")}</ul>`
      : `<p class="muted">${escapeHtml(this._t("save.no_changes"))}</p>`;
    const world = preview?.world_id || this._t("save.active_world");
    const worldSummary = preview?.portable_manifest ? this._t("save.world_manifest", { world }) : this._t("save.world_editor", { world });
    const review = preview ? `<section id="save-bundle-review" class="card bundle-review" tabindex="-1" aria-labelledby="save-review-title"><div class="row"><div><h2 id="save-review-title">${escapeHtml(this._t("save.review_title"))}</h2><p class="muted">${escapeHtml(worldSummary)}</p></div><span class="pill ${preview.changed ? "warning" : "on"}">${escapeHtml(preview.changed ? this._t("save.changes") : this._t("save.identical"))}</span></div><div class="bundle-counts"><div class="stat"><strong>${escapeHtml(this._t("save.replace"))}</strong><span>${escapeHtml(counts.replaced || 0)}</span></div><div class="stat"><strong>${escapeHtml(this._t("save.add"))}</strong><span>${escapeHtml(counts.added || 0)}</span></div><div class="stat"><strong>${escapeHtml(this._t("save.preserve"))}</strong><span>${escapeHtml(counts.preserved || 0)}</span></div><div class="stat"><strong>${escapeHtml(this._t("save.delete"))}</strong><span>${escapeHtml(counts.deleted || 0)}</span></div></div><div class="stat"><strong>${escapeHtml(this._t("save.current"))}</strong><span>${escapeHtml(this._t("save.file_count", { count: preview.current?.files || 0, bytes: this._formatBytes(preview.current?.bytes) }))}</span></div><div class="stat"><strong>${escapeHtml(this._t("save.proposed"))}</strong><span>${escapeHtml(this._t("save.file_count", { count: preview.proposed?.files || 0, bytes: this._formatBytes(preview.proposed?.bytes) }))}</span></div><h3>${escapeHtml(this._t("save.files_change"))}</h3>${fileMarkup}<p class="good"><strong>${escapeHtml(this._t("save.no_deletions"))}</strong> ${escapeHtml(this._t("save.no_deletions_help"))}</p>${preview.truncated ? `<p class="warning">${escapeHtml(this._t("save.truncated"))}</p>` : ""}${this.saveBundle.error ? `<p class="danger-text">${escapeHtml(this.saveBundle.error)}</p>` : ""}<div class="row"><button id="apply-save-bundle" class="danger" type="button" ${!preview.changed || !this.saveBundle.previewToken || !this.saveBundle.restoreEnabled || prerequisite || this.pending ? "disabled" : ""}>${escapeHtml(this._t("save.restore_confirm"))}</button></div>${!this.saveBundle.restoreEnabled ? `<p class="muted">${escapeHtml(this._t("save.read_only"))}</p>` : ""}</section>` : "";
    const progress = `<div id="save-bundle-progress" class="bundle-progress" role="group" aria-label="${escapeHtml(this._t("save.progress_a11y"))}" aria-live="off" hidden><strong id="save-bundle-progress-message">${escapeHtml(this._t("progress.prepare"))}</strong><progress id="save-bundle-progress-bar" aria-label="${escapeHtml(this._t("save.progress_a11y"))}"></progress><span id="save-bundle-progress-detail" class="bundle-progress-detail muted">${escapeHtml(this._t("common.processing"))}</span></div>`;
    const readiness = prerequisite
      ? `<div class="warning"><h3>${escapeHtml(this._t("save.stop_first"))}</h3><p>${escapeHtml(this._t("save.stop_help", { prerequisite }))}</p></div>`
      : `<div class="success-note"><h3>${escapeHtml(this._t("save.ready_title"))}</h3><p>${escapeHtml(this._t("save.ready_help"))}</p></div>`;
    return `<div class="stack"><section id="save-bundle-card" class="card" aria-labelledby="save-title" aria-busy="false"><h2 id="save-title">${escapeHtml(this._t("save.title"))}</h2><p>${escapeHtml(this._t("save.intro"))}</p><details><summary>${escapeHtml(this._t("save.safety"))}</summary><p>${escapeHtml(this._t("save.safety_help"))}</p></details>${readiness}<div class="stat"><strong>${escapeHtml(this._t("save.current_readiness"))}</strong><span>${escapeHtml(prerequisite || this._t("game.ready_stopped"))}</span></div>${progress}<div id="save-bundle-announcement" class="sr-only" aria-live="polite" aria-atomic="true"></div><p id="save-bundle-operation-result" tabindex="-1" aria-live="off" data-kind="${escapeHtml(this.saveBundleResult.kind || "idle")}">${escapeHtml(this.saveBundleResult.text || "")}</p></section><div class="grid"><section class="card" aria-labelledby="download-save-title"><h2 id="download-save-title">${escapeHtml(this._t("save.download_title"))}</h2><p>${escapeHtml(this._t("save.download_help"))}</p><div class="row"><button id="download-save-bundle" type="button" ${prerequisite || this.pending ? "disabled" : ""}>${escapeHtml(this._t("save.download_button"))}</button></div></section><section class="card" aria-labelledby="upload-save-title"><h2 id="upload-save-title">${escapeHtml(this._t("save.upload_title"))}</h2><p>${escapeHtml(this._t("save.upload_help"))}</p><label class="file-picker" for="save-bundle-upload"><strong>${escapeHtml(this._t("save.upload_label"))}</strong><input id="save-bundle-upload" type="file" accept=".zip,application/zip" ${prerequisite || this.pending ? "disabled" : ""}></label><p class="muted">${escapeHtml(this._t("save.upload_max"))}</p>${this.saveBundle.error && !preview ? `<p class="danger-text">${escapeHtml(this.saveBundle.error)}</p>` : ""}</section></div>${review}</div>`;
  }

  _gameSettings() {
    const reporting = this.reporting();
    const status = reporting.consent.status;
    const endpoint = this.snapshot?.profile?.public_state?.player_reporting?.endpoint_label || this._t("game.endpoint");
    const confirmed = status === "enabled"
      ? reporting.verification.status === "verified" ? this._t("status.enabled_verified") : reporting.verification.status === "not_testable" ? this._t("status.enabled_resume") : this._t("status.enabled_unverified")
      : status === "disabled" ? this._t("common.disabled") : status === "unresolved" ? this._t("status.decision_required") : this._t("status.decision_unavailable");
    const choiceDisabled = this._operationBlocked() || status === "unavailable";
    const editorReadBlocked = this._operationBlocked() ? this._t("game.operation_blocked") : "";
    const editorWriteBlocked = this._editorWritePrerequisite();
    const riskDetails = `<details><summary>${escapeHtml(this._t("security.why"))}</summary><p>${escapeHtml(this._t("security.risk", { endpoint }))}</p></details>`;
    const connectionNotice = status === "enabled"
      ? `<div class="security-note"><h3>${escapeHtml(this._t("security.approved"))}</h3><p>${escapeHtml(this._t("security.approved_help"))}</p>${riskDetails}</div>`
      : status === "disabled"
        ? `<div class="notice"><h3>${escapeHtml(this._t("security.off"))}</h3><p>${escapeHtml(this._t("security.off_help"))}</p>${riskDetails}</div>`
        : status === "unavailable"
          ? `<div class="error-notice" role="alert"><h3>${escapeHtml(this._t("security.unavailable"))}</h3><p>${escapeHtml(this._t("security.unavailable_help"))}</p></div>`
          : `<div class="notice"><h3>${escapeHtml(this._t("security.decision"))}</h3><p>${escapeHtml(this._t("security.decision_help"))}</p>${riskDetails}</div>`;
    return `<div class="stack"><section id="player-reporting-connection" class="card" aria-labelledby="player-reporting-title"><h2 id="player-reporting-title">${escapeHtml(this._t("reporting.title"))}</h2>${connectionNotice}<div class="stat"><strong>${escapeHtml(this._t("game.confirmed"))}</strong><span>${escapeHtml(confirmed)}</span></div><fieldset class="choice" ${choiceDisabled ? "disabled" : ""} aria-disabled="${Boolean(this.pending)}"><legend>${escapeHtml(this._t("game.choice"))}</legend><label><input id="player-reporting-disabled" type="radio" name="player-reporting-choice" value="disabled" aria-disabled="${Boolean(this.pending)}" ${status === "disabled" ? "checked" : ""}> ${escapeHtml(this._t("game.keep_off"))}</label><label><input id="player-reporting-enabled" type="radio" name="player-reporting-choice" value="enabled" aria-disabled="${Boolean(this.pending)}" ${status === "enabled" ? "checked" : ""}> ${escapeHtml(this._t("game.allow_plaintext"))}</label></fieldset></section><section class="card" aria-labelledby="server-settings-title"><h2 id="server-settings-title">${escapeHtml(this._t("game.settings"))}</h2><p>${escapeHtml(this._t("game.settings_help"))}</p><div class="stat"><strong>${escapeHtml(this._t("game.inspect"))}</strong><span>${editorReadBlocked ? escapeHtml(editorReadBlocked) : escapeHtml(this._t("game.ready_any"))}</span></div><div class="stat"><strong>${escapeHtml(this._t("game.preview_apply"))}</strong><span>${editorWriteBlocked ? escapeHtml(editorWriteBlocked) : escapeHtml(this._t("game.ready_stopped"))}</span></div><div class="stat"><strong>${escapeHtml(this._t("game.after"))}</strong><span>${escapeHtml(this._t("game.restart"))}</span></div><div class="row"><button id="open-settings-editor" type="button" ${editorReadBlocked || this.pending ? "disabled" : ""}>${escapeHtml(this._t("game.open"))}</button></div>${!this._editorMutationsEnabled() ? `<p class="muted">${escapeHtml(this._t("game.read_only"))}</p>` : ""}</section>${this._editorMarkup()}</div>`;
  }

  _operationBlocked() {
    const operations = this.snapshot?.operations;
    return operations?.blocked === true || operations?.available === false;
  }
}

export const createCockpit = () => new PalworldCockpit();
