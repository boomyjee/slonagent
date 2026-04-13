"""Stub library for slonagent sandbox scripts.

Usage:
    from agent import Skill, tool
    from src.transport.web import WebTransport
"""


def tool(description: str):
    def decorator(fn):
        fn._is_tool = True
        fn._tool_description = description
        return fn
    return decorator


class Skill:
    def __init__(self):
        self.agent = None  # set by runner to a Proxy

    async def register(self, agent):
        self.agent = agent

    async def start(self):
        pass

    async def get_context_prompt(self, user_text=""):
        return ""

    async def get_tool_prompt(self, tool_name):
        return ""

    async def get_tools(self):
        return []

    def get_bypass_commands(self, standalone_only=False):
        return {}

    async def is_bypass_command(self, text):
        return False

    async def dispatch_bypass(self, text):
        return None

    async def dispatch_tool_call(self, tool_call):
        return None
