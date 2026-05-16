import { html, Component, css, keyframes, createContext } from '../../lib.js';
import { Lightbox } from './Lightbox.js';

const cl = {};


export const IdeContext = createContext(null);

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
    // Markdown tables: detect consecutive lines with | delimiters and a separator row
    text = text.replace(/(?:^|\n)((?:\|[^\n]+\|\n)+)/g, (match, block) => {
        const rows = block.trim().split('\n').filter(r => r.trim());
        if (rows.length < 2) return match;
        const sepIdx = rows.findIndex(r => /^\|[\s:]*-+[\s:]*(\|[\s:]*-+[\s:]*)+\|?$/.test(r.trim()));
        if (sepIdx < 1) return match;
        const parseRow = (r, tag) => '<tr>' + r.replace(/^\||\|$/g, '').split('|').map(c => `<${tag}>${c.trim()}</${tag}>`).join('') + '</tr>';
        const head = rows.slice(0, sepIdx).map(r => parseRow(r, 'th')).join('');
        const body = rows.slice(sepIdx + 1).map(r => parseRow(r, 'td')).join('');
        return '\n<table><thead>' + head + '</thead><tbody>' + body + '</tbody></table>\n';
    });
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

function formatBytes(n) {
    if (n == null) return '';
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    return (n / (1024 * 1024)).toFixed(1) + ' MB';
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


// Один диалог = один тред. Владеет своим списком сообщений и стрим-индексом
// (messages/streams в state); Chat выше — лишь маршрутизатор: прокидывает
// входящие события в handleEvent. История подгружается на mount по thread_id.
//
// state.messages — массив элементов, каждый со своим kind:
//   msg | thinking | memory | tool | media | suggestions
// state.streams — { streamKey → idx в messages } для апдейтов on-the-fly:
//   `${stream_id}` для send_message, `t_${stream_id}` для thinking,
//   `m_${stream_id}` для memory_info. Индекс держим, чтоб расти в одном
//   и том же сообщении на каждый чанк, а не плодить новые.
export class ChatDialog extends Component {
    static contextType = IdeContext;

    constructor(props) {
        super(props);
        this.state = {
            messages: [], streams: {},
            input: '', expanded: {},
            attachments: [],     // [{kind: 'image'|'audio'|'file_text'|'file_meta', ...}]
            recording: false,
            palette: null,       // {items: [{cmd, desc}], selected: int} when /-popup visible
            ready: false,        // false до завершения history fetch
        };
        this._recorder = null;
        // Sticky-bottom flag.
        this._stick = true;
        this._lastSeenId = -1;
    }

    componentDidMount() {
        // Snap down once the initial buffer replay has rendered.
        if (this._scroll) this._scroll.scrollTop = this._scroll.scrollHeight;
        this._fetchHistory();
    }    

    async _fetchHistory() {
        let events = [];
        try {
            const r = await fetch(`api/history?thread_id=${encodeURIComponent(this.props.threadId || '')}`);
            if (r.ok) ({ events } = await r.json());
        } catch (e) {
            console.warn('history fetch failed', e);
        }
        for (const e of events || []) {
            if (e.id != null && e.id > this._lastSeenId) this._lastSeenId = e.id;
        }
        this.setState(prev => ({
            ...(events || []).reduce((s, e) => this._reduce(s, e), prev),
            ready: true,
        }));
    }    

    componentDidUpdate() {
        // Honor the sticky-bottom flag — proximity is recomputed only on user scroll
        if (this._stick && this._scroll) this._scroll.scrollTop = this._scroll.scrollHeight;
        if (this.state.ready && this._pending?.length)
            for (const ev of this._pending.splice(0)) this.handleEvent(ev);
    }    

    handleEvent(ev) {
        if (!this.state.ready) return (this._pending ??= []).push(ev);
        if (ev.id != null && ev.id <= this._lastSeenId) return;
        if (ev.id != null) this._lastSeenId = ev.id;
        this.setState(prev => this._reduce(prev, ev));
    }

    // Чистая (state, ev) → state редукция. Без `this`/DOM/setState — поэтому
    // вызывается и из handleEvent (live), и из _fetchHistory (батч событий
    // с диска), и напрямую из тестов. Возвращает старый state без копии,
    // если событие безымённое или нет работы — Preact это понимает.
    _reduce(state, ev) {
        const m = ev.method;
        const sid = ev.stream_id;
        const sk = sid != null ? String(sid) : null;

        if (m === 'send_message') {
            return this._stream(state, sk, ev, { kind: 'msg', role: 'assistant' });
        }
        if (m === 'send_thinking') {
            return this._stream(state, sk && `t_${sk}`, ev, { kind: 'thinking' });
        }
        if (m === 'send_memory_info') {
            // Memory observations are single-shot (no stream/final field
            // in the event). Mark final=true so the bubble can collapse.
            return this._push(state, { kind: 'memory', text: ev.text, final: true });
        }
        if (m === 'on_tool_call') {
            return this._push(state, { kind: 'tool', name: ev.name, args: ev.args, result: null });
        }
        if (m === 'on_tool_result') {
            const i = state.messages.findLastIndex(x =>
                x.kind === 'tool' && x.name === ev.name && x.result == null);
            if (i < 0) return state;
            const messages = state.messages.slice();
            messages[i] = { ...messages[i], result: ev.result };
            return { ...state, messages };
        }
        if (m === 'send_system_prompt') {
            const preview = (ev.text || '').split('\n')[0].slice(0, 80);
            return this._push(state, { kind: 'tool', name: preview, args: null, result: ev.text });
        }
        if (m === 'inject_message') {
            return this._push(state, { kind: 'msg', role: 'inject', text: ev.text });
        }
        if (m === 'send_images') {
            return this._push(state, { kind: 'media', media: 'images', items: ev.items || [] });
        }
        if (m === 'send_files') {
            return this._push(state, { kind: 'media', media: 'files', items: ev.items || [] });
        }
        if (m === 'send_voice') {
            return this._push(state, { kind: 'media', media: 'voice', items: [ev.item] });
        }
        if (m === 'send_suggestions') {
            return this._push(state, { kind: 'suggestions', text: ev.text || '', options: ev.options || [] });
        }
        if (m === 'process_message') {
            const items = [];
            for (const p of (ev.content_parts || [])) {
                if (p.type === 'text') items.push({ kind: 'text', text: p.text });
                else if (p.type === 'image_url') items.push({ kind: 'image', url: (p.image_url || {}).url });
            }
            if (!items.length) return state;
            const pi = state.messages.findLastIndex(x => x.pending);
            if (pi !== -1) {
                const messages = state.messages.slice();
                messages[pi] = { kind: 'msg', role: 'user', items };
                return { ...state, messages };
            }
            return this._push(state, { kind: 'msg', role: 'user', items });
        }
        return state;
    }

    _push(state, msg) {
        return { ...state, messages: [...state.messages, msg] };
    }

    _stream(state, sk, ev, base) {
        if (sk && state.streams[sk] != null) {
            const idx = state.streams[sk];
            const messages = state.messages.slice();
            messages[idx] = { ...messages[idx], text: ev.text, final: ev.final };
            return { ...state, messages };
        }
        const messages = [...state.messages, { ...base, text: ev.text, stream_id: ev.stream_id, final: ev.final }];
        if (sk) {
            return { ...state, messages, streams: { ...state.streams, [sk]: messages.length - 1 } };
        }
        return { ...state, messages };
    }

    _addPending(state, preview) {
        return this._push(state, { kind: 'msg', role: 'user', items: preview, pending: true });
    }

    _onScroll = () => {
        const el = this._scroll;
        if (!el) return;
        this._stick = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
    };

    _onInput = (e) => {
        const v = e.target.value;
        this.setState({ input: v });
        if (!v.startsWith('/')) {
            this._allCmds = null;
            this._paletteDismissed = false;
            if (this.state.palette) this.setState({ palette: null });
            return;
        }
        if (this._paletteDismissed) return;
        if (this._allCmds) this._filterPalette(v);
        else this._fetchCommands(v);
    };

    async _fetchCommands(input) {
        try {
            const r = await fetch('api/commands');
            if (!r.ok) return;
            const cmds = await r.json();
            this._allCmds = Object.entries(cmds).map(([cmd, desc]) => ({ cmd, desc }));
            this._filterPalette(input);
        } catch (e) {
            console.error('commands fetch:', e);
        }
    }

    _filterPalette(input) {
        const q = input.slice(1).toLowerCase().split(/\s+/)[0];
        const items = (this._allCmds || []).filter(({ cmd, desc }) =>
            cmd.toLowerCase().includes(q) || (desc || '').toLowerCase().includes(q)
        );
        this.setState({ palette: items.length ? { items, selected: -1 } : null });
    }

    _onPaste = (e) => {
        const items = e.clipboardData?.items;
        if (!items) return;
        const files = [];
        for (const item of items) {
            if (item.kind === 'file') files.push(item.getAsFile());
        }
        if (files.length) {
            e.preventDefault();
            this._onFiles(files);
        }
    };

    _onKeyDown = (e) => {
        const { palette } = this.state;
        if (palette && palette.items.length) {
            const n = palette.items.length;
            if (e.key === 'ArrowDown') {
                e.preventDefault();
                const next = palette.selected < 0 ? 0 : (palette.selected + 1) % n;
                this.setState({ palette: { ...palette, selected: next } });
                return;
            }
            if (e.key === 'ArrowUp') {
                e.preventDefault();
                const next = palette.selected < 0 ? n - 1 : (palette.selected - 1 + n) % n;
                this.setState({ palette: { ...palette, selected: next } });
                return;
            }
            if (e.key === 'Tab' && palette.selected >= 0) {
                e.preventDefault();
                const item = palette.items[palette.selected];
                this._allCmds = null;
                this.setState({ input: '/' + item.cmd + ' ', palette: null });
                return;
            }
            if (e.key === 'Enter' && !e.shiftKey && palette.selected >= 0) {
                e.preventDefault();
                const item = palette.items[palette.selected];
                this.setState({ input: '/' + item.cmd }, () => this._submit());
                return;
            }
            if (e.key === 'Escape') {
                e.preventDefault();
                this._paletteDismissed = true;
                this._allCmds = null;
                this.setState({ palette: null });
                return;
            }
        }
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            this._submit();
        }
    };

    _sendOption(opt) {
        // Юзер кликнул кнопку suggestion — отправляем её текст как обычное сообщение.
        this._send([{ type: 'text', text: opt }], [{ kind: 'text', text: opt }]);
    }

    _send(parts, preview) {
        this.props.onSend?.(parts);
        this.setState(prev => this._addPending(prev, preview));
    }

    _submit() {
        const text = this.state.input.trim();
        const att = this.state.attachments;
        if (!text && !att.length) return;
        const parts = [];
        const preview = [];
        const ide = this.context;
        // /-команды — это bypass'ы (skill bypass_commands), агенту они не идут, прицеплять туда ide-контекст нет смысла.
        if (ide?.selection && ide.enabled && !text.startsWith('/')) {
            const tag = ChatDialog._formatIdeTag(ide.selection);
            parts.push({ type: 'text', text: tag });
            preview.push({ kind: 'text', text: tag });
        }
        for (const a of att) {
            parts.push({ type: 'file', data: a.base64, mime: a.mime, name: a.name, size: a.size });
            if ((a.mime || '').startsWith('image/')) preview.push({ kind: 'image', url: a.base64url || ('data:' + a.mime + ';base64,' + a.base64) });
        }
        if (text) {
            parts.push({ type: 'text', text });
            preview.push({ kind: 'text', text });
        } else if (att.length) {
            preview.push({ kind: 'text', text: '⏳ Отправка...' });
        }

        this._send(parts, preview);
        this._allCmds = null;
        this._paletteDismissed = false;
        this.setState({ input: '', attachments: [], palette: null });
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

    _downloadAttachment(att) {
        const a = document.createElement('a');
        a.href = `data:${att.mime || 'application/octet-stream'};base64,${att.base64}`;
        a.download = att.name || 'download';
        document.body.appendChild(a);
        a.click();
        a.remove();
    }

    static _formatIdeTag(ide) {
        if (!ide) return '';
        if (ide.startLine != null) {
            return `<ide_selection>The user selected the lines ${ide.startLine} to ${ide.endLine} from ${ide.file}:\n${ide.text}\n\nThis may or may not be related to the current task.</ide_selection>`;
        }
        return `<ide_opened_file>The user opened the file ${ide.file} in the IDE. This may or may not be related to the current task.</ide_opened_file>`;
    }

    static _ideChipLabel(ide) {
        if (!ide) return '';
        const name = ide.file.split('/').pop();
        if (ide.startLine != null) {
            const range = ide.startLine === ide.endLine ? `L${ide.startLine}` : `L${ide.startLine}-${ide.endLine}`;
            return `${name} ${range}`;
        }
        return name;
    }

    // Подсказка для tool-заголовка: basename файла из file_path/path. Для
    // tool'ов без file-аргумента — null, ничего не подмешиваем.
    static _toolHint(args) {
        if (!args) return null;
        const path = args.file_path || args.path;
        if (typeof path !== 'string' || !path) return null;
        return path.split(/[/\\]/).pop() || path;
    }

    // Извлекает строки-комментарии из bash-команды (модель часто
    // складывает rationale в `# ...`-строки внутри command). Используется
    // как preview в свёрнутом виде sandbox_exec — чтоб не разворачивать
    // всю команду чтобы понять что она делает.
    static _bashComments(command) {
        if (typeof command !== 'string' || !command) return '';
        return command.split('\n')
            .map(l => l.trim())
            .filter(l => l.startsWith('#') && !l.startsWith('#!'))
            .map(l => l.replace(/^#+\s*/, ''))
            .join('\n');
    }

    // Распознаёт ide-теги в тексте user-item'а — нужно рендеру баббла, чтоб
    // вместо raw <ide_*>...</...> показать чип.
    static _parseIdeTag(text) {
        // (.+?) для file — non-greedy до `:\n`. На Windows путь содержит `:`
        // (E:/...), `[^:]+` срезал бы только букву диска.
        const sel = text.match(/^<ide_selection>The user selected the lines (\d+) to (\d+) from (.+?):\n([\s\S]*?)\n\nThis may or may not be related to the current task\.<\/ide_selection>$/);
        if (sel) return { file: sel[3], startLine: +sel[1], endLine: +sel[2], text: sel[4] };
        const opened = text.match(/^<ide_opened_file>The user opened the file (.+?) in the IDE\. This may or may not be related to the current task\.<\/ide_opened_file>$/);
        if (opened) return { file: opened[1] };
        return null;
    }

    // Один item в user-баббле: картинка, ide-tag (chip с текстом в title) или
    // обычный markdown-текст.
    _renderUserItem(it) {
        if (it.kind === 'image') return html`<img src=${it.url} class=${cl.thumb} />`;
        const ide = ChatDialog._parseIdeTag(it.text || '');
        if (!ide) return html`<div dangerouslySetInnerHTML=${{__html: mdToHtml(it.text)}}></div>`;
        const tip = ide.text ? `${ide.file}\n\n${ide.text}` : ide.file;
        return html`<div class="${cl.idePill} ${cl.idePillBubble}" title=${tip}>${ChatDialog._ideChipLabel(ide)}</div>`;
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

    render({ connected, className }, { input, expanded, messages }) {
        return html`
            <div class="${cl.dialog} ${className || ''}">
                <div class=${cl.messages} ref=${el => this._scroll = el} onScroll=${this._onScroll}>
                    ${messages.map((m, i) => {
                        if (m.kind === 'msg') {
                            // user-сообщения: items[] (text + image_url). assistant/inject:
                            // одна text-строка от send_message stream.
                            if (m.items) return html`
                                <div class="${cl.msg} ${m.role}${m.pending ? ' pending' : ''}">
                                    ${m.items.map(it => this._renderUserItem(it))}
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
                            const hint = ChatDialog._toolHint(m.args);
                            const comments = m.name === 'sandbox_exec' && !open
                                ? ChatDialog._bashComments(m.args?.command) : '';
                            return html`
                                <div class=${cl.tool}>
                                    <div class="hdr" onClick=${() => this.setState(({ expanded: e }) => ({ expanded: { ...e, [i]: !open } }))}>
                                        <span class="arr">${open ? '▼' : '▶'}</span>
                                        <span>⚙ ${m.name}</span>
                                        ${hint && html`<span class=${cl.toolHint}>${hint}</span>`}
                                    </div>
                                    ${comments && html`<div class="comments">${comments}</div>`}
                                    ${open && argsText && html`<div class="body">${argsText}</div>`}
                                    ${open && resultText && html`<div class="result">${resultText}</div>`}
                                </div>
                            `;
                        }
                        if (m.kind === 'media') {
                            if (m.media === 'images') {
                                const group = `chat-${i}`;
                                return html`
                                    <div class="${cl.msg} assistant ${cl.album}">
                                        ${m.items.map(it => html`
                                            <img src=${it.url} alt=${it.name}
                                                 data-lightbox=${group}
                                                 class=${cl.albumThumb}
                                                 onClick=${e => Lightbox.open(e.currentTarget)} />`)}
                                    </div>`;
                            }
                            if (m.media === 'voice') {
                                const it = m.items[0] || {};
                                return html`
                                    <div class="${cl.msg} assistant">
                                        <audio controls src=${it.url} style="display:block; max-width:100%;"></audio>
                                    </div>`;
                            }
                            // files
                            return html`
                                <div class="${cl.msg} assistant">
                                    ${m.items.map(it => html`
                                        <a href=${it.url} download=${it.name} class=${cl.fileChip}>
                                            <span class=${cl.fileChipIcon}>📎</span>
                                            <span class=${cl.fileChipName}>${it.name}</span>
                                            <span class=${cl.fileChipSize}>${formatBytes(it.size)}</span>
                                        </a>`)}
                                </div>`;
                        }
                        if (m.kind === 'suggestions') {
                            return html`
                                <div class="${cl.msg} assistant">
                                    ${m.text && html`<div dangerouslySetInnerHTML=${{__html: mdToHtml(m.text)}}></div>`}
                                    <div class=${cl.suggestions}>
                                        ${m.options.map(opt => html`
                                            <button class=${cl.suggestionBtn}
                                                    onClick=${() => this._sendOption(opt)}>${opt}</button>`)}
                                    </div>
                                </div>`;
                        }
                    })}
                </div>
                ${this.state.attachments.length > 0 && html`
                    <div class=${cl.attachments}>
                        ${this.state.attachments.map((a, i) => html`
                            <div class=${cl.chip} title="Скачать"
                                 onClick=${() => this._downloadAttachment(a)}>
                                ${a.mime?.startsWith('image/')
                                    ? html`<img src=${`data:${a.mime};base64,${a.base64}`} class=${cl.chipThumb} />`
                                    : html`<span class=${cl.chipIcon}>${a.mime?.startsWith('audio/') ? ICON_MIC : ICON_PAPERCLIP}</span>`}
                                <span class=${cl.chipName}>${a.name}</span>
                                <button class=${cl.chipRemove}
                                        onClick=${(e) => { e.stopPropagation(); this._removeAttachment(i); }}>×</button>
                            </div>
                        `)}
                    </div>
                `}
                <div class=${cl.input}>
                    ${this.context?.selection && html`
                        <div class="${cl.idePill} ${this.context.enabled ? '' : cl.idePillOff}"
                             title=${this.context.selection.file + ' · клик переключает прикрепление к сообщениям'}
                             onMouseDown=${e => e.preventDefault()}
                             onClick=${() => { this.context.enabled = !this.context.enabled; this.context.change(); }}>
                            <span>${ChatDialog._ideChipLabel(this.context.selection)}</span>
                        </div>`}
                    ${this.state.palette && html`
                        <div class=${cl.palette}>
                            ${this.state.palette.items.map((item, i) => html`
                                <div class="${cl.paletteItem} ${i === this.state.palette.selected ? 'sel' : ''}"
                                     onMouseDown=${e => {
                                         e.preventDefault();
                                         this._allCmds = null;
                                         this.setState({ input: '/' + item.cmd + ' ', palette: null });
                                     }}>
                                    <span class="cmd">/${item.cmd}</span>
                                    <span class="desc">${item.desc}</span>
                                </div>`)}
                        </div>`}
                    <input type="file" multiple ref=${el => this._fileInput = el}
                           style="display:none"
                           onChange=${e => { this._onFiles([...e.target.files]); e.target.value = ''; }} />
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
                        onInput=${this._onInput}
                        onKeyDown=${this._onKeyDown}
                        onPaste=${this._onPaste}
                        placeholder="Write a message..."
                        disabled=${!connected}
                    ></textarea>
                    <button class=${cl.iconBtn} onClick=${() => this._submit()} disabled=${!connected}>${ICON_SEND}</button>
                </div>
            </div>
        `;
    }


}

// --- styles ---

cl.dialog = css`
  flex: 1 1 0; min-height: 0; display: flex; flex-direction: column;
  position: relative;
  &, & * { box-sizing: border-box; }
`;
cl.messages = css`flex: 1; min-height: 0; overflow-y: auto; padding: 12px;`;
cl.msg = css`
  position: relative;
  margin-bottom: 10px; font-size: 13px; line-height: 1.5; padding: 8px 12px;
  border-radius: 8px; max-width: 90%; white-space: pre-wrap; word-break: break-word;
  &.user, &.inject { background: var(--user-msg-bg); color: var(--user-msg-fg); margin-left: auto; }
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
  & table { border-collapse: collapse; margin: 6px 0; font-size: 13px; width: auto; }
  & th, & td { border: 1px solid var(--border); padding: 4px 8px; text-align: left; white-space: normal; }
  & th { background: rgba(0,0,0,0.15); font-weight: 600; }
  & tr:nth-child(even) td { background: rgba(0,0,0,0.05); }
`;
cl.toolHint = css`
  margin-left: 0px;
  margin-top: 2px;
  padding: 2px 6px;
  background: var(--bg);
  font-family: monospace; font-size: 10px;
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
  & .comments { padding: 8px 10px; white-space: pre-wrap; color: var(--green); font-style: italic;
                border-top: 1px solid var(--border); max-height: 400px; overflow-y: auto; word-break: break-word; }
  & .result { padding: 8px 10px; white-space: pre-wrap; color: var(--text);
              border-top: 1px solid var(--border); max-height: 400px; overflow-y: auto; word-break: break-word;
              background: var(--bg); }
`;
cl.input = css`
  display: flex; align-items: flex-end; gap: 4px;
  margin: 8px; padding: 4px;
  background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
  transition: border-color 0.15s;
  position: relative;
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
  cursor: pointer;
  &:hover { background: var(--surface3); }
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
cl.idePill = css`
  position: absolute; top: -7px; right: 45px; z-index: 5;
  padding: 1px 8px;
  background: var(--surface);
  color: var(--accent);
  border: 1px solid var(--accent); border-radius: 10px;
  font-size: 8px; font-family: monospace;
  cursor: pointer; user-select: none;
  &:hover { background: var(--surface2); }
`;
cl.idePillOff = css`
  color: var(--text-dim) !important;
  border-color: var(--border) !important;
`;
cl.idePillBubble = css`
  top: -7px; right: 42px;
`;
cl.thumb = css`
  max-width: 240px; max-height: 240px; border-radius: 4px;
  display: block; margin: 4px 0;
`;
cl.palette = css`
  position: absolute; bottom: calc(100% + 4px); left: 0; right: 0;
  max-height: 240px; overflow-y: auto;
  background: var(--surface2); border: 1px solid var(--border); border-radius: 6px;
  box-shadow: 0 4px 12px rgba(0,0,0,0.4);
  z-index: 10;
`;
cl.paletteItem = css`
  display: flex; align-items: baseline; gap: 8px;
  padding: 6px 10px; cursor: pointer; font-size: 13px;
  & .cmd { font-family: monospace; color: var(--accent); flex-shrink: 0; }
  & .desc { color: var(--text-dim); font-size: 12px;
            overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  &:hover, &.sel { background: var(--surface3, var(--bg)); }
`;
cl.album = css`
  display: flex; flex-wrap: wrap; gap: 4px;
  padding: 4px;
`;
cl.albumThumb = css`
  height: 120px; width: auto; max-width: 100%;
  border-radius: 4px;
  cursor: zoom-in; display: block;
  &:hover { opacity: 0.85; }
`;
cl.fileChip = css`
  display: flex; align-items: center; gap: 8px;
  padding: 6px 10px; margin: 4px 0;
  background: var(--bg); border: 1px solid var(--border); border-radius: 6px;
  text-decoration: none; color: var(--text); font-size: 12px;
  max-width: 320px;
  &:hover { background: var(--surface3, var(--surface2)); }
`;
cl.fileChipIcon = css`flex-shrink: 0;`;
cl.fileChipName = css`flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;`;
cl.fileChipSize = css`color: var(--text-dim); flex-shrink: 0;`;
cl.suggestions = css`
  display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px;
`;
cl.suggestionBtn = css`
  background: var(--bg); color: var(--text);
  border: 1px solid var(--border); border-radius: 16px;
  padding: 6px 14px; font-size: 12px; cursor: pointer;
  &:hover { background: var(--accent); color: #1e1e2e; border-color: var(--accent); }
`;
