const PANEL_ROUTE = "/nitrado-game-servers";
const PANEL_ELEMENT = "nitrado-game-server-panel-2026-8-29-20145";
const COCKPIT_API_VERSION = 1;
const GUARD_TIMEOUT_MS = 1800;
const LIFECYCLE_TIMEOUT_MS = 5000;
const BOOTSTRAP_TIMEOUT_MS = 10000;
const CAPABILITY_TIMEOUT_MS = 30000;
const SAVE_BUNDLE_TIMEOUT_MS = 5 * 60 * 1000;
const SAVE_BUNDLE_JOB_TIMEOUT_MS = 30 * 60 * 1000;
const SAVE_BUNDLE_JOB_POLL_MS = 500;
const MAX_SAVE_BUNDLE_UPLOAD_BYTES = 256 * 1024 * 1024;
const HANDOFF_SIGN_TIMEOUT_MS = 10000;
const HANDOFF_SIGN_SECONDS = 60;
const HANDOFF_PATH_PATTERN = /^\/api\/nitrado_gameserver\/cockpit-handoff\/[A-Za-z0-9_-]{32,}$/;

const escapeHtml = (value) => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

const randomEpoch = () => globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
const withTimeout = async (promise, milliseconds, timeoutMessage) => {
  let timer;
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(String(timeoutMessage))), milliseconds); }),
    ]);
  } finally {
    clearTimeout(timer);
  }
};
const clone = (value) => globalThis.structuredClone ? structuredClone(value) : JSON.parse(JSON.stringify(value));
const humanizeRoute = (value) => String(value || "server")
  .replaceAll("-", " ")
  .replace(/(^|\s)\S/g, (letter) => letter.toUpperCase());
const deepFreeze = (value) => {
  if (!value || typeof value !== "object" || Object.isFrozen(value)) return value;
  Object.freeze(value);
  Object.values(value).forEach(deepFreeze);
  return value;
};
const interpolate = (template, values = {}) => String(template ?? "").replace(/\{([A-Za-z0-9_]+)\}/g, (match, key) => (
  Object.prototype.hasOwnProperty.call(values, key) ? String(values[key] ?? "") : match
));
const translateCatalog = (catalog, locale, key, values = {}) => {
  const language = String(locale || "en");
  const baseLanguage = language.split("-")[0];
  const template = catalog?.[language]?.[key]
    ?? catalog?.[baseLanguage]?.[key]
    ?? catalog?.en?.[key]
    ?? key;
  return interpolate(template, values);
};
export const hostTranslations = Object.freeze({
  en: Object.freeze({
    "status.running": "Running", "status.stopped": "Stopped", "status.stopping": "Stopping", "status.starting": "Starting", "status.restarting": "Restarting", "status.unavailable": "Status unavailable",
    "provider.fresh": "Status up to date", "provider.stale": "Status may be out of date", "provider.blocked": "Provider actions are unavailable until operation recovery is resolved in Nitrado tools.", "provider.refresh_first": "Refresh the server before using state-changing actions.",
    "provider.summary": "{game} · {profile} · {status} · {freshness}", "provider.unknown_game": "Unknown game", "provider.generic_profile": "Generic Nitrado", "provider.server": "Nitrado server",
    "action.start": "Start", "action.stop": "Stop", "action.refresh": "Refresh", "action.open_nitrado": "Open Nitrado", "action.opens_tab": "opens a new tab",
    "action.starting": "Starting…", "action.stopping": "Stopping…", "action.refreshing": "Refreshing…", "action.opening": "Opening Nitrado…", "action.working": "Working…",
    "result.start": "Start request accepted.", "result.stop": "Stop request accepted.", "result.refresh": "Server status refreshed.", "result.open": "Secure Nitrado page opened.", "result.accepted": "Home Assistant accepted the request.", "result.failed": "The request failed safely.",
    "stop.title": "Stop {name}?", "stop.explanation": "Stopping the server interrupts the current game session.{playerContext}", "stop.players": " {players} player(s) may be disconnected.", "stop.confirm": "Stop server",
    "popup.blocked": "Allow pop-ups for Home Assistant to open Nitrado.",
    "fallback.asset": "This game cockpit is installed, but its frontend asset is missing or failed validation.", "fallback.api": "This game cockpit is installed, but it is incompatible with the current integration version.", "fallback.none": "No specialized game cockpit is installed for this profile.", "fallback.basic": "{message} Basic Nitrado management remains available above.", "fallback.unsupported": "Unsupported game", "fallback.service_id": "Service ID", "fallback.address": "Server address", "fallback.unavailable": "Unavailable", "fallback.open_tools": "Open Nitrado tools",
    "tools.title": "Nitrado tools", "tools.intro": "Provider identity, transport, and capability facts. Game settings belong to the installed game cockpit.", "tools.identity": "Service identity", "tools.account": "Account entry", "tools.service_id": "Service ID", "tools.profile": "Profile", "tools.filesystem": "Provider filesystem", "tools.relevant": "Relevant", "tools.yes": "Yes", "tools.no": "No", "tools.transport": "Observed transport", "tools.not_observed": "Not observed", "tools.mapping": "HTTP root mapping", "tools.proven": "Proven", "tools.not_proven": "Not proven", "tools.recovery": "Operation recovery", "tools.unknown": "Outcome unknown: {kind}", "tools.ack_help": "Acknowledging this only clears the safety block; it does not claim the operation succeeded or failed.", "tools.ack": "Acknowledge uncertain outcome", "tools.unavailable": "Durable operation state is unavailable. Mutations fail closed.", "tools.active": "A provider operation is active.", "tools.clear": "No unresolved provider operation.",
    "shell.skip": "Skip to server content", "shell.brand": "Nitrado Servers", "shell.subtitle": "Provider host · game-owned cockpits", "shell.game_cockpit": "Game cockpit", "shell.provider_tools": "Nitrado provider tools", "shell.back": "Back to game", "shell.selected": "Selected Nitrado service", "shell.choose": "Choose a server", "shell.generic": "Generic",
    "state.loading": "Loading server cockpit…", "state.loading_help": "Checking the selected service and installed game profile.", "state.empty": "No imported Nitrado servers", "state.empty_help": "Import a service from the integration options to manage it here.", "state.open_settings": "Open Nitrado integration settings", "state.disambiguation": "Choose the exact Nitrado account", "state.unavailable": "Server cockpit unavailable", "state.retry": "Retry", "state.game_unavailable": "Game cockpit unavailable", "state.basic": "Basic Nitrado management remains available.", "state.retry_game": "Retry game cockpit",
    "dialog.title": "Confirm this action", "dialog.explanation": "Review this action before continuing.", "dialog.cancel": "Cancel", "dialog.continue": "Continue",
    "navigation.finish_nitrado": "Finish the active Nitrado operation before leaving this server.", "navigation.finish_operation": "Finish the active operation before leaving this server.", "navigation.leave_title": "Leave this work?", "navigation.leave_help": "The game cockpit has unsaved changes. Cancel to keep working, or leave and discard them.", "navigation.leave_confirm": "Leave and discard",
    "ack.title": "Acknowledge uncertain outcome?", "ack.help": "This clears the safety block without claiming the operation succeeded or failed.", "ack.confirm": "Acknowledge outcome", "ack.done": "The uncertain outcome was acknowledged; no success or failure was inferred.", "ack.failed": "The acknowledgement failed safely.",
    "document.unavailable": "Nitrado Servers · Unavailable · Home Assistant", "document.section": "{name} · {section} · Home Assistant",
    "error.discovery": "Nitrado server information could not be loaded.", "error.account_changed": "The selected Nitrado account changed.", "error.registry_changed": "The game profile registry changed.", "error.duplicate_service": "This service ID appears in more than one Nitrado account. Choose the exact account and server.", "error.load": "The selected server cockpit could not be loaded.", "error.module": "The installed cockpit module is incompatible with this integration version.", "error.contract": "Cockpit controller contract is incomplete.", "error.cockpit_load": "The game cockpit failed to load.", "error.cockpit_response": "The game cockpit stopped responding.",
    "error.context": "The cockpit context is unavailable; reload the server.", "error.stale": "The cockpit context is stale; reload the selected server.", "error.profile_changed": "The selected game profile changed.", "error.invalid_bundle": "The save-bundle request is invalid.", "error.bundle_failed": "The save-bundle request failed safely.", "error.job_invalid": "Home Assistant did not start a valid save-bundle job.", "error.preparation_failed": "Save-bundle preparation failed safely.", "error.preparation_timeout": "Save-bundle preparation exceeded the 30-minute safety limit.", "error.zip_empty": "Choose a non-empty ZIP file.", "error.zip_large": "The ZIP exceeds the 256 MiB upload limit.", "error.handoff": "The secure Nitrado handoff could not be created.", "error.handoff_auth": "Home Assistant cannot authenticate the Nitrado handoff; reload this page.", "error.handoff_not_auth": "Home Assistant did not authenticate the Nitrado handoff.", "error.handoff_invalid": "Home Assistant returned an invalid Nitrado handoff.", "error.transfer": "Home Assistant did not authorize the save-bundle transfer.", "error.cockpit_status": "Cockpit status changed.", "error.cockpit_fatal": "The game cockpit reported a fatal error.", "error.timed_out": "{label} timed out",
    "timeout.discovery": "Server discovery timed out.", "timeout.bootstrap": "Cockpit bootstrap timed out.", "timeout.navigation": "Navigation check timed out.", "timeout.discard": "Discarding the browser draft timed out.", "timeout.module": "Cockpit module loading timed out.", "timeout.mount": "Cockpit mounting timed out.", "timeout.ready": "Cockpit readiness timed out.", "timeout.update": "Cockpit updating timed out.", "timeout.stale_dispose": "Stale cockpit disposal timed out.", "timeout.dispose": "Cockpit disposal timed out.", "timeout.refresh": "Cockpit refresh timed out.", "timeout.operation": "Cockpit operation timed out.", "timeout.download": "Save-game download timed out.", "timeout.prepared_download": "Prepared save-game download timed out.", "timeout.inspect": "Save-game inspection timed out.", "timeout.handoff_auth": "Nitrado handoff authentication timed out.",
  }),
  "en-XA": Object.freeze({
    "shell.brand": "[ Nitrado server management and provider controls ]", "state.loading": "[ Loading the selected server cockpit and profile… ]", "action.open_nitrado": "[ Open the Nitrado provider page ]",
  }),
});

