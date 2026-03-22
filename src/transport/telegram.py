import html
import base64, io, os, re, asyncio, logging, json, mimetypes

log = logging.getLogger(__name__)
from typing import Annotated
from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import Message, FSInputFile, InputMediaPhoto, InputMediaDocument, LinkPreviewOptions, MessageOriginUser
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
        super().__init__()

    async def start(self):
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
        from src.skills.sandbox import SandboxSkill
        sandbox = next((s for s in self.agent.skills if isinstance(s, SandboxSkill)), None)
        host_paths = []
        for p in paths:
            host_path = sandbox.resolve_path(p) if sandbox else None
            if host_path is None:
                return {"error": f"Доступ запрещён: {p}"}
            if not os.path.exists(host_path):
                return {"error": f"Файл не найден: {host_path}"}
            host_paths.append(host_path)
        return host_paths

    @tool("Отправить один или несколько файлов как группу. Один вызов = одна группа. Для нескольких групп — вызови несколько раз.")
    async def send_files(self, paths: Annotated[list[str], "Список путей к файлам внутри контейнера."]):
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
    async def send_images(self, paths: Annotated[list[str], "Список путей к изображениям внутри контейнера."]):
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

    @tool("Скачать файл, отправленный пользователем, в рабочую директорию. Путь назначения должен быть внутри /workspace/.")
    async def download_file(
        self,
        tg_file_id: Annotated[str, "tg_file_id из метаданных прикреплённого файла."],
        dest_path: Annotated[str, "Путь назначения внутри контейнера (например /workspace/photo.jpg)."],
    ):
        from src.skills.sandbox import SandboxSkill
        sandbox = next((s for s in self.agent.skills if isinstance(s, SandboxSkill)), None)
        host_dest = sandbox.resolve_path(dest_path) if sandbox else None

        if host_dest is None:
            return {"error": f"Путь запрещён или недоступен: {dest_path}"}

        tg_file = await self.transport.bot.get_file(tg_file_id)
        os.makedirs(os.path.dirname(host_dest), exist_ok=True)
        await self.transport.bot.download_file(tg_file.file_path, host_dest)
        logging.info("[skill] download_file: %s → %s", tg_file_id, host_dest)
        return {"status": "ok", "saved_to": dest_path}


class TelegramTransport(BaseTransport):
    def __init__(self, bot: Bot, chat_id: int, thread_id: int | None = None, verbose: bool = True):
        self.bot = bot
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.thread_name: str | None = None
        self.agent = None
        self._no_link_preview = LinkPreviewOptions(is_disabled=True)
        self._skill = TelegramSkill(self)
        self.verbose = verbose
        self._tool_msg = None
        self._tool_call_text = ""
        self._pending_messages: list[Message] = []
        self._flush_task: asyncio.Task | None = None
        self._pending_answer = {}

    def set_agent(self, agent):
        self.agent = agent
        self._skill.register(agent)
        agent.skills.insert(0, self._skill)

    async def _tg_retry(self, fn):
        for attempt in range(3):
            try:
                return await fn()
            except TelegramRetryAfter as e:
                log.warning("Flood control, waiting %s sec (attempt %d)", e.retry_after, attempt + 1)
                await asyncio.sleep(e.retry_after)
        return await fn()

    async def _send(self, text: str, **kwargs) -> Message:
        return await self._tg_retry(lambda: self.bot.send_message(self.chat_id, text, message_thread_id=self.thread_id, link_preview_options=self._no_link_preview, **kwargs))

    async def _edit(self, msg: Message, text: str, **kwargs) -> Message:
        return await self._tg_retry(lambda: msg.edit_text(text, link_preview_options=self._no_link_preview, **kwargs))
    
    async def _answer(self, text: str, messages: list | None = None, expandable: bool = False, collapsed: bool = True, prefix: str = "", max_chunks = None):
        if messages is None: 
            messages = []
            await self._do_answer(text, messages, expandable, collapsed, prefix, max_chunks)
            return messages

        key = id(messages)
        pending = self._pending_answer.get(key)
        self._pending_answer[key] = (text, collapsed)
        if pending:
            return messages

        THROTLE_DELAY = 2.5
        async def _do():
            await asyncio.sleep(THROTLE_DELAY)
            recent_text, recent_collapsed = self._pending_answer[key]
            self._pending_answer[key] = None
            try:
                await self._do_answer(recent_text, messages, expandable, recent_collapsed, prefix, max_chunks)
            except Exception as e:
                log.error("_answer task failed: %s", e)

        asyncio.create_task(_do())
        return messages
    
    async def _do_answer(self, text: str, messages: list | None = None, expandable: bool = False, collapsed: bool = True, prefix: str = "", max_chunks = None):
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
            tag = "blockquote expandable" if collapsed else "blockquote"
            bodies = [f"<{tag}>{escaped_prefix}{c}</blockquote>" for c in raw_chunks]
        else:
            bodies = raw_chunks

        for m, body in enumerate(bodies):
            if m < len(messages):
                try:
                    messages[m] = await self._edit(messages[m], body, parse_mode="HTML")
                except Exception as e:
                    if "message is not modified" not in str(e):
                        raise
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
        try:
            await self._edit(
                self._tool_msg,
                f"<blockquote expandable>{self._tool_call_text}</blockquote>\n<blockquote expandable>{result_text}</blockquote>",
                parse_mode="HTML",
            )
        except Exception:
            logging.debug("[transport] Не удалось обновить tool message", exc_info=True)

    async def send_message(self, text: str, stream_id=None):
        return await self._answer((text or "").strip() or "[…]", messages=stream_id)

    async def inject_message(self, text: str):
        sent = await self._send("[→]" + text)

    async def send_system_prompt(self, text: str):
        if not self.verbose: return
        await self._answer(text, expandable=True, prefix="🔧 ", max_chunks=1)

    async def send_thinking(self, text: str, stream_id=None, final: bool = False):
        return await self._answer(text, expandable=True, collapsed=final, prefix="🧠 ", messages=stream_id)

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

        async def _typing_loop():
            try:
                while True:
                    await self.bot.send_chat_action(chat_id=self.chat_id, action="typing", message_thread_id=self.thread_id)
                    await asyncio.sleep(4)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                log.debug("[transport] typing loop stopped for %s: %s", self.chat_id, e)

        typing_task = asyncio.create_task(_typing_loop())

        content_parts = []

        user_id = first.from_user.id if first.from_user else None

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
            if text and text.startswith("/"):
                text = text.split("@")[0] + (text[text.index(" "):] if " " in text else "")
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
            typing_task.cancel()
            return

        try:
            await self.process_message(
                content_parts=content_parts,
                user_message_id=first.message_id,
            )
        except Exception as e:
            logging.exception("Error processing message")
            await self._send(f"Произошла ошибка при обработке: {e}")
        finally:
            typing_task.cancel()

