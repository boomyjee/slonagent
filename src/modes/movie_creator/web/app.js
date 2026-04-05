// Movie Creator — Preact app entry.
//
// State model (intentionally flat):
//   project     — single source of truth, streamed from server on every change
//   tab         — current UI tab
//   selected    — { scenes: id|'__new__'|null, characters: id|'__new__'|null }
//                 controls what the center pane renders
//   approval    — AI proposal currently shown in center (overrides selected)
//                 { kind, data, idx }
//   promptModal — single-field prompt modal (generation or approval); owns nothing
//   bulkModal   — bulk shots proposal modal
//   messages    — chat/approval/tool log
//
// No resolveEditing(), no edits-merge. Forms own their own draft internally.
// Galleries read straight from live project state, so external updates
// (AI-produced generations) flow in without touching any form.
import { render, html, useState, useEffect, useRef, createWS } from './lib.js';
import { Resizer } from './components/Resizer.js';
import { SceneList } from './components/SceneList.js';
import { CharacterList } from './components/CharacterList.js';
import { SceneView } from './components/SceneView.js';
import { CharacterView } from './components/CharacterView.js';
import { StoryboardView } from './components/StoryboardView.js';
import { ApprovalView } from './components/ApprovalView.js';
import { PromptModal } from './components/PromptModal.js';
import { BulkShotsModal } from './components/BulkShotsModal.js';
import { Chat } from './components/Chat.js';

