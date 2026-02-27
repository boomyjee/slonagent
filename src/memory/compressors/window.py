from memory import Memory
from src.memory.compressors.base import BaseCompressor


class WindowCompressor(BaseCompressor):
    """Стратегия по умолчанию: скользящее окно по токен-бюджету.

    Старые сообщения молча удаляются при переполнении.
    """

    def __init__(
        self,
        hard_limit_tokens: int = 500_000,
        soft_limit_tokens: int = 50_000,
        min_user_turns: int = 10,
    ):
        self.hard_limit_tokens = hard_limit_tokens
        self.soft_limit_tokens = soft_limit_tokens
        self.min_user_turns = min_user_turns

    async def compress(self, turns: list) -> list:
        result, tokens, user_ids = [], 0, set()
        for turn in reversed(turns):
            tokens += Memory.count_tokens([turn])
            if isinstance(turn, dict) and (uid := turn.get("_user_message_id")) is not None:
                user_ids.add(uid)
            if tokens > self.hard_limit_tokens:
                break
            if tokens > self.soft_limit_tokens and len(user_ids) >= self.min_user_turns:
                break
            result.append(turn)
        result.reverse()
        return result
