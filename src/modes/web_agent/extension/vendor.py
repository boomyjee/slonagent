"""Re-vendor all extension dependencies: page-controller from esm.sh
and UI files (lib.js, Chat.js, chat-widget.js) from the project.

Run from the repo root:
    python src/modes/web_agent/extension/vendor.py
"""
import re, shutil, urllib.request
from pathlib import Path

EXT = Path(__file__).resolve().parent
ROOT = EXT.parents[3]

# --- page-controller from esm.sh ---

PC_VERSION = "1.7.1"
PC_BASE = f"https://esm.sh/@page-agent/page-controller@{PC_VERSION}/es2022"
VENDOR = EXT / "vendor"


def fetch(url: str) -> str:
    print(f"  fetch {url}")
    with urllib.request.urlopen(url) as r:
        return r.read().decode("utf-8")


def vendor_page_controller():
    VENDOR.mkdir(parents=True, exist_ok=True)

    # Main module — rewrite SimulatorMask lazy chunk path
    pc = fetch(f"{PC_BASE}/page-controller.mjs")
    m = re.search(r'"\./dist/lib/(SimulatorMask-[^"]+\.mjs)"', pc)
    if not m:
        raise SystemExit("page-controller: SimulatorMask import not found")
    sim_path = m.group(1)
    pc = pc.replace(f'"./dist/lib/{sim_path}"', '"./simulator-mask.mjs"')
    (VENDOR / "page-controller.mjs").write_text(pc, encoding="utf-8")

    # SimulatorMask chunk — rewrite ai-motion import
    sim = fetch(f"{PC_BASE}/dist/lib/{sim_path}")
    sim = re.sub(r'"/ai-motion@[^"]+"', '"./ai-motion.mjs"', sim)
    (VENDOR / "simulator-mask.mjs").write_text(sim, encoding="utf-8")

    # ai-motion — follow the esm.sh stub to the actual module
    stub = fetch("https://esm.sh/ai-motion@^0.4.8?target=es2022")
    m = re.search(r'"(/ai-motion@[^"]+/es2022/ai-motion\.mjs)"', stub)
    if not m:
        raise SystemExit("ai-motion: entry stub structure changed")
    am = fetch(f"https://esm.sh{m.group(1)}")
    (VENDOR / "ai-motion.mjs").write_text(am, encoding="utf-8")

    print(f"  page-controller {PC_VERSION} -> {VENDOR.relative_to(ROOT)}")


# --- UI files from the project ---

SHARED_UI = ROOT / "src" / "transport" / "web" / "ui"
WEB_AGENT_UI = ROOT / "src" / "modes" / "web_agent" / "ui"
WEB_DEST = EXT / "web"

UI_SOURCES = [
    (SHARED_UI / "lib.js", WEB_DEST / "lib.js"),
    (SHARED_UI / "components" / "common" / "Chat.js", WEB_DEST / "components" / "common" / "Chat.js"),
    (WEB_AGENT_UI / "chat-widget.js", WEB_DEST / "chat-widget.js"),
]


def vendor_ui():
    for src, dst in UI_SOURCES:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        print(f"  {src.relative_to(ROOT)} -> {dst.relative_to(ROOT)}")


def main():
    print("vendor page-controller:")
    vendor_page_controller()
    print("vendor ui:")
    vendor_ui()
    print("done")


if __name__ == "__main__":
    main()
