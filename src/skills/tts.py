import asyncio
import base64
import logging
import os
import tempfile
from typing import Annotated

import numpy as np
import requests
import soundfile as sf

from agent import Skill, tool


# Полный список prebuilt-голосов Gemini TTS с характером (из публичной доки).
# Все голоса language-agnostic — поддерживают 24 языка, включая русский,
# определяя его автоматически по тексту.
_VOICES = {
    "Zephyr": "bright",
    "Puck": "upbeat",
    "Charon": "informative",
    "Kore": "firm",
    "Fenrir": "excitable",
    "Leda": "youthful",
    "Orus": "firm",
    "Aoede": "breezy",
    "Callirrhoe": "easy-going",
    "Autonoe": "bright",
    "Enceladus": "breathy",
    "Iapetus": "clear",
    "Umbriel": "easy-going",
    "Algieba": "smooth",
    "Despina": "smooth",
    "Erinome": "clear",
    "Algenib": "gravelly",
    "Rasalgethi": "informative",
    "Laomedeia": "upbeat",
    "Achernar": "soft",
    "Alnilam": "firm",
    "Schedar": "even",
    "Gacrux": "mature",
    "Pulcherrima": "forward",
    "Achird": "friendly",
    "Zubenelgenubi": "casual",
    "Vindemiatrix": "gentle",
    "Sadachbia": "lively",
    "Sadaltager": "knowledgeable",
    "Sulafat": "warm",
}

_MODEL_ID = "gemini-3.1-flash-tts-preview"

_VOICE_LIST_HINT = ", ".join(f"{n} ({c})" for n, c in _VOICES.items())


class TTSSkill(Skill):
    def __init__(self, api_key: str):
        self.api_key = api_key
        super().__init__()

    @tool(
        "Сгенерировать речь из текста (TTS) через Gemini 3.1 Flash TTS и отправить пользователю голосовым. "
        "Поддерживает русский и ещё 23 языка (определяются автоматически). "
        "В `directed_text` встраивай режиссёрские пометки естественным языком — модель их отыграет: "
        "ремарки в скобках ('(шёпотом)', '(пауза)', '(вздыхает)'), "
        "инструкции тона перед фразой ('Скажи устало и медленно: ...', "
        "'Произнеси с восторгом, делая акцент на словах X и Y: ...'), "
        "знаки препинания и многоточия для интонации, КАПС для подчёркнутых слов. "
        "Чем подробнее режиссура — тем лучше игра. "
        "В `text` идёт чистая версия без пометок — она уйдёт пользователю отдельным текстовым сообщением. "
        "Файл нигде не сохраняется."
    )
    async def generate_speech(
        self,
        text: Annotated[str, "Чистый текст без режиссёрских пометок — отправится пользователю как сообщение."],
        directed_text: Annotated[str, "Тот же текст, но с режиссурой для модели (скобки, инструкции тона, КАПС, паузы). Если пусто — берётся text как есть."] = "",
        voice: Annotated[str, f"Имя голоса с характером в скобках. Доступны: {_VOICE_LIST_HINT}."] = "Kore",
    ) -> dict:
        if voice not in _VOICES:
            return {"error": f"Голос '{voice}' не поддерживается. Доступные: {', '.join(_VOICES)}"}

        tts_input = directed_text or text
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{_MODEL_ID}:generateContent?key={self.api_key}"
        payload = {
            "contents": [{"parts": [{"text": tts_input}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}
                },
            },
        }
        proxy = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
        proxies = {"http": proxy, "https": proxy} if proxy else None

        try:
            response = await asyncio.to_thread(
                requests.post, url, json=payload, proxies=proxies, timeout=300,
            )
        except Exception as e:
            return {"error": f"Сбой запроса к TTS API: {e}"}
        if response.status_code != 200:
            return {"error": f"API Error {response.status_code}: {response.text[:500]}"}

        data = response.json()
        audio_b64, mime = None, "audio/L16;rate=24000"
        for cand in data.get("candidates", []):
            for part in cand.get("content", {}).get("parts", []):
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and "data" in inline:
                    audio_b64 = inline["data"]
                    mime = inline.get("mimeType") or inline.get("mime_type") or mime
                    break
            if audio_b64:
                break
        if not audio_b64:
            return {"error": "В ответе нет аудио.", "details": str(data)[:500]}

        pcm = base64.b64decode(audio_b64)
        # Gemini TTS отдаёт raw PCM 16-bit LE mono; sample rate из mime ("audio/L16;rate=24000").
        sample_rate = 24000
        if "rate=" in mime:
            try:
                sample_rate = int(mime.split("rate=")[1].split(";")[0].split(",")[0])
            except (ValueError, IndexError):
                pass

        fd, host_path = tempfile.mkstemp(suffix=".ogg", prefix="tts_")
        os.close(fd)
        try:
            audio = np.frombuffer(pcm, dtype="<i2")
            sf.write(host_path, audio, sample_rate, format="OGG", subtype="OPUS")

            try:
                await self.agent.transport.send_voice(host_path)
            except Exception as e:
                # Возвращаем ошибку наверх — агент должен знать что
                # голосовое не доехало (например телеграм-чат запрещает
                # voice notes), чтобы выбрать другую стратегию.
                logging.warning("[tts] send_voice failed", exc_info=True)
                return {"error": f"Голосовое не отправилось: {e}"}

            # Текст рядом — вспомогательный канал, его падение не критично.
            try:
                await self.agent.transport.send_message(text)
            except Exception:
                logging.warning("[tts] send_message failed", exc_info=True)
        finally:
            try: os.unlink(host_path)
            except OSError: pass

        return {
            "status": "success",
            "duration_sec": round(len(pcm) / (2 * sample_rate), 2),
            "voice": voice,
            "model": _MODEL_ID,
        }
