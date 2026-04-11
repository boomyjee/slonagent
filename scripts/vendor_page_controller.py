"""Re-vendor @page-agent/page-controller into the extension.

Downloads the main module + its lazy SimulatorMask chunk + ai-motion sub-dep
from esm.sh, rewrites their cross-chunk imports to clean relative paths and
writes the result to src/modes/web_agent/extension/vendor/.

Run after bumping the VERSION constant below.
"""
import urllib.request
from pathlib import Path

VERSION = "1.7.1"
BASE = f"https://esm.sh/@page-agent/page-controller@{VERSION}/es2022"
VENDOR = Path(__file__).resolve().parents[1] / "src" / "modes" / "web_agent" / "extension" / "vendor"


def fetch(url: str) -> str:
    print(f"  fetch {url}")
    with urllib.request.urlopen(url) as r:
        return r.read().decode("utf-8")


def main():
    VENDOR.mkdir(parents=True, exist_ok=True)

    # 1. Main module. The dynamic `import("./dist/lib/SimulatorMask-HASH.mjs")`
    # gets rewritten to `./simulator-mask.mjs` — we glob the original path so
    # the hash doesn't need hand-updating.
    pc = fetch(f"{BASE}/page-controller.mjs")
    import re
    m = re.search(r'"\./dist/lib/(SimulatorMask-[^"]+\.mjs)"', pc)
    if not m:
        raise SystemExit("page-controller: SimulatorMask import not found — did the bundle structure change?")
    sim_path = m.group(1)
    pc = pc.replace(f'"./dist/lib/{sim_path}"', '"./simulator-mask.mjs"')
    (VENDOR / "page-controller.mjs").write_text(pc, encoding="utf-8")

    # 2. SimulatorMask chunk. It has a static `from "/ai-motion@^0.4.8?target=es2022"`
    # which, when loaded as an extension file, would resolve to
    # chrome-extension://EXTID/ai-motion@^0.4.8 — not a real file. Rewrite it.
    sim = fetch(f"{BASE}/dist/lib/{sim_path}")
    sim = re.sub(r'"/ai-motion@[^"]+"', '"./ai-motion.mjs"', sim)
    (VENDOR / "simulator-mask.mjs").write_text(sim, encoding="utf-8")

    # 3. ai-motion — the /ai-motion@^0.4.8 entry is a one-line re-export, so
    # we follow it and grab the actual module to avoid another hop.
    stub = fetch("https://esm.sh/ai-motion@^0.4.8?target=es2022")
    m = re.search(r'"(/ai-motion@[^"]+/es2022/ai-motion\.mjs)"', stub)
    if not m:
        raise SystemExit("ai-motion: entry stub structure changed")
    am = fetch(f"https://esm.sh{m.group(1)}")
    (VENDOR / "ai-motion.mjs").write_text(am, encoding="utf-8")

    print(f"vendored {VERSION} -> {VENDOR}")


if __name__ == "__main__":
    main()
