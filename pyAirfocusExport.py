import argparse
import csv
from datetime import datetime
import importlib.util
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

CONTENT_TYPE = "application/json"
FIELD_OKR_KEY_RESULTS = "okr-key-results"
FIELD_OKR_KEY_RESULT_REF = "okr-key-result-reference"
FIELD_OKR_CONFIDENCE = "okr-confidence"
FIELD_OKR_PROGRESS = "okr-progress"
FIELD_OKR_TIME_PERIOD = "okr-time-period"

OKR_FIELD_TYPES = {
    FIELD_OKR_KEY_RESULTS,
    FIELD_OKR_KEY_RESULT_REF,
    FIELD_OKR_CONFIDENCE,
    FIELD_OKR_PROGRESS,
    FIELD_OKR_TIME_PERIOD,
}

HTTP_ERROR_MESSAGES = {
    401: "Unauthorized: verify the API key in config.py.",
    403: "Forbidden: the API key does not have permission to access this resource.",
    429: "Rate limit reached: retry later or slow down requests.",
}

SSL_ERROR_HINT = (
    "SSL verification failed. Set 'ignore_ssl_cert_check = True' in config.py "
    "or configure a trusted corporate/root CA."
)

class ExporterError(Exception):
    pass


def obfuscate_secret(value, prefix_length=8, suffix_length=4):
    text = str(value or "")
    if not text:
        return "<empty>"
    if len(text) <= prefix_length + suffix_length:
        return "xxxx"
    return f"{text[:prefix_length]}xxxx{text[-suffix_length:]}"

