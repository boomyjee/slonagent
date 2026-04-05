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
    // editing: { collection, id, isNew, approval?, edits, chatIdx? }
    //   - existing entity: id set, edits = user field overrides
    //   - new entity: id=null, isNew=true, edits = full form
    //   - approval: id=null, approval=true, edits = AI proposal
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

    // Resolve live editing data — merge project base with local edits
    function resolveEditing() {
        if (!editing) return null;
        if (editing.isNew || editing.approval) return editing.edits;
        const base = (project[editing.collection] || []).find(x => x.id === editing.id) || {};
        return { ...base, ...editing.edits };
    }

    function switchTab(newTab) {
        setTab(newTab);
        setEditing(null);
        send({ type: 'tab_changed', tab: newTab });
    }

    function openEntity(collection, item) {
        setSelected(s => ({ ...s, [collection]: item.id }));
        setEditing({ collection, id: item.id, isNew: false, edits: {} });
    }

    function openNewEntity(collection) {
        const blank = {};
        SCHEMAS[collection].fields.forEach(f => blank[f.name] = '');
        setEditing({ collection, id: null, isNew: true, edits: blank });
    }

    function onFieldChange(name, value) {
        setEditing(e => e && ({ ...e, edits: { ...e.edits, [name]: value } }));
    }

    function saveEntity() {
        if (!editing) return;
        const data = resolveEditing();
        const { id, order, generations, ...fields } = data;
        if (editing.approval) {
            send({ type: 'approval_response', action: 'approve', data: fields });
            markApprovalResolved(editing.chatIdx);
        } else {
            send({
                type: 'edit',
                collection: editing.collection,
                id: editing.isNew ? '' : editing.id,
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
        send({ type: 'delete', collection: editing.collection, id: editing.id });
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
                id: null,
                isNew: true,
                approval: true,
                edits: { ...msgItem.data },
                chatIdx: msgItem.idx,
            });
        }
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
            send({
                type: 'generate',
                collection: 'characters',
                id: portraitModal.charId,
                kind: 'portrait',
                prompt: portraitModal.prompt,
            });
        }
        setPortraitModal(null);
    }

    function newGeneration(owner) {
        setPortraitModal({
            charId: owner.id,
            charName: owner.name,
            prompt: defaultPortraitPrompt(owner),
            approval: false,
        });
    }

    function remixGeneration(owner, gen) {
        setPortraitModal({
            charId: owner.id,
            charName: owner.name,
            prompt: gen.prompt,
            approval: false,
        });
    }

    function setPrimary(owner, gen) {
        send({ type: 'set_primary', collection: 'characters', id: owner.id, generation_id: gen.id });
    }

    function deleteGeneration(owner, gen) {
        if (!confirm('Delete this generation?')) return;
        send({ type: 'delete_generation', collection: 'characters', id: owner.id, generation_id: gen.id });
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
    const editingData = resolveEditing();

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
                ${editing && editingData ? html`
                    <${EntityEditor}
                        schema=${SCHEMAS[editing.collection]}
                        data=${editingData}
                        isNew=${editing.isNew}
                        approval=${editing.approval}
                        onFieldChange=${onFieldChange}
                        onSave=${saveEntity}
                        onCancel=${() => setEditing(null)}
                        onDelete=${deleteEntity}
                        onReject=${rejectEntity}
                        onNewGeneration=${() => newGeneration(editingData)}
                        onRemixGeneration=${gen => remixGeneration(editingData, gen)}
                        onSetPrimary=${gen => setPrimary(editingData, gen)}
                        onDeleteGeneration=${gen => deleteGeneration(editingData, gen)}
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
