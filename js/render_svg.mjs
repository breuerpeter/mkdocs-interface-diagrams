// Render Excalidraw scenes to self-contained SVGs, run as a PERSISTENT worker.
//
// For each scene: render via the bundled exportToSvg under jsdom, then convert
// every <text> to vector <path> outlines (Nunito via fontkit) so the SVG
// renders identically everywhere — including when shown as an <img> (Obsidian,
// image viewers), where an embedded @font-face is NOT applied.
//
// The expensive part is one-time setup: importing the Excalidraw bundle under
// jsdom and loading the font (~1s). That's why this is a long-lived worker —
// generate.py spawns it once and streams many scenes through it rather than
// paying that cold-start per diagram.
//
// Protocol: line-delimited JSON. Request = {elements, appState?} per line on
// stdin. Response = {ok:true, svg:<string>} or {ok:false, error:<message>},
// one line per request. JSON.stringify escapes the SVG's newlines, so each
// message stays a single line and readline framing is unambiguous.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import readline from 'node:readline';
import { JSDOM } from 'jsdom';
import * as fontkit from 'fontkit';

const HERE = dirname(fileURLToPath(import.meta.url));
const FONTS_DIR = process.env.INTERFACE_DIAGRAMS_FONTS
    ?? join(HERE, '_fonts');

// --- jsdom + browser-global shims the Excalidraw bundle needs --------------
const dom = new JSDOM('<!DOCTYPE html><html><body></body></html>', {
    pretendToBeVisual: true, url: 'http://localhost/',
});
const { window } = dom;
for (const k of ['document', 'navigator', 'HTMLElement', 'Element', 'Node',
                 'DOMParser', 'location', 'getComputedStyle']) {
    if (window[k] !== undefined) globalThis[k] = window[k];
}
globalThis.window = window;
globalThis.self = window;
globalThis.top = window;
globalThis.devicePixelRatio = 1;
window.devicePixelRatio = 1;
globalThis.requestAnimationFrame = (cb) => setTimeout(() => cb(Date.now()), 0);
globalThis.cancelAnimationFrame = (id) => clearTimeout(id);
globalThis.matchMedia = window.matchMedia = () => ({
    matches: false, addListener() {}, removeListener() {},
    addEventListener() {}, removeEventListener() {},
});
globalThis.FontFace = class FontFace {
    constructor(family) { this.family = family; this.status = 'loaded'; }
    load() { return Promise.resolve(this); }
};
const fontset = new Set();
window.document.fonts = {
    add(f) { fontset.add(f); }, delete() {}, has() { return true; },
    forEach(cb) { fontset.forEach(cb); }, ready: Promise.resolve(),
    addEventListener() {}, removeEventListener() {},
    load() { return Promise.resolve([]); }, check() { return true; },
    values() { return fontset.values(); },
    [Symbol.iterator]() { return fontset[Symbol.iterator](); },
};
window.HTMLCanvasElement.prototype.getContext = () => ({
    measureText: (t) => ({ width: (t || '').length * 8 }),
    fillText() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() {}, fill() {},
    save() {}, restore() {}, setTransform() {}, scale() {}, translate() {}, rotate() {},
    arc() {}, closePath() {}, clearRect() {}, fillRect() {},
    createLinearGradient() { return { addColorStop() {} }; },
    getImageData() { return { data: [] }; }, putImageData() {}, drawImage() {},
});

// --- one-time heavy setup --------------------------------------------------
// Importing the Excalidraw bundle and loading the font is the slow part; do it
// once at worker startup, then reuse across every scene.
const { exportToSvg } = await import('@excalidraw/excalidraw');
const font = fontkit.create(
    readFileSync(join(FONTS_DIR, 'Nunito-Regular.woff2')));
const SVGNS = 'http://www.w3.org/2000/svg';
const fmt = (n) => +n.toFixed(4);

// Readability halo behind EVERY label so it stays legible where an edge line
// crosses it (e.g. an edge transiting a box). The halo colour is per-label,
// chosen by generate.py from what the label sits on (element.haloColor): "page"
// for labels over the page background, or a baked device/component box-fill hex
// for labels over a box. A box-fill halo inverts together with its box in dark
// mode, so it always matches; the page halo uses the `--diagram-halo` CSS
// variable (extra.css) — white in light mode, and the pre-image of the slate
// page background under the dark-mode invert filter in dark mode. The white
// fallback covers contexts with no page CSS (e.g. the SVG shown as a bare
// <img>).
const HALO_PAGE = 'var(--diagram-halo, #ffffff)';
const HALO_PAD_X = 3;       // box padding past the text, horizontal px
const HALO_PAD_Y = 1;       // box padding past the text, vertical px

