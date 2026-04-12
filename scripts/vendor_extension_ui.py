"""Copy the shared chat UI into the extension as local files.

The extension sidepanel imports lib.js, Chat.js and chat-widget.js
directly from its own package, so the chat is visible instantly after
opening the panel (no network round-trip).

This script mirrors those files from their authoritative locations
into src/modes/web_agent/extension/web/. The layout preserves the
server's `components/common/` subdir because Chat.js imports
`../../lib.js` by relative path — mirroring the structure avoids
rewriting the import.

Run after editing any of the source files:
    python -m scripts.vendor_extension_ui
"""
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHARED_UI = ROOT / "src" / "transport" / "web" / "ui"
WEB_AGENT_UI = ROOT / "src" / "modes" / "web_agent" / "ui"
DEST = ROOT / "src" / "modes" / "web_agent" / "extension" / "web"

SOURCES = [
    (SHARED_UI / "lib.js", DEST / "lib.js"),
    (SHARED_UI / "components" / "common" / "Chat.js", DEST / "components" / "common" / "Chat.js"),
    (WEB_AGENT_UI / "chat-widget.js", DEST / "chat-widget.js"),
]


def main():
    for src, dst in SOURCES:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        print(f"  {src.relative_to(ROOT)} -> {dst.relative_to(ROOT)}")
    print(f"vendored {len(SOURCES)} files into {DEST.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
