"""Russian checkers (шашки) game logic.

Board: 8x8, pieces on dark squares only.
Cells: 0=empty, 1=white, 2=black, 3=white king, 4=black king.
White moves up (row decreasing), black moves down (row increasing).
Player 1=white (user), 2=black (AI).
"""

EMPTY, WHITE, BLACK, WHITE_KING, BLACK_KING = 0, 1, 2, 3, 4


def owner(piece: int) -> int:
    if piece in (WHITE, WHITE_KING):
        return 1
    if piece in (BLACK, BLACK_KING):
        return 2
    return 0


def is_king(piece: int) -> bool:
    return piece in (WHITE_KING, BLACK_KING)


class Board:
    def __init__(self):
        self.cells = [[EMPTY] * 8 for _ in range(8)]
        self._setup()

    def _setup(self):
        for r in range(8):
            for c in range(8):
                if (r + c) % 2 == 1:
                    if r < 3:
                        self.cells[r][c] = BLACK
                    elif r > 4:
                        self.cells[r][c] = WHITE

    def get(self, r, c):
        return self.cells[r][c]

    def set(self, r, c, val):
        self.cells[r][c] = val

    def to_list(self) -> list[list[int]]:
        return [row[:] for row in self.cells]

    def count(self, player: int) -> int:
        pieces = (WHITE, WHITE_KING) if player == 1 else (BLACK, BLACK_KING)
        return sum(1 for r in range(8) for c in range(8) if self.cells[r][c] in pieces)

    def get_all_moves(self, player: int) -> dict[tuple, list[list[tuple]]]:
        """Returns {(r,c): [[(r1,c1), (r2,c2), ...], ...]} for all pieces of player.
        Each value is a list of move chains. A chain is a list of destination cells.
        If captures exist, only captures are returned (mandatory capture rule).
        """
        captures = {}
        simple = {}

        pieces = (WHITE, WHITE_KING) if player == 1 else (BLACK, BLACK_KING)
        for r in range(8):
            for c in range(8):
                if self.cells[r][c] not in pieces:
                    continue
                piece = self.cells[r][c]
                cap_chains = self._get_captures(r, c, piece, set())
                if cap_chains:
                    captures[(r, c)] = cap_chains
                else:
                    moves = self._get_simple_moves(r, c, piece)
                    if moves:
                        simple[(r, c)] = [[(mr, mc)] for mr, mc in moves]

        return captures if captures else simple

    def _get_simple_moves(self, r, c, piece) -> list[tuple]:
        moves = []
        if is_king(piece):
            dirs = [(-1, -1), (-1, 1), (1, -1), (1, 1)]
            for dr, dc in dirs:
                nr, nc = r + dr, c + dc
                while 0 <= nr < 8 and 0 <= nc < 8 and self.cells[nr][nc] == EMPTY:
                    moves.append((nr, nc))
                    nr += dr
                    nc += dc
        else:
            forward = -1 if piece == WHITE else 1
            for dc in (-1, 1):
                nr, nc = r + forward, c + dc
                if 0 <= nr < 8 and 0 <= nc < 8 and self.cells[nr][nc] == EMPTY:
                    moves.append((nr, nc))
        return moves

    def _get_captures(self, r, c, piece, removed: set) -> list[list[tuple]]:
        chains = []
        opponent = (BLACK, BLACK_KING) if owner(piece) == 1 else (WHITE, WHITE_KING)

        if is_king(piece):
            dirs = [(-1, -1), (-1, 1), (1, -1), (1, 1)]
            for dr, dc in dirs:
                nr, nc = r + dr, c + dc
                # Find opponent piece along diagonal
                while 0 <= nr < 8 and 0 <= nc < 8 and self.cells[nr][nc] == EMPTY:
                    nr += dr
                    nc += dc
                if not (0 <= nr < 8 and 0 <= nc < 8):
                    continue
                if self.cells[nr][nc] not in opponent or (nr, nc) in removed:
                    continue
                cap_r, cap_c = nr, nc
                # Land after captured piece
                nr, nc = cap_r + dr, cap_c + dc
                while 0 <= nr < 8 and 0 <= nc < 8 and self.cells[nr][nc] == EMPTY:
                    new_removed = removed | {(cap_r, cap_c)}
                    continuations = self._get_captures(nr, nc, piece, new_removed)
                    if continuations:
                        for chain in continuations:
                            chains.append([(nr, nc)] + chain)
                    else:
                        chains.append([(nr, nc)])
                    nr += dr
                    nc += dc
        else:
            dirs = [(-1, -1), (-1, 1), (1, -1), (1, 1)]
            for dr, dc in dirs:
                mr, mc = r + dr, c + dc
                if not (0 <= mr < 8 and 0 <= mc < 8):
                    continue
                if self.cells[mr][mc] not in opponent or (mr, mc) in removed:
                    continue
                lr, lc = mr + dr, mc + dc
                if not (0 <= lr < 8 and 0 <= lc < 8):
                    continue
                if self.cells[lr][lc] != EMPTY and (lr, lc) != (r, c):
                    continue
                new_removed = removed | {(mr, mc)}
                continuations = self._get_captures(lr, lc, piece, new_removed)
                if continuations:
                    for chain in continuations:
                        chains.append([(lr, lc)] + chain)
                else:
                    chains.append([(lr, lc)])
        return chains

    def make_move(self, player: int, fr: int, fc: int, chain: list[tuple]) -> list[tuple]:
        """Execute a move. Returns list of captured positions."""
        piece = self.cells[fr][fc]
        if owner(piece) != player:
            raise ValueError(f"Not your piece at ({fr},{fc})")

        all_moves = self.get_all_moves(player)
        if (fr, fc) not in all_moves:
            raise ValueError(f"No valid moves from ({fr},{fc})")
        if chain not in all_moves[(fr, fc)]:
            raise ValueError(f"Invalid move chain {chain} from ({fr},{fc})")

        captured = []
        cr, cc = fr, fc
        for tr, tc in chain:
            # Find captured piece between cr,cc and tr,tc
            dr = 1 if tr > cr else -1
            dc = 1 if tc > cc else -1
            nr, nc = cr + dr, cc + dc
            while (nr, nc) != (tr, tc):
                if self.cells[nr][nc] != EMPTY:
                    captured.append((nr, nc))
                    self.cells[nr][nc] = EMPTY
                nr += dr
                nc += dc
            cr, cc = tr, tc

        self.cells[fr][fc] = EMPTY
        # Promotion
        dest_r, dest_c = chain[-1]
        if piece == WHITE and dest_r == 0:
            piece = WHITE_KING
        elif piece == BLACK and dest_r == 7:
            piece = BLACK_KING
        self.cells[dest_r][dest_c] = piece

        return captured

    def describe_move(self, fr, fc, chain, captured) -> str:
        """Human-readable move description."""
        start = f"{chr(ord('a') + fc)}{8 - fr}"
        end_r, end_c = chain[-1]
        end = f"{chr(ord('a') + end_c)}{8 - end_r}"
        if captured:
            return f"{start}x{end} (взято {len(captured)})"
        return f"{start}-{end}"


def ai_move(board: Board) -> tuple[tuple, list[tuple], list[tuple]] | None:
    """Simple AI: pick best capture, or random move. Returns (from, chain, captured) or None."""
    import random
    all_moves = board.get_all_moves(2)
    if not all_moves:
        return None

    # Prefer longest capture chain
    best = []
    best_len = 0
    for (fr, fc), chains in all_moves.items():
        for chain in chains:
            # Count captures in this chain
            cr, cc = fr, fc
            caps = 0
            for tr, tc in chain:
                dr = 1 if tr > cr else -1
                dc = 1 if tc > cc else -1
                nr, nc = cr + dr, cc + dc
                while (nr, nc) != (tr, tc):
                    if board.cells[nr][nc] != EMPTY:
                        caps += 1
                    nr += dr
                    nc += dc
                cr, cc = tr, tc
            if caps > best_len:
                best = [((fr, fc), chain)]
                best_len = caps
            elif caps == best_len:
                best.append(((fr, fc), chain))

    (fr, fc), chain = random.choice(best)
    captured = board.make_move(2, fr, fc, chain)
    return (fr, fc), chain, captured
