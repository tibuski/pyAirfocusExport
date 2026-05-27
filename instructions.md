# Laws

- Use `uv` for Python dependency management, virtual environments, and running scripts.
- Store secrets and configuration in `config.py` (git-ignored). Keep `config.py.example` with dummy values as a template.
- Add `constants.py` to `.gitignore`.
- Fetch the latest API spec from https://developer.airfocus.com/openapi.json when working on new integrations.
- Reference general API docs at https://developer.airfocus.com.
- Keep this file up to date when adding features.
- Maintain `README.md` with concise technical documentation. No marketing or fluff.

# Project Goal

* Main goal is to export objective workspaces with hierarchy and corresponding key results in reporting-friendly CSV and JSON shapes.
* The result should support graphics, reporting, spreadsheet analysis, and AI-assisted visualization without requiring additional API queries per output file.
* Running `uv run pyAirfocusExport.py` without arguments should list the accessible objective workspace hierarchy.
* Export usage is `uv run pyAirfocusExport.py --parent "Objective Workspace Name"`
* `--parent` accepts either the full workspace name or the workspace short name / alias.
* By default, exporting with `--parent` writes five files under `Output/`: a legacy path CSV, normalized `-nodes.csv`, normalized `-edges.csv`, a management-friendly `-management.csv`, and a hierarchical `.json` file.

# Implementation Plan

## CSV Output Columns

The primary reporting output is normalized into `nodes` and `edges` CSV files. A spreadsheet-friendly `management.csv` and a hierarchical JSON export are also written from the same in-memory model. The legacy flat path CSV is still written for manual review.

The hierarchy represented by the normalized export is:

```
Objective Workspace (e.g., Company OKRs)
  └── Objective (parent objective, Parent_Objective)
        └── Child Objective (child objective within same workspace, Parent_ChildObjective)
              └── Key Results linked to child objective (Parent_KeyResult)
        └── Key Results linked to parent objective (Parent_KeyResult)
  └── Child Objective Workspace (e.g., Team OKRs)  →  Child0
        └── Objective (Child0_Objective)
              └── Child Objective (Child0_ChildObjective)
                    └── Key Results (Child0_KeyResult)
              └── Key Results (Child0_KeyResult)
```

Each `nodes.csv` row represents exactly one entity. Each `edges.csv` row represents one hierarchy relationship.

### Nodes CSV

| Column | Description |
|---|---|
| `Id` | Stable node identifier prefixed by entity type (`workspace:`, `item:`, `kr:`) |
| `NodeType` | `workspace`, `objective`, or `key_result` |
| `HierarchyRole` | `workspace`, `objective`, `child_objective`, or `key_result` |
| `WorkspaceId` | Owning workspace UUID |
| `WorkspaceName` | Owning workspace name |
| `Title` | Entity title or name |
| `Alias` | Workspace alias or item/key result alias when available |
| `StatusId` | airfocus status UUID when available in the payload |
| `Confidence` | OKR confidence level |
| `Progress` | Progress value |
| `TimePeriod` | OKR time period |
| `CreatedAt` | Creation timestamp when available |
| `UpdatedAt` | Update timestamp when available |
| `Archived` | `true` / `false` when available |
| `AssigneeUserIds` | Comma-separated assignee user IDs |

### Edges CSV

| Column | Description |
|---|---|
| `SourceId` | Parent node ID |
| `TargetId` | Child node ID |
| `RelationType` | `workspace_child`, `workspace_objective`, `objective_child`, or `objective_key_result` |
| `WorkspaceId` | Workspace UUID where the edge was discovered |
| `WorkspaceName` | Workspace name where the edge was discovered |

### Management CSV

| Column | Description |
|---|---|
| `Workspace` | Workspace name |
| `Objective` | Objective name |
| `ChildObjective` | Child objective name when applicable |
| `KeyResult` | Key result title when applicable |
| `Level` | Hierarchy role (`objective`, `child_objective`, `key_result`) |
| `NodeType` | Export row type |
| `StatusId` | airfocus status UUID when available in the payload |
| `Confidence` | OKR confidence |
| `Progress` | Progress value |
| `TimePeriod` | OKR time period |

