import asyncio, logging, os, sys
from dotenv import load_dotenv
from transport import TelegramTransport, CliTransport
from exec import ExecSkill
from config_skill import ConfigSkill
from skill_manager import SkillManager
from clawhub import Clawhub
from agent import Agent

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

if os.environ.get("MEMORY_BACKEND", "simplemem") == "simplemem":
    from simplemem_skill import SimplememSkill
    memory = SimplememSkill()
else:
    from memory import MemorySkill
    memory = MemorySkill(consolidation_model_name=os.environ["GEMINI_MEMORY_MODEL"], api_key=os.environ["GEMINI_API_KEY"])

agent = Agent(
    model_name=os.environ["GEMINI_MODEL"],
    api_key=os.environ["GEMINI_API_KEY"],
    memory=memory,
    include_thoughts=True,
    skills=[
        ConfigSkill(),
        ExecSkill(),
        Clawhub(),
        SkillManager(),
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
