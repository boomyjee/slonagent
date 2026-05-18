import { html, Component, Fragment, css, persist, useState, useEffect } from '../lib.js';
import { api } from './api.js';
import { loadMonaco, fileLang } from './Editor.js';

const cl = {};
const post = (path, body) => api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
});

const HISTORY_PAGE = 15;

// ─── component ────────────────────────────────────────────────────────────

export class Git extends Component {
    constructor(props) {
        super(props);
        const saved = persist.get(this._persistKey(props.path), {});
        this.state = {
            view: saved.view || 'working_tree',
            selectedCommit: saved.selectedCommit || '',
            historyDepth: saved.historyDepth || 1,
            message: saved.message || '',
            branch: '', branches: [],
            files: [], statusHash: '',
            commits: [], afterCommits: [],
            historyMore: false,
            expanded: {},
            error: '',
            menu: null,    // { name, x, y, items }
        };
        // Re-expanded after the file list arrives (set in _restoreExpanded).
        this._toRestore = saved.expandedFiles || [];
    }

    _persistKey(path) { return `git:${this.props.rootKey || ''}:${path || this.props.path}`; }
    _PERSISTED = ['view', 'selectedCommit', 'historyDepth', 'message'];

    componentDidMount() {
        document.addEventListener('click', this._closeMenu);
        this._refresh();
    }

    componentDidUpdate(prevProps, prevState) {
        if (prevProps.refreshKey !== this.props.refreshKey) this._refresh();
        const fieldChanged = this._PERSISTED.some(k => prevState[k] !== this.state[k]);
        const expandedKeys = Object.keys(this.state.expanded);
        const prevKeys = Object.keys(prevState.expanded);
        const expandedChanged =
            expandedKeys.length !== prevKeys.length ||
            expandedKeys.some(k => !prevKeys.includes(k));
        if (fieldChanged || expandedChanged) {
            const data = { expandedFiles: expandedKeys };
            for (const k of this._PERSISTED) data[k] = this.state[k];
            persist.set(this._persistKey(), data);
        }
    }

    componentWillUnmount() {
        document.removeEventListener('click', this._closeMenu);
    }

    // ─── data loading ──────────────────────────────────────────────

    _q(extra) {
        const base = `path=${encodeURIComponent(this.props.path)}`;
        return extra ? `${base}&${extra}` : base;
    }

    async _refresh() {
        const br = await api(`api/git/branches?${this._q()}`);
        if (br.error) { this.setState({ error: br.error }); return; }
        this.setState({ branch: br.current, branches: br.all || [], error: '' });
        if (this.state.view === 'working_tree') {
            await this._loadStatus();
        } else {
            await this._loadHistory(br.current, this.state.selectedCommit);
        }
        await this._restoreExpanded();
    }

    async _restoreExpanded() {
        const toOpen = this._toRestore || [];
        this._toRestore = null;
        if (!toOpen.length) return;
        const fileSet = new Set(this.state.files.map(f => f.file));
        for (const path of toOpen) {
            const f = this.state.files.find(x => x.file === path);
            if (f && fileSet.has(path)) await this._toggleDiff(f);
        }
    }

    async _loadStatus() {
        const s = await api(`api/git/status?${this._q()}`);
        if (s.error) { this.setState({ error: s.error, loading: false }); return; }
        this.setState(({ expanded }) => {
            const stillThere = new Set(s.files.map(f => f.file));
            const next = {};
            for (const k of Object.keys(expanded)) if (stillThere.has(k)) next[k] = expanded[k];
            return {
                files: s.files, statusHash: s.status_hash,
                expanded: next, error: '', loading: false,
            };
        });
    }

    async _loadHistory(branch, selectedSha) {
        const data = await api(`api/git/log?${this._q(`branch=${encodeURIComponent(branch)}&limit=${HISTORY_PAGE}`)}`);
        if (data.error) { this.setState({ error: data.error }); return; }
        const commits = data.commits || [];
        const selected = selectedSha && commits.find(c => c.sha_full === selectedSha)
            ? selectedSha
            : (commits[0]?.sha_full || '');
        const skip = commits.findIndex(c => c.sha_full === selected) + 1;
        const after = await api(`api/git/log?${this._q(`branch=${encodeURIComponent(branch)}&limit=${HISTORY_PAGE}&skip=${skip}`)}`);
        const more = await api(`api/git/log?${this._q(`branch=${encodeURIComponent(branch)}&limit=1&skip=${commits.length}`)}`);
        const files = selected
            ? (await api(`api/git/history?${this._q(`commit=${encodeURIComponent(selected)}&depth=${this.state.historyDepth}`)}`)).files || []
            : [];
        this.setState(({ expanded }) => {
            const stillThere = new Set(files.map(f => f.file));
            const next = {};
            for (const k of Object.keys(expanded)) if (stillThere.has(k)) next[k] = expanded[k];
            return {
                commits, afterCommits: after.commits || [],
                historyMore: !!(more.commits || []).length,
                selectedCommit: selected,
                files, expanded: next, error: '', loading: false,
            };
        });
    }

