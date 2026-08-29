const digestFromUrl = () => {
  const match = new URL(import.meta.url).pathname.match(/\/([0-9a-f]{64})\.js$/);
  return match ? `sha256:${match[1]}` : "development";
};

export const cockpitMetadata = Object.freeze({
  cockpitApiVersion: 1,
  frontendRevision: digestFromUrl(),
  key: "companion_harness",
});

export const createCockpit = () => ({
  mount(root) {
    root.replaceChildren(Object.assign(document.createElement("h1"), { textContent: "Companion Harness" }));
  },
  ready() {},
  update() {},
  navigationGuard() { return { decision: "allow" }; },
  dispose() {},
});
