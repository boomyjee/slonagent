import html
import base64, io, os, re, asyncio, logging, json, mimetypes, uuid

log = logging.getLogger(__name__)
from typing import Annotated
from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramMigrateToChat, TelegramRetryAfter, TelegramServerError
from aiogram.types import Message, CallbackQuery, FSInputFile, InputMediaPhoto, InputMediaDocument, LinkPreviewOptions, MessageOriginUser, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand, BotCommandScopeChat
from agent import Skill, tool
from src.transport.base import BaseTransport


def _markdown_to_html(text: str) -> str:
    """Convert normal markdown to Telegram-safe HTML."""
    if not text:
        return ""

    # 1. Extract and protect code blocks
    code_blocks: list[str] = []
    def save_code_block(m: re.Match) -> str:
        code_blocks.append(m.group(1))
        return f"\x00CB{len(code_blocks) - 1}\x00"
    text = re.sub(r'```[\w]*\n?([\s\S]*?)```', save_code_block, text)

    # 2. Extract and protect inline code
    inline_codes: list[str] = []
    def save_inline_code(m: re.Match) -> str:
        inline_codes.append(m.group(1))
        return f"\x00IC{len(inline_codes) - 1}\x00"
    text = re.sub(r'`([^`]+)`', save_inline_code, text)

    # 3. Headers → plain text
    text = re.sub(r'^#{1,6}\s+(.+)$', r'\1', text, flags=re.MULTILINE)

    # 4. Blockquotes > text → plain text
    text = re.sub(r'^>\s*(.*)$', r'\1', text, flags=re.MULTILINE)

    # 5. Escape HTML special characters
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # 6. Links [text](url)
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', text)

    # 7. Bold **text** or __text__
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'__(.+?)__', r'<b>\1</b>', text)

    # 8. Italic *text* or _text_ (avoid **bold** and snake_case)
    text = re.sub(r'(?<!\*)\*([^*\n]+)\*(?!\*)', r'<i>\1</i>', text)
    text = re.sub(r'(?<![a-zA-Z0-9])_([^_]+)_(?![a-zA-Z0-9])', r'<i>\1</i>', text)

    # 9. Strikethrough ~~text~~
    text = re.sub(r'~~(.+?)~~', r'<s>\1</s>', text)

    # 10. Bullet lists - item / * item → • item
    text = re.sub(r'^[-*]\s+', '• ', text, flags=re.MULTILINE)

    # 11. Restore inline code
    for i, code in enumerate(inline_codes):
        escaped = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = text.replace(f"\x00IC{i}\x00", f"<code>{escaped}</code>")

    # 12. Restore code blocks
    for i, code in enumerate(code_blocks):
        escaped = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = text.replace(f"\x00CB{i}\x00", f"<pre><code>{escaped}</code></pre>")

    return text


def _split_message_to_html(content: str, converter, max_len: int = 4096) -> list[str]:
    """Split content into html chunks ≤ max_len, preferring line breaks over word breaks.
    """
    chunks: list[str] = []
    while content:
        converted = converter(content)

        if len(converted) <= max_len:
            chunks.append(converted)
            break

        limit = max_len
        step = 100

        while True:
            cut = content[:limit]
            pos = cut.rfind('\n')
            if pos == -1:
                pos = cut.rfind(' ')
            if pos == -1:
                pos = limit

            converted = converter(content[:pos])
            if len(converted) <= max_len or limit <= step:
                chunks.append(converted)
                content = content[pos:].lstrip()
                break
            else:
                limit -= step

    return chunks


class TelegramSkill(Skill):
    def __init__(self, transport: "TelegramTransport"):
        self.transport = transport
        self._chat_title: str = ""
        self._is_private: bool = False
        self.sandbox = None
        super().__init__()

    async def start(self):
        from src.skills.sandbox import SandboxSkill
        self.sandbox = next((s for s in self.agent.skills if isinstance(s, SandboxSkill)), None)
        t = self.transport
        if t.chat_id is None:
            return
        try:
            chat = await t.bot.get_chat(t.chat_id)
            self._chat_title = chat.title or chat.full_name or str(t.chat_id)
            self._is_private = chat.type == "private"
        except Exception as e:
            logging.warning("[TelegramSkill] не удалось получить инфо о чате: %s", e)

    async def get_context_prompt(self, user_text: str = "") -> str:
        t = self.transport
        if not self._chat_title and t.chat_id is not None:
            try:
                chat = await t.bot.get_chat(t.chat_id)
                self._chat_title = chat.title or chat.full_name or str(t.chat_id)
                self._is_private = chat.type == "private"
            except Exception as e:
                logging.warning("[TelegramSkill] не удалось получить инфо о чате: %s", e)
                return ""
        if not self._chat_title:
            return ""
        if self._is_private:
            return f"Ты работаешь в личном чате с {self._chat_title}."
        if t.thread_id:
            topic = t.thread_name or f"#{t.thread_id}"
            return f"Ты работаешь в группе «{self._chat_title}», топик «{topic}»."
        return f"Ты работаешь в группе «{self._chat_title}»."

    @tool(lambda self: (
        "Скачать файл, отправленный пользователем. "
        "dest_path — путь внутри контейнера (напр. /workspace/file.jpg)."
        if self.sandbox
        else "Скачать файл, отправленный пользователем. "
        "dest_path — абсолютный путь на хост-машине."
    ))
    async def download_file(
        self,
        tg_file_id: Annotated[str, "tg_file_id из метаданных прикреплённого файла."],
        dest_path: Annotated[str, "Путь назначения для сохранения файла."],
    ):
        if self.sandbox:
            host_dest = self.sandbox.resolve_path(dest_path)
            if host_dest is None:
                return {"error": f"Путь запрещён или недоступен: {dest_path}"}
        else:
            host_dest = os.path.abspath(dest_path)

        dest_dir = os.path.dirname(host_dest)
        if not os.path.isdir(dest_dir):
            return {"error": f"Директория не существует: {dest_dir}"}

        tg_file = await self.transport.bot.get_file(tg_file_id)
        await self.transport.bot.download_file(tg_file.file_path, host_dest)
        logging.info("[skill] download_file: %s → %s", tg_file_id, host_dest)
        return {"status": "ok", "saved_to": dest_path}


