import base64
import re

import requests
from typing import Annotated
from agent import Skill, tool

MAX_RESPONSE_SIZE = 5 * 1024 * 1024  # 5MB
DEFAULT_TIMEOUT = 30
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"


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
            response = requests.get(url, timeout=DEFAULT_TIMEOUT, headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
            })
            # Retry with honest UA if Cloudflare blocks
            if response.status_code == 403 and "cf-mitigated" in response.headers.get("cf-mitigated", ""):
                response = requests.get(url, timeout=DEFAULT_TIMEOUT, headers={
                    "User-Agent": "slonagent",
                    "Accept-Language": "en-US,en;q=0.9",
                })
            response.raise_for_status()
            if len(response.content) > MAX_RESPONSE_SIZE:
                return {"error": "Response too large (exceeds 5MB)"}

            content_type = response.headers.get("content-type", "")
            mime = content_type.split(";")[0].strip().lower()

            # Images → base64 attachment
            if mime.startswith("image/") and mime not in ("image/svg+xml",):
                b64 = base64.b64encode(response.content).decode()
                return {"_parts": [{"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}]}

            text = response.text

            if format == "markdown" and "html" in content_type:
                from markdownify import markdownify
                text = markdownify(text, heading_style="ATX", strip=["script", "style", "meta", "link"])
            elif format == "text" and "html" in content_type:
                text = _html_to_text(text)

            return {"url": url, "content": text, "content_type": content_type}
        except Exception as e:
            return {"error": str(e)}


def _html_to_text(html: str) -> str:
    import html as html_mod
    text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html_mod.unescape(text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text
