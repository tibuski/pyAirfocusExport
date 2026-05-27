# pyAirfocusExport

Export airfocus OKR workspaces to reporting-friendly CSV and JSON files.

## Usage

```sh
uv run pyAirfocusExport.py
uv run pyAirfocusExport.py --parent "Objective Workspace Name"
uv run pyAirfocusExport.py --parent "WORKSPACEALIAS"
```

Run without arguments to list the objective workspace hierarchy you can access.

## Setup

1. Install `uv` if it is not already available.
2. Create a local virtual environment with `uv venv`.
3. Copy `config.py.example` to `config.py` and fill in your API key.
4. Run the exporter with `uv run`.

Example setup on Windows PowerShell:

```powershell
uv --version
uv venv
Copy-Item config.py.example config.py
uv run pyAirfocusExport.py
uv run pyAirfocusExport.py --parent "Objective Workspace Name"
```

`config.py` must define:

```python
apikey = "your_api_key_here"
baseurl = "https://app.airfocus.com"
airfocus_coproduct = "v2"
ignore_ssl_cert_check = True
```

If airfocus support asks you to temporarily restore legacy filter behavior, set `airfocus_coproduct = "v1"`.

## Dependencies

Python standard library only. No third-party packages.

## Output Files

Running with `--parent` now writes five files to `Output/` with the same timestamped prefix:

- `[date-time]-[parent-name].csv` — legacy flat path export, kept for spreadsheet-style review
- `[date-time]-[parent-name]-nodes.csv` — normalized reporting export with one row per workspace, objective, child objective, or key result
- `[date-time]-[parent-name]-edges.csv` — explicit hierarchy edges between workspaces, objectives, child objectives, and key results
- `[date-time]-[parent-name]-management.csv` — management-friendly flat export for Excel-style filtering, sorting, and pivoting
- `[date-time]-[parent-name].json` — hierarchical JSON export for AI-assisted graphics and nested consumers

Use `nodes` and `edges` for BI/reporting, `management.csv` for spreadsheet-oriented consumption, and the JSON export for AI or tools that prefer nested data.

### Nodes CSV

| Column | Description |
|---|---|
| `Id` | Stable export node identifier with a type prefix such as `workspace:`, `item:`, or `kr:` |
| `NodeType` | `workspace`, `objective`, or `key_result` |
| `HierarchyRole` | `workspace`, `objective`, `child_objective`, or `key_result` |
| `WorkspaceId` | Owning workspace UUID |
| `WorkspaceName` | Owning workspace name |
| `Title` | Entity title or name |
| `Alias` | Workspace alias or objective/key result alias when available |
| `StatusId` | airfocus status UUID when present in the payload |
| `Confidence` | OKR confidence (high/medium/low) |
| `Progress` | Progress value from the OKR payload |
| `TimePeriod` | OKR time period |
| `CreatedAt` | Entity creation timestamp when present |
| `UpdatedAt` | Entity update timestamp when present |
| `Archived` | `true` / `false` when present |
| `AssigneeUserIds` | Comma-separated assignee user IDs |

### Edges CSV

| Column | Description |
|---|---|
| `SourceId` | Parent node ID |
| `TargetId` | Child node ID |
| `RelationType` | `workspace_child`, `workspace_objective`, `objective_child`, or `objective_key_result` |
| `WorkspaceId` | Workspace UUID where the relation was discovered |
| `WorkspaceName` | Workspace name where the relation was discovered |

### Management CSV

| Column | Description |
|---|---|
| `Workspace` | Workspace name |
| `Objective` | Objective name |
| `ChildObjective` | Child objective name when applicable |
| `KeyResult` | Key result title when applicable |
| `Level` | Hierarchy role (`objective`, `child_objective`, `key_result`) |
| `NodeType` | Export row type for spreadsheet filtering |
| `StatusId` | airfocus status UUID when present in the payload |
| `Confidence` | OKR confidence |
| `Progress` | Progress value |
| `TimePeriod` | OKR time period |

### JSON Export

The JSON export writes the same hierarchy as a nested structure rooted at workspaces. It is intended for AI-assisted graphics generation and tools that prefer explicit nested children over CSV joins.

### Legacy Path CSV

The legacy path CSV remains available for manual review. It keeps the previous dynamic `Parent` / `Child...` columns and still outputs one row per workspace → objective → child objective → key result path.

## API

- API key: Bearer token in `Authorization` header
- Content type: `application/json`
- Optional coproduct pin: `x-airfocus-coproduct: v2` by default, with `v1` available as a fallback
- Base URL: `https://app.airfocus.com`
- Full spec: `https://developer.airfocus.com/openapi.json`

## Runtime behavior

- Running without `--parent` lists the accessible objective workspace hierarchy from workspace metadata and exits
- Running with `--parent` writes five files: legacy path CSV, normalized `-nodes.csv`, normalized `-edges.csv`, management-friendly `-management.csv`, and hierarchical `.json`
- Logs progress and API errors to stderr
- Paginates workspace items with a page size of 1000
- Resolves key results from objective OKR fields and from linked workspace items that reference those objectives
- Accepts either the full workspace name or the workspace short name / alias in `--parent`
- Prints available objective workspaces when `--parent` is not found
- Supports `ignore_ssl_cert_check = True` in `config.py` for environments with intercepted TLS certificates
