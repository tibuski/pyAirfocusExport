import argparse
import csv
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

import config

CONTENT_TYPE = "application/vnd.airfocus.markdown+json"

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


def api_request(method, path, body=None, params=None):
    url = f"{config.baseurl}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"

    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {config.apikey}")
    req.add_header("Content-Type", CONTENT_TYPE)
    req.add_header("Accept", CONTENT_TYPE)

    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        print(f"API error {e.code} for {method} {path}: {body_text}", file=sys.stderr)
        raise


def search_workspaces(name=None):
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
    result = api_request("POST", "/api/workspaces/search", body=body)
    items = result.get("items", [])
    return [ws for ws in items if ws.get("namespace") == "app:okr"]


def get_workspace(workspace_id):
    return api_request("GET", f"/api/workspaces/{workspace_id}")


def list_workspaces(workspace_ids):
    return api_request("POST", "/api/workspaces/list", body=workspace_ids)


def search_items(workspace_id, offset=0, limit=1000):
    params = {"offset": offset, "limit": limit}
    return api_request(
        "POST",
        f"/api/workspaces/{workspace_id}/items/search",
        body={},
        params=params,
    )


def list_items(workspace_id, item_ids):
    return api_request(
        "POST", f"/api/workspaces/{workspace_id}/items/list", body=item_ids
    )


def get_statuses(workspace_id):
    return api_request("GET", f"/api/workspaces/{workspace_id}/statuses")


def search_fields(workspace_ids=None):
    body = {}
    if workspace_ids:
        body["workspaceIds"] = workspace_ids
    result = api_request("POST", "/api/fields/search", body=body)
    return result.get("items", [])


def paginated_search_items(workspace_id):
    items = []
    offset = 0
    limit = 1000
    while True:
        page = search_items(workspace_id, offset=offset, limit=limit)
        batch = page.get("items", [])
        items.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
    return items


def build_field_type_map(workspace_ids):
    all_fields = search_fields(workspace_ids)
    field_map = {}
    for field in all_fields:
        ft = field.get("typeId")
        if ft in OKR_FIELD_TYPES:
            field_map[field["id"]] = ft
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


def resolve_items(workspace_id, item_ids):
    if not item_ids:
        return []
    resolved = []
    for i in range(0, len(item_ids), 1000):
        batch = item_ids[i : i + 1000]
        resolved.extend(list_items(workspace_id, batch))
    return [r for r in resolved if r is not None]


def discover_child_workspaces(items):
    child_ws_ids = set()
    for item in items:
        embed = item.get("_embedded", {})
        for child in embed.get("children", []):
            wid = child.get("workspaceId")
            if wid:
                child_ws_ids.add(wid)
    return child_ws_ids


def build_workspace_tree(root_ws_id, visited=None):
    if visited is None:
        visited = set()
    if root_ws_id in visited:
        return None
    visited.add(root_ws_id)

    ws = get_workspace(root_ws_id)
    print(f"  Fetching items from workspace '{ws.get('name')}'...", file=sys.stderr)
    items = paginated_search_items(root_ws_id)
    print(f"    Found {len(items)} items", file=sys.stderr)

    child_ws_ids = discover_child_workspaces(items) - visited
    child_ws = []
    if child_ws_ids:
        resolved = list_workspaces(list(child_ws_ids))
        print(
            f"  Found {len(child_ws_ids)} child workspaces, building hierarchy...",
            file=sys.stderr,
        )
        for cws in resolved:
            if cws is None:
                continue
            child = build_workspace_tree(cws["id"], visited)
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


def get_status_map(workspace_id):
    statuses = get_statuses(workspace_id)
    return {s["id"]: s["name"] for s in statuses}


def flatten_paths(workspace_node, depth=0, ancestor_ctx=None):
    if ancestor_ctx is None:
        ancestor_ctx = []

    ws = workspace_node["workspace"]
    ws_name = ws.get("name", "")
    items = workspace_node["items"]
    children = workspace_node["children"]

    status_map = get_status_map(ws["id"])

    field_map = build_field_type_map([ws["id"]])

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
                resolved_krs.extend(resolve_items(ws["id"], local_ids))
            for rws_id, ritem_id in cross_ws:
                ritems = resolve_items(rws_id, [ritem_id])
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
            yield from flatten_paths(child, depth + 1, current_ctx)


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
        required=True,
        help="Name of the root objective workspace to export",
    )
    args = parser.parse_args()

    if not hasattr(config, "apikey") or not hasattr(config, "baseurl"):
        print(
            "Error: config.py must define 'apikey' and 'baseurl'. "
            "Copy config.py.example to config.py and fill in your values.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Searching for objective workspace '{args.root}'...", file=sys.stderr)
    matches = search_workspaces(args.root)
    if not matches:
        all_okr = search_workspaces()
        names = [ws.get("name", "?") for ws in all_okr]
        print(
            f"Error: No objective workspace found matching '{args.root}'.",
            file=sys.stderr,
        )
        if names:
            print(f"Available objective workspaces: {', '.join(names)}", file=sys.stderr)
        sys.exit(1)

    root_ws = matches[0]
    root_ws_id = root_ws["id"]
    print(f"  Found: '{root_ws['name']}' (id={root_ws_id})", file=sys.stderr)

    print("Building workspace hierarchy...", file=sys.stderr)
    tree = build_workspace_tree(root_ws_id)

    print("Flattening hierarchy paths...", file=sys.stderr)
    rows = list(flatten_paths(tree))
    print(f"  Generated {len(rows)} rows", file=sys.stderr)

    write_csv(rows, sys.stdout)


if __name__ == "__main__":
    main()
