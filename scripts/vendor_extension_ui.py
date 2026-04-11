"""Copy the dashboard chat UI into the extension as local files.

The extension sidepanel no longer loads the UI from the slonagent web server
via an iframe — it imports lib.js, Chat.js and chat-widget.js directly from
its own package, so the chat is visible instantly after opening the panel
(no network round-trip to pull the widget code).

This script mirrors those three files from their authoritative locations
into src/modes/web_agent/extension/web/. The layout preserves the
dashboard's `components/` subdir because Chat.js imports `../lib.js` by
relative path — mirroring the structure avoids rewriting the import.

Run after editing any of the three source files:
    python -m scripts.vendor_extension_ui
"""
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_WEB = ROOT / "src" / "transport" / "dashboard" / "web"
WEB_AGENT_WEB = ROOT / "src" / "modes" / "web_agent" / "web"
DEST = ROOT / "src" / "modes" / "web_agent" / "extension" / "web"

SOURCES = [
    (DASHBOARD_WEB / "lib.js", DEST / "lib.js"),
    (DASHBOARD_WEB / "components" / "Chat.js", DEST / "components" / "Chat.js"),
    (WEB_AGENT_WEB / "chat-widget.js", DEST / "chat-widget.js"),
]


def main():
    for src, dst in SOURCES:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        print(f"  {src.relative_to(ROOT)} -> {dst.relative_to(ROOT)}")
    print(f"vendored {len(SOURCES)} files into {DEST.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
