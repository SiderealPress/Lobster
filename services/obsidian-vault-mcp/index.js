#!/usr/bin/env node
'use strict';

const { McpServer } = require('@modelcontextprotocol/sdk/server/mcp.js');
const { StdioServerTransport } = require('@modelcontextprotocol/sdk/server/stdio.js');
const { createVaultClient } = require('./vault-client');
const { buildToolDefinitions } = require('./tools');

const VAULT_API_URL = process.env.VAULT_API_URL || 'http://127.0.0.1:8082';
const VAULT_API_TOKEN = process.env.VAULT_API_TOKEN;
// Bounded timeout: this is the actual fix for #2119/#2219. The old stdio
// obsidian-mcp server had no timeout at all — a hang blocked the caller
// until an unrelated ~2h04m session-age restart. Here, any hang in the HTTP
// call surfaces as a normal tool error within this many milliseconds.
const VAULT_API_TIMEOUT_MS = parseInt(process.env.VAULT_API_TIMEOUT_MS || '20000', 10);

async function main() {
  if (!VAULT_API_TOKEN) {
    console.error('VAULT_API_TOKEN env var is required');
    process.exit(1);
  }

  const client = createVaultClient({
    baseUrl: VAULT_API_URL,
    token: VAULT_API_TOKEN,
    timeoutMs: VAULT_API_TIMEOUT_MS
  });

  // Resolve the configured vault name once at startup (bounded by the same
  // timeout as any other call — if vault-api is unreachable at boot, fail
  // fast and loud instead of registering tools against an unknown vault).
  const { vaults } = await client.get('/vaults');
  if (!vaults || vaults.length === 0) {
    console.error('vault-api reported no configured vaults');
    process.exit(1);
  }
  const vaultName = vaults[0].name;
  console.error(`obsidian-vault-mcp: using vault-api at ${VAULT_API_URL}, vault="${vaultName}", per-call timeout=${VAULT_API_TIMEOUT_MS}ms`);

  const server = new McpServer({ name: 'obsidian-vault-mcp', version: '1.0.0' }, { capabilities: { tools: {} } });

  for (const tool of buildToolDefinitions({ client, getVaultName: () => vaultName })) {
    server.registerTool(
      tool.name,
      { description: tool.description, inputSchema: tool.inputSchema },
      async (args) => {
        try {
          return await tool.handler(args);
        } catch (err) {
          console.error(`[${tool.name}] error:`, err.message);
          return {
            isError: true,
            content: [{ type: 'text', text: err.message }]
          };
        }
      }
    );
  }

  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error('obsidian-vault-mcp running on stdio');
}

main().catch((err) => {
  console.error('obsidian-vault-mcp failed to start:', err);
  process.exit(1);
});
