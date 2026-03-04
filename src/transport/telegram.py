import html
import io, os, re, asyncio, logging, json, mimetypes

log = logging.getLogger(__name__)
from typing import Annotated
from aiogram import Bot, Dispatcher
from aiogram.types import Message, FSInputFile, InputMediaPhoto, InputMediaDocument, LinkPreviewOptions
from aiogram.client.session.aiohttp import AiohttpSession
from agent import Skill, tool
from google.genai import types




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


def _split_message(content: str, max_len: int = 3500) -> list[str]:
    """Split content into chunks ≤ max_len, preferring line breaks over word breaks.

    Default limit is 3500 (not 4096) to leave headroom for HTML tag expansion
    when markdown is converted after splitting.
    """
    if len(content) <= max_len:
        return [content]
    chunks: list[str] = []
    while content:
        if len(content) <= max_len:
            chunks.append(content)
            break
        cut = content[:max_len]
        pos = cut.rfind('\n')
        if pos == -1:
            pos = cut.rfind(' ')
        if pos == -1:
            pos = max_len
        chunks.append(content[:pos])
        content = content[pos:].lstrip()
    return chunks


class TelegramSkill(Skill):
    def __init__(self, bot: Bot):
        self.bot = bot
        self._message = None
        super().__init__()

    def set_message(self, message):
        self._message = message

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
            await self._message.answer_document(FSInputFile(host_paths[0]))
        else:
            media = [InputMediaDocument(media=FSInputFile(p)) for p in host_paths]
            await self._message.answer_media_group(media)
        return {"status": "ok"}

    @tool("Отправить одно или несколько изображений как альбом (до 10). Один вызов = один альбом. Для нескольких альбомов — вызови несколько раз.")
    async def send_images(self, paths: Annotated[list[str], "Список путей к изображениям внутри контейнера."]):
        host_paths = self._resolve_paths(paths)
        if isinstance(host_paths, dict):
            return host_paths
        logging.info("[skill] send_images: %s", host_paths)
        if len(host_paths) == 1:
            await self._message.answer_photo(FSInputFile(host_paths[0]))
        else:
            media = [InputMediaPhoto(media=FSInputFile(p)) for p in host_paths]
            await self._message.answer_media_group(media)
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

        tg_file = await self.bot.get_file(tg_file_id)
        os.makedirs(os.path.dirname(host_dest), exist_ok=True)
        await self.bot.download_file(tg_file.file_path, host_dest)
        logging.info("[skill] download_file: %s → %s", tg_file_id, host_dest)
        return {"status": "ok", "saved_to": dest_path}


