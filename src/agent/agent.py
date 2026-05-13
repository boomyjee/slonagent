import asyncio, base64, glob, io, json, os, logging, tempfile, weakref
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

def _load_config() -> dict:
    """Eager-load .config.json при импорте модуля + применение env-блока.
    Возвращает пустой dict если файла нет (e.g. тесты в чистой папке)."""
    cfg = {}
    if os.path.exists(".config.json"):
        with open(".config.json", encoding="utf-8") as f:
            cfg = json.load(f)
    os.environ.update(cfg.get("env", {}))
    return cfg


class Agent:
    # Process-wide реестр агентов: один (agent_id, thread_id) — один Agent.
    # Заполняется через Agent.get; чистится в Agent.close.
    _instances: dict[tuple[str, str], "Agent"] = {}
    # Корневой .config.json — доступен отовсюду как Agent.config.
    config: dict = _load_config()
    # Фабрика транспорта для агентов из Agent.get. Ставит main.py на старте.
    # `() -> BaseTransport`. None → агенту достанется голый BaseTransport.
    transport_factory = None

    @staticmethod
    def _resolve_refs(value, root: dict):
        """`$path.in.config` → object из root."""
        if isinstance(value, str) and value.startswith("$"):
            obj = root
            for part in value[1:].split("."):
                obj = obj[part]
            return obj
        if isinstance(value, dict):
            return {k: Agent._resolve_refs(v, root) for k, v in value.items()}
        if isinstance(value, list):
            return [Agent._resolve_refs(v, root) for v in value]
        return value

    @classmethod
    async def get(cls, agent_id: str, thread_id: str = "", force_create: bool = False, copy_memory_from: "Agent | None" = None):
        """Получить (или создать-и-закешировать) агента для (agent_id, thread_id).

        Конвенция: main-агент в cwd, fork-агенты в forks/<agent_id>/. fork-config
        синтезируется из root[agent] + root[fork_agent] на первом обращении.
        Транспорт берётся из cls.transport_factory (None → агент без транспорта)."""
        key = (agent_id, thread_id)
        if not force_create and key in cls._instances:
            return cls._instances[key]

        is_main = (agent_id == "main")
        agent_dir = os.getcwd() if is_main else os.path.join(os.getcwd(), "forks", agent_id)
        if not force_create and not os.path.exists(agent_dir):
            return None

        config_path = os.path.join(agent_dir, ".config.json")
        if is_main:
            agent_cfg = cls.config["agent"]
        else:
            if not os.path.exists(config_path):
                from src.skills.config import _format_json
                os.makedirs(agent_dir, exist_ok=True)
                merged = {**cls.config["agent"],
                          **cls._resolve_refs(cls.config.get("fork_agent", {}), cls.config)}
                fork_config = {}
                if "sandbox" in cls.config:
                    fork_config["sandbox"] = cls.config["sandbox"]
                fork_config["agent"] = merged
                with open(config_path, "w", encoding="utf-8") as f:
                    f.write(_format_json(fork_config))
            with open(config_path, encoding="utf-8") as f:
                agent_cfg = json.load(f)["agent"]

        transport = cls.transport_factory() if cls.transport_factory else None
        agent = cls.from_config(cls._resolve_refs(agent_cfg, cls.config),
                                id=agent_id, thread_id=thread_id,
                                agent_dir=agent_dir, transport=transport)
        if copy_memory_from is not None:
            agent.memory.copy_from(copy_memory_from.memory)
        await agent.start()
        cls._instances[key] = agent
        return agent

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
        # По умолчанию subagent персистится под parent'ом. Caller может явно
        # передать agent_dir=None чтобы сделать эфемерного subagent, или свой путь.
        if "agent_dir" not in cfg_overrides:
            if self.agent_dir is None:
                raise RuntimeError(
                    "parent — эфемерный (agent_dir=None), не могу сам вычислить путь "
                    "под subagent. Передай agent_dir явно (None для эфемерного, путь — для персистентного)."
                )
            cfg_overrides["agent_dir"] = os.path.join(self.agent_dir, "memory", "subagents", name)
        cfg_overrides.setdefault("transport", self.transport)
        agent = Agent.from_config(self._config, id=f"{self.id}:{name}", **cfg_overrides)

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

    def __init__(self, id: str, model_name: str, api_key: str = "", base_url: str = "", backend: str = "openai", backend_params: dict | None = None, agent_dir: str | None = None, thread_id: str = "", memory_compressor = None, memory_providers: list | dict = None, skills: list = None, max_iterations: int = 20, transcription_model_name: str = "gemini-2.5-flash", transcription_api_key: str = None, transcription_base_url: str = None, transcription_whisper: str = "", transcription_whisper_language: str = "ru", transport=None):
        self.id = id
        self.thread_id = thread_id
        self.model_name = model_name
        self.api_key = api_key
        self.base_url = base_url
        self.transcription_model_name = transcription_model_name
        self.agent_dir = agent_dir
        if isinstance(memory_providers, dict):
            memory_providers = list(memory_providers.values())
        memory_dir = os.path.join(agent_dir, "memory") if agent_dir else ""
        self.memory = Memory(compressor=memory_compressor, providers=memory_providers or [], memory_dir=memory_dir, thread_id=thread_id)
        self.skills = self.memory.providers + (skills or []) + [AgentSkill()]
        from src.skills.sandbox import SandboxSkill
        self.sandbox = next((s for s in self.skills if isinstance(s, SandboxSkill)), None)
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
        elif backend == "echo":
            from src.agent.backends.echo import EchoBackend
            self.backend_impl = EchoBackend(self, **self.backend_params)
        else:
            raise ValueError(f"Unknown backend: {backend!r}")

        self.transcription_client = Agent.OpenAI(transcription_api_key or api_key, transcription_base_url or base_url)
        self.transcription_whisper = transcription_whisper
        self.transcription_whisper_language = transcription_whisper_language
        self._message_queue: asyncio.Queue = asyncio.Queue()
        self._stop_event = asyncio.Event()
        self._restrictions_file = os.path.join(memory_dir, ".restrictions.json") if memory_dir else None
        self._restrictions: dict = self._load_restrictions()
        self.thread_ensure(self.thread_id)
        self.memory.anonymous = self._load_threads().get(self.thread_id, {}).get("anonymous", False)

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

    def _threads_file(self) -> str | None:
        return os.path.join(self.memory.memory_dir, "THREADS.json") if self.memory.memory_dir else None

    def _load_threads(self) -> dict:
        if not (f := self._threads_file()): return {}
        try: 
            with open(f, encoding="utf-8") as fh: return json.load(fh)
        except FileNotFoundError: return {}
        except Exception as e: 
            logging.warning("[agent] threads load failed: %s", e)
            return {}

    def _save_threads(self, threads: dict):
        f = self._threads_file()
        if not f: return
        try:
            with open(f, "w", encoding="utf-8") as fh:
                json.dump(threads, fh, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.warning("[agent] threads save failed: %s", e)

    def thread_name(self, uuid: str) -> str | None:
        return self._load_threads().get(uuid, {}).get("name")

    def thread_list(self) -> dict:
        return self._load_threads()

    def thread_ensure(self, uuid: str):
        if not self.memory.memory_dir: return
        threads = self._load_threads()
        if uuid in threads: return
        threads[uuid] = {"name": ""}
        self._save_threads(threads)

    async def thread_rename(self, uuid: str, name: str):
        threads = self._load_threads()
        if threads.get(uuid, {}).get("name") == name: return
        threads.setdefault(uuid, {})["name"] = name
        self._save_threads(threads)
        await self.transport.thread_rename(uuid, name)

    async def thread_delete(self, uuid: str):
        if not self.memory.memory_dir: return
        target = await Agent.get(self.id, uuid)
        if target is not None:
            await target.close()
            target.memory.delete()
        threads = self._load_threads()
        threads.pop(uuid, None)
        self._save_threads(threads)
        await self.transport.thread_delete(uuid)

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
        await self.transport.close()
        await self.backend_impl.close()
        Agent._instances.pop((self.id, self.thread_id), None)

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

    @staticmethod
    def turn_text(turn) -> str:
        """Извлекает текст ассистента из результата llm() — нормализует разные форматы:
          - openai-бэкенд возвращает один dict {role, content, ...}
          - claude-бэкенд возвращает list[turn] (по блоку на запись), сшиваем content всех assistant'ов.
        """
        if isinstance(turn, list):
            parts = [t.get("content") for t in turn
                     if isinstance(t, dict) and t.get("role") == "assistant"
                     and isinstance(t.get("content"), str)]
            return "\n".join(parts).strip()
        if isinstance(turn, dict):
            return (turn.get("content") or "").strip()
        return ""

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
        if self.transcription_whisper:
            # Локальный whisper.cpp через subprocess. Модель ищем неявно: предпочитаем
            # large-v3-turbo, иначе первый ggml-*.bin. Whisper-cpp без FFmpeg не
            # читает OGG/Opus — конвертируем в WAV через soundfile.
            cli = os.path.join(self.transcription_whisper, "whisper-cli.exe")
            if not os.path.exists(cli):
                raise RuntimeError(f"whisper-cli.exe not found in {self.transcription_whisper}")
            model = os.path.join(self.transcription_whisper, "ggml-large-v3-turbo.bin")
            if not os.path.exists(model):
                cands = glob.glob(os.path.join(self.transcription_whisper, "ggml-*.bin"))
                if not cands:
                    raise RuntimeError(f"no ggml-*.bin in {self.transcription_whisper}")
                model = cands[0]
            if fmt not in ("wav", "mp3"):
                audio, sr = sf.read(io.BytesIO(data))
                wav_buf = io.BytesIO()
                sf.write(wav_buf, audio, sr, format='WAV', subtype='PCM_16')
                data, fmt = wav_buf.getvalue(), "wav"
            with tempfile.TemporaryDirectory() as td:
                inp = os.path.join(td, f"audio.{fmt}")
                with open(inp, "wb") as f:
                    f.write(data)
                out_base = os.path.join(td, "out")
                args = [cli, "-m", model, "-t", "8", "-otxt", "-of", out_base, "-f", inp]
                if self.transcription_whisper_language:
                    args += ["-l", self.transcription_whisper_language]
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                )
                _, err = await proc.communicate()
                if proc.returncode != 0:
                    raise RuntimeError(f"whisper-cli failed ({proc.returncode}): {err.decode(errors='replace')[:500]}")
                txt_path = out_base + ".txt"
                if not os.path.exists(txt_path):
                    raise RuntimeError(f"whisper-cli produced no output: {err.decode(errors='replace')[:500]}")
                with open(txt_path, encoding="utf-8") as f:
                    text = f.read().strip()
                await self.transport.send_message(f"🎤 {text}")
                return text
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

    async def dispatch_tool_calls(self, turn: dict, emit_transport_events: bool = True) -> list[dict]:
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

            if emit_transport_events:
                await self.transport.on_tool_call(name, args)
            try:
                result = await skill.dispatch_tool_call(fc)
            finally:
                if self.transport.get_agent() is not self: self.transport.set_agent(self)
            if emit_transport_events:
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
