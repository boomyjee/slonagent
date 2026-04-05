// Movie Creator — Preact app entry
import { render, html, useState, useEffect, useRef, SCHEMAS, TAB_COLLECTION, defaultGenerationPrompt, createWS } from './lib.js';
import { Resizer } from './components/Resizer.js';
import { EntityList } from './components/EntityList.js';
import { EntityEditor } from './components/EntityEditor.js';
import { StoryboardView } from './components/StoryboardView.js';
import { GenerationModal } from './components/GenerationModal.js';
import { BulkShotsModal } from './components/BulkShotsModal.js';
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
    const [genModal, setGenModal] = useState(null);
    const [bulkModal, setBulkModal] = useState(null);
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
        if (newTab === 'storyboard') {
            send({ type: 'scope_changed', scope: { scene_id: selected.scenes || '' } });
        } else {
            send({ type: 'scope_changed', scope: {} });
        }
    }

    function openEntity(collection, item) {
        setSelected(s => ({ ...s, [collection]: item.id }));
        if (tab === 'storyboard' && collection === 'scenes') {
            send({ type: 'scope_changed', scope: { scene_id: item.id } });
            setEditing(null);
            return;
        }
        setEditing({ collection, id: item.id, isNew: false, edits: {} });
    }

    function openNewEntity(collection) {
        const blank = {};
        (SCHEMAS[collection].fields || []).forEach(f => blank[f.name] = '');
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
            setGenModal({
                collection: 'characters',
                kind: 'portrait',
                ownerId: msgItem.data.character_id,
                ownerName: msgItem.data.character_name,
                initialPrompt: msgItem.data.prompt,
                approval: true,
                chatIdx: msgItem.idx,
            });
        } else if (kind === 'shots_bulk') {
            setBulkModal({
                sceneId: msgItem.data.scene_id,
                initialText: msgItem.data.text || '',
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

    function submitGeneration(prompt) {
        if (genModal.approval) {
            send({
                type: 'approval_response',
                action: 'approve',
                data: { prompt, character_id: genModal.ownerId, character_name: genModal.ownerName },
            });
            markApprovalResolved(genModal.chatIdx);
        } else {
            send({
                type: 'generate',
                collection: genModal.collection,
                id: genModal.ownerId,
                kind: genModal.kind,
                prompt,
            });
        }
        setGenModal(null);
    }

    function cancelGeneration() {
        if (genModal.approval) {
            const reason = prompt('Reason (optional):') || '';
            send({ type: 'approval_response', action: 'reject', reason });
            markApprovalResolved(genModal.chatIdx);
        }
        setGenModal(null);
    }

    function approveBulkShots(text) {
        send({
            type: 'approval_response',
            action: 'approve',
            data: { text, scene_id: bulkModal.sceneId },
        });
        markApprovalResolved(bulkModal.chatIdx);
        setBulkModal(null);
    }

    function rejectBulkShots() {
        const reason = prompt('Reason (optional):') || '';
        send({ type: 'approval_response', action: 'reject', reason });
        markApprovalResolved(bulkModal.chatIdx);
        setBulkModal(null);
    }

    function newGeneration(collection, owner) {
        const schema = SCHEMAS[collection];
        setGenModal({
            collection,
            kind: schema.gallery,
            ownerId: owner.id,
            ownerName: owner[schema.titleField] || schema.emptyTitle,
            initialPrompt: defaultGenerationPrompt(collection, owner),
            approval: false,
        });
    }

    function remixGeneration(collection, owner, gen) {
        const schema = SCHEMAS[collection];
        setGenModal({
            collection,
            kind: schema.gallery,
            ownerId: owner.id,
            ownerName: owner[schema.titleField] || schema.emptyTitle,
            initialPrompt: gen.prompt,
            approval: false,
        });
    }

    function setPrimary(collection, owner, gen) {
        send({ type: 'set_primary', collection, id: owner.id, generation_id: gen.id });
    }

    function deleteGeneration(collection, owner, gen) {
        if (!confirm('Delete this generation?')) return;
        send({ type: 'delete_generation', collection, id: owner.id, generation_id: gen.id });
    }

    function sendChat(text) { send({ type: 'chat', text }); }

    const collection = TAB_COLLECTION[tab];
    const schema = collection && SCHEMAS[collection];
    const items = collection ? (project[collection] || []) : [];
    const editingData = resolveEditing();
    const selectedScene = tab === 'storyboard' && selected.scenes
        ? (project.scenes || []).find(s => s.id === selected.scenes) : null;
    const sceneShots = selectedScene
        ? (project.shots || []).filter(s => s.scene_id === selectedScene.id) : [];

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
                        onNewGeneration=${() => newGeneration(editing.collection, editingData)}
                        onRemixGeneration=${gen => remixGeneration(editing.collection, editingData, gen)}
                        onSetPrimary=${gen => setPrimary(editing.collection, editingData, gen)}
                        onDeleteGeneration=${gen => deleteGeneration(editing.collection, editingData, gen)}
                    />
                ` : tab === 'storyboard' && selectedScene ? html`
                    <${StoryboardView}
                        scene=${selectedScene}
                        shots=${sceneShots}
                        onCreate=${() => send({ type: 'edit', collection: 'shots', id: '', data: { scene_id: selectedScene.id, description: '' } })}
                        onUpdate=${(shot, description) => send({ type: 'edit', collection: 'shots', id: shot.id, data: { description } })}
                        onDelete=${shot => confirm('Delete this shot?') && send({ type: 'delete', collection: 'shots', id: shot.id })}
                        onNewGeneration=${shot => newGeneration('shots', shot)}
                        onRemixGeneration=${(shot, gen) => remixGeneration('shots', shot, gen)}
                        onSetPrimary=${(shot, gen) => setPrimary('shots', shot, gen)}
                        onDeleteGeneration=${(shot, gen) => deleteGeneration('shots', shot, gen)}
                    />
                ` : html`
                    <div class="center-empty">
                        ${tab === 'storyboard' ? 'Select a scene to start storyboarding'
                            : schema ? schema.selectHint : 'Generation (coming soon)'}
                    </div>
                `}
            </div>
            <${Resizer} targetRef=${chatRef} side="right" />
            <${Chat} rootRef=${chatRef} messages=${messages} onSend=${sendChat} onApprovalClick=${handleApprovalClick} />
        </div>
        ${genModal ? html`
            <${GenerationModal}
                title=${(genModal.approval ? 'AI Proposal — Generation' : 'Generate') + ': ' + genModal.ownerName}
                initialPrompt=${genModal.initialPrompt}
                approval=${genModal.approval}
                onSubmit=${submitGeneration}
                onCancel=${cancelGeneration}
            />
        ` : null}
        ${bulkModal ? html`
            <${BulkShotsModal}
                initialText=${bulkModal.initialText}
                onApprove=${approveBulkShots}
                onReject=${rejectBulkShots}
            />
        ` : null}
    `;
}

render(html`<${App} />`, document.body);
