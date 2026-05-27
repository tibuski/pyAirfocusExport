import argparse
import csv
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


def print_accessible_objective_workspaces(config, stream):
    workspaces = search_workspaces(config)
    if not workspaces:
        print("No accessible objective workspaces found.", file=stream)
        return 0

    print("Accessible objective workspaces:", file=stream)
    for workspace in sorted(workspaces, key=lambda ws: ws.get("name", "").lower()):
        alias = workspace.get("alias")
        label = workspace.get("name", workspace.get("id", "?"))
        if alias:
            print(f"- {label} ({alias})", file=stream)
        else:
            print(f"- {label}", file=stream)
    return len(workspaces)


def get_workspace(config, workspace_id):
    return api_request(config, "GET", f"/api/workspaces/{workspace_id}")


def list_workspaces(config, workspace_ids):
    return unwrap_items_payload(
        api_request(config, "POST", "/api/workspaces/list", body=workspace_ids)
    )


def search_items(config, workspace_id, offset=0, limit=1000):
    params = {"offset": offset, "limit": limit}
    return api_request(
        config,
        "POST",
        f"/api/workspaces/{workspace_id}/items/search",
        body={},
        params=params,
    )


def list_items(config, workspace_id, item_ids):
    return unwrap_items_payload(
        api_request(config, "POST", f"/api/workspaces/{workspace_id}/items/list", body=item_ids)
    )


def get_statuses(config, workspace_id):
    return unwrap_items_payload(
        api_request(config, "GET", f"/api/workspaces/{workspace_id}/statuses")
    )


def search_fields(config, workspace_ids=None):
    body = {}
    if workspace_ids:
        body["workspaceIds"] = workspace_ids
    result = api_request(config, "POST", "/api/fields/search", body=body)
    return result.get("items", [])


def paginated_search_items(config, workspace_id):
    items = []
    offset = 0
    limit = 1000
    while True:
        page = search_items(config, workspace_id, offset=offset, limit=limit)
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


def extract_item_okr_fields(item, field_map):
    result = {
        "kr_ids": [],
        "confidence": None,
        "progress": None,
        "time_period": None,
    }
    fields = item.get("fields", {})
    for field_id, value in fields.items():
        ftype = field_map.get(field_id)
        if ftype == FIELD_OKR_KEY_RESULTS:
            ids = value.get("itemIds", [])
            result["kr_ids"].extend(ids)
        elif ftype == FIELD_OKR_KEY_RESULT_REF:
            wid = value.get("workspaceId")
            iid = value.get("itemId")
            if iid:
                result["kr_ids"].append((wid, iid))
        elif ftype == FIELD_OKR_CONFIDENCE:
            result["confidence"] = value.get("value")
        elif ftype == FIELD_OKR_PROGRESS:
            result["progress"] = value.get("percentage")
        elif ftype == FIELD_OKR_TIME_PERIOD:
            if "from" in value and "to" in value:
                result["time_period"] = f"{value['from']} - {value['to']}"
            elif "periodId" in value:
                result["time_period"] = value["periodId"]
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


def discover_child_workspaces(items):
    child_ws_ids = set()
    for item in items:
        embed = item.get("_embedded", {})
        for child in embed.get("children", []):
            wid = child.get("workspaceId")
            if wid:
                child_ws_ids.add(wid)
    return child_ws_ids


