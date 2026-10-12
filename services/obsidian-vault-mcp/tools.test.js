'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const { buildToolDefinitions } = require('./tools');

const VAULT = 'eloso-obsidian-vault';

function fakeClient(overrides = {}) {
  const calls = [];
  const client = {
    get: async (path, query) => { calls.push(['GET', path, query]); return overrides.get ? overrides.get(path, query) : {}; },
    post: async (path, body) => { calls.push(['POST', path, body]); return overrides.post ? overrides.post(path, body) : {}; },
    del: async (path, query) => { calls.push(['DELETE', path, query]); return overrides.del ? overrides.del(path, query) : {}; }
  };
  return { client, calls };
}

function findTool(tools, name) {
  const t = tools.find((x) => x.name === name);
  assert.ok(t, `tool ${name} not found`);
  return t;
}

test('every tool rejects a vault name other than the one vault-api actually serves', async () => {
  const { client } = fakeClient();
  const tools = buildToolDefinitions({ client, getVaultName: () => VAULT });
  for (const tool of tools) {
    if (!('vault' in tool.inputSchema)) continue;
    await assert.rejects(
      () => tool.handler({ vault: 'some-other-vault', filename: 'x.md', path: 'x.md', files: ['x.md'], tags: ['t'], oldTag: 'a', newTag: 'b', source: 'a.md', destination: 'b.md', query: 'q', operation: 'delete' }),
      /Unknown vault/,
      `${tool.name} should reject a mismatched vault name`
    );
  }
});

test('read-note joins folder+filename and ensures .md extension', async () => {
  const { client, calls } = fakeClient({ get: () => ({ content: 'hello' }) });
  const tool = findTool(buildToolDefinitions({ client, getVaultName: () => VAULT }), 'read-note');
  const result = await tool.handler({ vault: VAULT, filename: 'note', folder: 'journal' });
  assert.deepEqual(calls[0], ['GET', '/file', { path: 'journal/note.md' }]);
  assert.equal(result.content[0].text, 'hello');
});

test('create-note passes mode=create so vault-api rejects an existing file', async () => {
  const { client, calls } = fakeClient();
  const tool = findTool(buildToolDefinitions({ client, getVaultName: () => VAULT }), 'create-note');
  await tool.handler({ vault: VAULT, filename: 'note.md', content: 'body' });
  assert.deepEqual(calls[0], ['POST', '/file', { path: 'note.md', content: 'body', mode: 'create' }]);
});

test('edit-note append fetches existing content then writes the concatenation', async () => {
  const { client, calls } = fakeClient({ get: () => ({ content: 'first' }) });
  const tool = findTool(buildToolDefinitions({ client, getVaultName: () => VAULT }), 'edit-note');
  await tool.handler({ vault: VAULT, filename: 'note.md', operation: 'append', content: 'second' });
  assert.equal(calls[0][0], 'GET');
  assert.equal(calls[1][0], 'POST');
  assert.equal(calls[1][2].content, 'first\n\nsecond');
});

test('edit-note delete calls DELETE /file and requires no content', async () => {
  const { client, calls } = fakeClient();
  const tool = findTool(buildToolDefinitions({ client, getVaultName: () => VAULT }), 'edit-note');
  await tool.handler({ vault: VAULT, filename: 'note.md', operation: 'delete' });
  assert.deepEqual(calls[0], ['DELETE', '/file', { path: 'note.md' }]);
});

test('delete-note reports trash path from vault-api response', async () => {
  const { client } = fakeClient({ del: () => ({ success: true, permanent: false, trashPath: '.trash/note_x.md', updatedFiles: 2 }) });
  const tool = findTool(buildToolDefinitions({ client, getVaultName: () => VAULT }), 'delete-note');
  const result = await tool.handler({ vault: VAULT, path: 'note.md' });
  assert.match(result.content[0].text, /\.trash\/note_x\.md/);
  assert.match(result.content[0].text, /2 file/);
});

test('move-note calls POST /move with .md-normalized paths', async () => {
  const { client, calls } = fakeClient({ post: () => ({ source: 'a.md', destination: 'b/c.md', updatedFiles: 1 }) });
  const tool = findTool(buildToolDefinitions({ client, getVaultName: () => VAULT }), 'move-note');
  await tool.handler({ vault: VAULT, source: 'a', destination: 'b/c' });
  assert.deepEqual(calls[0], ['POST', '/move', { source: 'a.md', destination: 'b/c.md' }]);
});

test('search-vault formats matches from vault-api', async () => {
  const { client } = fakeClient({
    get: () => ({ results: [{ file: 'a.md', matches: [{ line: 1, text: 'hello' }] }], totalMatches: 1, matchedFiles: 1 })
  });
  const tool = findTool(buildToolDefinitions({ client, getVaultName: () => VAULT }), 'search-vault');
  const result = await tool.handler({ vault: VAULT, query: 'hello' });
  assert.match(result.content[0].text, /a\.md/);
  assert.match(result.content[0].text, /hello/);
});

test('rename-tag proxies oldTag/newTag straight through', async () => {
  const { client, calls } = fakeClient({ post: () => ({ updatedFiles: 3 }) });
  const tool = findTool(buildToolDefinitions({ client, getVaultName: () => VAULT }), 'rename-tag');
  const result = await tool.handler({ vault: VAULT, oldTag: 'project', newTag: 'work' });
  assert.deepEqual(calls[0], ['POST', '/tags/rename', { oldTag: 'project', newTag: 'work' }]);
  assert.match(result.content[0].text, /3 file/);
});

test('all 11 obsidian-mcp tool names are present, matching the upstream tool surface 1:1', () => {
  const { client } = fakeClient();
  const names = buildToolDefinitions({ client, getVaultName: () => VAULT }).map((t) => t.name).sort();
  assert.deepEqual(names, [
    'add-tags',
    'create-directory',
    'create-note',
    'delete-note',
    'edit-note',
    'list-available-vaults',
    'move-note',
    'read-note',
    'remove-tags',
    'rename-tag',
    'search-vault'
  ].sort());
});
