"""WebAgent mode — a worker sub-agent reachable from a bookmarklet chat widget.

Architecture
------------
One tool invocation = one sub-agent "worker". The sub captures the parent's
transport (Telegram + Dashboard) via a MultiTransport that fans out to both
the parent's transport stack AND a fresh `WebAgentTransport`. When the tool
exits, the parent's dispatch loop auto-restores its transport back to itself
(agent.py:401).

Agent loop is 1:1 with upstream page-agent `PageAgentCore.execute`
([PageAgentCore.ts:196-349](lib/page-agent/packages/core/src/PageAgentCore.ts#L196-L349)):

- System prompt is static (see `PageSkill.SYSTEM_PROMPT`).
- The user message is rebuilt every step from `<agent_state>` (task+step),
  `<agent_history>` (past `<step_N>` blocks), and `<browser_state>`. We
  throw away `sub.memory` each step and stuff a single synthesized user turn
  in — `sub.llm()` ends up seeing `[system, user]`, exactly like upstream.
- One forced macro-tool `AgentOutput` per step (`tool_choice="AgentOutput"`),
  whose `action` branch carries the actual command. `done` terminates the
  task, `ask_user` blocks on the next user message, everything else is
  dispatched through `PageSkill.execute_action`.
- `self.history` lives in this file, not in `sub.memory` — memory is just a
  transport for the rebuilt user prompt.

- **WebAgentTransport** is a regular `WebTransport` mounted at
  `/{sub_id}/web_agent/`. The dashboard routes the widget needs for `lib.js`
  come from the parent's DashboardTransport getting `set_agent(sub)` as part
  of the MultiTransport cascade — they end up mounted under the sub's id too.
"""
import asyncio, json, logging, uuid
from datetime import datetime

from fastapi import WebSocket, WebSocketDisconnect

from agent import Skill, tool
from src.modes.web_agent.page_skill import PageSkill
from src.transport.multi import MultiTransport
from src.transport.web import WebTransport

MAX_STEPS = 40

log = logging.getLogger(__name__)


class WebAgentTransport(WebTransport):
    """Adds a request/response RPC layer on top of the chat-transport WebSocket.

    The widget's run.js receives `{type: 'action', method, args, request_id}`,
    calls into its embedded `Page` controller, and replies with
    `{type: 'action_result', request_id, result|error}`. `call_action` below
    turns that into a plain Python coroutine the skill can await.
    """

    def __init__(self, verbose: bool = False):
        super().__init__(prefix="/web_agent", verbose=verbose)
        self.ws: WebSocket | None = None
        # method stored alongside the future so we know how to recover when
        # a new connection supersedes the old one mid-action.
        self._pending: dict[str, tuple[str, asyncio.Future]] = {}
        # Incremented on every widget reconnect. The main task loop snapshots
        # this before observe and checks after LLM — if it changed, the widget
        # was replaced (e.g. user hit F5) and the step's plan is stale; we
        # discard it and retry from observe. Without this, pending actions
        # are resolved as "🔄 navigation success" by _on_ws_connect and the
        # agent happily dispatches clicks to a fresh, un-indexed PageController.
        self.generation = 0

    async def call_action(self, method: str, *args, timeout: float = 30.0):
        if not self.ws:
            raise RuntimeError("No page connected — open the bookmarklet first")
        request_id = uuid.uuid4().hex
        fut = asyncio.get_event_loop().create_future()
        self._pending[request_id] = (method, fut)
        try:
            await self.send({
                "type": "action",
                "method": method,
                "args": list(args),
                "request_id": request_id,
            })
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(request_id, None)

    async def _handle_ws_message(self, msg: dict):
        if msg.get("type") == "action_result":
            entry = self._pending.get(msg.get("request_id"))
            if entry and not entry[1].done():
                fut = entry[1]
                if msg.get("error"):
                    fut.set_exception(RuntimeError(msg["error"]))
                else:
                    fut.set_result(msg.get("result"))
            return
        await super()._handle_ws_message(msg)

    async def send(self, event: dict):
        # action — это RPC (клик/ввод/скролл). Его нельзя буферить: иначе при
        # реконнекте после навигации (которую сам же этот клик вызвал) новый
        # виджет увидит старый action в replay и выполнит его ещё раз →
        # бесконечный цикл навигаций. Буферим только transport-события (чат).
        if event.get("type") != "action":
            self._buffer.append(event)
        if self.ws:
            try:
                await self.ws.send_text(json.dumps(event, ensure_ascii=False))
            except Exception:
                self.ws = None

    async def _on_ws_connect(self, ws: WebSocket):
        """Single-tab transport with full lifecycle override.

        New connection kicks the previous widget out (code 4001 → run.js
        removes it from the old page). In-flight actions are treated as
        'click caused navigation, succeeded' — so the agent gets a success
        result for the click and just keeps going on the new page.
        getBrowserState in flight during nav is the degenerate case: we
        fail it, and PageSkill.get_context_prompt swallows the RuntimeError
        and returns empty (next iteration re-reads from the new page)."""
        if self.ws:
            try: await self.ws.close(code=4001)
            except Exception: pass
        self.ws = ws
        self.generation += 1

        for event in list(self._buffer):
            await ws.send_text(json.dumps(event, ensure_ascii=False))

        for method, fut in self._pending.values():
            if fut.done():
                continue
            if method == "getBrowserState":
                fut.set_exception(RuntimeError("widget reconnected"))
            else:
                fut.set_result("🔄 Действие вызвало переход на новую страницу")
        self._pending.clear()

        try:
            while True:
                data = await ws.receive_text()
                try:
                    msg = json.loads(data)
                except json.JSONDecodeError:
                    log.warning("ws: invalid JSON: %s", data[:200])
                    continue
                await self._handle_ws_message(msg)
        except WebSocketDisconnect:
            pass
        finally:
            if self.ws is ws:
                self.ws = None



