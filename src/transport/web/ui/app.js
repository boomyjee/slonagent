import { render, html, css } from './lib.js';

const cl = css`display: flex; align-items: center; justify-content: center; height: 100vh; color: var(--text-dim); font-size: 16px;`;

render(html`<div class=${cl}>SlonAgent</div>`, document.body);