async function renderScene(scene) {
    const svg = await exportToSvg({
        elements: scene.elements,
        appState: {
            exportBackground: false,
            viewBackgroundColor: 'transparent',
            exportWithDarkMode: false,
            ...(scene.appState || {}),
        },
        files: null,
    });

    // Per-text halo colour, in document order. exportToSvg emits one <text>
    // per text element in array order, so this aligns with the non-empty <text>
    // nodes below. "page" → the theme-aware CSS variable; a hex → a baked
    // box-fill colour; "none" → no halo.
    const haloQueue = scene.elements
        .filter(e => e.type === 'text' && (e.text || '').trim())
        .map(e => e.haloColor || 'page');
    let haloIdx = 0;

    // Outline every <text> into <path> glyphs so no font is needed at render
    // time. fontkit reads the woff2 directly and gives per-glyph outlines.
    for (const t of [...svg.querySelectorAll('text')]) {
        const str = t.textContent || '';
        if (!str.trim()) { t.remove(); continue; }
        const halo = haloQueue[haloIdx++] || 'page';
        const fs = parseFloat((t.getAttribute('font-size') || '16').replace('px', ''));
        const x = parseFloat(t.getAttribute('x') || '0');
        const y = parseFloat(t.getAttribute('y') || '0');
        const anchor = t.getAttribute('text-anchor') || 'start';
        const fill = t.getAttribute('fill') || '#1e1e1e';
        // Is this label wrapped in a link? If so we add a full-bbox hit area
        // (below) so the whole label is clickable, not just the glyph strokes.
        let linked = false;
        for (let p = t.parentNode; p; p = p.parentNode) {
            if (p.tagName && p.tagName.toLowerCase() === 'a') { linked = true; break; }
        }
        try {
            const scale = fs / font.unitsPerEm;
            const run = font.layout(str);
            const totalAdv = run.positions.reduce((s, p) => s + p.xAdvance, 0) * scale;
            let startX = x;
            if (anchor === 'middle') startX -= totalAdv / 2;
            else if (anchor === 'end') startX -= totalAdv;
            // Glyph outlines are y-up in font units; scale to font-size and
            // flip Y (scale -scale) so the baseline lands on the text's y.
            const g = window.document.createElementNS(SVGNS, 'g');
            g.setAttribute('fill', fill);
            if (linked) {
                // Invisible but clickable rectangle over the label's bounding
                // box (ascent..descent, +2px horizontal padding) so the link
                // hit area is the whole label, not the thin glyph geometry.
                const ascent = font.ascent * scale;
                const descent = font.descent * scale;   // negative
                const rect = window.document.createElementNS(SVGNS, 'rect');
                rect.setAttribute('x', fmt(startX - 2));
                rect.setAttribute('y', fmt(y - ascent));
                rect.setAttribute('width', fmt(totalAdv + 4));
                rect.setAttribute('height', fmt(ascent - descent));
                rect.setAttribute('fill', 'none');
                rect.setAttribute('pointer-events', 'all');
                g.appendChild(rect);
            }
            const glyphSpecs = [];
            let penEm = 0;
            run.glyphs.forEach((glyph, i) => {
                const pos = run.positions[i];
                const d = glyph.path && glyph.path.toSVG();
                if (d) {
                    const px = startX + (penEm + (pos.xOffset || 0)) * scale;
                    const py = y - (pos.yOffset || 0) * scale;
                    const transform =
                        `translate(${fmt(px)} ${fmt(py)}) scale(${fmt(scale)} ${fmt(-scale)})`;
                    glyphSpecs.push({ transform, d });
                    const p = window.document.createElementNS(SVGNS, 'path');
                    p.setAttribute('transform', transform);
                    p.setAttribute('d', d);
                    g.appendChild(p);
                }
                penEm += pos.xAdvance;
            });
            // Readability backing: a filled rectangle over the label's bounding
            // box in the halo colour, BEHIND the glyph fill. A glyph-outline
            // stroke would trace the inner contour of a counter (the hole in
            // 'o', 'e', 'a', 'g'…) and leave it transparent, so a crossing line
            // shows through; a solid box covers the whole footprint. The halo
            // colour matches the background, so the box is invisible except
            // where it masks a line. HALO_PX is the box's padding past the text.
            if (halo !== 'none' && glyphSpecs.length) {
                const ascent = font.ascent * scale;
                const descent = font.descent * scale;    // negative
                // Hug the text horizontally so the box doesn't reach a nearby
                // port arrowhead (a short intra-device edge seats the label
                // right next to the port); the vertical extent does the line
                // masking and stays generous.
                const padX = HALO_PAD_X, padY = HALO_PAD_Y;
                const bg = window.document.createElementNS(SVGNS, 'rect');
                bg.setAttribute('x', fmt(startX - padX));
                bg.setAttribute('y', fmt(y - ascent - padY));
                bg.setAttribute('width', fmt(totalAdv + 2 * padX));
                bg.setAttribute('height', fmt((ascent - descent) + 2 * padY));
                // "page" → the theme-aware CSS variable (var() only resolves in
                // CSS, hence `style`); otherwise a baked box-fill hex.
                bg.setAttribute('style', `fill: ${halo === 'page' ? HALO_PAGE : halo}`);
                t.replaceWith(bg, g);
            } else {
                t.replaceWith(g);
            }
        } catch (e) {
            // Leave the original <text> if shaping fails (better than dropping it).
            process.stderr.write(
                `render_svg: text->path failed for ${JSON.stringify(str)}: ${e.message}\n`);
        }
    }
    return svg.outerHTML;
}

// --- request loop ----------------------------------------------------------
// for-await serializes: each scene is fully rendered before the next line is
// pulled, so one worker renders one scene at a time (the pool gives us
// cross-process parallelism instead).
const rl = readline.createInterface({ input: process.stdin });
for await (const line of rl) {
    if (!line) continue;
    try {
        const svg = await renderScene(JSON.parse(line));
        process.stdout.write(JSON.stringify({ ok: true, svg }) + '\n');
    } catch (err) {
        process.stdout.write(JSON.stringify({
            ok: false, error: String(err && err.stack ? err.stack : err),
        }) + '\n');
    }
}
