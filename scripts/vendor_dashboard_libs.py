"""Inline preact + preact/hooks + htm into src/transport/dashboard/web/lib.js.

lib.js used to `import 'https://esm.sh/preact@...'` — that works in the
web_agent iframe (tunnel origin, no CSP limits) but would break inside an
extension-page with MV3 CSP `script-src 'self'`. By inlining the three
libraries into lib.js itself, any consumer (dashboard page, bookmarklet,
extension sidepanel) gets Preact for free, no network fetches.

Run after bumping the *_VERSION constants below.
"""
import re
import urllib.request
from pathlib import Path

PREACT_VERSION = "10.22.0"
HTM_VERSION = "3.1.1"

PREACT_URL = f"https://esm.sh/preact@{PREACT_VERSION}/es2022/preact.mjs"
HOOKS_URL = f"https://esm.sh/preact@{PREACT_VERSION}/es2022/hooks.mjs"
HTM_URL = f"https://esm.sh/htm@{HTM_VERSION}/es2022/htm.mjs"

LIB_JS = Path(__file__).resolve().parents[1] / "src" / "transport" / "dashboard" / "web" / "lib.js"


def fetch(url: str) -> str:
    print(f"  fetch {url}")
    with urllib.request.urlopen(url) as r:
        return r.read().decode("utf-8")


def strip_sourcemap(s: str) -> str:
    return re.sub(r"\n?//# sourceMappingURL=.*$", "", s, flags=re.MULTILINE).rstrip()


def split_export(body: str) -> tuple[str, list[tuple[str, str]]]:
    """Peel the trailing `export{A as B,...};` off and return (body_before, pairs)."""
    m = re.search(r"export\{([^}]*)\};?\s*$", body)
    if not m:
        raise SystemExit("export statement not found in module body")
    pairs = []
    for item in m.group(1).split(","):
        parts = [p.strip() for p in item.split(" as ")]
        if len(parts) == 1:
            pairs.append((parts[0], parts[0]))
        else:
            pairs.append((parts[0], parts[1]))
    return body[: m.start()].rstrip(), pairs


def main():
    preact_raw = strip_sourcemap(fetch(PREACT_URL))
    hooks_raw = strip_sourcemap(fetch(HOOKS_URL))
    htm_raw = strip_sourcemap(fetch(HTM_URL))

    # preact: no imports, just trailing export.
    preact_body, preact_pairs = split_export(preact_raw)
    preact_ret = "return{" + ",".join(f"{to}:{fr}" for fr, to in preact_pairs) + "};"

    # hooks: `import{options as B}from"./preact.mjs";` at top — drop it,
    # B is passed in as the IIFE parameter.
    hooks_raw = re.sub(r'import\{[^}]*\}from"\./preact\.mjs";', "", hooks_raw, count=1)
    hooks_body, hooks_pairs = split_export(hooks_raw)
    hooks_ret = "return{" + ",".join(f"{to}:{fr}" for fr, to in hooks_pairs) + "};"

    # htm: trailing `export{b as default};` — return the default symbol.
    htm_body, htm_pairs = split_export(htm_raw)
    default_sym = next(fr for fr, to in htm_pairs if to == "default")
    htm_ret = f"return {default_sym};"

    preact_exports = ", ".join(to for _, to in preact_pairs)
    hooks_exports = ", ".join(to for _, to in hooks_pairs)

    out = (
        "/**\n"
        " * Dashboard client library — Preact + hooks + htm + bau-css, all inlined.\n"
        " *\n"
        " * Inlined so any consumer (dashboard page, bookmarklet, extension sidepanel)\n"
        " * gets Preact without a network fetch. Regenerate via\n"
        " * scripts/vendor_dashboard_libs.py after bumping library versions.\n"
        f" * Versions: preact@{PREACT_VERSION}, htm@{HTM_VERSION}.\n"
        " */\n"
        "\n"
        "const __preact = (() => {\n"
        f"{preact_body}\n"
        f"{preact_ret}\n"
        "})();\n"
        "\n"
        "const __hooks = ((B) => {\n"
        f"{hooks_body}\n"
        f"{hooks_ret}\n"
        "})(__preact.options);\n"
        "\n"
        "const __htm = (() => {\n"
        f"{htm_body}\n"
        f"{htm_ret}\n"
        "})();\n"
        "\n"
        f"export const {{ {preact_exports} }} = __preact;\n"
        f"export const {{ {hooks_exports} }} = __hooks;\n"
        "export const html = __htm.bind(h);\n"
        "\n"
        "// bau-css (inline, ~35 lines unminified)\n"
        "var d=n=>{let a=0,t=11;for(;a<n.length;)t=101*t+n.charCodeAt(a++)>>>0;return\"bau\"+t},"
        "r=(n,a,t,o)=>{let e=n.createElement(\"style\");e.id=t,e.append(o),(a??n.head).append(e)},"
        "m=(n,a)=>n.reduce((t,o,e)=>t+o+(a[e]??\"\"),\"\");\n"
        "function bauCss(n){let{document:a}=n?.window??window,t=o=>(e,...l)=>"
        "{let c=m(e,l),s=d(c);return!a.getElementById(s)&&r(a,n?.target,s,o(s,c)),s};"
        "return{css:t((o,e)=>`.${o} { ${e} }`),keyframes:t((o,e)=>`@keyframes ${o} { ${e} }`),"
        "createGlobalStyles:t((o,e)=>e)}}\n"
        "// Mutable style host - default is document.head; web_agent sets this\n"
        "// to a Shadow DOM root before importing components to isolate from\n"
        "// host-page CSS.\n"
        "export const stylesHost = {};\n"
        "export const { css, createGlobalStyles } = bauCss(stylesHost);\n"
    )
    LIB_JS.write_text(out, encoding="utf-8")
    print(f"wrote {LIB_JS} ({len(out)} bytes)")


if __name__ == "__main__":
    main()
