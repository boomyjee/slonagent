// Custom resolution hook so dashboard components can find lib.js the way
// they do in the browser. WebTransportServer walks _ui_dirs on each
// request: subclass first, web/ui/ as fallback. Files like lib.js,
// Tabs.js, Resizer.js live ONLY in web/ui/ — when dashboard's app.js
// or its components import them via './lib.js' or './common/Foo.js',
// the browser silently falls back. Node has no fallback, so we mirror
// the rule here for tests.
//
// Wired in by _dom.mjs via module.register(). Runs before any dashboard
// import resolves.

import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

export async function resolve(specifier, context, nextResolve) {
    const parent = context.parentURL;
    if (!parent || !parent.includes('/transport/dashboard/ui/')) {
        return nextResolve(specifier, context);
    }
    if (!specifier.startsWith('./') && !specifier.startsWith('../')) {
        return nextResolve(specifier, context);
    }
    // Try the natural resolution first; if the file is missing, redirect
    // into web/ui/ at the same relative position. The dashboard tree
    // lives at .../transport/dashboard/ui/<sub> and the shared tree at
    // .../transport/web/ui/<sub>, so swapping that segment is enough.
    try {
        const naturalUrl = new URL(specifier, parent);
        if (existsSync(fileURLToPath(naturalUrl))) {
            return nextResolve(specifier, context);
        }
    } catch {
        return nextResolve(specifier, context);
    }
    const fallback = new URL(specifier, parent.replace('/transport/dashboard/ui/', '/transport/web/ui/'));
    return nextResolve(fallback.href, context);
}
