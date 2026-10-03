'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const http = require('http');

const { createApp } = require('./server');

const TOKEN = 'test-token';

function makeVault() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'vault-api-test-'));
  fs.mkdirSync(path.join(dir, '.obsidian'));
  return dir;
}

function startServer(vaultPath) {
  const app = createApp({ vaultPath, token: TOKEN });
  const server = app.listen(0);
  const port = server.address().port;
  return { server, base: `http://127.0.0.1:${port}` };
}

async function req(base, method, urlPath, body) {
  const res = await fetch(`${base}${urlPath}`, {
    method,
    headers: {
      'content-type': 'application/json',
      authorization: `Bearer ${TOKEN}`
    },
    body: body === undefined ? undefined : JSON.stringify(body)
  });
  const json = await res.json().catch(() => ({}));
  return { status: res.status, json };
}

test('GET /file and POST /file preserve existing behavior (backward compat for eloso-bisque)', async (t) => {
  const vault = makeVault();
  const { server, base } = startServer(vault);
  t.after(() => server.close());

  const write = await req(base, 'POST', '/file', { path: 'note.md', content: '# Hello' });
  assert.equal(write.status, 200);
  assert.equal(write.json.success, true);

  const read = await req(base, 'GET', '/file?path=note.md');
  assert.equal(read.status, 200);
  assert.equal(read.json.content, '# Hello');

  // Existing behavior: POST with no mode overwrites silently.
  const overwrite = await req(base, 'POST', '/file', { path: 'note.md', content: '# Bye' });
  assert.equal(overwrite.status, 200);
  const reread = await req(base, 'GET', '/file?path=note.md');
  assert.equal(reread.json.content, '# Bye');
});

test('POST /file with mode=create fails if the note already exists (create-note semantics)', async (t) => {
  const vault = makeVault();
  const { server, base } = startServer(vault);
  t.after(() => server.close());

  await req(base, 'POST', '/file', { path: 'note.md', content: 'first' });
  const dup = await req(base, 'POST', '/file', { path: 'note.md', content: 'second', mode: 'create' });
  assert.equal(dup.status, 409);
  const unchanged = await req(base, 'GET', '/file?path=note.md');
  assert.equal(unchanged.json.content, 'first');
});

test('GET /vaults returns the single configured vault (replaces list-available-vaults)', async (t) => {
  const vault = makeVault();
  const { server, base } = startServer(vault);
  t.after(() => server.close());

  const res = await req(base, 'GET', '/vaults');
  assert.equal(res.status, 200);
  assert.equal(res.json.vaults.length, 1);
  assert.equal(res.json.vaults[0].path, vault);
});

test('GET /search finds content and tag matches (replaces search-vault)', async (t) => {
  const vault = makeVault();
  const { server, base } = startServer(vault);
  t.after(() => server.close());

  await req(base, 'POST', '/file', { path: 'a.md', content: '---\ntags: [project/docs]\n---\n\nhello world' });
  await req(base, 'POST', '/file', { path: 'b.md', content: 'nothing relevant here' });

  const contentSearch = await req(base, 'GET', '/search?q=hello');
  assert.equal(contentSearch.json.matchedFiles, 1);
  assert.equal(contentSearch.json.results[0].file, 'a.md');

  const tagSearch = await req(base, 'GET', '/search?q=tag:project');
  assert.equal(tagSearch.json.matchedFiles, 1, 'hierarchical tag search should match project/docs under project');
});

test('DELETE /file moves note to .trash by default and strikes through links elsewhere', async (t) => {
  const vault = makeVault();
  const { server, base } = startServer(vault);
  t.after(() => server.close());

  await req(base, 'POST', '/file', { path: 'target.md', content: 'body' });
  await req(base, 'POST', '/file', { path: 'linker.md', content: 'see [[target]] for details' });

  const del = await req(base, 'DELETE', '/file?path=target.md');
  assert.equal(del.status, 200);
  assert.equal(del.json.permanent, false);
  assert.equal(fs.existsSync(path.join(vault, 'target.md')), false);
  assert.equal(fs.readdirSync(path.join(vault, '.trash')).length, 1);

  const linker = await req(base, 'GET', '/file?path=linker.md');
  assert.match(linker.json.content, /~~\[\[target\]\]~~/);
});

test('DELETE /file?permanent=true removes the note without a trash copy', async (t) => {
  const vault = makeVault();
  const { server, base } = startServer(vault);
  t.after(() => server.close());

  await req(base, 'POST', '/file', { path: 'gone.md', content: 'x' });
  const del = await req(base, 'DELETE', '/file?path=gone.md&permanent=true');
  assert.equal(del.json.permanent, true);
  assert.equal(fs.existsSync(path.join(vault, '.trash')), false);
});

