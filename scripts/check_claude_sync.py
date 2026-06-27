"""Проверяет рассинхронизацию между нашей памятью и Claude jsonl-сессиями.

Логика та же что в ClaudeBackend._sync_claude_session, но в read-only режиме:
проходит по всем CLAUDE*.json в memory/, находит сопоставленный jsonl и
печатает статус каждой сессии.

Запуск:
    .venv/Scripts/python scripts/check_claude_sync.py
"""
import json
import os
import re
import sys
from pathlib import Path


def claude_jsonl_path(cwd: str, session_id: str) -> str | None:
    sanitized = re.sub(r'[^a-zA-Z0-9]', '-', cwd)
    path = os.path.join(os.path.expanduser("~"), ".claude", "projects", sanitized, f"{session_id}.jsonl")
    return path if os.path.isfile(path) else None


def load_memory_turns(memory_dir: str, thread_id: str) -> list[dict]:
    """Турны c _uuid из CONTEXT.json (jsonl), фильтруя по thread_id, в порядке файла."""
    context_path = os.path.join(memory_dir, "CONTEXT.json")
    if not os.path.isfile(context_path):
        return []
    turns = []
    with open(context_path, encoding="utf-8") as f:
        for line in f:
            try:
                t = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(t, dict):
                continue
            if t.get("_thread_id", "") != thread_id:
                continue
            if t.get("_uuid"):
                turns.append(t)
    return turns


def load_memory_uuids(memory_dir: str, thread_id: str) -> list[str]:
    return [t["_uuid"] for t in load_memory_turns(memory_dir, thread_id)]


def load_claude_entries(jsonl_path: str) -> list[dict]:
    """Все entries из jsonl, в порядке файла."""
    entries = []
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def turn_summary(turn: dict, source: str) -> str:
    """Короткая сводка турна для диагностики."""
    if source == "ours":
        role = turn.get("role", "?")
        ts = turn.get("_timestamp", "")[:19]
        if role == "tool":
            content = (turn.get("content") or "")
            content = content if isinstance(content, str) else json.dumps(content)
            return f"{role} | {ts} | tool_call_id={turn.get('tool_call_id','')[:12]} | {content[:80]!r}"
        if role == "assistant" and turn.get("tool_calls"):
            tcs = ", ".join(tc["function"]["name"] for tc in turn["tool_calls"])
            return f"{role} | {ts} | tool_calls=[{tcs}]"
        content = turn.get("content") or ""
        if isinstance(content, list):
            content = " ".join(b.get("text", "") for b in content if isinstance(b, dict) and "text" in b)
        return f"{role} | {ts} | {content[:120]!r}"
    # claude entry
    typ = turn.get("type", "?")
    ts = turn.get("timestamp", "")[:19]
    msg = turn.get("message", {}) or {}
    content = msg.get("content")
    if isinstance(content, list):
        parts = []
        for b in content:
            t = b.get("type") if isinstance(b, dict) else None
            if t == "text":
                parts.append(f"text={b.get('text','')[:80]!r}")
            elif t == "tool_use":
                parts.append(f"tool_use={b.get('name','')}")
            elif t == "tool_result":
                tr = b.get("content")
                if isinstance(tr, list):
                    tr = " ".join(p.get("text", "") for p in tr if isinstance(p, dict))
                parts.append(f"tool_result={(tr or '')[:80]!r}")
        content = " | ".join(parts)
    elif not isinstance(content, str):
        content = str(content)
    return f"{typ} | {ts} | {content[:120]}"


def load_claude_uuids(jsonl_path: str) -> list[tuple[int, str]]:
    """Список (line_idx, uuid) для user/assistant entries."""
    seq = []
    with open(jsonl_path, encoding="utf-8") as f:
        for i, line in enumerate(f):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") in ("user", "assistant"):
                uid = entry.get("uuid")
                if uid:
                    seq.append((i, uid))
    return seq


def canon_order(uuids: list[str], result_uuids: set[str]) -> list[str]:
    """Канонизирует порядок: внутри максимального прогона tool_result'ов UUID-ы
    сортируются (их порядок не определён). Зеркало ClaudeBackend._canon_order."""
    out: list[str] = []
    run: list[str] = []
    for u in uuids:
        if u in result_uuids:
            run.append(u)
        else:
            out.extend(sorted(run)); run.clear()
            out.append(u)
    out.extend(sorted(run))
    return out