class TelegramTransport:
    def __init__(self, bot_token: str, allowed_user_ids: set[int], verbose: bool = True):
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        self.bot = Bot(token=bot_token, session=AiohttpSession(proxy=proxy) if proxy else None)
        self.agent = None
        self.allowed_user_ids = allowed_user_ids
        self._no_link_preview = LinkPreviewOptions(is_disabled=True)
        self.dp = Dispatcher()
        self.dp.message()(self._handle_message)

        self._skill = TelegramSkill(self.bot)

        self.verbose = verbose
        self._current_message: Message | None = None
        self._media_groups: dict[str, list[Message]] = {}
        self._media_group_tasks: dict[str, asyncio.Task] = {}
        self._pending_answer = {}

    def set_agent(self, agent):
        self.agent = agent
        self._skill.register(agent)
        agent.skills.insert(0, self._skill)
        
    async def on_user_message(self, text: str):
        pass

    async def on_tool_call(self, name: str, args: dict):
        if not self.verbose: return
        lines = "\n".join(f"  {html.escape(k)}: {html.escape(str(v))}" for k, v in args.items())
        call_text = f"<b>[{html.escape(name)}]</b>\n{lines}" if lines else f"<b>[{html.escape(name)}]</b>"
        self._tool_call_text = call_text[:2000]
        self._tool_msg = await self._current_message.answer(
            f"<blockquote expandable>{self._tool_call_text}</blockquote>",
            parse_mode="HTML",
            link_preview_options=self._no_link_preview,
        )

    async def on_tool_result(self, name: str, result):
        if not self.verbose: return
        if isinstance(result, dict):
            parts = [f"<b>[{html.escape(k)}]</b>\n{html.escape(str(v))}" for k, v in result.items() if v not in (None, "", [], {})]
            result_text = "\n".join(parts) if parts else "(пусто)"
        else:
            result_text = html.escape(result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2))
        result_text = result_text[:2000]
        try:
            await self._tool_msg.edit_text(
                f"<blockquote expandable>{self._tool_call_text}</blockquote>\n<blockquote expandable>{result_text}</blockquote>",
                parse_mode="HTML",
                link_preview_options=self._no_link_preview,
            )
        except Exception:
            logging.debug("[transport] Не удалось обновить tool message", exc_info=True)

    # throttle
    async def _answer(self, text: str, messages: list | None = None, expandable: bool = False, prefix: str = ""):
        if messages is None: 
            messages = []
            await self._do_answer(text, messages, expandable, prefix)
            return messages

        key = id(messages)
        if self._pending_answer.get(key):
            self._pending_answer[key] = text
            return messages
        self._pending_answer[key] = text

        THROTLE_DELAY = 1.0
        async def _do():
            await asyncio.sleep(THROTLE_DELAY)
            recent_text = self._pending_answer[key]
            self._pending_answer[key] = None
            await self._do_answer(recent_text,messages,expandable,prefix)
        
        asyncio.create_task(_do())
        return messages

    async def _do_answer(self, text: str, messages: list | None = None, expandable: bool = False, prefix: str = ""):
        chunks = _split_message(text)
        for m, chunk in enumerate(chunks):
            if expandable:
                body = f"<blockquote expandable>{html.escape(prefix + chunk)}</blockquote>"
            else:
                body = _markdown_to_html(chunk)
            if m < len(messages):
                try:
                    messages[m] = await messages[m].edit_text(body, parse_mode="HTML", link_preview_options=self._no_link_preview)
                except Exception as e:
                    if "message is not modified" not in str(e):
                        raise
            else:
                messages.append(await self._current_message.answer(body, parse_mode="HTML", link_preview_options=self._no_link_preview))
        for msg in messages[len(chunks):]:
            try:
                await msg.delete()
            except Exception:
                pass
        del messages[len(chunks):]

    async def send_message(self, text: str, stream_id=None):
        return await self._answer((text or "").strip() or "[…]", messages=stream_id)

    async def inject_message(self, text: str):
        for chat_id in self.allowed_user_ids:
            sent = await self.bot.send_message(chat_id, "[→]" + text, link_preview_options=self._no_link_preview)
            self._current_message = sent
            self._tool_msg = None
            self._tool_call_text = ""
            self._skill.set_message(sent)
            await self.agent.process_message(
                message_parts=[{"text": text}],
                user_message_id=sent.message_id,
                user_query=text,
            )
            break

    async def send_system_prompt(self, text: str):
        if not self.verbose: return
        await self._answer(text[:3500] + ("..." if len(text) > 3500 else ""), expandable=True, prefix="🔧 ")

    async def send_thinking(self, text: str, stream_id=None):
        return await self._answer(text, expandable=True, prefix="🧠 ", messages=stream_id)

    async def send_code(self, lang: str, code: str):
        await self._answer(f"```{lang}\n{code}\n```")

    async def _handle_message(self, message: Message):
        if message.from_user.id not in self.allowed_user_ids:
            return

        if message.media_group_id:
            group_id = message.media_group_id
            self._media_groups.setdefault(group_id, []).append(message)
            if group_id in self._media_group_tasks:
                self._media_group_tasks[group_id].cancel()
            self._media_group_tasks[group_id] = asyncio.create_task(
                self._flush_media_group(group_id)
            )
        else:
            await self._process_messages([message])

    async def _flush_media_group(self, group_id: str):
        await asyncio.sleep(0.1)
        messages = self._media_groups.pop(group_id, [])
        self._media_group_tasks.pop(group_id, None)
        await self._process_messages(messages)

    async def _download_file(self, file_id: str) -> bytes:
        buf = io.BytesIO()
        await self.bot.download_file((await self.bot.get_file(file_id)).file_path, buf)
        return buf.getvalue()

    async def _typing_loop(self, chat_id: int) -> None:
        try:
            while True:
                await self.bot.send_chat_action(chat_id=chat_id, action="typing")
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.debug("[transport] typing loop stopped for %s: %s", chat_id, e)

    async def _process_messages(self, messages: list[Message]):
        first = messages[0]
        self._current_message = first
        self._tool_msg = None
        self._tool_call_text = ""

        self._skill.set_message(first)
        typing_task = asyncio.create_task(self._typing_loop(first.chat.id))

        message_parts = []
        user_texts = []

        for message in messages:
            text = message.text or message.caption
            if text:
                message_parts.append({"text": text})
                user_texts.append(text)

            if message.photo:
                message_parts.append(
                    types.Part.from_bytes(data=await self._download_file(message.photo[-1].file_id), mime_type="image/jpeg")
                )

            attachment, field = None, None
            if message.photo: attachment, field = message.photo[-1], "photo"
            elif message.audio: attachment, field = message.audio, "audio"
            elif message.video: attachment, field = message.video, "video"
            elif message.voice: attachment, field = message.voice, "voice"
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

            text_extensions = {
                ".txt", ".md", ".py", ".js", ".ts", ".json", ".yaml", ".yml", ".xml",
                ".html", ".css", ".sh", ".sql", ".csv", ".log",
            }
            content = None
            if field == "document" and os.path.splitext(filename or "")[1].lower() in text_extensions:
                try:
                    content = (await self._download_file(attachment.file_id)).decode("utf-8", errors="replace")
                except Exception:
                    logging.exception("[transport] Не удалось прочитать текстовый файл %s", filename)

            if field == "voice":
                try:
                    content = await self.agent.transcribe_audio(await self._download_file(attachment.file_id), "audio/ogg")
                except Exception:
                    logging.exception("[transport] Не удалось транскрибировать голосовое %s", attachment.file_id)

            attrs = " ".join(f'{k}="{v}"' for k, v in file_meta.items())
            if content:
                message_parts.append({
                    "text": f"<attached_file {attrs}>\n<content>\n{content}\n</content>\n</attached_file>",
                    "_document_id": f"{attachment.file_unique_id}_{filename}",
                })
            else:
                message_parts.append({"text": f"<attached_file {attrs} />"})

        if user_texts:
            await self.on_user_message(" ".join(user_texts).strip())

        try:
            await self.agent.process_message(
                message_parts=message_parts,
                user_message_id=first.message_id,
                user_query=" ".join(user_texts).strip(),
            )
        except Exception as e:
            logging.exception("Error processing message")
            await first.answer(f"Произошла ошибка при обработке: {e}", link_preview_options=self._no_link_preview)
        finally:
            typing_task.cancel()

    async def start(self):
        logging.info("Starting TelegramTransport...")
        await self.dp.start_polling(self.bot)
