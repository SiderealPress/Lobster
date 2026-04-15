# CLI/Library Approach Decision: Obsidian KM Skill

**Decision Date:** 2026-03-30
**Status:** Accepted
**Issue:** BIS-237

## Context

The Obsidian KM Skill needs to perform read/write operations on an Obsidian vault. We evaluated several approaches for implementing these operations.

## Options Considered

### Option 1: obsidian-cli (npm package)
- **Pros:** Purpose-built for Obsidian, handles plugins/templates
- **Cons:** Node.js dependency, external process overhead, limited maintenance, requires Obsidian app configuration

### Option 2: Obsidian Local REST API plugin
- **Pros:** Official-ish integration, rich API
- **Cons:** Requires running Obsidian app, plugin dependency, network overhead

### Option 3: Pure Python filesystem + frontmatter + ripgrep
- **Pros:**
  - Zero external dependencies beyond standard Python libraries
  - Direct filesystem access (fast)
  - ripgrep is already installed on Lobster systems
  - Full control over implementation
  - Functional programming patterns apply cleanly
  - Works on headless servers without Obsidian app
- **Cons:**
  - Must implement features manually
  - No plugin integration (but we don't need it)

## Decision

**Selected: Option 3 — Pure Python filesystem + `python-frontmatter` + ripgrep**

### Rationale

1. **Simplicity**: Obsidian vaults are just markdown files with YAML frontmatter. No special tooling required.

2. **Performance**: Direct filesystem access is faster than CLI subprocess or REST API calls.

3. **Functional fit**: Pure functions with injected vault paths compose well and are easily testable.

4. **Server compatibility**: Works on headless Lobster servers without requiring Obsidian app.

5. **Existing infrastructure**: ripgrep is already available system-wide for fast full-text search.

## Implementation Details

### Core Dependencies
- `python-frontmatter`: Parse/write YAML frontmatter in markdown files
- `pathlib`: Standard library path handling
- `subprocess` + `rg`: ripgrep for full-text search

### Module Structure
```
lobster-shop/obsidian-km/
├── docs/
│   └── cli-approach.md      # This document
├── src/
│   ├── __init__.py
│   └── vault_ops.py         # Core vault operations
└── scripts/
    └── vault_poc.py         # Proof of concept
```

### API Design Principles

1. **Pure functions**: All operations take explicit vault path parameter (no global state)
2. **Immutability**: Return new dicts/lists rather than mutating inputs
3. **Composition**: Small functions that can be combined
4. **Explicit errors**: Raise specific exceptions rather than returning None

### Core Functions

```python
# Path handling
resolve_vault_path(vault: Path | None = None) -> Path
sanitize_title(title: str) -> str

# CRUD operations
create_note(title, content, folder="Inbox", tags=None, vault=None) -> Path
read_note(title_or_path, folder=None, vault=None) -> dict
append_to_note(title_or_path, content, separator="\n", vault=None) -> dict

# Search and discovery
search_notes(query, folder=None, limit=10, vault=None) -> list[dict]
list_notes(folder=None, tag=None, limit=20, sort="modified", vault=None) -> dict
```

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Large vault performance | ripgrep handles large codebases; limit results |
| Concurrent access | Obsidian's conflict resolution is file-level |
| Sync conflicts | LiveSync handles conflicts; we append atomically |

## Future Considerations

- **MCP tool exposure**: vault_ops functions will become MCP tools in BIS-238+
- **Template support**: Can add template loading in future iterations
- **Wikilink resolution**: May add `[[wikilink]]` parsing if needed

## References

- [python-frontmatter](https://python-frontmatter.readthedocs.io/)
- [ripgrep](https://github.com/BurntSushi/ripgrep)
- [Obsidian vault format](https://help.obsidian.md/Files+and+folders/How+Obsidian+stores+data)
