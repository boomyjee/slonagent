"""
Тесты PersonalityProvider.

Запуск:
    venv\\Scripts\\python -m pytest tests/test_personality.py -v
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from agent import Skill


class PassthroughCompressor(Skill):
    async def compress(self, turns): return turns


def make_agent(tmp_path):
    from agent import Agent
    return Agent(
        model_name="test",
        api_key="test",
        base_url="http://test",
        agent_dir=str(tmp_path),
        memory_compressor=PassthroughCompressor(),
    )


async def make_provider(tmp_path):
    from src.memory.providers.personality import PersonalityProvider
    agent = make_agent(tmp_path)
    p = PersonalityProvider()
    p.register(agent)
    await p.start()
    return p


class TestPersonalityProvider:

    @pytest.mark.asyncio
    async def test_create_and_read(self, tmp_path):
        p = await make_provider(tmp_path)
        p.create("hero", "Герой приключений", "Храбрый и отважный")
        desc, content = p._read("hero")
        assert desc == "Герой приключений"
        assert content == "Храбрый и отважный"

    @pytest.mark.asyncio
    async def test_create_duplicate_returns_error(self, tmp_path):
        p = await make_provider(tmp_path)
        p.create("hero", "Герой", "Контент")
        result = p.create("hero", "Другой", "Иной")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_load_activates_personalities(self, tmp_path):
        p = await make_provider(tmp_path)
        p.create("hero", "Герой", "Храбрый")
        p.create("wizard", "Маг", "Мудрый")
        result = p.load(["hero", "wizard"])
        assert result["active"] == ["hero", "wizard"]
        assert p._active == ["hero", "wizard"]

    @pytest.mark.asyncio
    async def test_load_unknown_returns_error(self, tmp_path):
        p = await make_provider(tmp_path)
        result = p.load(["nonexistent"])
        assert "error" in result

    @pytest.mark.asyncio
    async def test_update_active_personality(self, tmp_path):
        p = await make_provider(tmp_path)
        p.create("hero", "Герой", "Старый контент")
        p.load(["hero"])
        result = p.update("hero", "Новый контент")
        assert result == {"updated": "hero"}
        _, content = p._read("hero")
        assert content == "Новый контент"

    @pytest.mark.asyncio
    async def test_update_inactive_returns_error(self, tmp_path):
        p = await make_provider(tmp_path)
        p.create("hero", "Герой", "Контент")
        # hero не активирован
        result = p.update("hero", "Новый")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_active_persisted_to_file(self, tmp_path):
        p = await make_provider(tmp_path)
        p.create("hero", "Герой", "Контент")
        p.load(["hero"])
        assert os.path.exists(p._active_file)

    @pytest.mark.asyncio
    async def test_list_returns_all_personalities(self, tmp_path):
        p = await make_provider(tmp_path)
        p.create("alpha", "A", "")
        p.create("beta", "B", "")
        names = p._list()
        assert "alpha" in names
        assert "beta" in names

    @pytest.mark.asyncio
    async def test_get_context_prompt_contains_active(self, tmp_path):
        p = await make_provider(tmp_path)
        p.create("hero", "Герой", "Я смелый и решительный")
        p.load(["hero"])
        prompt = await p.get_context_prompt()
        assert "Я смелый и решительный" in prompt

    @pytest.mark.asyncio
    async def test_get_context_prompt_creates_common(self, tmp_path):
        p = await make_provider(tmp_path)
        await p.get_context_prompt()
        assert "common" in p._list()
        assert "common" in p._active

    @pytest.mark.asyncio
    async def test_inactive_not_in_prompt_content(self, tmp_path):
        p = await make_provider(tmp_path)
        p.create("hero", "Герой", "Секретный контент героя")
        # Не активируем — контент не должен попасть в промпт
        p.load([])
        prompt = await p.get_context_prompt()
        assert "Секретный контент героя" not in prompt

    @pytest.mark.asyncio
    async def test_active_list_reloaded_on_start(self, tmp_path):
        p = await make_provider(tmp_path)
        p.create("hero", "Герой", "")
        p.load(["hero"])

        # Создаём новый провайдер — должен прочитать .active.json
        p2 = await make_provider(tmp_path)
        assert "hero" in p2._active

    @pytest.mark.asyncio
    async def test_replace_replaces_text(self, tmp_path):
        p = await make_provider(tmp_path)
        p.create("hero", "Герой", "Старый контент\n\nВторой абзац")
        result = p.replace("hero", "Старый контент", "Новый контент")
        assert result == {"ok": True}
        _, content = p._read("hero")
        assert "Новый контент" in content
        assert "Старый контент" not in content
        assert "Второй абзац" in content

    @pytest.mark.asyncio
    async def test_replace_appends_when_old_empty(self, tmp_path):
        p = await make_provider(tmp_path)
        p.create("hero", "Герой", "Первый абзац")
        result = p.replace("hero", "", "Новый блок")
        assert result == {"ok": True}
        _, content = p._read("hero")
        assert "Первый абзац" in content
        assert "Новый блок" in content

    @pytest.mark.asyncio
    async def test_replace_deletes_when_new_empty(self, tmp_path):
        p = await make_provider(tmp_path)
        p.create("hero", "Герой", "Первый абзац\n\nУдалить это")
        result = p.replace("hero", "Удалить это", "")
        assert result == {"ok": True}
        _, content = p._read("hero")
        assert "Удалить это" not in content
        assert "Первый абзац" in content

    @pytest.mark.asyncio
    async def test_replace_not_found_returns_error(self, tmp_path):
        p = await make_provider(tmp_path)
        p.create("hero", "Герой", "Контент")
        result = p.replace("hero", "Несуществующий текст", "Новый")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_replace_unknown_personality_returns_error(self, tmp_path):
        p = await make_provider(tmp_path)
        result = p.replace("nonexistent", "", "текст")
        assert "error" in result
