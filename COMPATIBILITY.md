# Compatibility and acceptance matrix

- Minimum Home Assistant: **2026.8.2** (the exact pinned real-HA harness).
- Python: **3.14.2 through 3.14.x**, matching Home Assistant 2026.8.2.
- Node.js: **22.19.0** for release CI.
- Frontend unit/accessibility tests: npm lockfile with Vitest/axe.
- Browser acceptance: Playwright **1.55.1** and its pinned Chromium at 1440, 768, 390, 360, and
  320 CSS pixels, including dark theme, keyboard flow, zoom/reflow contracts,
  reduced-motion-safe behavior, and automated accessibility checks.

The user interface is English-only in the initial release. Core/backend Home
Assistant translation files exist, but complete frontend localization is not
claimed. Human NVDA/VoiceOver listening remains a manual release checklist item.

Only the newest integration release on the declared minimum or newer compatible
Home Assistant version is supported. Browser engines outside Home Assistant's
supported webview/browser set receive best-effort support.
