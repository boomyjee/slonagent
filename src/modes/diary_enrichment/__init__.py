import json
import logging
import re
from pathlib import Path
from typing import Annotated

from agent import Skill, tool
from src.modes.diary_enrichment.diary import Diary, DiaryDay
from src.modes.diary_enrichment.enrichment import EnrichmentStore, _ID_RE
from src.modes.diary_enrichment.formatting import (
    feedback_note,
    format_enrich_proposal,
    format_glossary_add_proposal,
    format_glossary_update_proposal,
    is_approval,
    split_reply,
)
from src.modes.diary_enrichment.glossary import Glossary
from src.modes.diary_enrichment.photos import COLLAGE_MAX, find_day_photos, make_collage
from src.modes.diary_enrichment.protocol import append_soul, build_system_prompt
from src.modes.diary_enrichment.state import State

log = logging.getLogger(__name__)


class EnrichmentSkill(Skill):
    """Subagent skill with 4 tools for the enrichment loop."""

    def __init__(
        self,
        glossary: Glossary,
        enrichment_store: EnrichmentStore,
        week: list[DiaryDay],
        year: int,
        soul_path: Path,
        accumulated_feedback: list[str],
    ):
        super().__init__()
        self.glossary = glossary
        self.store = enrichment_store
        self.week = week
        self.year = year
        self.soul_path = soul_path
        self.accumulated_feedback = accumulated_feedback
        self.enrich_approved = False
        self.glossary_rejected = False
        self.enrichments_week: dict[str, str] = {}

    async def get_context_prompt(self, user_text: str = "") -> str:
        return build_system_prompt(
            self.soul_path, self.glossary, self.week, self.year, self.store,
        )

    @tool(
        "Сохранить аннотированные тексты дней недели. "
        "ОБЯЗАТЕЛЬНО передавать ВСЕ дни недели — нельзя пропустить ни один. "
        "Для каждого дня передаётся ПОЛНЫЙ текст дня с [ID:Category:Name] маркерами. "
        "Первый ID каждого дня — обязательно [ID:Loc:...]."
    )
    async def enrich_diary(self, days: Annotated[str, 'JSON массив объектов [{"date": "YYYY.MM.DD", "text": "полный аннотированный текст дня"}]'] = "") -> dict:
        transport = self.agent.transport
        week = self.week

        if self.glossary_rejected:
            return {"error": "enrich_diary отклонён: изменение глоссария было отклонено. Исправь глоссарий и повтори."}

        # Parse days from JSON string
        if isinstance(days, str):
            try:
                days = json.loads(days)
            except json.JSONDecodeError:
                return {"error": "days must be a JSON array of {date, text} objects"}

        # Normalize: dict → list
        if isinstance(days, dict):
            days = [{"date": k, "text": v} for k, v in days.items()]

        # Format check
        bad_items = [i for i, item in enumerate(days) if not item.get("date") or not item.get("text")]
        if bad_items:
            valid = ", ".join(d.date_str for d in week)
            return {"error": (
                f"FORMAT ERROR: items {bad_items} missing date or text. "
                f"Expected {{\"date\": \"YYYY.MM.DD\", \"text\": \"...\"}}. "
                f"Valid dates: {valid}"
            )}

        days_arg = {item["date"]: item["text"] for item in days}

        # Validation
        errors: list[str] = []
        valid_days: dict[str, str] = {}
        for ds, annotated in days_arg.items():
            orig = next((d.text for d in week if d.date_str == ds), None)
            if orig is None:
                valid = ", ".join(d.date_str for d in week)
                errors.append(f"Key \"{ds}\" not found. Valid keys: {valid}")
                continue
            ok, err = self.store.validate(orig, annotated)
            if not ok:
                errors.append(f"{ds}: {err}")
            else:
                valid_days[ds] = annotated

        # Check IDs exist in glossary
        glossary_dict = self.glossary.read_dict()
        for ds, annotated in list(valid_days.items()):
            unknown = []
            for m in _ID_RE.finditer(annotated):
                tag = m.group(0)
                glossary_key = tag[4:-1]
                if glossary_key.startswith("Loc:"):
                    continue
                if glossary_key not in glossary_dict:
                    unknown.append(tag)
            if unknown:
                errors.append(f"{ds}: IDs missing from glossary: {', '.join(unknown)}")
                valid_days.pop(ds)

        # Check all active days present
        active_dates = {d.date_str for d in week}
        submitted = set(days_arg.keys())
        missing = active_dates - submitted
        for ds in sorted(missing):
            errors.append(f"{ds}: day missing — all days are required")

        # Check location in header
        for ds, annotated in list(valid_days.items()):
            first_line = annotated.split("\n")[0]
            loc_in_header = bool(re.search(r'\[ID:Loc:', first_line))
            first_id = next(iter(_ID_RE.finditer(annotated)), None)
            if not loc_in_header:
                errors.append(
                    f"{ds}: location [ID:Loc:...] must be in the header line"
                )
                valid_days.pop(ds)
            elif first_id and not first_id.group(0).startswith("[ID:Loc:"):
                errors.append(
                    f"{ds}: first ID must be location [ID:Loc:...], "
                    f"not {first_id.group(0)}"
                )
                valid_days.pop(ds)

        if errors:
            err_text = "\n".join(errors)
            return {"error": "VALIDATION ERRORS:\n" + err_text}

        # Show proposal and wait for approval
        proposal = format_enrich_proposal(self.week, valid_days, self.glossary)
        await transport.send_message(proposal)

        while True:
            content_parts, _ = await self.agent.next_message()
            answer = " ".join(p.get("text", "") for p in content_parts if isinstance(p, dict)).strip()

            if is_approval(answer):
                # Save
                self.enrichments_week.update(valid_days)
                all_enc = self.store.load(self.year)
                all_enc.update(self.enrichments_week)
                self.store.save(self.year, all_enc)
                self.enrich_approved = True
                return {"status": "approved", "days_saved": len(valid_days)}
            else:
                feedback_text, soul_note = split_reply(answer)
                append_soul(self.soul_path, soul_note)
                self.accumulated_feedback.append(feedback_text)
                return {"error": feedback_note(self.accumulated_feedback)}

    @tool("Добавить новую запись в глоссарий")
    async def glossary_add(
        self,
        id: Annotated[str, "ID в формате Category:Name, напр. Pers:Arbuzov"],
        description: Annotated[str, "Полное описание сущности"],
    ) -> dict:
        if id.startswith("ID:"):
            id = id[3:]
        proposal = format_glossary_add_proposal(id, description)
        await self.agent.transport.send_message(proposal)

        content_parts, _ = await self.agent.next_message()
        answer = " ".join(p.get("text", "") for p in content_parts if isinstance(p, dict)).strip()

        if is_approval(answer):
            self.glossary.add(id, description)
            return {"result": f"Added ID:{id}"}
        else:
            feedback_text, soul_note = split_reply(answer)
            append_soul(self.soul_path, soul_note)
            self.glossary_rejected = True
            return {"error": f"ОТКЛОНЕНО: {feedback_text}" if feedback_text else "ОТКЛОНЕНО пользователем."}

    @tool("Обновить описание существующей записи в глоссарии")
    async def glossary_update(
        self,
        id: Annotated[str, "Существующий ID, напр. Pers:Arbuzov"],
        new_description: Annotated[str, "Новое полное описание"],
    ) -> dict:
        old_desc = self.glossary.read_dict().get(id, "")
        proposal = format_glossary_update_proposal(id, old_desc, new_description)
        await self.agent.transport.send_message(proposal)

        content_parts, _ = await self.agent.next_message()
        answer = " ".join(p.get("text", "") for p in content_parts if isinstance(p, dict)).strip()

        if is_approval(answer):
            self.glossary.update(id, new_description)
            return {"result": f"Updated ID:{id}"}
        else:
            feedback_text, soul_note = split_reply(answer)
            append_soul(self.soul_path, soul_note)
            self.glossary_rejected = True
            return {"error": f"ОТКЛОНЕНО: {feedback_text}" if feedback_text else "ОТКЛОНЕНО пользователем."}

    @tool("Задать уточняющие вопросы пользователю")
    async def ask_user(self, questions: Annotated[list[str], "Список вопросов"]) -> dict:
        date_from = self.week[0].date_str
        q_text = "\n".join(f"{i+1}. {q}" for i, q in enumerate(questions))
        await self.agent.transport.send_message(f"❓ **Вопросы по неделе {date_from}:**\n\n{q_text}")

        content_parts, _ = await self.agent.next_message()
        answer = " ".join(p.get("text", "") for p in content_parts if isinstance(p, dict)).strip()

        result, soul_note = split_reply(answer)
        append_soul(self.soul_path, soul_note)
        return {"answer": result or answer}


