import asyncio, base64, io, json, os, logging, weakref
import numpy as np
import soundfile as sf
from datetime import datetime
import httpx
from src.memory.memory import Memory
from src.agent.agent_skill import AgentSkill, SubagentSkill
from src.transport.base import BaseTransport
from src.agent.transport_skill import TransportSkill


class BadFinishReason(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"LLM finished with: {reason}")


async def stoppable(coro, *stop_events: asyncio.Event):
    task = asyncio.create_task(coro)
    stop_tasks = [asyncio.create_task(e.wait()) for e in stop_events]
    try:
        await asyncio.wait({task, *stop_tasks}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for st in stop_tasks:
            st.cancel()
        if not task.done():
            task.cancel()
            try:
                await task
            except BaseException:
                pass
    if task.cancelled() or any(st.done() and not st.cancelled() for st in stop_tasks):
        return None
    return task.result()

class Agent:
    @staticmethod
    def OpenAI(api_key, base_url, sync=False):
        from openai import AsyncOpenAI, OpenAI
        from urllib.parse import urlparse
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        if proxy:
            no_proxy = os.environ.get("NO_PROXY", "")
            host = urlparse(base_url).hostname or ""
            if any(h.strip() == host for h in no_proxy.split(",")):
                proxy = None
        http = httpx.Client(proxy=proxy, timeout=120.0) if sync else httpx.AsyncClient(proxy=proxy, timeout=120.0)
        return (OpenAI if sync else AsyncOpenAI)(api_key=api_key, base_url=base_url, http_client=http, max_retries=0)

    @classmethod
    def from_config(cls, cfg: dict, **overrides):
        import importlib
        def inst(v):
            if isinstance(v, list): return [inst(i) for i in v]
            if not isinstance(v, dict): return v
            if "__class__" not in v: return {k: inst(val) for k, val in v.items()}
            mod, name = v["__class__"].rsplit(".", 1)
            return getattr(importlib.import_module(mod), name)(**{k: inst(val) for k, val in v.items() if k != "__class__"})
        # cfg может задать свой подкласс через "__class__" (e.g. "src.agent.claude_agent.ClaudeAgent");
        # если не задал — подставляем cls по умолчанию.
        merged = {"__class__": f"{cls.__module__}.{cls.__name__}", **cfg, **overrides}
        agent = inst(merged)
        agent._config = cfg
        return agent

    def get_agent_dir(self) -> str:
        return self.agent_dir

    async def add_transport_skills(self):
        if not self.transport:
            return
        # TransportSkill — универсальные send_* тулы поверх любого транспорта.
        for skill in [TransportSkill(), *self.transport.get_skills()]:
            if skill not in self.skills:
                skill.register(self)
                await skill.start()
                self.skills.insert(0, skill)

    async def spawn_subagent(self, name: str, **cfg_overrides) -> "Agent":
        if self.agent_dir is None:
            raise RuntimeError("ephemeral agent (agent_dir=None) can't spawn subagents — nowhere to persist them")
        subagent_dir = os.path.join(self.agent_dir, "memory", "subagents", name)
        os.makedirs(subagent_dir, exist_ok=True)
        cfg_overrides.setdefault("transport", self.transport)
        agent = Agent.from_config(self._config, id=f"{self.id}:{name}", agent_dir=subagent_dir, **cfg_overrides)

        sub_skill = SubagentSkill()
        agent.skills.insert(0,sub_skill)
        sub_skill.register(agent)

        await agent.start(run_loop=False)

        # Propagate subagent's exit → parent's stop.
        # Closure captures only Events (not agent) so weakref.finalize
        # can cancel the task when agent is GC'd — no "Task was destroyed but pending" warnings.
        sub_exit = sub_skill.exit_event
        parent_stop = self._stop_event

        async def _propagate_stop():
            await sub_exit.wait()
            parent_stop.set()
        task = asyncio.create_task(_propagate_stop())
        weakref.finalize(agent, task.cancel)
        return agent

    def __init__(self, id: str, model_name: str, api_key: str = "", base_url: str = "", backend: str = "openai", backend_params: dict | None = None, agent_dir: str | None = None, memory_compressor = None, memory_providers: list | dict = None, skills: list = None, max_iterations: int = 20, transcription_model_name: str = "gemini-2.5-flash", transcription_api_key: str = None, transcription_base_url: str = None, transport=None):
        self.id = id
        self.model_name = model_name
        self.api_key = api_key
        self.base_url = base_url
        self.transcription_model_name = transcription_model_name
        self.agent_dir = agent_dir
        if isinstance(memory_providers, dict):
            memory_providers = list(memory_providers.values())
        memory_dir = os.path.join(agent_dir, "memory") if agent_dir else ""
        self.memory = Memory(compressor=memory_compressor, providers=memory_providers or [], memory_dir=memory_dir)
        self.skills = ([memory_compressor] if memory_compressor else []) + self.memory.providers + (skills or []) + [AgentSkill()]
        self.max_iterations = max_iterations
        for skill in self.skills:
            skill.register(self)
        self.transport = transport or BaseTransport()
        self.transport.set_agent(self)

        self.backend = backend
        self.backend_params = backend_params or {}
        if backend == "openai":
            from src.agent.backends.openai import OpenAIBackend
            self.backend_impl = OpenAIBackend(self, base_url=base_url, api_key=api_key, **self.backend_params)
        elif backend == "claude":
            from src.agent.backends.claude import ClaudeBackend
            self.backend_impl = ClaudeBackend(self, **self.backend_params)
        else:
            raise ValueError(f"Unknown backend: {backend!r}")

        self.transcription_client = Agent.OpenAI(transcription_api_key or api_key, transcription_base_url or base_url)
        self._message_queue: asyncio.Queue = asyncio.Queue()
        self._stop_event = asyncio.Event()
        self._restrictions_file = os.path.join(memory_dir, ".restrictions.json") if memory_dir else None
        self._restrictions: dict = self._load_restrictions()
        self._stream_counter: int = 0

    def _load_restrictions(self) -> dict:
        if not self._restrictions_file:
            return {}
        try:
            with open(self._restrictions_file, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def _save_restriction(self, key: str, value):
        self._restrictions[key] = value
        if not self._restrictions_file:
            return
        try:
            with open(self._restrictions_file, "w", encoding="utf-8") as f:
                json.dump(self._restrictions, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.warning("[agent] не удалось сохранить restrictions: %s", e)

    def apply_error_restriction(self, model_name: str, e: Exception, messages: list) -> list:
        """Выставляет ограничение на основе ошибки и возвращает обновлённые messages."""
        err = str(e)
        if "image input" in err and "404" in err:
            logging.warning("[agent] модель %s не поддерживает картинки, сохраняю ограничение", model_name)
            self._save_restriction(f"{model_name}.no_images", True)
            return self.strip_contents_private(messages, model_name)
        return messages

    def stop(self):
        """Прервать текущий ответ. Частичный ответ не сохраняется в историю."""
        self._stop_event.set()

    async def close(self):
        await self.backend_impl.close()

    def strip_contents_private(self, turns: list, model_name: str = None) -> list:
        model = model_name or self.model_name
        no_images = self._restrictions.get(f"{model}.no_images", False)
        result = []
        for t in turns:
            if not isinstance(t, dict):
                result.append(t)
                continue
            ts = ""
            ts_raw = t.get("_timestamp") or ""
            if ts_raw:
                try:
                    ts = datetime.fromisoformat(ts_raw).astimezone().strftime("%Y-%m-%dT%H:%M:%S")
                except ValueError:
                    logging.warning("[agent] invalid _timestamp: %r", ts_raw)
            turn = {k: v for k, v in t.items() if not k.startswith("_")}
            if "parts" in turn:
                turn["parts"] = [
                    {k: v for k, v in p.items() if not k.startswith("_")} if isinstance(p, dict) else p
                    for p in turn["parts"]
                ]
                if ts and turn.get("role") == "user" and any("text" in p for p in turn["parts"] if isinstance(p, dict)):
                    turn["parts"] = [{"text": f"[{ts}]"}] + turn["parts"]
            elif isinstance(turn.get("content"), list):
                blocks = [{k: v for k, v in b.items() if not k.startswith("_")} if isinstance(b, dict) else b
                          for b in turn["content"]]
                if no_images:
                    blocks = [b for b in blocks if not (isinstance(b, dict) and b.get("type") == "image_url")]
                if ts and turn.get("role") == "user":
                    blocks = [{"type": "text", "text": f"[{ts}]"}] + blocks
                turn["content"] = blocks or None
            result.append(turn)
        return result


    async def llm(self, **kwargs):
        return await self.backend_impl.llm(**kwargs)

    def call_before_next_message(self, coro):
        self._message_queue.put_nowait(coro)

    async def next_message(self) -> tuple[list, any, bool]:
        batch = []
        while not batch or not self._message_queue.empty():
            item = await self._message_queue.get()
            if asyncio.iscoroutine(item):
                try:
                    await item
                finally:
                    if self.transport.get_agent() is not self: self.transport.set_agent(self)
            else:
                batch.append(item)
        if len(batch) > 1:
            logging.info("[agent] merging %d queued messages", len(batch))
        content_parts = [p for i, (parts, _, _) in enumerate(batch) for p in ([{"type": "text", "text": "\n"}] if i > 0 else []) + list(parts)]
        user_message_id = next((mid for _, mid, _ in batch if mid is not None), None)
        trigger_answer = any(t for _, _, t in batch)
        user_query = " ".join(p.get("text", "") for p in content_parts if isinstance(p, dict) and "text" in p).strip()
        logging.info("[agent] incoming: %r", user_query)
        return content_parts, user_message_id, trigger_answer

    async def start(self, run_loop=True):
        for skill in self.skills:
            await skill.start()
        if run_loop:
            asyncio.create_task(self.loop())

    async def transcribe_audio(self, data: bytes, mime_type: str) -> str:
        fmt = mime_type.split("/")[-1]
        if fmt not in ("wav", "mp3"):
            audio, sr = sf.read(io.BytesIO(data))
            wav_buf = io.BytesIO()
            sf.write(wav_buf, audio, sr, format='WAV', subtype='PCM_16')
            data, fmt = wav_buf.getvalue(), "wav"
        max_retries, delay = 5, 0.5
        for attempt in range(max_retries):
            try:
                resp = await self.transcription_client.chat.completions.create(
                    model=self.transcription_model_name,
                    messages=[{"role": "user", "content": [
                        {"type": "text", "text": "Transcribe the audio. Return only the transcript text."},
                        {"type": "input_audio", "input_audio": {"data": base64.b64encode(data).decode(), "format": fmt}},
                    ]}],
                )
                return resp.choices[0].message.content or ""
            except Exception as e:
                if attempt + 1 == max_retries: raise
                wait = delay * 2 ** attempt
                logging.warning("[agent] transcribe_audio retry %d/%d in %ds: %s", attempt + 1, max_retries, wait, e)
                await asyncio.sleep(wait)

    async def describe_video(self, data: bytes, mime_type: str) -> str:
        if len(data) > 10 * 1024 * 1024:
            logging.warning("[agent] video >10MB отправляется inline, возможны ошибки")
        max_retries, delay = 5, 0.5
        for attempt in range(max_retries):
            try:
                resp = await self.transcription_client.chat.completions.create(
                    model=self.transcription_model_name,
                    messages=[{"role": "user", "content": [
                        {"type": "text", "text": "Describe the key events in this video, providing both audio and visual details. Include timestamps for salient moments."},
                        {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{base64.b64encode(data).decode()}"}},
                    ]}],
                )
                return resp.choices[0].message.content
            except Exception as e:
                if attempt + 1 == max_retries: raise
                wait = delay * 2 ** attempt
                logging.warning("[agent] describe_video retry %d/%d in %ds: %s", attempt + 1, max_retries, wait, e)
                await asyncio.sleep(wait)

    async def process_message(self, content_parts: list, user_message_id=None, trigger_answer: bool = True):
        user_query = " ".join(p.get("text", "") for p in content_parts if isinstance(p, dict) and "text" in p).strip()
        for skill in self.skills:
            if skill.is_bypass_command(user_query):
                result = await skill.dispatch_bypass(user_query)
                if result:
                    await self.transport.send_message(result)
                return
        await self._message_queue.put((content_parts, user_message_id, trigger_answer))

    async def dispatch_tool_calls(self, turn: dict) -> list[dict]:
        tool_calls = turn.get("tool_calls") or []
        tool_to_skill = {decl["function"]["name"]: skill for skill in self.skills for decl in skill.get_tools()}
        extra_parts = []
        tool_turns = []
        for fc in tool_calls:
            name = fc["function"]["name"]
            args = json.loads(fc["function"].get("arguments") or "{}")
            logging.info("Инструмент: %s", name)
            skill = tool_to_skill.get(name)
            if not skill:
                logging.warning("Tool %s not found in skills", name)
                tool_turns.append({"role": "tool", "tool_call_id": fc["id"], "name": name,
                                   "content": json.dumps({"error": f"Tool {name} not found"})})
                continue

            await self.transport.on_tool_call(name, args)
            try: 
                result = await skill.dispatch_tool_call(fc)
            finally: 
                if self.transport.get_agent() is not self: self.transport.set_agent(self)
            await self.transport.on_tool_result(name, result)
            extra_parts.extend(result.pop("_parts", []) if isinstance(result, dict) else [])
            tool_turns.append({
                "role": "tool", "tool_call_id": fc["id"], "name": name,
                "content": json.dumps(result if isinstance(result, dict) else {"result": result}, ensure_ascii=False),
            })

        if extra_parts:
            tool_turns.append({"role": "user", "content": extra_parts})
        return tool_turns


    async def loop(self):
        await self.add_transport_skills()
        try:
            while True:
                self._stop_event.clear()
                async def handle_turn():
                    content_parts, user_message_id, trigger_answer = await self.next_message()
                    try:
                        await self.memory.add_turn({"role": "user", "content": content_parts, "_user_message_id": user_message_id})
                        if not trigger_answer: return

                        await self.transport.send_processing(True)
                        if self.memory.memory_dir:
                            open(os.path.join(self.memory.memory_dir, "last_turn_chunks.log"), "w").close()
                        result = await self.llm()
                        if isinstance(result, list): # llm сам отработал тулы внутри
                            await self.memory.add_turn(*result)
                        else:
                            turn = result
                            iteration = 0
                            while turn.get("tool_calls"):
                                if iteration >= self.max_iterations:
                                    logging.warning("[agent] max_iterations=%d reached", self.max_iterations)
                                    await self.transport.send_message(f"⚠️ Достигнут лимит итераций ({self.max_iterations}). Ответ может быть неполным.")
                                    break
                                result_turns = await self.dispatch_tool_calls(turn)
                                await self.memory.add_turn(turn, *result_turns)
                                iteration += 1
                                turn = await self.llm()
                            else:
                                await self.memory.add_turn(turn)
                                
                    except Exception as e:
                        logging.warning("Ошибка агента: %s", e, exc_info=True)
                        await self.transport.send_message(f"Ошибка: {e}")
                    finally:
                        await self.transport.send_processing(False)

                await stoppable(handle_turn(), self._stop_event)
        finally:
            await self.close()
