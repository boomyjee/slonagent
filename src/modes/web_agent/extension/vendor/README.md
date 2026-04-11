# Vendored page-controller

Self-contained copy of `@page-agent/page-controller@1.7.1` and its lazy chunks
(`SimulatorMask`, `ai-motion`). We host these as extension files instead of
importing from esm.sh at runtime because:

- MV3 content scripts in MAIN world run inside the host page's JS context.
  Relative/root-relative dynamic imports inside the loaded module resolve
  against the module URL *sometimes* and the page's document base URL
  *other times*, depending on Chrome version and the page's import map /
  service worker. Any of those can break — exact repro: esm.sh returns 200,
  but `new PageController({ enableMask: true })` rejects with "Failed to
  fetch dynamically imported module" because `./dist/lib/SimulatorMask-*.mjs`
  resolves to the wrong origin.
- `chrome-extension://` URLs are exempt from page CSP, service workers and
  import maps, so there are no surprises.

Files must be listed in `manifest.json > web_accessible_resources` so the
MAIN-world content script can `import()` them. content-isolated.js passes
the extension-relative URL to content-main.js via `dataset.slonPcUrl` (which
is readable from MAIN world synchronously, no postMessage race).

To re-vendor (e.g. after bumping page-controller version):

    python -m scripts.vendor_page_controller

(see scripts/vendor_page_controller.py)
