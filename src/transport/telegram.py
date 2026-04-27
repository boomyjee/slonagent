import html
import base64, io, os, re, asyncio, logging, json, mimetypes

log = logging.getLogger(__name__)
from typing import Annotated
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramRetryAfter, TelegramServerError
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
        try:
            chat = await t.bot.get_chat(t.chat_id)
            self._chat_title = chat.title or chat.full_name or str(t.chat_id)
            self._is_private = chat.type == "private"
        except Exception as e:
            logging.warning("[TelegramSkill] не удалось получить инфо о чате: %s", e)

    async def get_context_prompt(self, user_text: str = "") -> str:
        if not self._chat_title:
            return ""
        t = self.transport
        if self._is_private:
            return f"Ты работаешь в личном чате с {self._chat_title}."
        if t.thread_id:
            topic = t.thread_name or f"#{t.thread_id}"
            return f"Ты работаешь в группе «{self._chat_title}», топик «{topic}»."
        return f"Ты работаешь в группе «{self._chat_title}»."

    def _resolve_paths(self, paths: list[str]) -> list[str] | dict:
        host_paths = []
        for p in paths:
            # С sandbox'ом — резолвим container-path в host. Без — принимаем
            # host-абсолютный путь как есть (Claude Code бегает по хосту).
            if self.sandbox:
                host_path = self.sandbox.resolve_path(p)
                if host_path is None:
                    return {"error": f"Доступ запрещён: {p}"}
            else:
                host_path = os.path.abspath(p)
            if not os.path.exists(host_path):
                return {"error": f"Файл не найден: {host_path}"}
            host_paths.append(host_path)
        return host_paths

    @tool("Отправить один или несколько файлов как группу. Один вызов = одна группа. Для нескольких групп — вызови несколько раз.")
    async def send_files(self, paths: Annotated[list[str], "Список путей к файлам."]):
        host_paths = self._resolve_paths(paths)
        if isinstance(host_paths, dict):
            return host_paths
        logging.info("[skill] send_files: %s", host_paths)
        if len(host_paths) == 1:
            await self.transport.bot.send_document(self.transport.chat_id, FSInputFile(host_paths[0]), message_thread_id=self.transport.thread_id)
        else:
            media = [InputMediaDocument(media=FSInputFile(p)) for p in host_paths]
            await self.transport.bot.send_media_group(self.transport.chat_id, media, message_thread_id=self.transport.thread_id)
        return {"status": "ok"}

    @tool("Отправить одно или несколько изображений как альбом (до 10). Один вызов = один альбом. Для нескольких альбомов — вызови несколько раз.")
    async def send_images(self, paths: Annotated[list[str], "Список путей к изображениям."]):
        host_paths = self._resolve_paths(paths)
        if isinstance(host_paths, dict):
            return host_paths
        logging.info("[skill] send_images: %s", host_paths)
        if len(host_paths) == 1:
            await self.transport.bot.send_photo(self.transport.chat_id, FSInputFile(host_paths[0]), message_thread_id=self.transport.thread_id)
        else:
            media = [InputMediaPhoto(media=FSInputFile(p)) for p in host_paths]
            await self.transport.bot.send_media_group(self.transport.chat_id, media, message_thread_id=self.transport.thread_id)
        return {"status": "ok"}

    @tool(
        "Предложить пользователю варианты ответа в виде кнопок под сообщением. "
        "Пользователь нажимает — его выбор приходит как обычное сообщение. "
        "ВАЖНО: текст каждой кнопки не должен превышать 57 байт в UTF-8 "
        "(~28 кириллических символов или ~57 латинских). Если вариант длиннее — сократи его."
    )
    async def suggest_user_answers(
        self,
        text: Annotated[str, "Текст сообщения перед кнопками"],
        options: Annotated[list[str], "Варианты ответа. Каждый не длиннее 57 байт в UTF-8"],
    ):
        too_long = [opt for opt in options if len(f"answer:{opt}".encode()) > 64]
        if too_long:
            return {"error": f"Слишком длинные варианты (>57 байт): {too_long}. Сократи их и вызови снова."}
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=opt, callback_data=f"answer:{opt}") for opt in options]
        ])
        await self.transport._send(_markdown_to_html(text), parse_mode="HTML", reply_markup=kb)
        return {"status": "ok"}

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

    def __init__(self, chat_id: int, thread_id: int | None = None, verbose: bool = True):
        super().__init__()
        self.chat_id = chat_id
        self.thread_id = thread_id
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
        self._last_edit_texts: dict[int, str] = {}  # msg_id → last sent text
        self._stream_messages: dict[int, list] = {}  # stream_id → list of Message

    async def send_processing(self, active: bool):
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

    def set_agent(self, agent):
        super().set_agent(agent)

        async def update_commands():
            all_commands = {
                cmd: desc
                for skill in agent.skills
                for cmd, desc in skill.get_bypass_commands(standalone_only=True).items()
            }
            if self.chat_id < 0:
                all_commands = {k: v for k, v in all_commands.items() if k == "stop"}
            commands = [BotCommand(command=cmd, description=desc) for cmd, desc in all_commands.items()]
            try:
                await self.bot.set_my_commands(commands, scope=BotCommandScopeChat(chat_id=self.chat_id))
            except Exception as e:
                log.warning("[telegram] set_my_commands failed: %s", e)
            log.info("[telegram] set %d commands for %s", len(commands), self.chat_id)
        asyncio.create_task(update_commands())

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

    async def _send(self, text: str, **kwargs) -> Message:
        self._ensure_queue()
        future = asyncio.get_event_loop().create_future()
        self._queue.put_nowait({"kind": "send", "text": text, "kwargs": kwargs, "future": future})
        return await future

    def _edit(self, msg: Message, text: str, **kwargs):
        self._ensure_queue()
        self._queue.put_nowait({"kind": "edit", "msg": msg, "text": text, "kwargs": kwargs})
    
    async def _answer(self, text: str, messages: list | None = None, expandable: bool = False, final: bool = False, prefix: str = "", max_chunks = None):
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
            bodies = [f"<{tag}>{escaped_prefix}{c}</blockquote>" for c in raw_chunks]
        else:
            bodies = raw_chunks

        for m, body in enumerate(bodies):
            if m < len(messages):
                self._edit(messages[m], body, parse_mode="HTML")
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
            self._edit(
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
        sent = await self._send("[→]" + text)

    async def send_app_url(self, url: str, text: str, button: str = ""):
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
            if content:
                content_parts.append({
                    "type": "text",
                    "text": f"<attached_file {attrs}>\n<content>\n{content}\n</content>\n</attached_file>",
                    "_document_id": f"{attachment.file_unique_id}_{filename}",
                })
            else:
                content_parts.append({"type": "text", "text": f"<attached_file {attrs} />"})

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

    @classmethod
    async def listen(cls, config: dict, make_agent):
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        cls.bot = Bot(token=config["bot_token"], session=AiohttpSession(proxy=proxy) if proxy else None)
        dp = Dispatcher()

        allowed_user_ids = config["allowed_user_ids"]
        transport_kwargs = config.get("transport", {})

        transports: dict[str, "TelegramTransport"] = {}
        async def get_transport(chat_id, thread_id, force_create=False, copy_memory_from=None):
            is_main = (chat_id == allowed_user_ids[0] and thread_id is None)
            agent_id = "main" if is_main else f"{chat_id}_{thread_id}"
            if agent_id in transports:
                return transports[agent_id]
            tg = cls(chat_id=chat_id, thread_id=thread_id, **transport_kwargs)
            agent = await make_agent(agent_id, tg, force_create=force_create, copy_memory_from=copy_memory_from)
            if agent:
                transports[agent_id] = tg
                return tg
            return None


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
            if message.chat.id < 0:
                if not await is_group_allowed(message.chat.id): return
            else:
                if message.from_user.id not in allowed_user_ids: return

            thread_id = message.message_thread_id if message.chat.is_forum else None
            tg = await get_transport(message.chat.id, thread_id)
            if not tg:
                if message.text:
                    kb = InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="🗒 Чистый агент", callback_data="new_agent_clean"),
                        InlineKeyboardButton(text="🧠 Клон с памятью", callback_data="new_agent_clone"),
                    ]])
                    await message.answer(
                        "В этом топике ещё нет агента.\n\n"
                        "• <b>🗒 Чистый агент</b> — начнёт с нуля, без памяти\n"
                        "• <b>🧠 Клон с памятью</b> — скопирует личность и инструменты из основного агента",
                        reply_markup=kb,
                        parse_mode="HTML",
                    )
                return

            await tg.handle_message(message)

        async def on_callback_query(callback: CallbackQuery):
            if not callback.from_user or callback.from_user.id not in allowed_user_ids: return

            chat_id = callback.message.chat.id
            thread_id = callback.message.message_thread_id if callback.message.chat.is_forum else None

            await callback.answer()
            await callback.message.edit_reply_markup(reply_markup=None)

            if callback.data.startswith("answer:"):
                text = callback.data[len("answer:"):]
                tg = await get_transport(chat_id, thread_id)
                if tg:
                    await callback.message.answer(text)
                    await tg.process_message(content_parts=[{"type": "text", "text": text}])
                return

            if callback.data in ("new_agent_clean", "new_agent_clone"):
                status = await callback.message.answer("⏳ Создаю агента...")
                copy_from = main_transport if callback.data == "new_agent_clone" else None
                await get_transport(chat_id, thread_id, force_create=True, copy_memory_from=copy_from)
                label = "✅ Клон создан" if callback.data == "new_agent_clone" else "✅ Агент создан"
                await status.edit_text(label)

        dp.message()(on_message)
        dp.callback_query()(on_callback_query)

        main_transport = await get_transport(allowed_user_ids[0], None, force_create=True)
        await dp.start_polling(cls.bot)

