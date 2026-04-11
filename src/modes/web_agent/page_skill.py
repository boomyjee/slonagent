"""PageSkill — macro-tool wrapper around `@page-agent/page-controller`.

One skill, one tool: `AgentOutput`. Schema mirrors upstream page-agent 1:1
([PageAgentCore.ts:360](lib/page-agent/packages/core/src/PageAgentCore.ts#L360)):
`{evaluation_previous_goal?, memory?, next_goal?, action: oneOf[...]}`.
The action branches (click_element_by_index, input_text, ...) are kept
verbatim from upstream [tools/index.ts](lib/page-agent/packages/core/src/tools/index.ts).

`dispatch_tool_call` is overridden to parse the macro tool — the standard
dispatch flow in agent.py still emits `on_tool_call`/`on_tool_result`, so
the widget renders a normal tool card for `AgentOutput`. `done` leaves its
text in the result (the main loop sees `action == "done"` and exits after
the card is rendered). `ask_user` blocks on `self.agent.next_message()`
and returns the answer as the tool result.
"""
import asyncio, json

from agent import Skill


SYSTEM_PROMPT = """You are an AI agent designed to operate in an iterative loop to automate browser tasks. Your ultimate goal is accomplishing the task provided in <user_request>.

<intro>
You excel at following tasks:
1. Navigating complex websites and extracting precise information
2. Automating form submissions and interactive web actions
3. Gathering and saving information
4. Operate effectively in an agent loop
5. Efficiently performing diverse web tasks
</intro>

<language_settings>
- Default working language: **English**
- Use the language that user is using. Return in user's language.
</language_settings>

<input>
At every step, your input will consist of:
1. <agent_history>: A chronological event stream including your previous actions and their results.
2. <agent_state>: Current <user_request> and <step_info>.
3. <browser_state>: Current URL, interactive elements indexed for actions, and visible page content.
</input>

<agent_history>
Agent history will be given as a list of step information as follows:

<step_{step_number}>:
Evaluation of Previous Step: Assessment of last action
Memory: Your memory of this step
Next Goal: Your goal for this step
Action Results: Your actions and their results
</step_{step_number}>

and system messages wrapped in <sys> tag.
</agent_history>

<user_request>
USER REQUEST: This is your ultimate objective and always remains visible.
- This has the highest priority. Make the user happy.
- If the user request is very specific - then carefully follow each step and dont skip or hallucinate steps.
- If the task is open ended you can plan yourself how to get it done.
</user_request>

<browser_state>
1. Browser State will be given as:

Current URL: URL of the page you are currently viewing.
Interactive Elements: All interactive elements will be provided in format as [index]<type>text</type> where
- index: Numeric identifier for interaction
- type: HTML element type (button, input, etc.)
- text: Element description

Examples:
[33]<div>User form</div>
\t*[35]<button aria-label='Submit form'>Submit</button>

Note that:
- Only elements with numeric indexes in [] are interactive
- (stacked) indentation (with \\t) is important and means that the element is a (html) child of the element above (with a lower index)
- Elements tagged with `*[` are the new clickable elements that appeared on the website since the last step - if url has not changed.
- Pure text elements without [] are not interactive.
</browser_state>

<browser_rules>
Strictly follow these rules while using the browser and navigating the web:
- Only interact with elements that have a numeric [index] assigned.
- Only use indexes that are explicitly provided.
- If the page changes after, for example, an input text action, analyze if you need to interact with new elements, e.g. selecting the right option from the list.
- By default, only elements in the visible viewport are listed. Use scrolling actions if you suspect relevant content is offscreen which you need to interact with. Scroll ONLY if there are more pixels below or above the page.
- You can scroll by a specific number of pages using the num_pages parameter (e.g., 0.5 for half page, 2.0 for two pages).
- All the elements that are scrollable are marked with `data-scrollable` attribute. Including the scrollable distance in every directions. You can scroll *the element* in case some area are overflowed.
- If a captcha appears, tell user you can not solve captcha. Finish the task and ask user to solve it.
- If expected elements are missing, try scrolling, or navigating back.
- If the page is not fully loaded, use the `wait` action.
- Do not repeat one action for more than 3 times unless some conditions changed.
- If you fill an input field and your action sequence is interrupted, most often something changed e.g. suggestions popped up under the field.
- If the <user_request> includes specific page information such as product type, rating, price, location, etc., try to apply filters to be more efficient.
- The <user_request> is the ultimate goal. If the user specifies explicit steps, they have always the highest priority.
- If you input_text into a field, you might need to press enter, click the search button, or select from dropdown for completion.
- Don't login into a page if you don't have to. Don't login if you don't have the credentials.
- There are 2 types of tasks always first think which type of request you are dealing with:
1. Very specific step by step instructions:
- Follow them as very precise and don't skip steps. Try to complete everything as requested.
2. Open ended tasks. Plan yourself, be creative in achieving them.
- If you get stuck e.g. with logins or captcha in open-ended tasks you can re-evaluate the task and try alternative ways, e.g. sometimes accidentally login pops up, even though there some part of the page is accessible or you get some information via web search.
</browser_rules>

<capability>
- You can only handle single page app. Do not jump out of current page.
- Do not click on link if it will open in a new page (e.g., <a target="_blank">)
- It is ok to fail the task.
\t- User can be wrong. If the request of user is not achievable, inappropriate or you do not have enough information or tools to achieve it. Tell user to make a better request.
\t- Webpage can be broken. All webpages or apps have bugs. Some bug will make it hard for your job. It's encouraged to tell user the problem of current page. Your feedbacks (including failing) are valuable for user.
\t- Trying too hard can be harmful. Repeating some action back and forth or pushing for a complex procedure with little knowledge can cause unwanted results and harmful side-effects. User would rather you complete the task with a fail.
- If you do not have knowledge for the current webpage or task. You must require user to give specific instructions and detailed steps.
</capability>

<task_completion_rules>
You must call the `done` action in one of three cases:
- When you have fully completed the USER REQUEST.
- When you reach the final allowed step (`max_steps`), even if the task is incomplete.
- When you feel stuck or unable to solve user request. Or user request is not clear or contains inappropriate content.
- If it is ABSOLUTELY IMPOSSIBLE to continue.

The `done` action is your opportunity to terminate and share your findings with the user.
- Set `success` to `true` only if the full USER REQUEST has been completed with no missing components.
- If any part of the request is missing, incomplete, or uncertain, set `success` to `false`.
- You can use the `text` field of the `done` action to communicate your findings and to provide a coherent reply to the user and fulfill the USER REQUEST.
- You are ONLY ALLOWED to call `done` as a single action. Don't call it together with other actions.
- If the user asks for specified format, such as "return JSON with following structure", "return a list of format...", MAKE sure to use the right format in your answer.
- If the user asks for a structured output, your `done` action's schema may be modified. Take this schema into account when solving the task!
</task_completion_rules>

<reasoning_rules>
Exhibit the following reasoning patterns to successfully achieve the <user_request>:

- Reason about <agent_history> to track progress and context toward <user_request>.
- Analyze the most recent "Next Goal" and "Action Result" in <agent_history> and clearly state what you previously tried to achieve.
- Analyze all relevant items in <agent_history> and <browser_state> to understand your state.
- Explicitly judge success/failure/uncertainty of the last action. Never assume an action succeeded just because it appears to be executed in your last step in <agent_history>. If the expected change is missing, mark the last action as failed (or uncertain) and plan a recovery.
- Analyze whether you are stuck, e.g. when you repeat the same actions multiple times without any progress. Then consider alternative approaches e.g. scrolling for more context or ask user for help.
- Ask user for help if you have any difficulty. Keep user in the loop.
- If you see information relevant to <user_request>, plan saving the information to memory.
- Always reason about the <user_request>. Make sure to carefully analyze the specific steps and information required. E.g. specific filters, specific form fields, specific information to search. Make sure to always compare the current trajectory with the user request and think carefully if thats how the user requested it.
</reasoning_rules>

<examples>
Here are examples of good output patterns. Use them as reference but never copy them directly.

<evaluation_examples>
"evaluation_previous_goal": "Successfully navigated to the product page and found the target information. Verdict: Success"
"evaluation_previous_goal": "Clicked the login button and user authentication form appeared. Verdict: Success"
</evaluation_examples>

<memory_examples>
"memory": "Found many pending reports that need to be analyzed in the main page. Successfully processed the first 2 reports on quarterly sales data and moving on to inventory analysis and customer feedback reports."
</memory_examples>

<next_goal_examples>
"next_goal": "Click on the 'Add to Cart' button to proceed with the purchase flow."
</next_goal_examples>
</examples>

<output>
{
  "evaluation_previous_goal": "Concise one-sentence analysis of your last action. Clearly state success, failure, or uncertain.",
  "memory": "1-3 concise sentences of specific memory of this step and overall progress. You should put here everything that will help you track progress in future steps. Like counting pages visited, items found, etc.",
  "next_goal": "State the next immediate goal and action to achieve it, in one clear sentence.",
  "action":{
    "Action name": {// Action parameters}
  }
}
</output>
"""


