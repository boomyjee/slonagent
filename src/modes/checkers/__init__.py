import asyncio
import json
import logging
from typing import Annotated

from agent import Skill, tool
from src.modes.checkers.game import Board, ai_move
from src.transport.web import WebTransport, WebFork

log = logging.getLogger(__name__)


class CheckersFork(WebFork):
    """Game board served at /{agent_id}/checkers/, WS for moves."""
    prefix = "/checkers"

    def __init__(self, ref_agent):
        self.board = Board()
        self.move_event = asyncio.Event()
        self.last_user_move: dict | None = None
        self.game_over = False
        super().__init__(ref_agent)

    async def ws_connect(self, ws):
        await ws.send_text(json.dumps(self._state_msg()))
        await super().ws_connect(ws)

    async def ws_handle_message(self, msg, ws=None):
        if msg.get("type") == "move":
            self.last_user_move = msg
            self.move_event.set()

    def _state_msg(self, last_move=None, comment=None, your_turn=True):
        valid_moves = {}
        if not self.game_over and your_turn:
            raw = self.board.get_all_moves(1)
            valid_moves = {f"{r},{c}": chains for (r, c), chains in raw.items()}
        msg = {"type": "state", "board": self.board.to_list(), "valid_moves": valid_moves, "game_over": self.game_over}
        if last_move:
            msg["last_move"] = last_move
        if comment:
            msg["comment"] = comment
        return msg

    async def _send_state(self, last_move=None, comment=None, your_turn=True):
        await self.send(self._state_msg(last_move, comment, your_turn))

    async def send_comment(self, text: str):
        await self.send({"type": "comment", "text": text})

    async def wait_for_user_move(self) -> dict:
        self.move_event.clear()
        self.last_user_move = None
        await self.move_event.wait()
        return self.last_user_move


class CheckersTransport(WebTransport):
    def create_fork(self, agent):
        return CheckersFork(ref_agent=agent)


class CommentatorSkill(Skill):
    def __init__(self, transport, checkers: CheckersFork):
        super().__init__()
        self._transport = transport
        self._checkers = checkers

    async def get_context_prompt(self, user_text: str = "") -> str:
        return "Ты комментатор партии в русские шашки. Комментируй ходы коротко (1-2 предложения), с юмором или драматизмом."

    @tool("Прокомментировать ходы белых и чёрных")
    async def comment(
        self,
        white_comment: Annotated[str, "Комментарий к ходу белых"],
        black_comment: Annotated[str, "Комментарий к ходу чёрных"],
    ) -> dict:
        await self._transport.send_message(f"⚪ {white_comment}")
        await self._transport.send_message(f"⚫ {black_comment}")
        await self._checkers.send_comment(f"⚪ {white_comment}\n⚫ {black_comment}")
        return {"status": "ok"}


class CheckersSkill(Skill):

    @tool("Запустить игру в шашки через веб-интерфейс")
    async def launch(self) -> dict:
        transport = self.agent.transport
        checkers_t = CheckersTransport()
        checkers_t.set_agent(self.agent)
        checkers = checkers_t.fork

        url = await checkers.get_auth_url("/")
        await transport.send_app_url(url, "Шашки готовы!", "🎮 Играть")

        commentator = await self.agent.spawn_subagent(
            "checkers_commentator", memory_providers=[],
            skills=[CommentatorSkill(transport, checkers)],
        )
        commentator.memory.clear()

        moves_played = 0
        try:
            while not checkers.game_over:
                move_data = await checkers.wait_for_user_move()
                fr, fc = move_data["from"]
                chain = [tuple(c) for c in move_data["chain"]]

                try:
                    captured = checkers.board.make_move(1, fr, fc, chain)
                except ValueError as e:
                    await checkers.send_comment(f"Недопустимый ход: {e}")
                    await checkers._send_state()
                    continue

                moves_played += 1
                move_desc = checkers.board.describe_move(fr, fc, chain, captured)
                await checkers._send_state(last_move={"from": [fr, fc], "to": list(chain[-1])}, your_turn=False)

                if checkers.board.count(2) == 0 or not checkers.board.get_all_moves(2):
                    checkers.game_over = True
                    await checkers._send_state(comment="Вы победили!")
                    await transport.send_message("🏆 Вы победили в шашки!")
                    break

                result = ai_move(checkers.board)
                if result is None:
                    checkers.game_over = True
                    await checkers._send_state(comment="Вы победили — у соперника нет ходов!")
                    await transport.send_message("🏆 Вы победили — у соперника нет ходов!")
                    break

                ai_from, ai_chain, _ = result
                ai_desc = checkers.board.describe_move(ai_from[0], ai_from[1], ai_chain, [])

                if checkers.board.count(1) == 0 or not checkers.board.get_all_moves(1):
                    checkers.game_over = True
                    await checkers._send_state(last_move={"from": list(ai_from), "to": list(ai_chain[-1])}, comment="Вы проиграли!")
                    await transport.send_message("Вы проиграли в шашки. Реванш?")
                    break

                await self._comment_moves(checkers, commentator, move_desc, ai_desc, moves_played)
                await checkers._send_state(last_move={"from": list(ai_from), "to": list(ai_chain[-1])})

        finally:
            checkers_t.cleanup()

        return {"status": "game_over", "moves": moves_played}

    async def _comment_moves(self, checkers, commentator, white_desc: str, black_desc: str, move_num: int):
        try:
            board_str = self._board_to_text(checkers.board)
            prompt = (
                f"Ход #{move_num}.\n"
                f"Белые: {white_desc}\n"
                f"Чёрные: {black_desc}\n"
                f"Позиция после:\n{board_str}\n"
                f"Вызови comment чтобы прокомментировать оба хода."
            )
            await commentator.memory.add_turn({"role": "user", "content": prompt})
            turn = await commentator.llm(tool_choice="commentator_comment")
            if turn.get("tool_calls"):
                result_turns = await commentator.dispatch_tool_calls(turn)
                await commentator.memory.add_turn(turn, *result_turns)
            else:
                await commentator.memory.add_turn(turn)
        except Exception as e:
            log.warning("[checkers] LLM comment failed: %s", e)
            await self.agent.transport.send_message(f"Не удалось сгенерировать комментарий: {e}")

    def _board_to_text(self, board: Board) -> str:
        symbols = {0: ".", 1: "w", 2: "b", 3: "W", 4: "B"}
        lines = ["  a b c d e f g h"]
        for r in range(8):
            row = " ".join(symbols[board.get(r, c)] for c in range(8))
            lines.append(f"{8-r} {row}")
        return "\n".join(lines)
