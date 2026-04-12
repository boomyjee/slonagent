import { render, html, Component, css } from './lib.js';

const CELL = 50;
const COLORS = {
    light: '#f0d9b5', dark: '#b58863', selected: '#7fc97f', valid: '#7fc9c9',
    white: '#fff', black: '#333', whiteKing: '#ffe066', blackKing: '#666', lastMove: '#c9a97f',
};

const cl = {};

class App extends Component {
    constructor() {
        super();
        this.state = { board: [], validMoves: {}, selected: null, gameOver: false, lastMove: null, status: 'Подключение...', comment: '' };
        this._canvas = null;
    }

    componentDidMount() {
        const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
        this._ws = new WebSocket(proto + location.host + location.pathname + 'ws');
        this._ws.onmessage = e => {
            const msg = JSON.parse(e.data);
            if (msg.type === 'state') {
                this.setState({
                    board: msg.board, validMoves: msg.valid_moves || {}, gameOver: msg.game_over,
                    lastMove: msg.last_move || null, selected: null,
                    status: msg.game_over ? 'Игра окончена!' : Object.keys(msg.valid_moves || {}).length > 0 ? 'Ваш ход (белые)' : 'Ход соперника...',
                    comment: msg.comment || this.state.comment,
                }, () => this._draw());
            } else if (msg.type === 'comment') {
                this.setState({ comment: msg.text });
            }
        };
        this._ws.onclose = () => this.setState({ status: 'Отключено' });
    }

    _draw() {
        const { board, validMoves, selected, lastMove } = this.state;
        const ctx = this._canvas?.getContext('2d');
        if (!ctx || !board.length) return;

        const validDests = new Set();
        if (selected) {
            const chains = validMoves[selected[0] + ',' + selected[1]] || [];
            chains.forEach(ch => { if (ch.length) validDests.add(ch[0][0] + ',' + ch[0][1]); });
        }

        for (let r = 0; r < 8; r++) {
            for (let c = 0; c < 8; c++) {
                let bg = (r + c) % 2 === 1 ? COLORS.dark : COLORS.light;
                if (lastMove && ((lastMove.from[0] === r && lastMove.from[1] === c) || (lastMove.to[0] === r && lastMove.to[1] === c)))
                    bg = COLORS.lastMove;
                if (selected && selected[0] === r && selected[1] === c) bg = COLORS.selected;
                if (validDests.has(r + ',' + c)) bg = COLORS.valid;

                ctx.fillStyle = bg;
                ctx.fillRect(c * CELL, r * CELL, CELL, CELL);

                const p = board[r][c];
                if (p > 0) {
                    ctx.beginPath();
                    ctx.arc(c * CELL + CELL / 2, r * CELL + CELL / 2, CELL * 0.38, 0, Math.PI * 2);
                    ctx.fillStyle = (p === 1 || p === 3) ? COLORS.white : COLORS.black;
                    ctx.fill();
                    ctx.strokeStyle = '#888';
                    ctx.lineWidth = 1.5;
                    ctx.stroke();
                    if (p === 3 || p === 4) {
                        ctx.fillStyle = (p === 3) ? COLORS.whiteKing : COLORS.blackKing;
                        ctx.font = 'bold 18px sans-serif';
                        ctx.textAlign = 'center';
                        ctx.textBaseline = 'middle';
                        ctx.fillText('♛', c * CELL + CELL / 2, r * CELL + CELL / 2 + 1);
                    }
                }
            }
        }
    }

    _onClick = (e) => {
        const { validMoves, selected, gameOver } = this.state;
        if (gameOver || !Object.keys(validMoves).length) return;
        const rect = this._canvas.getBoundingClientRect();
        const c = Math.floor((e.clientX - rect.left) / CELL);
        const r = Math.floor((e.clientY - rect.top) / CELL);
        if (r < 0 || r > 7 || c < 0 || c > 7) return;
        const key = r + ',' + c;

        if (!selected) {
            if (validMoves[key]) this.setState({ selected: [r, c] }, () => this._draw());
        } else {
            const chains = validMoves[selected[0] + ',' + selected[1]] || [];
            const match = chains.find(ch => ch.length && ch[0][0] === r && ch[0][1] === c);
            if (match) {
                this._ws.send(JSON.stringify({ type: 'move', from: selected, chain: match }));
                this.setState({ selected: null, validMoves: {}, status: 'Ход соперника...' }, () => this._draw());
            } else if (validMoves[key]) {
                this.setState({ selected: [r, c] }, () => this._draw());
            } else {
                this.setState({ selected: null }, () => this._draw());
            }
        }
    };

    render() {
        const { status, comment } = this.state;
        return html`
            <div class=${cl.app}>
                <div class=${cl.status}>${status}</div>
                <canvas ref=${c => { this._canvas = c; this._draw(); }}
                    width=${CELL * 8} height=${CELL * 8}
                    class=${cl.board} onClick=${this._onClick} />
                <div class=${cl.comment}>${comment}</div>
            </div>
        `;
    }
}

render(html`<${App} />`, document.body);

cl.app = css`display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 100vh;`;
cl.status = css`margin: 12px 0; font-size: 16px; min-height: 24px;`;
cl.board = css`border: 2px solid #444; border-radius: 4px; cursor: pointer; touch-action: none;`;
cl.comment = css`margin: 12px 0; font-style: italic; max-width: 400px; text-align: center; min-height: 20px; color: var(--text-dim);`;
