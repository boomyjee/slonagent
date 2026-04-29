import { html, Component, css, keyframes } from '../../lib.js';

const cl = {};

// Minimal markdown → HTML, mirrors src/transport/telegram.py:_markdown_to_html.
// Code spans/blocks are stashed first so their contents don't get re-formatted.
function mdToHtml(text) {
    if (!text) return '';
    const blocks = [];
    text = text.replace(/```[\w]*\n?([\s\S]*?)```/g, (_, c) => `\x00CB${blocks.push(c) - 1}\x00`);
    const inlines = [];
    text = text.replace(/`([^`]+)`/g, (_, c) => `\x00IC${inlines.push(c) - 1}\x00`);
    text = text.replace(/^>\s*(.*)$/gm, '$1');
    text = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    text = text.replace(/^(#{1,6})\s+(.+)$/gm, (_, h, t) => `<h${h.length}>${t}</h${h.length}>`);
    text = text.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
    text = text.replace(/\*\*(.+?)\*\*/g, '<b>$1</b>');
    text = text.replace(/__(.+?)__/g, '<b>$1</b>');
    text = text.replace(/(?<!\*)\*([^*\n]+)\*(?!\*)/g, '<i>$1</i>');
    text = text.replace(/(?<![a-zA-Z0-9])_([^_\n]+)_(?![a-zA-Z0-9])/g, '<i>$1</i>');
    text = text.replace(/~~(.+?)~~/g, '<s>$1</s>');
    text = text.replace(/^[-*]\s+/gm, '• ');
    // Bare URLs (not already inside an <a> tag)
    text = text.replace(/(?<!href="|">)(https?:\/\/[^\s<)]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
    // /commands at the start of a message
    text = text.replace(/^(\/\w+)/gm, '<span class="slash-cmd">$1</span>');
    const esc = c => c.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    inlines.forEach((c, i) => { text = text.replace(`\x00IC${i}\x00`, `<code>${esc(c)}</code>`); });
    blocks.forEach((c, i) => { text = text.replace(`\x00CB${i}\x00`, `<pre><code>${esc(c)}</code></pre>`); });
    return text;
}

// Read File → "data:..." dataURL (browser FileReader). Used for images and
// recorded voice blobs.
function readDataUrl(blob) {
    return new Promise((resolve, reject) => {
        const fr = new FileReader();
        fr.onloadend = () => resolve(fr.result);
        fr.onerror = reject;
        fr.readAsDataURL(blob);
    });
}

// Сжимаем картинку до 1920×1920 max и всегда в JPEG q85: токенов меньше,
// сетевой трафик меньше, server ничего не пережимает. Если входной JPEG уже
// помещается — пропускаем без re-encode.
async function compressImage(file, maxSide = 1920, quality = 0.85) {
    const srcUrl = await readDataUrl(file);
    const img = new Image();
    img.src = srcUrl;
    await img.decode();
    const w = img.naturalWidth, h = img.naturalHeight;
    if (Math.max(w, h) <= maxSide && file.type === 'image/jpeg') {
        return { dataUrl: srcUrl, mime: 'image/jpeg', size: file.size };
    }
    const scale = Math.min(1, maxSide / Math.max(w, h));
    const canvas = document.createElement('canvas');
    canvas.width = Math.round(w * scale);
    canvas.height = Math.round(h * scale);
    canvas.getContext('2d').drawImage(img, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise(r => canvas.toBlob(r, 'image/jpeg', quality));
    return { dataUrl: await readDataUrl(blob), mime: 'image/jpeg', size: blob.size };
}

// Browser MediaRecorder gives webm/opus on Chrome (ogg/opus on Firefox).
// libsndfile reads ogg/opus but not webm — convert to PCM via AudioContext
// and encode 16-bit WAV here so the server gets a format we know works.
async function blobToWav(blob) {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const audioBuffer = await ctx.decodeAudioData(await blob.arrayBuffer());
    const numCh = audioBuffer.numberOfChannels;
    const sr = audioBuffer.sampleRate;
    const samples = audioBuffer.length;
    const out = new ArrayBuffer(44 + samples * numCh * 2);
    const v = new DataView(out);
    const wrStr = (off, s) => { for (let i = 0; i < s.length; i++) v.setUint8(off + i, s.charCodeAt(i)); };
    wrStr(0, 'RIFF'); v.setUint32(4, out.byteLength - 8, true); wrStr(8, 'WAVE');
    wrStr(12, 'fmt '); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, numCh, true);
    v.setUint32(24, sr, true); v.setUint32(28, sr * numCh * 2, true);
    v.setUint16(32, numCh * 2, true); v.setUint16(34, 16, true);
    wrStr(36, 'data'); v.setUint32(40, samples * numCh * 2, true);
    const channels = []; for (let c = 0; c < numCh; c++) channels.push(audioBuffer.getChannelData(c));
    let off = 44;
    for (let i = 0; i < samples; i++)
        for (let c = 0; c < numCh; c++, off += 2) {
            const s = Math.max(-1, Math.min(1, channels[c][i]));
            v.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
        }
    return new Blob([out], { type: 'audio/wav' });
}


// Иконки одноцветные (currentColor), чтоб подхватывать color кнопки и не светиться
// эмодзи-палитрой. Lucide-style минимал.
const ICON_PAPERCLIP = html`
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor"
         stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l8.57-8.57A4 4 0 1 1 17.93 8.8L9.41 17.34a2 2 0 1 1-2.83-2.83l8.49-8.48"/>
    </svg>`;
const ICON_MIC = html`
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor"
         stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/>
        <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
        <line x1="12" x2="12" y1="19" y2="22"/>
    </svg>`;
const ICON_SEND = html`
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor"
         stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M12 19V5"/>
        <path d="m5 12 7-7 7 7"/>
    </svg>`;

export class Chat extends Component {
    constructor(props) {
        super(props);
        this.state = {
            messages: [], input: '', expanded: {}, processing: false,
            attachments: [],     // [{kind: 'image'|'audio'|'file_text'|'file_meta', ...}]
            recording: false,
        };
        this._streams = {};
        this._recorder = null;
        // Sticky-bottom flag. Starts true so initial buffer replay (where
        // scrollTop=0 but scrollHeight is already huge) still snaps down.
        // Flipped off when the user scrolls up, back on when they scroll
        // to within 120px of the bottom.
        this._stick = true;
    }

    _onScroll = () => {
        const el = this._scroll;
        if (!el) return;
        this._stick = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
    };

    handleMessage(ev) {
        const m = ev.method;
        if (m === 'send_message') {
            this.setState(({ messages }) => {
                if (ev.stream_id != null && this._streams[ev.stream_id] != null) {
                    const idx = this._streams[ev.stream_id];
                    const next = [...messages];
                    next[idx] = { ...next[idx], text: ev.text, final: ev.final };
                    return { messages: next };
                }
                const next = [...messages, { kind: 'msg', role: 'assistant', text: ev.text, stream_id: ev.stream_id, final: ev.final }];
                if (ev.stream_id != null) this._streams[ev.stream_id] = next.length - 1;
                return { messages: next };
            });
        } else if (m === 'send_thinking' || m === 'send_memory_info') {
            const kind = m === 'send_memory_info' ? 'memory' : 'thinking';
            const streamKey = (kind === 'memory' ? 'm_' : 't_') + ev.stream_id;
            this.setState(({ messages }) => {
                if (ev.stream_id != null && this._streams[streamKey] != null) {
                    const idx = this._streams[streamKey];
                    const next = [...messages];
                    next[idx] = { ...next[idx], text: ev.text, final: ev.final };
                    return { messages: next };
                }
                const next = [...messages, { kind, text: ev.text, stream_id: ev.stream_id, final: ev.final }];
                if (ev.stream_id != null) this._streams[streamKey] = next.length - 1;
                return { messages: next };
            });
        } else if (m === 'on_tool_call') {
            this.setState(({ messages }) => ({
                messages: [...messages, { kind: 'tool', name: ev.name, args: ev.args, result: null }]
            }));
        } else if (m === 'on_tool_result') {
            this.setState(({ messages }) => {
                for (let i = messages.length - 1; i >= 0; i--) {
                    if (messages[i].kind === 'tool' && messages[i].name === ev.name && messages[i].result == null) {
                        const next = [...messages];
                        next[i] = { ...next[i], result: ev.result };
                        return { messages: next };
                    }
                }
                return {};
            });
        } else if (m === 'send_processing') {
            this.setState({ processing: !!ev.active });
        } else if (m === 'send_system_prompt') {
            const preview = ev.text.split('\n')[0].slice(0, 80);
            this.setState(({ messages }) => ({
                messages: [...messages, { kind: 'tool', name: preview, args: null, result: ev.text }]
            }));
        } else if (m === 'inject_message') {
            this.setState(({ messages }) => ({
                messages: [...messages, { kind: 'msg', role: 'inject', text: ev.text }]
            }));
        } else if (m === 'process_message') {
            // Сохраняем структуру: текст + картинки рядом, чтобы при replay
            // картинки тоже отрисовывались. audio к этому моменту уже заменён
            // сервером на text-part с транскрипцией.
            const items = [];
            for (const p of (ev.content_parts || [])) {
                if (p.type === 'text') items.push({ kind: 'text', text: p.text });
                else if (p.type === 'image_url') items.push({ kind: 'image', url: (p.image_url || {}).url });
            }
            if (items.length) {
                this.setState(({ messages }) => {
                    const pi = messages.findLastIndex(m => m.pending);
                    if (pi !== -1) {
                        const next = [...messages];
                        next[pi] = { kind: 'msg', role: 'user', items };
                        return { messages: next };
                    }
                    return { messages: [...messages, { kind: 'msg', role: 'user', items }] };
                });
            }
        }
    }

    componentDidMount() {
        // Snap down once the initial buffer replay has rendered.
        if (this._scroll) this._scroll.scrollTop = this._scroll.scrollHeight;
    }


    _submit() {
        const text = this.state.input.trim();
        const att = this.state.attachments;
        if (!text && !att.length) return;
        const parts = att.map(a => ({
            type: 'file', data: a.base64, mime: a.mime, name: a.name, size: a.size,
        }));
        if (text) parts.push({ type: 'text', text });
        this.props.app.send({
            type: 'transport', method: 'process_message',
            content_parts: parts,
        });
        // Show a temporary placeholder until the server echoes
        // the real process_message back via the event buffer.
        const preview = [];
        for (const a of att) {
            if ((a.mime || '').startsWith('image/')) preview.push({ kind: 'image', url: a.base64url || ('data:' + a.mime + ';base64,' + a.base64) });
        }
        if (text) preview.push({ kind: 'text', text });
        else if (att.length) preview.push({ kind: 'text', text: '⏳ Отправка...' });
        this.setState(({ messages }) => ({
            input: '', attachments: [],
            messages: [...messages, { kind: 'msg', role: 'user', items: preview, pending: true }],
        }));
    }

    async _onFiles(files) {
        const additions = [];
        for (const f of files) {
            try {
                const isImage = (f.type || '').startsWith('image/');
                let dataUrl, mime, size, name;
                if (isImage) {
                    ({ dataUrl, mime, size } = await compressImage(f));
                    name = f.name.replace(/\.[^.]+$/, '') + '.jpg';
                } else {
                    dataUrl = await readDataUrl(f);
                    mime = f.type || 'application/octet-stream';
                    size = f.size;
                    name = f.name;
                }
                additions.push({
                    base64: dataUrl.split(',', 2)[1],
                    name, mime, size,
                });
            } catch (e) {
                console.error('attach failed:', e);
            }
        }
        this.setState(({ attachments }) => ({ attachments: [...attachments, ...additions] }));
    }

    async _toggleRecording() {
        if (this.state.recording) {
            this._recorder?.stop();
            return;
        }
        try {
            const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
            const recorder = new MediaRecorder(stream);
            const chunks = [];
            recorder.ondataavailable = e => { if (e.data && e.data.size) chunks.push(e.data); };
            recorder.onstop = async () => {
                stream.getTracks().forEach(t => t.stop());
                this._recorder = null;
                this.setState({ recording: false });
                try {
                    const wav = await blobToWav(new Blob(chunks, { type: recorder.mimeType }));
                    const dataUrl = await readDataUrl(wav);
                    const base64 = dataUrl.split(',', 2)[1];
                    const seconds = Math.max(1, Math.round((wav.size - 44) / 2 / 16000));  // rough estimate
                    this.setState(({ attachments }) => ({
                        attachments: [...attachments, {
                            base64, mime: 'audio/wav',
                            name: `voice_${seconds}s.wav`, size: wav.size,
                        }],
                    }));
                } catch (e) {
                    console.error('voice encode failed:', e);
                    alert('Voice encoding failed: ' + e.message);
                }
            };
            this._recorder = recorder;
            recorder.start();
            this.setState({ recording: true });
        } catch (e) {
            console.error('mic error:', e);
            alert('Microphone error: ' + e.message);
        }
    }

    _removeAttachment(idx) {
        this.setState(({ attachments }) => ({
            attachments: attachments.filter((_, i) => i !== idx),
        }));
    }

    _formatArgs(args) {
        if (!args) return '';
        const fmt = v => typeof v === 'object' ? JSON.stringify(v, null, 2) : String(v);
        return Object.entries(args).map(([k, v]) => `${k}: ${fmt(v)}`).join('\n');
    }

    _formatResult(result) {
        if (result == null) return null;
        if (typeof result === 'string') {
            if (result.startsWith('{') || result.startsWith('[')) {
                try { result = JSON.parse(result); } catch { return result; }
            } else return result;
        }
        if (typeof result === 'object' && !Array.isArray(result)) {
            return Object.entries(result)
                .filter(([, v]) => v != null && v !== '' && !(Array.isArray(v) && !v.length))
                .map(([k, v]) => `[${k}]\n${typeof v === 'object' ? JSON.stringify(v, null, 2) : String(v)}`)
                .join('\n') || '(empty)';
        }
        return JSON.stringify(result, null, 2);
    }

    render({ connected, className }, { messages, input, expanded, processing }) {
        return html`
            <div class="${cl.chat} ${className || ''}">
                <div class=${cl.header}>
                    <span>Chat</span>
                    ${processing && html`<span class=${cl.spinner}></span>`}
                </div>
                <div class=${cl.messages} ref=${el => this._scroll = el} onScroll=${this._onScroll}>
                    ${messages.map((m, i) => {
                        if (m.kind === 'msg') {
                            // user-сообщения: items[] (text + image_url). assistant/inject:
                            // одна text-строка от send_message stream.
                            if (m.items) return html`
                                <div class="${cl.msg} ${m.role}${m.pending ? ' pending' : ''}">
                                    ${m.items.map(it => it.kind === 'image'
                                        ? html`<img src=${it.url} class=${cl.thumb} />`
                                        : html`<div dangerouslySetInnerHTML=${{__html: mdToHtml(it.text)}}></div>`)}
                                </div>
                            `;
                            return html`
                                <div class="${cl.msg} ${m.role}"
                                     dangerouslySetInnerHTML=${{__html: mdToHtml(m.text)}}></div>
                            `;
                        }
                        if (m.kind === 'thinking' || m.kind === 'memory') {
                            const isCollapsed = !(expanded[i] ?? false) && m.final;
                            return html`
                                <div
                                    class="${cl.msg} thinking${m.kind === 'memory' ? ' memory' : ''}${isCollapsed ? ' collapsed' : ''}"
                                    onClick=${() => this.setState(({ expanded: e }) => ({ expanded: { ...e, [i]: isCollapsed } }))}
                                    dangerouslySetInnerHTML=${{__html: mdToHtml((m.text || '').trimEnd())}}></div>
                            `;
                        }
                        if (m.kind === 'tool') {
                            const open = expanded[i];
                            const argsText = this._formatArgs(m.args);
                            const resultText = this._formatResult(m.result);
                            return html`
                                <div class=${cl.tool}>
                                    <div class="hdr" onClick=${() => this.setState(({ expanded: e }) => ({ expanded: { ...e, [i]: !open } }))}>
                                        <span class="arr">${open ? '\u25BC' : '\u25B6'}</span>
                                        <span>\u2699 ${m.name}</span>
                                    </div>
                                    ${open && argsText && html`<div class="body">${argsText}</div>`}
                                    ${open && resultText && html`<div class="result">${resultText}</div>`}
                                </div>
                            `;
                        }
                    })}
                </div>
                ${this.state.attachments.length > 0 && html`
                    <div class=${cl.attachments}>
                        ${this.state.attachments.map((a, i) => html`
                            <div class=${cl.chip}>
                                ${a.mime?.startsWith('image/')
                                    ? html`<img src=${`data:${a.mime};base64,${a.base64}`} class=${cl.chipThumb} />`
                                    : html`<span class=${cl.chipIcon}>${a.mime?.startsWith('audio/') ? ICON_MIC : ICON_PAPERCLIP}</span>`}
                                <span class=${cl.chipName}>${a.name}</span>
                                <button class=${cl.chipRemove} onClick=${() => this._removeAttachment(i)}>\u00D7</button>
                            </div>
                        `)}
                    </div>
                `}
                <div class=${cl.input}>
                    <input type="file" multiple ref=${el => this._fileInput = el}
                           style="display:none"
                           onChange=${e => { this._onFiles(e.target.files); e.target.value = ''; }} />
                    <button class=${cl.iconBtn} title="Attach files"
                            onClick=${() => this._fileInput?.click()}
                            disabled=${!connected}>${ICON_PAPERCLIP}</button>
                    <button class="${cl.iconBtn} ${this.state.recording ? cl.recording : ''}"
                            title=${this.state.recording ? 'Stop recording' : 'Record voice'}
                            onClick=${() => this._toggleRecording()}
                            disabled=${!connected}>${ICON_MIC}</button>
                    <textarea
                        rows="1"
                        value=${input}
                        onInput=${e => this.setState({ input: e.target.value })}
                        onKeyDown=${e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); this._submit(); } }}
                        placeholder="Write a message..."
                        disabled=${!connected}
                    ></textarea>
                    <button class=${cl.iconBtn} onClick=${() => this._submit()} disabled=${!connected}>${ICON_SEND}</button>
                </div>
            </div>
        `;
    }

    componentDidUpdate() {
        // Honor the sticky-bottom flag \u2014 proximity is recomputed only on user
        // scroll, not on every render, so in-flight updates that grow
        // scrollHeight past the threshold don't disable autoscroll.
        if (this._stick && this._scroll) this._scroll.scrollTop = this._scroll.scrollHeight;
    }
}

// --- styles ---

cl.chat = css`
  flex: 1 1 0; min-width: 200px; min-height: 0; display: flex; flex-direction: column;
  border-left: 1px solid var(--border); background: var(--surface);
  &, & * { box-sizing: border-box; }
