// @vitest-environment happy-dom

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import axe from "axe-core";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { hostTranslations, PANEL_ELEMENT } from "../custom_components/nitrado_gameserver/frontend/nitrado-game-server-panel.js";
import * as palworldModule from "../custom_components/nitrado_gameserver/frontend/palworld-cockpit.js";

const HOST_SOURCE_PATH = join(process.cwd(), "custom_components/nitrado_gameserver/frontend/nitrado-game-server-panel.js");
const PALWORLD_SOURCE_PATH = join(process.cwd(), "custom_components/nitrado_gameserver/frontend/palworld-cockpit.js");
const literalTranslationKeys = (source) => [...source.matchAll(/(?:this\.)?_t\("([^"]+)"/g)].map((match) => match[1]);

const identity = {
  account_entry_id: "account-a",
  service_id: "19538356",
  service_name: "Palbox",
  game_name: "Palworld",
  profile_id: "palworld",
  profile_name: "Palworld",
};

const entityIds = Object.freeze({
  start: "button.palbox_start",
  stop: "button.palbox_stop",
  refresh: "button.palbox_refresh",
  player_count: "sensor.palbox_player_count",
  player_max: "sensor.palbox_player_max",
  online_players: "sensor.palbox_online_players",
  address: "sensor.palbox_address",
  player_data_valid: "binary_sensor.palbox_player_data_valid",
  auto_shutdown: "switch.palbox_auto_shutdown",
  auto_shutdown_status: "sensor.palbox_auto_shutdown_status",
  shutdown_pending: "binary_sensor.palbox_shutdown_pending",
  idle_time_remaining: "sensor.palbox_idle_time_remaining",
  idle_shutdown_minutes: "number.palbox_idle_shutdown_minutes",
  startup_cooldown_minutes: "number.palbox_startup_cooldown_minutes",
  cancel_pending_shutdown: "button.palbox_cancel_pending_shutdown",
  last_shutdown_reason: "sensor.palbox_last_shutdown_reason",
  last_reset_reason: "sensor.palbox_last_reset_reason",
});

const entityStates = () => ({
  start: "unknown",
  stop: "unavailable",
  refresh: "unknown",
  player_count: "0",
  player_max: "32",
  online_players: "None",
  address: "palbox.example:8211",
  player_data_valid: "on",
  auto_shutdown: "on",
  auto_shutdown_status: "Startup cooldown",
  shutdown_pending: "off",
  idle_time_remaining: "Not counting down",
  idle_shutdown_minutes: "20",
  startup_cooldown_minutes: "10",
  cancel_pending_shutdown: "unavailable",
  last_shutdown_reason: "None recorded",
  last_reset_reason: "Server stopped",
});

const reporting = (consent = "enabled", verification = "not_testable") => ({
  consent: {
    status: consent,
    reason: consent === "enabled"
      ? "Plaintext Palworld REST access is approved."
      : consent === "disabled"
        ? "Plaintext Palworld REST access is not approved."
        : consent === "unresolved"
          ? "An administrator decision is required."
          : "The saved decision cannot be read.",
  },
  verification: {
    status: verification,
    reason: verification === "verified"
      ? "A fresh trusted player count was received."
      : verification === "not_testable"
        ? "Verification resumes when the server runs."
        : verification === "unverified"
          ? "Palworld REST has not supplied a fresh trusted player count."
          : "Verification state cannot be read.",
  },
  endpoint_label: "palbox.example:8212",
});

const snapshot = ({ consent = "enabled", verification = "not_testable", states = {}, editorMutations = false, saveMutations = false, providerStatus = "stopped" } = {}) => {
  const values = { ...entityStates(), ...states };
  return {
    identity: { ...identity },
    provider: {
      status: providerStatus,
      address: "palbox.example:8211",
      status_fresh: true,
      using_cached_data: false,
    },
    entities: Object.fromEntries(Object.entries(entityIds).map(([key, entity_id]) => [key, {
      entity_id,
      state: values[key],
      attributes: {},
    }])),
    profile: {
      manifest: {},
      public_options: {},
      configured_options: [],
      public_state: { player_reporting: reporting(consent, verification) },
      editor_mutations_enabled: editorMutations,
      save_bundle_mutations_enabled: saveMutations,
    },
    tools: { filesystem: { relevant: true, observed_transport: "ftps", http_root_mapping_proven: true } },
  };
};

const descriptor = Object.freeze({
  key: "palworld",
  name: "Palworld",
  api_version: 1,
  frontend_revision: "development",
  default_route: "overview",
  route_keys: ["overview", "auto-shutdown", "save-games", "game-settings"],
  route_aliases: [["palworld", "settings", "game-settings"]],
  asset_url: "/api/nitrado_gameserver/cockpit-assets/nitrado_gameserver/palworld/development.js",
});

const handoffPath = "/api/nitrado_gameserver/cockpit-handoff/nonce_1234567890abcdefghijklmnopqrstuvwxyz";
const cockpitControllers = new Set();

const service = (account = "account-a", serviceId = "19538356", name = "Palbox") => ({
  account_entry_id: account,
  service_id: serviceId,
  name,
  profile: { name: "Palworld" },
});

const flush = async (turns = 5) => {
  for (let index = 0; index < turns; index += 1) {
    await Promise.resolve();
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
};

const waitFor = async (predicate) => {
  for (let index = 0; index < 30; index += 1) {
    if (predicate()) return;
    await flush(1);
  }
  throw new Error("Timed out waiting for frontend state");
};

const expectNoSeriousAxeViolations = async (root) => {
  const result = await axe.run(root, {
    runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"] },
    rules: { "color-contrast": { enabled: false } },
  });
  expect(result.violations.filter((item) => ["serious", "critical"].includes(item.impact)).map((item) => `${item.id}: ${item.help}`)).toEqual([]);
};

const makeBootstrap = (mountEpoch, overrides = {}) => ({
  cockpit_api_version: 1,
  registry_epoch: 4,
  lease: "lease-token",
  lease_expires_in: 900,
  mount_epoch: mountEpoch,
  degraded_code: null,
  cockpit: descriptor,
  snapshot: snapshot(),
  ...overrides,
});

const mountHost = async ({
  path = "/nitrado-game-servers/account-a/19538356/overview",
  services = [service()],
  bootstrapOverrides = {},
  states = {},
  module = palworldModule,
  bootstrapFactory = null,
} = {}) => {
  window.history.replaceState({}, "", path);
  let currentStates = { ...states };
  let currentBootstrapOverrides = bootstrapOverrides;
  const calls = [];
  const callApi = vi.fn(async (method, apiPath, body) => {
    calls.push({ method, path: apiPath, body });
    if (apiPath === "nitrado_gameserver/extensions") return { services };
    if (apiPath.includes("/cockpit/bootstrap")) {
      const mountEpoch = new URL(`http://ha.local/${apiPath}`).searchParams.get("mount_epoch");
      const overrides = bootstrapFactory ? bootstrapFactory(apiPath) : currentBootstrapOverrides;
      return makeBootstrap(mountEpoch, overrides);
    }
    if (apiPath.includes("/cockpit/capabilities/open-nitrado")) return { path: handoffPath };
    if (apiPath.includes("/cockpit/capabilities/save-bundle-transfer")) {
      return { code: "save_bundle_transfer_ready", transfer_token: "transfer_token_abcdefghijklmnopqrstuvwxyz" };
    }
    if (apiPath.includes("/cockpit/capabilities/save-bundle-job-status")) {
      if (body?.action === "download") {
        return { code: "save_bundle_job_ready", status: "ready", transfer_token: "ready_transfer_token_abcdefghijklmnopqrstuvwxyz" };
      }
      return { code: "save_bundle_review_ready", status: "ready", preview: { changed: false }, preview_token: null, restore_enabled: false };
    }
    if (apiPath.includes("/cockpit/capabilities/")) return { code: "accepted" };
    throw new Error(`Unexpected API call: ${method} ${apiPath}`);
  });
  const callWS = vi.fn(async (message) => {
    if (message?.type === "auth/sign_path") return { path: `${message.path}?authSig=signed-1` };
    throw new Error(`Unexpected WebSocket call: ${message?.type}`);
  });
  const panel = document.createElement(PANEL_ELEMENT);
  panel._module = vi.fn(async () => module);
  document.body.append(panel);
  panel.hass = {
    states: currentStates,
    callApi,
    callWS,
    connection: { subscribeEvents: vi.fn(async () => vi.fn()) },
  };
  await waitFor(() => panel._status === "degraded"
    || (panel._status === "ready" && (panel._route === "tools" || !panel._bootstrap?.cockpit || panel.shadowRoot.querySelector("#palworld-main"))));
  return {
    panel,
    root: panel.shadowRoot,
    callApi,
    callWS,
    calls,
    setBootstrap(overrides) { currentBootstrapOverrides = overrides; },
    pushStates(nextStates) {
      currentStates = nextStates;
      panel.hass = { ...panel.hass, states: currentStates };
    },
  };
};

const mountCockpit = async ({ route = "overview", consent = "enabled", verification = "not_testable", states = {}, editorMutations = false, saveMutations = false, providerStatus = "stopped", history = [], identityOverrides = {}, narrow = false, locale = "en" } = {}) => {
  const root = document.createElement("div");
  document.body.append(root);
  let current = { ...snapshot({ consent, verification, states, editorMutations, saveMutations, providerStatus }), identity: { ...identity, ...identityOverrides }, navigation: { current_route: route }, ui: { locale, narrow } };
  const context = {
    identity: current.identity,
    snapshot: current,
    navigation: {
      navigate: vi.fn(async (nextRoute) => {
        current = { ...current, navigation: { current_route: nextRoute } };
        await controller.update(current);
      }),
      openNitradoTools: vi.fn(),
    },
    operations: { entityAction: vi.fn(async () => undefined), refreshSnapshot: vi.fn(async () => undefined) },
    extensions: {
      updateProfileOption: vi.fn(async () => undefined),
      readEditableFile: vi.fn(async () => ({ file: {
        revision: "source-revision",
        model: {
          schema_revision: "palworld-1.0-2026-08",
          redacted_source: "[/Script/Pal.PalGameWorldSettings]\nOptionSettings=(ServerName=\"Test\",AdminPassword=[redacted],ExpRate=1.000000,bIsPvP=False,FutureKey=(A=1,B=2))\n",
          settings: [
            { key: "ServerName", raw_value: '"Test"', sensitive: false, configured: null },
            { key: "AdminPassword", raw_value: null, sensitive: true, configured: true },
            { key: "ExpRate", raw_value: "1.000000", sensitive: false, configured: null },
            { key: "bIsPvP", raw_value: "False", sensitive: false, configured: null },
            { key: "bEnablePlayerToPlayerDamage", raw_value: "False", sensitive: false, configured: null },
            { key: "bEnableDefenseOtherGuildPlayer", raw_value: "False", sensitive: false, configured: null },
            { key: "FutureKey", raw_value: "(A=1,B=2)", sensitive: false, configured: null },
          ],
        },
      } })),
      previewEditableFile: vi.fn(async () => ({
        preview_token: "preview-token",
        preview: {
          verdict: { state: "supported", reason: null },
          changed: true,
          requires_restart: true,
          redacted_diff: "-ServerName=Test\n+ServerName=Changed\n",
          redacted_changes_hidden: false,
        },
      })),
      applyEditableFile: vi.fn(async () => ({ wrote: true, requires_restart: true })),
      editableFileHistory: vi.fn(async () => ({ history })),
      rollbackEditableFile: vi.fn(async () => ({ wrote: true, requires_restart: true })),
      downloadSaveBundle: vi.fn(async () => ({ filename: "palworld-save.zip", bytes: 1234 })),
      inspectSaveBundle: vi.fn(async () => ({
        preview_token: "save-preview-token",
        restore_enabled: saveMutations,
        preview: {
          world_id: "world-id",
          changed: true,
          portable_manifest: true,
          current: { files: 3, bytes: 300 },
          proposed: { files: 4, bytes: 420 },
          counts: { replaced: 1, added: 1, unchanged: 0, preserved: 2, deleted: 0 },
          replaced: [{ path: "Level.sav", size: 100 }],
          added: [{ path: "Players/new.sav", size: 20 }],
          truncated: false,
        },
      })),
      applySaveBundle: vi.fn(async () => ({ operation_id: "op-save", transaction_id: "tx-save" })),
    },
    host: { confirm: vi.fn(async () => true), publishNavigationState: vi.fn(), announce: vi.fn(), reportFatal: vi.fn() },
    ui: {
      locale,
      narrow,
      interpolate: (template, values = {}) => String(template).replace(/\{([A-Za-z0-9_]+)\}/g, (match, key) => key in values ? String(values[key]) : match),
      translate: (catalog, key, values = {}) => {
        const template = catalog?.[locale]?.[key] ?? catalog?.[locale.split("-")[0]]?.[key] ?? catalog?.en?.[key] ?? key;
        return String(template).replace(/\{([A-Za-z0-9_]+)\}/g, (match, name) => name in values ? String(values[name]) : match);
      },
    },
    metadata: descriptor,
  };
  const controller = palworldModule.createCockpit();
  await controller.mount(root, context);
  cockpitControllers.add(controller);
  return { root, controller, context, update: (next) => controller.update(next) };
};

describe("profile-owned cockpit host", () => {
  beforeEach(() => { document.body.innerHTML = ""; sessionStorage.clear(); });
  afterEach(() => { document.body.innerHTML = ""; sessionStorage.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

  it("titles the page before focusing the active route heading", async () => {
    const mounted = await mountHost();
    await flush();
    expect(document.title).toContain("Palbox · Overview");
    expect(mounted.root.activeElement?.id).toBe("palworld-status-title");
  });

  it("migrates a unique legacy route to the composite identity and declared alias", async () => {
    const mounted = await mountHost({ path: "/nitrado-game-servers/19538356/settings" });
    expect(window.location.pathname).toBe("/nitrado-game-servers/account-a/19538356/game-settings");
    expect(mounted.root.querySelector('[role="tab"][aria-selected="true"]')?.textContent).toBe("Game settings");
  });

  it("maps legacy advanced to core tools for every profile", async () => {
    const mounted = await mountHost({ path: "/nitrado-game-servers/19538356/advanced" });
    expect(window.location.pathname).toBe("/nitrado-game-servers/account-a/19538356/tools");
    expect(mounted.root.querySelector("#nitrado-tools-main")).not.toBeNull();
  });

  it("preserves a route only across the same cockpit identity and revision", async () => {
    const mounted = await mountHost({
      path: "/nitrado-game-servers/account-a/19538356/auto-shutdown",
      services: [service(), service("account-b", "222", "Palbox Two")],
    });
    await mounted.panel._selectService("account-b|222");
    expect(window.location.pathname).toBe("/nitrado-game-servers/account-b/222/auto-shutdown");
  });

  it("ignores a stale bootstrap failure after a newer service is ready", async () => {
    const mounted = await mountHost({
      services: [
        service(),
        service("account-b", "222", "Palbox Two"),
        service("account-c", "333", "Palbox Three"),
      ],
    });
    let rejectB;
    let requestedB = false;
    const pendingB = new Promise((_resolve, reject) => { rejectB = reject; });
    const originalCallApi = mounted.panel.hass.callApi;
    mounted.panel.hass = {
      ...mounted.panel.hass,
      callApi: vi.fn(async (method, apiPath, body) => {
        if (apiPath.includes("accounts/account-b/")) {
          requestedB = true;
          return pendingB;
        }
        if (apiPath.includes("accounts/account-c/")) {
          const mountEpoch = new URL(`http://ha.local/${apiPath}`).searchParams.get("mount_epoch");
          return makeBootstrap(mountEpoch, {
            snapshot: {
              ...snapshot(),
              identity: {
                ...identity,
                account_entry_id: "account-c",
                service_id: "333",
                service_name: "Palbox Three",
              },
            },
          });
        }
        return originalCallApi(method, apiPath, body);
      }),
    };

    mounted.panel._selected = mounted.panel._services.find((item) => item.service_id === "222");
    const switchingB = mounted.panel._loadBootstrap();
    await waitFor(() => requestedB);
    mounted.panel._selected = mounted.panel._services.find((item) => item.service_id === "333");
    await mounted.panel._loadBootstrap();
    rejectB(new Error("late B failure"));
    await Promise.allSettled([switchingB]);

    expect(mounted.panel._status).toBe("ready");
    expect(mounted.panel._selected.service_id).toBe("333");
    expect(mounted.panel._error).not.toContain("late B failure");
  });

  it("does not let a stale controller update degrade a replacement cockpit", async () => {
    const mounted = await mountHost();
    let rejectOldUpdate;
    const oldController = mounted.panel._controller;
    oldController.update = vi.fn(() => new Promise((_resolve, reject) => { rejectOldUpdate = reject; }));
    const oldUpdate = mounted.panel._updateController();
    await waitFor(() => typeof rejectOldUpdate === "function");

    const nextEpoch = "replacement-mount-epoch";
    const nextController = { update: vi.fn(), dispose: vi.fn() };
    mounted.panel._mountEpoch = nextEpoch;
    mounted.panel._bootstrap = makeBootstrap(nextEpoch);
    mounted.panel._controller = nextController;
    rejectOldUpdate(new Error("late old-controller failure"));
    await oldUpdate;

    expect(mounted.panel._status).toBe("ready");
    expect(mounted.panel._controller).toBe(nextController);
    expect(nextController.dispose).not.toHaveBeenCalled();
  });

  it("abandons a stale module load before constructing its controller", async () => {
    const mounted = await mountHost();
    await mounted.panel._disposeController();
    let resolveOldModule;
    const createCockpit = vi.fn();
    mounted.panel._module = vi.fn(() => new Promise((resolve) => { resolveOldModule = resolve; }));
    const oldMount = mounted.panel._mountCockpit();
    await waitFor(() => typeof resolveOldModule === "function");

    mounted.panel._mountEpoch = "replacement-mount-epoch";
    mounted.panel._bootstrap = makeBootstrap("replacement-mount-epoch");
    resolveOldModule({ createCockpit });
    await oldMount;

    expect(createCockpit).not.toHaveBeenCalled();
    expect(mounted.panel._status).toBe("ready");
  });

  it("disposes a subscription that resolves after the panel disconnects", async () => {
    const panel = document.createElement(PANEL_ELEMENT);
    document.body.append(panel);
    let resolveSubscription;
    const unsubscribe = vi.fn();
    panel._hass = {
      connection: {
        subscribeEvents: vi.fn(() => new Promise((resolve) => { resolveSubscription = resolve; })),
      },
    };
    const subscribing = panel._subscribeEpochs();
    await waitFor(() => typeof resolveSubscription === "function");
    panel.remove();
    resolveSubscription(unsubscribe);
    await subscribing;

    expect(unsubscribe).toHaveBeenCalledOnce();
    expect(panel._unsubscribe).toBeNull();
  });

  it("restarts initialization after disconnect during discovery and reconnect", async () => {
    let resolveFirstDiscovery;
    const firstDiscovery = new Promise((resolve) => { resolveFirstDiscovery = resolve; });
    let discoveryCalls = 0;
    const panel = document.createElement(PANEL_ELEMENT);
    panel._module = vi.fn(async () => palworldModule);
    document.body.append(panel);
    panel.hass = {
      states: {},
      connection: { subscribeEvents: vi.fn(async () => vi.fn()) },
      callApi: vi.fn(async (_method, apiPath) => {
        if (apiPath === "nitrado_gameserver/extensions") {
          discoveryCalls += 1;
          if (discoveryCalls === 1) return firstDiscovery;
          return { services: [service()] };
        }
        if (apiPath.includes("/cockpit/bootstrap")) {
          const mountEpoch = new URL(`http://ha.local/${apiPath}`).searchParams.get("mount_epoch");
          return makeBootstrap(mountEpoch);
        }
        throw new Error(`Unexpected API path: ${apiPath}`);
      }),
    };
    await waitFor(() => discoveryCalls === 1);

    panel.remove();
    document.body.append(panel);
    resolveFirstDiscovery({ services: [service()] });

    await waitFor(() => discoveryCalls === 2 && panel._status === "ready");
    expect(panel.shadowRoot.querySelector("#palworld-main")).not.toBeNull();
    expect(panel._initializationPromise).toBeNull();
  });

  it("recovers from a transient epoch-subscription failure", async () => {
    const mounted = await mountHost();
    mounted.panel._unsubscribe?.();
    mounted.panel._unsubscribe = null;
    let attempts = 0;
    const unsubscribe = vi.fn();
    mounted.panel.hass.connection.subscribeEvents = vi.fn(async () => {
      attempts += 1;
      if (attempts === 1) throw new Error("transient websocket failure");
      return unsubscribe;
    });

    expect(await mounted.panel._subscribeEpochs()).toBe(false);
    expect(mounted.panel._unsubscribe).toBeNull();
    mounted.pushStates({ ...mounted.panel.hass.states });
    await waitFor(() => mounted.panel._unsubscribe === unsubscribe);

    expect(attempts).toBe(2);
    expect(mounted.panel._status).toBe("ready");
  });

  it("does not reuse an in-flight epoch subscription across reconnect", async () => {
    const panel = document.createElement(PANEL_ELEMENT);
    document.body.append(panel);
    const pending = [];
    panel._hass = {
      connection: {
        subscribeEvents: vi.fn(() => new Promise((resolve) => pending.push(resolve))),
      },
    };

    const staleEpoch = panel._connectionEpoch;
    const staleAttempt = panel._subscribeEpochs(staleEpoch);
    await waitFor(() => pending.length === 1);
    panel.remove();
    document.body.append(panel);

    const currentAttempt = panel._subscribeEpochs(panel._connectionEpoch);
    await waitFor(() => pending.length === 2);
    const staleUnsubscribe = vi.fn();
    pending[0](staleUnsubscribe);
    expect(await staleAttempt).toBe(false);
    expect(staleUnsubscribe).toHaveBeenCalledOnce();

    const currentUnsubscribe = vi.fn();
    pending[1](currentUnsubscribe);
    expect(await currentAttempt).toBe(true);
    expect(panel._unsubscribe).toBe(currentUnsubscribe);
    expect(panel._subscriptionRetryTimer).toBeNull();
  });

  it("uses a source-qualified alias or destination default across cockpit identities", async () => {
    const ark = {
      ...descriptor,
      key: "ark",
      name: "ARK",
      default_route: "dashboard",
      route_keys: ["dashboard", "shutdown-policy"],
      route_aliases: [["palworld", "auto-shutdown", "shutdown-policy"]],
    };
    const services = [service(), service("account-b", "222", "ARK box")];
    const mounted = await mountHost({
      path: "/nitrado-game-servers/account-a/19538356/auto-shutdown",
      services,
      bootstrapFactory: (apiPath) => apiPath.includes("accounts/account-b/")
        ? { cockpit: ark, snapshot: { ...snapshot(), identity: { ...identity, account_entry_id: "account-b", service_id: "222", service_name: "ARK box", profile_id: "ark", profile_name: "ARK" } } }
        : {},
    });
    mounted.panel._module = vi.fn(async () => palworldModule);
    await mounted.panel._selectService("account-b|222");
    expect(window.location.pathname).toBe("/nitrado-game-servers/account-b/222/shutdown-policy");

    ark.route_aliases = [];
    await mounted.panel._selectService("account-a|19538356");
    await mounted.panel._selectService("account-b|222");
    expect(window.location.pathname).toBe("/nitrado-game-servers/account-b/222/dashboard");
  });

  it("uses one-shot transfer capabilities for binary saves without exposing HA tokens", async () => {
    const mounted = await mountHost();
    const downloadResponse = new Response(new Blob(["zip-bytes"], { type: "application/zip" }), {
      status: 200,
      headers: { "Content-Disposition": 'attachment; filename="palworld-save.zip"' },
    });
    const startedPayload = { status: "running", job_id: "job_abcdefghijklmnopqrstuvwxyz" };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(startedPayload), {
        status: 202,
        headers: { "Content-Type": "application/json" },
      }))
      .mockResolvedValueOnce(downloadResponse)
      .mockResolvedValueOnce(new Response(JSON.stringify(startedPayload), {
        status: 202,
        headers: { "Content-Type": "application/json" },
      }));
    vi.stubGlobal("fetch", fetchMock);
    URL.createObjectURL = vi.fn(() => "blob:test");
    URL.revokeObjectURL = vi.fn();
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    const context = mounted.panel._controller.context;
    await context.extensions.downloadSaveBundle("world");
    await context.extensions.inspectSaveBundle("world", new File(["zip"], "save.zip", { type: "application/zip" }));
    const [downloadUrl, downloadOptions] = fetchMock.mock.calls[0];
    const [preparedUrl, preparedOptions] = fetchMock.mock.calls[1];
    const [inspectUrl, inspectOptions] = fetchMock.mock.calls[2];
    expect(downloadUrl).toContain("/save-bundles/world/download");
    expect(inspectUrl).toContain("/save-bundles/world/inspect");
    expect(downloadUrl).not.toContain("authSig");
    expect(inspectUrl).not.toContain("authSig");
    expect(downloadOptions.headers.Authorization).toBeUndefined();
    expect(inspectOptions.headers.Authorization).toBeUndefined();
    expect(downloadOptions.headers["X-Nitrado-Save-Transfer"]).toBe("transfer_token_abcdefghijklmnopqrstuvwxyz");
    expect(preparedUrl).toBe(downloadUrl);
    expect(preparedOptions.headers["X-Nitrado-Save-Transfer"]).toBe("ready_transfer_token_abcdefghijklmnopqrstuvwxyz");
    expect(preparedOptions.headers["X-Nitrado-Save-Job"]).toBe("job_abcdefghijklmnopqrstuvwxyz");
    expect(inspectOptions.headers["X-Nitrado-Save-Transfer"]).toBe("transfer_token_abcdefghijklmnopqrstuvwxyz");
    expect(mounted.callApi).toHaveBeenCalledWith(
      "POST",
      expect.stringContaining("/cockpit/capabilities/save-bundle-transfer"),
      expect.objectContaining({ lease: "lease-token", bundle_key: "world", action: "download" }),
    );
  });

  it("honors namespaced source identity during Back/Forward restoration", async () => {
    const mounted = await mountHost({ path: "/nitrado-game-servers/account-a/19538356/overview" });
    window.history.replaceState({
      nitradoCockpit: true,
      accountEntryId: "account-a",
      serviceId: "19538356",
      cockpitKey: "other-game",
      frontendRevision: "sha256:old",
      route: "auto-shutdown",
    }, "", "/nitrado-game-servers/account-a/19538356/auto-shutdown");
    await mounted.panel._historyNavigation();
    expect(window.location.pathname).toBe("/nitrado-game-servers/account-a/19538356/overview");

    window.history.replaceState({
      nitradoCockpit: true,
      accountEntryId: "account-a",
      serviceId: "19538356",
      cockpitKey: "palworld",
      frontendRevision: "development",
      route: "auto-shutdown",
    }, "", "/nitrado-game-servers/account-a/19538356/auto-shutdown");
    await mounted.panel._historyNavigation();
    expect(window.location.pathname).toBe("/nitrado-game-servers/account-a/19538356/auto-shutdown");
  });

  it("refuses to guess when two accounts contain the same service ID", async () => {
    window.history.replaceState({}, "", "/nitrado-game-servers/19538356/overview");
    const panel = document.createElement(PANEL_ELEMENT);
    document.body.append(panel);
    panel.hass = {
      states: {},
      callApi: vi.fn(async () => ({ services: [service("account-a"), service("account-b")] })),
      connection: { subscribeEvents: vi.fn() },
    };
    await waitFor(() => panel._status === "disambiguation");
    expect(panel.shadowRoot.textContent).toContain("more than one Nitrado account");
    expect(panel.shadowRoot.querySelector("#cockpit-mount")).toBeNull();
  });

  it.each([
    ["cockpit_asset_unavailable", "frontend asset is missing or failed validation"],
    ["cockpit_api_unsupported", "incompatible with the current integration version"],
  ])("shows the real cockpit degradation for %s", async (degradedCode, expected) => {
    const mounted = await mountHost({
      bootstrapOverrides: { cockpit: null, degraded_code: degradedCode },
    });
    expect(mounted.root.querySelector("#generic-main").classList.contains("degraded")).toBe(true);
    expect(mounted.root.textContent).toContain(expected);
    expect(mounted.root.textContent).not.toContain("No specialized game cockpit is installed");
  });

  it("keeps the mounted controller and DOM root across HA/provider refreshes", async () => {
    const mounted = await mountHost({ states: Object.fromEntries(Object.entries(entityIds).map(([key, id]) => [id, { state: entityStates()[key] }])) });
    const controller = mounted.panel._controller;
    const cockpitRoot = mounted.root.querySelector("#cockpit-mount");
    mounted.pushStates({
      ...mounted.panel.hass.states,
      [entityIds.player_count]: { state: "3" },
      [entityIds.online_players]: { state: "Anubis, Lamball, Depresso" },
    });
    await flush();
    expect(mounted.panel._controller).toBe(controller);
    expect(mounted.root.querySelector("#cockpit-mount")).toBe(cockpitRoot);
    expect(mounted.root.querySelector("#palworld-main")).not.toBeNull();
    expect(mounted.root.textContent).toContain("3 of 32 players online");
  });

  it("keeps Cockpit API v1 backward compatible while live-updating locale and narrow", async () => {
    let mountedContext;
    const oldController = {
      mount: vi.fn((root, context) => { mountedContext = context; root.innerHTML = '<main id="palworld-main"><h1 id="old-cockpit" tabindex="-1">Old cockpit</h1><div class="panel"><h2 tabindex="-1">Overview</h2></div></main>'; }),
      update: vi.fn(),
      dispose: vi.fn(),
    };
    const module = {
      cockpitMetadata: { cockpitApiVersion: 1, frontendRevision: "development", key: "palworld" },
      createCockpit: () => oldController,
    };
    const mounted = await mountHost({ module });
    expect(mountedContext.ui.locale).toBe("en");
    expect(mountedContext.ui.translate({ en: { hello: "Hello {name}" } }, "hello", { name: "Gabe" })).toBe("Hello Gabe");
    mounted.panel.narrow = true;
    await flush();
    expect(oldController.update).toHaveBeenLastCalledWith(expect.objectContaining({ ui: expect.objectContaining({ narrow: true }) }));
    mounted.panel.hass = { ...mounted.panel.hass, locale: { language: "en-XA" } };
    await flush();
    expect(oldController.update).toHaveBeenLastCalledWith(expect.objectContaining({ ui: expect.objectContaining({ locale: "en-XA" }) }));
  });

  it("keeps host and Palworld copy in complete, strictly keyed owner catalogs", () => {
    const hostSource = readFileSync(HOST_SOURCE_PATH, "utf8");
    const palworldSource = readFileSync(PALWORLD_SOURCE_PATH, "utf8");
    expect(hostSource).not.toMatch(/\bPalworld\b/);

    for (const key of literalTranslationKeys(hostSource)) expect(hostTranslations.en[key], `missing host translation: ${key}`).toBeTypeOf("string");
    for (const key of literalTranslationKeys(palworldSource)) expect(palworldModule.palworldTranslations.en[key], `missing Palworld translation: ${key}`).toBeTypeOf("string");
    for (const key of ["running", "stopped", "stopping", "starting", "restarting", "unavailable"]) {
      expect(hostTranslations.en[`status.${key}`]).toBeTypeOf("string");
    }
    for (const key of ["overview", "auto-shutdown", "save-games", "game-settings"]) {
      expect(palworldModule.palworldTranslations.en[`route.${key}`]).toBeTypeOf("string");
    }
    for (const key of ["all", "favorites", "world", "players", "pals", "combat", "bases", "multiplayer", "server", "performance", "other"]) {
      expect(palworldModule.palworldTranslations.en[`category.${key}`]).toBeTypeOf("string");
    }

    const visibleClassLiteral = />\s*[A-Z][A-Za-z][^<>{}\n]{2,}</;
    const directTextAssignment = /(?:textContent|innerText)\s*=\s*["`][A-Z]/;
    expect(hostSource.slice(hostSource.indexOf("class NitradoGameServerPanel"))).not.toMatch(visibleClassLiteral);
    expect(palworldSource.slice(palworldSource.indexOf("class PalworldCockpit"))).not.toMatch(visibleClassLiteral);
    expect(hostSource.slice(hostSource.indexOf("class NitradoGameServerPanel"))).not.toMatch(directTextAssignment);
    expect(palworldSource.slice(palworldSource.indexOf("class PalworldCockpit"))).not.toMatch(directTextAssignment);

    for (const template of [...Object.values(hostTranslations.en), ...Object.values(palworldModule.palworldTranslations.en)]) {
      expect(template).not.toMatch(/%[sd]/);
      for (const placeholder of template.matchAll(/\{([^{}]+)\}/g)) expect(placeholder[1]).toMatch(/^[A-Za-z0-9_]+$/);
    }
  });

  it("keeps one provider region and moves focus to its stable result when Start becomes Stop", async () => {
    let bootstrapCount = 0;
    const mounted = await mountHost({
      bootstrapFactory: () => ({
        snapshot: snapshot({ providerStatus: bootstrapCount++ === 0 ? "stopped" : "started" }),
      }),
    });
    const provider = mounted.root.querySelector(".provider");
    const start = mounted.root.querySelector("#host-start");
    start.focus();
    start.click();
    expect(mounted.root.querySelector(".provider")).toBe(provider);
    expect(mounted.root.querySelector("#host-start")).toBe(start);
    expect(start.textContent).toContain("Starting");
    await waitFor(() => mounted.root.querySelector("#host-stop") && !mounted.panel._providerPending);
    await flush();
    expect(mounted.root.querySelector(".provider")).toBe(provider);
    expect(mounted.root.querySelector("#host-start")).toBeNull();
    expect(mounted.root.activeElement?.id).toBe("provider-operation-result");
    expect(mounted.root.querySelector("#provider-operation-result").textContent).toContain("Start request accepted");
  });

  it("confirms Stop with Cancel focused and ignores a stale state change while the dialog is open", async () => {
    let bootstrapCount = 0;
    const mounted = await mountHost({
      bootstrapFactory: () => ({
        snapshot: snapshot({ providerStatus: bootstrapCount++ === 0 ? "started" : "stopped", states: { stop: "unknown" } }),
      }),
    });
    mounted.root.querySelector("#host-stop").click();
    await waitFor(() => mounted.root.querySelector("dialog"));
    expect(mounted.root.activeElement?.id).toBe("dialog-cancel");
    expect(mounted.root.querySelector("dialog").textContent).toContain("Stop Palbox");
    mounted.panel._bootstrap.snapshot.provider.status = "stopped";
    mounted.root.querySelector("#dialog-confirm").click();
    await flush();
    expect(mounted.calls.some((call) => call.path.includes("/capabilities/stop"))).toBe(false);
  });

  it("rejects a newer confirmation and settles the visible request on disconnect", async () => {
    const mounted = await mountHost();
    const first = mounted.panel._controller.context.host.confirm({
      title: "First confirmation",
      explanation: "Keep this request visible.",
      confirmLabel: "Proceed",
    });
    await waitFor(() => mounted.root.querySelector("dialog"));
    const second = await mounted.panel._controller.context.host.confirm({ title: "Second confirmation" });
    expect(second).toBe(false);
    expect(mounted.root.querySelector("dialog").textContent).toContain("First confirmation");
    expect(mounted.root.querySelector("dialog").textContent).not.toContain("Second confirmation");
    mounted.panel.disconnectedCallback();
    await expect(first).resolves.toBe(false);
  });

  it("mounts and settles the host confirmation beside a profile-owned dialog", async () => {
    const mounted = await mountHost();
    const profileDialog = document.createElement("dialog");
    profileDialog.id = "profile-owned-dialog";
    mounted.root.querySelector("#cockpit-mount").append(profileDialog);
    profileDialog.showModal();
    const decision = mounted.panel._controller.context.host.confirm({
      title: "Nested confirmation",
      explanation: "The host must own a separate dialog.",
      confirmLabel: "Continue",
    });
    await waitFor(() => mounted.root.querySelector("#host-confirmation-dialog")?.open);
    expect(mounted.root.querySelector("#profile-owned-dialog")).toBe(profileDialog);
    expect(mounted.root.querySelector("#host-confirmation-dialog")).not.toBe(profileDialog);
    expect(mounted.root.activeElement?.id).toBe("dialog-cancel");
    mounted.root.querySelector("#dialog-cancel").click();
    await expect(decision).resolves.toBe(false);
    expect(mounted.root.querySelector("#host-confirmation-dialog")).toBeNull();
    expect(mounted.root.querySelector("#profile-owned-dialog")).toBe(profileDialog);
  });

  it("fails mutations closed and exposes truthful unknown-operation recovery", async () => {
    const operation = {
      operation_id: "op-deadbeef",
      kind: "stop",
      target_key: "server-control",
      phase: "unknown",
      expected_evidence: "A later status refresh must establish the provider outcome.",
    };
    const mounted = await mountHost({
      bootstrapOverrides: {
        snapshot: {
          ...snapshot(),
          operations: { available: true, blocked: true, active: [], unknown: [operation], history: [] },
        },
      },
    });
    expect(mounted.root.querySelector("#host-start").disabled).toBe(true);
    await mounted.panel._navigate("tools");
    expect(mounted.root.textContent).toContain("Outcome unknown: stop");
    expect(mounted.root.textContent).toContain("does not claim the operation succeeded or failed");
  });

  it("changes cockpit routes without detaching the mount and moves focus to the selected tab", async () => {
    const mounted = await mountHost();
    const controller = mounted.panel._controller;
    const cockpitRoot = mounted.root.querySelector("#cockpit-mount");
    mounted.root.querySelector('#palworld-tab-auto-shutdown').click();
    await flush();
    expect(window.location.pathname).toBe("/nitrado-game-servers/account-a/19538356/auto-shutdown");
    expect(mounted.panel._controller).toBe(controller);
    expect(mounted.root.querySelector("#cockpit-mount")).toBe(cockpitRoot);
    expect(mounted.root.activeElement?.id).toBe("palworld-tab-auto-shutdown");
    expect(document.title).toContain("Palbox · Auto Shutdown");
  });

  it("opens and cancels a navigation confirmation without replacing the cockpit mount", async () => {
    const mounted = await mountHost();
    const cockpitRoot = mounted.root.querySelector("#cockpit-mount");
    mounted.panel._controller.navigationGuard = vi.fn(async () => ({ decision: "confirm" }));
    const trigger = mounted.root.querySelector("#palworld-tab-overview");
    trigger.focus();
    const navigation = mounted.panel._navigate("auto-shutdown", { focus: "#palworld-tab-auto-shutdown" });
    await waitFor(() => mounted.root.querySelector("dialog"));
    expect(mounted.root.querySelector("#cockpit-mount")).toBe(cockpitRoot);
    expect(mounted.root.activeElement?.id).toBe("dialog-cancel");
    mounted.root.querySelector("#dialog-cancel").click();
    await navigation;
    expect(window.location.pathname).toBe("/nitrado-game-servers/account-a/19538356/overview");
    expect(mounted.root.querySelector("#cockpit-mount")).toBe(cockpitRoot);
    expect(mounted.root.querySelector("dialog")).toBeNull();
    await flush();
    expect(mounted.root.activeElement).toBe(trigger);
  });

  it("treats Escape as Cancel and accepts an explicit Leave decision", async () => {
    const mounted = await mountHost();
    mounted.panel._controller.navigationGuard = vi.fn(async () => ({ decision: "confirm" }));
    let navigation = mounted.panel._navigate("auto-shutdown", { focus: "#palworld-tab-auto-shutdown" });
    await waitFor(() => mounted.root.querySelector("dialog"));
    mounted.root.querySelector("dialog").dispatchEvent(new Event("cancel", { cancelable: true }));
    await navigation;
    expect(mounted.panel._route).toBe("overview");

    navigation = mounted.panel._navigate("auto-shutdown", { focus: "#palworld-tab-auto-shutdown" });
    await waitFor(() => mounted.root.querySelector("dialog"));
    mounted.root.querySelector("#dialog-confirm").click();
    await navigation;
    expect(mounted.panel._route).toBe("auto-shutdown");
    expect(mounted.root.activeElement?.id).toBe("palworld-tab-auto-shutdown");
  });

  it("fails closed for retained pending and unknown navigation state", async () => {
    const mounted = await mountHost();
    const context = mounted.panel._controller.context;
    mounted.panel._controller.navigationGuard = vi.fn(async () => ({ decision: "allow" }));
    context.host.publishNavigationState({ dirty: false, mutationPhase: "pending", messageCode: "pending" });
    await mounted.panel._navigate("auto-shutdown");
    expect(mounted.panel._route).toBe("overview");
    expect(mounted.root.querySelector("#host-live").textContent).toContain("Finish the active operation");

    mounted.panel._controller.navigationGuard = vi.fn(async () => { throw new Error("crashed"); });
    context.host.publishNavigationState({ dirty: false, mutationPhase: "unknown", messageCode: "unknown" });
    const navigation = mounted.panel._navigate("auto-shutdown");
    await waitFor(() => mounted.root.querySelector("dialog"));
    mounted.root.querySelector("#dialog-cancel").click();
    await navigation;
    expect(mounted.panel._route).toBe("overview");
  });

  it("confirms conservatively when a navigation guard times out without retained state", async () => {
    const mounted = await mountHost();
    mounted.panel._navigationState = null;
    mounted.panel._controller.navigationGuard = vi.fn(() => new Promise(() => {}));
    vi.useFakeTimers();
    try {
      const navigation = mounted.panel._navigate("auto-shutdown");
      await vi.advanceTimersByTimeAsync(1801);
      expect(mounted.root.querySelector("dialog")).not.toBeNull();
      mounted.root.querySelector("#dialog-cancel").click();
      await navigation;
      expect(mounted.panel._route).toBe("overview");
    } finally {
      vi.useRealTimers();
    }
  });

  it("degrades on a lifecycle timeout and Retry remounts with title and focus", async () => {
    const mounted = await mountHost();
    const oldContext = mounted.panel._controller.context;
    mounted.panel._controller.update = vi.fn(() => new Promise(() => {}));
    vi.useFakeTimers();
    try {
      const navigation = mounted.panel._navigate("auto-shutdown");
      await vi.advanceTimersByTimeAsync(5001);
      await navigation;
    } finally {
      vi.useRealTimers();
    }
    expect(mounted.panel._status).toBe("degraded");
    expect(() => oldContext.operations.entityAction("auto_shutdown", "turn_off")).toThrow(/stale/);
    mounted.root.querySelector("#cockpit-retry").click();
    await waitFor(() => mounted.panel._status === "ready" && mounted.root.querySelector("#palworld-main"));
    await flush();
    expect(document.title).toContain("Auto Shutdown");
    expect(mounted.root.activeElement?.id).toBe("auto-title");
  });

  it("invalidates every captured capability when a cockpit degrades", async () => {
    const mounted = await mountHost();
    const context = mounted.panel._controller.context;
    const capabilityCalls = () => mounted.callApi.mock.calls.filter(([, path]) => path.includes("/cockpit/capabilities/")).length;
    const before = capabilityCalls();
    await mounted.panel._degradeCockpit("late failure");
    expect(() => context.operations.entityAction("auto_shutdown", "turn_off")).toThrow(/stale/);
    expect(() => context.extensions.updateProfileOption("allow_insecure_rest", true, { confirmed: true })).toThrow(/stale/);
    expect(capabilityCalls()).toBe(before);
    expect(document.title).toContain("Game cockpit unavailable");
    expect(mounted.root.activeElement?.id).toBe("degraded-title");
  });

  it("titles and focuses an initial cockpit load failure and retargets Retry safely", async () => {
    const brokenModule = { createCockpit: () => { throw new Error("factory failed"); } };
    const mounted = await mountHost({ module: brokenModule });
    await flush();
    expect(mounted.panel._status).toBe("degraded");
    expect(document.title).toContain("Game cockpit unavailable");
    expect(mounted.root.activeElement?.id).toBe("degraded-title");
    expect(mounted.root.querySelector("#skip-link").getAttribute("href")).toBe("#degraded-title");
  });

  it("disposes at the tools boundary and remounts a fresh controller on return", async () => {
    const mounted = await mountHost();
    const first = mounted.panel._controller;
    mounted.root.querySelector("#tools-toggle").click();
    await flush();
    expect(mounted.panel._controller).toBeNull();
    expect(mounted.root.querySelector("#nitrado-tools-main")).not.toBeNull();
    mounted.root.querySelector("#tools-toggle").click();
    await flush();
    expect(mounted.panel._controller).not.toBeNull();
    expect(mounted.panel._controller).not.toBe(first);
    expect(mounted.root.querySelector("#palworld-main")).not.toBeNull();
  });

  it("opens only a Home Assistant-signed one-time handoff path in a synchronously created tab", async () => {
    const replace = vi.fn();
    const popup = { location: { replace }, close: vi.fn(), opener: window };
    vi.spyOn(window, "open").mockReturnValue(popup);
    const mounted = await mountHost();
    mounted.root.querySelector("#host-open").click();
    await flush();
    expect(window.open).toHaveBeenCalledWith("about:blank", "_blank");
    expect(popup.opener).toBeNull();
    expect(mounted.callWS).toHaveBeenCalledWith({
      type: "auth/sign_path",
      path: handoffPath,
      expires: 60,
    });
    expect(replace).toHaveBeenCalledWith(`${handoffPath}?authSig=signed-1`);
    expect(mounted.callApi).toHaveBeenCalledWith(
      "POST",
      expect.stringContaining("/accounts/account-a/services/19538356/cockpit/capabilities/open-nitrado"),
      { lease: "lease-token" },
    );
  });

  it("closes the popup and fails visibly when Home Assistant cannot sign the handoff", async () => {
    const replace = vi.fn();
    const popup = { location: { replace }, close: vi.fn(), opener: window };
    vi.spyOn(window, "open").mockReturnValue(popup);
    const mounted = await mountHost();
    mounted.callWS.mockRejectedValueOnce(new Error("signing unavailable"));
    mounted.root.querySelector("#host-open").click();
    await flush();
    expect(replace).not.toHaveBeenCalled();
    expect(popup.close).toHaveBeenCalledOnce();
    expect(mounted.root.querySelector("#host-live").textContent).toContain("signing unavailable");
  });

  it("does not mint a handoff when the browser blocks the new tab", async () => {
    vi.spyOn(window, "open").mockReturnValue(null);
    const mounted = await mountHost();
    mounted.root.querySelector("#host-open").click();
    await flush();
    expect(mounted.callApi.mock.calls.some(([, path]) => path.includes("open-nitrado"))).toBe(false);
    expect(mounted.root.querySelector("#host-live").textContent).toContain("Allow pop-ups");
  });

  it("uses a generic skip target without knowing a profile's internal markup", async () => {
    const mounted = await mountHost();
    expect(mounted.root.querySelector("#skip-link").getAttribute("href")).toBe("#cockpit-mount");
    expect(mounted.root.querySelector("#cockpit-mount").getAttribute("tabindex")).toBe("-1");
    mounted.root.querySelector("#skip-link").click();
    expect(mounted.root.activeElement?.id).toBe("cockpit-mount");
  });

  it("keeps live-region nodes mounted and retargets skip on degradation", async () => {
    const mounted = await mountHost();
    const hostLive = mounted.root.querySelector("#host-live");
    const cockpitLive = mounted.root.querySelector("#cockpit-live");
    await mounted.panel._navigate("tools");
    expect(mounted.root.querySelector("#host-live")).toBe(hostLive);
    expect(mounted.root.querySelector("#cockpit-live")).toBe(cockpitLive);
    await mounted.panel._navigate("overview");
    await mounted.panel._degradeCockpit("broken module");
    expect(mounted.root.querySelector("#skip-link").getAttribute("href")).toBe("#degraded-title");
    mounted.root.querySelector("#skip-link").click();
    expect(mounted.root.activeElement?.id).toBe("degraded-title");
  });

  it("has no serious or critical structural accessibility violations across host states", async () => {
    const mounted = await mountHost();
    await expectNoSeriousAxeViolations(mounted.panel);
    await mounted.panel._navigate("tools");
    await expectNoSeriousAxeViolations(mounted.panel);
    await mounted.panel._degradeCockpit("broken module");
    await expectNoSeriousAxeViolations(mounted.panel);

    const generic = await mountHost({ bootstrapOverrides: { cockpit: null } });
    await expectNoSeriousAxeViolations(generic.panel);
  });

  it("has no serious or critical structural accessibility violations in the guard dialog and operation recovery", async () => {
    const operation = { operation_id: "op-1", kind: "stop", phase: "unknown", expected_evidence: "Refresh provider status." };
    const mounted = await mountHost({ bootstrapOverrides: { snapshot: { ...snapshot(), operations: { available: true, blocked: true, active: [], unknown: [operation], history: [] } } } });
    await mounted.panel._navigate("tools");
    await expectNoSeriousAxeViolations(mounted.panel);
    await mounted.panel._navigate("overview");
    mounted.panel._controller.navigationGuard = vi.fn(async () => ({ decision: "confirm" }));
    const navigation = mounted.panel._navigate("auto-shutdown");
    await waitFor(() => mounted.root.querySelector("dialog"));
    await expectNoSeriousAxeViolations(mounted.panel);
    mounted.root.querySelector("#dialog-cancel").click();
    await navigation;
  });

  it("retains the automatable responsive and zoom-safe CSS invariants", async () => {
    const mounted = await mountHost();
    const hostCss = mounted.root.querySelector("style").textContent;
    const cockpitCss = mounted.root.querySelector("#cockpit-mount style").textContent;
    expect(hostCss).toContain("@media (max-width:720px)");
    expect(hostCss).toContain("@media (max-width:360px)");
    expect(hostCss).toContain("flex-wrap:wrap");
    expect(hostCss).toContain("overflow-wrap:anywhere");
    expect(hostCss).toContain("--nitrado-readable-accent:color-mix");
    expect(hostCss).toContain("button { background:var(--nitrado-readable-accent);");
    expect(hostCss).toContain("button.danger { background:var(--nitrado-readable-error);");
    expect(cockpitCss).toContain("@media (max-width:680px)");
    expect(cockpitCss).toContain("overflow-x:auto");
    expect(cockpitCss).toContain("max-width:100%");
    expect(cockpitCss).toContain("min-height:44px");
    expect(cockpitCss).toContain("background:var(--nitrado-readable-accent);");
    expect(cockpitCss).toContain("details > summary { display:flex; align-items:center; min-height:44px;");
    expect(cockpitCss).toContain('.tabs button[aria-selected="true"] { color:var(--primary-text-color);');
  });
});

describe("Palworld cockpit", () => {
  beforeEach(() => { document.body.innerHTML = ""; sessionStorage.clear(); });
  afterEach(async () => {
    await Promise.all([...cockpitControllers].map((controller) => controller.dispose()));
    cockpitControllers.clear();
    document.body.innerHTML = "";
    sessionStorage.clear();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it.each([
    ["unresolved", "not_testable", "Player reporting needs a decision", "Blocked — player reporting decision required"],
    ["disabled", "not_testable", "Live player reporting is off", "Blocked — player reporting is off"],
    ["unavailable", "unavailable", "Player reporting status cannot be confirmed", "Blocked — player reporting status unavailable"],
    ["enabled", "unverified", "Player reporting is enabled but not verified", "Blocked — player reporting unavailable"],
  ])("renders truthful %s/%s dependency states", async (consent, verification, warning, blocked) => {
    const mounted = await mountCockpit({ consent, verification });
    expect(mounted.root.textContent).toContain(warning);
    expect(mounted.root.textContent).toContain(blocked);
  });

  it("stays quiet when reporting is enabled but cannot be tested while stopped", async () => {
    const mounted = await mountCockpit();
    expect(mounted.root.querySelector(".callout")).toBeNull();
    expect(mounted.root.querySelector("#palworld-tab-game-settings .tab-badge")).toBeNull();
    expect(mounted.root.textContent).toContain("0 of 32 players online");
    expect(mounted.root.textContent).toContain("Startup cooldown");
  });

  it.each([
    ["disabled", "not_testable", "Off", "Game settings, player reporting off"],
    ["unresolved", "not_testable", "Action needed", "Game settings, action needed for player reporting"],
    ["unavailable", "unavailable", "Status unavailable", "Game settings, player reporting status unavailable"],
    ["enabled", "unverified", "Check setup", "Game settings, player reporting needs attention"],
  ])("advertises the buried reporting prerequisite for %s/%s", async (consent, verification, badge, accessibleName) => {
    const mounted = await mountCockpit({ consent, verification });
    const tab = mounted.root.querySelector("#palworld-tab-game-settings");
    expect(tab.getAttribute("aria-label")).toBe(accessibleName);
    expect(tab.querySelector(".tab-badge")?.textContent).toBe(badge);
    tab.click();
    await flush();
    expect(mounted.context.navigation.navigate).toHaveBeenCalledWith("game-settings", { focus: "#palworld-tab-game-settings" });
    expect(mounted.root.querySelector("#player-reporting-title")).not.toBeNull();
  });

  it.each([
    ["unresolved", null, false],
    ["disabled", "disabled", false],
    ["enabled", "enabled", false],
    ["unavailable", null, true],
  ])("keeps confirmed and unresolved consent distinct for %s", async (consent, checked, disabled) => {
    const mounted = await mountCockpit({ route: "game-settings", consent, verification: consent === "enabled" ? "verified" : "not_testable" });
    const choices = [...mounted.root.querySelectorAll('input[name="player-reporting-choice"]')];
    expect(choices.find((item) => item.checked)?.value || null).toBe(checked);
    expect(mounted.root.querySelector("fieldset").disabled).toBe(disabled);
    if (consent === "unavailable") expect(mounted.root.textContent).toContain("changes are disabled");
  });

  it("saves explicit insecure-HTTP consent through the profile capability and refreshes truth", async () => {
    const mounted = await mountCockpit({ route: "game-settings", consent: "unresolved" });
    const enabled = mounted.root.querySelector('input[value="enabled"]');
    enabled.click();
    await flush();
    expect(mounted.context.extensions.updateProfileOption).toHaveBeenCalledWith(
      "allow_insecure_rest",
      true,
      { confirmed: true },
    );
    expect(mounted.context.operations.refreshSnapshot).toHaveBeenCalledOnce();
    expect(mounted.root.textContent).toContain("decision saved");
  });

  it("does not rewrite frozen confirmed truth when saving fails", async () => {
    const mounted = await mountCockpit({ route: "game-settings", consent: "disabled" });
    mounted.context.extensions.updateProfileOption.mockRejectedValueOnce(new Error("save failed"));
    mounted.root.querySelector('input[value="enabled"]').click();
    await flush();
    expect(mounted.root.querySelector('input[value="disabled"]').checked).toBe(true);
    expect(mounted.root.textContent).toContain("save failed");
    expect(mounted.root.querySelector(".feedback").dataset.kind).toBe("error");
    expect(document.activeElement?.id).toBe("player-reporting-enabled");
  });

  it("keeps the reporting choice focused and uses the persistent host announcer", async () => {
    let release;
    const pending = new Promise((resolve) => { release = resolve; });
    const mounted = await mountCockpit({ route: "game-settings", consent: "unresolved" });
    mounted.context.extensions.updateProfileOption.mockReturnValueOnce(pending);
    const feedback = mounted.root.querySelector(".feedback");
    mounted.root.querySelector("#player-reporting-enabled").click();
    await flush(1);
    expect(document.activeElement?.id).toBe("player-reporting-enabled");
    expect(mounted.root.querySelector(".feedback")).toBe(feedback);
    expect(mounted.context.host.announce).toHaveBeenCalledWith("reporting_decision_pending", expect.objectContaining({ message: expect.stringContaining("Saving") }));
    release();
    await flush();
    expect(document.activeElement?.id).toBe("player-reporting-enabled");
  });

  it.each([
    ["unresolved", "not_testable", true],
    ["disabled", "not_testable", true],
    ["unavailable", "unavailable", true],
    ["enabled", "unverified", true],
    ["enabled", "not_testable", false],
    ["enabled", "verified", false],
  ])("gates Auto Shutdown enablement for %s/%s", async (consent, verification, disabled) => {
    const mounted = await mountCockpit({ route: "auto-shutdown", consent, verification, states: { auto_shutdown: "off" } });
    expect(mounted.root.querySelector("#auto-toggle").disabled).toBe(disabled);
    expect(Boolean(mounted.root.querySelector("#review-player-reporting"))).toBe(disabled);
  });

  it("still permits disabling an already-enabled automation when reporting is blocked", async () => {
    const mounted = await mountCockpit({ route: "auto-shutdown", consent: "disabled" });
    expect(mounted.root.querySelector("#auto-toggle").disabled).toBe(false);
    expect(mounted.root.querySelector("#auto-toggle").textContent).toContain("Disable");
  });

  it("rejects empty and invalid Auto Shutdown timing without dispatching a provider action", async () => {
    const mounted = await mountCockpit({ route: "auto-shutdown" });
    const input = mounted.root.querySelector("#idle_shutdown_minutes");
    const save = mounted.root.querySelector('[data-number-action="idle_shutdown_minutes"]');
    input.value = "";
    save.click();
    expect(mounted.context.operations.entityAction).not.toHaveBeenCalled();
    expect(mounted.root.querySelector(".feedback").textContent).toContain("valid timing value");
    input.value = "0";
    save.click();
    expect(mounted.context.operations.entityAction).not.toHaveBeenCalled();
    input.value = "21";
    save.click();
    await flush();
    expect(mounted.context.operations.entityAction).toHaveBeenCalledWith("idle_shutdown_minutes", "set_value", 21);
  });

  it("shows no countdown while Auto Shutdown is inactive", async () => {
    const mounted = await mountCockpit({
      route: "auto-shutdown",
      providerStatus: "stopped",
      states: { auto_shutdown: "off", idle_time_remaining: "0.0" },
    });
    expect(mounted.root.textContent).toContain("Not counting down");
    expect(mounted.root.textContent).not.toContain("Idle time remaining0.0");
  });

  it.each(["Startup cooldown", "Waiting for zero players", "Paused", "Final checks"])(
    "shows no stale countdown during %s",
    async (status) => {
      const mounted = await mountCockpit({
        route: "auto-shutdown",
        providerStatus: "started",
        states: { auto_shutdown: "on", auto_shutdown_status: status, idle_time_remaining: "0.0" },
      });
      expect(mounted.root.textContent).toContain(`Current activity${status}`);
      expect(mounted.root.textContent).toContain("Idle time remainingNot counting down");
      expect(mounted.root.textContent).not.toContain("Idle time remaining0.0");
    },
  );

  it.each(["Starting countdown", "10.0 min remaining"])("shows remaining time only during %s", async (status) => {
    const mounted = await mountCockpit({
      route: "auto-shutdown",
      providerStatus: "started",
      states: { auto_shutdown: "on", auto_shutdown_status: status, idle_time_remaining: "10.0" },
    });
    expect(mounted.root.textContent).toContain("Idle time remaining10.0");
  });

  it("supports arrow-key tab navigation and publishes a clean navigation state before ready", async () => {
    const mounted = await mountCockpit();
    expect(mounted.context.host.publishNavigationState).toHaveBeenCalledWith({
      dirty: false,
      mutationPhase: "idle",
      messageCode: "clean",
    });
    const overview = mounted.root.querySelector("#palworld-tab-overview");
    overview.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
    await flush();
    expect(mounted.context.navigation.navigate).toHaveBeenCalledWith("auto-shutdown", { focus: "#palworld-tab-auto-shutdown" });
    expect(mounted.root.querySelector('[role="tab"][aria-selected="true"]')?.id).toBe("palworld-tab-auto-shutdown");
  });

  it("uses the cockpit-owned catalog and narrow selector with long pseudo-localized labels", async () => {
    const mounted = await mountCockpit({ narrow: true, locale: "en-XA" });
    expect(mounted.root.querySelector("#palworld-main").classList.contains("narrow")).toBe(true);
    expect(mounted.root.querySelector(".route-select span").textContent).toContain("Choose a Palworld server section");
    expect(mounted.root.querySelector('#palworld-route-select option[value="game-settings"]').textContent).toContain("Palworld game settings and player reporting");
  });

  it("uses calm acknowledged security and satisfied save-readiness treatments", async () => {
    const settings = await mountCockpit({ route: "game-settings", consent: "enabled", verification: "verified" });
    expect(settings.root.querySelector("#player-reporting-connection .security-note")).not.toBeNull();
    expect(settings.root.querySelector("#player-reporting-connection .error-notice")).toBeNull();
    expect(settings.root.querySelector("#player-reporting-connection .warning")).toBeNull();
    expect(settings.root.querySelector("#player-reporting-connection details")).not.toBeNull();

    const readySave = await mountCockpit({ route: "save-games", providerStatus: "stopped" });
    expect(readySave.root.querySelector("#save-bundle-card .success-note")).not.toBeNull();
    expect(readySave.root.querySelector("#save-bundle-card .warning")).toBeNull();
    const blockedSave = await mountCockpit({ route: "save-games", providerStatus: "started" });
    expect(blockedSave.root.querySelector("#save-bundle-card .warning")).not.toBeNull();
    expect(blockedSave.root.querySelector("#save-bundle-card .success-note")).toBeNull();
  });

  it("downloads and inspects save bundles while keeping restore behind the backend gate", async () => {
    const mounted = await mountCockpit({ route: "save-games" });
    expect(mounted.root.textContent).toContain("Save-game bundles");
    mounted.root.querySelector("#download-save-bundle").click();
    await flush();
    expect(mounted.context.extensions.downloadSaveBundle).toHaveBeenCalledWith("world", expect.any(Function));
    const input = mounted.root.querySelector("#save-bundle-upload");
    const file = new File(["zip"], "edited.zip", { type: "application/zip" });
    Object.defineProperty(input, "files", { value: [file], configurable: true });
    input.dispatchEvent(new Event("change", { bubbles: true }));
    await flush();
    expect(mounted.context.extensions.inspectSaveBundle).toHaveBeenCalledWith("world", file, expect.any(Function));
    expect(mounted.root.querySelector("#save-bundle-review").textContent).toContain("No implicit deletions");
    expect(mounted.root.textContent).toContain("read-only in this release");
    expect(mounted.root.querySelector("#apply-save-bundle").disabled).toBe(true);
  });

  it("keeps one uninterrupted save-package activity bar while swapping progress text", async () => {
    const mounted = await mountCockpit({ route: "save-games" });
    let finish;
    let publishProgress;
    mounted.context.extensions.downloadSaveBundle.mockImplementation((_key, onProgress) => new Promise((resolve) => {
      finish = () => resolve({ filename: "palworld-save.zip", bytes: 1234 });
      publishProgress = onProgress;
    }));
    const operationArea = mounted.root.querySelector("#save-bundle-progress");
    const activityBar = mounted.root.querySelector("#save-bundle-progress-bar");
    expect(operationArea.hidden).toBe(true);
    mounted.root.querySelector("#download-save-bundle").click();
    await flush();
    expect(mounted.root.querySelector("#save-bundle-progress")).toBe(operationArea);
    expect(mounted.root.querySelector("#save-bundle-progress-bar")).toBe(activityBar);
    expect(operationArea.hidden).toBe(false);
    expect(mounted.root.querySelector("#save-bundle-card").getAttribute("aria-busy")).toBe("true");
    expect(activityBar.hasAttribute("value")).toBe(false);
    expect(mounted.root.querySelector("#save-bundle-progress-detail").textContent).toBe("Processing…");
    publishProgress({
      stage: "scanning",
      message: "Scanning the live save tree…",
      files_done: 1,
      files_total: 7,
    });
    expect(mounted.root.querySelector("#save-bundle-progress-bar")).toBe(activityBar);
    expect(activityBar.hasAttribute("value")).toBe(false);
    mounted.controller.render();
    expect(mounted.root.querySelector("#save-bundle-progress")).toBe(operationArea);
    expect(mounted.root.querySelector("#save-bundle-progress-bar")).toBe(activityBar);
    await mounted.update({ ...snapshot(), navigation: { current_route: "save-games" } });
    expect(mounted.root.querySelector("#save-bundle-progress")).toBe(operationArea);
    expect(mounted.root.querySelector("#save-bundle-progress-bar")).toBe(activityBar);
    publishProgress({ stage: "connecting", message: "Connecting securely to the save storage…" });
    expect(mounted.root.querySelector("#save-bundle-progress-bar")).toBe(activityBar);
    expect(activityBar.hasAttribute("value")).toBe(false);
    expect(mounted.root.querySelector("#save-bundle-progress-detail").textContent).toBe("Processing…");
    publishProgress({
      stage: "downloading",
      message: "Downloading live save files — 3 complete, 2.0 KiB read…",
      files_done: 3,
      files_total: 7,
      bytes_done: 2048,
      bytes_total: 4096,
      current_file: "Players/abc.sav",
      elapsed_seconds: 7,
    });
    const announcement = mounted.root.querySelector("#save-bundle-announcement");
    const announcedMilestone = announcement.textContent;
    publishProgress({
      stage: "downloading",
      message: "Downloading live save files — still working…",
      files_done: 3,
      files_total: 7,
      bytes_done: 2200,
      bytes_total: 4096,
      current_file: "Players/another.sav",
      elapsed_seconds: 9,
    });
    expect(announcement.textContent).toBe(announcedMilestone);
    expect(mounted.context.host.announce).not.toHaveBeenCalled();
    expect(mounted.root.querySelector("#save-bundle-progress-bar")).toBe(activityBar);
    expect(activityBar.hasAttribute("value")).toBe(false);
    expect(mounted.root.querySelector("#save-bundle-progress-message").textContent).toContain("Downloading live save files");
    expect(mounted.root.querySelector("#save-bundle-progress-detail").textContent).toContain("3 of 7 files");
    expect(mounted.root.querySelector("#save-bundle-progress-detail").textContent).toContain("2.15 KiB of 4.00 KiB");
    expect(mounted.root.querySelector("#save-bundle-progress-detail").textContent).toContain("Players/another.sav");
    expect(mounted.root.textContent).toContain("backup/ history, provider paths");
    finish();
    await flush();
    expect(mounted.root.querySelector("#save-bundle-progress").hidden).toBe(true);
    expect(mounted.root.querySelector("#save-bundle-card").getAttribute("aria-busy")).toBe("false");
    expect(mounted.root.querySelector("#save-bundle-announcement").textContent.toLowerCase()).toContain("downloaded");
  });

  it("applies only the exact reviewed save token when the backend gate is enabled", async () => {
    const mounted = await mountCockpit({ route: "save-games", saveMutations: true });
    const input = mounted.root.querySelector("#save-bundle-upload");
    const file = new File(["zip"], "edited.zip", { type: "application/zip" });
    Object.defineProperty(input, "files", { value: [file], configurable: true });
    input.dispatchEvent(new Event("change", { bubbles: true }));
    await flush();
    window.confirm = vi.fn(() => true);
    mounted.root.querySelector("#apply-save-bundle").click();
    await flush();
    expect(mounted.context.extensions.applySaveBundle).toHaveBeenCalledWith("world", "save-preview-token");
    expect(mounted.root.textContent).toContain("Audit op-save");
  });

  it("requires a freshly stopped server before save download or upload", async () => {
    const mounted = await mountCockpit({ route: "save-games", providerStatus: "started" });
    expect(mounted.root.textContent).toContain("Stop Palworld first");
    expect(mounted.root.querySelector("#download-save-bundle").disabled).toBe(true);
    expect(mounted.root.querySelector("#save-bundle-upload").disabled).toBe(true);
  });

  it("keeps focus on a mutation control while reporting pending state", async () => {
    const mounted = await mountCockpit({ route: "auto-shutdown" });
    const action = mounted.root.querySelector("#auto-toggle");
    action.focus();
    action.click();
    await flush(1);
    expect(document.activeElement?.id).toBe("auto-toggle");
    await flush();
    expect(mounted.context.operations.entityAction).toHaveBeenCalledWith("auto_shutdown", "turn_off", undefined);
  });

  it("opens a structured secret-safe settings workspace and keeps live writes gated", async () => {
    const mounted = await mountCockpit({ route: "game-settings" });
    expect(mounted.root.textContent).toContain("Palworld server settings");
    expect(mounted.root.textContent).toContain("read-only in this release");
    mounted.root.querySelector("#open-settings-editor").click();
    await flush();
    const dialog = mounted.root.querySelector("#palworld-settings-editor");
    expect(dialog?.open).toBe(true);
    expect(mounted.context.extensions.readEditableFile).toHaveBeenCalledWith("settings");
    expect(mounted.context.extensions.editableFileHistory).toHaveBeenCalledWith("settings");
    expect(mounted.root.querySelector("#setting-ServerName").value).toBe("Test");
    expect(mounted.root.querySelector("#setting-AdminPassword")).toBeNull();
    expect(mounted.root.textContent).not.toContain("admin-secret");
    const serverName = mounted.root.querySelector("#setting-ServerName");
    serverName.value = "Changed";
    serverName.dispatchEvent(new Event("change", { bubbles: true }));
    expect((await mounted.controller.navigationGuard()).decision).toBe("prompt");
    mounted.root.querySelector("#editor-preview").click();
    await flush();
    expect(mounted.context.extensions.previewEditableFile).toHaveBeenCalledWith("settings", {
      sourceRevision: "source-revision",
      operations: [{ op: "set", key: "ServerName", raw_value: '"Changed"' }],
    });
    expect(mounted.root.textContent).toContain("Valid · Changed · Restart required");
    expect(mounted.root.querySelector("#editor-apply").disabled).toBe(true);
  });

  it("allows inspection while Palworld runs but blocks preview until it is stopped", async () => {
    const mounted = await mountCockpit({ route: "game-settings", providerStatus: "started" });
    expect(mounted.root.querySelector("#open-settings-editor").disabled).toBe(false);
    mounted.root.querySelector("#open-settings-editor").click();
    await flush();
    const name = mounted.root.querySelector("#setting-ServerName");
    name.value = "Running draft";
    name.dispatchEvent(new Event("change", { bubbles: true }));
    expect(mounted.root.textContent).toContain("Stop Palworld before previewing or applying changes");
    expect(mounted.root.querySelector("#editor-preview").disabled).toBe(true);
  });

  it("enables Apply only for the exact valid preview when the backend gate is enabled", async () => {
    const mounted = await mountCockpit({ route: "game-settings", editorMutations: true });
    mounted.root.querySelector("#open-settings-editor").click();
    await flush();
    const exp = mounted.root.querySelector("#setting-ExpRate");
    exp.value = "2.5";
    exp.dispatchEvent(new Event("change", { bubbles: true }));
    mounted.root.querySelector("#editor-preview").click();
    await flush();
    expect(mounted.root.querySelector("#editor-apply").disabled).toBe(false);
    const updatedExp = mounted.root.querySelector("#setting-ExpRate");
    updatedExp.value = "3";
    updatedExp.dispatchEvent(new Event("change", { bubbles: true }));
    expect(mounted.root.querySelector("#editor-apply").disabled).toBe(true);
  });

  it("searches across categories and edits unknown future settings without discarding them", async () => {
    const mounted = await mountCockpit({ route: "game-settings" });
    mounted.root.querySelector("#open-settings-editor").click();
    await flush();
    const search = mounted.root.querySelector("#settings-search");
    search.value = "FutureKey";
    search.dispatchEvent(new Event("input", { bubbles: true }));
    expect(mounted.root.querySelector("#setting-FutureKey").value).toBe("(A=1,B=2)");
    const future = mounted.root.querySelector("#setting-FutureKey");
    future.value = "(A=2,B=3)";
    future.dispatchEvent(new Event("change", { bubbles: true }));
    expect(mounted.root.textContent).toContain("1 unsaved change");
    mounted.root.querySelector('[data-editor-scope="changed"]').click();
    expect(mounted.root.querySelector("#setting-FutureKey")).not.toBeNull();
    expect(mounted.root.querySelector("#setting-ServerName")).toBeNull();
  });

  it("keeps All Settings permanently available and narrows the same search by category", async () => {
    const mounted = await mountCockpit({ route: "game-settings" });
    mounted.root.querySelector("#open-settings-editor").click();
    await flush();

    const allSettings = mounted.root.querySelector('[data-editor-category="all"]');
    expect(allSettings).not.toBeNull();
    expect(mounted.root.querySelector('[data-editor-category="favorites"]').getAttribute("aria-current")).toBe("true");

    mounted.root.querySelector('[data-editor-category="server"]').click();
    const search = mounted.root.querySelector("#settings-search");
    search.value = "FutureKey";
    search.dispatchEvent(new Event("input", { bubbles: true }));
    expect(mounted.root.querySelector('[data-editor-category="all"]').getAttribute("aria-current")).toBe("true");
    expect(mounted.root.querySelector("#editor-filter-status").textContent).toBe('All Settings. 1 setting shown matching “FutureKey”.');
    expect(mounted.root.querySelector("#setting-FutureKey")).not.toBeNull();

    mounted.root.querySelector('[data-editor-category="other"]').click();
    expect(search.value).toBe("FutureKey");
    expect(mounted.root.querySelector('[data-editor-category="other"]').getAttribute("aria-current")).toBe("true");
    expect(mounted.root.querySelector("#editor-filter-status").textContent).toBe('Other & new settings. 1 setting shown matching “FutureKey”.');
    expect(mounted.root.querySelector("#setting-FutureKey")).not.toBeNull();
    expect(mounted.root.querySelector("#setting-ServerName")).toBeNull();
  });

  it("shows changed settings from every category before allowing category narrowing", async () => {
    const mounted = await mountCockpit({ route: "game-settings" });
    mounted.root.querySelector("#open-settings-editor").click();
    await flush();

    mounted.root.querySelector('[data-editor-category="server"]').click();
    const serverName = mounted.root.querySelector("#setting-ServerName");
    serverName.value = "Changed across categories";
    serverName.dispatchEvent(new Event("change", { bubbles: true }));

    mounted.root.querySelector('[data-editor-category="other"]').click();
    const future = mounted.root.querySelector("#setting-FutureKey");
    future.value = "(A=9,B=9)";
    future.dispatchEvent(new Event("change", { bubbles: true }));

    mounted.root.querySelector('[data-editor-scope="changed"]').click();
    expect(mounted.root.querySelector('[data-editor-category="all"]').getAttribute("aria-current")).toBe("true");
    expect(mounted.root.querySelector("#setting-ServerName")).not.toBeNull();
    expect(mounted.root.querySelector("#setting-FutureKey")).not.toBeNull();
    expect(mounted.root.querySelector("#setting-ExpRate")).toBeNull();

    mounted.root.querySelector('[data-editor-category="server"]').click();
    expect(mounted.root.querySelector("#setting-ServerName")).not.toBeNull();
    expect(mounted.root.querySelector("#setting-FutureKey")).toBeNull();
    expect(mounted.root.querySelector('[data-editor-scope="changed"]').getAttribute("aria-pressed")).toBe("true");
  });

  it("does not accumulate filter listeners or strand recovery actions after live search", async () => {
    const mounted = await mountCockpit({
      route: "game-settings",
      editorMutations: true,
      history: [{ recovery_id: "recovery-1", backup_kind: "apply", created_at: 1, operation_id: "op-1", available: true }],
    });
    mounted.root.querySelector("#open-settings-editor").click();
    await flush();
    const rerender = vi.spyOn(mounted.controller, "_renderEditorResults");
    const search = mounted.root.querySelector("#settings-search");
    for (const value of ["F", "Fu", "Fut", "FutureKey"]) {
      search.value = value;
      search.dispatchEvent(new Event("input", { bubbles: true }));
    }
    expect(rerender).toHaveBeenCalledTimes(4);
    mounted.root.querySelector('[data-editor-scope="changed"]').click();
    expect(rerender).toHaveBeenCalledTimes(5);

    search.value = "";
    search.dispatchEvent(new Event("input", { bubbles: true }));
    window.confirm = vi.fn(() => true);
    mounted.root.querySelector("[data-recovery-id=recovery-1]").click();
    await flush();
    expect(mounted.context.extensions.rollbackEditableFile).toHaveBeenCalledWith("settings", "recovery-1");
  });

  it("preserves the typed control DOM, focus, caret, and native undo surface on ordinary changes", async () => {
    const mounted = await mountCockpit({ route: "game-settings" });
    mounted.root.querySelector("#open-settings-editor").click();
    await flush();
    const serverName = mounted.root.querySelector("#setting-ServerName");
    serverName.focus();
    serverName.value = "Keyboard flow";
    serverName.setSelectionRange(4, 8);
    serverName.dispatchEvent(new Event("change", { bubbles: true }));
    expect(mounted.root.querySelector("#setting-ServerName")).toBe(serverName);
    expect(document.activeElement).toBe(serverName);
    expect(serverName.selectionStart).toBe(4);
    expect(serverName.selectionEnd).toBe(8);
    expect(mounted.root.textContent).toContain("Saved → proposed:");
  });

  it("keeps category keyboard focus on the selected category", async () => {
    const mounted = await mountCockpit({ route: "game-settings" });
    mounted.root.querySelector("#open-settings-editor").click();
    await flush();
    mounted.root.querySelector('[data-editor-category="server"]').click();
    expect(document.activeElement?.id).toBe("editor-category-server");
  });

  it("uses explicit keep replace clear secret controls and never prefills stored secrets", async () => {
    const mounted = await mountCockpit({ route: "game-settings" });
    mounted.root.querySelector("#open-settings-editor").click();
    await flush();
    mounted.root.querySelector('[data-editor-category="server"]').click();
    const action = mounted.root.querySelector("#setting-AdminPassword-action");
    expect(action.value).toBe("keep");
    expect(mounted.root.querySelector("#setting-AdminPassword")).toBeNull();
    action.value = "replace";
    action.dispatchEvent(new Event("change", { bubbles: true }));
    const replacement = mounted.root.querySelector("#setting-AdminPassword");
    expect(replacement.type).toBe("password");
    expect(replacement.value).toBe("");
    replacement.value = "new,secret";
    replacement.dispatchEvent(new Event("input", { bubbles: true }));
    expect(replacement.getAttribute("value")).toBeNull();
    expect(mounted.controller._editorField("AdminPassword").draftRaw).toBeNull();
    mounted.root.querySelector("#editor-preview").click();
    await flush();
    expect(mounted.context.extensions.previewEditableFile).toHaveBeenCalledWith("settings", {
      sourceRevision: "source-revision",
      operations: [{ op: "set", key: "AdminPassword", raw_value: '"new,secret"' }],
    });
    expect(mounted.root.textContent).not.toContain("new,secret");
    expect(mounted.root.querySelector("#setting-AdminPassword")).toBe(replacement);
    expect(replacement.value).toBe("new,secret");
  });

  it("keeps replacement secrets transiently across filters without serializing them", async () => {
    const mounted = await mountCockpit({ route: "game-settings" });
    mounted.root.querySelector("#open-settings-editor").click();
    await flush();
    mounted.root.querySelector('[data-editor-category="server"]').click();
    const action = mounted.root.querySelector("#setting-AdminPassword-action");
    action.value = "replace";
    action.dispatchEvent(new Event("change", { bubbles: true }));
    const replacement = mounted.root.querySelector("#setting-AdminPassword");
    replacement.value = "secret survives navigation";
    replacement.dispatchEvent(new Event("input", { bubbles: true }));

    mounted.root.querySelector('[data-editor-scope="changed"]').click();
    const restored = mounted.root.querySelector("#setting-AdminPassword");
    expect(restored).not.toBe(replacement);
    expect(restored.value).toBe("secret survives navigation");
    expect(restored.getAttribute("value")).toBeNull();
    expect(mounted.root.querySelector("#palworld-settings-editor").innerHTML).not.toContain("secret survives navigation");
  });

  it("recovers only revision-bound non-sensitive drafts and discloses omitted secrets", async () => {
    const first = await mountCockpit({ route: "game-settings" });
    first.root.querySelector("#open-settings-editor").click();
    await flush();
    first.root.querySelector('[data-editor-category="server"]').click();
    const name = first.root.querySelector("#setting-ServerName");
    name.value = "Recovered Palbox";
    name.dispatchEvent(new Event("change", { bubbles: true }));
    const action = first.root.querySelector("#setting-AdminPassword-action");
    action.value = "replace";
    action.dispatchEvent(new Event("change", { bubbles: true }));
    const secret = first.root.querySelector("#setting-AdminPassword");
    secret.value = "never serialize me";
    secret.dispatchEvent(new Event("input", { bubbles: true }));
    const serialized = [...Array(sessionStorage.length)].map((_, index) => sessionStorage.getItem(sessionStorage.key(index))).join("\n");
    expect(serialized).toContain("Recovered Palbox");
    expect(serialized).not.toContain("never serialize me");
    expect(serialized).not.toContain("AdminPassword");
    expect(serialized).not.toContain("preview-token");
    await first.controller.dispose();

    const second = await mountCockpit({ route: "game-settings" });
    second.root.querySelector("#open-settings-editor").click();
    await flush();
    expect(second.root.querySelector("#setting-ServerName").value).toBe("Recovered Palbox");
    expect(second.root.querySelector("#setting-AdminPassword-action").value).toBe("keep");
    expect(second.root.textContent).toContain("Sensitive or no-longer-safe fields were intentionally omitted");
  });

  it("isolates draft recovery by service and clears it on Undo all", async () => {
    const first = await mountCockpit({ route: "game-settings", identityOverrides: { service_id: "service-a" } });
    first.root.querySelector("#open-settings-editor").click();
    await flush();
    const field = first.root.querySelector("#setting-ServerName");
    field.value = "Service A draft";
    field.dispatchEvent(new Event("change", { bubbles: true }));
    expect(sessionStorage.length).toBe(1);
    await first.controller.dispose();

    const other = await mountCockpit({ route: "game-settings", identityOverrides: { service_id: "service-b" } });
    other.root.querySelector("#open-settings-editor").click();
    await flush();
    expect(other.root.querySelector("#setting-ServerName").value).toBe("Test");
    await other.controller.dispose();

    const restored = await mountCockpit({ route: "game-settings", identityOverrides: { service_id: "service-a" } });
    restored.root.querySelector("#open-settings-editor").click();
    await flush();
    expect(restored.root.querySelector("#setting-ServerName").value).toBe("Service A draft");
    restored.root.querySelector("#editor-undo-all").click();
    await flush();
    expect(sessionStorage.length).toBe(0);
    expect(restored.root.querySelector("#setting-ServerName").value).toBe("Test");
  });

  it("discards both stored and in-memory drafts before navigation", async () => {
    const mounted = await mountCockpit({ route: "game-settings" });
    mounted.root.querySelector("#open-settings-editor").click();
    await flush();
    const field = mounted.root.querySelector("#setting-ServerName");
    field.value = "Must be discarded";
    field.dispatchEvent(new Event("change", { bubbles: true }));
    expect(sessionStorage.length).toBe(1);
    expect(mounted.controller.editor.dirty).toBe(true);

    await mounted.controller.discardPendingNavigation();

    expect(sessionStorage.length).toBe(0);
    expect(mounted.controller.editor).toBeNull();
    expect((await mounted.controller.navigationGuard()).decision).toBe("allow");
    mounted.controller.render();
    mounted.root.querySelector("#open-settings-editor").click();
    await flush();
    expect(mounted.root.querySelector("#setting-ServerName").value).toBe("Test");
  });

  it("discards malformed recovery and refuses a field reclassified as sensitive", async () => {
    const seed = await mountCockpit({ route: "game-settings" });
    seed.root.querySelector("#open-settings-editor").click();
    await flush();
    const key = seed.controller._draftStorageKey();
    await seed.controller.dispose();
    sessionStorage.setItem(key, "not-json");
    const malformed = await mountCockpit({ route: "game-settings" });
    malformed.root.querySelector("#open-settings-editor").click();
    await flush();
    expect(malformed.root.textContent).toContain("invalid saved draft was discarded safely");
    await malformed.controller.dispose();

    sessionStorage.setItem(key, JSON.stringify({
      version: 1,
      createdAt: Date.now(),
      schemaRevision: "palworld-1.0-2026-08",
      sourceRevision: "source-revision",
      changes: [{ key: "ServerName", draftRaw: '"must not restore"' }],
      omittedSensitive: false,
    }));
    const reclassified = await mountCockpit({ route: "game-settings" });
    reclassified.context.extensions.readEditableFile.mockResolvedValueOnce({ file: {
      revision: "source-revision",
      model: {
        schema_revision: "palworld-1.0-2026-08",
        redacted_source: "ServerName=[redacted]",
        settings: [{ key: "ServerName", raw_value: null, sensitive: true, configured: true }],
      },
    } });
    reclassified.root.querySelector("#open-settings-editor").click();
    await flush();
    expect(reclassified.root.querySelector("#setting-ServerName")).toBeNull();
    expect(reclassified.root.querySelector("#setting-ServerName-action").value).toBe("keep");
    expect(reclassified.root.textContent).toContain("Recovery was partial");
    expect(reclassified.root.textContent).not.toContain("must not restore");
  });

  it("installs the unload guard only while the editor is dirty", async () => {
    const mounted = await mountCockpit({ route: "game-settings" });
    mounted.root.querySelector("#open-settings-editor").click();
    await flush();
    const cleanEvent = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(cleanEvent);
    expect(cleanEvent.defaultPrevented).toBe(false);
    const field = mounted.root.querySelector("#setting-ServerName");
    field.value = "Dirty";
    field.dispatchEvent(new Event("change", { bubbles: true }));
    const dirtyEvent = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(dirtyEvent);
    expect(dirtyEvent.defaultPrevented).toBe(true);
    mounted.root.querySelector("#editor-undo-all").click();
    await flush();
    const clearedEvent = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(clearedEvent);
    expect(clearedEvent.defaultPrevented).toBe(false);
  });

  it("revalidates the draft epoch after Apply confirmation", async () => {
    const mounted = await mountCockpit({ route: "game-settings", editorMutations: true });
    mounted.root.querySelector("#open-settings-editor").click();
    await flush();
    const name = mounted.root.querySelector("#setting-ServerName");
    name.value = "Reviewed";
    name.dispatchEvent(new Event("change", { bubbles: true }));
    mounted.root.querySelector("#editor-preview").click();
    await flush();
    let decide;
    mounted.context.host.confirm.mockImplementationOnce(() => new Promise((resolve) => { decide = resolve; }));
    mounted.root.querySelector("#editor-apply").click();
    const exp = mounted.root.querySelector("#setting-ExpRate");
    exp.value = "2";
    exp.dispatchEvent(new Event("change", { bubbles: true }));
    decide(true);
    await flush();
    expect(mounted.context.extensions.applyEditableFile).not.toHaveBeenCalled();
  });

  it("abandons settings reads and previews when the cockpit is disposed mid-await", async () => {
    let resolveRead;
    const read = await mountCockpit({ route: "game-settings" });
    read.context.extensions.readEditableFile.mockImplementationOnce(() => new Promise((resolve) => { resolveRead = resolve; }));
    read.root.querySelector("#open-settings-editor").click();
    await flush(1);
    const pendingReadEditor = read.controller.editor;
    await read.controller.dispose();
    resolveRead({ file: { revision: "late", model: { schema_revision: "late", redacted_source: "", settings: [] } } });
    await flush();
    expect(pendingReadEditor.loading).toBe(true);
    expect(read.context.host.reportFatal).not.toHaveBeenCalled();

    let resolvePreview;
    const preview = await mountCockpit({ route: "game-settings" });
    preview.root.querySelector("#open-settings-editor").click();
    await flush();
    const field = preview.root.querySelector("#setting-ServerName");
    field.value = "Late preview";
    field.dispatchEvent(new Event("change", { bubbles: true }));
    preview.context.extensions.previewEditableFile.mockImplementationOnce(() => new Promise((resolve) => { resolvePreview = resolve; }));
    preview.root.querySelector("#editor-preview").click();
    await flush(1);
    const pendingPreviewEditor = preview.controller.editor;
    await preview.controller.dispose();
    resolvePreview({ preview_token: "late-token", preview: { verdict: { state: "supported" }, changed: true } });
    await flush();
    expect(pendingPreviewEditor.preview).toBeNull();
    expect(pendingPreviewEditor.previewToken).toBeNull();
  });

  it("abandons save, reporting refresh, and clipboard continuations after disposal", async () => {
    let resolveSave;
    const save = await mountCockpit({ route: "save-games" });
    save.context.extensions.downloadSaveBundle.mockImplementationOnce(() => new Promise((resolve) => { resolveSave = resolve; }));
    save.root.querySelector("#download-save-bundle").click();
    await flush(1);
    const pendingResult = { ...save.controller.saveBundleResult };
    await save.controller.dispose();
    resolveSave({ filename: "late.zip" });
    await flush();
    expect(save.controller.saveBundleResult).toEqual(pendingResult);

    let resolveReporting;
    const reportingMount = await mountCockpit({ route: "game-settings", consent: "unresolved" });
    reportingMount.context.extensions.updateProfileOption.mockImplementationOnce(() => new Promise((resolve) => { resolveReporting = resolve; }));
    reportingMount.root.querySelector("#player-reporting-enabled").click();
    await flush(1);
    await reportingMount.controller.dispose();
    resolveReporting();
    await flush();
    expect(reportingMount.context.operations.refreshSnapshot).not.toHaveBeenCalled();

    let resolveClipboard;
    const clipboard = await mountCockpit({ route: "overview" });
    Object.defineProperty(globalThis.navigator, "clipboard", {
      configurable: true,
      value: { writeText: vi.fn(() => new Promise((resolve) => { resolveClipboard = resolve; })) },
    });
    const result = clipboard.root.querySelector("#copy-address-result");
    clipboard.root.querySelector("#copy-address").click();
    await flush(1);
    await clipboard.controller.dispose();
    resolveClipboard();
    await flush();
    expect(result.textContent).toBe("");
  });

  it("keeps setting-specific names and dynamic descriptions for every control shape", async () => {
    const mounted = await mountCockpit({ route: "game-settings" });
    const original = await mounted.context.extensions.readEditableFile();
    original.file.model.settings.push(
      { key: "DeathPenalty", raw_value: "Item", sensitive: false, configured: null },
      { key: "BaseCampWorkerMaxNum", raw_value: "15", sensitive: false, configured: null },
    );
    mounted.context.extensions.readEditableFile.mockResolvedValueOnce(original);
    mounted.root.querySelector("#open-settings-editor").click();
    await flush();
    for (const id of ["setting-ServerName", "setting-ExpRate", "setting-bIsPvP", "setting-DeathPenalty", "setting-AdminPassword-action"]) {
      const control = mounted.root.querySelector(`#${id}`);
      const key = id.replace(/^setting-/, "").replace(/-action$/, "");
      expect(control.getAttribute("aria-labelledby")).toContain(`label-${key}`);
      expect(control.getAttribute("aria-describedby")).toContain(`help-${key}`);
    }
    const workers = mounted.root.querySelector("#setting-BaseCampWorkerMaxNum");
    workers.value = "100";
    workers.dispatchEvent(new Event("change", { bubbles: true }));
    expect(workers.getAttribute("aria-describedby")).toContain("issue-BaseCampWorkerMaxNum");
    expect(workers.getAttribute("aria-describedby")).toContain("help-BaseCampWorkerMaxNum");
    workers.value = "20";
    workers.dispatchEvent(new Event("change", { bubbles: true }));
    expect(workers.getAttribute("aria-describedby")).not.toContain("issue-BaseCampWorkerMaxNum");
    expect(workers.getAttribute("aria-describedby")).toContain("help-BaseCampWorkerMaxNum");
  });

  it("preserves the editor dialog, body, status, and scroll node through preview and model replacement", async () => {
    const mounted = await mountCockpit({ route: "game-settings", editorMutations: true, narrow: true });
    mounted.root.querySelector("#open-settings-editor").click();
    await flush();
    const dialog = mounted.root.querySelector("#palworld-settings-editor");
    const body = mounted.root.querySelector(".editor-body");
    const status = mounted.root.querySelector("#editor-operation-status");
    const workspace = mounted.root.querySelector(".editor-workspace");
    const content = mounted.root.querySelector(".editor-content");
    workspace.scrollTop = 29;
    content.scrollTop = 41;
    const name = mounted.root.querySelector("#setting-ServerName");
    name.value = "Stable shell";
    name.dispatchEvent(new Event("change", { bubbles: true }));
    mounted.root.querySelector("#editor-preview").click();
    await flush();
    expect(mounted.root.querySelector("#palworld-settings-editor")).toBe(dialog);
    expect(mounted.root.querySelector(".editor-body")).toBe(body);
    expect(mounted.root.querySelector("#editor-operation-status")).toBe(status);
    expect(mounted.root.querySelector(".editor-workspace").scrollTop).toBe(29);
    expect(mounted.root.querySelector(".editor-content").scrollTop).toBe(41);
    mounted.root.querySelector("#editor-apply").click();
    await flush();
    expect(mounted.root.querySelector("#palworld-settings-editor")).toBe(dialog);
    expect(mounted.root.querySelector(".editor-body")).toBe(body);
    expect(mounted.root.querySelector("#editor-operation-status")).toBe(status);
    expect(status.textContent).toContain("Settings applied");
  });

  it("preserves open editor controls on coordinator updates and blocks stale Apply", async () => {
    const mounted = await mountCockpit({ route: "game-settings", editorMutations: true });
    mounted.root.querySelector("#open-settings-editor").click();
    await flush();
    const exp = mounted.root.querySelector("#setting-ExpRate");
    exp.value = "2.5";
    exp.dispatchEvent(new Event("change", { bubbles: true }));
    mounted.root.querySelector("#editor-preview").click();
    await flush();
    expect(mounted.root.querySelector("#editor-apply").disabled).toBe(false);

    await mounted.update({
      ...mounted.controller.snapshot,
      provider: { ...mounted.controller.snapshot.provider, status: "started" },
    });

    expect(mounted.root.querySelector("#setting-ExpRate")).toBe(exp);
    expect(exp.value).toBe("2.5");
    expect(mounted.root.querySelector("#editor-apply").disabled).toBe(true);
    expect(mounted.root.querySelector("#editor-apply-reason").textContent).toContain("Stop Palworld");
  });

  it("preserves a still-visible filtered control after editing", async () => {
    const mounted = await mountCockpit({ route: "game-settings" });
    mounted.root.querySelector("#open-settings-editor").click();
    await flush();
    const serverName = mounted.root.querySelector("#setting-ServerName");
    serverName.value = "Filtered draft";
    serverName.dispatchEvent(new Event("change", { bubbles: true }));
    mounted.root.querySelector('[data-editor-scope="changed"]').click();
    const filteredName = mounted.root.querySelector("#setting-ServerName");
    filteredName.focus();
    filteredName.value = "Filtered draft updated";
    filteredName.setSelectionRange(9, 14);
    filteredName.dispatchEvent(new Event("change", { bubbles: true }));
    expect(mounted.root.querySelector("#setting-ServerName")).toBe(filteredName);
    expect(document.activeElement).toBe(filteredName);
    expect(filteredName.selectionStart).toBe(9);
    expect(filteredName.selectionEnd).toBe(14);
  });

  it("moves focus when a direct edit removes its Changed or Issues card", async () => {
    const changed = await mountCockpit({ route: "game-settings" });
    changed.root.querySelector("#open-settings-editor").click();
    await flush();
    const name = changed.root.querySelector("#setting-ServerName");
    name.value = "Changed";
    name.dispatchEvent(new Event("change", { bubbles: true }));
    changed.root.querySelector('[data-editor-scope="changed"]').click();
    const filteredName = changed.root.querySelector("#setting-ServerName");
    filteredName.focus();
    filteredName.value = "Test";
    filteredName.dispatchEvent(new Event("change", { bubbles: true }));
    expect(document.activeElement?.id).toBe("editor-category-title");

    const issues = await mountCockpit({ route: "game-settings" });
    issues.root.querySelector("#open-settings-editor").click();
    await flush();
    const pvp = issues.root.querySelector("#setting-bIsPvP");
    pvp.checked = true;
    pvp.dispatchEvent(new Event("change", { bubbles: true }));
    issues.root.querySelector('[data-editor-scope="issues"]').click();
    const filteredPvp = issues.root.querySelector("#setting-bIsPvP");
    filteredPvp.focus();
    filteredPvp.checked = false;
    filteredPvp.dispatchEvent(new Event("change", { bubbles: true }));
    expect(document.activeElement?.id).toBe("editor-category-title");
  });

  it("moves focus when Fix PvP requirements resolves the active Issues card", async () => {
    const mounted = await mountCockpit({ route: "game-settings" });
    mounted.root.querySelector("#open-settings-editor").click();
    await flush();
    const pvp = mounted.root.querySelector("#setting-bIsPvP");
    pvp.checked = true;
    pvp.dispatchEvent(new Event("change", { bubbles: true }));
    mounted.root.querySelector('[data-editor-scope="issues"]').click();
    const fix = mounted.root.querySelector("#fix-pvp-requirements");
    fix.focus();
    fix.click();
    expect(document.activeElement?.id).toBe("editor-category-title");
    expect(mounted.root.querySelector("#fix-pvp-requirements")).toBeNull();
  });

  it("keeps one announcement owner for editor outcomes", async () => {
    const mounted = await mountCockpit({ route: "game-settings", editorMutations: true });
    mounted.root.querySelector("#open-settings-editor").click();
    await flush();
    const name = mounted.root.querySelector("#setting-ServerName");
    name.value = "One announcer";
    name.dispatchEvent(new Event("change", { bubbles: true }));
    mounted.root.querySelector("#editor-preview").click();
    await flush();
    expect(mounted.root.querySelector("#editor-operation-status").getAttribute("aria-live")).toBe("polite");
    expect(mounted.root.querySelector("#editor-preview-status").getAttribute("aria-live")).toBe("off");
    mounted.context.host.announce.mockClear();
    mounted.root.querySelector("#editor-apply").click();
    await flush();
    expect(mounted.context.host.announce).not.toHaveBeenCalled();
    expect(document.activeElement?.id).toBe("editor-close");
  });

  it.each([
    ["first", "ServerName", "setting-ExpRate"],
    ["middle", "ExpRate", "setting-FutureKey"],
    ["last", "FutureKey", "setting-ExpRate"],
  ])("moves focus to a stable %s Changed result when Undo removes the active card", async (_position, undoKey, expectedId) => {
    const mounted = await mountCockpit({ route: "game-settings" });
    mounted.root.querySelector("#open-settings-editor").click();
    await flush();
    mounted.root.querySelector('[data-editor-category="all"]').click();
    for (const [key, value] of [["ServerName", "Changed"], ["ExpRate", "2.5"], ["FutureKey", "(A=2,B=3)"]]) {
      const input = mounted.root.querySelector(`#setting-${key}`);
      input.value = value;
      input.dispatchEvent(new Event("change", { bubbles: true }));
    }
    mounted.root.querySelector('[data-editor-scope="changed"]').click();
    mounted.root.querySelector(`[data-undo-setting="${undoKey}"]`).click();
    expect(document.activeElement?.id).toBe(expectedId);
  });

  it("focuses the results heading when the only Changed card or all Changed cards disappear", async () => {
    const only = await mountCockpit({ route: "game-settings" });
    only.root.querySelector("#open-settings-editor").click();
    await flush();
    const onlyInput = only.root.querySelector("#setting-ServerName");
    onlyInput.value = "Only change";
    onlyInput.dispatchEvent(new Event("change", { bubbles: true }));
    only.root.querySelector('[data-editor-scope="changed"]').click();
    only.root.querySelector('[data-undo-setting="ServerName"]').click();
    expect(document.activeElement?.id).toBe("editor-category-title");

    const multiple = await mountCockpit({ route: "game-settings" });
    multiple.root.querySelector("#open-settings-editor").click();
    await flush();
    for (const [key, value] of [["ServerName", "Multiple"], ["ExpRate", "2.5"]]) {
      const input = multiple.root.querySelector(`#setting-${key}`);
      input.value = value;
      input.dispatchEvent(new Event("change", { bubbles: true }));
    }
    multiple.root.querySelector('[data-editor-scope="changed"]').click();
    multiple.root.querySelector("#editor-undo-all").click();
    await flush();
    expect(multiple.context.host.confirm).toHaveBeenCalledWith(expect.objectContaining({ destructive: true }));
    expect(document.activeElement?.id).toBe("editor-category-title");
  });

  it("renders a human-readable secret-safe saved-to-proposed review", async () => {
    const mounted = await mountCockpit({ route: "game-settings" });
    mounted.root.querySelector("#open-settings-editor").click();
    await flush();
    const serverName = mounted.root.querySelector("#setting-ServerName");
    serverName.value = "Changed";
    serverName.dispatchEvent(new Event("change", { bubbles: true }));
    mounted.root.querySelector("#editor-preview").click();
    await flush();
    const review = mounted.root.querySelector("#editor-preview-status");
    expect(review.textContent).toContain("Proposed changes");
    expect(review.textContent).toContain("Server Name");
    expect(review.textContent).toContain("Test");
    expect(review.textContent).toContain("Changed");
    expect(review.textContent).toContain("Changes gameplay or server behavior");
    expect(review.querySelector("details summary").textContent).toContain("Redacted INI diff");
  });

  it("keeps disabled Review and Apply reasons visible and programmatically linked", async () => {
    const mounted = await mountCockpit({ route: "game-settings", providerStatus: "started" });
    mounted.root.querySelector("#open-settings-editor").click();
    await flush();
    expect(mounted.root.querySelector("#editor-preview").getAttribute("aria-describedby")).toBe("editor-review-reason");
    expect(mounted.root.querySelector("#editor-review-reason").textContent).toContain("Make a change");
    expect(mounted.root.querySelector("#editor-apply").getAttribute("aria-describedby")).toBe("editor-apply-reason");
    expect(mounted.root.querySelector("#editor-apply-reason").textContent).toContain("read-only in this release");
  });

  it("keeps expert source credential-redacted and preserves the draft after filtering", async () => {
    const mounted = await mountCockpit({ route: "game-settings" });
    mounted.root.querySelector("#open-settings-editor").click();
    await flush();
    const serverName = mounted.root.querySelector("#setting-ServerName");
    serverName.value = "Panic at the Palbox";
    serverName.dispatchEvent(new Event("change", { bubbles: true }));
    mounted.root.querySelector('[data-editor-scope="nondefault"]').click();
    expect(mounted.root.querySelector("#setting-ServerName").value).toBe("Panic at the Palbox");
    expect(mounted.root.textContent).toContain("AdminPassword=[redacted]");
  });

  it("has no serious axe violations with the structured editor open", async () => {
    const mounted = await mountCockpit({ route: "game-settings" });
    mounted.root.querySelector("#open-settings-editor").click();
    await flush();
    await expectNoSeriousAxeViolations(mounted.root);
  });

  it("detects and repairs Pocketpair's three-setting PvP dependency", async () => {
    const mounted = await mountCockpit({ route: "game-settings" });
    mounted.root.querySelector("#open-settings-editor").click();
    await flush();
    const pvp = mounted.root.querySelector("#setting-bIsPvP");
    pvp.checked = true;
    pvp.dispatchEvent(new Event("change", { bubbles: true }));
    expect(mounted.root.textContent).toContain("trial PvP mode also requires player damage");
    expect(mounted.root.querySelector("#editor-preview").disabled).toBe(true);
    mounted.root.querySelector("#fix-pvp-requirements").click();
    expect(mounted.root.textContent).not.toContain("trial PvP mode also requires player damage");
    expect(mounted.controller._editorOperations()).toEqual(expect.arrayContaining([
      { op: "set", key: "bIsPvP", raw_value: "True" },
      { op: "set", key: "bEnablePlayerToPlayerDamage", raw_value: "True" },
      { op: "set", key: "bEnableDefenseOtherGuildPlayer", raw_value: "True" },
    ]));
  });

  it.each(["overview", "auto-shutdown", "save-games", "game-settings"])("has no automated WCAG A/AA structural violations on %s", async (route) => {
    const mounted = await mountCockpit({ route, consent: "unresolved" });
    await expectNoSeriousAxeViolations(mounted.root);
  });
});
