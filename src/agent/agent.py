import asyncio, base64, io, json, os, logging, re, weakref
import numpy as np
import soundfile as sf
from datetime import datetime
import httpx
from openai.lib.streaming.chat import ChatCompletionStreamState
from src.memory.memory import Memory
from src.agent.agent_skill import AgentSkill


class BadFinishReason(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"LLM finished with: {reason}")


async def stoppable(coro, stop_event: asyncio.Event):
    task = asyncio.create_task(coro)
    stop_task = asyncio.create_task(stop_event.wait())
    try:
        await asyncio.wait({task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stop_task.cancel()
        if not task.done():
            task.cancel()
            try:
                await task
            except BaseException:
                pass
    if task.cancelled() or stop_task.done() and not stop_task.cancelled():
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
        agent = cls(**{**inst(cfg), **overrides})
        agent._config = cfg
        return agent

    def add_transport_skills(self):
        if not self.transport:
            return
        for skill in self.transport.get_skills():
            if skill not in self.skills:
                skill.register(self)
                self.skills.insert(0, skill)

    async def spawn_subagent(self, name: str, **cfg_overrides) -> "Agent":
        subagent_dir = os.path.join(self.agent_dir, "memory", "subagents", name)
        os.makedirs(subagent_dir, exist_ok=True)
        cfg_overrides.setdefault("transport", self.transport)
        agent = Agent.from_config(self._config, id=f"{self.id}:{name}", agent_dir=subagent_dir, **cfg_overrides)
        await agent.start(run_loop=False)

        # Propagate subagent's stop → parent's tool_stop.
        # Closure captures only Events (not agent) so weakref.finalize
        # can cancel the task when agent is GC'd — no "Task was destroyed but pending" warnings.
        stop_event = agent._stop_event
        parent_tool_stop = self._tool_stop_event

        async def _propagate_stop():
            await stop_event.wait()
            parent_tool_stop.set()
        task = asyncio.create_task(_propagate_stop())
        weakref.finalize(agent, task.cancel)

        return agent

    def __init__(self, id: str, model_name: str, api_key: str, base_url: str, agent_dir: str, memory_compressor = None, memory_providers: list | dict = None, skills: list = None, max_iterations: int = 20, transcription_model_name: str = "gemini-2.5-flash", transcription_api_key: str = None, transcription_base_url: str = None, transport=None):
        self.id = id
        self.model_name = model_name
        self.api_key = api_key
        self.transcription_model_name = transcription_model_name
        self.agent_dir = agent_dir
        if isinstance(memory_providers, dict):
            memory_providers = list(memory_providers.values())
        memory_dir = os.path.join(agent_dir, "memory")
        self.memory = Memory(compressor=memory_compressor, providers=memory_providers or [], memory_dir=memory_dir)
        self.skills = ([memory_compressor] if memory_compressor else []) + self.memory.providers + (skills or []) + [AgentSkill()]
        self.max_iterations = max_iterations
        for skill in self.skills:
            skill.register(self)
        self.transport = transport
        if transport:
            transport.set_agent(self)

        self.client = Agent.OpenAI(api_key, base_url)
        self.transcription_client = Agent.OpenAI(transcription_api_key or api_key, transcription_base_url or base_url)
        self._message_queue: asyncio.Queue = asyncio.Queue()
        self._stop_event = asyncio.Event()
        self._tool_stop_event = asyncio.Event()
        self._restrictions_file = os.path.join(memory_dir, ".restrictions.json")
        self._restrictions: dict = self._load_restrictions()
        self._current_content_parts: list = []
        self._system_instruction: str | None = None
        self._stream_counter: int = 0

    def _load_restrictions(self) -> dict:
        try:
            with open(self._restrictions_file, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def _save_restriction(self, key: str, value):
        self._restrictions[key] = value
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


    async def llm(self, tool_choice: str = None, parallel_tool_calls: bool = None):
        if self._system_instruction is None:
            user_query = " ".join(p.get("text", "") for p in self._current_content_parts if isinstance(p, dict) and "text" in p).strip()
            system_parts = []
            for skill in self.skills:
                skill_context = await skill.get_context_prompt(user_query)
                if skill_context:
                    system_parts.append(skill_context)
                    await self.transport.send_system_prompt(f"[{skill.__class__.__name__}]\n{skill_context}")
            self._system_instruction = "\n\n".join(system_parts)
            self._tools = []
            for skill in self.skills:
                for decl in skill.get_tools():
                    fn = decl["function"]
                    extra = await skill.get_tool_prompt(fn["name"])
                    if extra:
                        decl = {"type": "function", "function": {**fn, "description": fn["description"] + "\n\n---\n" + extra}}
                    self._tools.append(decl)
            if self._tools:
                tool_lines = [f"{d['function']['name']}: {d['function']['description'].splitlines()[0][:100]}" for d in self._tools]
                await self.transport.send_system_prompt("[Tools]\n" + "\n".join(tool_lines))
        contents = self.strip_contents_private(await self.memory.get_contents())
        tools = self._tools
        messages = ([{"role": "system", "content": self._system_instruction}] if self._system_instruction else []) + contents
        logging.info("[agent] → LLM %s", self.model_name)
        max_retries, delay = 5, 0.5
        for attempt in range(max_retries):
            try:
                kwargs: dict = {"model": self.model_name, "messages": messages, "stream": True, "temperature": 1.0}
                if tools:
                    kwargs["tools"] = tools
                    if tool_choice in ("auto", "required"):
                        kwargs["tool_choice"] = tool_choice
                    elif tool_choice:
                        kwargs["tool_choice"] = {"type": "function", "function": {"name": tool_choice}}
                    if parallel_tool_calls is not None:
                        kwargs["parallel_tool_calls"] = parallel_tool_calls
                kwargs["extra_body"] = {"extra_body": {"google": {"thinking_config": {"include_thoughts": True}}}}

                stream = await self.client.chat.completions.create(**kwargs)

                state = ChatCompletionStreamState()
                display_text = ""
                display_thinking = ""
                thinking_id = self._stream_counter = self._stream_counter + 1
                stream_id = self._stream_counter = self._stream_counter + 1
                is_thought = False
                tc_counter = 0
                seen = {}

                async for chunk in stream:
                    # openai accumulate_delta склеивает строки и суммирует числа (bool
                    # наследуется от int!). Gemini шлёт role="assistant" в каждом чанке
                    # и thought=true в каждом thinking-чанке — в итоге role становится
                    # "assistantassistant...", thought становится 2/3/... Оба поля
                    # оставляем из первого чанка, из остальных — вычищаем.
                    if chunk.choices:
                        delta = chunk.choices[0].delta
                        for tc in delta.tool_calls or ():
                            if tc.index is None:
                                tc.index = tc_counter
                                tc_counter += 1
                        if delta.role:
                            if "role" in seen: delta.role = None
                            seen["role"] = True
                        google = ((delta.model_extra or {}).get("extra_content") or {}).get("google") or {}
                        if google.get("thought"):
                            if "thought" in seen: google.pop("thought")
                            seen["thought"] = True

                    state.handle_chunk(chunk)
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta

                    delta_extra = getattr(delta, "model_extra", None) or {}
                    thought_extra = delta_extra.get("reasoning_content") or delta_extra.get("reasoning")
                    if thought_extra and isinstance(thought_extra, str):
                        display_thinking += thought_extra
                        await self.transport.send_thinking(display_thinking, thinking_id)

                    # Gemini заворачивает мысли в литералы <thought>...</thought> прямо в content
                    if content := delta.content or "":
                        text = ""
                        if "<thought>" in content: is_thought = True
                        if is_thought:
                            thought, *rest = content.removeprefix("<thought>").split("</thought>")
                            if rest:
                                is_thought = False
                                text = rest[0]
                            display_thinking += thought
                            await self.transport.send_thinking(display_thinking, thinking_id, final=not is_thought)
                        else:
                            text = content
                            
                        if text:
                            display_text += text
                            await self.transport.send_message(display_text, stream_id, final=False)

                if display_thinking and not display_text:
                    await self.transport.send_thinking(display_thinking, thinking_id, final=True)
                if display_text:
                    await self.transport.send_message(display_text, stream_id, final=True)

                final = state.get_final_completion()
                finish_reason = final.choices[0].finish_reason if final.choices else None
                if finish_reason and finish_reason not in ("stop", "tool_calls"):
                    raise BadFinishReason(finish_reason)

                turn = final.choices[0].message.model_dump(exclude_none=True)
                if turn.get("content"):
                    turn["content"] = re.sub(r"<thought>.*?</thought>", "", turn["content"], flags=re.DOTALL).strip() or None

                for tc in turn.get("tool_calls") or ():
                    tc.pop("index", None)
                    logging.info("[stream] function_call: %s", tc["function"]["name"])

                logging.info("[agent] ← LLM %s", self.model_name)
                return turn
            except BadFinishReason:
                raise
            except Exception as e:
                messages = self.apply_error_restriction(self.model_name, e, messages)
                if attempt + 1 == max_retries:
                    raise
                wait = delay * 2 ** attempt
                logging.warning("[agent] LLM %s error, retry %d/%d in %ds: %s", self.model_name, attempt + 1, max_retries, wait, e)
                await asyncio.sleep(wait)

    async def next_message(self) -> tuple[list, any, bool]:
        content_parts, user_message_id, trigger_answer = await self._message_queue.get()
        batch = []
        while not self._message_queue.empty():
            batch.append(self._message_queue.get_nowait())
        if batch:
            logging.info("[agent] merging %d queued messages", len(batch))
            all_items = [(content_parts, user_message_id, trigger_answer)] + batch
            content_parts = [p for i, (parts, _, _) in enumerate(all_items) for p in ([{"type": "text", "text": "\n"}] if i > 0 else []) + list(parts)]
            user_message_id = next((mid for _, mid, _ in all_items if mid is not None), None)
            trigger_answer = any(t for _, _, t in all_items)
        self._current_content_parts = content_parts
        self._system_instruction = None
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
            self._tool_stop_event.clear()
            result = await stoppable(skill.dispatch_tool_call(fc), self._tool_stop_event)
            if result is None:
                result = {"error": "прервано пользователем"}
            if self.transport.agent is not self:
                self.transport.set_agent(self)
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
        self.add_transport_skills()
        while True:
            content_parts, user_message_id, trigger_answer = await self.next_message()
            self._stop_event.clear()

            async def run_message():
                try:
                    await self.memory.add_turn({"role": "user", "content": content_parts, "_user_message_id": user_message_id})
                    if not trigger_answer: return

                    await self.transport.send_processing(True)
                    turn = await self.llm()
                    iteration = 0
                    while turn.get("tool_calls") and iteration < self.max_iterations:
                        result_turns = await self.dispatch_tool_calls(turn)
                        await self.memory.add_turn(turn, *result_turns)
                        iteration += 1
                        turn = await self.llm()
                    if turn.get("tool_calls"):
                        logging.warning("[agent] max_iterations=%d reached", self.max_iterations)
                        await self.transport.send_message(f"⚠️ Достигнут лимит итераций ({self.max_iterations}). Ответ может быть неполным.")
                    else:
                        await self.memory.add_turn(turn)
                except Exception as e:
                    logging.warning("Ошибка агента: %s", e, exc_info=True)
                    await self.transport.send_message(f"Ошибка: {e}")
                finally:
                    await self.transport.send_processing(False)

            await stoppable(run_message(), self._stop_event)
