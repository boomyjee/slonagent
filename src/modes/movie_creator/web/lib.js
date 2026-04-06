// Shared imports, prompt defaults, WS factory.
export { h, render, Component, createContext } from 'https://esm.sh/preact@10.22.0';
export { useState, useEffect, useRef, useContext } from 'https://esm.sh/preact@10.22.0/hooks';
import { h } from 'https://esm.sh/preact@10.22.0';
import htm from 'https://esm.sh/htm@3.1.1';
export const html = htm.bind(h);

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
