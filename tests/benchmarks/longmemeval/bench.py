"""
Быстрый прогон LongMemEval для LogCompressor.

Запуск:
    python tests/benchmarks/longmemeval/bench.py [N]

    N — сколько вопросов прогнать (по умолчанию 10)
"""
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT))

from src.memory.compressors.log import LogCompressor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bench")


async def retry(fn, retries=5, base_delay=1):
    """Повторяет fn при 503/429, экспоненциальная задержка."""
    for attempt in range(retries):
        try:
            return await fn()
        except Exception as e:
            code = getattr(e, "code", None) or getattr(getattr(e, "error", None), "get", lambda k, d=None: d)("code")
            msg  = str(e)
            retryable = "503" in msg or "429" in msg or "UNAVAILABLE" in msg or "quota" in msg.lower()
            if retryable and attempt < retries - 1:
                delay = base_delay * (2 ** attempt)
                log.warning("attempt %d/%d failed (%s), retry in %ds...", attempt + 1, retries, msg[:60], delay)
                await asyncio.sleep(delay)
            else:
                raise


COMPRESSOR_MODEL = "gemini-2.5-flash"
ANSWER_MODEL     = "gemini-3-flash-preview"


def load_config():
    cfg = json.loads((ROOT / ".config.json").read_text(encoding="utf-8"))
    for k, v in cfg.get("env", {}).items():
        os.environ.setdefault(k, v)
    keys = cfg.get("keys", {})
    return keys.get("llm") or os.environ["LLM_KEY"], keys.get("llm_url", "")


async def prepare(compressor, sample):
    """Кормим все сессии компрессору по одной. Он сам решает когда стрелять Observer."""
    turns = []
    for session, date in zip(sample["haystack_sessions"], sample["haystack_dates"]):
        for msg in session:
            if not msg.get("content"):
                continue
            turns.append({
                "role": "user" if msg["role"] == "user" else "assistant",
                "content": [{"type": "text", "text": msg["content"]}],
                "_timestamp": date,
            })
        turns = await retry(lambda t=turns: compressor.compress(t))
    return turns


async def ask(client, model, turns, question, question_date):
    """Задаём вопрос, используя сжатую память как контекст."""
    messages = [{"role": "system", "content": "You are a helpful assistant with memory of past conversations."}]

    for t in turns:
        if not isinstance(t, dict) or t.get("role") not in ("user", "assistant"):
            continue
        content = t.get("content")
        if isinstance(content, list):
            text = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
        else:
            text = str(content or "")
        if text.strip():
            messages.append({"role": t["role"], "content": text})

    messages.append({"role": "user", "content": f"Current Date: {question_date}\nQuestion: {question}"})

    async def call():
        resp = await client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0,
            max_tokens=256,
        )
        return resp.choices[0].message.content.strip()

    return await retry(call)


async def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 10

    api_key, base_url = load_config()

    print(f"Компрессор: {COMPRESSOR_MODEL}")
    print(f"Ответы:     {ANSWER_MODEL}")
    print(f"Лимит:      {limit} вопросов")
    print()

    import httpx
    from openai import AsyncOpenAI
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    http_client = httpx.AsyncClient(proxy=proxy, timeout=120.0) if proxy else httpx.AsyncClient(timeout=120.0)
    client = AsyncOpenAI(api_key=api_key, base_url=base_url, http_client=http_client)

    compressor = LogCompressor(model_name=COMPRESSOR_MODEL, api_key=api_key, base_url=base_url)

    data_path = Path(__file__).parent / "data" / "longmemeval_s_cleaned.json"
    data = json.loads(data_path.read_text(encoding="utf-8"))[:limit]

    print(f"Прогоняем {len(data)} вопросов...\n")

    for i, sample in enumerate(data, 1):
        qid   = sample["question_id"]
        qtype = sample["question_type"]
        q     = sample["question"]
        expected = sample["answer"]
        sessions = sample["haystack_sessions"]

        print(f"[{i}/{len(data)}] {qid} ({qtype}) — {len(sessions)} сессий")
        print(f"  Вопрос: {q}")

        turns = await prepare(compressor, sample)

        compressed = any(t.get("_observation_message") for t in turns if isinstance(t, dict))
        print(f"  Память: {'сжата ✓' if compressed else 'не сжималась (мало токенов)'}")

        answer = await ask(client, ANSWER_MODEL, turns, q, sample.get("question_date", ""))

        print(f"  Ответ:    {answer}")
        print(f"  Правильно: {expected}")
        print()


asyncio.run(main())