# Verbatim from page-agent tools/index.ts — each entry is one branch of
# the `action` oneOf in the AgentOutput macro tool.
ACTION_SCHEMAS: dict[str, dict] = {
    "done": {
        "description": "Complete task. Text is your final response to the user — keep it concise unless the user explicitly asks for detail.",
        "properties": {
            "text": {"type": "string"},
            "success": {"type": "boolean", "default": True},
        },
        "required": ["text"],
    },
    "wait": {
        "description": "Wait for x seconds. Can be used to wait until the page or data is fully loaded.",
        "properties": {
            "seconds": {"type": "integer", "minimum": 1, "maximum": 10, "default": 1},
        },
        "required": ["seconds"],
    },
    "ask_user": {
        "description": "Ask the user a question and wait for their answer. Use this if you need more information or clarification.",
        "properties": {
            "question": {"type": "string"},
        },
        "required": ["question"],
    },
    "click_element_by_index": {
        "description": "Click element by index",
        "properties": {
            "index": {"type": "integer", "minimum": 0},
        },
        "required": ["index"],
    },
    "input_text": {
        "description": "Click and type text into an interactive input element",
        "properties": {
            "index": {"type": "integer", "minimum": 0},
            "text": {"type": "string"},
        },
        "required": ["index", "text"],
    },
    "select_dropdown_option": {
        "description": "Select dropdown option for interactive element index by the text of the option you want to select",
        "properties": {
            "index": {"type": "integer", "minimum": 0},
            "text": {"type": "string"},
        },
        "required": ["index", "text"],
    },
    "scroll": {
        "description": "Scroll vertically. Without index: scrolls the document. With index: scrolls the container at that index (or its nearest scrollable ancestor). Use index of a data-scrollable element to scroll a specific area.",
        "properties": {
            "down": {"type": "boolean", "default": True},
            "num_pages": {"type": "number", "minimum": 0, "maximum": 10, "default": 0.1},
            "pixels": {"type": "integer", "minimum": 0},
            "index": {"type": "integer", "minimum": 0},
        },
        "required": [],
    },
    "scroll_horizontally": {
        "description": "Scroll horizontally. Without index: scrolls the document. With index: scrolls the container at that index (or its nearest scrollable ancestor).",
        "properties": {
            "right": {"type": "boolean", "default": True},
            "pixels": {"type": "integer", "minimum": 0},
            "index": {"type": "integer", "minimum": 0},
        },
        "required": ["pixels"],
    },
    "execute_javascript": {
        "description": "Execute JavaScript code on the current page. Supports async/await syntax. Use with caution!",
        "properties": {
            "script": {"type": "string"},
        },
        "required": ["script"],
    },
}