    // ─── menu helpers ──────────────────────────────────────────────

    _openMenu(name, e) {
        e.preventDefault(); e.stopPropagation();
        if (this.state.menu?.name === name) {
            this.setState({ menu: null });
            return;
        }
        const r = e.currentTarget.getBoundingClientRect();
        this.setState({ menu: { name, x: r.left, y: r.bottom + 2, right: r.right } });
    }
    _closeMenu = () => { if (this.state.menu) this.setState({ menu: null }); };

    // ─── actions ──────────────────────────────────────────────────

    async _toggleStaged(f) {
        await post('api/git/stage', { path: this.props.path, file: f.file, staged: !f.staged });
        this._loadStatus();
    }
    async _stageAll() {
        await Promise.all(this.state.files.filter(f => !f.staged || f.partial).map(f =>
            post('api/git/stage', { path: this.props.path, file: f.file, staged: true })
        ));
        this._loadStatus();
    }
    async _checkout(f) {
        if (!confirm(`Discard changes to ${f.file}?`)) return;
        await post('api/git/checkout', { path: this.props.path, file: f.file, old_file: f.old_file });
        this._loadStatus();
    }
    async _commit() {
        const message = this.state.message.trim();
        if (!message) return;
        const data = await post('api/git/commit', {
            path: this.props.path, message, status_hash: this.state.statusHash,
        });
        if (data.status_hash) this.setState({ statusHash: data.status_hash });
        if (data.error || data.status !== 'ok') {
            this.setState({ error: data.error || data.output || 'commit failed' });
            this._loadStatus();
            return;
        }
        this.setState({ message: '', error: '' });
        this._loadStatus();
    }
    async _amend() {
        const message = this.state.message.trim();
        if (!confirm('Amend last commit?')) return;
        const data = await post('api/git/amend', {
            path: this.props.path, message, status_hash: this.state.statusHash,
        });
        if (data.status_hash) this.setState({ statusHash: data.status_hash });
        if (data.error || data.status !== 'ok') {
            this.setState({ error: data.error || data.output || 'amend failed' });
            this._loadStatus();
            return;
        }
        this.setState({ message: '', error: '' });
        this._loadStatus();
    }
    async _switchBranch(name) {
        const data = await post('api/git/switch_branch', { path: this.props.path, branch: name });
        if (data.error || data.status !== 'ok') {
            this.setState({ error: data.error || data.output || 'switch failed' });
            return;
        }
        this._refresh();
    }
    async _selectCommit(sha) {
        this.setState({ selectedCommit: sha });
        await this._loadHistory(this.state.branch, sha);
    }
    async _selectDepth(n) {
        this.setState({ historyDepth: n }, () => this._loadHistory(this.state.branch, this.state.selectedCommit));
    }
    async _switchView(v) {
        // Wipe data instantly so the panel doesn't flash old content from
        // the previous view while async fetches are in flight.
        this.setState({ view: v, files: [], commits: [], afterCommits: [], expanded: {}, loading: true },
            () => this._refresh());
    }

    // ─── diff ─────────────────────────────────────────────────────

