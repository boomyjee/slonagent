import asyncio, logging, os
from dotenv import load_dotenv
from transport import TelegramTransport
from exec import ExecSkill
from config_skill import ConfigSkill
from skill_manager import SkillManager
from clawhub import Clawhub
from agent import Agent

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

_MEMORY_BACKEND = os.environ.get("MEMORY_BACKEND", "simplemem")
if _MEMORY_BACKEND == "simplemem":
    from simplemem_skill import SimplememSkill
    memory_skill = SimplememSkill()
else:
    from memory import MemorySkill
    memory_skill = MemorySkill(consolidation_model_name=os.environ["GEMINI_MEMORY_MODEL"], api_key=os.environ["GEMINI_API_KEY"])

agent = Agent(
    model_name=os.environ["GEMINI_MODEL"],
    include_thoughts=True,
    api_key=os.environ["GEMINI_API_KEY"],
    skills=[
        ConfigSkill(),
        memory_skill,
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