### JSON Export

The JSON export mirrors the workspace → objective → child objective → key result hierarchy as nested objects. It is intended for AI-assisted graphics generation and other consumers that work better with explicit nested children than with joined CSV files.

### Legacy Path Columns

The top-level workspace columns use `Parent`. Deeper workspace levels use child prefixes such as `Child0`, `Child0-0`, `Child0-0-0`.

| Column | Description |
|---|---|
| `Parent` / `Child...` | Name of the objective workspace at this depth |
| `Parent_Objective` / `Child..._Objective` | Name of the parent objective in this workspace |
| `Parent_ChildObjective` / `Child..._ChildObjective` | Name of a child objective (within same workspace, under the parent objective) |
| `Parent_KeyResult` / `Child..._KeyResult` | Alias (e.g., `OKR-42`) when available, otherwise the key result title |

If an objective has no child objective, `ChildObjective` is left empty and `KeyResult` applies to the parent objective directly.

Example for a 2-level hierarchy: `Parent`, `Parent_Objective`, `Parent_ChildObjective`, `Parent_KeyResult`, `Child0`, `Child0_Objective`, `Child0_ChildObjective`, `Child0_KeyResult`.

### Static Columns

| Column | Description |
|---|---|
| `Status` | Empty placeholder column kept for backward compatibility |
| `Confidence` | OKR confidence level (high/medium/low) |
| `Progress` | Progress percentage |
| `TimePeriod` | OKR time period |

## Required API Endpoints

### 1. Search Workspaces
`POST /api/workspaces/search`

Finds the parent objective workspace by name. Send an empty body or a filter matching the workspace name. Returns a paginated list of `WorkspaceWithWorkspaceEmbed` objects. Filter results to only those with `namespace == "app:okr"`.

### 2. Get Workspace by ID
`GET /api/workspaces/{workspaceId}`

Returns details of a single workspace, including its `namespace` (`"app:okr"` = objective workspace, `"main"` = regular, `"app:portfolio"` = portfolio), `name`, `alias`, and `_embedded`.

### 2b. List Workspaces by ID
`POST /api/workspaces/list`

Takes an array of workspace UUIDs in the request body and returns their details. Used to enrich accessible objective workspaces with `_embedded.parents`, `_embedded.children`, and OKR app settings so the no-argument hierarchy can be built locally. It is also used to resolve child objective workspaces discovered while traversing item relationships during export.

### 3. Search Items in a Workspace
`POST /api/workspaces/{workspaceId}/items/search?offset=0&limit=1000`

Returns items with key properties:
- `id`, `name`, `number`, `workspaceId`, `statusId`
- `fields`: custom field values keyed by field ID — includes special OKR field types
- `_embedded.children`: array of child items (each with `id`, `name`)
- `_embedded.parents`: array of parent items
- `_embedded.progress`: progress info with `percentage`, `doneCount`, `totalCount`

### 4. List Specific Items
`POST /api/workspaces/{workspaceId}/items/list`

Takes an array of item UUIDs in the request body and returns their full details. Used to resolve child workspace items or key results after discovering their IDs.

### 5. Search Fields
`POST /api/fields/search`

Discovers which custom fields are installed in a workspace, including OKR-specific fields (`okr-key-results`, `okr-key-result-reference`, `okr-confidence`, `okr-progress`, `okr-time-period`). Required to interpret the raw `fields` map on items.

## Key OKR Field Types

When an item's `fields` map contains these type IDs, their values need special handling:

| Field Type (`typeId`) | Value Shape in `fields` | What to Extract |
|---|---|---|
| `okr-key-results` | `{"itemIds": ["uuid", ...]}` | Array of key result item UUIDs |
| `okr-key-result-reference` | `{"itemId": "uuid"}` or `{"workspaceId": "uuid", "itemId": "uuid"}` | Reference to a key result in another workspace |
| `okr-confidence` | `{"value": "high"\|"medium"\|"low"}` | Confidence level |
| `okr-progress` | `{"percentage": 0-100}` | Progress percentage |
| `okr-time-period` | `{"from": "date", "to": "date"}` or `{"periodId": "uuid"}` | Time period range |

