import os, logging, httpx
from google import genai
from google.genai import types


class Agent:
    def __init__(self, model_name: str, api_key: str, skills: list = None, max_iterations: int = 20):
        self.model_name = model_name
        self.skills = skills or []
        self.messages = []
        self.max_iterations = max_iterations
        for skill in self.skills:
            skill.agent = self

        proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        http_client = httpx.Client(proxy=proxy_url) if proxy_url else None
        http_options = {"httpx_client": http_client, "api_version": "v1alpha"} if http_client else {"api_version": "v1alpha"}
        self.client = genai.Client(api_key=api_key, http_options=http_options)

    async def process_message(self, text: str, instructions: str = "", transport=None):
        logging.info("[agent] incoming: %r", text)

        for skill in self.skills:
            if hasattr(skill, "is_bypass_command") and skill.is_bypass_command(text):
                if transport: await transport.on_content(skill.handle_bypass_command(text))
                return

        self.messages.append({"role": "user", "content": text})

        tools = []
        tool_to_skill = {}
        for s in self.skills:
            for f in s.tools:
                tools.append(types.Tool(function_declarations=[f]))
                tool_to_skill[f.name] = s

        contents = [{"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["content"]}]} for m in self.messages[-20:]]
        skill_context = "\n\n".join(s.get_context_prompt() for s in self.skills if hasattr(s, "get_context_prompt"))
        system = "\n\n".join(filter(None, [instructions, skill_context]))

        try:
            config = types.GenerateContentConfig(system_instruction=system, temperature=0.7, tools=tools)
            response = self.client.models.generate_content(model=self.model_name, contents=contents, config=config)

            iteration = 0
            while response.function_calls and iteration < self.max_iterations:
                contents.append(response.candidates[0].content)
                for tool_call in response.function_calls:
                    logging.info("Инструмент: %s", tool_call.name)
                    skill = tool_to_skill.get(tool_call.name)
                    if not skill:
                        logging.warning("Tool %s not found in skills", tool_call.name)
                        continue

                    if transport: await transport.on_tool_call(tool_call.name, dict(tool_call.args or {}))
                    result = await skill.dispatch_tool_call(tool_call)
                    if transport: await transport.on_tool_result(tool_call.name, result)
                    contents.append({"role": "user", "parts": [{"text": f"Результат {tool_call.name}:\n{result}"}]})

                iteration += 1
                response = self.client.models.generate_content(model=self.model_name, contents=contents, config=config)

            self.messages.append({"role": "model", "content": response.text})
            if transport: await transport.on_content(response.text)

            for s in self.skills:
                if hasattr(s, "on_message_processed"): await s.on_message_processed(self.messages)

        except Exception as e:
            logging.exception("Ошибка при обращении к Gemini")
            if transport: await transport.on_content(f"Ошибка: {e}")