def build_workspace_tree(config, root_ws_id, visited=None):
    if visited is None:
        visited = set()
    if root_ws_id in visited:
        return None
    visited.add(root_ws_id)

    ws = get_workspace(config, root_ws_id)
    print(f"  Fetching items from workspace '{ws.get('name')}'...", file=sys.stderr)
    items = paginated_search_items(config, root_ws_id)
    print(f"    Found {len(items)} items", file=sys.stderr)

    child_ws_ids = {
        child_ws_id
        for child_ws_id in discover_child_workspaces(items)
        if child_ws_id and child_ws_id != root_ws_id and child_ws_id not in visited
    }
    child_ws = []
    if child_ws_ids:
        resolved = list_workspaces(config, list(child_ws_ids))
        print(
            f"  Found {len(child_ws_ids)} child workspaces, building hierarchy...",
            file=sys.stderr,
        )
        for cws in resolved:
            if cws is None:
                continue
            child = build_workspace_tree(config, cws["id"], visited)
            if child:
                child_ws.append(child)

    return {
        "workspace": ws,
        "items": items,
        "children": child_ws,
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


def get_status_map(config, workspace_id, cache):
    if workspace_id in cache:
        return cache[workspace_id]

    statuses = get_statuses(config, workspace_id)
    status_map = {status["id"]: status["name"] for status in statuses}
    cache[workspace_id] = status_map
    return status_map


def flatten_paths(
    config,
    workspace_node,
    depth=0,
    ancestor_ctx=None,
    field_map_cache=None,
    status_cache=None,
    resolved_item_cache=None,
):
    if ancestor_ctx is None:
        ancestor_ctx = []
    if field_map_cache is None:
        field_map_cache = {}
    if status_cache is None:
        status_cache = {}
    if resolved_item_cache is None:
        resolved_item_cache = {}

    ws = workspace_node["workspace"]
    ws_name = ws.get("name", "")
    items = workspace_node["items"]
    children = workspace_node["children"]

    status_map = get_status_map(config, ws["id"], status_cache)

    field_map = build_field_type_map(config, [ws["id"]], field_map_cache)

    current_ctx = ancestor_ctx + [
        {
            "ws_name": ws_name,
            "item_name": None,
            "child_name": None,
            "kr_alias": None,
            "status": None,
            "confidence": None,
            "progress": None,
            "time_period": None,
        }
    ]

    item_kr_list = []
    for item in items:
        okr = extract_item_okr_fields(item, field_map)
        status_name = status_map.get(item.get("statusId"), item.get("statusId"))

        kr_ids = okr["kr_ids"]
        if kr_ids:
            local_ids = [i for i in kr_ids if isinstance(i, str)]
            cross_ws = [i for i in kr_ids if isinstance(i, tuple)]

            resolved_krs = []
            if local_ids:
                resolved_krs.extend(
                    resolve_items(config, ws["id"], local_ids, resolved_item_cache)
                )
            for rws_id, ritem_id in cross_ws:
                if not rws_id:
                    continue
                ritems = resolve_items(
                    config, rws_id, [ritem_id], resolved_item_cache
                )
                resolved_krs.extend(ritems)

            if resolved_krs:
                for kr in resolved_krs:
                    kr_alias = get_item_alias(kr)
                    item_kr_list.append(
                        {
                            "item": item,
                            "child_item": None,
                            "kr_alias": kr_alias,
                            "status": status_name,
                            "confidence": okr["confidence"],
                            "progress": okr["progress"],
                            "time_period": okr["time_period"],
                        }
                    )
            else:
                item_kr_list.append(
                    {
                        "item": item,
                        "child_item": None,
                        "kr_alias": None,
                        "status": status_name,
                        "confidence": okr["confidence"],
                        "progress": okr["progress"],
                        "time_period": okr["time_period"],
                    }
                )
        else:
            item_kr_list.append(
                {
                    "item": item,
                    "child_item": None,
                    "kr_alias": None,
                    "status": status_name,
                    "confidence": okr["confidence"],
                    "progress": okr["progress"],
                    "time_period": okr["time_period"],
                }
            )

    ws_items_parents = {}
    for item in items:
        embed = item.get("_embedded", {})
        parents_list = [
            p["itemId"]
            for p in embed.get("parents", [])
            if p.get("workspaceId") == ws["id"]
        ]
        ws_items_parents[item["id"]] = parents_list

    top_level = [ik for ik in item_kr_list if not ws_items_parents.get(ik["item"]["id"])]

    child_map = {}
    for ik in item_kr_list:
        iid = ik["item"]["id"]
        parent_ids = ws_items_parents.get(iid, [])
        for pid in parent_ids:
            child_map.setdefault(pid, []).append(ik)

    def build_kr_paths(parent_ik, current_ctx_level):
        pid = parent_ik["item"]["id"]
        children_ik = child_map.get(pid, [])

        if not children_ik:
            ctx_entry = current_ctx_level.copy()
            ctx_entry["item_name"] = parent_ik["item"].get("name", "")
            ctx_entry["child_name"] = None
            ctx_entry["kr_alias"] = parent_ik["kr_alias"]
            ctx_entry["status"] = parent_ik["status"]
            ctx_entry["confidence"] = parent_ik["confidence"]
            ctx_entry["progress"] = parent_ik["progress"]
            ctx_entry["time_period"] = parent_ik["time_period"]
            yield list(current_ctx[:-1]) + [ctx_entry]
        else:
            for child_ik in children_ik:
                ctx_entry = current_ctx_level.copy()
                ctx_entry["item_name"] = parent_ik["item"].get("name", "")
                ctx_entry["child_name"] = child_ik["item"].get("name", "")
                ctx_entry["kr_alias"] = child_ik["kr_alias"]
                ctx_entry["status"] = child_ik["status"]
                ctx_entry["confidence"] = child_ik["confidence"]
                ctx_entry["progress"] = child_ik["progress"]
                ctx_entry["time_period"] = child_ik["time_period"]
                yield list(current_ctx[:-1]) + [ctx_entry]

    ctx_level = current_ctx[-1]
    if top_level:
        for tl_item_kr in top_level:
            yield from build_kr_paths(tl_item_kr, ctx_level)
    else:
        yield list(current_ctx[:-1]) + [ctx_level]

    if children:
        for child in children:
            yield from flatten_paths(
                config,
                child,
                depth + 1,
                current_ctx,
                field_map_cache,
                status_cache,
                resolved_item_cache,
            )


def write_csv(rows, outfile):
    max_depth = max(len(r) for r in rows) if rows else 0

    fieldnames = []
    for i in range(max_depth):
        fieldnames.extend(
            [
                f"Parent{i}",
                f"Parent{i}_Item",
                f"Parent{i}_ChildItem",
                f"Parent{i}_KeyResult",
            ]
        )
    fieldnames.extend(["Status", "Confidence", "Progress", "TimePeriod"])

    writer = csv.DictWriter(outfile, fieldnames=fieldnames)
    writer.writeheader()

    for row in rows:
        csv_row = {}
        for i, level in enumerate(row):
            csv_row[f"Parent{i}"] = level.get("ws_name", "")
            csv_row[f"Parent{i}_Item"] = level.get("item_name") or ""
            csv_row[f"Parent{i}_ChildItem"] = level.get("child_name") or ""
            csv_row[f"Parent{i}_KeyResult"] = level.get("kr_alias") or ""

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
        "--root",
        help="Name of the root objective workspace to export",
    )
    args = parser.parse_args()

    config = load_config()

    try:
        if not args.root:
            print_accessible_objective_workspaces(config, sys.stdout)
            return

        print(f"Searching for objective workspace '{args.root}'...", file=sys.stderr)
        matches = search_workspaces(config, args.root)
        if not matches:
            all_okr = search_workspaces(config)
            names = [ws.get("name", "?") for ws in all_okr]
            print(
                f"Error: No objective workspace found matching '{args.root}'.",
                file=sys.stderr,
            )
            if names:
                print(
                    f"Available objective workspaces: {', '.join(names)}",
                    file=sys.stderr,
                )
            sys.exit(1)

        root_ws = matches[0]
        root_ws_id = root_ws["id"]
        print(f"  Found: '{root_ws['name']}' (id={root_ws_id})", file=sys.stderr)

        print("Building workspace hierarchy...", file=sys.stderr)
        tree = build_workspace_tree(config, root_ws_id)
        print("Flattening hierarchy paths...", file=sys.stderr)
        rows = list(flatten_paths(config, tree))
    except ExporterError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"Unexpected error: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"  Generated {len(rows)} rows", file=sys.stderr)

    write_csv(rows, sys.stdout)


if __name__ == "__main__":
    main()