const styles = `
  :host { display:block; min-height:100%; color:var(--primary-text-color); background:var(--primary-background-color); --nitrado-readable-accent:color-mix(in srgb,var(--primary-color,#006b8f) 55%,var(--primary-text-color,#212121)); --nitrado-readable-error:color-mix(in srgb,var(--error-color,#b3261e) 62%,var(--primary-text-color,#212121)); }
  * { box-sizing:border-box; }
  button, select { font:inherit; }
  button, select { min-height:44px; border:1px solid color-mix(in srgb,var(--primary-color) 45%,var(--divider-color)); border-radius:9px; padding:9px 13px; }
  button { background:var(--nitrado-readable-accent); color:var(--primary-background-color,#fff); font-weight:750; cursor:pointer; box-shadow:0 2px 4px rgba(0,0,0,.12); }
  button.secondary { background:var(--card-background-color); color:var(--primary-text-color); }
  button.danger { background:var(--nitrado-readable-error); border-color:var(--nitrado-readable-error); color:var(--primary-background-color,#fff); }
  button:disabled { opacity:.56; cursor:not-allowed; box-shadow:none; }
  button:focus-visible, select:focus-visible, a:focus-visible, [tabindex]:focus-visible { outline:3px solid var(--primary-color); outline-offset:3px; }
  a { color:var(--nitrado-readable-accent); }
  .skip { position:fixed; z-index:20; top:8px; left:8px; transform:translateY(-180%); padding:9px 12px; border-radius:8px; background:var(--card-background-color); }
  .skip:focus { transform:none; }
  header { position:sticky; top:0; z-index:5; padding:14px 18px; border-bottom:1px solid var(--divider-color); background:color-mix(in srgb,var(--card-background-color) 96%,transparent); backdrop-filter:blur(8px); }
  .header-row { display:flex; gap:14px; align-items:end; flex-wrap:wrap; max-width:1180px; margin:auto; }
  .brand { min-width:210px; margin-right:auto; }
  .brand strong { display:block; font-size:20px; }
  .brand span { display:block; margin-top:3px; color:var(--secondary-text-color); font-size:13px; }
  .picker { display:grid; gap:5px; min-width:min(320px,100%); }
  .picker label { font-size:12px; font-weight:800; }
  .picker select { width:100%; background:var(--card-background-color); color:var(--primary-text-color); }
  .shell { max-width:1180px; margin:auto; padding:18px; }
  .provider { display:grid; grid-template-columns:minmax(230px,1fr) auto; gap:15px; align-items:center; padding:16px; border:1px solid var(--divider-color); border-radius:13px; background:var(--card-background-color); box-shadow:0 2px 7px rgba(0,0,0,.08); }
  .provider h1 { margin:0; font-size:23px; overflow-wrap:anywhere; }
  .provider p { margin:5px 0 0; color:var(--secondary-text-color); }
  .provider-result { min-height:1.45em; outline:none; }
  .provider-reason { font-size:13px; }
  .actions { display:flex; gap:8px; align-items:center; justify-content:flex-end; flex-wrap:wrap; }
  .host-nav { display:flex; justify-content:flex-end; margin:12px 0 2px; }
  .mount { margin-top:16px; min-width:0; }
  .state { padding:22px; border:1px solid var(--divider-color); border-radius:13px; background:var(--card-background-color); }
  .state h1, .state h2 { margin:0 0 8px; }
  .state p { margin:7px 0 0; line-height:1.45; }
  .degraded { border-left:4px solid var(--warning-color,#f0a800); }
  .tools-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:15px; }
  .tools-grid section { padding:17px; border:1px solid var(--divider-color); border-radius:12px; background:var(--card-background-color); }
  .tools-grid h2 { margin:0 0 10px; }
  .stat { display:flex; justify-content:space-between; gap:12px; padding:9px 0; border-top:1px solid var(--divider-color); }
  .stat:first-of-type { border-top:0; }
  .stat span { text-align:right; overflow-wrap:anywhere; }
  dialog { width:min(470px,calc(100vw - 32px)); border:1px solid var(--divider-color); border-radius:13px; padding:0; background:var(--card-background-color); color:var(--primary-text-color); }
  dialog::backdrop { background:rgba(0,0,0,.56); }
  .dialog-body { padding:20px; }
  .dialog-body h2 { margin:0 0 9px; }
  .dialog-actions { display:flex; justify-content:flex-end; gap:9px; margin-top:18px; }
  .sr-live { position:absolute; width:1px; height:1px; overflow:hidden; clip:rect(0 0 0 0); white-space:nowrap; }
  @media (max-width:720px) { header { position:static; padding:13px 12px; } .shell { padding:12px; } .provider { grid-template-columns:1fr; } .actions { justify-content:flex-start; } .brand { width:100%; } .picker { width:100%; } .tools-grid { grid-template-columns:1fr; } }
  @media (max-width:360px) { .actions button { width:100%; } }
  @media (prefers-reduced-motion:reduce) { *,*::before,*::after { scroll-behavior:auto !important; transition:none !important; animation:none !important; } }
`;

class NitradoGameServerPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._narrow = null;
    this._services = [];
    this._selected = null;
    this._bootstrap = null;
    this._controller = null;
    this._mountEpoch = null;
    this._navigationState = null;
    this._navigationBusy = false;
    this._providerPending = false;
    this._providerOperationGeneration = 0;
    this._providerOperation = null;
    this._providerResult = "";
    this._status = "loading";
    this._error = "";
    this._route = "overview";
    this._routeSource = null;
    this._pendingFocus = null;
    this._unsubscribe = null;
    this._subscriptionPromise = null;
    this._subscriptionPromiseEpoch = null;
    this._subscriptionRetryTimer = null;
    this._moduleCache = new Map();
    this._hostAnnouncement = "";
    this._cockpitAnnouncement = "";
    this._connected = false;
    this._connectionEpoch = randomEpoch();
    this._epochSignal = 0;
    this._initializationPromise = null;
    this._controllerUpdatePromise = null;
    this._controllerUpdateQueued = false;
    this._navigationWaiters = [];
    this._dialogReturnFocus = null;
    this._dialogReturnScroll = null;
    this._dialogRequest = null;
    this._activeBinding = null;
    this._announcementServiceKey = null;
    this._stableScroll = { x: window.scrollX || 0, y: window.scrollY || 0 };
    this._navigationReturnScroll = null;
  }

  set hass(value) {
    const first = !this._hass;
    this._hass = value;
    if (first && this.isConnected) void this._initialize();
    else if (this._bootstrap) {
      this._updateFromHassStates();
      if (this._connected && !this._unsubscribe) void this._subscribeEpochs();
    }
  }

  get hass() { return this._hass; }

  _t(key, values = {}) {
    return translateCatalog(
      hostTranslations,
      String(this._hass?.locale?.language || this._hass?.language || "en"),
      key,
      values,
    );
  }

  _providerStatus(value) {
    const normalized = String(value || "").trim().toLowerCase();
    const key = ({ started: "running", running: "running", stopped: "stopped", stopping: "stopping", starting: "starting", restarting: "restarting", unavailable: "unavailable" })[normalized];
    return key ? this._t(`status.${key}`) : humanizeRoute(value || this._t("status.unavailable"));
  }

  set narrow(value) {
    const next = typeof value === "boolean" ? value : null;
    if (next === this._narrow) return;
    this._narrow = next;
    if (this._controller) void this._updateController();
  }

  get narrow() { return this._narrow; }

  connectedCallback() {
    this._connected = true;
    this._connectionEpoch = randomEpoch();
    window.addEventListener("popstate", this._popstate = () => void this._historyNavigation({ ...this._stableScroll }));
    window.addEventListener("scroll", this._scrollListener = () => {
      this._stableScroll = { x: window.scrollX || 0, y: window.scrollY || 0 };
    }, { passive: true });
    this._ensureShell();
    if (this._hass) void this._initialize();
  }

  disconnectedCallback() {
    this._connected = false;
    this._connectionEpoch = randomEpoch();
    this._mountEpoch = randomEpoch();
    this._providerOperationGeneration += 1;
    if (this._dialogResolve) this._finishDialog(false);
    window.removeEventListener("popstate", this._popstate);
    window.removeEventListener("scroll", this._scrollListener);
    this._unsubscribe?.();
    this._unsubscribe = null;
    this._subscriptionPromise = null;
    this._subscriptionPromiseEpoch = null;
    clearTimeout(this._subscriptionRetryTimer);
    this._subscriptionRetryTimer = null;
    void this._disposeController();
  }

  async _initialize() {
    if (!this._hass) return;
    if (this._initializationPromise) return this._initializationPromise;
    const connectionEpoch = this._connectionEpoch;
    const initialize = (async () => {
      this._status = "initializing";
      this._render();
      try {
        const result = await withTimeout(this._hass.callApi("GET", "nitrado_gameserver/extensions"), BOOTSTRAP_TIMEOUT_MS, this._t("timeout.discovery"));
        if (!this._connected || connectionEpoch !== this._connectionEpoch) return;
        this._services = Array.isArray(result?.services) ? result.services : [];
        this._resolveLocation();
        if (!this._selected) {
          this._status = this._services.length ? "disambiguation" : "empty";
          this._render();
          return;
        }
        await this._subscribeEpochs(connectionEpoch);
        if (!this._connected || connectionEpoch !== this._connectionEpoch) return;
        for (let attempt = 0; attempt < 2; attempt += 1) {
          const epochSignal = this._epochSignal;
          await this._loadBootstrap({ replaceHistory: true });
          if (!this._connected || connectionEpoch !== this._connectionEpoch || epochSignal === this._epochSignal) break;
        }
      } catch (_error) {
        if (!this._connected || connectionEpoch !== this._connectionEpoch) return;
        this._status = "error";
        this._error = this._t("error.discovery");
        this._render();
        this._setTitleAndFocus(this._t("document.unavailable"), "#error-title");
      }
    })();
    this._initializationPromise = initialize;
    try {
      await initialize;
    } finally {
      if (this._initializationPromise === initialize) this._initializationPromise = null;
      if (this._connected && connectionEpoch !== this._connectionEpoch) void this._initialize();
    }
  }

  async _subscribeEpochs(connectionEpoch = this._connectionEpoch) {
    if (this._unsubscribe) return true;
    if (!this._connected || !this._hass?.connection?.subscribeEvents) return false;
    if (this._subscriptionPromise && this._subscriptionPromiseEpoch === connectionEpoch) {
      return this._subscriptionPromise;
    }
    const attempt = (async () => {
      try {
        const unsubscribe = await this._hass.connection.subscribeEvents(
        (event) => {
          if (event?.data?.scope === "account") {
            if (String(event?.data?.account_entry_id || "") !== String(this._selected?.account_entry_id || "")) return;
            this._epochSignal += 1;
            if (this._bootstrap
              && Number(event?.data?.account_epoch) !== Number(this._bootstrap?.account_epoch)) {
              void this._mandatoryReload(this._t("error.account_changed"));
            }
            return;
          }
          this._epochSignal += 1;
          if (this._bootstrap && Number(event?.data?.epoch) !== Number(this._bootstrap?.registry_epoch)) {
            void this._mandatoryReload(this._t("error.registry_changed"));
          }
        },
        "nitrado_gameserver_cockpit_epoch",
        );
        if (!this._connected || connectionEpoch !== this._connectionEpoch) {
          unsubscribe?.();
          return false;
        }
        this._unsubscribe = unsubscribe;
        clearTimeout(this._subscriptionRetryTimer);
        this._subscriptionRetryTimer = null;
        return true;
      } catch (_error) {
        if (this._connected && connectionEpoch === this._connectionEpoch) {
          this._scheduleEpochSubscriptionRetry(connectionEpoch);
        }
        return false;
      }
    })();
    this._subscriptionPromise = attempt;
    this._subscriptionPromiseEpoch = connectionEpoch;
    try {
      return await attempt;
    } finally {
      if (this._subscriptionPromise === attempt) {
        this._subscriptionPromise = null;
        this._subscriptionPromiseEpoch = null;
      }
    }
  }

  _scheduleEpochSubscriptionRetry(connectionEpoch) {
    if (this._subscriptionRetryTimer || !this._connected || connectionEpoch !== this._connectionEpoch) return;
    this._subscriptionRetryTimer = setTimeout(() => {
      this._subscriptionRetryTimer = null;
      if (this._connected && connectionEpoch === this._connectionEpoch && !this._unsubscribe) {
        void this._subscribeEpochs(connectionEpoch);
      }
    }, 1000);
  }

  _pathSegments() {
    return window.location.pathname.split("/").filter(Boolean).map((part) => {
      try { return decodeURIComponent(part); } catch (_error) { return ""; }
    });
  }

  _resolveLocation() {
    const segments = this._pathSegments();
    const offset = segments[0] === PANEL_ROUTE.slice(1) ? 1 : 0;
    const first = segments[offset] || "";
    const second = segments[offset + 1] || "";
    const exact = this._services.find((item) => String(item.account_entry_id) === first && String(item.service_id) === second);
    if (exact) {
      this._selected = exact;
      this._route = segments[offset + 2] || "overview";
      const state = window.history.state;
      this._routeSource = state?.nitradoCockpit
        && String(state.accountEntryId) === String(exact.account_entry_id)
        && String(state.serviceId) === String(exact.service_id)
        ? { cockpitKey: state.cockpitKey, frontendRevision: state.frontendRevision }
        : { direct: true };
      return;
    }
    const legacy = this._services.filter((item) => String(item.service_id) === first);
    if (legacy.length === 1) {
      this._selected = legacy[0];
      this._route = second || "overview";
      this._routeSource = { legacy: true };
      return;
    }
    if (legacy.length > 1) {
      this._selected = null;
      this._error = this._t("error.duplicate_service");
      return;
    }
    this._selected = this._services[0] || null;
    this._route = "overview";
    this._routeSource = { direct: true };
  }

  _canonicalPath(route = this._route) {
    if (!this._selected) return PANEL_ROUTE;
    return `${PANEL_ROUTE}/${encodeURIComponent(this._selected.account_entry_id)}/${encodeURIComponent(this._selected.service_id)}/${encodeURIComponent(route)}`;
  }

  async _loadBootstrap({ replaceHistory = false } = {}) {
    if (!this._selected || !this._hass) return;
    const selectedKey = `${this._selected.account_entry_id}:${this._selected.service_id}`;
    if (selectedKey !== this._announcementServiceKey) {
      this._announcementServiceKey = selectedKey;
      this._hostAnnouncement = "";
      this._cockpitAnnouncement = "";
      this.shadowRoot.querySelector("#host-live")?.replaceChildren();
      this.shadowRoot.querySelector("#cockpit-live")?.replaceChildren();
    }
    this._status = "loading";
    this._mountEpoch = randomEpoch();
    this._navigationState = null;
    await this._disposeController();
    this._render();
    const path = `nitrado_gameserver/accounts/${encodeURIComponent(this._selected.account_entry_id)}/services/${encodeURIComponent(this._selected.service_id)}/cockpit/bootstrap?mount_epoch=${encodeURIComponent(this._mountEpoch)}`;
    try {
      const bootstrap = await withTimeout(this._hass.callApi("GET", path), BOOTSTRAP_TIMEOUT_MS, this._t("timeout.bootstrap"));
      if (!this._connected || selectedKey !== `${this._selected?.account_entry_id}:${this._selected?.service_id}` || bootstrap.mount_epoch !== this._mountEpoch) return;
      this._bootstrap = bootstrap;
      this._route = this._normalizeRoute(this._route, bootstrap.cockpit, this._routeSource);
      this._routeSource = bootstrap.cockpit
        ? { cockpitKey: bootstrap.cockpit.key, frontendRevision: bootstrap.cockpit.frontend_revision }
        : { cockpitKey: "generic", frontendRevision: "core" };
      this._status = "ready";
      if (replaceHistory) window.history.replaceState(this._historyState(), "", this._canonicalPath());
      else window.history.pushState(this._historyState(), "", this._canonicalPath());
      this._render();
      if (this._route !== "tools" && bootstrap.cockpit) await this._mountCockpit();
      if (this._status === "ready") this._focusAfterNavigation();
    } catch (error) {
      if (!this._connected
        || selectedKey !== `${this._selected?.account_entry_id}:${this._selected?.service_id}`
        || this._mountEpoch === null
        || !path.endsWith(`mount_epoch=${encodeURIComponent(this._mountEpoch)}`)) return;
      this._status = "error";
      this._error = error?.message || this._t("error.load");
      this._render();
      this._setTitleAndFocus(this._t("document.unavailable"), "#error-title");
    }
  }

  _normalizeRoute(route, cockpit, source = null) {
    if (route === "advanced" || route === "tools") return "tools";
    if (!cockpit) return "overview";
    if (source?.legacy) {
      const legacyAlias = cockpit.route_aliases?.find(([sourceProfile, sourceRoute]) => sourceProfile === cockpit.key && sourceRoute === route);
      return legacyAlias?.[2] || cockpit.default_route;
    }
    const sameCockpit = source?.cockpitKey === cockpit.key
      && source?.frontendRevision === cockpit.frontend_revision;
    if ((source?.direct || sameCockpit) && cockpit.route_keys.includes(route)) return route;
    const alias = cockpit.route_aliases?.find(([sourceProfile, sourceRoute]) => sourceProfile === source?.cockpitKey && sourceRoute === route);
    return alias?.[2] || cockpit.default_route;
  }

  _historyState() {
    return {
      nitradoCockpit: true,
      accountEntryId: this._selected?.account_entry_id,
      serviceId: this._selected?.service_id,
      cockpitKey: this._bootstrap?.cockpit?.key || "generic",
      frontendRevision: this._bootstrap?.cockpit?.frontend_revision || "core",
      route: this._route,
    };
  }

  async _historyNavigation(returnScroll = null) {
    const prior = { selected: this._selected, route: this._route, path: this._canonicalPath() };
    this._navigationReturnScroll = returnScroll;
    const allowed = await this._guardNavigation({ type: "history" });
    this._navigationReturnScroll = null;
    if (!allowed) {
      window.history.pushState(this._historyState(), "", prior.path);
      return;
    }
    this._resolveLocation();
    await this._loadBootstrap({ replaceHistory: true });
  }

  async _guardNavigation(intent) {
    if (this._navigationBusy) {
      await new Promise((resolve) => this._navigationWaiters.push(resolve));
    }
    if (this._providerPending) {
      this._announceHost(this._t("navigation.finish_nitrado"));
      return false;
    }
    this._navigationBusy = true;
    try {
      let result = null;
      if (this._controller?.navigationGuard) {
        try { result = await withTimeout(Promise.resolve(this._controller.navigationGuard(deepFreeze(clone(intent)))), GUARD_TIMEOUT_MS, this._t("timeout.navigation")); }
        catch (_error) { result = null; }
      }
      const decision = result?.decision;
      if (this._navigationState?.mutationPhase === "pending" || decision === "block") {
        this._announceHost(this._t("navigation.finish_operation"));
        return false;
      }
      if (decision === "allow" && this._navigationState?.mutationPhase !== "unknown") return true;
      const dirty = decision === "confirm"
        || this._navigationState?.dirty === true
        || this._navigationState?.mutationPhase === "unknown"
        || !this._navigationState;
      if (!dirty) return true;
      const accepted = await this._confirmDiscard();
      if (accepted && this._controller?.discardPendingNavigation) {
        try { await withTimeout(Promise.resolve(this._controller.discardPendingNavigation()), GUARD_TIMEOUT_MS, this._t("timeout.discard")); }
        catch (_error) { return false; }
      }
      return accepted;
    } finally {
      this._navigationBusy = false;
      this._navigationWaiters.shift()?.();
    }
  }

  _confirmDiscard() {
    return this._requestConfirmation({
      title: this._t("navigation.leave_title"),
      explanation: this._t("navigation.leave_help"),
      confirmLabel: this._t("navigation.leave_confirm"),
      destructive: true,
      returnScroll: this._navigationReturnScroll,
    });
  }

  _requestConfirmation(request = {}) {
    if (this._dialogResolve || !this._connected) return Promise.resolve(false);
    return new Promise((resolve) => {
      this._dialogReturnFocus = this.shadowRoot.activeElement;
      this._dialogReturnScroll = request.returnScroll || { x: window.scrollX || 0, y: window.scrollY || 0 };
      this._dialogRequest = {
        title: String(request.title || this._t("dialog.title")),
        explanation: String(request.explanation || this._t("dialog.explanation")),
        cancelLabel: String(request.cancelLabel || this._t("dialog.cancel")),
        confirmLabel: String(request.confirmLabel || this._t("dialog.continue")),
        destructive: request.destructive === true,
      };
      this._renderDialog = true;
      this._dialogResolve = resolve;
      this._mountDialog();
      const dialog = this.shadowRoot.querySelector("#host-confirmation-dialog");
      dialog?.showModal?.();
      this.shadowRoot.querySelector("#dialog-cancel")?.focus();
    });
  }

  _finishDialog(value) {
    const dialog = this.shadowRoot.querySelector("#host-confirmation-dialog");
    dialog?.close?.();
    dialog?.remove();
    this._renderDialog = false;
    const resolve = this._dialogResolve;
    this._dialogResolve = null;
    const returnFocus = this._dialogReturnFocus;
    const returnScroll = this._dialogReturnScroll;
    this._dialogReturnFocus = null;
    this._dialogReturnScroll = null;
    this._dialogRequest = null;
    if (!value) queueMicrotask(() => {
      if (returnFocus?.isConnected) returnFocus.focus();
      if (returnScroll && typeof window.scrollTo === "function") {
        window.scrollTo(returnScroll.x, returnScroll.y);
        this._stableScroll = returnScroll;
      }
    });
    resolve?.(value);
  }

  async _navigate(route, options = {}) {
    if (route === this._route) return;
    if (!await this._guardNavigation({ type: route === "tools" ? "tools" : "cockpit_route", route })) return;
    const priorRoute = this._route;
    const nextRoute = this._normalizeRoute(route, this._bootstrap?.cockpit, this._routeSource);
    const leavingCockpit = priorRoute !== "tools" && nextRoute === "tools";
    const enteringCockpit = priorRoute === "tools" && nextRoute !== "tools";
    if (leavingCockpit) {
      this._mountEpoch = randomEpoch();
      await this._disposeController();
    }
    this._route = nextRoute;
    this._routeSource = this._bootstrap?.cockpit
      ? { cockpitKey: this._bootstrap.cockpit.key, frontendRevision: this._bootstrap.cockpit.frontend_revision }
      : { cockpitKey: "generic", frontendRevision: "core" };
    this._pendingFocus = options.focus || null;
    window.history.pushState(this._historyState(), "", this._canonicalPath());
    if (leavingCockpit || enteringCockpit || !this._bootstrap?.cockpit) {
      this._render();
      if (enteringCockpit && this._bootstrap?.cockpit) await this._mountCockpit();
    } else if (this._route !== "tools" && this._bootstrap?.cockpit) {
      if (!this._controller) {
        this._render();
        await this._mountCockpit();
      } else {
        await this._updateController();
        this._retargetSkipLink();
      }
    }
    if (this._status === "ready") this._focusAfterNavigation();
  }

  async _selectService(value) {
    const [accountEntryId, serviceId] = value.split("|");
    const next = this._services.find((item) => String(item.account_entry_id) === accountEntryId && String(item.service_id) === serviceId);
    if (!next || next === this._selected) return;
    if (!await this._guardNavigation({ type: "service_switch", accountEntryId, serviceId })) {
      const picker = this.shadowRoot.querySelector("#service-picker");
      if (picker && this._selected) picker.value = `${this._selected.account_entry_id}|${this._selected.service_id}`;
      return;
    }
    this._routeSource = this._bootstrap?.cockpit
      ? { cockpitKey: this._bootstrap.cockpit.key, frontendRevision: this._bootstrap.cockpit.frontend_revision }
      : { cockpitKey: "generic", frontendRevision: "core" };
    this._selected = next;
    this._providerOperationGeneration += 1;
    this._providerPending = false;
    this._providerOperation = null;
    this._providerResult = "";
    await this._loadBootstrap();
  }

  async _mandatoryReload(reason) {
    this._announceHost(reason);
    if (this._dialogResolve) this._finishDialog(false);
    this._mountEpoch = randomEpoch();
    this._providerOperationGeneration += 1;
    await this._disposeController();
    this._bootstrap = null;
    await this._initialize();
  }

  async _module() {
    const descriptor = this._bootstrap?.cockpit;
    if (!descriptor) return null;
    const key = `${descriptor.asset_url}|${descriptor.frontend_revision}`;
    if (!this._moduleCache.has(key)) this._moduleCache.set(key, import(descriptor.asset_url));
    const module = await withTimeout(this._moduleCache.get(key), LIFECYCLE_TIMEOUT_MS, this._t("timeout.module"));
    const metadata = module?.cockpitMetadata;
    if (metadata?.cockpitApiVersion !== COCKPIT_API_VERSION || metadata?.frontendRevision !== descriptor.frontend_revision || metadata?.key !== descriptor.key || typeof module.createCockpit !== "function") {
      throw new Error(this._t("error.module"));
    }
    return module;
  }

  _context(controller = this._controller) {
    const epoch = this._mountEpoch;
    const descriptor = this._bootstrap.cockpit;
    const binding = {
      epoch,
      accountEntryId: String(this._selected.account_entry_id),
      serviceId: String(this._selected.service_id),
      lease: this._bootstrap.lease,
    };
    this._activeBinding = binding;
    const guard = () => {
      if (epoch !== this._mountEpoch
        || binding.accountEntryId !== String(this._selected?.account_entry_id)
        || binding.serviceId !== String(this._selected?.service_id)) {
        throw new Error(this._t("error.stale"));
      }
    };
    return deepFreeze({
      identity: clone(this._bootstrap.snapshot.identity),
      snapshot: this._snapshotForController(),
      navigation: {
        currentRoute: this._route,
        navigate: (route, options) => { guard(); return this._navigate(route, options); },
        openNitradoTools: () => { guard(); return this._navigate("tools"); },
      },
      operations: {
        entityAction: (entityKey, action, value) => { guard(); return this._capability("entity-action", { entity_key: entityKey, action, value }, binding); },
        refreshSnapshot: async () => { guard(); await this._capability("refresh", {}, binding); guard(); await this._refreshBootstrapSnapshot(); },
      },
      extensions: {
        updateProfileOption: (optionKey, value, options = {}) => { guard(); return this._capability("profile-option", { option_key: optionKey, value, confirm: options.confirmed === true }, binding); },
        readEditableFile: (fileKey) => { guard(); return this._capability("editable-read", { file_key: fileKey }, binding); },
        previewEditableFile: (fileKey, proposal) => {
          guard();
          const body = proposal && Array.isArray(proposal.operations)
            ? { file_key: fileKey, operations: proposal.operations, source_revision: proposal.sourceRevision }
            : { file_key: fileKey, value: proposal };
          return this._capability("editable-preview", body, binding);
        },
        applyEditableFile: (fileKey, proposal, previewToken) => {
          guard();
          const body = proposal && Array.isArray(proposal.operations)
            ? { file_key: fileKey, operations: proposal.operations, source_revision: proposal.sourceRevision }
            : { file_key: fileKey, value: proposal };
          return this._capability("editable-apply", { ...body, preview_token: previewToken, confirm: true }, binding);
        },
        editableFileHistory: (fileKey) => { guard(); return this._capability("editable-history", { file_key: fileKey }, binding); },
        rollbackEditableFile: (fileKey, recoveryId) => { guard(); return this._capability("editable-rollback", { file_key: fileKey, recovery_id: recoveryId, confirm: true }, binding); },
        downloadSaveBundle: (bundleKey, onProgress) => { guard(); return this._downloadSaveBundle(bundleKey, binding, onProgress); },
        inspectSaveBundle: (bundleKey, file, onProgress) => { guard(); return this._inspectSaveBundle(bundleKey, file, binding, onProgress); },
        applySaveBundle: (bundleKey, previewToken) => {
          guard();
          return this._capability("save-bundle-apply", { bundle_key: bundleKey, preview_token: previewToken, confirm: true }, binding);
        },
      },
      host: {
        confirm: (request) => { guard(); return this._requestConfirmation(request); },
        publishNavigationState: (state) => {
          guard();
          const clean = state && typeof state.dirty === "boolean" && ["idle", "pending", "unknown"].includes(state.mutationPhase);
          this._navigationState = clean ? deepFreeze(clone(state)) : { dirty: true, mutationPhase: "unknown", messageCode: "invalid_state" };
        },
        announce: (_code, metadata = {}) => { guard(); this._announceCockpit(String(metadata.message || this._t("error.cockpit_status"))); },
        reportFatal: () => {
          guard();
          void this._degradeCockpit(
            this._t("error.cockpit_fatal"),
            { epoch, bootstrap: this._bootstrap, controller, accountEntryId: binding.accountEntryId, serviceId: binding.serviceId },
          );
        },
      },
      ui: {
        locale: String(this._hass?.locale?.language || this._hass?.language || "en"),
        narrow: this._narrow,
        interpolate,
        translate: (catalog, key, values = {}) => translateCatalog(
          catalog,
          String(this._hass?.locale?.language || this._hass?.language || "en"),
          key,
          values,
        ),
      },
      metadata: deepFreeze(clone(descriptor)),
    });
  }

  _snapshotForController() {
    const snapshot = clone(this._bootstrap.snapshot);
    snapshot.navigation = { current_route: this._route };
    snapshot.ui = {
      locale: String(this._hass?.locale?.language || this._hass?.language || "en"),
      narrow: this._narrow,
    };
    return deepFreeze(snapshot);
  }

  async _mountCockpit() {
    const root = this.shadowRoot.querySelector("#cockpit-mount");
    if (!root || !this._bootstrap?.cockpit) return;
    const ownership = {
      epoch: this._mountEpoch,
      bootstrap: this._bootstrap,
      controller: null,
      accountEntryId: String(this._selected?.account_entry_id),
      serviceId: String(this._selected?.service_id),
      root,
    };
    let controller = null;
    try {
      const module = await this._module();
      if (!module || !this._ownsCockpit(ownership)) return;
      controller = module.createCockpit();
      if (!controller || typeof controller.mount !== "function" || typeof controller.update !== "function" || typeof controller.dispose !== "function") throw new Error(this._t("error.contract"));
      ownership.controller = controller;
      if (!this._ownsCockpit(ownership, { requireController: false })) {
        await this._disposeStaleController(controller);
        return;
      }
      this._controller = controller;
      await withTimeout(Promise.resolve(controller.mount(root, this._context(controller))), LIFECYCLE_TIMEOUT_MS, this._t("timeout.mount"));
      if (!this._ownsCockpit(ownership)) {
        await this._disposeStaleController(controller);
        return;
      }
      if (typeof controller.ready === "function") await withTimeout(Promise.resolve(controller.ready()), LIFECYCLE_TIMEOUT_MS, this._t("timeout.ready"));
      if (!this._ownsCockpit(ownership)) {
        await this._disposeStaleController(controller);
        return;
      }
      this._retargetSkipLink();
    } catch (error) {
      if (controller && this._controller !== controller) await this._disposeStaleController(controller);
      await this._degradeCockpit(error?.message || this._t("error.cockpit_load"), ownership);
    }
  }

  async _updateController() {
    if (this._controllerUpdatePromise) {
      this._controllerUpdateQueued = true;
      return this._controllerUpdatePromise;
    }
    this._controllerUpdatePromise = (async () => {
      do {
        this._controllerUpdateQueued = false;
        const controller = this._controller;
        if (!controller) return;
        const ownership = {
          epoch: this._mountEpoch,
          bootstrap: this._bootstrap,
          controller,
          accountEntryId: String(this._selected?.account_entry_id),
          serviceId: String(this._selected?.service_id),
        };
        try {
          await withTimeout(Promise.resolve(controller.update(this._snapshotForController())), LIFECYCLE_TIMEOUT_MS, this._t("timeout.update"));
        } catch (_error) {
          await this._degradeCockpit(this._t("error.cockpit_response"), ownership);
        }
      } while (this._controllerUpdateQueued);
    })();
    try { await this._controllerUpdatePromise; } finally { this._controllerUpdatePromise = null; }
  }

  _ownsCockpit(ownership, { requireController = true } = {}) {
    return this._connected
      && ownership?.epoch === this._mountEpoch
      && ownership?.bootstrap === this._bootstrap
      && ownership?.accountEntryId === String(this._selected?.account_entry_id)
      && ownership?.serviceId === String(this._selected?.service_id)
      && (!ownership.root || ownership.root === this.shadowRoot.querySelector("#cockpit-mount"))
      && (!requireController || ownership.controller === this._controller);
  }

  async _disposeStaleController(controller) {
    if (!controller?.dispose || controller === this._controller) return;
    try { await withTimeout(Promise.resolve(controller.dispose()), LIFECYCLE_TIMEOUT_MS, this._t("timeout.stale_dispose")); } catch (_error) {}
  }

  async _disposeController() {
    const controller = this._controller;
    this._controller = null;
    this._activeBinding = null;
    if (!controller?.dispose) return;
    try { await withTimeout(Promise.resolve(controller.dispose()), LIFECYCLE_TIMEOUT_MS, this._t("timeout.dispose")); } catch (_error) {}
  }

  async _degradeCockpit(message, ownership = null) {
    if (ownership && !this._ownsCockpit(ownership, { requireController: Boolean(ownership.controller) })) return;
    if (this._dialogResolve) this._finishDialog(false);
    const descriptor = this._bootstrap?.cockpit;
    if (descriptor) this._moduleCache.delete(`${descriptor.asset_url}|${descriptor.frontend_revision}`);
    this._mountEpoch = randomEpoch();
    await this._disposeController();
    this._status = "degraded";
    this._error = message;
    this._render();
    const name = this._bootstrap?.snapshot?.identity?.service_name || this._t("provider.server");
    this._setTitleAndFocus(`${name} · Game cockpit unavailable · Home Assistant`, "#degraded-title");
  }

  async _refreshBootstrapSnapshot() {
    if (!this._selected || !this._bootstrap) return;
    const selectedKey = `${this._selected.account_entry_id}:${this._selected.service_id}`;
    const epoch = this._mountEpoch;
    const path = `nitrado_gameserver/accounts/${encodeURIComponent(this._selected.account_entry_id)}/services/${encodeURIComponent(this._selected.service_id)}/cockpit/bootstrap?mount_epoch=${encodeURIComponent(this._mountEpoch)}`;
    const next = await withTimeout(this._hass.callApi("GET", path), BOOTSTRAP_TIMEOUT_MS, this._t("timeout.refresh"));
    if (!this._connected || epoch !== this._mountEpoch || selectedKey !== `${this._selected?.account_entry_id}:${this._selected?.service_id}`) return;
    this._bootstrap = next;
    if (this._activeBinding?.epoch === epoch) this._activeBinding.lease = next.lease;
    this._updateProviderDom();
    await this._updateController();
  }

  _updateFromHassStates() {
    if (!this._bootstrap?.snapshot?.entities) return;
    for (const item of Object.values(this._bootstrap.snapshot.entities)) {
      const current = this._hass.states?.[item.entity_id];
      if (current) item.state = current.state;
    }
    this._updateProviderDom();
    void this._updateController();
  }

  _updateProviderDom() {
    const current = this.shadowRoot.querySelector(".provider");
    if (!current || !this._bootstrap) return;
    const view = this._providerView();
    current.setAttribute("aria-busy", String(this._providerPending));
    const title = current.querySelector("#service-title");
    if (title) title.textContent = view.name;
    const state = current.querySelector("#provider-state");
    if (state) state.textContent = view.summary;
    const reason = current.querySelector("#provider-action-reason");
    if (reason) {
      reason.textContent = view.reason;
      reason.hidden = !view.reason;
    }
    const result = current.querySelector("#provider-operation-result");
    if (result) result.textContent = this._providerResult;
    const actions = current.querySelector("#provider-actions");
    if (!actions) return;
    if (this._providerPending) {
      actions.querySelectorAll("button").forEach((button) => { button.disabled = true; });
      const active = actions.querySelector(`#${this._providerOperation?.controlId || ""}`);
      if (active) active.textContent = this._providerOperation?.pendingLabel || active.textContent;
      return;
    }
    actions.innerHTML = this._providerActionsMarkup(view);
    this._bindProviderActions();
  }

  async _capability(capability, payload, binding = null) {
    const accountEntryId = binding?.accountEntryId ?? this._selected?.account_entry_id;
    const serviceId = binding?.serviceId ?? this._selected?.service_id;
    const lease = binding?.lease ?? this._bootstrap?.lease;
    if (!lease || !accountEntryId || !serviceId) throw new Error(this._t("error.context"));
    if (binding && binding.epoch !== this._mountEpoch) throw new Error(this._t("error.stale"));
    const path = `nitrado_gameserver/accounts/${encodeURIComponent(accountEntryId)}/services/${encodeURIComponent(serviceId)}/cockpit/capabilities/${capability}`;
    try {
      return await withTimeout(this._hass.callApi("POST", path, { lease, ...payload }), CAPABILITY_TIMEOUT_MS, this._t("timeout.operation"));
    } catch (error) {
      if (String(error?.message || "").includes("stale")) void this._mandatoryReload(this._t("error.profile_changed"));
      throw new Error(error?.message || this._t("result.failed"));
    }
  }

  _saveBundlePath(bundleKey, action, binding) {
    if (!/^[a-z][a-z0-9_]*$/.test(String(bundleKey)) || !["download", "inspect"].includes(action)) {
      throw new Error(this._t("error.invalid_bundle"));
    }
    return `/api/nitrado_gameserver/accounts/${encodeURIComponent(binding.accountEntryId)}/services/${encodeURIComponent(binding.serviceId)}/cockpit/save-bundles/${encodeURIComponent(bundleKey)}/${action}`;
  }

  async _saveBundleFetch(url, options, label) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), SAVE_BUNDLE_TIMEOUT_MS);
    try {
      const response = await fetch(url, { ...options, signal: controller.signal, credentials: "same-origin" });
      if (!response.ok) {
        let message = this._t("error.bundle_failed");
        try {
          const payload = await response.json();
          if (typeof payload?.message === "string") message = payload.message;
        } catch (_error) {}
        throw new Error(message);
      }
      return response;
    } catch (error) {
      if (error?.name === "AbortError") throw new Error(this._t("error.timed_out", { label }));
      throw error;
    } finally {
      clearTimeout(timer);
    }
  }

  async _pollSaveBundleJob(jobId, bundleKey, action, binding, onProgress = null) {
    if (typeof jobId !== "string" || !/^[A-Za-z0-9_-]{24,}$/.test(jobId)) {
      throw new Error(this._t("error.job_invalid"));
    }
    const deadline = Date.now() + SAVE_BUNDLE_JOB_TIMEOUT_MS;
    while (Date.now() < deadline) {
      const result = await this._capability(
        "save-bundle-job-status",
        { job_id: jobId, bundle_key: bundleKey, action },
        binding,
      );
      if (typeof onProgress === "function" && result?.progress && typeof result.progress === "object") {
        onProgress(result.progress);
      }
      if (result?.status === "ready") return result;
      if (result?.status === "error") throw new Error(result?.message || this._t("error.preparation_failed"));
      await new Promise((resolve) => setTimeout(resolve, SAVE_BUNDLE_JOB_POLL_MS));
    }
    throw new Error(this._t("error.preparation_timeout"));
  }

  async _downloadSaveBundle(bundleKey, binding, onProgress = null) {
    const transferToken = await this._requestSaveBundleTransfer(bundleKey, "download", binding);
    const started = await this._saveBundleFetch(
      this._saveBundlePath(bundleKey, "download", binding),
      {
        method: "POST",
        headers: { "X-Nitrado-Save-Transfer": transferToken },
      },
      this._t("timeout.download"),
    );
    const job = await started.json();
    const ready = await this._pollSaveBundleJob(job?.job_id, bundleKey, "download", binding, onProgress);
    const response = await this._saveBundleFetch(
      this._saveBundlePath(bundleKey, "download", binding),
      {
        method: "POST",
        headers: {
          "X-Nitrado-Save-Transfer": ready.transfer_token,
          "X-Nitrado-Save-Job": job.job_id,
        },
      },
      this._t("timeout.prepared_download"),
    );
    const disposition = response.headers.get("Content-Disposition") || "";
    const match = disposition.match(/filename="([A-Za-z0-9._-]+)"/);
    const filename = match ? match[1] : "game-save.zip";
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    try {
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = filename;
      anchor.rel = "noopener";
      anchor.hidden = true;
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
    } finally {
      setTimeout(() => URL.revokeObjectURL(url), 0);
    }
    return { filename, bytes: blob.size };
  }

  async _inspectSaveBundle(bundleKey, file, binding, onProgress = null) {
    if (!(file instanceof Blob) || file.size < 1) throw new Error(this._t("error.zip_empty"));
    if (file.size > MAX_SAVE_BUNDLE_UPLOAD_BYTES) throw new Error(this._t("error.zip_large"));
    const transferToken = await this._requestSaveBundleTransfer(bundleKey, "inspect", binding);
    const response = await this._saveBundleFetch(
      this._saveBundlePath(bundleKey, "inspect", binding),
      {
        method: "POST",
        headers: {
          "Content-Type": "application/zip",
          "X-Nitrado-Save-Transfer": transferToken,
        },
        body: file,
      },
      this._t("timeout.inspect"),
    );
    const job = await response.json();
    return await this._pollSaveBundleJob(job?.job_id, bundleKey, "inspect", binding, onProgress);
  }

  async _signedHandoffPath(path) {
    if (typeof path !== "string" || !HANDOFF_PATH_PATTERN.test(path)) {
      throw new Error(this._t("error.handoff"));
    }
    if (typeof this._hass?.callWS !== "function") {
      throw new Error(this._t("error.handoff_auth"));
    }
    const result = await withTimeout(this._hass.callWS({
      type: "auth/sign_path",
      path,
      expires: HANDOFF_SIGN_SECONDS,
    }), HANDOFF_SIGN_TIMEOUT_MS, this._t("timeout.handoff_auth"));
    const signedPath = result?.path;
    if (typeof signedPath !== "string") {
      throw new Error(this._t("error.handoff_not_auth"));
    }
    const signedUrl = new URL(signedPath, window.location.origin);
    if (signedUrl.origin !== window.location.origin
      || signedUrl.pathname !== path
      || !signedUrl.searchParams.has("authSig")
      || signedUrl.hash) {
      throw new Error(this._t("error.handoff_invalid"));
    }
    return `${signedUrl.pathname}${signedUrl.search}`;
  }

  async _requestSaveBundleTransfer(bundleKey, action, binding) {
    const result = await this._capability(
      "save-bundle-transfer",
      { bundle_key: bundleKey, action },
      binding,
    );
    const token = result?.transfer_token;
    if (typeof token !== "string" || !/^[A-Za-z0-9_-]{32,}$/.test(token)) {
      throw new Error(this._t("error.transfer"));
    }
    return token;
  }

  async _providerAction(action) {
    if (this._providerPending) return;
    const selectedKey = `${this._selected?.account_entry_id}:${this._selected?.service_id}`;
    const invokingControl = this.shadowRoot.activeElement;
    const controlId = `host-${action === "open-nitrado" ? "open" : action}`;
    if (action === "stop") {
      const name = this._providerView().name;
      const players = this._entityState("player_count");
      const playerContext = Number.isFinite(Number(players)) && Number(players) > 0
        ? this._t("stop.players", { players })
        : "";
      const accepted = await this._requestConfirmation({
        title: this._t("stop.title", { name }),
        explanation: this._t("stop.explanation", { playerContext }),
        confirmLabel: this._t("stop.confirm"),
        destructive: true,
      });
      if (!accepted
        || selectedKey !== `${this._selected?.account_entry_id}:${this._selected?.service_id}`
        || this._providerPending
        || this._operationBlocked()
        || !this._providerView().showStop) return;
    }
    let popup = null;
    if (action === "open-nitrado") {
      popup = window.open("about:blank", "_blank");
      if (!popup) {
        this._announceHost(this._t("popup.blocked"));
        return;
      }
      try { popup.opener = null; } catch (_error) {}
    }
    const operationGeneration = ++this._providerOperationGeneration;
    const ownsOperation = () => this._connected
      && operationGeneration === this._providerOperationGeneration
      && selectedKey === `${this._selected?.account_entry_id}:${this._selected?.service_id}`;
    const labels = {
      start: [this._t("action.starting"), this._t("result.start")],
      stop: [this._t("action.stopping"), this._t("result.stop")],
      refresh: [this._t("action.refreshing"), this._t("result.refresh")],
      "open-nitrado": [this._t("action.opening"), this._t("result.open")],
    };
    this._providerPending = true;
    this._providerOperation = { generation: operationGeneration, selectedKey, controlId, pendingLabel: labels[action]?.[0] || this._t("action.working") };
    this._providerResult = labels[action]?.[0] || this._t("action.working");
    this._updateProviderDom();
    try {
      const result = await this._capability(action, {});
      if (!ownsOperation()) return;
      if (popup && result?.path) popup.location.replace(await this._signedHandoffPath(result.path));
      else if (popup) throw new Error(this._t("error.handoff"));
      if (!ownsOperation()) return;
      if (["start", "stop", "refresh"].includes(action)) await this._refreshBootstrapSnapshot();
      if (!ownsOperation()) return;
      this._providerResult = labels[action]?.[1] || this._t("result.accepted");
      this._announceHost(this._providerResult);
    } catch (error) {
      popup?.close?.();
      if (!ownsOperation()) return;
      this._providerResult = error?.message || this._t("result.failed");
      this._announceHost(this._providerResult);
    } finally {
      if (!ownsOperation()) return;
      this._providerPending = false;
      this._providerOperation = null;
      this._updateProviderDom();
      queueMicrotask(() => {
        const currentControl = this.shadowRoot.querySelector(`#${controlId}`);
        if (currentControl && invokingControl?.id === controlId) currentControl.focus();
        else this.shadowRoot.querySelector("#provider-operation-result")?.focus();
      });
    }
  }

  _entityState(key) { return this._bootstrap?.snapshot?.entities?.[key]?.state; }
  _entityAvailable(key) { const value = String(this._entityState(key) ?? "").toLowerCase(); return !["", "unavailable"].includes(value); }
  _operationBlocked() { const operations = this._bootstrap?.snapshot?.operations; return operations?.blocked === true || operations?.available === false; }

  _providerView() {
    const snapshot = this._bootstrap?.snapshot;
    const identity = snapshot?.identity || {};
    const provider = snapshot?.provider || {};
    const rawStatus = String(provider.status || "unavailable").trim().toLowerCase();
    const running = ["started", "running"].includes(rawStatus);
    const stopped = ["stopped", "offline"].includes(rawStatus);
    const blocked = this._operationBlocked();
    const freshness = provider.status_fresh ? this._t("provider.fresh") : this._t("provider.stale");
    const reason = blocked
      ? this._t("provider.blocked")
      : !provider.status_fresh
        ? this._t("provider.refresh_first")
        : "";
    return {
      name: String(identity.service_name || this._selected?.name || this._t("provider.server")),
      summary: this._t("provider.summary", { game: identity.game_name || this._t("provider.unknown_game"), profile: identity.profile_name || this._t("provider.generic_profile"), status: this._providerStatus(rawStatus), freshness }),
      reason,
      blocked,
      showStart: stopped || (!running && this._entityAvailable("start")),
      showStop: running || (!stopped && this._entityAvailable("stop")),
      startDisabled: blocked || !provider.status_fresh || !this._entityAvailable("start"),
      stopDisabled: blocked || !provider.status_fresh || !this._entityAvailable("stop"),
      refreshDisabled: !this._entityAvailable("refresh"),
    };
  }

  _providerActionsMarkup(view = this._providerView()) {
    const describedBy = view.reason ? ' aria-describedby="provider-action-reason"' : "";
    return `${view.showStart ? `<button id="host-start" type="button" ${view.startDisabled ? "disabled" : ""}${describedBy}>${escapeHtml(this._t("action.start"))}</button>` : ""}${view.showStop ? `<button id="host-stop" class="danger" type="button" ${view.stopDisabled ? "disabled" : ""}${describedBy}>${escapeHtml(this._t("action.stop"))}</button>` : ""}<button id="host-refresh" class="secondary" type="button" ${view.refreshDisabled ? "disabled" : ""}>${escapeHtml(this._t("action.refresh"))}</button><button id="host-open" class="secondary" type="button">${escapeHtml(this._t("action.open_nitrado"))} <span class="sr-live">${escapeHtml(this._t("action.opens_tab"))}</span></button>`;
  }

  _providerMarkup() {
    const view = this._providerView();
    return `<section class="provider" aria-labelledby="service-title" aria-busy="${String(this._providerPending)}"><div><h1 id="service-title" tabindex="-1">${escapeHtml(view.name)}</h1><p id="provider-state">${escapeHtml(view.summary)}</p><p id="provider-action-reason" class="provider-reason" ${view.reason ? "" : "hidden"}>${escapeHtml(view.reason)}</p><p id="provider-operation-result" class="provider-result" tabindex="-1" role="status" aria-live="off">${escapeHtml(this._providerResult)}</p></div><div id="provider-actions" class="actions">${this._providerActionsMarkup(view)}</div></section>`;
  }

  _fallbackMarkup() {
    const identity = this._bootstrap?.snapshot?.identity || {};
    const provider = this._bootstrap?.snapshot?.provider || {};
    const degradedCode = String(this._bootstrap?.degraded_code || "");
    const degradedMessages = {
      cockpit_asset_unavailable: this._t("fallback.asset"),
      cockpit_api_unsupported: this._t("fallback.api"),
    };
    const message = degradedMessages[degradedCode]
      || this._t("fallback.none");
    return `<section class="state ${degradedCode ? "degraded" : ""}" id="generic-main" aria-labelledby="generic-title"><h1 id="generic-title" tabindex="-1">${escapeHtml(identity.game_name || this._t("fallback.unsupported"))}</h1><p>${escapeHtml(this._t("fallback.basic", { message }))}</p><div class="stat"><strong>${escapeHtml(this._t("fallback.service_id"))}</strong><span>${escapeHtml(identity.service_id)}</span></div><div class="stat"><strong>${escapeHtml(this._t("fallback.address"))}</strong><span>${escapeHtml(provider.address || this._t("fallback.unavailable"))}</span></div><p><a href="#" id="fallback-tools">${escapeHtml(this._t("fallback.open_tools"))}</a></p></section>`;
  }

  _toolsMarkup() {
    const snapshot = this._bootstrap?.snapshot || {};
    const identity = snapshot.identity || {};
    const filesystem = snapshot.tools?.filesystem || {};
    const operations = snapshot.operations || {};
    const unknown = Array.isArray(operations.unknown) ? operations.unknown : [];
    const operationItems = unknown.length
      ? unknown.map((item) => `<div class="state degraded"><h3>${escapeHtml(this._t("tools.unknown", { kind: item.kind }))}</h3><p>${escapeHtml(item.expected_evidence)}</p><p>${escapeHtml(this._t("tools.ack_help"))}</p><button class="secondary acknowledge-operation" type="button" data-operation-id="${escapeHtml(item.operation_id)}">${escapeHtml(this._t("tools.ack"))}</button></div>`).join("")
      : `<p>${escapeHtml(operations.available === false ? this._t("tools.unavailable") : operations.active?.length ? this._t("tools.active") : this._t("tools.clear"))}</p>`;
    return `<main id="nitrado-tools-main" aria-labelledby="tools-title"><section class="state"><h1 id="tools-title" tabindex="-1">${escapeHtml(this._t("tools.title"))}</h1><p>${escapeHtml(this._t("tools.intro"))}</p></section><div class="tools-grid"><section aria-labelledby="identity-tools-title"><h2 id="identity-tools-title">${escapeHtml(this._t("tools.identity"))}</h2><div class="stat"><strong>${escapeHtml(this._t("tools.account"))}</strong><span>${escapeHtml(identity.account_entry_id)}</span></div><div class="stat"><strong>${escapeHtml(this._t("tools.service_id"))}</strong><span>${escapeHtml(identity.service_id)}</span></div><div class="stat"><strong>${escapeHtml(this._t("tools.profile"))}</strong><span>${escapeHtml(identity.profile_name)}</span></div></section><section aria-labelledby="filesystem-tools-title"><h2 id="filesystem-tools-title">${escapeHtml(this._t("tools.filesystem"))}</h2><div class="stat"><strong>${escapeHtml(this._t("tools.relevant"))}</strong><span>${escapeHtml(filesystem.relevant ? this._t("tools.yes") : this._t("tools.no"))}</span></div><div class="stat"><strong>${escapeHtml(this._t("tools.transport"))}</strong><span>${escapeHtml(filesystem.observed_transport || this._t("tools.not_observed"))}</span></div><div class="stat"><strong>${escapeHtml(this._t("tools.mapping"))}</strong><span>${escapeHtml(filesystem.http_root_mapping_proven ? this._t("tools.proven") : this._t("tools.not_proven"))}</span></div></section></div><section aria-labelledby="operation-tools-title"><h2 id="operation-tools-title">${escapeHtml(this._t("tools.recovery"))}</h2>${operationItems}</section></main>`;
  }

  _ensureShell() {
    if (this.shadowRoot.querySelector("#host-shell")) return;
    this.shadowRoot.innerHTML = `<style>${styles}</style><div id="host-shell"><a class="skip" href="#server-content" id="skip-link">${escapeHtml(this._t("shell.skip"))}</a><header><div class="header-row"><div class="brand"><strong>${escapeHtml(this._t("shell.brand"))}</strong><span>${escapeHtml(this._t("shell.subtitle"))}</span></div><div class="picker" id="service-picker-region"></div></div></header><div class="shell" id="server-content"></div></div><div id="host-live" class="sr-live" aria-live="polite" aria-atomic="true"></div><div id="cockpit-live" class="sr-live" aria-live="polite" aria-atomic="true"></div>`;
    this.shadowRoot.querySelector("#skip-link")?.addEventListener("click", (event) => {
      const target = this.shadowRoot.querySelector(event.currentTarget.dataset.target || "#server-content");
      if (!target) return;
      event.preventDefault();
      target.focus?.();
      target.scrollIntoView?.({ block: "start", behavior: "auto" });
    });
  }

  _render() {
    this._ensureShell();
    const selectedValue = this._selected ? `${this._selected.account_entry_id}|${this._selected.service_id}` : "";
    const options = this._services.map((item) => `<option value="${escapeHtml(`${item.account_entry_id}|${item.service_id}`)}" ${selectedValue === `${item.account_entry_id}|${item.service_id}` ? "selected" : ""}>${escapeHtml(item.name)} · ${escapeHtml(item.account_title || item.account_label || item.account_entry_id)} · ${escapeHtml(item.profile?.name || this._t("shell.generic"))}</option>`).join("");
    let content = "";
    if (["loading", "initializing"].includes(this._status)) content = `<section class="state" role="status"><h1 id="loading-title" tabindex="-1">${escapeHtml(this._t("state.loading"))}</h1><p>${escapeHtml(this._t("state.loading_help"))}</p></section>`;
    else if (this._status === "empty") content = `<section class="state"><h1 id="empty-title" tabindex="-1">${escapeHtml(this._t("state.empty"))}</h1><p>${escapeHtml(this._t("state.empty_help"))}</p><p><a href="/config/integrations/integration/nitrado_gameserver">${escapeHtml(this._t("state.open_settings"))}</a></p></section>`;
    else if (this._status === "disambiguation") content = `<section class="state"><h1 id="disambiguation-title" tabindex="-1">${escapeHtml(this._t("state.disambiguation"))}</h1><p>${escapeHtml(this._error)}</p></section>`;
    else if (this._status === "error") content = `<section class="state"><h1 id="error-title" tabindex="-1">${escapeHtml(this._t("state.unavailable"))}</h1><p>${escapeHtml(this._error)}</p><p><button id="host-retry" type="button">${escapeHtml(this._t("state.retry"))}</button></p></section>`;
    else if (this._status === "degraded") content = `${this._providerMarkup()}<section class="state degraded"><h1 id="degraded-title" tabindex="-1">${escapeHtml(this._t("state.game_unavailable"))}</h1><p>${escapeHtml(this._error)}</p><p>${escapeHtml(this._t("state.basic"))}</p><div class="actions"><button id="cockpit-retry" type="button">${escapeHtml(this._t("state.retry_game"))}</button></div></section>${this._fallbackMarkup()}`;
    else if (this._status === "ready") {
      const body = this._route === "tools" ? this._toolsMarkup() : this._bootstrap?.cockpit ? `<div id="cockpit-mount" class="mount" aria-label="${escapeHtml(this._t("shell.game_cockpit"))}" tabindex="-1"></div>` : this._fallbackMarkup();
      content = `${this._providerMarkup()}<nav class="host-nav" aria-label="${escapeHtml(this._t("shell.provider_tools"))}"><button id="tools-toggle" class="secondary" type="button">${escapeHtml(this._route === "tools" ? this._t("shell.back") : this._t("tools.title"))}</button></nav>${body}`;
    }
    const picker = this.shadowRoot.querySelector("#service-picker-region");
    picker.hidden = this._services.length === 1;
    picker.innerHTML = `<label for="service-picker">${escapeHtml(this._t("shell.selected"))}</label><select id="service-picker" ${this._services.length ? "" : "disabled"}><option value="">${escapeHtml(this._t("shell.choose"))}</option>${options}</select>`;
    this.shadowRoot.querySelector("#server-content").innerHTML = content;
    this._bindHost();
    this._retargetSkipLink();
  }

  _mountDialog() {
    if (!this._renderDialog || this.shadowRoot.querySelector("#host-confirmation-dialog")) return;
    const request = this._dialogRequest || {};
    const template = document.createElement("template");
    template.innerHTML = `<dialog id="host-confirmation-dialog" aria-labelledby="confirm-title" aria-describedby="confirm-description"><div class="dialog-body"><h2 id="confirm-title">${escapeHtml(request.title || this._t("dialog.title"))}</h2><p id="confirm-description">${escapeHtml(request.explanation || this._t("dialog.explanation"))}</p><div class="dialog-actions"><button id="dialog-cancel" class="secondary" type="button">${escapeHtml(request.cancelLabel || this._t("dialog.cancel"))}</button><button id="dialog-confirm" class="${request.destructive ? "danger" : ""}" type="button">${escapeHtml(request.confirmLabel || this._t("dialog.continue"))}</button></div></div></dialog>`;
    this.shadowRoot.append(template.content);
    this._bindDialog();
  }

  _bindProviderActions() {
    this.shadowRoot.querySelector("#host-start")?.addEventListener("click", () => void this._providerAction("start"));
    this.shadowRoot.querySelector("#host-stop")?.addEventListener("click", () => void this._providerAction("stop"));
    this.shadowRoot.querySelector("#host-refresh")?.addEventListener("click", () => void this._providerAction("refresh"));
    this.shadowRoot.querySelector("#host-open")?.addEventListener("click", () => void this._providerAction("open-nitrado"));
  }

  _bindDialog() {
    this.shadowRoot.querySelector("#dialog-cancel")?.addEventListener("click", () => this._finishDialog(false));
    this.shadowRoot.querySelector("#dialog-confirm")?.addEventListener("click", () => this._finishDialog(true));
    this.shadowRoot.querySelector("#host-confirmation-dialog")?.addEventListener("cancel", (event) => { event.preventDefault(); this._finishDialog(false); });
  }

  _bindHost() {
    this.shadowRoot.querySelector("#service-picker")?.addEventListener("change", (event) => void this._selectService(event.target.value));
    this._bindProviderActions();
    this.shadowRoot.querySelector("#tools-toggle")?.addEventListener("click", () => void this._navigate(this._route === "tools" ? (this._bootstrap?.cockpit?.default_route || "overview") : "tools"));
    this.shadowRoot.querySelector("#fallback-tools")?.addEventListener("click", (event) => { event.preventDefault(); void this._navigate("tools"); });
    this.shadowRoot.querySelector("#host-retry")?.addEventListener("click", () => { this._status = "loading"; void this._initialize(); });
    this.shadowRoot.querySelector("#cockpit-retry")?.addEventListener("click", () => { this._error = ""; void this._loadBootstrap({ replaceHistory: true }); });
    this.shadowRoot.querySelectorAll(".acknowledge-operation").forEach((button) => button.addEventListener("click", () => void this._acknowledgeOperation(button.dataset.operationId)));
    this._bindDialog();
  }

  _retargetSkipLink() {
    const link = this.shadowRoot.querySelector("#skip-link");
    if (!link) return;
    let target = "server-content";
    if (this._status === "ready") target = this._route === "tools" ? "nitrado-tools-main" : this._bootstrap?.cockpit ? "cockpit-mount" : "generic-main";
    else if (this._status === "degraded") target = "degraded-title";
    else if (this._status === "error") target = "error-title";
    else if (["loading", "initializing"].includes(this._status)) target = "loading-title";
    else if (this._status === "empty") target = "empty-title";
    else if (this._status === "disambiguation") target = "disambiguation-title";
    link.setAttribute("href", `#${target}`);
    link.dataset.target = `#${target}`;
  }

  async _acknowledgeOperation(operationId) {
    const selectedKey = `${this._selected?.account_entry_id}:${this._selected?.service_id}`;
    const accepted = await this._requestConfirmation({
      title: this._t("ack.title"),
      explanation: this._t("ack.help"),
      confirmLabel: this._t("ack.confirm"),
    });
    if (!accepted
      || selectedKey !== `${this._selected?.account_entry_id}:${this._selected?.service_id}`
      || !this._operationBlocked()) return;
    try {
      await this._capability("acknowledge-operation", { operation_id: operationId, confirm: true });
      await this._refreshBootstrapSnapshot();
      this._render();
      this._announceHost(this._t("ack.done"));
    } catch (error) {
      this._announceHost(error?.message || this._t("ack.failed"));
    }
  }

  _focusAfterNavigation() {
    queueMicrotask(() => {
      const explicit = this._pendingFocus;
      this._pendingFocus = null;
      const name = this._bootstrap?.snapshot?.identity?.service_name || this._t("provider.server");
      const routeItem = this._bootstrap?.cockpit?.routes?.find?.((item) => (Array.isArray(item) ? item[0] : item?.key) === this._route);
      const cockpitRoute = Array.isArray(routeItem) ? routeItem[1] : routeItem?.label;
      const section = this._route === "tools" ? this._t("tools.title") : cockpitRoute || humanizeRoute(this._route);
      document.title = this._t("document.section", { name, section });
      const defaultTarget = this._route === "tools"
        ? "#tools-title"
        : this._bootstrap?.cockpit ? "#cockpit-mount .panel h2" : "#generic-title";
      const target = explicit
        ? this.shadowRoot.querySelector(explicit)
        : this.shadowRoot.querySelector(defaultTarget);
      target?.focus?.();
    });
  }

  _setTitleAndFocus(title, selector) {
    document.title = title;
    queueMicrotask(() => this.shadowRoot.querySelector(selector)?.focus?.());
  }

  _announceHost(message) { this._hostAnnouncement = message === this._hostAnnouncement ? `${message} ` : message; this.shadowRoot.querySelector("#host-live")?.replaceChildren(document.createTextNode(this._hostAnnouncement)); }
  _announceCockpit(message) { this._cockpitAnnouncement = message === this._cockpitAnnouncement ? `${message} ` : message; this.shadowRoot.querySelector("#cockpit-live")?.replaceChildren(document.createTextNode(this._cockpitAnnouncement)); }
}

if (!customElements.get(PANEL_ELEMENT)) customElements.define(PANEL_ELEMENT, NitradoGameServerPanel);

export { PANEL_ELEMENT };
