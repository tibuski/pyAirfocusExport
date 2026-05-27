# pyAirfocusExport

Export airfocus OKR workspaces with hierarchy and key results to CSV.

## Usage

```sh
uv run pyAirfocusExport.py --root "Objective Workspace Name" > output.csv
```

## Setup

1. Copy `config.py.example` to `config.py` and fill in your API key.
2. Run with `uv`.

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
| `Status` | Status of the deepest objective |
| `Confidence` | OKR confidence (high/medium/low) |
| `Progress` | Progress percentage |
| `TimePeriod` | OKR time period |

## API

- API key: Bearer token in `Authorization` header
- Base URL: `https://app.airfocus.com`
- Full spec: `https://developer.airfocus.com/openapi.json`