def check_session(memory_dir: str, claude_state_path: str) -> dict:
    """Возвращает {status, detail, ...stats} для одной сессии."""
    fname = os.path.basename(claude_state_path)
    # CLAUDE.json → main thread (id=""), CLAUDE_<tid>.json → thread <tid>
    m = re.match(r"^CLAUDE(?:_(.+))?\.json$", fname)
    thread_id = m.group(1) or "" if m else ""

    with open(claude_state_path, encoding="utf-8") as f:
        state = json.load(f)
    session_id = state.get("session_id", "")

    cwd = os.path.join(memory_dir, "workspace")
    jsonl_path = claude_jsonl_path(cwd, session_id)
    our_turns = load_memory_turns(memory_dir, thread_id)
    our_uuids = [t["_uuid"] for t in our_turns]

    info = {
        "memory_dir": memory_dir,
        "thread_id": thread_id or "(main)",
        "session_id": session_id[:8],
        "our_turns": len(our_uuids),
        "jsonl_path": jsonl_path,
    }

    if not our_uuids:
        info["status"] = "EMPTY (one-shot legitimate)"
        return info

    if not jsonl_path:
        info["status"] = "DESYNC: jsonl missing"
        return info

    claude_entries = load_claude_entries(jsonl_path)
    # Все uuid-bearing entries, любого type (user/assistant/system/...)
    claude_seq = []  # (entry_idx, uuid)
    claude_result_uuids = set()
    for idx, entry in enumerate(claude_entries):
        uid = entry.get("uuid")
        if not uid:
            continue
        claude_seq.append((idx, uid))
        if entry.get("type") == "user":
            content = (entry.get("message") or {}).get("content")
            if isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content
            ):
                claude_result_uuids.add(uid)
    info["claude_turns"] = len(claude_seq)

    claude_uuids_set = {u for _, u in claude_seq}
    our_uuids_set = set(our_uuids)
    result_uuids = claude_result_uuids | {
        t["_uuid"] for t in our_turns if t.get("role") == "tool" and t.get("_uuid")
    }

    our_synced = [u for u in our_uuids if u in claude_uuids_set]
    claude_synced = [u for _, u in claude_seq if u in our_uuids_set]
    info["nested_only"] = len(our_uuids) - len(our_synced)

    if not our_synced:
        info["status"] = (
            f"DESYNC: ни один из {len(our_uuids)} UUID памяти не найден в Claude jsonl"
        )
        return info

    # Сравниваем с канонизированным порядком кластеров tool_result'ов (их порядок
    # не определён). Маппинг для summary — по UUID, а не по позиции.
    uuid_to_our_idx = {t["_uuid"]: i for i, t in enumerate(our_turns)}
    uuid_to_claude_entry = {u: claude_entries[idx] for idx, u in claude_seq}
    our_canon = canon_order(our_synced, result_uuids)
    claude_canon = canon_order(claude_synced, result_uuids)

    for i in range(min(len(our_canon), len(claude_canon))):
        if our_canon[i] != claude_canon[i]:
            info["status"] = (
                f"DESYNC: UUID mismatch at synced position {i} "
                f"(ours={our_canon[i][:8]}, claude={claude_canon[i][:8]})"
            )
            ctx_start = max(0, i - 3)
            ctx_end = min(len(our_canon), len(claude_canon), i + 6)
            info["mismatch_context"] = []
            for j in range(ctx_start, ctx_end):
                marker = " <-- mismatch" if j == i else ""
                info["mismatch_context"].append(
                    f"    [{j:3}] ours={our_canon[j][:8]} | claude={claude_canon[j][:8]}{marker}"
                )
            info["mismatch_full_ours"] = our_canon[i]
            info["mismatch_full_claude"] = claude_canon[i]
            our_turn = our_turns[uuid_to_our_idx[our_canon[i]]]
            claude_entry = uuid_to_claude_entry[claude_canon[i]]
            info["mismatch_ours_summary"] = turn_summary(our_turn, "ours")
            info["mismatch_claude_summary"] = turn_summary(claude_entry, "claude")
            return info

    if len(our_synced) != len(claude_synced):
        ahead = "we" if len(our_synced) > len(claude_synced) else "Claude"
        diff = abs(len(our_synced) - len(claude_synced))
        info["status"] = f"DESYNC: {ahead} ahead by {diff} synced turns"
        return info

    first_match_line = next(idx for idx, u in claude_seq if u in our_uuids_set)
    pre_count = sum(
        1 for idx, u in claude_seq
        if idx < first_match_line and u not in our_uuids_set
    )
    info["pre_count"] = pre_count
    if pre_count > 20:
        info["status"] = f"OK (would prune {pre_count} old extras on next sync)"
    else:
        info["status"] = "OK (in sync)"
    return info


def find_memory_dirs(root: str) -> list[str]:
    dirs = []
    if os.path.isfile(os.path.join(root, "CONTEXT.json")):
        dirs.append(root)
    for sub in glob_subagent_memories(root):
        dirs.append(sub)
    return dirs


def glob_subagent_memories(root: str) -> list[str]:
    base = os.path.join(root, "subagents")
    if not os.path.isdir(base):
        return []
    return [
        os.path.join(base, name, "memory")
        for name in os.listdir(base)
        if os.path.isfile(os.path.join(base, name, "memory", "CONTEXT.json"))
    ]


def main():
    project_root = Path(__file__).parent.parent
    memory_root = project_root / "memory"
    if not memory_root.is_dir():
        print(f"memory dir not found: {memory_root}")
        sys.exit(1)

    memory_dirs = find_memory_dirs(str(memory_root))
    if not memory_dirs:
        print("No memory dirs with CONTEXT.json found")
        return

    results = []
    for mdir in memory_dirs:
        for fname in sorted(os.listdir(mdir)):
            if fname.startswith("CLAUDE") and fname.endswith(".json"):
                state_path = os.path.join(mdir, fname)
                results.append(check_session(mdir, state_path))

    if not results:
        print("No CLAUDE*.json state files found")
        return

    for r in results:
        rel_dir = os.path.relpath(r["memory_dir"], project_root)
        print(f"\n=== {rel_dir} | thread={r['thread_id']} | session={r['session_id']} ===")
        for k in ("status", "our_turns", "claude_turns", "nested_only", "pre_count", "jsonl_path"):
            if k in r:
                print(f"  {k}: {r[k]}")
        if "mismatch_context" in r:
            print("  context (position | ours | claude):")
            for line in r["mismatch_context"]:
                print(line)
            print(f"  full UUIDs: ours={r['mismatch_full_ours']}")
            print(f"              claude={r['mismatch_full_claude']}")
            print(f"  ours   turn: {r['mismatch_ours_summary']}")
            print(f"  claude turn: {r['mismatch_claude_summary']}")


if __name__ == "__main__":
    main()
