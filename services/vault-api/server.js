'use strict';

const express = require('express');
const cors = require('cors');
const fs = require('fs');
const path = require('path');
const yaml = require('js-yaml');

// ---------------------------------------------------------------------------
// Pure helpers (no I/O) — kept separate from the Express route handlers below
// so they can be unit tested without spinning up a server or touching disk.
// ---------------------------------------------------------------------------

const FRONTMATTER_RE = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?/;

/** Split a note's raw text into { frontmatter, body }. frontmatter is a plain
 *  object (parsed YAML) or {} if the note has no frontmatter block.
 *
 *  Uses js-yaml's JSON_SCHEMA (rather than the default schema) deliberately:
 *  the default schema auto-resolves date-like scalars (e.g. `created:
 *  2026-04-10`) into native JS Date objects on load, which `yaml.dump()`
 *  then re-serializes as a full ISO timestamp (`2026-04-10T00:00:00.000Z`)
 *  even when nothing about that field was touched. JSON_SCHEMA has no
 *  timestamp type, so such values round-trip as plain strings. */
function parseNote(raw) {
  const match = raw.match(FRONTMATTER_RE);
  if (!match) {
    return { frontmatter: {}, body: raw, hadFrontmatter: false };
  }
  let frontmatter;
  try {
    frontmatter = yaml.load(match[1], { schema: yaml.JSON_SCHEMA }) || {};
  } catch {
    // Malformed frontmatter — treat the whole file as body rather than
    // throwing, so a single bad note doesn't break tag operations vault-wide.
    return { frontmatter: {}, body: raw, hadFrontmatter: false };
  }
  return { frontmatter, body: raw.slice(match[0].length), hadFrontmatter: true };
}

/** Reassemble { frontmatter, body } back into note text. Omits the
 *  frontmatter block entirely if frontmatter is empty. */
function stringifyNote({ frontmatter, body }) {
  if (!frontmatter || Object.keys(frontmatter).length === 0) {
    return body;
  }
  // Dump with the same JSON_SCHEMA used by parseNote's load — using
  // mismatched schemas (load: JSON_SCHEMA, dump: default) makes the dumper
  // defensively quote plain date-like strings (e.g. `created: '2026-04-10'`)
  // since it can't be sure a default-schema loader wouldn't reinterpret them
  // as timestamps. Matching schemas avoids that unnecessary quoting.
  return `---\n${yaml.dump(frontmatter, { schema: yaml.JSON_SCHEMA }).trimEnd()}\n---\n\n${body.replace(/^\n+/, '')}`;
}

/** Normalize a tag the same rough way obsidian-mcp did: lowercase, spaces and
 *  underscores to hyphens, strip characters other than [a-z0-9/-]. This is a
 *  deliberate simplification of upstream's fuller normalization (documented
 *  in the PR) — good enough for exact-tag and hierarchical add/remove/rename. */
function normalizeTag(tag) {
  return String(tag)
    .trim()
    .toLowerCase()
    .replace(/[\s_]+/g, '-')
    .replace(/[^a-z0-9/-]/g, '');
}

/** True if `tag` equals `target` or is a hierarchical child of it
 *  (e.g. target="project", tag="project/docs"). Both must already be
 *  normalized. */
function tagMatches(tag, target) {
  return tag === target || tag.startsWith(`${target}/`);
}

// Every tag helper below returns { ..., changed: boolean } instead of just
// the transformed value, and route handlers below only call stringifyNote()
// + write the file when `changed` is true. This is deliberate, not
// stylistic: a full parseNote()/stringifyNote() round trip re-serializes
// YAML from scratch (flow arrays become block lists, key order can shift,
// etc.), so writing a file back "just in case" — even when the requested
// tag operation found nothing to do on that file — silently rewrites and
// reformats content the caller never asked to touch. This exact bug was
// caught during manual end-to-end testing of rename-tag against the real
// production vault: scanning all files and writing back whenever the
// re-serialized string merely *differed* from the raw string (rather than
// checking whether a tag was actually renamed) reformatted 119 unrelated
// notes for a rename-tag call whose target tag matched none of them.

