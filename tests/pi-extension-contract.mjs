// Run with: node tests/pi-extension-contract.mjs /absolute/pi-package-directory
// Uses the installed Pi loader and TypeBox; the Hippo binary is an isolated fixture.
import { mkdtempSync, writeFileSync, chmodSync, rmSync, realpathSync, existsSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import assert from 'node:assert/strict';

if (!process.argv[2]) throw new Error('Pass the installed Pi package directory');
const runtime = resolve(process.argv[2]);
const { createJiti } = await import(pathToFileURL(join(runtime, 'node_modules/jiti/lib/jiti.mjs')));
const base = realpathSync(mkdtempSync(join(tmpdir(), 'hippo-pi-contract-')));
try {
  const bin = join(base, 'hippo-fixture');
  const record = join(base, 'capture.json');
  writeFileSync(bin, `#!/usr/bin/env python3
import sys,os,json
args=sys.argv[1:]
result={"args":args,"cwd":os.getcwd()}
if args[:2]==['hook','capture']:
    result['payload']=json.load(sys.stdin)
    with open(os.environ['HIPPO_CONTRACT_RECORD'],'w') as out:
        json.dump(result,out)
else:
    print(json.dumps(result))
`);
  chmodSync(bin, 0o755);
  process.env.HIPPO_BIN = bin;
  process.env.HIPPO_CONTRACT_RECORD = record;
  const jiti = createJiti(import.meta.url, {
    moduleCache: false, fsCache: false,
    alias: { typebox: join(runtime, 'node_modules/typebox/build/index.mjs') },
  });
  const extension = await jiti.import(new URL('../integrations/pi/hippo-memory.ts', import.meta.url).pathname, { default: true });
  for (const queueVersion of ['1', '2']) {
    process.env.HIPPO_QUEUE_VERSION = queueVersion;
    const tools = new Map();
    const handlers = new Map();
    extension({ registerTool: t => tools.set(t.name, t), registerCommand() {}, on: (name, fn) => handlers.set(name, fn) });
    for (const [name, params, action] of [
      ['search_memories', { query: 'fixture' }, 'search'], ['add_memory', { text: 'fixture' }, 'add'],
    ]) {
      const result = await tools.get(name).execute('id', params, undefined, undefined, { cwd: base });
      assert.equal(result.isError, undefined, result.content[0].text);
      const observed = JSON.parse(result.content[0].text);
      assert.equal(observed.cwd, base);
      assert.deepEqual(observed.args.slice(0, 2), ['agent', action]);
      assert.deepEqual(observed.args.slice(-2), ['--context-dir', base]);
      const missing = await tools.get(name).execute('id', params, undefined, undefined, undefined);
      assert.equal(missing.isError, true);
      const global = await tools.get(name).execute('id', { ...params, scope: 'global' }, undefined, undefined, undefined);
      assert.equal(global.isError, undefined, global.content[0].text);
    }
    for (const event of ['agent_settled', 'session_shutdown']) {
      rmSync(record, { force: true });
      handlers.get(event)({}, { cwd: base, sessionManager: { getSessionId: () => 'fixture-session' } });
      let captured;
      for (let attempt = 0; attempt < 100; attempt++) {
        try { if (existsSync(record)) captured = JSON.parse(readFileSync(record, 'utf8')); } catch {}
        if (captured) break;
        await new Promise(resolve => setTimeout(resolve, 20));
      }
      assert.ok(captured, 'Capture subprocess did not finish');
      assert.deepEqual(captured.args, ['hook', 'capture', '--host', 'pi', '--queue-version', queueVersion]);
      assert.equal(captured.payload.project_dir, base);
      assert.equal(captured.payload.event, event);
    }
  }
  process.env.HIPPO_QUEUE_VERSION = '3';
  assert.throws(() => extension({}), /HIPPO_QUEUE_VERSION/);
  console.log('Pi contract passed: ctx.cwd, safe tools, missing context, global, both events, v1/v2 capture.');
} finally {
  rmSync(base, { recursive: true, force: true });
}
