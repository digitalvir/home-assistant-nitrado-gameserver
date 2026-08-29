"""Constants for the Nitrado Game Server integration."""

DOMAIN = "nitrado_gameserver"
PROFILE_REGISTRY_TRANSITION_DATA_KEY = f"{DOMAIN}_profile_registry_api"

# Configuration key name, not a credential.
CONF_API_TOKEN = "api_token"  # nosec B105
CONF_ACCOUNT_ID = "account_id"
CONF_ACCOUNT_UUID = "account_uuid"
CONF_DISCOVERY_MODE = "discovery_mode"
CONF_DISCOVERY_INTERVAL = "discovery_interval"
CONF_STATUS_INTERVAL = "status_interval"
CONF_MISSING_SERVICE_THRESHOLD = "missing_service_threshold"
CONF_SETTLE_SECONDS = "settle_seconds"
CONF_IMPORTED_SERVICE_IDS = "imported_service_ids"
CONF_IGNORED_SERVICE_IDS = "ignored_service_ids"
CONF_SERVICE_DISPLAY_NAMES = "service_display_names"
CONF_SERVICE_AREA_IDS = "service_area_ids"
CONF_IDLE_SHUTDOWN_SERVICE_IDS = "idle_shutdown_service_ids"
CONF_MAINTENANCE_SERVICE_IDS = "maintenance_service_ids"
CONF_DRY_RUN_SERVICE_IDS = "dry_run_service_ids"
CONF_IDLE_MINUTES = "idle_minutes"
CONF_STARTUP_COOLDOWN_MINUTES = "startup_cooldown_minutes"
CONF_PROFILE_OPTIONS = "profile_options"
CONF_PROFILE_OPTION_ACKNOWLEDGEMENTS = "profile_option_acknowledgements"
CONF_ALLOW_PLAINTEXT_FTP_SERVICE_IDS = "allow_plaintext_ftp_service_ids"

DISCOVERY_AUTO_ADD = "auto_add"
DISCOVERY_ASK = "ask"
DISCOVERY_MANUAL = "manual"
DISCOVERY_MODES = {DISCOVERY_AUTO_ADD, DISCOVERY_ASK, DISCOVERY_MANUAL}

DEFAULT_DISCOVERY_MODE = DISCOVERY_ASK
DEFAULT_DISCOVERY_INTERVAL = 600
DEFAULT_STATUS_INTERVAL = 60
DEFAULT_MISSING_SERVICE_THRESHOLD = 3
DEFAULT_SETTLE_SECONDS = 600
DEFAULT_API_FAILURE_THRESHOLD = 5
DEFAULT_IDLE_MINUTES = 15
DEFAULT_STARTUP_COOLDOWN_MINUTES = 10
DEFAULT_FINAL_SHUTDOWN_CHECK_DELAY_SECONDS = 15

# Release A capability gates. These are deliberately code-owned and default
# off; they are not user options. UI hiding is never the authorization
# boundary for provider mutations that have not completed controlled
# acceptance.
EDITOR_MUTATIONS_ENABLED = False
FILESYSTEM_TREE_REPLACE_ENABLED = False
NATIVE_BACKUP_RESTORE_ENABLED = False

RUNNING_STATUS = "started"
STOPPED_STATUS = "stopped"
BLOCKED_STATUSES = {"suspended", "guardian_locked"}
TRANSITION_STATUSES = {
    "starting",
    "stopping",
    "restarting",
    "gs_installation",
    "backup_restore",
    "backup_creation",
    "chunkfix",
    "updating",
    "update",
    "installing",
}