function addTagsToFrontmatter(frontmatter, tags) {
  const existingRaw = Array.isArray(frontmatter.tags) ? frontmatter.tags : [];
  const existingNorm = existingRaw.map(normalizeTag);
  const seen = new Set(existingNorm);
  const toAdd = [];
  for (const t of tags.map(normalizeTag)) {
    if (!seen.has(t)) { seen.add(t); toAdd.push(t); }
  }
  if (toAdd.length === 0) return { frontmatter, changed: false };
  return { frontmatter: { ...frontmatter, tags: [...existingRaw, ...toAdd] }, changed: true };
}

function removeTagsFromFrontmatter(frontmatter, tags) {
  if (!Array.isArray(frontmatter.tags)) return { frontmatter, changed: false };
  const targets = tags.map(normalizeTag);
  const kept = frontmatter.tags.filter((t) => !targets.some((target) => tagMatches(normalizeTag(t), target)));
  if (kept.length === frontmatter.tags.length) return { frontmatter, changed: false };
  const next = { ...frontmatter, tags: kept };
  if (kept.length === 0) delete next.tags;
  return { frontmatter: next, changed: true };
}

function renameTagInFrontmatter(frontmatter, oldTag, newTag) {
  if (!Array.isArray(frontmatter.tags)) return { frontmatter, changed: false };
  const normOld = normalizeTag(oldTag);
  const normNew = normalizeTag(newTag);
  let changed = false;
  const renamed = frontmatter.tags.map((t) => {
    const nt = normalizeTag(t);
    if (!tagMatches(nt, normOld)) return t; // preserve original, untouched tag exactly as-is
    changed = true;
    return nt.replace(new RegExp(`^${normOld}`), normNew);
  });
  if (!changed) return { frontmatter, changed: false };
  return { frontmatter: { ...frontmatter, tags: Array.from(new Set(renamed)) }, changed: true };
}

