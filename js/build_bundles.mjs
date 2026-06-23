// Build the two self-contained Node workers into ../interface_diagrams/_js.
// Excalidraw's exportToSvg is bundled browser-style (React/roughjs inlined);
// jsdom + fontkit are bundled platform:node; canvas (jsdom's optional native
// dep) is marked external so esbuild doesn't try to bundle a native module.
import { build } from 'esbuild';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const out = path.join(here, '..', 'interface_diagrams', '_js');

await build({
  entryPoints: [path.join(here, 'elk_layout.mjs')],
  outfile: path.join(out, 'elk_layout.bundle.mjs'),
  bundle: true, format: 'esm', platform: 'node', logLevel: 'info',
  // elkjs's main.js has a conditional require('web-worker') for browser
  // environments; on Node it falls back to native workers, so this path is
  // never reached at runtime. Mark it external to suppress the bundle error.
  external: ['web-worker'],
});

// jsdom and its deps are CommonJS modules that use require() to load Node
// built-ins (path, fs, etc.).  When the bundle format is ESM, there is no
// require() in scope, which causes a "Dynamic require of X is not supported"
// crash at startup.  The standard fix is to inject a createRequire-based shim
// via a banner so that CJS require calls resolve correctly inside the ESM
// bundle.
await build({
  entryPoints: [path.join(here, 'render_svg.mjs')],
  outfile: path.join(out, 'render_svg.bundle.mjs'),
  bundle: true, format: 'esm', platform: 'node',
  external: ['canvas'],
  loader: { '.css': 'empty', '.woff2': 'empty', '.ttf': 'empty', '.svg': 'text' },
  mainFields: ['module', 'main'],
  conditions: ['production'],
  banner: {
    // jsdom and its CJS deps need require() to resolve Node built-ins.
    // We also wrap require.resolve so that jsdom's XHR sync-worker check
    // (require.resolve('./xhr-sync-worker.js')) fails gracefully rather than
    // crashing at startup — synchronous XHR is never used by exportToSvg.
    js: `import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const _origResolve = require.resolve.bind(require);
require.resolve = (id, opts) => { try { return _origResolve(id, opts); } catch { return null; } };`,
  },
  logLevel: 'info',
});

console.log('built _js/elk_layout.bundle.mjs and _js/render_svg.bundle.mjs');
