# Laws

- Use `uv` for Python dependency management, virtual environments, and running scripts.
- Store secrets and configuration in `config.py` (git-ignored). Keep `config.py.example` with dummy values as a template.
- Add `constants.py` to `.gitignore`.
- Fetch the latest API spec from https://developer.airfocus.com/openapi.json when working on new integrations.
- Reference general API docs at https://developer.airfocus.com.
- Keep this file up to date when adding features.
- Maintain `README.md` with concise technical documentation. No marketing or fluff.

# Project Goal

* Main goal is to export objective workspaces with hierarchy and corresponding key results for each level of objective workspace.
* The result should be a .csv file that will be used to create graphics and reports.
* Running `uv run pyAirfocusExport.py` without arguments should list the accessible parent objective workspaces.
* Export usage is `uv run pyAirfocusExport.py --root "Objective Workspace Name"`

# Implementation Plan

## CSV Output Columns

Columns are generated dynamically based on the hierarchy depth. The hierarchy is:

```
Objective Workspace (e.g., Company OKRs)
  └── Objective Item (parent objective, Parent0_Item)
        └── Child Objective Item (child objective within same workspace, Parent0_ChildItem)
              └── Key Results linked to child objective (Parent0_KeyResult)
        └── Key Results linked to parent objective (Parent0_KeyResult)
  └── Child Objective Workspace (e.g., Team OKRs)  →  Parent1
        └── Objective Item (Parent1_Item)
              └── Child Objective Item (Parent1_ChildItem)
                    └── Key Results (Parent1_KeyResult)
              └── Key Results (Parent1_KeyResult)
```

Each row represents a path through the hierarchy: workspace → objective → child objective → key result.

### Dynamic Path Columns

For each depth level N (0 = outermost objective workspace, increasing inward):

| Column | Description |
|---|---|
| `Parent{N}` | Name of the objective workspace at this depth |
| `Parent{N}_Item` | Name of the parent objective item in this workspace |
| `Parent{N}_ChildItem` | Name of a child objective (within same workspace, under the parent item) |
| `Parent{N}_KeyResult` | Alias (e.g., `OKR-42`) of the key result linked to the deepest objective |

If an objective has no child item, `ChildItem` is left empty and `KeyResult` applies to the parent objective directly.

Example for a 2-level hierarchy: `Parent0`, `Parent0_Item`, `Parent0_ChildItem`, `Parent0_KeyResult`, `Parent1`, `Parent1_Item`, `Parent1_ChildItem`, `Parent1_KeyResult`.

### Static Columns

| Column | Description |
|---|---|
| `Status` | Current status of the deepest objective |
| `Confidence` | OKR confidence level (high/medium/low) |
| `Progress` | Progress percentage |
| `TimePeriod` | OKR time period |

## Required API Endpoints

### 1. Search Workspaces
`POST /api/workspaces/search`

Finds the root objective workspace by name. Send an empty body or a filter matching the workspace name. Returns a paginated list of `WorkspaceWithWorkspaceEmbed` objects. Filter results to only those with `namespace == "app:okr"`.

### 2. Get Workspace by ID
`GET /api/workspaces/{workspaceId}`

Returns details of a single workspace, including its `namespace` (`"app:okr"` = objective workspace, `"main"` = regular, `"app:portfolio"` = portfolio), `name`, `alias`, and `_embedded`.

### 2b. List Workspaces by ID
`POST /api/workspaces/list`

Takes an array of workspace UUIDs in the request body and returns their details. Used to resolve child objective workspaces discovered while traversing item relationships.

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

### 5. Get Workspace Statuses
`GET /api/workspaces/{workspaceId}/statuses`

Returns the status definitions for a workspace, mapping `statusId` values to human-readable names and categories.

### 6. Search Fields
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
ignore_ssl_cert_check = True
```

## Authentication

Send the API key in every request as an HTTP header:
```
Authorization: Bearer <apikey>
```
Use `Content-Type: application/json` and accept `application/json` (or `application/vnd.airfocus.markdown+json` for markdown descriptions).

## Algorithm

1. **Bootstrap** — Read `apikey` and `baseurl` from `config.py`.

2. **List accessible objective workspaces when no root is provided** — If `--root` is omitted, search workspaces with an empty filter, keep only those with `namespace == "app:okr"`, print their names, and exit without exporting CSV.

3. **Find root objective workspace** — Search workspaces by name (the `--root` argument). Filter to only those with `namespace == "app:okr"`. If not found, list all accessible objective workspaces to help the user. This is `Parent0`.

4. **Build workspace hierarchy** — Recursively traverse from the root:
   - Search items in current workspace.
   - Each item may have parent/child objectives within the same workspace (via `_embedded.parents`/`_embedded.children`).
   - Each item's children may reference a child objective workspace (via `workspaceId` differing from the current).
   - Collect all discovered child workspace IDs, resolve them via `POST /api/workspaces/list`, and recurse. These become `Parent1`, `Parent2`, etc.

5. **Extract items and key results** — For each workspace in the hierarchy:
   - Fetch all items via `POST /api/workspaces/{id}/items/search`.
   - For each item, read the `okr-key-results` field to get linked key result UUIDs.
   - Resolve key result details via `POST /api/workspaces/{workspaceId}/items/list`.
   - Read `okr-confidence`, `okr-progress`, and `okr-time-period` from item fields.
   - Map `statusId` to human-readable status via the workspace statuses endpoint.

6. **Build parent-child relationships** — Use `_embedded.parents` and `_embedded.children` on each item to reconstruct the hierarchy.

7. **Write CSV** — Build the column list dynamically based on max hierarchy depth. For each path from root to leaf, output one row per key result at each level. Use `csv.DictWriter`.

## Error Handling

- If `config.py` is missing or malformed, print a clear error and exit.
- If no `--root` argument is provided, print the accessible objective workspaces and exit successfully.
- If the root workspace name is not found, print available objective workspaces (`namespace == "app:okr"`) and exit.
- If TLS interception breaks certificate validation, allow `ignore_ssl_cert_check = True` in `config.py`.
- Paginate results (max 1000 per page) to handle large workspaces.
- Handle HTTP errors (401 = bad API key, 403 = insufficient permissions, 429 = rate limit).
- Log progress to stderr (e.g., "Fetching workspace X...", "Fetching items...") so the CSV on stdout/stderr is clean.

## Dependencies

Only use the Python standard library:
- `urllib.request` or `http.client` for HTTP requests
- `json` for parsing responses
- `csv` for writing output
- `argparse` for CLI argument parsing

No third-party packages needed.