## Module Structure

```
pyAirfocusExport.py          # Entry point — CLI arg parsing, orchestrator
config.py                    # API key and base URL (gitignored)
```

`config.py` should contain:
```python
apikey = "your_api_key_here"
baseurl = "https://app.airfocus.com"
airfocus_coproduct = "v2"
ignore_ssl_cert_check = True
```

## Authentication

Send the API key in every request as an HTTP header:
```
Authorization: Bearer <apikey>
```
Also send `x-airfocus-coproduct: v2` by default. If airfocus support asks to temporarily restore the legacy behavior, switch `airfocus_coproduct` in `config.py` to `"v1"`.
Use `Content-Type: application/json` and accept `application/json` (or `application/vnd.airfocus.markdown+json` for markdown descriptions).

## Algorithm

1. **Bootstrap** — Read `apikey` and `baseurl` from `config.py`.

2. **List accessible objective workspaces when no parent is provided** — If `--parent` is omitted, search workspaces with an empty filter, keep only those with `namespace == "app:okr"`, load their full workspace details with `POST /api/workspaces/list`, derive parent/child links from workspace `_embedded.parents`, `_embedded.children`, and OKR app hierarchy settings, print the hierarchy, and exit without exporting CSV.

3. **Find parent objective workspace** — Load the accessible objective workspaces and match the `--parent` argument locally against either the workspace name or alias (case-insensitive). If not found, list all accessible objective workspaces to help the user. This is `Parent0`.

4. **Build workspace hierarchy** — Recursively traverse from the parent workspace using workspace metadata:
      - Load accessible OKR workspaces and derive parent/child workspace links from `_embedded.parents`, `_embedded.children`, and OKR app hierarchy settings.
      - Start from the selected parent workspace and recurse through its child objective workspaces.
      - For each workspace in that tree, fetch its items via `POST /api/workspaces/{id}/items/search`.

5. **Extract items and key results** — For each workspace in the hierarchy:
   - Fetch all items via `POST /api/workspaces/{id}/items/search`.
      - For each item, read the `okr-key-results` and `okr-key-result-reference` fields to get linked key result UUIDs.
      - Also resolve key results from linked workspaces configured in the OKR app when those items reference the objective through the linked workspace `keyResultReferenceFieldId`.
      - Resolve key result details via `POST /api/workspaces/{workspaceId}/items/list` when the key result item IDs are already known.
   - Read `okr-confidence`, `okr-progress`, and `okr-time-period` from item fields.

6. **Build parent-child relationships** — Use `_embedded.parents` and `_embedded.children` on each item to reconstruct the hierarchy.

7. **Write export files**
      - Build `nodes.csv` with one row per workspace, objective, child objective, and key result.
      - Build `edges.csv` with explicit parent-child links between workspaces, objectives, child objectives, and key results.
      - Build `management.csv` for spreadsheet-oriented filtering and pivoting.
      - Build hierarchical `.json` from the same in-memory export model for AI and nested consumers.
      - Also write the legacy path CSV for manual review and backward compatibility.

## Error Handling

- If `config.py` is missing or malformed, print a clear error and exit.
- If no `--parent` argument is provided, print the accessible objective workspace hierarchy and exit successfully.
- If the parent workspace name is not found, print available objective workspaces (`namespace == "app:okr"`) and exit.
- If TLS interception breaks certificate validation, allow `ignore_ssl_cert_check = True` in `config.py`.
- Paginate results (max 1000 per page) to handle large workspaces.
- Handle HTTP errors (401 = bad API key, 403 = insufficient permissions, 429 = rate limit).
- Log progress to stderr (e.g., "Fetching workspace X...", "Fetching items...") so generated files remain the only structured output.

## Dependencies

Only use the Python standard library:
- `urllib.request` or `http.client` for HTTP requests
- `json` for parsing responses
- `csv` for writing output
- `argparse` for CLI argument parsing

No third-party packages needed.