    async _toggleDiff(f) {
        const { expanded, view, selectedCommit, historyDepth } = this.state;
        if (f.file in expanded) {
            const next = { ...expanded };
            delete next[f.file];
            this.setState({ expanded: next });
            return;
        }
        this.setState({ expanded: { ...expanded, [f.file]: { loading: true } } });
        let data;
        if (view === 'working_tree') {
            const q = this._q(`file=${encodeURIComponent(f.file)}&staged=${f.staged && !f.partial ? 1 : 0}`);
            data = await api(`api/git/diff?${q}`);
        } else {
            const sha1 = `${selectedCommit}~${historyDepth}`;
            const sha2 = selectedCommit;
            const q = this._q(`file=${encodeURIComponent(f.file)}&sha1=${encodeURIComponent(sha1)}&sha2=${encodeURIComponent(sha2)}`);
            data = await api(`api/git/diff_between?${q}`);
        }
        const parts = view === 'working_tree'
            ? ['HEAD', f.staged && !f.partial ? 'index' : 'WT']
            : [`${selectedCommit}~${historyDepth}`, selectedCommit];
        this.setState(({ expanded: e }) => ({
            expanded: { ...e, [f.file]: { text: data.diff || '(no diff)', parts } },
        }));
    }

    _onLineClick = (file, line, side) => {
        const repo = this.props.path;
        if (side === 'WT') {
            const path = repo.replace(/\/$/, '') + '/' + file;
            this.props.onOpenFile?.(path, file.split('/').pop(), line);
        } else {
            // 'index' is the staged side; reading staged content via show requires
            // ':<file>' (empty ref). For viewing, fall back to HEAD — index isn't a
            // first-class ref the show endpoint accepts cleanly.
            const ref = side === 'index' ? 'HEAD' : side;
            this.props.onOpenGitShow?.(repo, ref, file, line);
        }
    };

    // ─── render ───────────────────────────────────────────────────

    render() {
        const { view, branch, branches, files, commits, afterCommits, historyMore,
                selectedCommit, historyDepth, message, expanded, error, menu, loading } = this.state;
        const stagedCount = files.filter(f => f.staged).length;
        const isHistory = view === 'history';

        const menuItems = () => {
            const m = this.state.menu;
            if (!m) return [];
            if (m.name === 'branch') {
                return branches.map(b => ({
                    label: b, selected: b === branch, action: () => this._switchBranch(b),
                }));
            }
            if (m.name === 'commit') {
                return [
                    ...commits.map(c => ({
                        label: html`<b>${c.sha_short}</b> ${c.date.slice(0, 10)} — ${truncate(c.message, 80)}`,
                        tooltip: `${c.sha_short} ${c.date}\n${c.message}`,
                        selected: c.sha_full === selectedCommit,
                        action: () => this._selectCommit(c.sha_full),
                    })),
                    ...(historyMore ? [{ label: html`<b>Show more</b>`, keepOpen: true, action: () => this._showMoreCommits() }] : []),
                ];
            }
            if (m.name === 'depth') {
                return afterCommits.map((c, i) => ({
                    label: html`<b>~${i + 1}</b> ${c.sha_short} — ${truncate(c.message, 80)}`,
                    tooltip: `~${i + 1}  ${c.sha_short} ${c.date}\n${c.message}`,
                    selected: (i + 1) === historyDepth,
                    action: () => this._selectDepth(i + 1),
                }));
            }
            if (m.name === 'commitMore') {
                return [{ label: 'Amend', action: () => this._amend() }];
            }
            return [];
        };

        return html`<div class=${cl.wrap}>
            <div class=${cl.toolbar}>
                <button class=${cl.btn} onClick=${() => this._refresh()} title="Refresh">↻</button>
                <${ComboButton} label=${isHistory ? 'history' : 'tree'}
                                onClick=${() => this._switchView(isHistory ? 'working_tree' : 'history')} />
                <${ComboButton} label=${branch || '(detached)'} onClick=${e => this._openMenu('branch', e)} />
                ${isHistory ? html`
                    <${ComboButton} label=${selectedCommit ? selectedCommit.slice(0,7) : '(empty)'}
                                    onClick=${e => this._openMenu('commit', e)} />
                    <${ComboButton} label=${'~' + historyDepth} onClick=${e => this._openMenu('depth', e)} />
                ` : html`
                    <button class=${cl.btn} onClick=${() => this._stageAll()}
                            disabled=${!files.some(f => !f.staged || f.partial)} title="Stage All">✓</button>
                    <input class=${cl.msg} type="text" placeholder="commit message"
                           value=${message}
                           onInput=${e => this.setState({ message: e.target.value })}
                           onKeyDown=${e => { if (e.key === 'Enter') this._commit(); }} />
                    <div class=${cl.commitGroup}>
                        <button class=${cl.btn + ' ' + cl.commitMain} onClick=${() => this._commit()}
                                disabled=${stagedCount === 0 || !message.trim()}>Commit</button>
                        <button class=${cl.btn + ' ' + cl.commitArrow}
                                onClick=${e => this._openMenu('commitMore', e)}>▾</button>
                    </div>
                `}
            </div>

            ${error && html`<div class=${cl.error}>${error}</div>`}

            <div class=${cl.scroll}>
                <table class=${cl.table}>
                    <thead><tr>
                        ${!isHistory && html`<th class="staged">Staged</th>`}
                        <th class="state">State</th>
                        <th class="filename">Filename</th>
                        ${!isHistory && html`<th class="checkout">Checkout</th>`}
                        <th class="empty"></th>
                    </tr></thead>
                    <tbody>
                        ${files.length === 0 && html`<tr><td colspan="5" class=${cl.emptyRow}>
                            ${loading ? 'loading…' :
                                (isHistory ? 'No changes in this commit' : 'Nothing to commit, working tree clean')}
                        </td></tr>`}
                        ${files.map(f => html`
                            <${FileRow} key=${f.file} f=${f} isHistory=${isHistory}
                                onToggleStaged=${() => this._toggleStaged(f)}
                                onCheckout=${() => this._checkout(f)}
                                onClickName=${() => this._toggleDiff(f)}
                                expanded=${expanded[f.file]}
                                onLineClick=${this._onLineClick} />
                        `)}
                    </tbody>
                </table>
            </div>

            ${menu && html`<${Menu} menu=${menu} items=${menuItems()} onClose=${this._closeMenu}
                                alignRight=${menu.name === 'commitMore'} />`}
        </div>`;
    }