def _extract_text(content_parts: list) -> str:
    return " ".join(
        p.get("text", "") for p in content_parts
        if isinstance(p, dict) and "text" in p
    ).strip()


def _assemble_user_prompt(task: str, history: list, browser_state: dict | None, step: int) -> str:
    """Build the user message page-agent-style ([PageAgentCore.ts:552](lib/page-agent/packages/core/src/PageAgentCore.ts#L552))."""
    out = []
    out.append("<agent_state>")
    out.append("<user_request>")
    out.append(task)
    out.append("</user_request>")
    out.append("<step_info>")
    out.append(f"Step {step + 1} of {MAX_STEPS} max possible steps")
    out.append(f"Current time: {datetime.now().isoformat(sep=' ', timespec='seconds')}")
    out.append("</step_info>")
    out.append("</agent_state>\n")

    out.append("<agent_history>")
    for h in history:
        if h["type"] == "step":
            idx = h["step"]
            r = h["reflection"]
            out.append(f"<step_{idx}>")
            out.append(f"Evaluation of Previous Step: {r.get('evaluation_previous_goal') or ''}")
            out.append(f"Memory: {r.get('memory') or ''}")
            out.append(f"Next Goal: {r.get('next_goal') or ''}")
            out.append(f"Action Results: {h['result']}")
            out.append(f"</step_{idx}>")
        elif h["type"] == "observation":
            out.append(f"<sys>{h['content']}</sys>")
    out.append("</agent_history>\n")

    out.append("<browser_state>")
    if browser_state:
        out.append(browser_state.get("header", ""))
        out.append(browser_state.get("content", ""))
        out.append(browser_state.get("footer", ""))
    else:
        # Happens when the agent navigated (e.g. clicked an external link)
        # to a page where the userscript doesn't match → no widget → no WS.
        # The agent has NO way to recover from this: we can't navigate back
        # from Python because only the widget can do that, and it isn't
        # there. The only valid move is `done` with a failure text.
        out.append(
            "⚠️ The widget is NOT available on the current page. "
            "A previous action navigated the browser to a URL that is outside "
            "the userscript's match list, so the widget was not injected. "
            "You have NO way to recover from Python — you cannot navigate, "
            "click, type, or run JS here. Your ONLY valid action now is "
            "`done` with success=false and a text explaining that the task "
            "failed because the browser left the controlled domain."
        )
    out.append("</browser_state>\n")

    return "\n".join(out)