function App() {
    const [connected, setConnected] = useState(false);
    const [project, setProject] = useState({ title: '', scenes: {}, characters: {} });
    const [tab, setTab] = useState('screenplay');
    const [selected, setSelected] = useState({ scenes: null, characters: null });
    const [approval, setApproval] = useState(null);
    const [promptModal, setPromptModal] = useState(null);
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
            setMessages(prev => [...prev, {
                kind: 'approval',
                approvalKind: msg.kind,
                data: msg.data,
                resolved: false,
                idx: prev.length,
            }]);
        }
    }

    function markResolved(idx) {
        if (idx == null) return;
        setMessages(prev => prev.map((m, i) => i === idx ? { ...m, resolved: true } : m));
    }

    function askReason() { return prompt('Reason (optional):') || ''; }

    // ── selection ──

    function switchTab(newTab) {
        setTab(newTab);
        setApproval(null);
        send({ type: 'tab_changed', tab: newTab });
        if (newTab === 'storyboard') {
            send({ type: 'scope_changed', scope: { scene_id: selected.scenes || '' } });
        } else {
            send({ type: 'scope_changed', scope: {} });
        }
    }

    function selectEntity(collection, id) {
        setSelected(s => ({ ...s, [collection]: id }));
        if (tab === 'storyboard' && collection === 'scenes') {
            send({ type: 'scope_changed', scope: { scene_id: id } });
        }
    }

    function addNew(collection) {
        setSelected(s => ({ ...s, [collection]: '__new__' }));
    }

    function closeCenter(collection) {
        setSelected(s => ({ ...s, [collection]: null }));
    }

    // ── approvals ──

    function handleApprovalClick(msgItem) {
        if (msgItem.resolved) return;
        const kind = msgItem.approvalKind;

        if (kind === 'portrait') {
            setPromptModal({
                title: `Portrait: ${msgItem.data.character_name || ''}`,
                initial: msgItem.data.prompt || '',
                approval: true,
                onSubmit: prompt => {
                    send({
                        type: 'approval_response',
                        action: 'approve',
                        data: {
                            prompt,
                            character_id: msgItem.data.character_id,
                            character_name: msgItem.data.character_name,
                        },
                    });
                    markResolved(msgItem.idx);
                    setPromptModal(null);
                },
                onCancel: () => {
                    send({ type: 'approval_response', action: 'reject', reason: askReason() });
                    markResolved(msgItem.idx);
                    setPromptModal(null);
                },
            });
            return;
        }

        if (kind === 'shots_bulk') {
            setBulkModal({
                initialText: msgItem.data.text || '',
                onApprove: text => {
                    send({
                        type: 'approval_response',
                        action: 'approve',
                        data: { text, scene_id: msgItem.data.scene_id },
                    });
                    markResolved(msgItem.idx);
                    setBulkModal(null);
                },
                onReject: () => {
                    send({ type: 'approval_response', action: 'reject', reason: askReason() });
                    markResolved(msgItem.idx);
                    setBulkModal(null);
                },
            });
            return;
        }

        // scene / character / shot — entity form shown in center
        setApproval({ kind, data: msgItem.data.fields || msgItem.data, idx: msgItem.idx });
    }

    function approveEntity(data) {
        send({ type: 'approval_response', action: 'approve', data });
        if (approval) markResolved(approval.idx);
        setApproval(null);
    }

    function rejectEntity() {
        send({ type: 'approval_response', action: 'reject', reason: askReason() });
        if (approval) markResolved(approval.idx);
        setApproval(null);
    }

    function sendChat(text) { send({ type: 'chat', text }); }

    // ── render ──

    let sidebarView = null;
    if (tab === 'screenplay' || tab === 'storyboard') {
        sidebarView = html`<${SceneList}
            scenes=${project.scenes}
            selectedId=${selected.scenes}
            onSelect=${id => selectEntity('scenes', id)}
            onAdd=${tab === 'screenplay' ? () => addNew('scenes') : null}
        />`;
    } else if (tab === 'characters') {
        sidebarView = html`<${CharacterList}
            characters=${project.characters}
            selectedId=${selected.characters}
            onSelect=${id => selectEntity('characters', id)}
            onAdd=${() => addNew('characters')}
        />`;
    }

    let centerView;
    if (approval) {
        centerView = html`<${ApprovalView}
            key=${'approval-' + approval.idx}
            kind=${approval.kind}
            data=${approval.data}
            onApprove=${approveEntity}
            onReject=${rejectEntity}
        />`;
    } else if (tab === 'screenplay') {
        const sel = selected.scenes;
        if (sel === '__new__') {
            centerView = html`<${SceneView}
                key="new-scene"
                isNew=${true}
                send=${send}
                onClose=${() => closeCenter('scenes')}
            />`;
        } else if (sel && project.scenes[sel]) {
            centerView = html`<${SceneView}
                key=${'scene-' + sel}
                scene=${project.scenes[sel]}
                send=${send}
                onClose=${() => closeCenter('scenes')}
            />`;
        } else {
            centerView = html`<div class="center-empty">Select a scene or create a new one</div>`;
        }
    } else if (tab === 'characters') {
        const sel = selected.characters;
        if (sel === '__new__') {
            centerView = html`<${CharacterView}
                key="new-char"
                isNew=${true}
                send=${send}
                onClose=${() => closeCenter('characters')}
                openPromptModal=${setPromptModal}
            />`;
        } else if (sel && project.characters[sel]) {
            centerView = html`<${CharacterView}
                key=${'char-' + sel}
                character=${project.characters[sel]}
                send=${send}
                onClose=${() => closeCenter('characters')}
                openPromptModal=${setPromptModal}
            />`;
        } else {
            centerView = html`<div class="center-empty">Select a character or create a new one</div>`;
        }
    } else if (tab === 'storyboard') {
        const sel = selected.scenes;
        if (sel && project.scenes[sel]) {
            centerView = html`<${StoryboardView}
                key=${'sb-' + sel}
                scene=${project.scenes[sel]}
                send=${send}
                openPromptModal=${setPromptModal}
            />`;
        } else {
            centerView = html`<div class="center-empty">Select a scene to start storyboarding</div>`;
        }
    } else {
        centerView = html`<div class="center-empty">Generation (coming soon)</div>`;
    }

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
            <div class="sidebar" ref=${sidebarRef}>${sidebarView}</div>
            <${Resizer} targetRef=${sidebarRef} side="left" />
            <div class="center">${centerView}</div>
            <${Resizer} targetRef=${chatRef} side="right" />
            <${Chat} rootRef=${chatRef} messages=${messages} onSend=${sendChat} onApprovalClick=${handleApprovalClick} />
        </div>
        ${promptModal ? html`<${PromptModal}
            title=${promptModal.title}
            initial=${promptModal.initial}
            approval=${promptModal.approval}
            onSubmit=${promptModal.onSubmit}
            onCancel=${promptModal.onCancel}
        />` : null}
        ${bulkModal ? html`<${BulkShotsModal}
            initialText=${bulkModal.initialText}
            onApprove=${bulkModal.onApprove}
            onReject=${bulkModal.onReject}
        />` : null}
    `;
}

render(html`<${App} />`, document.body);
