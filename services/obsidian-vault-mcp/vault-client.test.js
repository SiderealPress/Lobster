'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('http');

const { createVaultClient, VaultApiTimeoutError, VaultApiError } = require('./vault-client');

/** Starts a bare HTTP server whose handler is fully controlled by the test. */
function startServer(handler) {
  const server = http.createServer(handler);
  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => resolve(server));
  });
}

test('get() returns parsed JSON on a normal, fast response (baseline — no regression on the happy path)', async (t) => {
  const server = await startServer((req, res) => {
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ ok: true }));
  });
  t.after(() => server.close());

  const client = createVaultClient({ baseUrl: `http://127.0.0.1:${server.address().port}`, token: 't', timeoutMs: 5000 });
  const result = await client.get('/anything');
  assert.deepEqual(result, { ok: true });
});

test('non-2xx responses are surfaced as VaultApiError carrying the server message and status', async (t) => {
  const server = await startServer((req, res) => {
    res.writeHead(404, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ error: 'File not found' }));
  });
  t.after(() => server.close());

  const client = createVaultClient({ baseUrl: `http://127.0.0.1:${server.address().port}`, token: 't', timeoutMs: 5000 });
  await assert.rejects(() => client.get('/file', { path: 'missing.md' }), (err) => {
    assert.ok(err instanceof VaultApiError);
    assert.equal(err.status, 404);
    assert.equal(err.message, 'File not found');
    return true;
  });
});

// --- The key regression test for #2119/#2219 -------------------------------
//
// This is the scenario that used to hang obsidian-mcp indefinitely: the
// server-side operation never responds. Before this fix, the equivalent
// stdio call had NO timeout at all and would hang until an unrelated
// ~2-hour session-age restart killed the whole session (see PR description
// and the manual repro against the real obsidian-mcp binary).
//
// Here we point the client at a server that accepts the TCP connection but
// never writes a response, with a short client-side timeout, and assert
// that the call fails with VaultApiTimeoutError close to the configured
// timeout — not after the test's own hard deadline, and never "forever".
test('a hung vault-api response is bounded by the client-side timeout, not left to hang indefinitely', async (t) => {
  const server = await startServer((req, res) => {
    // Deliberately never call res.end() or res.writeHead() — simulates the
    // exact "process alive but never responds" failure mode observed in
    // the original obsidian-mcp incidents.
  });
  t.after(() => server.close());

  const TIMEOUT_MS = 200;
  const client = createVaultClient({ baseUrl: `http://127.0.0.1:${server.address().port}`, token: 't', timeoutMs: TIMEOUT_MS });

  const start = Date.now();
  await assert.rejects(() => client.get('/list-available-vaults'), (err) => {
    assert.ok(err instanceof VaultApiTimeoutError, `expected VaultApiTimeoutError, got ${err.constructor.name}: ${err.message}`);
    return true;
  });
  const elapsed = Date.now() - start;

  // Bounded means "close to the configured timeout", not "eventually, on
  // some unrelated multi-hour schedule". Generous upper bound to avoid CI
  // flakiness while still failing outright if the timeout wiring is removed
  // (in which case this call would still be pending when the test process
  // exits, or the test runner's own default timeout — tens of seconds —
  // would fire instead of this assertion).
  assert.ok(elapsed < TIMEOUT_MS + 2000, `expected timeout close to ${TIMEOUT_MS}ms, took ${elapsed}ms`);
});

test('network errors (connection refused) are surfaced immediately as VaultApiError, not a timeout', async () => {
  const client = createVaultClient({ baseUrl: 'http://127.0.0.1:1', token: 't', timeoutMs: 5000 });
  const start = Date.now();
  await assert.rejects(() => client.get('/vaults'), (err) => {
    assert.ok(err instanceof VaultApiError);
    assert.notEqual(err.code, 'TIMEOUT');
    return true;
  });
  assert.ok(Date.now() - start < 5000, 'connection-refused should fail fast, well under the configured timeout');
});

test('post() sends a JSON body and content-type header', async (t) => {
  let received;
  const server = await startServer((req, res) => {
    let raw = '';
    req.on('data', (c) => (raw += c));
    req.on('end', () => {
      received = { contentType: req.headers['content-type'], body: JSON.parse(raw) };
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ success: true }));
    });
  });
  t.after(() => server.close());

  const client = createVaultClient({ baseUrl: `http://127.0.0.1:${server.address().port}`, token: 't', timeoutMs: 5000 });
  await client.post('/file', { path: 'a.md', content: 'hi' });
  assert.equal(received.contentType, 'application/json');
  assert.deepEqual(received.body, { path: 'a.md', content: 'hi' });
});
