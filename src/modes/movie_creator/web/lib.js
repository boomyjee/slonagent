// Shared imports and schemas
export { h, render } from 'https://esm.sh/preact@10.22.0';
export { useState, useEffect, useRef } from 'https://esm.sh/preact@10.22.0/hooks';
import { h } from 'https://esm.sh/preact@10.22.0';
import htm from 'https://esm.sh/htm@3.1.1';
export const html = htm.bind(h);

export const SCHEMAS = {
    scenes: {
        label: 'Scene',
        plural: 'Scenes',
        titleField: 'title',
        emptyTitle: 'Untitled',
        numbered: true,
        emptyText: 'No scenes yet',
        selectHint: 'Select a scene or create a new one',
        fields: [
            { name: 'title', label: 'Title', placeholder: 'Scene title' },
            { name: 'location', label: 'Location', placeholder: 'INT. APARTMENT - NIGHT' },
            { name: 'text', label: 'Scene text', type: 'textarea', grow: true, placeholder: 'Scene description and dialogue...' },
        ],
    },
    characters: {
        label: 'Character',
        plural: 'Characters',
        titleField: 'name',
        emptyTitle: 'Unnamed',
        thumb: 'image',
        portrait: true,
        emptyText: 'No characters yet',
        selectHint: 'Select a character or create a new one',
        fields: [
            { name: 'name', label: 'Name', placeholder: 'Character name' },
            { name: 'description', label: 'Description', type: 'textarea', grow: true, placeholder: 'Role, personality, motivation...' },
            { name: 'appearance', label: 'Appearance', type: 'textarea', grow: true, placeholder: 'Age, height, hair, clothing...' },
        ],
    },
};

export const TAB_COLLECTION = { screenplay: 'scenes', characters: 'characters' };

export function defaultPortraitPrompt(char) {
    const appearance = char.appearance || 'a film character';
    return `Cinematic portrait of ${char.name || 'character'}. ${appearance}. Head and shoulders, cinematic lighting, film still, shallow depth of field.`;
}

export function createWS(onMessage, onStatus) {
    const ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws');
    ws.onopen = () => onStatus(true);
    ws.onclose = () => onStatus(false);
    ws.onmessage = e => onMessage(JSON.parse(e.data));
    return { send: msg => ws.readyState === 1 && ws.send(JSON.stringify(msg)) };
}
