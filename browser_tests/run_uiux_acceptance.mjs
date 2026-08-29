import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawn } from "node:child_process";
import { chromium as playwrightChromium } from "playwright";

const root = new URL("../", import.meta.url).pathname;
const httpPort = 18765;
const debugPort = 18766;
const profile = await mkdtemp(join(tmpdir(), "nitrado-uiux-chromium-"));
const children = [];

const stop = async () => {
  for (const child of children.reverse()) {
    if (!child.killed) {
      child.kill("SIGTERM");
      await Promise.race([
        new Promise((resolve) => child.once("exit", resolve)),
        new Promise((resolve) => setTimeout(resolve, 1000)),
      ]);
    }
  }
  for (let attempt = 0; attempt < 5; attempt += 1) {
    try { await rm(profile, { recursive: true, force: true }); break; }
    catch (error) {
      if (attempt === 4) throw error;
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
  }
};
process.on("SIGINT", () => { void stop().finally(() => process.exit(130)); });
process.on("SIGTERM", () => { void stop().finally(() => process.exit(143)); });

const waitFor = async (
  predicate,
  label,
  attempts = 100,
  fatalError = () => null,
  timeoutDetail = () => "",
) => {
  let lastError = null;
  for (let index = 0; index < attempts; index += 1) {
    const fatal = fatalError();
    if (fatal) throw fatal;
    try {
      if (await predicate()) return;
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  const detail = timeoutDetail().trim() || lastError?.message || "";
  throw new Error(`Timed out waiting for ${label}${detail ? `: ${detail}` : ""}`);
};

class Cdp {
  constructor(url) {
    this.socket = new WebSocket(url);
    this.nextId = 1;
    this.pending = new Map();
    this.listeners = new Map();
    this.socket.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      if (!message.id) {
        for (const listener of this.listeners.get(message.method) || []) listener(message.params || {});
        return;
      }
      const request = this.pending.get(message.id);
      if (!request) return;
      this.pending.delete(message.id);
      if (message.error) request.reject(new Error(message.error.message));
      else request.resolve(message.result);
    });
  }

  async open() {
    await new Promise((resolve, reject) => {
      this.socket.addEventListener("open", resolve, { once: true });
      this.socket.addEventListener("error", reject, { once: true });
    });
  }

  call(method, params = {}) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }

  on(method, listener) {
    const listeners = this.listeners.get(method) || [];
    listeners.push(listener);
    this.listeners.set(method, listeners);
  }

  async eval(expression) {
    const result = await this.call("Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true,
      userGesture: true,
    });
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.text || "Browser evaluation failed");
    return result.result.value;
  }
}

