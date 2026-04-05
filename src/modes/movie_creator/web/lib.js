// Shared imports, entity schemas, prompt defaults, WS factory.
export { h, render, Component } from 'https://esm.sh/preact@10.22.0';
export { useState, useEffect, useRef } from 'https://esm.sh/preact@10.22.0/hooks';
import { h } from 'https://esm.sh/preact@10.22.0';
import htm from 'https://esm.sh/htm@3.1.1';
export const html = htm.bind(h);

// Field schemas for EntityForm. Each view picks the right one.
export const SCENE_SCHEMA = {
    label: 'Scene',
    titleField: 'title',
    emptyTitle: 'Untitled',
    fields: [
        { name: 'title', label: 'Title', placeholder: 'Scene title' },
        { name: 'location', label: 'Location', placeholder: 'INT. APARTMENT - NIGHT' },
        { name: 'text', label: 'Scene text', type: 'textarea', grow: true, placeholder: 'Scene description and dialogue...' },
    ],
};

export const CHARACTER_SCHEMA = {
    label: 'Character',
    titleField: 'name',
    emptyTitle: 'Unnamed',
    fields: [
        { name: 'name', label: 'Name', placeholder: 'Character name' },
        { name: 'description', label: 'Description', type: 'textarea', grow: true, placeholder: 'Role, personality, motivation...' },
        { name: 'appearance', label: 'Appearance', type: 'textarea', grow: true, placeholder: 'Age, height, hair, clothing...' },
    ],
};

export const SHOT_SCHEMA = {
    label: 'Shot',
    titleField: 'description',
    emptyTitle: '(empty)',
    fields: [
        { name: 'description', label: 'Description', type: 'textarea', grow: true, placeholder: 'Framing, action, camera, dialogue...' },
    ],
};

// Used by ApprovalView to resolve approval.kind → schema.
export const APPROVAL_SCHEMAS = {
    scene: SCENE_SCHEMA,
    character: CHARACTER_SCHEMA,
    shot: SHOT_SCHEMA,
};

export function characterPrompt(char) {
    const appearance = char.appearance || 'a film character';
    return `Cinematic portrait of ${char.name || 'character'}. ${appearance}. Head and shoulders, cinematic lighting, film still, shallow depth of field.`;
}

export function shotPrompt(shot) {
    return `Cinematic film still. ${shot.description || ''}. Cinematic lighting, shallow depth of field.`;
}

let _ws = null;

export function createWS(onMessage, onStatus) {
    _ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws');
    _ws.onopen = () => onStatus(true);
    _ws.onclose = () => onStatus(false);
    _ws.onmessage = e => onMessage(JSON.parse(e.data));
}

export function send(msg) {
    if (_ws && _ws.readyState === 1) _ws.send(JSON.stringify(msg));
}
