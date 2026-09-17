"""Antigravity PreToolUse Hook: Enforces tool permission policies for Google Antigravity CLI.

Spawned by agy.exe before any tool execution when configured in ~/.gemini/config/hooks.json.
Reads JSON from stdin, verifies if toolCall is allowed, and prints JSON response to stdout.
"""
import argparse
import json
import os
import sys


def parse_args():
    parser = argparse.ArgumentParser(description="SlonAgent Antigravity Tool Hook")
    parser.add_argument("--log", type=str, default="", help="Optional path to log hook executions")
    parser.add_argument("--forbidden", type=str, default="", help="Comma-separated forbidden tool names")
    parser.add_argument("--allowed", type=str, default="", help="Comma-separated allowed tool names")
    parser.add_argument("--reason", type=str, default="", help="Custom denial reason")
    return parser.parse_args()


def main():
    args = parse_args()
    log_path = args.log or os.environ.get("SLON_ANTIGRAVITY_HOOK_LOG", "")

    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw else {}
    except Exception as e:
        data = {"error": str(e)}

    tool_call = data.get("toolCall") or {}
    tool_name = tool_call.get("name") or ""

    forbidden_set = {t.strip() for t in args.forbidden.split(",") if t.strip()}
    allowed_set = {t.strip() for t in args.allowed.split(",") if t.strip()}

    # Determine if tool is denied. Хук вызывается только по matcher'у запрещённых
    # тулов, поэтому при нечитаемом stdin (имя тула неизвестно) отказываем — не разрешаем.
    # 1) If allowed set is specified: deny if tool_name not in allowed
    # 2) Else if forbidden set is specified: deny if tool_name in forbidden or unknown
    # 3) Else (hook was registered for forbidden tools via matcher): deny unconditionally
    is_denied = True
    if allowed_set:
        is_denied = tool_name not in allowed_set
    elif forbidden_set:
        is_denied = not tool_name or tool_name in forbidden_set

    if is_denied:
        reason = args.reason or f"Tool '{tool_name or '<unknown>'}' is forbidden by SlonAgent policy."
        resp = {
            "decision": "deny",
            "reason": reason,
        }
    else:
        resp = {
            "decision": "allow",
        }

    # Log execution if log_path is provided
    if log_path:
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "tool": tool_name,
                    "input": data,
                    "response": resp,
                }, ensure_ascii=False) + "\n")
        except Exception:
            pass

    print(json.dumps(resp, ensure_ascii=False))


if __name__ == "__main__":
    main()
