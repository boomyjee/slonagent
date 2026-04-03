import asyncio
import logging
from typing import Annotated

from agent import Skill, tool
from src.modes.checkers.game import Board, ai_move
from src.modes.checkers.server import CheckersServer

log = logging.getLogger(__name__)


async def start_tunnel(port: int, subdomain: str, sish_domain: str, sish_port: int, sish_key: str):
    """Start SSH tunnel via sish using asyncssh. Returns (public_url, connection)."""
    import asyncssh
    key = asyncssh.import_private_key(sish_key)
    conn = await asyncssh.connect(
        sish_domain, sish_port, known_hosts=None, client_keys=[key], username="tunnel",
    )
    await conn.forward_remote_port(subdomain, 80, "localhost", port)
    url = f"https://{subdomain}.{sish_domain}:8443"
    log.info("[checkers] tunnel URL: %s", url)
    return url, conn


class CommentatorSkill(Skill):
    def __init__(self, transport, server):
        super().__init__()
        self._transport = transport
        self._server = server

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
        await self._server.send_comment(f"⚪ {white_comment}\n⚫ {black_comment}")
        return {"status": "ok"}


class CheckersSkill(Skill):
    def __init__(self, port: int = 3100, sish_port: int = 2222, sish_domain: str = "", sish_key: str = ""):
        super().__init__()
        self._port = port
        self._sish_port = sish_port
        self._sish_domain = sish_domain
        self._sish_key = sish_key

    @tool("Запустить игру в шашки через веб-интерфейс в Telegram")
    async def start_checkers(self) -> dict:
        transport = self.agent.transport
        import random, string
        session_id = ''.join(random.choices(string.ascii_lowercase, k=6))
        server = CheckersServer(self._port)
        await server.start()

        # Start tunnel
        try:
            url, tunnel_conn = await asyncio.wait_for(
                start_tunnel(self._port, f"checkers-{session_id}", self._sish_domain, self._sish_port, self._sish_key),
                timeout=10,
            )
        except Exception as e:
            await transport.send_message(f"Не удалось запустить туннель: {e}")
            return {"error": str(e)}

        # Send Web App button
        from src.transport.telegram import TelegramTransport
        if isinstance(transport, TelegramTransport):
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🎮 Играть в шашки", web_app=WebAppInfo(url=url))
            ]])
            await transport.bot.send_message(
                transport.chat_id, "Шашки готовы!",
                reply_markup=kb, message_thread_id=transport.thread_id,
            )
        else:
            await transport.send_message(f"🎮 Шашки готовы: {url}")

        # Commentator subagent
        commentator = await self.agent.spawn_subagent(
            "checkers_commentator", memory_providers=[],
            skills=[CommentatorSkill(transport, server)],
        )
        commentator.memory.clear()

        # Game loop
        moves_played = 0
        try:
            while not server.game_over:
                # Wait for user move
                move_data = await server.wait_for_user_move()
                fr, fc = move_data["from"]
                chain = [tuple(c) for c in move_data["chain"]]

                try:
                    captured = server.board.make_move(1, fr, fc, chain)
                except ValueError as e:
                    await server.send_comment(f"Недопустимый ход: {e}")
                    await server._send_state()
                    continue

                moves_played += 1
                move_desc = server.board.describe_move(fr, fc, chain, captured)

                # Show user's move immediately, opponent thinking
                await server._send_state(last_move={"from": [fr, fc], "to": list(chain[-1])}, your_turn=False)

                # Check if black lost
                if server.board.count(2) == 0 or not server.board.get_all_moves(2):
                    server.game_over = True
                    await server._send_state(comment="Вы победили!")
                    await transport.send_message("🏆 Вы победили в шашки!")
                    break

                # AI move (instant)
                result = ai_move(server.board)
                if result is None:
                    server.game_over = True
                    await server._send_state(comment="Вы победили — у соперника нет ходов!")
                    await transport.send_message("🏆 Вы победили — у соперника нет ходов!")
                    break

                ai_from, ai_chain, _ = result
                ai_desc = server.board.describe_move(ai_from[0], ai_from[1], ai_chain, [])

                # Check if white lost
                if server.board.count(1) == 0 or not server.board.get_all_moves(1):
                    server.game_over = True
                    await server._send_state(last_move={"from": list(ai_from), "to": list(ai_chain[-1])}, comment="Вы проиграли!")
                    await transport.send_message("Вы проиграли в шашки. Реванш?")
                    break

                # LLM comments on both moves, then show AI move + your turn
                await self._comment_moves(server, commentator, move_desc, ai_desc, moves_played)
                await server._send_state(last_move={"from": list(ai_from), "to": list(ai_chain[-1])})

        finally:
            if tunnel_conn:
                tunnel_conn.close()

        return {"status": "game_over", "moves": moves_played}

    async def _comment_moves(self, server, commentator, white_desc: str, black_desc: str, move_num: int):
        """Ask LLM to comment on both moves via tool call."""
        try:
            board_str = self._board_to_text(server.board)
            prompt = (
                f"Ход #{move_num}.\n"
                f"Белые: {white_desc}\n"
                f"Чёрные: {black_desc}\n"
                f"Позиция после:\n{board_str}\n"
                f"Вызови comment чтобы прокомментировать оба хода."
            )
            await commentator.memory.add_turn({"role": "user", "content": prompt})
            tool_calls, text = await commentator.llm(tool_choice="commentator_comment")
            if tool_calls:
                await commentator.dispatch_tool_calls(tool_calls)
            elif text:
                await commentator.memory.add_turn({"role": "assistant", "content": text})
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