    async _showMoreCommits() {
        const { branch, commits } = this.state;
        const more = await api(`api/git/log?${this._q(`branch=${encodeURIComponent(branch)}&limit=${HISTORY_PAGE}&skip=${commits.length}`)}`);
        const next = [...commits, ...(more.commits || [])];
        const probe = await api(`api/git/log?${this._q(`branch=${encodeURIComponent(branch)}&limit=1&skip=${next.length}`)}`);
        this.setState({ commits: next, historyMore: !!(probe.commits || []).length });
    }
}

// ─── small helpers ────────────────────────────────────────────────────────

function truncate(s, n) { return s.length > n ? s.slice(0, n) + '…' : s; }

function ComboButton({ label, onClick }) {
    return html`<button class=${cl.combo} onClick=${onClick}>${label} <span class="arr">▾</span></button>`;
}

function Menu({ menu, items, onClose, alignRight }) {
    const pos = alignRight
        ? { right: (window.innerWidth - menu.right) + 'px', top: menu.y + 'px' }
        : { left: menu.x + 'px', top: menu.y + 'px' };
    return html`<div class=${cl.menu} style=${pos}
                     onClick=${e => e.stopPropagation()}>
        ${items.map((it, i) => html`
            <div key=${i} class="${cl.menuItem}${it.selected ? ' selected' : ''}"
                 title=${it.tooltip || ''}
                 onClick=${e => { e.stopPropagation(); it.action(); if (!it.keepOpen) onClose(); }}>
                ${it.label}
            </div>`)}
    </div>`;
}

// ─── file row + diff ──────────────────────────────────────────────────────

function FileRow({ f, isHistory, onToggleStaged, onCheckout, onClickName, expanded, onLineClick }) {
    const name = f.old_file ? html`<span class=${cl.rename}>${f.old_file}</span> → ${f.file}` : f.file;
    return html`<${Fragment}>
        <tr class=${cl.fileRow}>
            ${!isHistory && html`<td class="staged">
                <input type="checkbox" checked=${f.staged}
                       class=${f.partial ? cl.partial : ''}
                       onChange=${onToggleStaged} />
            </td>`}
            <td class="state">${f.state}</td>
            <td class="filename" onClick=${onClickName}>${name}</td>
            ${!isHistory && html`<td class="checkout">
                <button class=${cl.xBtn} title="Discard changes" onClick=${onCheckout}>×</button>
            </td>`}
            <td class="empty"></td>
        </tr>
        ${expanded && html`<tr><td colspan="5" class=${cl.diffCell}>
            ${expanded.loading
                ? html`<div class=${cl.loading}>loading…</div>`
                : html`<${Diff} text=${expanded.text} parts=${expanded.parts} file=${f.file} onLineClick=${onLineClick} />`}
        </td></tr>`}
    </${Fragment}>`;
}

