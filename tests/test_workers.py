"""The node worker bridge: the line-delimited JSON protocol (_Worker /
WorkerPool) against dependency-free stub node scripts, and the run_elk /
render_svg response handling against fake pools.

The stubs need only bare `node` (no node_modules), so these run anywhere node
is installed; they're skipped where it isn't. The real elk_layout.mjs /
render_svg.mjs request loops use the same one-line-in/one-line-out contract
the stubs implement."""

import shutil
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from interface_diagrams import generate
from interface_diagrams.generate import WorkerPool, _Worker

from .helpers import SCRIPTS  # noqa: F401

HAVE_NODE = shutil.which("node") is not None

# Echoes {ok:true, result:{echo:<request>, n:<request count>}} per line.
# ESM with a for-await readline loop — the same shape as the real workers.
ECHO_WORKER = """\
import readline from 'node:readline';
const rl = readline.createInterface({ input: process.stdin });
let n = 0;
for await (const line of rl) {
    if (!line) continue;
    n += 1;
    process.stdout.write(JSON.stringify(
        { ok: true, result: { echo: JSON.parse(line), n } }) + '\\n');
}
"""

# Dies after answering its first request — exercises EOF detection.
DIES_AFTER_ONE = """\
import readline from 'node:readline';
const rl = readline.createInterface({ input: process.stdin });
for await (const line of rl) {
    process.stdout.write(JSON.stringify({ ok: true, result: 1 }) + '\\n');
    process.exit(0);
}
"""


def _script(tmpdir: str, source: str) -> Path:
    p = Path(tmpdir) / "worker.mjs"
    p.write_text(source, encoding="utf-8")
    return p


@unittest.skipUnless(HAVE_NODE, "node not installed")
class WorkerProtocol(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)

    def test_round_trip_preserves_payload(self):
        w = _Worker(_script(self.td.name, ECHO_WORKER))
        self.addCleanup(w.close)
        resp = w.call({"a": [1, 2], "s": "x"})
        self.assertEqual(resp["result"]["echo"], {"a": [1, 2], "s": "x"})

    def test_one_process_serves_many_requests_in_order(self):
        w = _Worker(_script(self.td.name, ECHO_WORKER))
        self.addCleanup(w.close)
        ns = [w.call({"i": i})["result"]["n"] for i in range(5)]
        self.assertEqual(ns, [1, 2, 3, 4, 5])  # same process, not respawned

    def test_newlines_in_payload_survive_line_framing(self):
        w = _Worker(_script(self.td.name, ECHO_WORKER))
        self.addCleanup(w.close)
        tricky = {"svg": "<svg>\n  <text>multi\nline</text>\n</svg>"}
        self.assertEqual(w.call(tricky)["result"]["echo"], tricky)

    def test_dead_worker_raises_instead_of_hanging(self):
        w = _Worker(_script(self.td.name, DIES_AFTER_ONE))
        self.addCleanup(w.close)
        self.assertTrue(w.call({})["ok"])
        with self.assertRaisesRegex(RuntimeError, "exited unexpectedly"):
            w.call({})


@unittest.skipUnless(HAVE_NODE, "node not installed")
class PoolBehavior(unittest.TestCase):
    def test_concurrent_callers_each_get_their_own_answer(self):
        with tempfile.TemporaryDirectory() as td:
            pool = WorkerPool(_script(td, ECHO_WORKER), size=2)
            try:
                with ThreadPoolExecutor(max_workers=8) as ex:
                    results = list(ex.map(lambda i: pool.call({"i": i})["result"]["echo"]["i"], range(32)))
                self.assertEqual(results, list(range(32)))
            finally:
                pool.close()

    def test_close_terminates_all_workers(self):
        with tempfile.TemporaryDirectory() as td:
            pool = WorkerPool(_script(td, ECHO_WORKER), size=2)
            pool.call({})
            pool.close()
            for w in pool._workers:
                self.assertIsNotNone(w.proc.poll())


class RequireRenderToolchain(unittest.TestCase):
    """The render path's fail-fast: a missing node toolchain must raise SystemExit.
    --check never calls this — it needs no node at all."""

    def test_missing_node_names_node(self):
        from unittest.mock import patch

        with patch.dict("os.environ", {"INTERFACE_DIAGRAMS_NODE": ""}, clear=False):
            with patch("shutil.which", return_value=None):
                with self.assertRaisesRegex(SystemExit, "node"):
                    generate.require_render_toolchain()

    def test_missing_bundles_names_bundles(self):
        from pathlib import Path
        from unittest.mock import patch

        nonexistent = Path("/nonexistent/elk_layout.bundle.mjs")
        with patch("interface_diagrams.generate._workers.resolve_node", return_value="/fake/node"):
            with patch("interface_diagrams.generate._workers.check_node", return_value=None):
                with patch("interface_diagrams.generate._workers.bundle_path", return_value=nonexistent):
                    with self.assertRaisesRegex(SystemExit, "bundle"):
                        generate.require_render_toolchain()

    @unittest.skipUnless(HAVE_NODE, "node not installed")
    def test_ready_toolchain_passes(self):
        generate.require_render_toolchain()  # must not raise


class _FakePool:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def call(self, payload):
        self.requests.append(payload)
        return self.response


class ResponseHandling(unittest.TestCase):
    """run_elk / render_svg unwrap {ok,...} envelopes and surface errors."""

    def setUp(self):
        self._elk, self._render = generate._ELK_POOL, generate._RENDER_POOL
        self.addCleanup(self._restore)

    def _restore(self):
        generate._ELK_POOL, generate._RENDER_POOL = self._elk, self._render

    def test_run_elk_returns_result_on_ok(self):
        generate._ELK_POOL = _FakePool({"ok": True, "result": {"id": "root"}})
        self.assertEqual(generate.run_elk({"id": "root"}), {"id": "root"})
        self.assertEqual(generate._ELK_POOL.requests, [{"id": "root"}])

    def test_run_elk_raises_on_error(self):
        generate._ELK_POOL = _FakePool({"ok": False, "error": "boom"})
        import contextlib
        import io

        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                generate.run_elk({})

    def test_render_svg_returns_svg_on_ok(self):
        generate._RENDER_POOL = _FakePool({"ok": True, "svg": "<svg/>"})
        self.assertEqual(generate.render_svg([]), "<svg/>")
        self.assertEqual(generate._RENDER_POOL.requests, [{"elements": []}])

    def test_render_svg_raises_with_worker_error_message(self):
        generate._RENDER_POOL = _FakePool({"ok": False, "error": "shaping failed"})
        with self.assertRaisesRegex(RuntimeError, "shaping failed"):
            generate.render_svg([])


if __name__ == "__main__":
    unittest.main()
