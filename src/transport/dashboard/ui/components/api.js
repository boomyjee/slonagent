import { persist } from '../lib.js';

const BASE = new URL('./', location.href).href;
const ROOT_KEY = 'dashboard.root';

export const currentRoot = () => persist.get(ROOT_KEY, '');
export const setCurrentRoot = (root) => persist.set(ROOT_KEY, root || '');

// Stateless server: every file/git request carries the current root.
// For GETs/DELETEs we append `?root=`; for body requests (POST/PUT/PATCH)
// we splice it into the JSON body. Components don't need to know.
export function api(path, opts) {
    const root = currentRoot();
    if (root && opts?.body) {
        try {
            const body = JSON.parse(opts.body);
            if (body.root === undefined) body.root = root;
            opts = { ...opts, body: JSON.stringify(body) };
        } catch {
            // Non-JSON body — leave alone.
        }
    } else if (root && (!opts || !opts.body)) {
        const sep = path.includes('?') ? '&' : '?';
        path = `${path}${sep}root=${encodeURIComponent(root)}`;
    }
    return fetch(BASE + path, opts).then(r => r.json());
}