// ─── unified-diff renderer with clickable lines ───────────────────────────

function Diff({ text, parts, file, onLineClick }) {
    if (!text || text === '(no diff)') return html`<div class=${cl.diffEmpty}>(no diff)</div>`;
    const lines = text.split('\n');
    const hunks = [];
    let current = null;
    let oldNum = 0, newNum = 0;

    for (const line of lines) {
        if (line.startsWith('diff ') || line.startsWith('index ') ||
            line.startsWith('--- ') || line.startsWith('+++ ') || line === '') continue;
        if (line.startsWith('@@')) {
            const m = line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
            if (!m) continue;
            oldNum = parseInt(m[1]);
            newNum = parseInt(m[2]);
            current = { header: line, rows: [] };
            hunks.push(current);
            continue;
        }
        if (!current) continue;
        if (line.startsWith('\\')) continue;
        if (line.startsWith('+')) {
            current.rows.push({ kind: 'add', oldN: '', newN: newNum, text: line.slice(1) });
            newNum++;
        } else if (line.startsWith('-')) {
            current.rows.push({ kind: 'del', oldN: oldNum, newN: '', text: line.slice(1) });
            oldNum++;
        } else {
            current.rows.push({ kind: 'ctx', oldN: oldNum, newN: newNum, text: line.slice(1) });
            oldNum++; newNum++;
        }
    }

    // No hunks usually means a binary diff ("Binary files a/x and b/x differ").
    // Show whatever git did emit so the user isn't staring at an empty cell.
    if (!hunks.length) {
        const summary = lines.find(l => l.startsWith('Binary files')) || lines.find(l => l.trim()) || '(no textual diff)';
        return html`<div class=${cl.diffEmpty}>${summary}</div>`;
    }

    return html`<${HighlightedDiff} hunks=${hunks} file=${file} parts=${parts} onLineClick=${onLineClick} />`;
}

// Каждая строка прогоняется через monaco.editor.colorize, который читает
// активную глобальную тему (vs-slon, ставится в loadMonaco). Подсветка делается
// построчно — без многострочного контекста; для diff это нормальный компромисс,
// мульти-строчные конструкции (heredoc'и, тройные кавычки) могут локально путаться.
function HighlightedDiff({ hunks, file, parts, onLineClick }) {
    const [colored, setColored] = useState({});

    useEffect(() => {
        let alive = true;
        setColored({});
        (async () => {
            const monaco = await loadMonaco();
            const lang = fileLang(file);
            const tasks = [];
            hunks.forEach((h, i) => h.rows.forEach((r, j) => {
                tasks.push(monaco.editor.colorize(r.text || ' ', lang, { tabSize: 4 })
                    .then(out => [`${i}_${j}`, out]));
            }));
            const results = await Promise.all(tasks);
            if (!alive) return;
            const map = {};
            for (const [k, v] of results) map[k] = v;
            setColored(map);
        })();
        return () => { alive = false; };
    }, [hunks, file]);

    return html`<div class=${cl.diff}>
        ${hunks.map((h, i) => html`<div key=${i} class=${cl.hunk}>
            <div class="hdr">${h.header}</div>
            ${h.rows.map((r, j) => {
                const htmlOut = colored[`${i}_${j}`];
                const content = htmlOut !== undefined
                    ? html`<span class="content" dangerouslySetInnerHTML=${{ __html: htmlOut }}></span>`
                    : html`<span class="content">${r.text || ' '}</span>`;
                return html`<div key=${j} class="row ${r.kind}"
                    onClick=${() => {
                        if (r.kind === 'add' || r.kind === 'ctx') onLineClick?.(file, r.newN, parts[1]);
                        else onLineClick?.(file, r.oldN, parts[0]);
                    }}>
                    <span class="numDel">${r.oldN}</span>
                    <span class="numAdd">${r.newN}</span>
                    ${content}
                </div>`;
            })}
        </div>`)}
    </div>`;
}

// ─── styles ───────────────────────────────────────────────────────────────

