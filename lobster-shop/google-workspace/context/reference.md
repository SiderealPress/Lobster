# Google Workspace — Quick Reference

## Auth check

```python
from integrations.google_workspace.token_store import load_token
token = load_token(user_id)
is_authenticated = token is not None
```

## Token location

`~/messages/config/workspace-tokens/{user_id}.json` (0o600)

## Function signatures

### Docs (Slice 2 — read; Slice 3 — write)

```python
from integrations.google_workspace.docs_client import gdocs_read, gdocs_create, gdocs_edit
from integrations.google_workspace.docs_client import DocFile

# Read — returns plain text or None
content: str | None = gdocs_read(user_id: str, doc_id_or_url: str)

# Create — returns DocFile or None
doc: DocFile | None = gdocs_create(user_id: str, title: str, content: str = "")
# DocFile.id, DocFile.title, DocFile.url

# Edit — returns bool
ok: bool = gdocs_edit(user_id: str, doc_id: str, instructions: str)
```

### Drive (Slice 4)

```python
from integrations.google_workspace.drive_client import gdrive_list, gdrive_search
from integrations.google_workspace.drive_client import DriveFile

files: list[DriveFile] = gdrive_list(user_id: str, folder_id: str = "root", max_results: int = 20)
files: list[DriveFile] = gdrive_search(user_id: str, query: str, max_results: int = 10)
# DriveFile.id, DriveFile.name, DriveFile.mime_type, DriveFile.modified_time, DriveFile.url
```

### Sheets (Slices 5 + 6)

```python
from integrations.google_workspace.sheets_client import gsheets_read, gsheets_write, gsheets_create

rows: list[list[str]] = gsheets_read(user_id: str, sheet_id: str, range_a1: str)
ok: bool = gsheets_write(user_id: str, sheet_id: str, range_a1: str, values: list[list])
sheet: DriveFile | None = gsheets_create(user_id: str, title: str)
```

## Consent flow (unauthenticated)

```python
from integrations.google_auth.consent import generate_consent_link
url = generate_consent_link("workspace")  # one-time URL, 30-min TTL
```

## Scope bundle

The workspace consent grants: Docs, Drive, Drive.file, Sheets, Gmail, Calendar.
