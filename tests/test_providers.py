"""
Тесты провайдеров памяти с мок-LLM.

Запуск:
    venv\\Scripts\\python -m pytest tests/test_providers.py -v
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)


# ═══════════════════════════════════════════════════════════════════════════════
# FactProvider — per-thread retain items
# ═══════════════════════════════════════════════════════════════════════════════

class TestFactProviderThreadGrouping:

    def _make_provider(self):
        from src.memory.providers.fact import FactProvider
        return FactProvider(model_name="test", api_key="test", base_url="http://test")

    def test_build_retain_items_single_thread(self):
        p = self._make_provider()
        thread_pending = [
            {"role": "user", "content": "марсиане атакуют",
             "_thread_id": "thr_a", "_timestamp": "2025-01-01T10:00:00"},
            {"role": "assistant", "content": "приготовь зонтик",
             "_thread_id": "thr_a", "_timestamp": "2025-01-01T10:00:01"},
        ]
        items = p._build_retain_items(thread_pending)
        # Один conversation-item на тред
        conv = [i for i in items if i.context == "conversation"]
        assert len(conv) == 1
        assert "марсиане" in conv[0].content
        assert "приготовь" in conv[0].content

    def test_consolidate_groups_by_thread(self, tmp_path, monkeypatch):
        """_consolidate с микшированным pending передаёт в retain N conversation-items (по одному на тред)."""
        captured = []
        import src.memory.providers.fact as fact_module
        monkeypatch.setattr(fact_module, "retain",
                            lambda items, *a, **kw: captured.extend(items))

        p = self._make_provider()
        # storage обходится — retain замокан, OpenAI клиент не дёргается
        mixed = [
            {"role": "user", "content": "марсиане",
             "_thread_id": "thr_a", "_timestamp": "2025-01-01T10:00:00"},
            {"role": "assistant", "content": "ответ A",
             "_thread_id": "thr_a", "_timestamp": "2025-01-01T10:00:01"},
            {"role": "user", "content": "лошадки",
             "_thread_id": "thr_b", "_timestamp": "2025-01-01T11:00:00"},
            {"role": "assistant", "content": "ответ B",
             "_thread_id": "thr_b", "_timestamp": "2025-01-01T11:00:01"},
        ]
        import asyncio
        asyncio.run(p._consolidate(mixed))

        conv_items = [i for i in captured if i.context == "conversation"]
        assert len(conv_items) == 2
        joined = [i.content for i in conv_items]
        assert any("марсиане" in c and "лошадки" not in c for c in joined)
        assert any("лошадки" in c and "марсиане" not in c for c in joined)
