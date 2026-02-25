import asyncio, logging, os, sys
from dotenv import load_dotenv
from src.transport.telegram import TelegramTransport
from src.transport.cli import CliTransport
from src.skills.exec import ExecSkill
from src.skills.config import ConfigSkill
from src.skills.skill_writer import SkillWriterSkill
from src.skills.clawhub import ClawhubSkill
from agent import Agent

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

if os.environ.get("MEMORY_BACKEND", "simplemem") == "simplemem":
    from src.memory.simplemem import SimpleMemMemory
    memory = SimpleMemMemory(model_name=os.environ["GEMINI_MEMORY_MODEL"], api_key=os.environ["GEMINI_API_KEY"], consolidate_tokens=1000)
else:
    from src.memory.file import FileMemory
    memory = FileMemory(consolidation_model_name=os.environ["GEMINI_MEMORY_MODEL"], api_key=os.environ["GEMINI_API_KEY"])

agent = Agent(
    model_name=os.environ["GEMINI_MODEL"],
    api_key=os.environ["GEMINI_API_KEY"],
    memory=memory,
    include_thoughts=True,
    skills=[
        ConfigSkill(),
        ExecSkill(),
        # ClawhubSkill(),
        SkillWriterSkill(),
    ]
)
if "--cli" in sys.argv:
    transport = CliTransport(agent)
else:
    transport = TelegramTransport(
        bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
        allowed_user_ids={int(uid) for uid in os.environ["TELEGRAM_ALLOWED_USERS"].split(",")},
        agent=agent,
    )
asyncio.run(transport.start())