test('POST /move renames a note and rewrites wikilinks pointing at it', async (t) => {
  const vault = makeVault();
  const { server, base } = startServer(vault);
  t.after(() => server.close());

  await req(base, 'POST', '/file', { path: 'old.md', content: 'body' });
  await req(base, 'POST', '/file', { path: 'linker.md', content: 'ref [[old]] here' });

  const move = await req(base, 'POST', '/move', { source: 'old.md', destination: 'folder/new.md' });
  assert.equal(move.status, 200);
  assert.equal(fs.existsSync(path.join(vault, 'old.md')), false);
  assert.equal(fs.existsSync(path.join(vault, 'folder', 'new.md')), true);

  const linker = await req(base, 'GET', '/file?path=linker.md');
  assert.match(linker.json.content, /\[\[new\]\]/);
});

test('POST /move fails if destination already exists', async (t) => {
  const vault = makeVault();
  const { server, base } = startServer(vault);
  t.after(() => server.close());

  await req(base, 'POST', '/file', { path: 'a.md', content: '1' });
  await req(base, 'POST', '/file', { path: 'b.md', content: '2' });
  const move = await req(base, 'POST', '/move', { source: 'a.md', destination: 'b.md' });
  assert.equal(move.status, 409);
});

test('POST /directory creates a new directory and rejects duplicates', async (t) => {
  const vault = makeVault();
  const { server, base } = startServer(vault);
  t.after(() => server.close());

  const create = await req(base, 'POST', '/directory', { path: 'journal/2026' });
  assert.equal(create.status, 200);
  assert.equal(fs.existsSync(path.join(vault, 'journal', '2026')), true);

  const dup = await req(base, 'POST', '/directory', { path: 'journal/2026' });
  assert.equal(dup.status, 409);
});

