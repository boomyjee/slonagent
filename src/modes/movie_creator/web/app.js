// Movie Creator — Preact app entry
import { render, html, useState, useEffect, useRef, SCHEMAS, TAB_COLLECTION, defaultPortraitPrompt, createWS } from './lib.js';
import { Resizer } from './components/Resizer.js';
import { EntityList } from './components/EntityList.js';
import { EntityEditor } from './components/EntityEditor.js';
import { PortraitModal } from './components/PortraitModal.js';
import { Chat } from './components/Chat.js';

function App() {
    const [connected, setConnected] = useState(false);
    const [project, setProject] = useState({ title: '', scenes: [], characters: [] });
    const [tab, setTab] = useState('screenplay');
    const [selected, setSelected] = useState({ scenes: null, characters: null });
    const [editing, setEditing] = useState(null);
    const [portraitModal, setPortraitModal] = useState(null);
    const [messages, setMessages] = useState([]);
    const wsRef = useRef(null);
    const streamElsRef = useRef({});
    const sidebarRef = useRef(null);
    const chatRef = useRef(null);

    useEffect(() => {
        wsRef.current = createWS(handleMessage, setConnected);
        // eslint-disable-next-line
    }, []);

    function send(msg) { wsRef.current && wsRef.current.send(msg); }

    function handleMessage(msg) {
        if (msg.type === 'project_updated') {
            setProject(msg.project);
        } else if (msg.type === 'message') {
            setMessages(prev => {
                if (msg.stream_id != null && streamElsRef.current[msg.stream_id] != null) {
                    const idx = streamElsRef.current[msg.stream_id];
                    const next = [...prev];
                    next[idx] = { ...next[idx], text: msg.text, final: msg.final };
                    return next;
                }
                const next = [...prev, { kind: 'msg', role: msg.role, text: msg.text, stream_id: msg.stream_id, final: msg.final }];
                if (msg.stream_id != null) streamElsRef.current[msg.stream_id] = next.length - 1;
                return next;
            });
        } else if (msg.type === 'tool_call') {
            setMessages(prev => [...prev, { kind: 'tool', name: msg.name }]);
        } else if (msg.type === 'processing') {
            setMessages(prev => [...prev, { kind: 'processing' }]);
        } else if (msg.type === 'processing_done') {
            setMessages(prev => prev.filter(m => m.kind !== 'processing'));
        } else if (msg.type === 'approval_request') {
            setMessages(prev => [...prev, { kind: 'approval', approvalKind: msg.kind, data: msg.data, resolved: false, idx: prev.length }]);
        }
    }

    function switchTab(newTab) {
        setTab(newTab);
        setEditing(null);
        send({ type: 'tab_changed', tab: newTab });
    }

    function openEntity(collection, item) {
        setSelected(s => ({ ...s, [collection]: item.id }));
        setEditing({ collection, data: { ...item }, isNew: false });
    }

    function openNewEntity(collection) {
        const blank = {};
        SCHEMAS[collection].fields.forEach(f => blank[f.name] = '');
        setEditing({ collection, data: blank, isNew: true });
    }

    function saveEntity() {
        if (!editing) return;
        const { id, order, ...fields } = editing.data;
        if (editing.approval) {
            send({ type: 'approval_response', action: 'approve', data: fields });
            markApprovalResolved(editing.chatIdx);
        } else {
            send({
                type: 'edit',
                collection: editing.collection,
                id: editing.isNew ? '' : (id || ''),
                data: fields,
            });
        }
        setEditing(null);
    }

    function rejectEntity() {
        if (!editing || !editing.approval) return;
        const reason = prompt('Reason (optional):') || '';
        send({ type: 'approval_response', action: 'reject', reason });
        markApprovalResolved(editing.chatIdx);
        setEditing(null);
    }

    function deleteEntity() {
        if (!editing || editing.isNew) return;
        if (!confirm(`Delete this ${SCHEMAS[editing.collection].label.toLowerCase()}?`)) return;
        send({ type: 'delete', collection: editing.collection, id: editing.data.id });
        setEditing(null);
    }

    function markApprovalResolved(idx) {
        if (idx == null) return;
        setMessages(prev => prev.map((m, i) => i === idx ? { ...m, resolved: true } : m));
    }

    function handleApprovalClick(msgItem) {
        if (msgItem.resolved) return;
        const kind = msgItem.approvalKind;
        if (kind === 'portrait') {
            setPortraitModal({
                charId: msgItem.data.character_id,
                charName: msgItem.data.character_name,
                prompt: msgItem.data.prompt,
                approval: true,
                chatIdx: msgItem.idx,
            });
        } else {
            const collection = kind === 'scene' ? 'scenes' : 'characters';
            setEditing({
                collection,
                data: { ...msgItem.data },
                isNew: true,
                approval: true,
                chatIdx: msgItem.idx,
            });
        }
    }

    function openPortraitForChar(char) {
        setPortraitModal({
            charId: char.id,
            charName: char.name,
            prompt: defaultPortraitPrompt(char),
            approval: false,
        });
    }

    function generatePortrait() {
        if (!portraitModal) return;
        if (portraitModal.approval) {
            send({
                type: 'approval_response',
                action: 'approve',
                data: { prompt: portraitModal.prompt, character_id: portraitModal.charId, character_name: portraitModal.charName },
            });
            markApprovalResolved(portraitModal.chatIdx);
        } else {
            send({ type: 'generate_portrait', id: portraitModal.charId, prompt: portraitModal.prompt });
        }
        setPortraitModal(null);
    }

    function rejectPortrait() {
        if (!portraitModal || !portraitModal.approval) return;
        const reason = prompt('Reason (optional):') || '';
        send({ type: 'approval_response', action: 'reject', reason });
        markApprovalResolved(portraitModal.chatIdx);
        setPortraitModal(null);
    }

    function sendChat(text) { send({ type: 'chat', text }); }

    const collection = TAB_COLLECTION[tab];
    const schema = collection && SCHEMAS[collection];
    const items = collection ? (project[collection] || []) : [];

    return html`
        <div class="header">
            <h1>Movie Creator</h1>
            <span class="status" style=${{ color: connected ? 'var(--green)' : 'var(--red)' }}>
                ${connected ? 'connected' : 'disconnected'}
            </span>
        </div>
        <div class="tabs">
            ${['screenplay', 'characters', 'storyboard', 'generation'].map(t => html`
                <div class=${'tab' + (tab === t ? ' active' : '')} onClick=${() => switchTab(t)}>
                    ${t.charAt(0).toUpperCase() + t.slice(1)}
                </div>
            `)}
        </div>
        <div class="main">
            <div class="sidebar" ref=${sidebarRef}>
                ${schema ? html`
                    <${EntityList}
                        schema=${schema}
                        items=${items}
                        selectedId=${selected[collection]}
                        onSelect=${item => openEntity(collection, item)}
                        onAdd=${() => openNewEntity(collection)}
                    />
                ` : null}
            </div>
            <${Resizer} targetRef=${sidebarRef} side="left" />
            <div class="center">
                ${editing ? html`
                    <${EntityEditor}
                        schema=${SCHEMAS[editing.collection]}
                        editing=${editing}
                        onChange=${data => setEditing({ ...editing, data })}
                        onSave=${saveEntity}
                        onCancel=${() => setEditing(null)}
                        onDelete=${deleteEntity}
                        onReject=${rejectEntity}
                        onGeneratePortrait=${() => openPortraitForChar(editing.data)}
                    />
                ` : html`
                    <div class="center-empty">
                        ${schema ? schema.selectHint : (tab === 'storyboard' ? 'Storyboard (coming soon)' : 'Generation (coming soon)')}
                    </div>
                `}
            </div>
            <${Resizer} targetRef=${chatRef} side="right" />
            <${Chat} rootRef=${chatRef} messages=${messages} onSend=${sendChat} onApprovalClick=${handleApprovalClick} />
        </div>
        ${portraitModal ? html`
            <${PortraitModal}
                modal=${portraitModal}
                onChange=${prompt => setPortraitModal({ ...portraitModal, prompt })}
                onGenerate=${generatePortrait}
                onReject=${rejectPortrait}
                onCancel=${() => setPortraitModal(null)}
            />
        ` : null}
    `;
}

render(html`<${App} />`, document.body);
