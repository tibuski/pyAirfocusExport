# pyAirfocusExport

Export airfocus OKR workspaces with hierarchy and key results to CSV.

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
ignore_ssl_cert_check = True
```

## Dependencies

Python standard library only. No third-party packages.

## CSV Output

Dynamic columns per hierarchy depth:

| Column | Description |
|---|---|
| `Parent{N}` | Objective workspace name at depth N |
| `Parent{N}_Item` | Objective item name |
| `Parent{N}_ChildItem` | Child objective (within same workspace) |
| `Parent{N}_KeyResult` | Key result alias linked to the objective |
| `Status` | Empty placeholder column; workspace status metadata is not queried |
| `Confidence` | OKR confidence (high/medium/low) |
| `Progress` | Progress percentage |
| `TimePeriod` | OKR time period |

## API

- API key: Bearer token in `Authorization` header
- Content type: `application/json`
- Base URL: `https://app.airfocus.com`
- Full spec: `https://developer.airfocus.com/openapi.json`

## Runtime behavior

- Running without `--parent` lists the accessible objective workspace hierarchy from workspace metadata and exits
- Running with `--parent` writes a CSV file named `Output\[date-time]-[parent-name].csv`
- Logs progress and API errors to stderr
- Paginates workspace items with a page size of 1000
- Accepts either the full workspace name or the workspace short name / alias in `--parent`
- Prints available objective workspaces when `--parent` is not found
- Supports `ignore_ssl_cert_check = True` in `config.py` for environments with intercepted TLS certificates