try {
  const server = spawn("python3", ["-m", "http.server", String(httpPort), "--bind", "127.0.0.1"], {
    cwd: root,
    stdio: "ignore",
  });
  children.push(server);
  await waitFor(async () => (await fetch(`http://127.0.0.1:${httpPort}/browser_tests/uiux_fixture.html`)).ok, "fixture server");

  const chromiumExecutable = process.env.CHROMIUM_PATH || playwrightChromium.executablePath();
  let chromiumStderr = "";
  const chromium = spawn(chromiumExecutable, [
    "--headless=new",
    "--no-sandbox",
    "--disable-gpu",
    `--remote-debugging-port=${debugPort}`,
    `--user-data-dir=${profile}`,
    "about:blank",
  ], { stdio: ["ignore", "ignore", "pipe"] });
  chromium.stderr.setEncoding("utf8");
  chromium.stderr.on("data", (chunk) => {
    chromiumStderr = `${chromiumStderr}${chunk}`.slice(-8192);
  });
  children.push(chromium);
  await waitFor(
    async () => (await fetch(`http://127.0.0.1:${debugPort}/json/version`)).ok,
    "Chromium DevTools",
    400,
    () => {
      if (chromium.exitCode === null && chromium.signalCode === null) return null;
      const status = chromium.signalCode ? `signal ${chromium.signalCode}` : `exit ${chromium.exitCode}`;
      return new Error(`Chromium exited before DevTools was ready (${status})${chromiumStderr.trim() ? `: ${chromiumStderr.trim()}` : ""}`);
    },
    () => chromiumStderr,
  );
  const page = await (await fetch(`http://127.0.0.1:${debugPort}/json/new?${encodeURIComponent(`http://127.0.0.1:${httpPort}/browser_tests/uiux_fixture.html`)}`, { method: "PUT" })).json();
  const cdp = new Cdp(page.webSocketDebuggerUrl);
  await cdp.open();
  const consoleErrors = [];
  cdp.on("Runtime.exceptionThrown", ({ exceptionDetails }) => consoleErrors.push(exceptionDetails?.text || "Uncaught browser exception"));
  cdp.on("Runtime.consoleAPICalled", ({ type, args = [] }) => {
    if (["error", "assert"].includes(type)) consoleErrors.push(args.map((arg) => arg.value ?? arg.description ?? "").join(" "));
  });
  await Promise.all([cdp.call("Runtime.enable"), cdp.call("Page.enable"), cdp.call("Accessibility.enable")]);
  await cdp.call("Page.bringToFront");
  await waitFor(() => cdp.eval("window.__fixtureReady === true"), "fixture mount");

  const results = { chromium: await cdp.eval("navigator.userAgent"), widths: {}, keyboard: {}, identity: {}, confirmations: {}, focus: {}, announcements: {}, responsive: {}, provider: {}, themes: {}, axe: null, consoleErrors };
  for (const width of [1440, 768, 390, 360, 320]) {
    await cdp.call("Emulation.setDeviceMetricsOverride", { width, height: width === 1440 ? 900 : 844, deviceScaleFactor: 1, mobile: width < 500 });
    await cdp.eval(`(async()=>{ window.fixture.panel.narrow=${width <= 768}; await window.fixture.openEditor(); })()`);
    const measurements = await cdp.eval(`(()=>{
      const root=window.fixture.root;
      const visible=(node)=>{ const style=getComputedStyle(node); const rect=node.getBoundingClientRect(); return style.display!=="none" && style.visibility!=="hidden" && !node.hidden && rect.width>0 && rect.height>0; };
      const targets=[...root.querySelectorAll("button,input,select,summary")].filter(visible).map((node)=>{
        const target=(node.matches('input[type="radio"],input[type="checkbox"]')&&node.closest("label"))||node;
        const rect=target.getBoundingClientRect();
        return { id:node.id||node.textContent.trim().slice(0,40), target:target===node?node.tagName.toLowerCase():"label", width:rect.width, height:rect.height };
      });
      const workspace=root.querySelector(".editor-workspace")?.getBoundingClientRect();
      const footer=root.querySelector(".editor-footer")?.getBoundingClientRect();
      return {
        innerWidth,
        pageOverflow:document.documentElement.scrollWidth>innerWidth,
        undersized:targets.filter((item)=>item.width<44 || item.height<44),
        footerOverlap:Boolean(workspace&&footer&&workspace.bottom>footer.top+1),
        tabsVisible:visible(root.querySelector(".tabs")),
        routeSelectVisible:visible(root.querySelector(".route-select")),
        railVisible:visible(root.querySelector(".editor-rail")),
        categorySelectVisible:visible(root.querySelector(".editor-category-select")),
        dialog:{ width:root.querySelector("#palworld-settings-editor").getBoundingClientRect().width, height:root.querySelector("#palworld-settings-editor").getBoundingClientRect().height },
      };
    })()`);
    results.widths[width] = measurements;
    await cdp.eval("window.fixture.root.querySelector('#editor-close').click()");
  }

  await cdp.call("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await cdp.eval("window.fixture.openEditor()");
  results.identity.before = await cdp.eval(`(()=>{ const root=window.fixture.root; window.__dialog=root.querySelector("#palworld-settings-editor"); window.__body=root.querySelector(".editor-body"); window.__status=root.querySelector("#editor-operation-status"); return true; })()`);
  await cdp.eval(`(()=>{ const root=window.fixture.root; const field=root.querySelector("#setting-ServerName"); field.value="Browser preview"; field.dispatchEvent(new Event("change",{bubbles:true})); root.querySelector("#editor-preview").click(); })()`);
  await waitFor(() => cdp.eval("window.fixture.root.querySelector('#editor-preview-status') !== null"), "browser preview");
  await waitFor(() => cdp.eval("window.fixture.panel._controller.pending === null"), "preview completion");
  results.identity.preserved = await cdp.eval(`window.__dialog===window.fixture.root.querySelector("#palworld-settings-editor") && window.__body===window.fixture.root.querySelector(".editor-body") && window.__status===window.fixture.root.querySelector("#editor-operation-status")`);

  results.keyboard.editorDirty = await cdp.eval("window.fixture.panel._controller.editor?.dirty === true");
  await cdp.eval("window.focus(); window.fixture.root.querySelector('#editor-close').focus(); true");
  await cdp.call("Input.dispatchKeyEvent", { type: "rawKeyDown", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27, nativeVirtualKeyCode: 27 });
  await cdp.call("Input.dispatchKeyEvent", { type: "keyUp", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27, nativeVirtualKeyCode: 27 });
  await new Promise((resolve) => setTimeout(resolve, 250));
  results.keyboard.nativeEscapeOpenedConfirmation = await cdp.eval("Boolean(window.fixture.root.querySelector('#host-confirmation-dialog')?.open)");
  if (!results.keyboard.nativeEscapeOpenedConfirmation) {
    await cdp.eval("window.fixture.root.querySelector('#palworld-settings-editor').dispatchEvent(new Event('cancel',{cancelable:true}))");
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  if (!await cdp.eval("Boolean(window.fixture.root.querySelector('#host-confirmation-dialog')?.open)")) {
    await cdp.eval("window.fixture.root.querySelector('#editor-close').click()");
  }
  await waitFor(() => cdp.eval("Boolean(window.fixture.root.querySelector('#host-confirmation-dialog')?.open)"), "dirty editor confirmation");
  results.keyboard.cancelInitiallyFocused = await cdp.eval("window.fixture.root.activeElement?.id === 'dialog-cancel'");
  await cdp.call("Input.dispatchKeyEvent", { type: "rawKeyDown", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27, nativeVirtualKeyCode: 27 });
  await cdp.call("Input.dispatchKeyEvent", { type: "keyUp", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27, nativeVirtualKeyCode: 27 });
  await waitFor(() => cdp.eval("!window.fixture.root.querySelector('#host-confirmation-dialog')?.open"), "confirmation cancellation");
  results.keyboard.editorRemainsOpen = await cdp.eval("window.fixture.root.querySelector('#palworld-settings-editor')?.open === true");
  results.keyboard.focusInsideEditor = await cdp.eval("window.fixture.root.querySelector('#palworld-settings-editor')?.contains(window.fixture.root.activeElement) === true");

  await cdp.eval("window.fixture.root.querySelector('#editor-apply').click()");
  await waitFor(() => cdp.eval("Boolean(window.fixture.root.querySelector('#host-confirmation-dialog')?.open)"), "Apply confirmation");
  results.confirmations.applyDialogBesideEditor = await cdp.eval("window.fixture.root.querySelector('#palworld-settings-editor')?.open === true && window.fixture.root.querySelector('#host-confirmation-dialog')?.open === true");
  results.confirmations.applyCancelInitiallyFocused = await cdp.eval("window.fixture.root.activeElement?.id === 'dialog-cancel'");
  await cdp.eval("window.fixture.root.querySelector('#dialog-confirm').click()");
  await waitFor(() => cdp.eval("window.fixture.panel._controller.pending === null && !window.fixture.root.querySelector('#host-confirmation-dialog')?.open"), "Apply completion");
  results.confirmations.applyResultPersistent = await cdp.eval("window.fixture.root.querySelector('#editor-operation-status')?.textContent.includes('Settings applied') === true");
  results.confirmations.applyFocusStableControl = await cdp.eval("window.fixture.root.activeElement?.id === 'editor-close'");

  results.announcements.editorSingleOwner = await cdp.eval(`(()=>{
    const root=window.fixture.root;
    const review=root.querySelector('#editor-preview-status');
    const host=root.querySelector('#host-live');
    const cockpit=root.querySelector('#cockpit-live');
    return root.querySelector('#editor-operation-status')?.getAttribute('aria-live')==='polite'
      && (!review || review.getAttribute('aria-live')==='off')
      && host?.getAttribute('aria-live')==='polite'
      && cockpit?.getAttribute('aria-live')==='polite'
      && !host?.textContent.includes('Settings applied')
      && !cockpit?.textContent.includes('Settings applied');
  })()`);

  results.responsive.narrowScrollPreserved = await cdp.eval(`(()=>{
    const controller=window.fixture.panel._controller;
    const workspace=window.fixture.root.querySelector('.editor-workspace');
    const content=window.fixture.root.querySelector('.editor-content');
    workspace.scrollTop=Math.min(120,Math.max(0,workspace.scrollHeight-workspace.clientHeight));
    content.scrollTop=Math.min(120,Math.max(0,content.scrollHeight-content.clientHeight));
    const expectedWorkspace=workspace.scrollTop;
    const expectedContent=content.scrollTop;
    controller._renderEditorResults();
    return window.fixture.root.querySelector('.editor-workspace').scrollTop===expectedWorkspace
      && window.fixture.root.querySelector('.editor-content').scrollTop===expectedContent;
  })()`);

  await cdp.eval(`(()=>{
    const root=window.fixture.root;
    const field=root.querySelector('#setting-ServerName');
    field.value='Changed for focus test';
    field.dispatchEvent(new Event('change',{bubbles:true}));
    root.querySelector('[data-editor-scope="changed"]').click();
    const changed=root.querySelector('#setting-ServerName');
    changed.focus();
    changed.value='Test';
    changed.dispatchEvent(new Event('change',{bubbles:true}));
  })()`);
  results.focus.directEditRemovalFallback = await cdp.eval("window.fixture.root.activeElement?.id === 'editor-category-title'");

  await cdp.eval(`(()=>{
    const root=window.fixture.root;
    const controller=window.fixture.panel._controller;
    controller.editor.category='all';
    controller.editor.scope='all';
    controller._renderEditorResults();
    const pvp=root.querySelector('#setting-bIsPvP');
    pvp.checked=true;
    pvp.dispatchEvent(new Event('change',{bubbles:true}));
    root.querySelector('[data-editor-scope="issues"]').click();
    const fix=root.querySelector('#fix-pvp-requirements');
    fix.focus();
    fix.click();
  })()`);
  results.focus.pvpRepairRemovalFallback = await cdp.eval("window.fixture.root.activeElement?.id === 'editor-category-title' && !window.fixture.root.querySelector('#fix-pvp-requirements')");

  await cdp.eval(`(()=>{
    const controller=window.fixture.panel._controller;
    controller.editor.history=[{recovery_id:'fixture-recovery',backup_kind:'apply',created_at:1,operation_id:'fixture-op',available:true}];
    controller._renderEditorResults();
    window.fixture.root.querySelector('[data-recovery-id="fixture-recovery"]').click();
  })()`);
  await waitFor(() => cdp.eval("Boolean(window.fixture.root.querySelector('#host-confirmation-dialog')?.open)"), "Rollback confirmation");
  results.confirmations.rollbackCancelInitiallyFocused = await cdp.eval("window.fixture.root.activeElement?.id === 'dialog-cancel'");
  await cdp.eval("window.fixture.root.querySelector('#dialog-confirm').click()");
  await waitFor(() => cdp.eval("window.fixture.panel._controller.pending === null && !window.fixture.root.querySelector('#host-confirmation-dialog')?.open"), "Rollback completion");
  results.confirmations.rollbackResultPersistent = await cdp.eval("window.fixture.root.querySelector('#editor-operation-status')?.textContent.includes('verified backup was restored') === true");
  results.confirmations.rollbackFocusStableControl = await cdp.eval("window.fixture.root.activeElement?.id === 'editor-close'");
  await cdp.eval("window.fixture.root.querySelector('#editor-close').click()");
  await waitFor(() => cdp.eval("!window.fixture.root.querySelector('#palworld-settings-editor')?.open"), "clean editor close");

  await cdp.eval(`(async()=>{
    await window.fixture.route("save-games");
    const controller=window.fixture.panel._controller;
    controller.saveBundle={
      preview:{ changed:true, world_id:"fixture-world", portable_manifest:true,
        counts:{replaced:1,added:1,preserved:5,deleted:0},
        current:{files:6,bytes:1024}, proposed:{files:7,bytes:2048},
        replaced:[{path:"Level.sav",size:1024}], added:[{path:"Players/test.sav",size:512}] },
      previewToken:"fixture-save-preview", restoreEnabled:true, error:""
    };
    controller.render();
  })()`);
  await cdp.eval("window.fixture.root.querySelector('#apply-save-bundle').focus(); window.fixture.root.querySelector('#apply-save-bundle').click()");
  await waitFor(() => cdp.eval("Boolean(window.fixture.root.querySelector('#host-confirmation-dialog')?.open)"), "Restore confirmation");
  results.confirmations.restoreCancelInitiallyFocused = await cdp.eval("window.fixture.root.activeElement?.id === 'dialog-cancel'");
  await cdp.eval("window.fixture.root.querySelector('#dialog-cancel').click()");
  await waitFor(() => cdp.eval("!window.fixture.root.querySelector('#host-confirmation-dialog')?.open"), "Restore cancellation");
  results.confirmations.restoreCancelActiveId = await cdp.eval("window.fixture.root.activeElement?.id || ''");
  results.confirmations.restoreCancelReturnsFocus = results.confirmations.restoreCancelActiveId === "apply-save-bundle";
  results.confirmations.restoreReviewSurvivesCancel = await cdp.eval("Boolean(window.fixture.root.querySelector('#save-bundle-review'))");
  await cdp.eval("window.fixture.root.querySelector('#apply-save-bundle').click()");
  await waitFor(() => cdp.eval("Boolean(window.fixture.root.querySelector('#host-confirmation-dialog')?.open)"), "second Restore confirmation");
  await cdp.eval("window.fixture.root.querySelector('#dialog-confirm').click()");
  await waitFor(() => cdp.eval("window.fixture.panel._controller.pending === null && !window.fixture.root.querySelector('#host-confirmation-dialog')?.open"), "Restore completion");
  results.confirmations.restoreResultPersistent = await cdp.eval("window.fixture.root.querySelector('#save-bundle-operation-result')?.textContent.includes('Save restored and verified') === true");
  results.confirmations.restoreFocusResult = await cdp.eval("window.fixture.root.activeElement?.id === 'save-bundle-operation-result'");
  results.confirmations.noWriteFixtureCalls = await cdp.eval(`window.fixture.calls.filter((call)=>call.path.includes("editable-apply")||call.path.includes("save-bundle-apply")).map((call)=>call.path)`);

  await cdp.call("Emulation.setEmulatedMedia", { features:[{ name:"prefers-reduced-motion", value:"reduce" }] });
  results.responsive.reducedMotion = await cdp.eval(`(()=>{
    const host=getComputedStyle(window.fixture.root.querySelector('#provider-operation-result'));
    return host.animationName==='none' && host.transitionDuration==='0s';
  })()`);
  await cdp.call("Emulation.setEmulatedMedia", { features:[] });

  await cdp.eval(`(()=>{
    const panel=window.fixture.panel;
    panel.hass={...panel.hass,locale:{language:'en-XA'},language:'en-XA'};
    panel.narrow=true;
    return true;
  })()`);
  await waitFor(() => cdp.eval("window.fixture.root.querySelector('.route-select option:checked')?.textContent.includes('[') === true"), "pseudo-localized route");
  results.responsive.pseudoLocalizedNarrow = await cdp.eval(`(()=>({
    longLabel:window.fixture.root.querySelector('.route-select option:checked')?.textContent.length>20,
    pageOverflow:document.documentElement.scrollWidth>innerWidth,
    selectorOverflow:window.fixture.root.querySelector('.route-select').scrollWidth>window.fixture.root.querySelector('.route-select').clientWidth
  }))()`);
  await cdp.eval(`(()=>{ const panel=window.fixture.panel; panel.hass={...panel.hass,locale:{language:'en'},language:'en'}; return true; })()`);

  await cdp.eval(`(()=>{ const root=window.fixture.root; root.querySelector('#host-start').focus(); root.querySelector('#host-start').click(); })()`);
  await waitFor(() => cdp.eval("window.fixture.panel._providerPending === false && !window.fixture.root.querySelector('#host-start')"), "provider start transition");
  results.provider.startPreservesRegion = await cdp.eval("window.fixture.root.querySelector('.provider') !== null");
  results.provider.startFocusFallback = await cdp.eval("window.fixture.root.activeElement?.id === 'provider-operation-result'");
  await cdp.eval(`(()=>{ const root=window.fixture.root; root.querySelector('#host-stop').focus(); root.querySelector('#host-stop').click(); })()`);
  await waitFor(() => cdp.eval("Boolean(window.fixture.root.querySelector('#host-confirmation-dialog')?.open)"), "provider stop confirmation");
  await cdp.eval("window.fixture.root.querySelector('#dialog-confirm').click()");
  await waitFor(() => cdp.eval("window.fixture.panel._providerPending === false && !window.fixture.root.querySelector('#host-stop')"), "provider stop transition");
  results.provider.stopFocusFallback = await cdp.eval("window.fixture.root.activeElement?.id === 'provider-operation-result'");

  await cdp.eval(`(()=>{
    const style=document.documentElement.style;
    style.setProperty("--primary-color","#80cbc4");
    style.setProperty("--primary-text-color","#f5f5f5");
    style.setProperty("--secondary-text-color","#c6c6c6");
    style.setProperty("--card-background-color","#202124");
    style.setProperty("--primary-background-color","#111315");
    style.setProperty("--secondary-background-color","#2b2d30");
    style.setProperty("--divider-color","#62656a");
    true;
  })()`);
  await cdp.call("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await cdp.eval("window.fixture.panel.narrow=true");
  results.themes.dark390 = await cdp.eval(`(()=>({pageOverflow:document.documentElement.scrollWidth>innerWidth,routeSelectVisible:getComputedStyle(window.fixture.root.querySelector('.route-select')).display!=="none"}))()`);
  await cdp.eval("window.fixture.openEditor()");
  results.themes.darkAxe = await cdp.eval(`window.axe.run(document,{runOnly:{type:"tag",values:["wcag2a","wcag2aa","wcag21a","wcag21aa"]}}).then((r)=>r.violations.filter((v)=>["serious","critical"].includes(v.impact)).map((v)=>v.id))`);

  results.axe = await cdp.eval(`window.axe.run(document,{runOnly:{type:"tag",values:["wcag2a","wcag2aa","wcag21a","wcag21aa"]}}).then((r)=>r.violations.filter((v)=>["serious","critical"].includes(v.impact)).map((v)=>({id:v.id,impact:v.impact,help:v.help,nodes:v.nodes.map((node)=>({target:node.target,html:node.html,failureSummary:node.failureSummary}))})))`);
  const ax = await cdp.call("Accessibility.getFullAXTree");
  const controlRoles = new Set(["checkbox", "combobox", "radio", "spinbutton", "textbox"]);
  results.accessibility = {
    genericSettingNames: ax.nodes.filter((node) => controlRoles.has(node.role?.value) && ["Value", "Enabled", "Disabled", "Stored value is configured"].includes(node.name?.value)).map((node) => ({ role: node.role.value, name: node.name.value, backendDOMNodeId: node.backendDOMNodeId })),
    hasServerNameControl: ax.nodes.some((node) => String(node.name?.value || "").includes("Server Name")),
  };
  results.pass = Object.values(results.widths).every((item) => !item.pageOverflow && !item.footerOverlap && item.undersized.length === 0)
    && results.widths[768].categorySelectVisible && !results.widths[768].railVisible
    && results.widths[1440].railVisible && !results.widths[1440].categorySelectVisible
    && results.identity.preserved
    && Object.values(results.focus).every(Boolean)
    && Object.values(results.announcements).every(Boolean)
    && results.responsive.narrowScrollPreserved
    && results.responsive.reducedMotion
    && results.responsive.pseudoLocalizedNarrow.longLabel
    && !results.responsive.pseudoLocalizedNarrow.pageOverflow
    && !results.responsive.pseudoLocalizedNarrow.selectorOverflow
    && Object.values(results.provider).every(Boolean)
    && results.keyboard.cancelInitiallyFocused
    && results.keyboard.nativeEscapeOpenedConfirmation
    && results.keyboard.editorRemainsOpen
    && results.keyboard.focusInsideEditor
    && Object.entries(results.confirmations).filter(([key]) => !["noWriteFixtureCalls", "restoreCancelActiveId"].includes(key)).every(([,value]) => value === true)
    && results.confirmations.noWriteFixtureCalls.length === 2
    && !results.themes.dark390.pageOverflow
    && results.themes.dark390.routeSelectVisible
    && results.themes.darkAxe.length === 0
    && results.axe.length === 0
    && results.accessibility.genericSettingNames.length === 0
    && results.accessibility.hasServerNameControl;
  process.stdout.write(`${JSON.stringify(results, null, 2)}\n`);
  if (!results.pass) process.exitCode = 1;
  cdp.socket.close();
} finally {
  await stop();
}