test('POST /tags/add adds tags to frontmatter and inline content', async (t) => {
  const vault = makeVault();
  const { server, base } = startServer(vault);
  t.after(() => server.close());

  await req(base, 'POST', '/file', { path: 'note.md', content: 'body text' });
  const add = await req(base, 'POST', '/tags/add', { files: ['note.md'], tags: ['status/active'] });
  assert.equal(add.status, 200);
  assert.equal(add.json.applied['note.md'].success, true);

  const note = await req(base, 'GET', '/file?path=note.md');
  assert.match(note.json.content, /tags:\s*\n\s*-\s*status\/active/);
  assert.match(note.json.content, /#status\/active/);
});

test('POST /tags/remove removes a previously added tag from both locations', async (t) => {
  const vault = makeVault();
  const { server, base } = startServer(vault);
  t.after(() => server.close());

  await req(base, 'POST', '/file', { path: 'note.md', content: 'body text' });
  await req(base, 'POST', '/tags/add', { files: ['note.md'], tags: ['status/active'] });
  const remove = await req(base, 'POST', '/tags/remove', { files: ['note.md'], tags: ['status/active'] });
  assert.equal(remove.status, 200);

  const note = await req(base, 'GET', '/file?path=note.md');
  assert.doesNotMatch(note.json.content, /status\/active/);
});

test('POST /tags/rename renames a tag vault-wide across frontmatter and inline occurrences', async (t) => {
  const vault = makeVault();
  const { server, base } = startServer(vault);
  t.after(() => server.close());

  await req(base, 'POST', '/file', { path: 'a.md', content: '---\ntags: [project]\n---\n\nsee #project/docs' });
  await req(base, 'POST', '/file', { path: 'b.md', content: 'unrelated #other' });

  const rename = await req(base, 'POST', '/tags/rename', { oldTag: 'project', newTag: 'work' });
  assert.equal(rename.status, 200);
  assert.equal(rename.json.updatedFiles, 1);

  const a = await req(base, 'GET', '/file?path=a.md');
  assert.match(a.json.content, /tags:\s*\n\s*-\s*work/);
  assert.match(a.json.content, /#work\/docs/);

  const b = await req(base, 'GET', '/file?path=b.md');
  assert.match(b.json.content, /#other/);
});

// --- Regression tests for a real data-mutation bug found during manual
// end-to-end testing of this PR against the live production vault:
// rename-tag scanned every note and wrote it back whenever the re-serialized
// YAML merely *differed* from the raw text, rather than checking whether the
// target tag actually matched. That reformatted 119 unrelated real notes
// (flow-style tag arrays turned into block lists, `created: 2026-04-10`
// silently rewritten to `2026-04-10T00:00:00.000Z`) for a rename whose tag
// matched none of them. Fixed by making every tag helper report an explicit
// `changed` flag and gating every write on it, plus a substring pre-filter
// in /tags/rename so files that don't even mention the tag are never parsed
// or re-serialized at all.

test('POST /tags/rename does not modify files that do not contain the target tag at all', async (t) => {
  const vault = makeVault();
  const { server, base } = startServer(vault);
  t.after(() => server.close());

  const untouchedContent = '---\ntags: [person, kissinger, lobsterdrop]\ncreated: 2026-04-10\n---\n\n# Someone\nBody text.';
  await req(base, 'POST', '/file', { path: 'unrelated.md', content: untouchedContent });
  const before = fs.readFileSync(path.join(vault, 'unrelated.md'), 'utf8');

  const rename = await req(base, 'POST', '/tags/rename', { oldTag: 'zzz-nonexistent-tag', newTag: 'zzz-renamed' });
  assert.equal(rename.json.updatedFiles, 0);

  const after = fs.readFileSync(path.join(vault, 'unrelated.md'), 'utf8');
  assert.equal(after, before, 'a file with no matching tag must be byte-for-byte untouched — no reformatting, no date corruption');
});

test('POST /tags/rename only rewrites files where the tag actually matched, leaving sibling files untouched', async (t) => {
  const vault = makeVault();
  const { server, base } = startServer(vault);
  t.after(() => server.close());

  await req(base, 'POST', '/file', { path: 'match.md', content: '---\ntags: [project]\n---\n\nbody' });
  const siblingContent = '---\ntags: [person, kissinger, lobsterdrop]\ncreated: 2026-04-10\n---\n\nbody';
  await req(base, 'POST', '/file', { path: 'sibling.md', content: siblingContent });

  const rename = await req(base, 'POST', '/tags/rename', { oldTag: 'project', newTag: 'work' });
  assert.equal(rename.json.updatedFiles, 1);

  const sibling = fs.readFileSync(path.join(vault, 'sibling.md'), 'utf8');
  assert.equal(sibling, siblingContent, 'sibling.md never mentioned "project" and must be byte-for-byte untouched');
});

test('POST /tags/rename preserves a plain date-formatted frontmatter field on a file it does rewrite', async (t) => {
  const vault = makeVault();
  const { server, base } = startServer(vault);
  t.after(() => server.close());

  await req(base, 'POST', '/file', { path: 'note.md', content: '---\ntags: [project]\ncreated: 2026-04-10\n---\n\nbody' });
  await req(base, 'POST', '/tags/rename', { oldTag: 'project', newTag: 'work' });

  const note = await req(base, 'GET', '/file?path=note.md');
  assert.match(note.json.content, /created:\s*2026-04-10\s*$/m, 'date-like scalar must round-trip as a plain string, not be promoted to a full timestamp');
  assert.doesNotMatch(note.json.content, /2026-04-10T/, 'must not be corrupted into an ISO datetime');
});

test('POST /tags/add is idempotent — calling it twice with the same tag does not duplicate the inline tag', async (t) => {
  const vault = makeVault();
  const { server, base } = startServer(vault);
  t.after(() => server.close());

  await req(base, 'POST', '/file', { path: 'note.md', content: 'body text' });
  const first = await req(base, 'POST', '/tags/add', { files: ['note.md'], tags: ['status/active'] });
  assert.equal(first.json.applied['note.md'].changed, true);

  const second = await req(base, 'POST', '/tags/add', { files: ['note.md'], tags: ['status/active'] });
  assert.equal(second.json.applied['note.md'].changed, false, 'adding an already-present tag a second time should be a no-op');

  const note = await req(base, 'GET', '/file?path=note.md');
  const occurrences = (note.json.content.match(/#status\/active/g) || []).length;
  assert.equal(occurrences, 1, 'inline tag must appear exactly once, not duplicated');
});

test('requireAuth rejects requests with a missing or wrong token on every route', async (t) => {
  const vault = makeVault();
  const app = createApp({ vaultPath: vault, token: TOKEN });
  const server = app.listen(0);
  t.after(() => server.close());
  const base = `http://127.0.0.1:${server.address().port}`;

  const noAuth = await fetch(`${base}/vaults`);
  assert.equal(noAuth.status, 401);

  const wrongAuth = await fetch(`${base}/vaults`, { headers: { authorization: 'Bearer nope' } });
  assert.equal(wrongAuth.status, 403);
});