def load_config():
    config_path = os.path.join(os.path.dirname(__file__), "config.py")
    if not os.path.exists(config_path):
        print(
            "Error: config.py is missing. Copy config.py.example to config.py and fill in your values.",
            file=sys.stderr,
        )
        sys.exit(1)

    spec = importlib.util.spec_from_file_location("config", config_path)
    if spec is None or spec.loader is None:
        print("Error: could not load config.py.", file=sys.stderr)
        sys.exit(1)

    config_module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(config_module)
    except Exception as exc:
        print(f"Error: failed to load config.py: {exc}", file=sys.stderr)
        sys.exit(1)

    missing = [name for name in ("apikey", "baseurl") if not hasattr(config_module, name)]
    if missing:
        print(
            "Error: config.py must define 'apikey' and 'baseurl'. "
            "Copy config.py.example to config.py and fill in your values.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not hasattr(config_module, "ignore_ssl_cert_check"):
        config_module.ignore_ssl_cert_check = True
    if not hasattr(config_module, "airfocus_coproduct"):
        config_module.airfocus_coproduct = "v2"

    print(
        f"Loaded config.py with apikey={obfuscate_secret(config_module.apikey)}",
        file=sys.stderr,
    )

    return config_module


def unwrap_items_payload(payload):
    if isinstance(payload, dict):
        return payload.get("items", [])
    if isinstance(payload, list):
        return payload
    return []


def extract_api_error_message(body_text):
    try:
        payload = json.loads(body_text)
    except json.JSONDecodeError:
        return body_text.strip()

    message = payload.get("message")
    if message:
        return message
    code = payload.get("code")
    if code:
        return str(code)
    return body_text.strip()


def api_request(config, method, path, body=None, params=None):
    url = f"{config.baseurl}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"

    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {config.apikey}")
    req.add_header("Content-Type", CONTENT_TYPE)
    req.add_header("Accept", CONTENT_TYPE)
    coproduct = getattr(config, "airfocus_coproduct", None)
    if coproduct:
        req.add_header("x-airfocus-coproduct", str(coproduct))

    ssl_context = None
    if getattr(config, "ignore_ssl_cert_check", True):
        ssl_context = ssl._create_unverified_context()

    try:
        with urllib.request.urlopen(req, context=ssl_context) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        detail = HTTP_ERROR_MESSAGES.get(e.code) or extract_api_error_message(body_text)
        raise ExporterError(
            f"API error {e.code} for {method} {path}: {detail}"
        ) from None
    except urllib.error.URLError as e:
        if isinstance(e.reason, ssl.SSLError):
            raise ExporterError(SSL_ERROR_HINT) from None
        raise ExporterError(
            f"Network error for {method} {path}: {e.reason}"
        ) from None


def search_workspaces(config, name=None):
    body = {}
    if name:
        body = {
            "filter": {
                "type": "and",
                "inner": [
                    {
                        "type": "name",
                        "mode": "equal",
                        "text": name,
                        "caseSensitive": False,
                    }
                ],
            }
        }
    result = api_request(config, "POST", "/api/workspaces/search", body=body)
    items = result.get("items", [])
    return [ws for ws in items if ws.get("namespace") == "app:okr"]


def get_workspace_label(workspace):
    alias = workspace.get("alias")
    label = workspace.get("name", workspace.get("id", "?"))
    if alias:
        return f"{label} ({alias})"
    return label


def get_workspace_alias(workspace):
    return workspace.get("alias") or ""


def get_okr_app_settings(workspace):
    embedded = workspace.get("_embedded", {})
    for app in normalize_embedded_collection(embedded.get("apps")):
        if app.get("typeId") == "okr":
            settings = app.get("settings")
            if isinstance(settings, dict):
                return settings
    return {}


def normalize_settings_collection(value):
    if isinstance(value, dict):
        return [item for item in value.values() if isinstance(item, dict)]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def find_workspace_settings_entry(settings_collection, workspace_id):
    if not workspace_id:
        return {}

    if isinstance(settings_collection, dict):
        direct = settings_collection.get(workspace_id)
        if isinstance(direct, dict):
            return direct

    for entry in normalize_settings_collection(settings_collection):
        if entry.get("workspaceId") == workspace_id:
            return entry
    return {}


def get_linked_workspace_kr_sources(workspace):
    settings = get_okr_app_settings(workspace)
    objective_workspaces = settings.get("objectiveWorkspaces", {})
    linked_workspaces = settings.get("linkedWorkspaces", {})

    workspace_settings = find_workspace_settings_entry(
        objective_workspaces,
        workspace.get("id"),
    )
    linked_workspace_ids = workspace_settings.get("linkedWorkspaceIds", [])

    sources = []
    for linked_workspace_id in linked_workspace_ids:
        linked_settings = find_workspace_settings_entry(
            linked_workspaces,
            linked_workspace_id,
        )
        reference_field_id = linked_settings.get("keyResultReferenceFieldId")
        if linked_workspace_id and reference_field_id:
            sources.append(
                {
                    "workspace_id": linked_workspace_id,
                    "reference_field_id": reference_field_id,
                }
            )

    if sources:
        return sources

    for linked_settings in normalize_settings_collection(linked_workspaces):
        linked_workspace_id = linked_settings.get("workspaceId")
        reference_field_id = linked_settings.get("keyResultReferenceFieldId")
        if linked_workspace_id and reference_field_id:
            sources.append(
                {
                    "workspace_id": linked_workspace_id,
                    "reference_field_id": reference_field_id,
                }
            )
    return sources


def sanitize_filename_component(value):
    text = str(value or "").strip()
    if not text:
        return "export"

    invalid_chars = '<>:"/\\|?*'
    sanitized = "".join("-" if char in invalid_chars else char for char in text)
    sanitized = " ".join(sanitized.split())
    sanitized = sanitized.replace(" ", "-").strip(".-")
    return sanitized or "export"


def get_default_output_path(workspace):
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    workspace_name = workspace.get("name") or workspace.get("alias") or workspace.get("id")
    safe_name = sanitize_filename_component(workspace_name)
    return os.path.join(os.getcwd(), "Output", f"{timestamp}-{safe_name}.csv")


def get_output_paths(workspace):
    legacy_path = get_default_output_path(workspace)
    prefix, _ = os.path.splitext(legacy_path)
    return {
        "paths": legacy_path,
        "nodes": f"{prefix}-nodes.csv",
        "edges": f"{prefix}-edges.csv",
        "management": f"{prefix}-management.csv",
        "json": f"{prefix}.json",
    }


def sort_workspaces(workspaces):
    return sorted(workspaces, key=lambda ws: ws.get("name", "").lower())


def normalize_embedded_collection(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        items = value.get("items")
        if isinstance(items, list):
            return items
        return [item for item in value.values() if isinstance(item, dict)]
    return []


def add_workspace_relationship(
    parent_workspace_map,
    child_workspace_map,
    parent_workspace_id,
    child_workspace_id,
):
    if (
        not parent_workspace_id
        or not child_workspace_id
        or parent_workspace_id == child_workspace_id
    ):
        return
    child_workspace_map.setdefault(parent_workspace_id, set()).add(child_workspace_id)
    parent_workspace_map.setdefault(child_workspace_id, set()).add(parent_workspace_id)


def discover_workspace_relationships(
    workspace_entry,
    accessible_workspace_ids,
    parent_workspace_map,
    child_workspace_map,
    current_parent_workspace_id=None,
):
    if not isinstance(workspace_entry, dict):
        return

    workspace_id = workspace_entry.get("workspaceId") or workspace_entry.get("id")
    if workspace_id not in accessible_workspace_ids:
        workspace_id = None

    if current_parent_workspace_id and workspace_id:
        add_workspace_relationship(
            parent_workspace_map,
            child_workspace_map,
            current_parent_workspace_id,
            workspace_id,
        )

    next_parent_workspace_id = workspace_id or current_parent_workspace_id
    for child_workspace in normalize_embedded_collection(workspace_entry.get("children")):
        discover_workspace_relationships(
            child_workspace,
            accessible_workspace_ids,
            parent_workspace_map,
            child_workspace_map,
            next_parent_workspace_id,
        )


def get_workspace_relationship_maps(workspaces):
    workspace_by_id = {workspace["id"]: workspace for workspace in workspaces}
    accessible_workspace_ids = set(workspace_by_id)
    child_workspace_map = {workspace_id: set() for workspace_id in workspace_by_id}
    parent_workspace_map = {workspace_id: set() for workspace_id in workspace_by_id}

    for workspace in workspaces:
        workspace_id = workspace["id"]
        embedded = workspace.get("_embedded", {})

        for app in normalize_embedded_collection(embedded.get("apps")):
            settings = app.get("settings", {})
            hierarchy = settings.get("hierarchy")
            if isinstance(hierarchy, dict):
                discover_workspace_relationships(
                    hierarchy,
                    accessible_workspace_ids,
                    parent_workspace_map,
                    child_workspace_map,
                )
            elif isinstance(hierarchy, list):
                for workspace_entry in hierarchy:
                    discover_workspace_relationships(
                        workspace_entry,
                        accessible_workspace_ids,
                        parent_workspace_map,
                        child_workspace_map,
                    )

        for child_workspace in normalize_embedded_collection(embedded.get("children")):
            child_workspace_id = child_workspace.get("id") or child_workspace.get("workspaceId")
            if child_workspace_id in accessible_workspace_ids:
                add_workspace_relationship(
                    parent_workspace_map,
                    child_workspace_map,
                    workspace_id,
                    child_workspace_id,
                )

        for parent_workspace in normalize_embedded_collection(embedded.get("parents")):
            parent_workspace_id = parent_workspace.get("id") or parent_workspace.get("workspaceId")
            if parent_workspace_id in accessible_workspace_ids:
                add_workspace_relationship(
                    parent_workspace_map,
                    child_workspace_map,
                    parent_workspace_id,
                    workspace_id,
                )

    sorted_child_workspace_map = {
        workspace_id: sorted(
            child_ids,
            key=lambda child_id: workspace_by_id[child_id].get("name", "").lower(),
        )
        for workspace_id, child_ids in child_workspace_map.items()
    }
    return workspace_by_id, sorted_child_workspace_map, parent_workspace_map


def get_accessible_objective_workspaces(config, name=None):
    workspaces = search_workspaces(config, name=name)
    if not workspaces:
        return []

    workspace_ids = [workspace["id"] for workspace in workspaces if workspace.get("id")]
    if not workspace_ids:
        return workspaces

    detailed_workspaces = list_workspaces(config, workspace_ids)
    if not detailed_workspaces:
        return workspaces

    detailed_by_id = {
        workspace.get("id"): workspace
        for workspace in detailed_workspaces
        if isinstance(workspace, dict) and workspace.get("id")
    }
    return [detailed_by_id.get(workspace_id, {"id": workspace_id}) for workspace_id in workspace_ids]


def normalize_workspace_key(value):
    return str(value or "").strip().casefold()


def find_matching_workspaces(workspaces, query):
    normalized_query = normalize_workspace_key(query)
    if not normalized_query:
        return []

    return [
        workspace
        for workspace in workspaces
        if normalize_workspace_key(workspace.get("name")) == normalized_query
        or normalize_workspace_key(workspace.get("alias")) == normalized_query
    ]


def format_workspace_hierarchy_lines(
    workspace_by_id,
    child_workspace_map,
    root_workspace_ids,
):
    lines = []
    visited = set()

    def render_node(workspace_id, depth, path):
        workspace = workspace_by_id[workspace_id]
        indent = "  " * depth
        lines.append(f"{indent}- {get_workspace_label(workspace)}")
        rendered_ids = {workspace_id}

        if workspace_id in path:
            lines.append(f"{indent}  - [cycle detected]")
            return rendered_ids

        next_path = path | {workspace_id}
        for child_id in child_workspace_map.get(workspace_id, []):
            rendered_ids.update(render_node(child_id, depth + 1, next_path))
        return rendered_ids

    for workspace_id in root_workspace_ids:
        visited.update(render_node(workspace_id, 0, set()))

    remaining_ids = [
        workspace_id
        for workspace_id in sorted(
            workspace_by_id,
            key=lambda current_id: workspace_by_id[current_id].get("name", "").lower(),
        )
        if workspace_id not in root_workspace_ids
    ]

    for workspace_id in remaining_ids:
        if workspace_id in visited:
            continue
        visited.update(render_node(workspace_id, 0, set()))

    return lines


def print_accessible_objective_workspaces(config, stream):
    workspaces = get_accessible_objective_workspaces(config)
    if not workspaces:
        print("No accessible objective workspaces found.", file=stream)
        return 0

    print("Resolving accessible objective workspace hierarchy...", file=sys.stderr)
    workspace_by_id, child_workspace_map, parent_workspace_map = get_workspace_relationship_maps(workspaces)
    root_workspace_ids = [
        workspace["id"]
        for workspace in sort_workspaces(workspaces)
        if not parent_workspace_map.get(workspace["id"])
    ]
    if not root_workspace_ids:
        root_workspace_ids = [workspace["id"] for workspace in sort_workspaces(workspaces)]

    print("Accessible objective workspace hierarchy:", file=stream)
    for line in format_workspace_hierarchy_lines(
        workspace_by_id,
        child_workspace_map,
        root_workspace_ids,
    ):
        print(line, file=stream)
    return len(workspaces)


def get_workspace(config, workspace_id):
    return api_request(config, "GET", f"/api/workspaces/{workspace_id}")


def list_workspaces(config, workspace_ids):
    if not workspace_ids:
        return []
    return unwrap_items_payload(
        api_request(config, "POST", "/api/workspaces/list", body=workspace_ids)
    )


def search_items(config, workspace_id, query=None, offset=0, limit=1000):
    params = {"offset": offset, "limit": limit}
    return api_request(
        config,
        "POST",
        f"/api/workspaces/{workspace_id}/items/search",
        body=query or {},
        params=params,
    )


def list_items(config, workspace_id, item_ids):
    return unwrap_items_payload(
        api_request(config, "POST", f"/api/workspaces/{workspace_id}/items/list", body=item_ids)
    )


def search_fields(config, workspace_ids=None):
    body = {}
    if workspace_ids:
        body["workspaceIds"] = workspace_ids
    result = api_request(config, "POST", "/api/fields/search", body=body)
    return result.get("items", [])


def paginated_search_items(config, workspace_id, query=None):
    items = []
    offset = 0
    limit = 1000
    while True:
        page = search_items(config, workspace_id, query=query, offset=offset, limit=limit)
        batch = page.get("items", [])
        items.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
    return items


def build_field_type_map(config, workspace_ids, cache):
    cache_key = tuple(sorted(workspace_ids))
    if cache_key in cache:
        return cache[cache_key]

    all_fields = search_fields(config, workspace_ids)
    field_map = {}
    for field in all_fields:
        ft = field.get("typeId")
        if ft in OKR_FIELD_TYPES:
            field_map[field["id"]] = ft
    cache[cache_key] = field_map
    return field_map


def collect_item_refs(value, default_workspace_id=None):
    refs = []

    def visit(current):
        if current is None:
            return
        if isinstance(current, str):
            refs.append(current)
            return
        if isinstance(current, list):
            for item in current:
                visit(item)
            return
        if not isinstance(current, dict):
            return

        if "itemIds" in current:
            visit(current.get("itemIds"))
        if "items" in current:
            visit(current.get("items"))
        if "value" in current and not any(
            key in current for key in ("itemId", "workspaceId", "itemIds", "items")
        ):
            visit(current.get("value"))

        item_id = current.get("itemId") or current.get("id")
        workspace_id = current.get("workspaceId") or current.get("workspace", {}).get("id")
        if item_id:
            if workspace_id and workspace_id != default_workspace_id:
                refs.append((workspace_id, item_id))
            else:
                refs.append(item_id)

    visit(value)

    unique_refs = []
    seen = set()
    for ref in refs:
        if ref in seen:
            continue
        seen.add(ref)
        unique_refs.append(ref)
    return unique_refs


def collect_key_result_refs(value, default_workspace_id=None):
    return collect_item_refs(value, default_workspace_id=default_workspace_id)


def collect_objective_refs(value, default_workspace_id=None):
    return collect_item_refs(value, default_workspace_id=default_workspace_id)


def collect_key_result_entries(value):
    entries = []

    def visit(current):
        if current is None:
            return
        if isinstance(current, list):
            for item in current:
                visit(item)
            return
        if not isinstance(current, dict):
            return

        if isinstance(current.get("keyResults"), list):
            visit(current.get("keyResults"))
        if isinstance(current.get("linkedItems"), list):
            visit(current.get("linkedItems"))
        if isinstance(current.get("items"), list):
            visit(current.get("items"))
        if "value" in current and not any(
            key in current for key in ("id", "title", "keyResults", "items", "linkedItems")
        ):
            visit(current.get("value"))

        if current.get("id") and current.get("title"):
            entries.append(current)

    visit(value)

    unique_entries = []
    seen_ids = set()
    for entry in entries:
        entry_id = entry.get("id")
        if not entry_id or entry_id in seen_ids:
            continue
        seen_ids.add(entry_id)
        unique_entries.append(entry)
    return unique_entries


def extract_confidence_value(value):
    if not isinstance(value, dict):
        return None
    if value.get("value") is not None:
        return value.get("value")
    if value.get("confidence") is not None:
        return value.get("confidence")
    return None


def extract_progress_value(value):
    if not isinstance(value, dict):
        return None
    if value.get("percentage") is not None:
        return value.get("percentage")
    if value.get("value") is not None:
        return value.get("value")
    ratio = value.get("ratio")
    if isinstance(ratio, (int, float)):
        return int(round(ratio * 100))
    return None


def extract_time_period_value(value):
    if not isinstance(value, dict):
        return None
    if "from" in value and "to" in value:
        return f"{value['from']} - {value['to']}"
    if value.get("periodId"):
        return value.get("periodId")

    date_range = value.get("dateRange")
    if isinstance(date_range, dict):
        start_date = date_range.get("startDate")
        end_date = date_range.get("endDate")
        if start_date and end_date:
            return f"{start_date} - {end_date}"

    time_period_ids = value.get("timePeriodIds")
    if isinstance(time_period_ids, list) and time_period_ids:
        return ", ".join(str(period_id) for period_id in time_period_ids)
    return None


def get_latest_checkin(key_result):
    if not isinstance(key_result, dict):
        return None
    checkins = key_result.get("checkins", {})
    entries = checkins.get("checkins") if isinstance(checkins, dict) else None
    if not isinstance(entries, list) or not entries:
        return None

    def sort_key(entry):
        return entry.get("checkinAt") or entry.get("createdAt") or ""

    return max((entry for entry in entries if isinstance(entry, dict)), key=sort_key, default=None)


def get_key_result_row_values(key_result):
    latest_checkin = get_latest_checkin(key_result)

    confidence = extract_confidence_value(key_result.get("confidence"))
    if confidence is None and latest_checkin:
        confidence = extract_confidence_value(latest_checkin.get("confidence"))

    progress = extract_progress_value(key_result.get("progress"))
    if progress is None and latest_checkin:
        progress = extract_progress_value(latest_checkin.get("progress"))

    time_period = extract_time_period_value(key_result.get("timePeriod"))

    return {
        "confidence": confidence,
        "progress": progress,
        "time_period": time_period,
    }


def build_linked_key_result_map(config, workspace, objective_items, linked_workspace_item_cache):
    sources = get_linked_workspace_kr_sources(workspace)
    if not sources or not objective_items:
        return {}

    objective_ids = [item.get("id") for item in objective_items if item.get("id")]
    if not objective_ids:
        return {}

    objective_id_set = set(objective_ids)
    objective_workspace_id = workspace.get("id")
    linked_krs_by_objective = {}

    for source in sources:
        linked_workspace_id = source["workspace_id"]
        if linked_workspace_id not in linked_workspace_item_cache:
            linked_workspace_item_cache[linked_workspace_id] = paginated_search_items(
                config,
                linked_workspace_id,
            )

        linked_items = linked_workspace_item_cache[linked_workspace_id]

        for linked_item in linked_items:
            fields = linked_item.get("fields", {})
            refs = collect_objective_refs(
                fields.get(source["reference_field_id"]),
                default_workspace_id=objective_workspace_id,
            )

            matched_objective_ids = []
            for ref in refs:
                if isinstance(ref, tuple):
                    ref_workspace_id, ref_item_id = ref
                    if ref_workspace_id and ref_workspace_id != objective_workspace_id:
                        continue
                    objective_id = ref_item_id
                else:
                    objective_id = ref

                if objective_id in objective_id_set and objective_id not in matched_objective_ids:
                    matched_objective_ids.append(objective_id)

            for objective_id in matched_objective_ids:
                linked_krs_by_objective.setdefault(objective_id, []).append(linked_item)

    return linked_krs_by_objective


def extract_item_okr_fields(item, field_map):
    result = {
        "kr_ids": [],
        "kr_entries": [],
        "confidence": None,
        "progress": None,
        "time_period": None,
    }
    default_workspace_id = item.get("workspaceId")
    fields = item.get("fields", {})
    for field_id, value in fields.items():
        ftype = field_map.get(field_id)
        if ftype == FIELD_OKR_KEY_RESULTS:
            result["kr_entries"].extend(collect_key_result_entries(value))
            result["kr_ids"].extend(
                collect_key_result_refs(value, default_workspace_id=default_workspace_id)
            )
        elif ftype == FIELD_OKR_KEY_RESULT_REF:
            result["kr_ids"].extend(
                collect_key_result_refs(value, default_workspace_id=default_workspace_id)
            )
        elif ftype == FIELD_OKR_CONFIDENCE:
            result["confidence"] = extract_confidence_value(value)
        elif ftype == FIELD_OKR_PROGRESS:
            result["progress"] = extract_progress_value(value)
        elif ftype == FIELD_OKR_TIME_PERIOD:
            result["time_period"] = extract_time_period_value(value)
    return result


def resolve_items(config, workspace_id, item_ids, cache):
    if not item_ids:
        return []

    workspace_cache = cache.setdefault(workspace_id, {})
    resolved = []
    missing_ids = []
    for item_id in item_ids:
        cached_item = workspace_cache.get(item_id)
        if cached_item is not None:
            resolved.append(cached_item)
        else:
            missing_ids.append(item_id)

    if not missing_ids:
        return resolved

    resolved = []
    for i in range(0, len(missing_ids), 1000):
        batch = missing_ids[i : i + 1000]
        resolved.extend(list_items(config, workspace_id, batch))

    filtered = [item for item in resolved if item is not None]
    for item in filtered:
        workspace_cache[item["id"]] = item

    return [workspace_cache[item_id] for item_id in item_ids if item_id in workspace_cache]


def build_workspace_tree(
    config,
    parent_ws_id,
    workspace_by_id,
    workspace_child_map,
    visited=None,
):
    if visited is None:
        visited = set()
    if parent_ws_id in visited:
        return None
    visited.add(parent_ws_id)

    ws = workspace_by_id.get(parent_ws_id) or get_workspace(config, parent_ws_id)
    print(f"  Fetching items from workspace '{ws.get('name')}'...", file=sys.stderr)
    items = paginated_search_items(config, parent_ws_id)
    print(f"    Found {len(items)} items", file=sys.stderr)

    child_workspaces = []
    child_workspace_ids = [
        child_workspace_id
        for child_workspace_id in workspace_child_map.get(parent_ws_id, [])
        if child_workspace_id != parent_ws_id and child_workspace_id not in visited
    ]
    if child_workspace_ids:
        print(
            f"  Found {len(child_workspace_ids)} child workspaces, building hierarchy...",
            file=sys.stderr,
        )
        for child_workspace_id in child_workspace_ids:
            child = build_workspace_tree(
                config,
                child_workspace_id,
                workspace_by_id,
                workspace_child_map,
                visited,
            )
            if child:
                child_workspaces.append(child)

    return {
        "workspace": ws,
        "items": items,
        "children": child_workspaces,
    }


def get_item_alias(item):
    embed = item.get("_embedded", {})
    alias = embed.get("alias")
    if alias:
        return alias
    number = item.get("number")
    ws_alias = embed.get("workspaceItemType")
    if number is not None and ws_alias:
        return f"{ws_alias}-{number}"
    return str(number) if number is not None else item["id"]


def get_key_result_label(key_result):
    if not isinstance(key_result, dict):
        return str(key_result or "")

    embedded = key_result.get("_embedded")
    if isinstance(embedded, dict) or "number" in key_result:
        return get_item_alias(key_result)

    alias = key_result.get("alias")
    if alias:
        return alias

    title = key_result.get("title")
    if title:
        return title

    name = key_result.get("name")
    if name:
        return name

    return key_result.get("id", "")


def flatten_paths(
    config,
    workspace_tree,
    depth=0,
    workspace_path_context=None,
    field_map_cache=None,
    resolved_item_cache=None,
    linked_workspace_item_cache=None,
):
    if workspace_path_context is None:
        workspace_path_context = []
    if field_map_cache is None:
        field_map_cache = {}
    if resolved_item_cache is None:
        resolved_item_cache = {}
    if linked_workspace_item_cache is None:
        linked_workspace_item_cache = {}

    ws = workspace_tree["workspace"]
    ws_name = ws.get("name", "")
    items = workspace_tree["items"]
    children = workspace_tree["children"]

    field_map = build_field_type_map(config, [ws["id"]], field_map_cache)

    current_workspace_path = workspace_path_context + [
        {
            "ws_name": ws_name,
            "objective_name": None,
            "child_objective_name": None,
            "kr_alias": None,
            "status": None,
            "confidence": None,
            "progress": None,
            "time_period": None,
        }
    ]

    linked_krs_by_objective = build_linked_key_result_map(
        config,
        ws,
        items,
        linked_workspace_item_cache,
    )

    objective_key_result_rows = []
    for item in items:
        okr = extract_item_okr_fields(item, field_map)

        kr_ids = okr["kr_ids"]
        direct_krs = list(okr["kr_entries"])
        if kr_ids or direct_krs:
            local_ids = [i for i in kr_ids if isinstance(i, str)]
            cross_ws = [i for i in kr_ids if isinstance(i, tuple)]

            resolved_krs = list(direct_krs)
            if local_ids and not direct_krs:
                resolved_krs.extend(
                    resolve_items(config, ws["id"], local_ids, resolved_item_cache)
                )
            if not direct_krs:
                for rws_id, ritem_id in cross_ws:
                    if not rws_id:
                        continue
                    ritems = resolve_items(
                        config, rws_id, [ritem_id], resolved_item_cache
                    )
                    resolved_krs.extend(ritems)

            for linked_kr in linked_krs_by_objective.get(item["id"], []):
                linked_kr_id = linked_kr.get("id")
                if linked_kr_id and any(
                    resolved_kr.get("id") == linked_kr_id
                    for resolved_kr in resolved_krs
                    if isinstance(resolved_kr, dict)
                ):
                    continue
                resolved_krs.append(linked_kr)

            if resolved_krs:
                for kr in resolved_krs:
                    kr_alias = get_key_result_label(kr)
                    kr_values = get_key_result_row_values(kr)
                    objective_key_result_rows.append(
                        {
                            "item": item,
                            "child_item": None,
                            "kr_alias": kr_alias,
                            "status": "",
                            "confidence": kr_values["confidence"] if kr_values["confidence"] is not None else okr["confidence"],
                            "progress": kr_values["progress"] if kr_values["progress"] is not None else okr["progress"],
                            "time_period": kr_values["time_period"] or okr["time_period"],
                        }
                    )
            else:
                objective_key_result_rows.append(
                    {
                        "item": item,
                        "child_item": None,
                        "kr_alias": None,
                        "status": "",
                        "confidence": okr["confidence"],
                        "progress": okr["progress"],
                        "time_period": okr["time_period"],
                    }
                )
        else:
            linked_krs = linked_krs_by_objective.get(item["id"], [])
            if linked_krs:
                for kr in linked_krs:
                    kr_values = get_key_result_row_values(kr)
                    objective_key_result_rows.append(
                        {
                            "item": item,
                            "child_item": None,
                            "kr_alias": get_key_result_label(kr),
                            "status": "",
                            "confidence": kr_values["confidence"] if kr_values["confidence"] is not None else okr["confidence"],
                            "progress": kr_values["progress"] if kr_values["progress"] is not None else okr["progress"],
                            "time_period": kr_values["time_period"] or okr["time_period"],
                        }
                    )
                continue

            objective_key_result_rows.append(
                {
                    "item": item,
                    "child_item": None,
                    "kr_alias": None,
                    "status": "",
                    "confidence": okr["confidence"],
                    "progress": okr["progress"],
                    "time_period": okr["time_period"],
                }
            )

    workspace_item_parent_map = {}
    for item in items:
        embed = item.get("_embedded", {})
        parents_list = [
            p["itemId"]
            for p in embed.get("parents", [])
            if p.get("workspaceId") == ws["id"]
        ]
        workspace_item_parent_map[item["id"]] = parents_list

    root_objective_rows = [
        objective_row
        for objective_row in objective_key_result_rows
        if not workspace_item_parent_map.get(objective_row["item"]["id"])
    ]

    child_objective_map = {}
    for objective_row in objective_key_result_rows:
        item_id = objective_row["item"]["id"]
        parent_item_ids = workspace_item_parent_map.get(item_id, [])
        for parent_item_id in parent_item_ids:
            child_objective_map.setdefault(parent_item_id, []).append(objective_row)

    def build_objective_key_result_paths(
        root_objective_row,
        current_objective_row,
        current_workspace_level,
        child_objective_path=None,
        visited=None,
    ):
        if child_objective_path is None:
            child_objective_path = []
        if visited is None:
            visited = set()

        current_id = current_objective_row["item"]["id"]
        if current_id in visited:
            return

        child_objective_rows = child_objective_map.get(current_id, [])

        if current_objective_row["kr_alias"] is not None or not child_objective_rows:
            workspace_level_entry = current_workspace_level.copy()
            workspace_level_entry["objective_name"] = root_objective_row["item"].get("name", "")
            workspace_level_entry["child_objective_name"] = " > ".join(child_objective_path) or None
            workspace_level_entry["kr_alias"] = current_objective_row["kr_alias"]
            workspace_level_entry["status"] = current_objective_row["status"]
            workspace_level_entry["confidence"] = current_objective_row["confidence"]
            workspace_level_entry["progress"] = current_objective_row["progress"]
            workspace_level_entry["time_period"] = current_objective_row["time_period"]
            yield list(current_workspace_path[:-1]) + [workspace_level_entry]

        if child_objective_rows:
            next_visited = visited | {current_id}
            for child_objective_row in child_objective_rows:
                child_name = child_objective_row["item"].get("name", "")
                yield from build_objective_key_result_paths(
                    root_objective_row,
                    child_objective_row,
                    current_workspace_level,
                    child_objective_path + [child_name],
                    next_visited,
                )

    workspace_level = current_workspace_path[-1]
    if root_objective_rows:
        for root_objective_row in root_objective_rows:
            yield from build_objective_key_result_paths(
                root_objective_row,
                root_objective_row,
                workspace_level,
            )
    else:
        yield list(current_workspace_path[:-1]) + [workspace_level]

    if children:
        for child_workspace in children:
            yield from flatten_paths(
                config,
                child_workspace,
                depth + 1,
                current_workspace_path,
                field_map_cache,
                resolved_item_cache,
                linked_workspace_item_cache,
            )


def add_unique_row(rows, seen_keys, key, row):
    if key in seen_keys:
        return
    seen_keys.add(key)
    rows.append(row)


def get_same_workspace_parent_ids(item, workspace_id):
    parents = item.get("_embedded", {}).get("parents", [])
    return [
        parent.get("itemId")
        for parent in parents
        if parent.get("workspaceId") == workspace_id and parent.get("itemId")
    ]


def resolve_item_key_results(
    config,
    workspace,
    item,
    field_map,
    linked_krs_by_objective,
    resolved_item_cache,
):
    okr = extract_item_okr_fields(item, field_map)

    kr_ids = okr["kr_ids"]
    direct_krs = list(okr["kr_entries"])
    local_ids = [ref for ref in kr_ids if isinstance(ref, str)]
    cross_workspace_refs = [ref for ref in kr_ids if isinstance(ref, tuple)]

    resolved_krs = list(direct_krs)
    if local_ids and not direct_krs:
        resolved_krs.extend(resolve_items(config, workspace["id"], local_ids, resolved_item_cache))

    if not direct_krs:
        for ref_workspace_id, ref_item_id in cross_workspace_refs:
            if not ref_workspace_id:
                continue
            resolved_krs.extend(
                resolve_items(config, ref_workspace_id, [ref_item_id], resolved_item_cache)
            )

    for linked_kr in linked_krs_by_objective.get(item["id"], []):
        linked_kr_id = linked_kr.get("id")
        if linked_kr_id and any(
            isinstance(resolved_kr, dict) and resolved_kr.get("id") == linked_kr_id
            for resolved_kr in resolved_krs
        ):
            continue
        resolved_krs.append(linked_kr)

    return okr, resolved_krs


def build_reporting_rows(
    config,
    workspace_tree,
    parent_workspace_record_id=None,
    field_map_cache=None,
    resolved_item_cache=None,
    linked_workspace_item_cache=None,
    reporting_records=None,
    reporting_record_keys=None,
    relationship_rows=None,
    relationship_keys=None,
):
    if field_map_cache is None:
        field_map_cache = {}
    if resolved_item_cache is None:
        resolved_item_cache = {}
    if linked_workspace_item_cache is None:
        linked_workspace_item_cache = {}
    if reporting_records is None:
        reporting_records = []
    if reporting_record_keys is None:
        reporting_record_keys = set()
    if relationship_rows is None:
        relationship_rows = []
    if relationship_keys is None:
        relationship_keys = set()

    workspace = workspace_tree["workspace"]
    workspace_id = workspace["id"]
    workspace_name = workspace.get("name", "")
    workspace_record_id = f"workspace:{workspace_id}"

    add_unique_row(
        reporting_records,
        reporting_record_keys,
        workspace_record_id,
        {
            "Id": workspace_record_id,
            "NodeType": "workspace",
            "HierarchyRole": "workspace",
            "WorkspaceId": workspace_id,
            "WorkspaceName": workspace_name,
            "Title": workspace_name,
            "Alias": workspace.get("alias") or "",
            "StatusId": "",
            "Confidence": "",
            "Progress": "",
            "TimePeriod": "",
            "CreatedAt": "",
            "UpdatedAt": "",
            "Archived": "",
            "AssigneeUserIds": "",
        },
    )

    if parent_workspace_record_id:
        relationship_key = (
            parent_workspace_record_id,
            workspace_record_id,
            "workspace_child",
        )
        add_unique_row(
            relationship_rows,
            relationship_keys,
            relationship_key,
            {
                "SourceId": parent_workspace_record_id,
                "TargetId": workspace_record_id,
                "RelationType": "workspace_child",
                "WorkspaceId": workspace_id,
                "WorkspaceName": workspace_name,
            },
        )

    items = workspace_tree["items"]
    item_ids = {item.get("id") for item in items if item.get("id")}
    field_map = build_field_type_map(config, [workspace_id], field_map_cache)
    linked_krs_by_objective = build_linked_key_result_map(
        config,
        workspace,
        items,
        linked_workspace_item_cache,
    )

    for item in items:
        item_id = item.get("id")
        if not item_id:
            continue

        parent_item_ids = [
            parent_id
            for parent_id in get_same_workspace_parent_ids(item, workspace_id)
            if parent_id in item_ids
        ]
        okr, resolved_krs = resolve_item_key_results(
            config,
            workspace,
            item,
            field_map,
            linked_krs_by_objective,
            resolved_item_cache,
        )

        objective_record_id = f"item:{item_id}"
        hierarchy_role = "child_objective" if parent_item_ids else "objective"
        add_unique_row(
            reporting_records,
            reporting_record_keys,
            objective_record_id,
            {
                "Id": objective_record_id,
                "NodeType": "objective",
                "HierarchyRole": hierarchy_role,
                "WorkspaceId": workspace_id,
                "WorkspaceName": workspace_name,
                "Title": item.get("name") or "",
                "Alias": get_item_alias(item),
                "StatusId": item.get("statusId") or "",
                "Confidence": okr.get("confidence") or "",
                "Progress": okr.get("progress") if okr.get("progress") is not None else "",
                "TimePeriod": okr.get("time_period") or "",
                "CreatedAt": item.get("createdAt") or "",
                "UpdatedAt": item.get("lastUpdatedAt") or "",
                "Archived": str(bool(item.get("archived"))).lower(),
                "AssigneeUserIds": ",".join(item.get("assigneeUserIds", [])),
            },
        )

        if parent_item_ids:
            for parent_item_id in parent_item_ids:
                relationship_key = (
                    f"item:{parent_item_id}",
                    objective_record_id,
                    "objective_child",
                )
                add_unique_row(
                    relationship_rows,
                    relationship_keys,
                    relationship_key,
                    {
                        "SourceId": f"item:{parent_item_id}",
                        "TargetId": objective_record_id,
                        "RelationType": "objective_child",
                        "WorkspaceId": workspace_id,
                        "WorkspaceName": workspace_name,
                    },
                )
        else:
            relationship_key = (
                workspace_record_id,
                objective_record_id,
                "workspace_objective",
            )
            add_unique_row(
                relationship_rows,
                relationship_keys,
                relationship_key,
                {
                    "SourceId": workspace_record_id,
                    "TargetId": objective_record_id,
                    "RelationType": "workspace_objective",
                    "WorkspaceId": workspace_id,
                    "WorkspaceName": workspace_name,
                },
            )

        for key_result in resolved_krs:
            if not isinstance(key_result, dict):
                continue

            key_result_raw_id = key_result.get("id") or f"{item_id}:{get_key_result_label(key_result)}"
            key_result_record_id = f"kr:{key_result_raw_id}"
            key_result_values = get_key_result_row_values(key_result)
            key_result_workspace_id = key_result.get("workspaceId") or workspace_id
            key_result_workspace_name = workspace_name

            add_unique_row(
                reporting_records,
                reporting_record_keys,
                key_result_record_id,
                {
                    "Id": key_result_record_id,
                    "NodeType": "key_result",
                    "HierarchyRole": "key_result",
                    "WorkspaceId": key_result_workspace_id,
                    "WorkspaceName": key_result_workspace_name,
                    "Title": key_result.get("title") or key_result.get("name") or get_key_result_label(key_result),
                    "Alias": get_key_result_label(key_result),
                    "StatusId": key_result.get("statusId") or "",
                    "Confidence": key_result_values.get("confidence") or "",
                    "Progress": key_result_values.get("progress") if key_result_values.get("progress") is not None else "",
                    "TimePeriod": key_result_values.get("time_period") or "",
                    "CreatedAt": key_result.get("createdAt") or "",
                    "UpdatedAt": key_result.get("updatedAt") or "",
                    "Archived": str(bool(key_result.get("archived"))).lower(),
                    "AssigneeUserIds": ",".join(key_result.get("assigneeUserIds", [])),
                },
            )

            relationship_key = (
                objective_record_id,
                key_result_record_id,
                "objective_key_result",
            )
            add_unique_row(
                relationship_rows,
                relationship_keys,
                relationship_key,
                {
                    "SourceId": objective_record_id,
                    "TargetId": key_result_record_id,
                    "RelationType": "objective_key_result",
                    "WorkspaceId": workspace_id,
                    "WorkspaceName": workspace_name,
                },
            )

    for child_workspace in workspace_tree["children"]:
        build_reporting_rows(
            config,
            child_workspace,
            parent_workspace_record_id=workspace_record_id,
            field_map_cache=field_map_cache,
            resolved_item_cache=resolved_item_cache,
            linked_workspace_item_cache=linked_workspace_item_cache,
            reporting_records=reporting_records,
            reporting_record_keys=reporting_record_keys,
            relationship_rows=relationship_rows,
            relationship_keys=relationship_keys,
        )

    return reporting_records, relationship_rows


def write_reporting_nodes_csv(rows, outfile):
    fieldnames = [
        "Id",
        "NodeType",
        "HierarchyRole",
        "WorkspaceId",
        "WorkspaceName",
        "Title",
        "Alias",
        "StatusId",
        "Confidence",
        "Progress",
        "TimePeriod",
        "CreatedAt",
        "UpdatedAt",
        "Archived",
        "AssigneeUserIds",
    ]
    writer = csv.DictWriter(outfile, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)


def write_reporting_edges_csv(rows, outfile):
    fieldnames = [
        "SourceId",
        "TargetId",
        "RelationType",
        "WorkspaceId",
        "WorkspaceName",
    ]
    writer = csv.DictWriter(outfile, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)


def build_management_rows(reporting_records, relationship_rows):
    record_by_id = {record["Id"]: record for record in reporting_records}
    child_objective_ids_by_objective = {}
    key_result_ids_by_objective = {}

    for relationship in relationship_rows:
        relation_type = relationship["RelationType"]
        source_id = relationship["SourceId"]
        target_id = relationship["TargetId"]
        if relation_type == "objective_child":
            child_objective_ids_by_objective.setdefault(source_id, []).append(target_id)
        elif relation_type == "objective_key_result":
            key_result_ids_by_objective.setdefault(source_id, []).append(target_id)

    rows = []
    seen_keys = set()

    def add_row(key, row):
        if key in seen_keys:
            return
        seen_keys.add(key)
        rows.append(row)

    for objective_record in reporting_records:
        if objective_record["NodeType"] != "objective":
            continue

        objective_id = objective_record["Id"]
        child_ids = child_objective_ids_by_objective.get(objective_id, [])
        key_result_ids = key_result_ids_by_objective.get(objective_id, [])

        add_row(
            (objective_id, "objective", ""),
            {
                "Workspace": objective_record["WorkspaceName"],
                "Objective": objective_record["Title"],
                "ChildObjective": "",
                "KeyResult": "",
                "Level": objective_record["HierarchyRole"],
                "NodeType": "objective",
                "StatusId": objective_record["StatusId"],
                "Confidence": objective_record["Confidence"],
                "Progress": objective_record["Progress"],
                "TimePeriod": objective_record["TimePeriod"],
            },
        )

        for child_id in child_ids:
            child_objective_record = record_by_id.get(child_id)
            if not child_objective_record:
                continue
            add_row(
                (objective_id, "child_objective", child_id),
                {
                    "Workspace": child_objective_record["WorkspaceName"],
                    "Objective": objective_record["Title"],
                    "ChildObjective": child_objective_record["Title"],
                    "KeyResult": "",
                    "Level": child_objective_record["HierarchyRole"],
                    "NodeType": "child_objective",
                    "StatusId": child_objective_record["StatusId"],
                    "Confidence": child_objective_record["Confidence"],
                    "Progress": child_objective_record["Progress"],
                    "TimePeriod": child_objective_record["TimePeriod"],
                },
            )

        for key_result_id in key_result_ids:
            key_result_record = record_by_id.get(key_result_id)
            if not key_result_record:
                continue
            add_row(
                (objective_id, "key_result", key_result_id),
                {
                    "Workspace": key_result_record["WorkspaceName"],
                    "Objective": objective_record["Title"],
                    "ChildObjective": "",
                    "KeyResult": key_result_record["Title"],
                    "Level": "key_result",
                    "NodeType": "key_result",
                    "StatusId": key_result_record["StatusId"],
                    "Confidence": key_result_record["Confidence"],
                    "Progress": key_result_record["Progress"],
                    "TimePeriod": key_result_record["TimePeriod"],
                },
            )

            for child_id in child_ids:
                child_key_results = key_result_ids_by_objective.get(child_id, [])
                if key_result_id not in child_key_results:
                    continue
                child_objective_record = record_by_id.get(child_id)
                if not child_objective_record:
                    continue
                add_row(
                    (child_id, "child_key_result", key_result_id),
                    {
                        "Workspace": key_result_record["WorkspaceName"],
                        "Objective": objective_record["Title"],
                        "ChildObjective": child_objective_record["Title"],
                        "KeyResult": key_result_record["Title"],
                        "Level": "key_result",
                        "NodeType": "key_result",
                        "StatusId": key_result_record["StatusId"],
                        "Confidence": key_result_record["Confidence"],
                        "Progress": key_result_record["Progress"],
                        "TimePeriod": key_result_record["TimePeriod"],
                    },
                )

    return rows


def write_management_csv(rows, outfile):
    fieldnames = [
        "Workspace",
        "Objective",
        "ChildObjective",
        "KeyResult",
        "Level",
        "NodeType",
        "StatusId",
        "Confidence",
        "Progress",
        "TimePeriod",
    ]
    writer = csv.DictWriter(outfile, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)


def build_hierarchical_export(reporting_records, relationship_rows):
    record_by_id = {record["Id"]: record for record in reporting_records}
    child_relationships_by_source = {}
    for relationship in relationship_rows:
        child_relationships_by_source.setdefault(relationship["SourceId"], []).append(relationship)

    def format_record(record_id):
        record = record_by_id[record_id]
        payload = {
            "id": record["Id"],
            "type": record["NodeType"],
            "role": record["HierarchyRole"],
            "workspaceId": record["WorkspaceId"],
            "workspaceName": record["WorkspaceName"],
            "title": record["Title"],
            "alias": record["Alias"],
            "statusId": record["StatusId"],
            "confidence": record["Confidence"],
            "progress": record["Progress"],
            "timePeriod": record["TimePeriod"],
            "createdAt": record["CreatedAt"],
            "updatedAt": record["UpdatedAt"],
            "archived": record["Archived"],
            "assigneeUserIds": [value for value in record["AssigneeUserIds"].split(",") if value],
            "children": [],
        }
        for relationship in child_relationships_by_source.get(record_id, []):
            payload["children"].append(
                {
                    "relationType": relationship["RelationType"],
                    "node": format_record(relationship["TargetId"]),
                }
            )
        return payload

    root_workspace_record_ids = [
        record["Id"]
        for record in reporting_records
        if record["NodeType"] == "workspace"
    ]
    return {"workspaces": [format_record(record_id) for record_id in root_workspace_record_ids]}


def write_hierarchical_json(payload, outfile):
    json.dump(payload, outfile, indent=2)
    outfile.write("\n")


def write_path_csv(rows, outfile):
    max_depth = max(len(r) for r in rows) if rows else 0

    def get_level_prefix(depth):
        if depth == 0:
            return "Parent"
        return "Child0" + "-0" * (depth - 1)

    fieldnames = []
    for i in range(max_depth):
        prefix = get_level_prefix(i)
        fieldnames.extend(
            [
                prefix,
                f"{prefix}_Objective",
                f"{prefix}_ChildObjective",
                f"{prefix}_KeyResult",
            ]
        )
    fieldnames.extend(["Status", "Confidence", "Progress", "TimePeriod"])

    writer = csv.DictWriter(outfile, fieldnames=fieldnames)
    writer.writeheader()

    for row in rows:
        csv_row = {}
        for i, level in enumerate(row):
            prefix = get_level_prefix(i)
            csv_row[prefix] = level.get("ws_name", "")
            csv_row[f"{prefix}_Objective"] = level.get("objective_name") or ""
            csv_row[f"{prefix}_ChildObjective"] = level.get("child_objective_name") or ""
            csv_row[f"{prefix}_KeyResult"] = level.get("kr_alias") or ""

        last = row[-1] if row else {}
        csv_row["Status"] = last.get("status") or ""
        csv_row["Confidence"] = last.get("confidence") or ""
        csv_row["Progress"] = last.get("progress") if last.get("progress") is not None else ""
        csv_row["TimePeriod"] = last.get("time_period") or ""

        writer.writerow(csv_row)


def main():
    parser = argparse.ArgumentParser(
        description="Export airfocus objective workspaces with hierarchy and key results to CSV."
    )
    parser.add_argument(
        "--parent",
        help="Name or short name of the parent objective workspace to export",
    )
    args = parser.parse_args()

    config = load_config()

    try:
        if not args.parent:
            print_accessible_objective_workspaces(config, sys.stdout)
            return

        print(
            f"Searching for objective workspace '{args.parent}' by name or short name...",
            file=sys.stderr,
        )
        all_okr = get_accessible_objective_workspaces(config)
        matches = find_matching_workspaces(all_okr, args.parent)
        if not matches:
            names = [get_workspace_label(ws) for ws in sort_workspaces(all_okr)]
            print(
                f"Error: No objective workspace found matching '{args.parent}' by name or short name.",
                file=sys.stderr,
            )
            if names:
                print(
                    f"Available objective workspaces: {', '.join(names)}",
                    file=sys.stderr,
                )
            sys.exit(1)

        parent_ws = matches[0]
        parent_ws_id = parent_ws["id"]
        print(f"  Found: '{parent_ws['name']}' (id={parent_ws_id})", file=sys.stderr)

        print("Building workspace hierarchy...", file=sys.stderr)
        workspace_by_id, workspace_child_map, _ = get_workspace_relationship_maps(all_okr)
        tree = build_workspace_tree(
            config,
            parent_ws_id,
            workspace_by_id,
            workspace_child_map,
        )
        field_map_cache = {}
        resolved_item_cache = {}
        linked_workspace_item_cache = {}

        print("Flattening hierarchy paths...", file=sys.stderr)
        rows = list(
            flatten_paths(
                config,
                tree,
                field_map_cache=field_map_cache,
                resolved_item_cache=resolved_item_cache,
                linked_workspace_item_cache=linked_workspace_item_cache,
            )
        )

        reporting_records, relationship_rows = build_reporting_rows(
            config,
            tree,
            field_map_cache=field_map_cache,
            resolved_item_cache=resolved_item_cache,
            linked_workspace_item_cache=linked_workspace_item_cache,
        )

        management_rows = build_management_rows(reporting_records, relationship_rows)
        hierarchical_payload = build_hierarchical_export(reporting_records, relationship_rows)
    except ExporterError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"Unexpected error: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"  Generated {len(rows)} legacy path rows", file=sys.stderr)
    output_paths = get_output_paths(parent_ws)
    os.makedirs(os.path.dirname(output_paths["paths"]), exist_ok=True)

    with open(output_paths["paths"], "w", newline="", encoding="utf-8") as output_file:
        write_path_csv(rows, output_file)

    with open(output_paths["nodes"], "w", newline="", encoding="utf-8") as output_file:
        write_reporting_nodes_csv(reporting_records, output_file)
    with open(output_paths["edges"], "w", newline="", encoding="utf-8") as output_file:
        write_reporting_edges_csv(relationship_rows, output_file)
    with open(output_paths["management"], "w", newline="", encoding="utf-8") as output_file:
        write_management_csv(management_rows, output_file)
    with open(output_paths["json"], "w", encoding="utf-8") as output_file:
        write_hierarchical_json(hierarchical_payload, output_file)

    print(
        f"  Generated {len(reporting_records)} reporting records and {len(relationship_rows)} relationships",
        file=sys.stderr,
    )
    print(f"  Wrote path CSV to {output_paths['paths']}", file=sys.stderr)
    print(f"  Wrote reporting records CSV to {output_paths['nodes']}", file=sys.stderr)
    print(f"  Wrote relationships CSV to {output_paths['edges']}", file=sys.stderr)
    print(f"  Wrote management CSV to {output_paths['management']}", file=sys.stderr)
    print(f"  Wrote JSON export to {output_paths['json']}", file=sys.stderr)


if __name__ == "__main__":
    main()