class DiaryEnrichmentSkill(Skill):
    """Main skill registered in .config.json. Provides the start_enrichment tool."""

    def __init__(
        self,
        data_dir: str,
        photos_dir: str,
        model_name: str,
        api_key: str,
        base_url: str,
        fallback_model_name: str,
        fallback_api_key: str,
        fallback_base_url: str,
        years: list[int] | None = None,
    ):
        super().__init__()
        self._data_dir = Path(data_dir)
        self._photos_dir = Path(photos_dir)
        self._years = years or list(range(2014, 2025))
        self._model_name = model_name
        self._api_key = api_key
        self._base_url = base_url
        self._fallback_model_name = fallback_model_name
        self._fallback_api_key = fallback_api_key
        self._fallback_base_url = fallback_base_url

    def _resolve_paths(self):
        data_dir = self._data_dir
        photos_dir = self._photos_dir
        soul_path = data_dir / "SOUL.md"
        state_path = data_dir / "state.json"
        return data_dir, photos_dir, soul_path, state_path

    @tool(
        "Запустить обогащение дневника ID-аннотациями. "
        "LLM читает дневник понедельно, расставляет [ID:Category:Name] маркеры, "
        "пользователь одобряет/отклоняет. Можно указать rerun_date для повтора конкретной недели."
    )
    async def start_enrichment(self, rerun_date: Annotated[str, "Дата для повтора конкретной недели (YYYY.MM.DD), или пусто"] = "") -> dict:
        data_dir, photos_dir, soul_path, state_path = self._resolve_paths()
        diary = Diary(data_dir, self._years)
        glossary = Glossary(data_dir / "glossary.md")
        store = EnrichmentStore(data_dir)

        transport = self.agent.transport
        total_weeks = 0
        total_ids = 0

        if rerun_date:
            result = diary.find_week_by_date(rerun_date)
            if not result:
                return {"error": f"Date {rerun_date} not found in diaries."}
            week, year = result
            await self._send_week_preview(transport, week, photos_dir)
            ok = await self._run_enrichment_loop(week, year, glossary, store, soul_path, self._model_name, self._api_key, self._base_url)
            if ok:
                store.build_compiled(year, diary, glossary)
                total_weeks = 1
            return {"status": "done", "weeks_processed": total_weeks}

        state = State.load(state_path)
        await transport.send_message(
            f"✅ Начинаем обогащение. Позиция: **{state.year}**, неделя **{state.week_idx + 1}**."
        )

        for year in self._years:
            if year < state.year:
                continue
            days = diary.parse_year(year)
            if not days:
                continue
            weeks = diary.group_into_weeks(days)
            start_idx = state.week_idx if year == state.year else 0

            for week_idx in range(start_idx, len(weeks)):
                week = weeks[week_idx]

                await self._send_week_preview(transport, week, photos_dir)
                ok = await self._run_enrichment_loop(week, year, glossary, store, soul_path, self._model_name, self._api_key, self._base_url)

                if ok:
                    store.build_compiled(year, diary, glossary)
                    enrichments = store.load(year)
                    used_ids: set[str] = set()
                    for ds in (d.date_str for d in week):
                        for m in _ID_RE.finditer(enrichments.get(ds, "")):
                            used_ids.add(m.group(0)[1:-1])
                    total_ids += len(used_ids)
                    total_weeks += 1

                    date_from = week[0].date_str
                    date_to = week[-1].date_str
                    await transport.send_message(
                        f"✅ **Неделя {date_from} -- {date_to} сохранена.** Использовано ID: {len(used_ids)}."
                    )

                state.year = year
                state.week_idx = week_idx + 1
                state.save(state_path)

            state.year = year + 1
            state.week_idx = 0
            state.save(state_path)

        return {"status": "done", "weeks_processed": total_weeks, "total_ids": total_ids}

    async def _send_week_preview(self, transport, week: list[DiaryDay], photos_dir: Path):
        """Send photos and diary text for each day of the week."""
        from src.transport.telegram import TelegramTransport
        is_telegram = isinstance(transport, TelegramTransport)

        for day in week:
            photos = find_day_photos(photos_dir, day.date_str)
            if photos and is_telegram:
                from aiogram.types import BufferedInputFile
                for i in range(0, len(photos), COLLAGE_MAX):
                    buf = make_collage(photos[i:i + COLLAGE_MAX])
                    try:
                        photo = BufferedInputFile(buf.read(), filename=f"{day.date_str}_{i}.jpg")
                        await transport.bot.send_photo(
                            transport.chat_id, photo,
                            message_thread_id=transport.thread_id,
                        )
                    finally:
                        buf.close()

            text_body = day.text.removeprefix(day.date_str).lstrip()
            await transport.send_message(f"📖 **{day.date_str}** {text_body}")

    async def _run_enrichment_loop(
        self,
        week: list[DiaryDay],
        year: int,
        glossary: Glossary,
        store: EnrichmentStore,
        soul_path: Path,
        model_name: str = "",
        api_key: str = "",
        base_url: str = "",
    ) -> bool:
        """Run the enrichment loop for one week. Returns True if approved."""
        from agent import BadFinishReason

        transport = self.agent.transport

        week_text = "\n\n".join(d.text for d in week)
        date_from = week[0].date_str
        date_to = week[-1].date_str
        week_date_list = ", ".join(d.date_str for d in week)

        accumulated_feedback: list[str] = []
        skill = EnrichmentSkill(
            glossary=glossary,
            enrichment_store=store,
            week=week,
            year=year,
            soul_path=soul_path,
            accumulated_feedback=accumulated_feedback,
        )

        sub = await self.agent.spawn_subagent(
            "diary_enrichment",
            memory_providers=[],
            skills=[skill],
            model_name=model_name,
            api_key=api_key,
            base_url=base_url,
        )

        await sub.memory.add_turn({"role": "user", "content": (
            f"[DIARY WEEK {date_from} — {date_to}]\n"
            f"Days (use these as keys in enrich_diary): {week_date_list}\n\n"
            f"{week_text}"
        )})

        max_iterations = 20
        for iteration in range(max_iterations):
            await transport.send_message(f"_🤔 LLM думает (итерация {iteration + 1})..._")
            await sub.transport.send_processing(True)
            try:
                tool_calls, text = await sub.llm()
            except BadFinishReason as e:
                await sub.transport.send_processing(False)
                if self._fallback_model_name and model_name != self._fallback_model_name:
                    await transport.send_message(
                        f"LLM отказал ({e.reason}), переключаюсь на {self._fallback_model_name}"
                    )
                    return await self._run_enrichment_loop(
                        week, year, glossary, store, soul_path,
                        model_name=self._fallback_model_name,
                        api_key=self._fallback_api_key,
                        base_url=self._fallback_base_url,
                    )
                await transport.send_message(f"LLM отказал: {e.reason}. Пропускаю неделю.")
                return False
            await sub.transport.send_processing(False)

            if tool_calls:
                names = ", ".join(c["function"]["name"] for c in tool_calls)
                await transport.send_message(f"🔧 Инструменты: {names}")

            if not tool_calls:
                await sub.memory.add_turn({"role": "assistant", "content": text or ""})
                await sub.memory.add_turn({"role": "user", "content": (
                    "Ты не вызвал enrich_diary. Обработай неделю и вызови enrich_diary со всеми днями."
                )})
                continue

            # Glossary calls first, then others (1:1 from original)
            _glossary_names = {"enrichment_glossary_add", "enrichment_glossary_update"}
            glossary_calls = [c for c in tool_calls if c["function"]["name"] in _glossary_names]
            other_calls = [c for c in tool_calls if c["function"]["name"] not in _glossary_names]

            skill.glossary_rejected = False
            await sub.dispatch_tool_calls(glossary_calls + other_calls)

            if skill.enrich_approved:
                return True

        log.warning("[diary_enrichment] max iterations reached for week %s", date_from)
        await transport.send_message(f"⚠️ Достигнут лимит итераций для недели {date_from}.")
        return False