`;
cl.header = css`
  padding: 10px 16px; border-bottom: 1px solid var(--border); font-size: 12px;
  color: var(--text-dim); text-transform: uppercase;
  display: flex; align-items: center; gap: 10px;
`;
cl.messages = css`flex: 1; min-height: 0; overflow-y: auto; padding: 12px;`;
cl.msg = css`
  margin-bottom: 10px; font-size: 13px; line-height: 1.5; padding: 8px 12px;
  border-radius: 8px; max-width: 90%; white-space: pre-wrap; word-break: break-word;
  &.user, &.inject { background: var(--accent); color: #1e1e2e; margin-left: auto; }
  &.pending { opacity: 0.5; }
  &.assistant { background: var(--surface2); }
  &.thinking { background: var(--surface2); font-size: 12px; color: var(--text-dim); font-style: italic; }
  &.thinking.memory { color: var(--warn); }
  &.thinking.collapsed {
    /* line-height becomes the visual height of the box; line 2 starts
       AT the bottom edge so overflow has nothing to clip mid-glyph. */
    line-height: 34px; height: 34px;
    padding-top: 0; padding-bottom: 0;
    overflow: hidden; cursor: pointer; opacity: 0.5;
  }
  &.thinking.collapsed:hover { opacity: 0.7; }
  & h1, & h2, & h3, & h4, & h5, & h6 { font-size: 14px; font-weight: 600; margin: 4px 0; }
  & a, & .slash-cmd { color: inherit; text-decoration: underline; cursor: pointer; }
  & code { background: rgba(0,0,0,0.25); padding: 1px 4px; border-radius: 3px;
           font-family: monospace; font-size: 12px; }
  & pre { background: rgba(0,0,0,0.25); padding: 8px 10px; border-radius: 4px;
          margin: 4px 0; overflow-x: auto; white-space: pre; }
  & pre code { background: transparent; padding: 0; }
`;
cl.tool = css`
  margin-bottom: 5px; border: 1px solid var(--border); border-radius: 3px; font-size: 12px;
  border-left: 3px solid var(--accent);
  & .hdr { padding: 5px 10px; background: var(--surface); cursor: pointer; color: var(--text-dim);
            display: flex; align-items: center; gap: 6px; user-select: none; }
  & .hdr:hover { background: var(--surface2); color: var(--text); }
  & .arr { font-size: 9px; }
  & .body { padding: 8px 10px; white-space: pre-wrap; color: var(--accent);
            border-top: 1px solid var(--border); max-height: 400px; overflow-y: auto; word-break: break-word; }
  & .result { padding: 8px 10px; white-space: pre-wrap; color: var(--text);
              border-top: 1px solid var(--border); max-height: 400px; overflow-y: auto; word-break: break-word;
              background: var(--bg); }
`;
const spin = keyframes`to { transform: rotate(360deg); }`;
cl.spinner = css`
  display: inline-block; width: 12px; height: 12px;
  border: 2px solid var(--surface3); border-top-color: var(--accent);
  border-radius: 50%; animation: ${spin} 0.8s linear infinite;
`;
cl.input = css`
  display: flex; align-items: flex-end; gap: 4px;
  margin: 8px; padding: 4px;
  background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
  transition: border-color 0.15s;
  &:focus-within { border-color: var(--accent); }
  & textarea {
    flex: 1; min-width: 0;
    background: transparent; color: var(--text);
    border: none; outline: none; resize: none;
    font-family: inherit; font-size: 13px; line-height: 1.5;
    padding: 6px 4px;
    /* CSS-нативный autosize. Поддерживается Chrome 123+ / Firefox 137+ /
       Safari 18.4+. min-height: 1lh = ровно одна строка по line-height,
       max-height: 5lh — пять. Без JS и без mount-flicker. */
    field-sizing: content;
    min-height: 1lh; max-height: 5lh;
    overflow-y: auto;
  }
  & textarea:disabled { color: var(--text-dim); cursor: not-allowed; }
`;
cl.iconBtn = css`
  flex: 0 0 auto;
  background: transparent; color: var(--text-dim); border: none;
  padding: 0; width: 32px; height: 32px; cursor: pointer;
  border-radius: 4px;
  display: inline-flex; align-items: center; justify-content: center;
  transition: background 0.1s, color 0.1s;
  &:hover:not(:disabled) { background: var(--surface2); color: var(--text); }
  &:disabled { opacity: 0.4; cursor: default; }
  & svg { display: block; }
`;
const pulse = keyframes`
  0%, 100% { opacity: 1; }
  50% { opacity: 0.35; }
`;
cl.recording = css`
  color: var(--err, #e74c3c) !important;
  animation: ${pulse} 0.8s infinite;
`;
cl.attachments = css`
  display: flex; flex-wrap: wrap; gap: 6px;
  padding: 8px 10px; border-top: 1px solid var(--border);
  background: var(--surface);
`;
cl.chip = css`
  display: flex; align-items: center; gap: 6px;
  padding: 4px 6px 4px 4px; background: var(--surface2);
  border: 1px solid var(--border); border-radius: 4px;
  font-size: 12px; max-width: 240px;
`;
cl.chipThumb = css`
  width: 28px; height: 28px; object-fit: cover; border-radius: 3px;
  display: block;
`;
cl.chipIcon = css`color: var(--text-dim); display: inline-flex; & svg { display: block; }`;
cl.chipName = css`overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1;`;
cl.chipRemove = css`
  background: none; border: none; color: var(--text-dim); cursor: pointer;
  padding: 0 2px; font-size: 16px; line-height: 1;
  &:hover { color: var(--text); }
`;
cl.thumb = css`
  max-width: 240px; max-height: 240px; border-radius: 4px;
  display: block; margin: 4px 0;
`;
