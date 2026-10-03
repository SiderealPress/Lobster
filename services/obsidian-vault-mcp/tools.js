'use strict';

const { z } = require('zod');

function ensureMd(name) {
  return name.endsWith('.md') ? name : `${name}.md`;
}

function joinPath(folder, filename) {
  return folder ? `${folder}/${filename}` : filename;
}

function text(str) {
  return { content: [{ type: 'text', text: str }] };
}

function assertVault(vaultName, expected) {
  if (vaultName !== expected) {
    throw new Error(`Unknown vault "${vaultName}". The only vault configured for this server is "${expected}".`);
  }
}

/**
 * Builds the 11 tool definitions matching obsidian-mcp's own tool names and
 * argument shapes 1:1, so callers/prompts written against `mcp__obsidian__*`
 * do not need to change. Each handler is a thin translation from the
 * obsidian-mcp argument shape to a vault-api HTTP call via `client`.
 *
 * `getVaultName()` is a function (not a plain value) so it can be resolved
 * lazily from vault-api's /vaults endpoint at startup.
 */
function buildToolDefinitions({ client, getVaultName }) {
  return [
    {
      name: 'list-available-vaults',
      description: 'Lists all available vaults that can be used with other tools',
      inputSchema: {},
      handler: async () => {
        const { vaults } = await client.get('/vaults');
        if (vaults.length === 0) return text('No vaults are currently available');
        return text(['Available vaults:', ...vaults.map((v) => `  - ${v.name}`)].join('\n'));
      }
    },

    {
      name: 'read-note',
      description: 'Read the content of an existing note in the vault.',
      inputSchema: {
        vault: z.string().min(1),
        filename: z.string().min(1),
        folder: z.string().optional()
      },
      handler: async ({ vault, filename, folder }) => {
        assertVault(vault, getVaultName());
        const relPath = joinPath(folder, ensureMd(filename));
        const { content } = await client.get('/file', { path: relPath });
        return text(content);
      }
    },

    {
      name: 'create-note',
      description: 'Create a new note in the specified vault with markdown content.',
      inputSchema: {
        vault: z.string().min(1),
        filename: z.string().min(1),
        content: z.string().min(1),
        folder: z.string().optional()
      },
      handler: async ({ vault, filename, content, folder }) => {
        assertVault(vault, getVaultName());
        const relPath = joinPath(folder, ensureMd(filename));
        await client.post('/file', { path: relPath, content, mode: 'create' });
        return text(`Note created successfully at: ${relPath}`);
      }
    },

    {
      name: 'edit-note',
      description: "Edit an existing note. Supported operations: append, prepend, replace, delete.",
      inputSchema: {
        vault: z.string().min(1),
        filename: z.string().min(1),
        folder: z.string().optional(),
        operation: z.enum(['append', 'prepend', 'replace', 'delete']),
        content: z.string().optional()
      },
      handler: async ({ vault, filename, folder, operation, content }) => {
        assertVault(vault, getVaultName());
        const relPath = joinPath(folder, ensureMd(filename));

        if (operation === 'delete') {
          await client.del('/file', { path: relPath });
          return text(`Note deleted successfully: ${relPath}`);
        }

        if (typeof content !== 'string' || content.length === 0) {
          throw new Error('content is required for append/prepend/replace operations');
        }

        const existing = await client.get('/file', { path: relPath });
        let newContent;
        if (operation === 'append') {
          newContent = `${existing.content.trim()}\n\n${content}`;
        } else if (operation === 'prepend') {
          newContent = `${content}\n\n${existing.content.trim()}`;
        } else {
          newContent = content;
        }
        await client.post('/file', { path: relPath, content: newContent });
        return text(`Note ${operation}ed successfully: ${relPath}`);
      }
    },

    {
      name: 'delete-note',
      description: 'Delete a note, moving it to .trash by default or permanently deleting if specified.',
      inputSchema: {
        vault: z.string().min(1),
        path: z.string().min(1),
        reason: z.string().optional(),
        permanent: z.boolean().optional()
      },
      handler: async ({ vault, path: relPath, reason, permanent }) => {
        assertVault(vault, getVaultName());
        const result = await client.del('/file', { path: ensureMd(relPath), reason, permanent });
        return text(
          result.permanent
            ? `Permanently deleted note "${relPath}". Updated ${result.updatedFiles} file(s) with broken links.`
            : `Moved note "${relPath}" to "${result.trashPath}". Updated ${result.updatedFiles} file(s) with broken links.`
        );
      }
    },

    {
      name: 'move-note',
      description: 'Move/rename a note while preserving links.',
      inputSchema: {
        vault: z.string().min(1),
        source: z.string().min(1),
        destination: z.string().min(1)
      },
      handler: async ({ vault, source, destination }) => {
        assertVault(vault, getVaultName());
        const result = await client.post('/move', { source: ensureMd(source), destination: ensureMd(destination) });
        return text(`Successfully moved note from "${result.source}" to "${result.destination}". Updated links in ${result.updatedFiles} file(s).`);
      }
    },

    {
      name: 'create-directory',
      description: 'Create a new directory in the specified vault.',
      inputSchema: {
        vault: z.string().min(1),
        path: z.string().min(1),
        recursive: z.boolean().optional()
      },
      handler: async ({ vault, path: dirPath, recursive }) => {
        assertVault(vault, getVaultName());
        const result = await client.post('/directory', { path: dirPath, recursive: recursive ?? true });
        return text(`Successfully created directory at: ${result.path}`);
      }
    },

    {
      name: 'search-vault',
      description: 'Search for specific content within vault notes (content, filename, or tag: prefix for tags).',
      inputSchema: {
        vault: z.string().min(1),
        query: z.string().min(1),
        path: z.string().optional(),
        caseSensitive: z.boolean().optional(),
        searchType: z.enum(['content', 'filename', 'both']).optional()
      },
      handler: async ({ vault, query, path: subPath, caseSensitive, searchType }) => {
        assertVault(vault, getVaultName());
        const result = await client.get('/search', { q: query, path: subPath, caseSensitive, searchType });
        if (result.matchedFiles === 0) return text('No matches found.');
        const lines = result.results.map((r) => `${r.file}:\n${r.matches.map((m) => `  ${m.line}: ${m.text}`).join('\n')}`);
        return text(`Found ${result.totalMatches} match(es) in ${result.matchedFiles} file(s):\n\n${lines.join('\n\n')}`);
      }
    },

    {
      name: 'add-tags',
      description: 'Add tags to notes in frontmatter and/or content.',
      inputSchema: {
        vault: z.string().min(1),
        files: z.array(z.string()).min(1),
        tags: z.array(z.string()).min(1),
        location: z.enum(['frontmatter', 'content', 'both']).optional()
      },
      handler: async ({ vault, files, tags, location }) => {
        assertVault(vault, getVaultName());
        const result = await client.post('/tags/add', { files: files.map(ensureMd), tags, location });
        return text(`Tag addition completed: ${JSON.stringify(result.applied)}`);
      }
    },

    {
      name: 'remove-tags',
      description: 'Remove tags from notes in frontmatter and/or content.',
      inputSchema: {
        vault: z.string().min(1),
        files: z.array(z.string()).min(1),
        tags: z.array(z.string()).min(1),
        options: z.object({ location: z.enum(['frontmatter', 'content', 'both']).optional() }).optional()
      },
      handler: async ({ vault, files, tags, options }) => {
        assertVault(vault, getVaultName());
        const result = await client.post('/tags/remove', { files: files.map(ensureMd), tags, location: options?.location });
        return text(`Tag removal completed: ${JSON.stringify(result.applied)}`);
      }
    },

    {
      name: 'rename-tag',
      description: 'Rename a tag across every note in the vault (frontmatter and inline).',
      inputSchema: {
        vault: z.string().min(1),
        oldTag: z.string().min(1),
        newTag: z.string().min(1)
      },
      handler: async ({ vault, oldTag, newTag }) => {
        assertVault(vault, getVaultName());
        const result = await client.post('/tags/rename', { oldTag, newTag });
        return text(`Renamed tag "${oldTag}" to "${newTag}" in ${result.updatedFiles} file(s).`);
      }
    }
  ];
}

module.exports = { buildToolDefinitions, ensureMd, joinPath, assertVault };