cl.wrap = css`flex: 1; display: flex; flex-direction: column; overflow: hidden; min-height: 0;`;
cl.toolbar = css`
  display: flex; align-items: center; gap: 6px; padding: 6px 10px;
  background: var(--surface); border-bottom: 1px solid var(--border); flex-shrink: 0;
`;
cl.btn = css`
  background: var(--surface2); color: var(--text); border: 1px solid var(--border);
  padding: 4px 10px; cursor: pointer; font-size: 12px;
  &:hover:not(:disabled) { background: var(--surface3); }
  &:disabled { opacity: 0.5; cursor: default; }
`;
cl.combo = css`
  background: var(--surface2); color: var(--text); border: 1px solid var(--border);
  padding: 4px 8px; cursor: pointer; font-size: 12px; display: inline-flex; align-items: center; gap: 4px;
  &:hover { background: var(--surface3); }
  & .arr { color: var(--text-dim); font-size: 9px; }
`;
cl.msg = css`
  flex: 1; min-width: 120px; background: var(--bg); color: var(--text);
  border: 1px solid var(--border); padding: 4px 8px; font-size: 12px; outline: none;
  &:focus { border-color: var(--accent); }
`;
cl.error = css`
  padding: 6px 10px; background: var(--surface2); color: var(--red);
  font-size: 12px; white-space: pre-wrap; border-bottom: 1px solid var(--border);
`;
cl.scroll = css`flex: 1; overflow: auto;`;
cl.table = css`
  width: 100%; border-collapse: collapse; font-size: 12px;
  & thead th {
    text-align: left; font-weight: normal; color: var(--text-dim);
    background: var(--surface); padding: 4px 8px; border-bottom: 1px solid var(--border);
    position: sticky; top: 0;
  }
  & th.staged, & th.state, & th.checkout { width: 60px; }
  & th.empty { width: 100%; }
`;
cl.fileRow = css`
  & td { padding: 4px 8px; border-bottom: 1px solid var(--border); vertical-align: middle; }
  & td.filename { cursor: pointer; user-select: none; }
  & td.filename:hover { color: var(--accent); }
  & td.state { font-family: monospace; color: var(--accent); user-select: none; }
  & input[type="checkbox"] {
    accent-color: var(--accent);
    cursor: pointer;
  }
`;
cl.partial = css`opacity: 0.5;`;
cl.commitGroup = css`display: inline-flex; align-items: stretch;`;
cl.commitMain = css`border-right: none;`;
cl.commitArrow = css`padding: 4px 6px; border-left: none;`;
cl.rename = css`color: var(--text-dim);`;
cl.xBtn = css`
  background: transparent; border: none; color: var(--text-dim); cursor: pointer;
  font-size: 14px; padding: 0 4px;
  &:hover { color: var(--red); }
`;
cl.emptyRow = css`text-align: center; color: var(--text-dim); padding: 16px; font-style: italic;`;
cl.diffCell = css`padding: 0 !important; background: var(--bg);`;
cl.loading = css`padding: 8px 12px; color: var(--text-dim); font-size: 12px;`;
cl.diffEmpty = css`padding: 8px 12px; color: var(--text-dim); font-size: 12px; font-style: italic;`;
cl.diff = css`font-family: monospace; font-size: var(--font-size-mono, 13px); line-height: 1.5;`;
cl.hunk = css`
  border-bottom: 1px solid var(--border);
  & .hdr { background: var(--surface2); color: var(--accent); padding: 2px 8px; }
  & .row { display: flex; cursor: pointer; }
  & .row:hover { background: var(--surface2); }
  & .numDel, & .numAdd {
    width: 50px; padding: 0 6px; text-align: right; flex-shrink: 0;
    color: var(--text-dim); user-select: none; border-right: 1px solid var(--border);
  }
  & .content { padding: 0 8px; white-space: pre; }
  & .row.add .content { color: var(--green); }
  & .row.add { background: var(--diff-add-bg); }
  & .row.add:hover { background: var(--diff-add-bg-hover); }
  & .row.del .content { color: var(--red); }
  & .row.del { background: var(--diff-del-bg); }
  & .row.del:hover { background: var(--diff-del-bg-hover); }
`;
cl.menu = css`
  position: fixed; z-index: 200; max-width: 480px;
  max-height: 400px; overflow-y: auto;
  background: var(--surface2); border: 1px solid var(--border);
  box-shadow: 0 4px 12px var(--shadow); padding: 4px 0; font-size: 12px;
`;
cl.menuItem = css`
  padding: 4px 12px; color: var(--text); cursor: pointer; white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis;
  &.selected { background: var(--surface3); }
  &:hover { background: var(--accent); color: var(--accent-fg); }
  & b { font-weight: 700; color: inherit; }
`;
