"""WebAgent mode — a worker sub-agent reachable from a bookmarklet chat widget.

One tool invocation = one sub-agent. Its transport is a MultiTransport that
fans out to both the parent's transport stack AND a fresh `WebAgentTransport`;
when the tool exits, agent.py's dispatch loop restores the parent's transport
automatically (agent.py:401).

Agent loop mirrors upstream page-agent
[PageAgentCore.execute](lib/page-agent/packages/core/src/PageAgentCore.ts#L196-L349):
the user message is rebuilt every step from <agent_state>+<agent_history>+
<browser_state> and stuffed into `sub.memory` as a single turn (so `sub.llm()`
sees `[system, user]` every time). `self.history` lives here, not in
`sub.memory` — memory is just a carrier for the synthesized user prompt.

`WebAgentTransport` mounts at `/{sub_id}/web_agent/`. Shared UI files
(lib.js, Chat.js) are served via WebTransport's cascading ui/ lookup.
"""
import asyncio, json, logging, uuid
from datetime import datetime

from fastapi import WebSocket

from agent import Skill, tool, bypass
from src.modes.web_agent.page_skill import PageSkill
from src.transport.multi import MultiTransport
from src.transport.web import WebTransport

MAX_STEPS = 40

log = logging.getLogger(__name__)


class WebAgentTransport(WebTransport):
    """Adds a request/response RPC layer on top of the chat WebSocket: the
    widget replies to `{type:'action', method, args, request_id}` with
    `{type:'action_result', ...}`, turned into an awaitable by `call_action`.
    """

    def __init__(self, verbose: bool = False):
        super().__init__(prefix="/web_agent", verbose=verbose)
        self._pending: dict[str, tuple[str, asyncio.Future]] = {}
        self.generation = 0

    async def call_action(self, method: str, *args, timeout: float = 30.0):
        if not self._clients:
            raise RuntimeError("No page connected — open the bookmarklet first")
        request_id = uuid.uuid4().hex
        fut = asyncio.get_event_loop().create_future()
        self._pending[request_id] = (method, fut)
        try:
            await self.send({"type": "action","method": method,"args": list(args),"request_id": request_id})
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(request_id, None)

    async def ws_handle_message(self, msg: dict):
        if msg.get("type") == "action_result":
            entry = self._pending.get(msg.get("request_id"))
            if entry and not entry[1].done():
                fut = entry[1]
                if msg.get("error"):
                    fut.set_exception(RuntimeError(msg["error"]))
                else:
                    fut.set_result(msg.get("result"))
            return
        await super().ws_handle_message(msg)

    async def ws_connect(self, ws: WebSocket):
        """Single-tab transport: a new widget kicks the old one out (close
        code 4001 → run.js removes itself from the old page). In-flight
        actions get resolved as "navigation success" except getBrowserState,
        which gets failed so PageSkill can retry the observe on the new page.
        """
        for old in self._clients:
            try: await old.close(code=4001)
            except Exception: pass
        self._clients = set()

        self.generation += 1

        for method, fut in self._pending.values():
            if fut.done(): continue
            if method == "getBrowserState":
                fut.set_exception(RuntimeError("widget reconnected"))
            else:
                fut.set_result("🔄 Действие вызвало переход на новую страницу")
        self._pending.clear()

        await super().ws_connect(ws)



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
        # Agent navigated to a page where no widget could be injected
        # (bookmarklet: didn't re-click; extension: content script blocked
        # by page CSP). Python can't recover from this — the only valid
        # action is `done` with success=false.
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
    def __init__(self, expose_tool: bool = True):
        super().__init__()
        self._expose_tool = expose_tool

    def get_tools(self):
        return super().get_tools() if self._expose_tool else []

    @bypass("webagent", "Запустить web-agent", standalone=True)
    async def launch_command(self,args:str):
        self.agent.call_before_next_message(self.launch())

    @tool("Запустить web-agent: возвращает ссылку на страницу с букмарклетом для вставки чат-виджета на произвольный сайт. Создаётся один саб-агент-воркер, захватывающий управление основным чатом. Блокируется до /stop от пользователя.")
    async def launch(self):
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
                task = " ".join(
                    p.get("text", "") for p in content_parts
                    if isinstance(p, dict) and "text" in p
                ).strip()
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
        """observe → think → act loop, mirrors upstream
        [PageAgentCore.execute](lib/page-agent/packages/core/src/PageAgentCore.ts#L196).
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

            user_prompt = _assemble_user_prompt(task, history, browser_state, step)
            sub.memory.clear()
            await sub.transport.send_system_prompt(f'[WebAgent] {user_prompt}')
            await sub.memory.add_turn({"role": "user", "content": user_prompt})

            # think
            turn = await sub.llm(tool_choice="AgentOutput", parallel_tool_calls=False)
            tool_calls = turn.get("tool_calls") or []
            if not tool_calls:
                # Gemini sometimes drops the forced tool call after a
                # thinking block (stream ends mid-thought). Push a <sys>
                # nudge and retry the step instead of aborting.
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

            # Stale plan: widget reconnected between observe and now.
            if web_transport.generation != generation:
                log.info("[web_agent] widget reconnected during step %d (gen %d → %d), retrying",
                         step, generation, web_transport.generation)
                continue

            args = json.loads(tool_calls[0]["function"].get("arguments") or "{}")
            reflection = {
                "evaluation_previous_goal": args.get("evaluation_previous_goal") or "",
                "memory": args.get("memory") or "",
                "next_goal": args.get("next_goal") or "",
            }

            # act
            tool_turns = await sub.dispatch_tool_calls(turn)
            tool_content = json.loads(tool_turns[0]["content"]) if tool_turns else {}
            action_name = tool_content.get("action_name")
            result = tool_content.get("result") or tool_content.get("error") or ""

            if action_name == "done":
                await sub.transport.send_message(result)
                return

            history.append({"type": "step","step": step + 1,"reflection": reflection,"result": result})

            # Give navigation time to settle before the next observe
            # (upstream PageAgentCore uses stepDelay=0.4).
            await asyncio.sleep(0.5)

        await sub.transport.send_message(f"⚠️ Достигнут лимит шагов ({MAX_STEPS})")
