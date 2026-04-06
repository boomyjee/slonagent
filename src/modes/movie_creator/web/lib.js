// Preact + htm re-exports.
export { h, render, Component, createContext } from 'https://esm.sh/preact@10.22.0';
export { useState, useEffect, useRef, useContext } from 'https://esm.sh/preact@10.22.0/hooks';
import { h } from 'https://esm.sh/preact@10.22.0';
import htm from 'https://esm.sh/htm@3.1.1';
export const html = htm.bind(h);