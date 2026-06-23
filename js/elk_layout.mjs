// elkjs wrapper, run as a PERSISTENT worker. elkjs (a GWT-transpiled Java
// blob) costs ~0.2s to import, so generate.py spawns this once and streams many
// requests through it instead of paying that cold-start per diagram.
//
// Protocol: line-delimited JSON, one request per line on stdin, one response
// line per request on stdout. Request = an ELK graph spec. Response =
// {ok:true, result:<laid-out graph>} or {ok:false, error:<message>}.
// JSON.stringify escapes embedded newlines, so each message is exactly one line
// and readline framing is unambiguous in both directions.

import ELK from 'elkjs';
import readline from 'node:readline';

const elk = new ELK();
const rl = readline.createInterface({ input: process.stdin });

// for-await serializes: the loop body is awaited before the next line is
// pulled, so this worker handles one layout at a time (the pool gives us
// cross-process parallelism instead).
for await (const line of rl) {
    if (!line) continue;
    try {
        const laid = await elk.layout(JSON.parse(line));
        process.stdout.write(JSON.stringify({ ok: true, result: laid }) + '\n');
    } catch (err) {
        // ELK's raw stack traces dump the minified GWT source; keep just the
        // message so a failure is actionable.
        process.stdout.write(
            JSON.stringify({ ok: false, error: String(err.message || err) }) + '\n');
    }
}
