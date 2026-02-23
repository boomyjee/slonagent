import asyncio, logging, os
from dotenv import load_dotenv
from transport import TelegramTransport
from memory import MemorySkill
from exec import ExecSkill
from config import ConfigSkill
from skill_manager import SkillManager
from clawhub import Clawhub
from agent import Agent

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

agent = Agent(
    model_name=os.environ["GEMINI_MODEL"],
    include_thoughts=True,
    api_key=os.environ["GEMINI_API_KEY"],
    skills=[
        ConfigSkill(),
        MemorySkill(consolidation_model_name=os.environ["GEMINI_MEMORY_MODEL"], api_key=os.environ["GEMINI_API_KEY"]),
        ExecSkill(),
        Clawhub(),
        SkillManager(),
    ]
)
transport = TelegramTransport(
    bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
    allowed_user_ids={int(uid) for uid in os.environ["TELEGRAM_ALLOWED_USERS"].split(",")},
    agent=agent,
)
asyncio.run(transport.start())