class WebAgentModeSkill(Skill):
    @tool("Запустить web-agent: возвращает ссылку на страницу с букмарклетом для вставки чат-виджета на произвольный сайт. Создаётся один саб-агент-воркер, захватывающий управление основным чатом. Блокируется до /stop от пользователя.")
    async def start_web_agent(self):
        web_transport = WebAgentTransport(verbose=False)
        page_skill = PageSkill(web_transport)
        sub = await self.agent.spawn_subagent("web_agent",
            memory_providers=[],
            skills=[page_skill],
            transport=MultiTransport([self.agent.transport, web_transport]),
        )

        url = await web_transport.get_url('/')
        first_msg = (
            f"🔗 {url}\n"
            "Откройте ссылку и перетащите кнопку на панель закладок. "
            "Затем кликните её на любом сайте — появится чат-виджет.\n"
            "Для выхода: /stop"
        )
        try:
            await self.agent.transport.send_message(first_msg)
            while True:
                content_parts, _, trigger_answer = await sub.next_message()
                task = _extract_text(content_parts)
                if not task or not trigger_answer:
                    continue
                try:
                    await self._execute_task(sub, page_skill, task)
                except Exception as e:
                    log.warning("[web_agent] %s", e, exc_info=True)
                    await sub.transport.send_message(f"Ошибка: {e}")
                finally:
                    await sub.transport.send_processing(False)
        finally:
            web_transport.remove_routes()

    async def _execute_task(self, sub, page_skill: PageSkill, task: str):
        """Page-agent-style React loop: observe → think → act → loop until `done`.

        Mirrors [PageAgentCore.execute](lib/page-agent/packages/core/src/PageAgentCore.ts#L196).
        Uses standard `sub.dispatch_tool_calls` so the widget renders a normal
        tool card for `AgentOutput`. `done` is detected via `action_name` in
        the dispatch return value.

        Navigation robustness: widget snapshots its `generation` counter
        before observe; if it changes by dispatch time (agent's own click
        caused navigation → new widget), the step is discarded and retried.
        `stepDelay` at the tail gives navigations time to settle.
        """
        web_transport: WebAgentTransport = page_skill.transport
        history: list[dict] = []
        last_url = ""

        await sub.transport.send_processing(True)

        for step in range(MAX_STEPS):
            generation = web_transport.generation

            # observe
            browser_state = await page_skill.get_browser_state()
            current_url = (browser_state or {}).get("url", "") if browser_state else ""
            if current_url and current_url != last_url:
                history.append({"type": "observation", "content": f"Page navigated to → {current_url}"})
                last_url = current_url

            # assemble user prompt and swap it into sub.memory
            user_prompt = _assemble_user_prompt(task, history, browser_state, step)
            sub.memory.clear()
            await sub.memory.add_turn({"role": "user", "content": user_prompt})

            # think — one forced AgentOutput call, single action guaranteed
            turn = await sub.llm(tool_choice="AgentOutput", parallel_tool_calls=False)
            tool_calls = turn.get("tool_calls") or []
            if not tool_calls:
                # Gemini sometimes drops the forced tool call after emitting a
                # thinking block (stream ends mid-thought, no </thought>, no
                # tool_calls). Instead of aborting the whole task, push a sys
                # observation telling the model what went wrong and retry.
                log.warning("[web_agent] step %d: no tool_calls, turn=%r", step, turn)
                history.append({
                    "type": "observation",
                    "content": (
                        "⚠️ Your previous response did not call AgentOutput. "
                        "You MUST emit exactly one AgentOutput tool call per step. "
                        "Try again now."
                    ),
                })
                continue

            # Widget reconnected between observe and now → state is stale,
            # whatever the LLM decided is based on the wrong page. Discard.
            if web_transport.generation != generation:
                log.info("[web_agent] widget reconnected during step %d (gen %d → %d), retrying",
                         step, generation, web_transport.generation)
                continue

            # Reflection fields live in the tool_call args — grab them before
            # dispatch so we can stuff them into `<step_N>` blocks.
            args = json.loads(tool_calls[0]["function"].get("arguments") or "{}")
            reflection = {
                "evaluation_previous_goal": args.get("evaluation_previous_goal") or "",
                "memory": args.get("memory") or "",
                "next_goal": args.get("next_goal") or "",
            }

            # act — standard dispatch emits the tool card and runs our
            # PageSkill.dispatch_tool_call override.
            tool_turns = await sub.dispatch_tool_calls(turn)
            tool_content = json.loads(tool_turns[0]["content"]) if tool_turns else {}
            action_name = tool_content.get("action_name")
            result = tool_content.get("result") or tool_content.get("error") or ""

            if action_name == "done":
                await sub.transport.send_message(result)
                return

            history.append({
                "type": "step",
                "step": step + 1,
                "reflection": reflection,
                "result": result,
            })

            # Let navigation (if any) start before next observe — upstream
            # PageAgentCore uses stepDelay=0.4 for the same reason.
            await asyncio.sleep(0.5)

        await sub.transport.send_message(f"⚠️ Достигнут лимит шагов ({MAX_STEPS})")
