import base64
import html as html_mod
import io
import ipaddress
import re
import socket
from urllib.parse import urljoin, urlparse

import requests
from markdownify import markdownify
from PIL import Image
from typing import Annotated
from agent import Skill, tool

MAX_TEXT_CHARS = 50_000
MAX_IMAGE_SIDE = 1024
DEFAULT_TIMEOUT = 30
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"


def _public_host_error(url: str) -> str | None:
    """None если хост URL резолвится только в публичные IP, иначе текст ошибки.
    Защита от SSRF: агент по prompt-injection не должен скачать внутренний адрес
    (cloud-metadata 169.254.169.254, localhost, приватные сети)."""
    host = urlparse(url).hostname
    if not host:
        return "URL без хоста"
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return f"не удалось разрешить хост: {host}"
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return f"внутренний адрес заблокирован: {ip}"
    return None


def _safe_get(url: str, headers: dict, max_redirects: int = 5):
    """requests.get с защитой от SSRF. Редиректы следуем вручную и проверяем хост
    на каждом хопе — иначе редирект на внутренний адрес обошёл бы проверку.
    (DNS-rebinding не покрыт — резолв в проверке и в самом запросе могут разойтись.)"""
    for _ in range(max_redirects + 1):
        err = _public_host_error(url)
        if err:
            raise ValueError(err)
        resp = requests.get(url, timeout=DEFAULT_TIMEOUT, headers=headers, allow_redirects=False)
        if resp.is_redirect and resp.headers.get("location"):
            url = urljoin(url, resp.headers["location"])
            continue
        return resp
    raise ValueError("слишком много редиректов")


class WebSkill(Skill):
    def __init__(self, api_key: str):
        self.api_key = api_key
        super().__init__()

    @tool("Выполнить поиск в интернете. Возвращает заголовки, ссылки и описания.")
    def search(
        self,
        query: Annotated[str, "Поисковый запрос"],
        count: Annotated[int, "Количество результатов (1-20, по умолчанию 10)"] = 10,
    ) -> dict:
        try:
            response = requests.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers={"Accept": "application/json", "Accept-Encoding": "gzip", "X-Subscription-Token": self.api_key},
                params={"q": query, "count": min(max(count, 1), 20)},
                timeout=DEFAULT_TIMEOUT,
            )
            response.raise_for_status()
            results = response.json().get("web", {}).get("results", [])
            return {
                "query": query,
                "total_results": len(results),
                "items": [{"title": r.get("title"), "url": r.get("url"), "description": r.get("description"), "age": r.get("age")} for r in results],
            }
        except Exception as e:
            return {"error": str(e)}

    @tool("Скачать и прочитать содержимое веб-страницы по URL. По умолчанию конвертирует HTML в markdown.")
    def fetch(
        self,
        url: Annotated[str, "URL страницы"],
        format: Annotated[str, "Формат: markdown (по умолчанию), text или html"] = "markdown",
    ) -> dict:
        if not url.startswith(("http://", "https://")):
            return {"error": "URL must start with http:// or https://"}
        try:
            response = _safe_get(url, {
                "User-Agent": USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
            })
            # Retry with honest UA if Cloudflare blocks
            if response.status_code == 403 and "cf-mitigated" in response.headers.get("cf-mitigated", ""):
                response = _safe_get(url, {
                    "User-Agent": "slonagent",
                    "Accept-Language": "en-US,en;q=0.9",
                })
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            mime = content_type.split(";")[0].strip().lower()

            # Images → resize and base64 attachment
            if mime.startswith("image/") and mime not in ("image/svg+xml",):
                img = Image.open(io.BytesIO(response.content))
                img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=85)
                b64 = base64.b64encode(buf.getvalue()).decode()
                return {"_parts": [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}

            text = response.text

            if format == "markdown" and "html" in content_type:
                text = markdownify(text, heading_style="ATX", strip=["script", "style", "meta", "link"])
            elif format == "text" and "html" in content_type:
                text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
                text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
                text = re.sub(r'<[^>]+>', ' ', text)
                text = html_mod.unescape(text)
                text = re.sub(r'\s+', ' ', text).strip()

            result = {"url": url, "content_type": content_type}
            if len(text) > MAX_TEXT_CHARS:
                result["content"] = text[:MAX_TEXT_CHARS]
                result["error"] = "Overflow, output truncated"
            else:
                result["content"] = text
            return result
        except Exception as e:
            return {"error": str(e)}
