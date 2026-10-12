'use strict';

/**
 * Thin HTTP client for vault-api, with a bounded per-call timeout.
 *
 * This module is the actual fix for SiderealPress/lobster#2119/#2219: every
 * call is wrapped in an AbortController that fires after `timeoutMs`. Where
 * the old stdio `obsidian-mcp` server could go silently unresponsive forever
 * (its own idle-timeout self-closes the transport's stdin reader without
 * exiting the process or notifying the client — see PR description), an HTTP
 * request either succeeds, fails, or is aborted — there is no code path that
 * leaves the caller waiting indefinitely.
 */

class VaultApiError extends Error {
  constructor(message, { status, code } = {}) {
    super(message);
    this.name = 'VaultApiError';
    this.status = status;
    this.code = code;
  }
}

class VaultApiTimeoutError extends VaultApiError {
  constructor(method, path, timeoutMs) {
    super(`vault-api request timed out after ${timeoutMs}ms: ${method} ${path}`, { code: 'TIMEOUT' });
    this.name = 'VaultApiTimeoutError';
  }
}

function createVaultClient({ baseUrl, token, timeoutMs, fetchImpl = fetch }) {
  async function request(method, path, { query, body } = {}) {
    const url = new URL(path, baseUrl);
    if (query) {
      for (const [k, v] of Object.entries(query)) {
        if (v !== undefined && v !== null) url.searchParams.set(k, String(v));
      }
    }

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    let res;
    try {
      res = await fetchImpl(url, {
        method,
        headers: {
          authorization: `Bearer ${token}`,
          ...(body !== undefined ? { 'content-type': 'application/json' } : {})
        },
        body: body !== undefined ? JSON.stringify(body) : undefined,
        signal: controller.signal
      });
    } catch (err) {
      if (err.name === 'AbortError') {
        throw new VaultApiTimeoutError(method, path, timeoutMs);
      }
      throw new VaultApiError(`vault-api request failed: ${method} ${path}: ${err.message}`, { code: 'NETWORK_ERROR' });
    } finally {
      clearTimeout(timer);
    }

    const json = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new VaultApiError(json.error || `vault-api returned HTTP ${res.status}`, { status: res.status });
    }
    return json;
  }

  return {
    get: (path, query) => request('GET', path, { query }),
    post: (path, body) => request('POST', path, { body }),
    del: (path, query) => request('DELETE', path, { query })
  };
}

module.exports = { createVaultClient, VaultApiError, VaultApiTimeoutError };