class PageSkill(Skill):
    def __init__(self, transport):
        super().__init__()
        self.transport = transport

    async def get_context_prompt(self, user_text: str = "") -> str:
        return SYSTEM_PROMPT

    def get_tools(self) -> list:
        action_branches = []
        for name, schema in ACTION_SCHEMAS.items():
            action_branches.append({
                "type": "object",
                "properties": {
                    name: {
                        "type": "object",
                        "description": schema["description"],
                        "properties": schema["properties"],
                        "required": schema["required"],
                    }
                },
                "required": [name],
            })
        return [{
            "type": "function",
            "function": {
                "name": "AgentOutput",
                "description": "You MUST call this tool every step!",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "evaluation_previous_goal": {"type": "string"},
                        "memory": {"type": "string"},
                        "next_goal": {"type": "string"},
                        "action": {"anyOf": action_branches},
                    },
                    "required": ["action"],
                },
            },
        }]

    async def get_browser_state(self, reconnect_timeout: float = 10.0) -> dict | None:
        # Nav actions leave a gap between widget #1 dying and #2 connecting —
        # poll briefly instead of failing immediately.
        deadline = asyncio.get_event_loop().time() + reconnect_timeout
        while self.transport.ws is None:
            if asyncio.get_event_loop().time() > deadline:
                return None
            await asyncio.sleep(0.1)
        try:
            return await self.transport.call_action("getBrowserState")
        except RuntimeError:
            return None

    async def dispatch_tool_call(self, tool_call: dict) -> dict:
        """Parse AgentOutput, execute one action, return `{action_name, result}`.

        The main loop reads `action_name` to detect `done` and to build
        `<agent_history>`. Agent.dispatch_tool_calls json-serializes the
        whole dict as the tool result content (so it also shows up in the
        widget tool card).
        """
        try:
            args = json.loads(tool_call["function"].get("arguments") or "{}")
        except json.JSONDecodeError:
            return {"error": "AgentOutput arguments are not valid JSON"}

        action = args.get("action") or {}
        action_name = next(iter(action), None)
        action_args = action.get(action_name) or {} if action_name else {}
        if action_name is None:
            return {"error": "AgentOutput.action is missing"}

        # Main loop exits on action_name == "done" after the tool card is
        # emitted — we just put the text in the result here.
        if action_name == "done":
            return {"action_name": "done", "result": action_args.get("text") or "(готово)"}

        if action_name == "ask_user":
            question = action_args.get("question") or ""
            await self.agent.transport.send_message(question)
            await self.agent.transport.send_processing(False)
            try:
                parts, _, _ = await self.agent.next_message()
            finally:
                await self.agent.transport.send_processing(True)
            answer = " ".join(
                p.get("text", "") for p in parts
                if isinstance(p, dict) and "text" in p
            ).strip()
            return {"action_name": "ask_user", "result": f"User answered: {answer}"}

        if action_name == "wait":
            seconds = max(1, min(10, action_args.get("seconds", 1)))
            await asyncio.sleep(seconds)
            return {"action_name": action_name, "result": f"✅ Waited for {seconds} seconds."}

        method_args: tuple
        if action_name == "click_element_by_index":
            method, method_args = "clickElement", (action_args["index"],)
        elif action_name == "input_text":
            method, method_args = "inputText", (action_args["index"], action_args["text"])
        elif action_name == "select_dropdown_option":
            method, method_args = "selectOption", (action_args["index"], action_args["text"])
        elif action_name == "scroll":
            opts = {"down": action_args.get("down", True)}
            if "num_pages" in action_args: opts["numPages"] = action_args["num_pages"]
            if "pixels" in action_args: opts["pixels"] = action_args["pixels"]
            if "index" in action_args: opts["index"] = action_args["index"]
            method, method_args = "scroll", (opts,)
        elif action_name == "scroll_horizontally":
            opts = {"right": action_args.get("right", True), "pixels": action_args["pixels"]}
            if "index" in action_args: opts["index"] = action_args["index"]
            method, method_args = "scrollHorizontally", (opts,)
        elif action_name == "execute_javascript":
            method, method_args = "executeJavascript", (action_args["script"],)
        else:
            return {"error": f"Unknown action: {action_name}"}

        try:
            raw = await self.transport.call_action(method, *method_args)
        except Exception as e:
            return {"action_name": action_name, "result": f"Error: {e}"}

        result = raw["message"] if isinstance(raw, dict) and "message" in raw else str(raw)
        return {"action_name": action_name, "result": result}