class TelegramTransport(BaseTransport):
    bot: Bot = None
    _main_chat_id: int | None = None  # ставится в start() из allowed_user_ids[0]
    _BINDINGS_PATH = os.path.join("forks", "telegram.json")

    def __init__(self, verbose: bool = True):
        super().__init__()
        self.chat_id: int | None = None
        self.thread_id: int | None = None
        self.thread_name: str | None = None
        self.agent = None
        self._no_link_preview = LinkPreviewOptions(is_disabled=True)
        self.verbose = verbose
        self._tool_msg = None
        self._tool_call_text = ""
        self._pending_messages: list[Message] = []
        self._flush_task: asyncio.Task | None = None
        self._typing_task: asyncio.Task | None = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._queue_task: asyncio.Task | None = None
        self._update_commands_task: asyncio.Task | None = None
        self._last_edit_texts: dict[int, str] = {}  # msg_id → last sent text
        self._stream_messages: dict[int, list] = {}  # stream_id → list of Message
        self._suggestion_options: dict[int, list[str]] = {}  # msg_id → options для длинных кнопок

    @classmethod
    def _migrate_chat(cls, old: int, new: int) -> None:
        b = cls.load_bindings()
        if str(old) in b:
            b[str(new)] = b.pop(str(old))
            cls.save_bindings(b)
        if cls._main_chat_id == old:
            cls._main_chat_id = new
        log.info("[telegram] migrated chat %s → %s", old, new)

    _topic_warned: set[tuple] = set()

    async def _ensure_chat_and_topic(self) -> bool:
        """True — можно слать. False — у агента нет группы или не удалось создать топик."""
        if self.chat_id is None:
            self.chat_id, self.thread_id = self.reverse_binding(self.agent.id, self.agent.thread_id)
        if self.chat_id is None:
            self.chat_id = self.find_chat_for_agent(self.agent.id)
            if self.chat_id is None:
                return False
        if self.thread_id is not None or self.agent.thread_id == "":
            return True
        name = self.agent.thread_name(self.agent.thread_id) or f"thread {self.agent.thread_id[:8]}"
        try:
            topic = await self.bot.create_forum_topic(self.chat_id, name=name)
        except TelegramMigrateToChat as e:
            # Юзер включил Topics → группа стала супергруппой с новым chat_id.
            self._migrate_chat(self.chat_id, e.migrate_to_chat_id)
            self.chat_id = e.migrate_to_chat_id
            return await self._ensure_chat_and_topic()
        except Exception as e:
            key = (self.chat_id, self.agent.thread_id)
            if key not in self._topic_warned:
                self._topic_warned.add(key)
                log.warning("create_forum_topic failed: %s", e)
                if self.agent and self.agent.transport:
                    asyncio.create_task(self.agent.transport.send_message(
                        f"⚠️ Telegram: не удалось создать топик «{self.agent.thread_id[:8]}» — {e}"
                    ))
            return False
        self.thread_id = topic.message_thread_id
        self.thread_name = name
        b = self.load_bindings()
        chat = b.setdefault(str(self.chat_id), {"topics": {}})
        chat.setdefault("topics", {})[str(self.thread_id)] = self.agent.thread_id
        self.save_bindings(b)
        return True

    async def send_processing(self, active: bool):
        if not await self._ensure_chat_and_topic(): return
        if active:
            if self._typing_task:
                return
            async def _typing_loop():
                try:
                    while True:
                        try:
                            await self.bot.send_chat_action(chat_id=self.chat_id, action="typing", message_thread_id=self.thread_id)
                        except TelegramRetryAfter as e:
                            log.warning("typing flood, waiting %s sec", e.retry_after)
                            await asyncio.sleep(e.retry_after)
                        except Exception as e:
                            log.warning("typing action failed: %s", e)
                        await asyncio.sleep(4)
                except asyncio.CancelledError:
                    pass
            self._typing_task = asyncio.create_task(_typing_loop())
        else:
            if self._typing_task:
                self._typing_task.cancel()
                self._typing_task = None

    def get_skills(self):
        return [TelegramSkill(self)]

    async def send_images(self, paths):
        if not await self._ensure_chat_and_topic(): return
        if len(paths) == 1:
            await self.bot.send_photo(self.chat_id, FSInputFile(paths[0]),
                                      message_thread_id=self.thread_id)
            return
        # Telegram album: 10 max per call.
        for chunk in [paths[i:i+10] for i in range(0, len(paths), 10)]:
            media = [InputMediaPhoto(media=FSInputFile(p)) for p in chunk]
            await self.bot.send_media_group(self.chat_id, media,
                                            message_thread_id=self.thread_id)

    async def send_files(self, paths):
        if not await self._ensure_chat_and_topic(): return
        if len(paths) == 1:
            await self.bot.send_document(self.chat_id, FSInputFile(paths[0]),
                                         message_thread_id=self.thread_id)
            return
        for chunk in [paths[i:i+10] for i in range(0, len(paths), 10)]:
            media = [InputMediaDocument(media=FSInputFile(p)) for p in chunk]
            await self.bot.send_media_group(self.chat_id, media,
                                            message_thread_id=self.thread_id)

    async def send_voice(self, audio_path):
        if not await self._ensure_chat_and_topic(): return
        await self.bot.send_voice(self.chat_id, FSInputFile(audio_path),
                                  message_thread_id=self.thread_id)

    # Первый emoji в опции — приклеиваем к номеру кнопки для узнаваемости.
    _EMOJI_RE = re.compile(
        r"[\U0001F300-\U0001F9FF\U0001FA70-\U0001FAFF"
        r"\U00002600-\U000026FF\U00002700-\U000027BF]"
    )

    async def send_suggestions(self, text, options):
        if not await self._ensure_chat_and_topic(): return
        # Универсальный layout: варианты в теле сообщения нумерованным списком,
        # кнопки — короткие цифровые лейблы (опционально + emoji из опции).
        # До 8 кнопок в ряд (Telegram-лимит) — короткие лейблы влезают всегда.
        lines = [f"{i + 1}. {o}" for i, o in enumerate(options)]
        body = (text + "\n\n" if text else "") + "\n".join(lines)

        def _label(i, opt):
            m = self._EMOJI_RE.search(opt)
            return f"{i + 1} {m.group(0)}" if m else str(i + 1)

        buttons = [
            InlineKeyboardButton(text=_label(i, o), callback_data=f"answer_idx:{i}")
            for i, o in enumerate(options)
        ]
        rows = [buttons[i:i + 8] for i in range(0, len(buttons), 8)]
        kb = InlineKeyboardMarkup(inline_keyboard=rows)

        msg = await self._send(_markdown_to_html(body), parse_mode="HTML", reply_markup=kb)
        if msg is not None:
            self._suggestion_options[msg.message_id] = list(options)

    async def thread_rename(self, uuid: str, name: str):
        # Main thread агента (uuid="") маппится на General топик группы.
        # Отдельный API метод edit_general_forum_topic, и в bindings.topics
        # General не лежит — итерим по чатам где этот агент привязан.
        if uuid == "":
            if self.agent is None:
                return
            for chat_str, chat in self.load_bindings().items():
                if chat.get("agent_id") != self.agent.id:
                    continue
                # Личка у main-агента — chat_id положительный, в ней нет
                # General-топика. edit_general_forum_topic тут вернёт ошибку.
                if int(chat_str) >= 0:
                    continue
                try:
                    await self.bot.edit_general_forum_topic(int(chat_str), name=name)
                except Exception as e:
                    log.warning("[telegram] edit_general_forum_topic failed: %s", e)
            if self.agent.thread_id == "":
                self.thread_name = name
            return
        for chat_str, chat in self.load_bindings().items():
            for tid, tu in chat.get("topics", {}).items():
                if tu != uuid: continue
                try:
                    await self.bot.edit_forum_topic(int(chat_str), int(tid), name=name)
                except Exception as e:
                    log.warning("[telegram] edit_forum_topic failed: %s", e)
                if self.agent is not None and self.agent.thread_id == uuid:
                    self.thread_name = name
                return

    async def thread_delete(self, uuid: str):
        b = self.load_bindings()
        # Main тред: General топик удалить нельзя, bulk-delete сообщений тоже
        # нет. Для форумов переименовываем General в DELETED + шлём пояснение,
        # для лички (main-агент в DM) просто шлём пояснение.
        if uuid == "":
            chat_ids: set[int] = set()
            if self.agent.id == "main" and TelegramTransport._main_chat_id is not None:
                chat_ids.add(TelegramTransport._main_chat_id)
            for chat_str, chat in b.items():
                if chat.get("agent_id") == self.agent.id:
                    chat_ids.add(int(chat_str))
            for chat_id in chat_ids:
                if chat_id < 0:
                    try:
                        await self.bot.edit_general_forum_topic(chat_id, name="DELETED")
                    except Exception as e:
                        log.warning("[telegram] edit_general_forum_topic failed: %s", e)
                try:
                    await self.bot.send_message(chat_id, "⚠️ История этого треда была удалена в агенте.")
                except Exception as e:
                    log.warning("[telegram] send_message failed: %s", e)
            return

        changed = False
        for chat_str, chat in b.items():
            topics = chat.get("topics", {})
            for tid, tu in list(topics.items()):
                if tu == uuid:
                    del topics[tid]
                    changed = True
                    try:
                        await self.bot.delete_forum_topic(int(chat_str), int(tid))
                    except Exception as e:
                        log.warning("[telegram] delete_forum_topic failed: %s", e)
        if changed:
            self.save_bindings(b)

    def set_agent(self, agent):
        super().set_agent(agent)
        if self._update_commands_task is not None: return

        async def update_commands():
            last_hash = None
            while True:
                if not await self._ensure_chat_and_topic():
                    await asyncio.sleep(1)
                    continue
                cmds = {
                    cmd: desc
                    for skill in self.agent.skills
                    for cmd, desc in skill.get_bypass_commands(standalone_only=True).items()
                }
                if self.chat_id < 0:
                    cmds = {k: v for k, v in cmds.items() if k == "stop"}
                h = hash(tuple(sorted(cmds.items())))
                if h != last_hash:
                    last_hash = h
                    try:
                        await self.bot.set_my_commands(
                            [BotCommand(command=k, description=v) for k, v in cmds.items()],
                            scope=BotCommandScopeChat(chat_id=self.chat_id),
                        )
                        log.info("[telegram] set %d commands for %s", len(cmds), self.chat_id)
                    except Exception as e:
                        log.warning("[telegram] set_my_commands failed: %s", e)
                await asyncio.sleep(1)
        self._update_commands_task = asyncio.create_task(update_commands())

    async def _exec_item(self, item):
        kind = item["kind"]
        if kind == "send":
            result = await self.bot.send_message(
                self.chat_id, item["text"],
                message_thread_id=self.thread_id,
                link_preview_options=self._no_link_preview,
                **item["kwargs"],
            )
            self._last_edit_texts[result.message_id] = item["text"]
            return result
        elif kind == "edit":
            msg, text = item["msg"], item["text"]
            if self._last_edit_texts.get(msg.message_id) == text:
                return msg
            result = await msg.edit_text(text, link_preview_options=self._no_link_preview, **item["kwargs"])
            self._last_edit_texts[msg.message_id] = text
            return result

    async def _queue_worker(self):
        """Process send/edit queue with rate limiting."""

        last_action = 0.0
        while True:
            item = await self._queue.get()
            # Skip stale edits — if queue has a newer edit for same message, skip this one
            if item["kind"] == "edit" and any(
                q["kind"] == "edit" and q["msg"].message_id == item["msg"].message_id
                for q in self._queue._queue
            ):
                continue
            
            wait = (2.0 if item["kind"]=="edit" else 1.0) - max(0, asyncio.get_event_loop().time() - last_action)
            if wait > 0: await asyncio.sleep(wait)

            future = item.get("future")
            try:
                result = await self._exec_item(item)
                if future: future.set_result(result)
            except (TelegramRetryAfter, TelegramServerError) as e:
                wait = e.retry_after if isinstance(e, TelegramRetryAfter) else 5
                log.warning("Telegram error, waiting %s sec: %s", wait, e)
                await asyncio.sleep(wait)
                try:
                    result = await self._exec_item(item)
                    if future: future.set_result(result)
                except Exception as e2:
                    if future: future.set_exception(e2)
                    else: log.warning("queue item failed after retry: %s", e2, exc_info=True)
            except Exception as e:
                if future: future.set_exception(e)
                else: log.warning("queue item failed: %s", e, exc_info=True)
            last_action = asyncio.get_event_loop().time()            

    def _ensure_queue(self):
        if not self._queue_task or self._queue_task.done():
            self._queue_task = asyncio.create_task(self._queue_worker())

    async def _send(self, text: str, **kwargs) -> Message | None:
        if not await self._ensure_chat_and_topic(): return None
        self._ensure_queue()
        future = asyncio.get_event_loop().create_future()
        self._queue.put_nowait({"kind": "send", "text": text, "kwargs": kwargs, "future": future})
        return await future

    async def _edit(self, msg: Message, text: str, **kwargs):
        if not await self._ensure_chat_and_topic(): return
        self._ensure_queue()
        self._queue.put_nowait({"kind": "edit", "msg": msg, "text": text, "kwargs": kwargs})

    async def _answer(self, text: str, messages: list | None = None, expandable: bool = False, final: bool = False, prefix: str = "", max_chunks = None):
        if not await self._ensure_chat_and_topic(): return
        if messages is None:
            messages = []
        if expandable:
            escaped_prefix = html.escape(prefix)
            overhead = len(escaped_prefix) + 36  # <blockquote expandable>…</blockquote>
            converter = html.escape
        else:
            overhead = 0
            converter = _markdown_to_html

        if max_chunks: overhead += 3
        raw_chunks = _split_message_to_html(text, converter, max_len=4096 - overhead)

        if max_chunks:
            if len(raw_chunks) > max_chunks:
                raw_chunks = raw_chunks[:max_chunks]
                raw_chunks[max_chunks-1] += "..."

        if expandable:
            tag = "blockquote expandable" if final else "blockquote"
            bodies = [f"<{tag}>{escaped_prefix}{c.rstrip()}</blockquote>" for c in raw_chunks]
        else:
            bodies = raw_chunks

        for m, body in enumerate(bodies):
            if m < len(messages):
                await self._edit(messages[m], body, parse_mode="HTML")
            else:
                messages.append(await self._send(body, parse_mode="HTML"))
        for msg in messages[len(bodies):]:
            try:
                await msg.delete()
            except Exception:
                pass
        del messages[len(bodies):]

    async def on_tool_call(self, name: str, args: dict):
        lines = "\n".join(f"{html.escape(k)}: {html.escape(str(v))}" for k, v in args.items())
        call_text = f"<b>[{html.escape(name)}]</b>\n{lines}" if lines else f"<b>[{html.escape(name)}]</b>"
        self._tool_call_text = call_text[:2000]
        self._tool_msg = await self._send(f"<blockquote expandable>{self._tool_call_text}</blockquote>", parse_mode="HTML")

    async def on_tool_result(self, name: str, result):
        if isinstance(result, dict):
            parts = [f"<b>[{html.escape(k)}]</b>\n{html.escape(str(v))}" for k, v in result.items() if v not in (None, "", [], {})]
            result_text = "\n".join(parts) if parts else "(пусто)"
        else:
            result_text = html.escape(result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2))
        result_text = result_text[:2000]
        if self._tool_msg:
            await self._edit(
                self._tool_msg,
                f"<blockquote expandable>{self._tool_call_text}</blockquote>\n<blockquote expandable>{result_text}</blockquote>",
                parse_mode="HTML",
            )
        else:
            await self._send(
                f"<blockquote expandable><b>[{html.escape(name)}]</b>\n{result_text}</blockquote>",
                parse_mode="HTML",
            )

    async def send_message(self, text: str, stream_id=None, final: bool = True):
        messages = self._stream_messages.setdefault(stream_id, []) if stream_id else None
        await self._answer((text or "").strip() or "[…]", messages=messages, final=final)

    async def inject_message(self, text: str):
        await self._answer("[→]" + text, final=True)

    async def send_app_url(self, url: str, text: str, button: str = ""):
        if not await self._ensure_chat_and_topic(): return
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=button or text, web_app=WebAppInfo(url=url))
        ]])
        await self.bot.send_message(self.chat_id, text, reply_markup=kb, message_thread_id=self.thread_id)

    async def send_system_prompt(self, text: str):
        if not self.verbose: return
        await self._answer(text, expandable=True, prefix="🔧 ", max_chunks=1)

    async def send_thinking(self, text: str, stream_id=None, final: bool = False):
        messages = self._stream_messages.setdefault(stream_id, []) if stream_id else None
        await self._answer(text, expandable=True, final=final, prefix="🧠 ", messages=messages)

    async def send_memory_info(self, text: str):
        await self._answer(text, expandable=True, final=True, prefix="💾 ")

    async def send_code(self, lang: str, code: str):
        await self._answer(f"```{lang}\n{code}\n```")

    async def handle_message(self, message: Message):
        if self.thread_id and not self.thread_name:
            topic = getattr(getattr(message.reply_to_message, "forum_topic_created", None), "name", None)
            if topic:
                self.thread_name = topic
        self._pending_messages.append(message)
        if self._flush_task:
            self._flush_task.cancel()

        async def flush():
            await asyncio.sleep(0.1)
            messages = self._pending_messages[:]
            self._pending_messages.clear()
            self._flush_task = None
            await self._handle_messages(messages)

        self._flush_task = asyncio.create_task(flush())

    async def _handle_messages(self, messages: list[Message]):
        first = messages[0]
        self._tool_msg = None
        self._tool_call_text = ""

        async def _download_file(file_id: str) -> bytes:
            buf = io.BytesIO()
            await self.bot.download_file((await self.bot.get_file(file_id)).file_path, buf)
            return buf.getvalue()

        content_parts = []

        user_id = first.from_user.id if first.from_user else None
        if self.chat_id < 0 and first.from_user:
            count = await self.bot.get_chat_member_count(self.chat_id)
            if count > 2:  # more than just one user + bot
                u = first.from_user
                sender_name = " ".join(filter(None, [u.first_name, u.last_name])) or u.username or str(u.id)
                content_parts.append({"type": "text", "text": f"[from: {sender_name}]"})

        for message in messages:
            origin = message.forward_origin
            if origin:
                if isinstance(origin, MessageOriginUser) and origin.sender_user.id == user_id:
                    forward_sender = "myself"
                elif isinstance(origin, MessageOriginUser):
                    u = origin.sender_user
                    forward_sender = " ".join(filter(None, [u.first_name, u.last_name])) or u.username or str(u.id)
                else:
                    forward_sender = getattr(origin, "sender_user_name", None) \
                        or getattr(getattr(origin, "chat", None), "title", None) \
                        or "unknown"
            else:
                forward_sender = None

            text = message.text or message.caption
            if text and text.startswith("/") and "@" in text.split()[0]:
                cmd = text.split()[0]
                text = cmd.split("@")[0] + text[len(cmd):]
            if text:
                if forward_sender:
                    content_parts.append({"type": "text", "text": f"<forwarded_message from=\"{forward_sender}\">\n{text}\n</forwarded_message>"})
                else:
                    content_parts.append({"type": "text", "text": text})

            if message.photo:
                data = await _download_file(message.photo[-1].file_id)
                content_parts.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64.b64encode(data).decode()}"}})

            if message.sticker:
                sticker = message.sticker
                if not sticker.is_animated and not sticker.is_video:
                    data = await _download_file(sticker.file_id)
                    content_parts.append({"type": "image_url", "image_url": {"url": f"data:image/webp;base64,{base64.b64encode(data).decode()}"}})

                hint = f"[стикер {sticker.emoji}]" if sticker.emoji else "[стикер]"
                content_parts.append({"type": "text", "text": hint})

            attachment, field = None, None
            if message.photo: attachment, field = message.photo[-1], "photo"
            elif message.audio: attachment, field = message.audio, "audio"
            elif message.video: attachment, field = message.video, "video"
            elif message.voice: attachment, field = message.voice, "voice"
            elif message.video_note: attachment, field = message.video_note, "video_note"
            elif message.document: attachment, field = message.document, "document"
            else: continue

            filename = f"photo_{attachment.file_unique_id}.jpg" if field == "photo" else getattr(attachment, "file_name", None)
            file_meta = {
                "tg_file_id": attachment.file_id,
                "filename": filename,
                "type": field,
                "mime_type": mimetypes.guess_type(filename or "")[0] or "application/octet-stream",
                "size_bytes": attachment.file_size,
            }
            if forward_sender:
                file_meta["forwarded_from"] = forward_sender

            text_extensions = {
                ".txt", ".md", ".py", ".js", ".ts", ".json", ".yaml", ".yml", ".xml",
                ".html", ".css", ".sh", ".sql", ".csv", ".log",
            }
            content = None
            if field == "document" and os.path.splitext(filename or "")[1].lower() in text_extensions:
                try:
                    content = (await _download_file(attachment.file_id)).decode("utf-8", errors="replace")
                except Exception:
                    logging.exception("[transport] Не удалось прочитать текстовый файл %s", filename)

            if field == "voice":
                try:
                    content = await self.agent.transcribe_audio(await _download_file(attachment.file_id), "audio/ogg")
                except Exception as e:
                    logging.exception("[transport] Не удалось транскрибировать голосовое %s", attachment.file_id)
                    await self._send(f"⚠️ Не удалось распознать голосовое сообщение: {e}")

            if field in ("video", "video_note"):
                try:
                    video_mime = getattr(attachment, "mime_type", None) or "video/mp4"
                    content = await self.agent.describe_video(await _download_file(attachment.file_id), video_mime)
                except Exception:
                    logging.exception("[transport] Не удалось распознать видео %s", attachment.file_id)

            attrs = " ".join(f'{k}="{v}"' for k, v in file_meta.items())
            part = {
                "type": "text",
                "text": f"<attached_file {attrs}>\n<content>\n{content}\n</content>\n</attached_file>"
                        if content else f"<attached_file {attrs} />",
            }
            if content and field == "document":
                part["_document_id"] = f"{attachment.file_unique_id}_{filename}"
            content_parts.append(part)

        if not content_parts:
            return

        trigger_answer = True
        # В групповых чатах с несколькими людьми (chat_id < 0 + >2 участников,
        # т.е. помимо бота больше одного человека) отвечаем только если к боту
        # обратились явно — reply или @mention. В 1:1-группе (я+бот) — как в DM.
        if self.chat_id < 0 and await self.bot.get_chat_member_count(self.chat_id) > 2:
            bot_id = (await self.bot.me()).id
            bot_username = (await self.bot.me()).username
            reply = first.reply_to_message
            replied_to_bot = reply and reply.from_user and reply.from_user.id == bot_id
            mentioned = f"@{bot_username}" in (first.text or first.caption or "")
            trigger_answer = replied_to_bot or mentioned

        await self.process_message(
            content_parts=content_parts,
            user_message_id=first.message_id,
            trigger_answer=trigger_answer,
        )

    # forks/telegram.json:
    #   { "<chat_id>": {"agent_id": "<id>", "topics": {"<topic_id>": "<thread_id>"}} }
    # Личка/general (telegram_thread_id is None) — это thread "" агента, в topics не пишем.
    # Новый топик в привязанной группе автоматически создаёт thread с id == topic_id.
    # Implicit: личка _main_chat_id привязана к main (можно не хранить).

    _bindings_cache: dict | None = None

    @classmethod
    def load_bindings(cls) -> dict:
        if cls._bindings_cache is None:
            try:
                with open(cls._BINDINGS_PATH, encoding="utf-8") as f:
                    cls._bindings_cache = json.load(f)
            except FileNotFoundError:
                cls._bindings_cache = {}
        return cls._bindings_cache

    @classmethod
    def save_bindings(cls, b: dict):
        os.makedirs(os.path.dirname(cls._BINDINGS_PATH), exist_ok=True)
        with open(cls._BINDINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(b, f, ensure_ascii=False, indent=2)
        cls._bindings_cache = b

    @classmethod
    def lookup_binding(cls, chat_id: int, telegram_thread_id: int | None) -> tuple[str, str] | None:
        b = cls.load_bindings()
        chat = b.get(str(chat_id))
        if not chat:
            if chat_id == cls._main_chat_id and telegram_thread_id is None:
                return ("main", "")
            return None
        agent_id = chat.get("agent_id")
        if not agent_id:
            return None
        if telegram_thread_id is None:
            return (agent_id, "")
        tk = str(telegram_thread_id)
        topics = chat.setdefault("topics", {})
        if tk not in topics:
            topics[tk] = uuid.uuid4().hex[:8]
            cls.save_bindings(b)
        return (agent_id, topics[tk])

    @classmethod
    def reverse_binding(cls, agent_id: str, agent_thread_id: str) -> tuple[int | None, int | None]:
        # main-агент особый: дефолтный тред — всегда личка.
        if agent_id == "main" and agent_thread_id == "":
            return (cls._main_chat_id, None) if cls._main_chat_id is not None else (None, None)
        b = cls.load_bindings()
        for chat_str, chat in b.items():
            if chat.get("agent_id") != agent_id:
                continue
            if agent_thread_id == "":
                return (int(chat_str), None)
            for tk, tid in chat.get("topics", {}).items():
                if tid == agent_thread_id:
                    return (int(chat_str), int(tk))
        return (None, None)

    @classmethod
    def find_chat_for_agent(cls, agent_id: str) -> int | None:
        for chat_str, chat in cls.load_bindings().items():
            if chat.get("agent_id") == agent_id:
                return int(chat_str)
        if agent_id == "main" and cls._main_chat_id is not None:
            return cls._main_chat_id
        return None

    @classmethod
    def start(cls, config: dict):
        from agent import Agent
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        cls.bot = Bot(token=config["bot_token"], session=AiohttpSession(proxy=proxy) if proxy else None)
        dp = Dispatcher()

        allowed_user_ids = config["allowed_user_ids"]
        cls._main_chat_id = allowed_user_ids[0]

        # Multi-step bind flow per (chat, telegram_thread): "name" | "clone".
        pending: dict[tuple, dict] = {}

        def _list_forks() -> list[str]:
            forks_dir = "forks"
            if not os.path.isdir(forks_dir): return ["main"]
            names = [n for n in os.listdir(forks_dir) if os.path.isdir(os.path.join(forks_dir, n))]
            return ["main"] + sorted(names)

        def _save_binding(chat_id: int, agent_id: str):
            # Снимаем старую привязку этого агента, если была — один агент на одну группу.
            b = cls.load_bindings()
            chat_key = str(chat_id)
            for k, v in list(b.items()):
                if k != chat_key and v.get("agent_id") == agent_id:
                    del b[k]
            b.setdefault(chat_key, {"topics": {}})["agent_id"] = agent_id
            cls.save_bindings(b)

        def _kb_grid(buttons: list, per_row: int = 3) -> InlineKeyboardMarkup:
            rows = [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]
            return InlineKeyboardMarkup(inline_keyboard=rows)

        async def _ask_initial(chat_id: int, ttid: int | None):
            buttons = [InlineKeyboardButton(text=f, callback_data=f"bind_to:{f}") for f in _list_forks()]
            buttons.append(InlineKeyboardButton(text="+ Новый агент", callback_data="bind_new"))
            await cls.bot.send_message(
                chat_id,
                "Этот чат не привязан к агенту. Выбери существующего или создай нового:",
                message_thread_id=ttid,
                reply_markup=_kb_grid(buttons),
            )

        async def _ask_clone(chat_id: int, ttid: int | None, name: str):
            buttons = [InlineKeyboardButton(text="без памяти", callback_data="clone_from:_none_")]
            buttons += [InlineKeyboardButton(text=f, callback_data=f"clone_from:{f}") for f in _list_forks()]
            await cls.bot.send_message(
                chat_id,
                f"Откуда копировать память для <b>{html.escape(name)}</b>?",
                parse_mode="HTML",
                message_thread_id=ttid,
                reply_markup=_kb_grid(buttons),
            )

        async def resolve_tg(chat_id: int, telegram_thread_id: int | None):
            binding = cls.lookup_binding(chat_id, telegram_thread_id)
            if not binding: return None
            agent_id, agent_thread_id = binding
            agent = await Agent.get(agent_id, agent_thread_id)
            if not agent: return None
            transports = getattr(agent.transport, "transports", [agent.transport])
            tg = next((t for t in transports if isinstance(t, cls)), None)
            if tg:
                # Биндинг мог быть только что добавлен — set_agent его ещё не видел.
                tg.chat_id, tg.thread_id = chat_id, telegram_thread_id
            return tg

        allowed_groups: dict[int, bool] = {}
        async def is_group_allowed(chat_id: int) -> bool:
            if chat_id in allowed_groups:
                return allowed_groups[chat_id]
            admins = await cls.bot.get_chat_administrators(chat_id)
            admin_ids = {a.user.id for a in admins}
            allowed = bool(admin_ids & set(allowed_user_ids))
            allowed_groups[chat_id] = allowed
            return allowed

        async def on_message(message: Message):
            if not message.from_user: return
            if message.forum_topic_created and message.from_user.is_bot: return
            if message.chat.id < 0:
                if not await is_group_allowed(message.chat.id): return
            else:
                if message.from_user.id not in allowed_user_ids: return

            ttid = message.message_thread_id if message.chat.is_forum else None
            key = (message.chat.id, ttid)

            # Bind-flow: ждём имя нового агента
            if pending.get(key, {}).get("step") == "name":
                name = (message.text or "").strip()
                if not name or "/" in name or name.startswith("."):
                    await message.reply("Имя пустое или с недопустимыми символами. Попробуй ещё:")
                    return
                if name in _list_forks():
                    await message.reply(f"Имя '{name}' уже занято. Введи другое:")
                    return
                pending[key] = {"step": "clone", "name": name}
                await _ask_clone(message.chat.id, ttid, name)
                return

            binding = cls.lookup_binding(message.chat.id, ttid)
            if binding and binding[0] == "main" and ttid is None and message.chat.id != cls._main_chat_id:
                await message.reply("Главному агенту можно писать только в личку без топика.")
                return

            tg = await resolve_tg(message.chat.id, ttid)
            if not tg:
                await _ask_initial(message.chat.id, ttid)
                return
            if message.forum_topic_created:
                if message.forum_topic_created.name:
                    await tg.agent.thread_rename(tg.agent.thread_id, message.forum_topic_created.name)
                return
            await tg.handle_message(message)

        async def on_callback_query(callback: CallbackQuery):
            if not callback.from_user or callback.from_user.id not in allowed_user_ids: return
            chat_id = callback.message.chat.id
            ttid = callback.message.message_thread_id if callback.message.chat.is_forum else None
            key = (chat_id, ttid)
            await callback.answer()
            await callback.message.edit_reply_markup(reply_markup=None)

            if callback.data.startswith("answer_idx:"):
                tg = await resolve_tg(chat_id, ttid)
                if not tg: return
                try: idx = int(callback.data[len("answer_idx:"):])
                except ValueError: return
                opts = tg._suggestion_options.pop(callback.message.message_id, None)
                if not opts or idx >= len(opts): return
                text = opts[idx]
                await callback.message.answer(text)
                await tg.process_message(content_parts=[{"type": "text", "text": text}])
                return

            if callback.data.startswith("bind_to:"):
                agent_id = callback.data[len("bind_to:"):]
                _save_binding(chat_id, agent_id)
                pending.pop(key, None)
                await callback.message.answer(f"✅ Чат привязан к <b>{html.escape(agent_id)}</b>. Напиши сообщение.", parse_mode="HTML")
                return

            if callback.data == "bind_new":
                pending[key] = {"step": "name"}
                await callback.message.answer("Введи имя нового агента ответом:")
                return

            if callback.data.startswith("clone_from:"):
                state = pending.get(key)
                if not state or state.get("step") != "clone":
                    await callback.message.answer("Сессия истекла, начни заново.")
                    return
                name = state["name"]
                choice = callback.data[len("clone_from:"):]
                # Биндинг сохраняем ДО Agent.get: set_agent внутри прочитает его.
                _save_binding(chat_id, name)
                copy_from = None if choice == "_none_" else await Agent.get(choice)
                agent = await Agent.get(name, "", force_create=True, copy_memory_from=copy_from)
                if not agent:
                    await callback.message.answer(f"❌ Не удалось создать агента {name}.")
                    pending.pop(key, None)
                    return
                pending.pop(key, None)
                label = "✅ Создан" if choice == "_none_" else f"✅ Создан, память клонирована из {choice}"
                await callback.message.answer(f"{label}: <b>{html.escape(name)}</b>. Напиши сообщение.", parse_mode="HTML")
                return

        async def on_topic_edit(message: Message):
            edited = message.forum_topic_edited
            if not edited or not edited.name: return
            if message.chat.id < 0:
                if not await is_group_allowed(message.chat.id): return
            else:
                if message.from_user and message.from_user.id not in allowed_user_ids: return
            binding = cls.lookup_binding(message.chat.id, message.message_thread_id)
            if not binding: return
            agent_id, thread_uuid = binding
            agent = await Agent.get(agent_id, thread_uuid)
            if not agent: return
            await agent.thread_rename(thread_uuid, edited.name)

        dp.message(F.forum_topic_edited)(on_topic_edit)
        dp.message()(on_message)
        dp.callback_query()(on_callback_query)
        asyncio.create_task(dp.start_polling(cls.bot))

