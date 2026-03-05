import requests
from typing import Annotated
from agent import Skill, tool


class BraveSearchSkill(Skill):
    def __init__(self, api_key: str):
        self.api_key = api_key
        super().__init__()

    @tool("Выполнить поиск в интернете через Brave Search API. Возвращает заголовки, ссылки и описания.")
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