const INLINE_TAG_RE = /(^|[\s(])#([a-zA-Z0-9][a-zA-Z0-9/-]*)/g;

function existingInlineTags(body) {
  return new Set([...body.matchAll(INLINE_TAG_RE)].map((m) => normalizeTag(m[2])));
}

function addInlineTags(body, tags) {
  const existing = existingInlineTags(body);
  const toAdd = tags.map(normalizeTag).filter((t) => !existing.has(t));
  if (toAdd.length === 0) return { body, changed: false };
  const suffix = toAdd.map((t) => `#${t}`).join(' ');
  return { body: `${body.trimEnd()}\n\n${suffix}\n`, changed: true };
}

function removeInlineTags(body, tags) {
  const targets = tags.map(normalizeTag);
  let changed = false;
  const next = body.replace(INLINE_TAG_RE, (match, pre, tagName) => {
    const normalized = normalizeTag(tagName);
    if (!targets.some((target) => tagMatches(normalized, target))) return match;
    changed = true;
    return pre;
  });
  return { body: next, changed };
}

function renameInlineTags(body, oldTag, newTag) {
  const normOld = normalizeTag(oldTag);
  const normNew = normalizeTag(newTag);
  let changed = false;
  const next = body.replace(INLINE_TAG_RE, (match, pre, tagName) => {
    const normalized = normalizeTag(tagName);
    if (!tagMatches(normalized, normOld)) return match;
    changed = true;
    const renamed = normalized.replace(new RegExp(`^${normOld}`), normNew);
    return `${pre}#${renamed}`;
  });
  return { body: next, changed };
}

/** Rewrite [[wikilinks]] and [text](file.md) references to `oldName` (a
 *  filename with no extension, e.g. "note") so they point at `newName`
 *  instead. Pass newName=null to strike through references to a deleted note. */
function rewriteLinks(content, oldName, newName) {
  const wikilink = new RegExp(`\\[\\[${escapeRegex(oldName)}(\\|[^\\]]*)?\\]\\]`, 'g');
  const mdlink = new RegExp(`\\[([^\\]]*)\\]\\(${escapeRegex(oldName)}\\.md\\)`, 'g');
  if (newName === null) {
    return content
      .replace(wikilink, (m) => `~~${m}~~`)
      .replace(mdlink, (m) => `~~${m}~~`);
  }
  return content
    .replace(wikilink, (_m, alias = '') => `[[${newName}${alias || ''}]]`)
    .replace(mdlink, (_m, text) => `[${text}](${newName}.md)`);
}

function escapeRegex(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** Derive the vault "name" the same way obsidian-mcp does: last path
 *  segment, lowercased, non-alphanumeric runs collapsed to a single hyphen. */
function deriveVaultName(vaultPath) {
  return path
    .basename(vaultPath)
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
}

function isTagQuery(query) {
  return query.startsWith('tag:');
}

/** Search markdown files under `files` (absolute paths) for `query`.
 *  Mirrors obsidian-mcp's search-vault semantics: content, filename, or
 *  both; tag: prefix searches frontmatter+inline tags instead of raw text. */
function searchFiles(files, vaultPath, query, { caseSensitive = false, searchType = 'content' } = {}) {
  const results = [];
  const wantFilename = searchType === 'filename' || searchType === 'both';
  const wantContent = searchType === 'content' || searchType === 'both';
  const tagQuery = isTagQuery(query) ? normalizeTag(query.slice(4)) : null;
  const needle = caseSensitive ? query : query.toLowerCase();

  for (const absPath of files) {
    const relPath = path.relative(vaultPath, absPath);
    const matches = [];

    if (wantFilename) {
      const target = caseSensitive ? relPath : relPath.toLowerCase();
      if (target.includes(needle)) {
        matches.push({ line: 0, text: `Filename match: ${relPath}` });
      }
    }

    if (wantContent) {
      const raw = fs.readFileSync(absPath, 'utf8');
      if (tagQuery) {
        const { frontmatter, body } = parseNote(raw);
        const frontmatterTags = Array.isArray(frontmatter.tags) ? frontmatter.tags.map(normalizeTag) : [];
        if (frontmatterTags.some((t) => tagMatches(t, tagQuery))) {
          matches.push({ line: 0, text: `Frontmatter tag match: ${tagQuery}` });
        }
        body.split('\n').forEach((line, idx) => {
          const lineTags = [...line.matchAll(INLINE_TAG_RE)].map((m) => normalizeTag(m[2]));
          if (lineTags.some((t) => tagMatches(t, tagQuery))) {
            matches.push({ line: idx + 1, text: line.trim() });
          }
        });
      } else {
        raw.split('\n').forEach((line, idx) => {
          const target = caseSensitive ? line : line.toLowerCase();
          if (target.includes(needle)) {
            matches.push({ line: idx + 1, text: line.trim() });
          }
        });
      }
    }

    if (matches.length > 0) {
      results.push({ file: relPath, matches });
    }
  }
  return results;
}

function listMarkdownFilesRecursive(dirPath) {
  const out = [];
  for (const entry of fs.readdirSync(dirPath, { withFileTypes: true })) {
    if (entry.name.startsWith('.')) continue;
    const full = path.join(dirPath, entry.name);
    if (entry.isDirectory()) {
      out.push(...listMarkdownFilesRecursive(full));
    } else if (entry.isFile() && entry.name.endsWith('.md')) {
      out.push(full);
    }
  }
  return out;
}

// ---------------------------------------------------------------------------
// App factory — takes vaultPath/token as arguments (rather than reading
// process.env directly) so tests can point it at a throwaway temp directory.
// ---------------------------------------------------------------------------

function createApp({ vaultPath, token }) {
  const app = express();

  app.use(cors({
    origin: [
      'https://eloso-bisque.vercel.app',
      /^https:\/\/eloso-bisque.*\.vercel\.app$/,
      'http://localhost:3000',
      'http://localhost:3001'
    ],
    credentials: true
  }));

  app.use(express.json({ limit: '10mb' }));

  function requireAuth(req, res, next) {
    const authHeader = req.headers['authorization'];
    if (!authHeader || !authHeader.startsWith('Bearer ')) {
      return res.status(401).json({ error: 'Missing or invalid Authorization header' });
    }
    if (authHeader.slice(7) !== token) {
      return res.status(403).json({ error: 'Invalid token' });
    }
    next();
  }

  // Validate that a requested path is within the vault (prevent directory traversal)
  function resolveVaultPath(relPath) {
    const normalized = path.normalize(relPath).replace(/^(\.\.[/\\])+/, '');
    const absolute = path.resolve(vaultPath, normalized);
    if (!absolute.startsWith(path.resolve(vaultPath))) {
      return null;
    }
    return absolute;
  }

  function buildTree(dirPath, relBase) {
    const entries = fs.readdirSync(dirPath, { withFileTypes: true });
    const nodes = [];
    for (const entry of entries) {
      if (entry.name.startsWith('.')) continue;
      const relPath = relBase ? `${relBase}/${entry.name}` : entry.name;
      if (entry.isDirectory()) {
        nodes.push({ type: 'directory', name: entry.name, path: relPath, children: buildTree(path.join(dirPath, entry.name), relPath) });
      } else if (entry.isFile() && entry.name.endsWith('.md')) {
        nodes.push({ type: 'file', name: entry.name, path: relPath });
      }
    }
    nodes.sort((a, b) => (a.type !== b.type ? (a.type === 'directory' ? -1 : 1) : a.name.localeCompare(b.name)));
    return nodes;
  }

  // ---- Existing endpoints (unchanged behavior — eloso-bisque depends on these) ----

  app.get('/files', requireAuth, (req, res) => {
    try {
      res.json({ tree: buildTree(vaultPath, '') });
    } catch (err) {
      console.error('Error building file tree:', err);
      res.status(500).json({ error: 'Failed to read vault directory' });
    }
  });

  app.get('/file', requireAuth, (req, res) => {
    const { path: relPath } = req.query;
    if (!relPath) return res.status(400).json({ error: 'path query parameter is required' });
    const absPath = resolveVaultPath(relPath);
    if (!absPath) return res.status(400).json({ error: 'Invalid path' });
    if (!absPath.endsWith('.md')) return res.status(400).json({ error: 'Only .md files are allowed' });
    try {
      const content = fs.readFileSync(absPath, 'utf8');
      res.json({ path: relPath, content });
    } catch (err) {
      if (err.code === 'ENOENT') return res.status(404).json({ error: 'File not found' });
      console.error('Error reading file:', err);
      res.status(500).json({ error: 'Failed to read file' });
    }
  });

  app.post('/file', requireAuth, (req, res) => {
    const { path: relPath, content, mode } = req.body;
    if (!relPath) return res.status(400).json({ error: 'path is required in request body' });
    if (typeof content !== 'string') return res.status(400).json({ error: 'content must be a string' });
    const absPath = resolveVaultPath(relPath);
    if (!absPath) return res.status(400).json({ error: 'Invalid path' });
    if (!absPath.endsWith('.md')) return res.status(400).json({ error: 'Only .md files are allowed' });
    if (mode === 'create' && fs.existsSync(absPath)) {
      return res.status(409).json({ error: `A note already exists at: ${relPath}` });
    }
    try {
      fs.mkdirSync(path.dirname(absPath), { recursive: true });
      fs.writeFileSync(absPath, content, 'utf8');
      res.json({ success: true, path: relPath });
    } catch (err) {
      console.error('Error writing file:', err);
      res.status(500).json({ error: 'Failed to write file' });
    }
  });

  app.get('/health', (req, res) => {
    res.json({ status: 'ok', vault: vaultPath });
  });

  // ---- New endpoints added for issue #2219 (obsidian-vault-mcp wrapper) ----

  // GET /vaults — replaces obsidian-mcp's list-available-vaults tool.
  app.get('/vaults', requireAuth, (req, res) => {
    res.json({ vaults: [{ name: deriveVaultName(vaultPath), path: vaultPath }] });
  });

  // GET /search?q=...&path=&caseSensitive=&searchType= — replaces search-vault.
  app.get('/search', requireAuth, (req, res) => {
    const { q, path: subPath, caseSensitive, searchType } = req.query;
    if (!q) return res.status(400).json({ error: 'q query parameter is required' });
    const searchRoot = subPath ? resolveVaultPath(subPath) : vaultPath;
    if (!searchRoot) return res.status(400).json({ error: 'Invalid path' });
    try {
      const files = listMarkdownFilesRecursive(searchRoot);
      const results = searchFiles(files, vaultPath, q, {
        caseSensitive: caseSensitive === 'true',
        searchType: searchType || 'content'
      });
      const totalMatches = results.reduce((sum, r) => sum + r.matches.length, 0);
      res.json({ results, totalMatches, matchedFiles: results.length });
    } catch (err) {
      console.error('Error searching vault:', err);
      res.status(500).json({ error: 'Failed to search vault' });
    }
  });

  // DELETE /file?path=&permanent=&reason= — replaces delete-note.
  app.delete('/file', requireAuth, (req, res) => {
    const { path: relPath, permanent, reason } = req.query;
    if (!relPath) return res.status(400).json({ error: 'path query parameter is required' });
    const absPath = resolveVaultPath(relPath);
    if (!absPath || !absPath.endsWith('.md')) return res.status(400).json({ error: 'Invalid path' });
    if (!fs.existsSync(absPath)) return res.status(404).json({ error: 'File not found' });

    try {
      const noteName = path.basename(relPath, '.md');
      let updatedFiles = 0;
      for (const f of listMarkdownFilesRecursive(vaultPath)) {
        if (f === absPath) continue;
        const raw = fs.readFileSync(f, 'utf8');
        const rewritten = rewriteLinks(raw, noteName, null);
        if (rewritten !== raw) {
          fs.writeFileSync(f, rewritten, 'utf8');
          updatedFiles += 1;
        }
      }

      if (permanent === 'true') {
        fs.unlinkSync(absPath);
        return res.json({ success: true, permanent: true, updatedFiles });
      }

      const trashDir = path.join(vaultPath, '.trash');
      fs.mkdirSync(trashDir, { recursive: true });
      const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
      const trashName = `${noteName}_${timestamp}.md`;
      const original = fs.readFileSync(absPath, 'utf8');
      const withMeta = `---\ntrash_metadata:\n  original_path: ${relPath}\n  deleted_at: ${new Date().toISOString()}${reason ? `\n  reason: ${reason}` : ''}\n---\n\n${original}`;
      fs.writeFileSync(path.join(trashDir, trashName), withMeta, 'utf8');
      fs.unlinkSync(absPath);
      res.json({ success: true, permanent: false, trashPath: `.trash/${trashName}`, updatedFiles });
    } catch (err) {
      console.error('Error deleting note:', err);
      res.status(500).json({ error: 'Failed to delete note' });
    }
  });

  // POST /move — { source, destination } — replaces move-note.
  app.post('/move', requireAuth, (req, res) => {
    const { source, destination } = req.body;
    if (!source || !destination) return res.status(400).json({ error: 'source and destination are required' });
    const absSource = resolveVaultPath(source);
    const absDest = resolveVaultPath(destination);
    if (!absSource || !absDest) return res.status(400).json({ error: 'Invalid path' });
    if (!fs.existsSync(absSource)) return res.status(404).json({ error: 'Source note not found' });
    if (fs.existsSync(absDest)) return res.status(409).json({ error: 'A note already exists at destination' });

    try {
      fs.mkdirSync(path.dirname(absDest), { recursive: true });
      fs.renameSync(absSource, absDest);
      const oldName = path.basename(source, '.md');
      const newName = path.basename(destination, '.md');
      let updatedFiles = 0;
      for (const f of listMarkdownFilesRecursive(vaultPath)) {
        if (f === absDest) continue;
        const raw = fs.readFileSync(f, 'utf8');
        const rewritten = rewriteLinks(raw, oldName, newName);
        if (rewritten !== raw) {
          fs.writeFileSync(f, rewritten, 'utf8');
          updatedFiles += 1;
        }
      }
      res.json({ success: true, source, destination, updatedFiles });
    } catch (err) {
      console.error('Error moving note:', err);
      res.status(500).json({ error: 'Failed to move note' });
    }
  });

  // POST /directory — { path, recursive } — replaces create-directory.
  app.post('/directory', requireAuth, (req, res) => {
    const { path: relPath, recursive } = req.body;
    if (!relPath) return res.status(400).json({ error: 'path is required' });
    const absPath = resolveVaultPath(relPath);
    if (!absPath) return res.status(400).json({ error: 'Invalid path' });
    if (fs.existsSync(absPath)) return res.status(409).json({ error: `A directory already exists at: ${relPath}` });
    try {
      fs.mkdirSync(absPath, { recursive: recursive !== false });
      res.json({ success: true, path: relPath });
    } catch (err) {
      console.error('Error creating directory:', err);
      res.status(500).json({ error: 'Failed to create directory' });
    }
  });

  // POST /tags/add — { files: [relPath...], tags: [...], location? } — replaces add-tags.
  app.post('/tags/add', requireAuth, (req, res) => {
    const { files, tags, location = 'both' } = req.body;
    if (!Array.isArray(files) || files.length === 0) return res.status(400).json({ error: 'files must be a non-empty array' });
    if (!Array.isArray(tags) || tags.length === 0) return res.status(400).json({ error: 'tags must be a non-empty array' });
    const applied = {};
    for (const relPath of files) {
      const absPath = resolveVaultPath(relPath);
      if (!absPath || !fs.existsSync(absPath)) { applied[relPath] = { error: 'File not found' }; continue; }
      const raw = fs.readFileSync(absPath, 'utf8');
      const note = parseNote(raw);
      let changed = false;
      if (location !== 'content') {
        const r = addTagsToFrontmatter(note.frontmatter, tags);
        note.frontmatter = r.frontmatter;
        changed = changed || r.changed;
      }
      if (location !== 'frontmatter') {
        const r = addInlineTags(note.body, tags);
        note.body = r.body;
        changed = changed || r.changed;
      }
      // Only rewrite the file if a tag was actually added — see the note
      // above the tag helpers for why "always write" is unsafe here.
      if (changed) fs.writeFileSync(absPath, stringifyNote(note), 'utf8');
      applied[relPath] = { success: true, changed };
    }
    res.json({ applied });
  });

  // POST /tags/remove — { files, tags, location? } — replaces remove-tags.
  app.post('/tags/remove', requireAuth, (req, res) => {
    const { files, tags, location = 'both' } = req.body;
    if (!Array.isArray(files) || files.length === 0) return res.status(400).json({ error: 'files must be a non-empty array' });
    if (!Array.isArray(tags) || tags.length === 0) return res.status(400).json({ error: 'tags must be a non-empty array' });
    const applied = {};
    for (const relPath of files) {
      const absPath = resolveVaultPath(relPath);
      if (!absPath || !fs.existsSync(absPath)) { applied[relPath] = { error: 'File not found' }; continue; }
      const raw = fs.readFileSync(absPath, 'utf8');
      const note = parseNote(raw);
      let changed = false;
      if (location !== 'content') {
        const r = removeTagsFromFrontmatter(note.frontmatter, tags);
        note.frontmatter = r.frontmatter;
        changed = changed || r.changed;
      }
      if (location !== 'frontmatter') {
        const r = removeInlineTags(note.body, tags);
        note.body = r.body;
        changed = changed || r.changed;
      }
      if (changed) fs.writeFileSync(absPath, stringifyNote(note), 'utf8');
      applied[relPath] = { success: true, changed };
    }
    res.json({ applied });
  });

  // POST /tags/rename — { oldTag, newTag } — replaces rename-tag (vault-wide).
  app.post('/tags/rename', requireAuth, (req, res) => {
    const { oldTag, newTag } = req.body;
    if (!oldTag || !newTag) return res.status(400).json({ error: 'oldTag and newTag are required' });
    // Cheap pre-filter: skip any file that doesn't even contain the tag's
    // root token before parsing/re-serializing it. This is not just a perf
    // optimization — it is the primary guard against rewriting files this
    // operation was never asked to touch (see the note above the tag
    // helpers). A vault-wide scan without this pre-filter previously
    // rewrote 119 unrelated real notes for a rename-tag call whose target
    // matched zero of them, caught during manual end-to-end testing.
    const needle = normalizeTag(oldTag).split('/')[0];
    let updatedFiles = 0;
    for (const absPath of listMarkdownFilesRecursive(vaultPath)) {
      const raw = fs.readFileSync(absPath, 'utf8');
      if (!raw.toLowerCase().includes(needle)) continue;
      const note = parseNote(raw);
      const fm = renameTagInFrontmatter(note.frontmatter, oldTag, newTag);
      const bd = renameInlineTags(note.body, oldTag, newTag);
      if (!fm.changed && !bd.changed) continue;
      note.frontmatter = fm.frontmatter;
      note.body = bd.body;
      fs.writeFileSync(absPath, stringifyNote(note), 'utf8');
      updatedFiles += 1;
    }
    res.json({ success: true, updatedFiles });
  });

  return app;
}

module.exports = { createApp, parseNote, stringifyNote, normalizeTag, tagMatches, rewriteLinks, deriveVaultName, searchFiles };

if (require.main === module) {
  const PORT = process.env.VAULT_API_PORT || 8082;
  const VAULT_PATH = process.env.VAULT_PATH || '/home/lobster/LobsterDrop/eloso-obsidian-vault';
  const VAULT_API_TOKEN = process.env.VAULT_API_TOKEN;

  if (!VAULT_API_TOKEN) {
    console.error('VAULT_API_TOKEN env var is required');
    process.exit(1);
  }

  const app = createApp({ vaultPath: VAULT_PATH, token: VAULT_API_TOKEN });
  app.listen(PORT, '127.0.0.1', () => {
    console.log(`vault-api listening on http://127.0.0.1:${PORT}`);
    console.log(`Vault path: ${VAULT_PATH}`);
  });
}
