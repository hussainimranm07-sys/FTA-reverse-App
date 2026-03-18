import streamlit as st
import json, math, os, requests, uuid, io, re, html as _html
from datetime import datetime
import streamlit.components.v1 as components

def esc(s):
    """HTML-escape a string so it's safe to embed in unsafe_allow_html blocks."""
    return _html.escape(str(s) if s is not None else "")

st.set_page_config(page_title="FTA Reverse Engineer", page_icon="⚠️",
                   layout="wide", initial_sidebar_state="expanded")

# ── Constants ─────────────────────────────────────────────────────────────
# GROUP = "Combined Faults" oval — an intermediate AND/OR gate node.
# It has no independent failure meaning; it just groups children under a
# specific gate before feeding into its parent via the parent's gate.
# Visually rendered as an oval (like in standard FTA diagrams).
LEVEL_ORDER        = ["HAZARD", "SF", "FF", "IF", "GROUP"]
LEVEL_COLORS       = {"HAZARD":"#ff4d4d","SF":"#ff8c42","FF":"#f5c518","IF":"#4caf7d","GROUP":"#7e57c2"}
LEVEL_TEXT         = {"HAZARD":"#fff","SF":"#fff","FF":"#111","IF":"#fff","GROUP":"#fff"}
VALID_PARENT_TYPES = ["HAZARD","SF","FF","GROUP"]
VALID_CHILD_TYPES  = ["SF","FF","IF","GROUP"]
# For display ordering (GROUP shown between FF and IF)
DISPLAY_ORDER      = ["HAZARD","SF","FF","IF","GROUP"]

# ── Gist helpers ──────────────────────────────────────────────────────────
def gh(token): return {"Authorization":f"token {token}","Accept":"application/vnd.github+json"}

def get_gist(token, gid):
    try:
        r = requests.get(f"https://api.github.com/gists/{gid}", headers=gh(token), timeout=10)
        return r.json() if r.status_code == 200 else None
    except: return None

def list_gist_files(token, gid):
    g = get_gist(token, gid)
    return sorted(g.get("files",{}).keys()) if g else []

def load_gist_file(token, gid, fname):
    g = get_gist(token, gid)
    if not g: return []
    try:
        raw = json.loads(g.get("files",{}).get(fname,{}).get("content","[]"))
    except:
        return []
    # Normalise nodes — ensure all expected fields exist for forward-compatibility
    for n in raw:
        n.setdefault("nodeId",    n.get("id",""))
        n.setdefault("ftLabel",   "")
        n.setdefault("fixedValue", None)
        n.setdefault("targetValue", None)
        n.setdefault("calculatedValue", None)
        n.setdefault("parentIds", [])
        n.setdefault("gate", "OR")
        n.setdefault("type", "IF")
    return raw

def save_gist_file(token, gid, fname, data):
    try:
        r = requests.patch(f"https://api.github.com/gists/{gid}", headers=gh(token),
                           json={"files":{fname:{"content":json.dumps(data,indent=2)}}}, timeout=10)
        return r.status_code == 200
    except: return False

def del_gist_file(token, gid, fname):
    try:
        requests.patch(f"https://api.github.com/gists/{gid}", headers=gh(token),
                       json={"files":{fname:None}}, timeout=10)
    except: pass

# ── Calculation ───────────────────────────────────────────────────────────
# ── Calculation ───────────────────────────────────────────────────────────
def recalculate(nodes):
    """
    Top-down reverse distribution using Kahn topological sort.

    Pin propagation (NEW):
    - If ANY node in a nodeId group has fixedValue set, ALL nodes with the
      same nodeId inherit that fixedValue before calculation begins.
      This means pinning one IF-085 instance pins all IF-085 instances.
    - Most-conservative rule: if multiple instances have different fixedValues,
      the MAXIMUM (worst case) is used across the group.

    fixedValue support:
    - A node with fixedValue set is PINNED — calculatedValue always = fixedValue
    - When an OR-gate parent distributes:
        1. Fixed children claim their pinned value from budget first
        2. Remainder shared equally among unfixed children
    - AND gate: fixed children excluded from product decomposition

    Standard FTA rules:
    - OR:  remainder = parent - sum(fixed); each unfixed = remainder / n_unfixed
    - AND: remainder = parent / product(fixed); each unfixed = remainder^(1/n_unfixed)
    - Shared nodes (multiple parents): MAX value — most conservative wins
    """
    if not nodes:
        return nodes

    updated = [dict(n) for n in nodes]
    by_id   = {n["id"]: n for n in updated}

    # ── Pin propagation: sync fixedValue across all same-nodeId instances ──
    from collections import defaultdict as _defdict
    nid_groups = _defdict(list)
    for n in updated:
        nid = (n.get("nodeId") or "").strip()
        if nid:
            nid_groups[nid].append(n)
    for nid, grp in nid_groups.items():
        if len(grp) < 2:
            continue
        # Find the maximum fixedValue across all pinned instances in this group
        pinned_vals = [n["fixedValue"] for n in grp if n.get("fixedValue") is not None]
        if not pinned_vals:
            continue
        # Use MAX of all pinned values (most conservative / worst case)
        group_pin = max(pinned_vals)
        for n in grp:
            n["fixedValue"]     = group_pin
            n["calculatedValue"] = group_pin
    # ── End pin propagation ────────────────────────────────────────────────

    for n in updated:
        if n["type"] == "HAZARD":
            n["calculatedValue"] = n.get("targetValue") or 1e-7
        elif n.get("fixedValue") is not None:
            n["calculatedValue"] = n["fixedValue"]
        else:
            n["calculatedValue"] = None

    children_of = {n["id"]: [] for n in updated}
    in_degree   = {}
    for n in updated:
        nid  = n["id"]
        pids = [p for p in (n.get("parentIds") or []) if p in by_id]
        in_degree[nid] = len(pids)
        for pid in pids:
            if pid in children_of:
                children_of[pid].append(nid)

    from collections import deque
    resolved         = set()
    queue            = deque()
    parents_resolved = {n["id"]: 0 for n in updated}

    for n in updated:
        if n["type"] == "HAZARD" or n.get("fixedValue") is not None:
            resolved.add(n["id"])
            queue.append(n["id"])

    while queue:
        pid        = queue.popleft()
        parent     = by_id[pid]
        parent_val = parent.get("calculatedValue")
        if parent_val is None:
            continue

        child_ids = children_of.get(pid, [])
        if not child_ids:
            continue

        fixed_ids   = [cid for cid in child_ids if by_id[cid].get("fixedValue") is not None]
        unfixed_ids = [cid for cid in child_ids if by_id[cid].get("fixedValue") is None]
        n_unfixed   = len(unfixed_ids)

        if parent["gate"] == "OR":
            fixed_sum = sum(by_id[cid]["fixedValue"] for cid in fixed_ids)
            remainder = max(parent_val - fixed_sum, 0.0)
            child_val = (remainder / n_unfixed) if n_unfixed > 0 else 0.0
        else:  # AND
            if fixed_ids and unfixed_ids:
                fixed_product = 1.0
                for cid in fixed_ids:
                    fv = by_id[cid]["fixedValue"]
                    if fv and fv > 0:
                        fixed_product *= fv
                remainder = parent_val / fixed_product if fixed_product > 0 else parent_val
                child_val = remainder ** (1.0 / n_unfixed) if n_unfixed > 0 else parent_val
            else:
                child_val = parent_val ** (1.0 / n_unfixed) if n_unfixed > 0 else parent_val

        for cid in unfixed_ids:
            child    = by_id[cid]
            existing = child.get("calculatedValue")
            if existing is None:
                child["calculatedValue"] = child_val
            else:
                child["calculatedValue"] = max(existing, child_val)

        for cid in child_ids:
            parents_resolved[cid] += 1
            total_parents = in_degree.get(cid, 0)
            if parents_resolved[cid] >= total_parents and cid not in resolved:
                resolved.add(cid)
                queue.append(cid)

    return updated

def fmt(v):
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))): return "-"
    return f"{v:.3e}"

def now_str(): return datetime.now().strftime("%Y-%m-%d_%H-%M")
def is_snap(n): return n.startswith("snapshot_")
def is_named(n): return not is_snap(n)

# ── Export: JSON ──────────────────────────────────────────────────────────
def export_json(nodes):
    """Export all nodes. Ensures every node has all standard fields."""
    clean = []
    for n in nodes:
        nc = dict(n)
        nc.setdefault("nodeId",   nc.get("id",""))
        nc.setdefault("ftLabel",  "")
        nc.setdefault("fixedValue", None)
        nc.setdefault("targetValue", None)
        nc.setdefault("calculatedValue", None)
        clean.append(nc)
    return json.dumps(clean, indent=2).encode("utf-8")

# ── Export: Cypher (Neo4j) ────────────────────────────────────────────────
def export_cypher(nodes):
    """
    Generate Cypher statements to recreate the FTA in Neo4j.
    Includes: fixedValue (pinned nodes), nodeId (user reference), GROUP type.
    Run in Neo4j Browser or via neo4j-shell.
    """
    by_id = {n["id"]: n for n in nodes}
    n_pinned = sum(1 for n in nodes if n.get("fixedValue") is not None)
    n_shared  = sum(1 for n in nodes if len(n.get("parentIds") or []) > 1)
    lines = [
        "// ── FTA Fault Tree — Cypher Export ──────────────────────────",
        f"// Generated:    {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"// Total nodes:  {len(nodes)}",
        f"// Pinned nodes: {n_pinned}  (fixedValue set — budget subtracted from siblings)",
        f"// Shared nodes: {n_shared}  (multiple parents — MAX allocation rule applied)",
        "//",
        "// Run in Neo4j Browser to create the full graph.",
        "",
        "// STEP 1: Clear existing FTA nodes (optional — comment out to merge)",
        "// MATCH (n:FTANode) DETACH DELETE n;",
        "",
        "// STEP 2: Create all nodes",
    ]
    for n in nodes:
        val       = n.get("calculatedValue")
        fv        = n.get("fixedValue")
        val_s     = fmt(val) if val is not None else "null"
        fv_s      = fmt(fv)  if fv  is not None else "null"
        name      = n["name"].replace("'", "\\'")
        node_id   = (n.get("nodeId") or n["id"]).replace("'", "\\'")
        ft_lbl    = (n.get("ftLabel") or "").replace("'", "\\'")
        is_shared = len(n.get("parentIds") or []) > 1
        is_pinned = fv is not None
        is_group  = n["type"] == "GROUP"
        lines.append(
            f"CREATE (:FTANode {{"
            f"id:'{n['id']}', nodeId:'{node_id}', ftLabel:'{ft_lbl}', name:'{name}', "
            f"type:'{n['type']}', gate:'{n['gate']}', "
            f"calculatedValue:{val_s if val is not None else 'null'}, "
            f"fixedValue:{fv_s if fv is not None else 'null'}, "
            f"valueStr:'{val_s}', "
            f"shared:{str(is_shared).lower()}, "
            f"pinned:{str(is_pinned).lower()}, "
            f"isGroup:{str(is_group).lower()}"
            f"}});"
        )

    lines += ["", "// STEP 3: Create relationships (FEEDS_INTO = child → parent)"]
    for n in nodes:
        for pid in (n.get("parentIds") or []):
            if pid in by_id:
                lines.append(
                    f"MATCH (c:FTANode {{id:'{n['id']}'}}), (p:FTANode {{id:'{pid}'}}) "
                    f"CREATE (c)-[:FEEDS_INTO {{gate:'{by_id[pid]['gate']}', "
                    f"childPinned:{str(n.get('fixedValue') is not None).lower()}}}]->(p);"
                )

    lines += [
        "",
        "// STEP 4: Useful queries",
        "",
        "// Show full graph:",
        "// MATCH (n:FTANode)-[r]->(m) RETURN n,r,m;",
        "",
        "// Show all shared nodes with their values:",
        "// MATCH (n:FTANode) WHERE n.shared=true",
        "// RETURN n.nodeId, n.name, n.type, n.valueStr ORDER BY n.type;",
        "",
        "// Show all pinned (fixed-value) nodes:",
        "// MATCH (n:FTANode) WHERE n.pinned=true",
        "// RETURN n.nodeId, n.name, n.fixedValue, n.valueStr;",
        "",
        "// Show path from any IF to its HAZARD with values:",
        "// MATCH p=(i:FTANode {type:'IF'})-[:FEEDS_INTO*]->(h:FTANode {type:'HAZARD'})",
        "// RETURN [node IN nodes(p) | node.nodeId + ':' + node.valueStr] AS path LIMIT 10;",
        "",
        "// Find all IFs contributing to a specific hazard (e.g. 'SF-10'):",
        "// MATCH p=(i:FTANode {type:'IF'})-[:FEEDS_INTO*]->(h:FTANode)",
        "// WHERE h.name CONTAINS 'SF-10'",
        "// RETURN i.nodeId, i.name, i.valueStr ORDER BY i.calculatedValue DESC;",
        "",
        "// Budget check: verify OR-gate children sum ≤ parent for all nodes:",
        "// MATCH (p:FTANode {gate:'OR'})<-[:FEEDS_INTO]-(c:FTANode)",
        "// WITH p, sum(c.calculatedValue) AS childSum",
        "// WHERE childSum > p.calculatedValue * 1.01",
        "// RETURN p.nodeId, p.name, p.valueStr, childSum AS actualSum;",
    ]
    return "\n".join(lines).encode("utf-8")

# ── Export: Excel ─────────────────────────────────────────────────────────
def sanitize_xl(val):
    if val is None: return "-"
    if isinstance(val, (int, float)): return val
    s = str(val)
    try:
        from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
        s = ILLEGAL_CHARACTERS_RE.sub("", s)
    except ImportError: pass
    return s or "-"

def export_excel(nodes):
    try:
        import openpyxl
        from openpyxl.styles import PatternFill, Font, Alignment
        from openpyxl.utils import get_column_letter
    except ImportError: return None

    by_id    = {n["id"]: n for n in nodes}
    wb       = openpyxl.Workbook()
    fills    = {lvl: PatternFill("solid", fgColor=c) for lvl, c in
                [("HAZARD","FFFF4D4D"),("SF","FFFF8C42"),("FF","FFF5C518"),
                 ("IF","FF4CAF7D"),("GROUP","FF7E57C2")]}
    hdr_fill = PatternFill("solid", fgColor="FF0F3460")
    pin_fill = PatternFill("solid", fgColor="FF3A0A14")  # dark red bg for pinned

    def hdr_font(c): return Font(bold=True, color="FFFFFFFF", name="Courier New")
    def row_font(lvl):
        dark_text = lvl in ("FF",)
        return Font(name="Courier New", size=10, color="FF111111" if dark_text else "FFFFFFFF")
    def ctr(): return Alignment(horizontal="center", vertical="center")
    def lft(): return Alignment(horizontal="left",   vertical="center", wrap_text=True)

    # ── Sheet 1: All nodes ──────────────────────────────────────────────
    ws = wb.active; ws.title = "FTA Nodes"
    hdrs = ["Level", "Type", "Node Name", "Node ID", "FT Label",
            "Gate", "Calc. Value", "Fixed Value (📌)", "Shared",
            "Parent Nodes", "Child Nodes"]
    for ci, h in enumerate(hdrs, 1):
        c = ws.cell(1, ci, h); c.font = hdr_font(None); c.fill = hdr_fill; c.alignment = ctr()
    row = 2
    for lvl in DISPLAY_ORDER:
        for n in [x for x in nodes if x["type"] == lvl]:
            pnames   = " | ".join(by_id[p]["name"] for p in (n.get("parentIds") or []) if p in by_id)
            cnames   = " | ".join(x["name"] for x in nodes if n["id"] in (x.get("parentIds") or []))
            fv       = n.get("fixedValue")
            fv_str   = fmt(fv) if fv is not None else ""
            is_pinned = fv is not None
            ft_lbl   = n.get("ftLabel", "")
            vals = [
                DISPLAY_ORDER.index(lvl) + 1,
                sanitize_xl(lvl),
                sanitize_xl(n["name"]),
                sanitize_xl(n.get("nodeId", n["id"])),
                sanitize_xl(ft_lbl),
                sanitize_xl(n["gate"]),
                n.get("calculatedValue"),
                sanitize_xl(fv_str),
                "YES" if len(n.get("parentIds") or []) > 1 else "NO",
                sanitize_xl(pnames or "-"),
                sanitize_xl(cnames or "-"),
            ]
            node_fill = pin_fill if is_pinned else fills.get(lvl, hdr_fill)
            for ci, v in enumerate(vals, 1):
                cell = ws.cell(row, ci, sanitize_xl(v) if isinstance(v, str) else v)
                cell.font      = row_font(lvl)
                cell.fill      = node_fill
                cell.alignment = lft() if ci in (3, 10, 11) else ctr()
            # Mark fixed value cell red text
            if is_pinned:
                ws.cell(row, 8).font = Font(name="Courier New", size=10,
                                            color="FFFF4D4D", bold=True)
            row += 1
    for ci, w in enumerate([8, 10, 30, 12, 10, 8, 16, 16, 8, 30, 30], 1):
        ws.column_dimensions[get_column_letter(ci)].width = w

    # ── Sheet 2: Per-hazard hierarchy ───────────────────────────────────
    ws2 = wb.create_sheet("Hierarchy"); ws2.sheet_view.showGridLines = False
    ws2.cell(1, 1, "FTA HIERARCHY - TOP TO BOTTOM").font = Font(
        bold=True, size=14, name="Courier New", color="FFE94560")
    ws2.cell(2, 1, f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}").font = Font(
        size=9, name="Courier New", color="FF888888")
    row = 4
    def write_hier(nid, depth, seen):
        nonlocal row
        if nid in seen: return
        seen.add(nid)
        n = by_id.get(nid)
        if not n: return
        label  = f"{'    ' * depth}{'  -> ' if depth else ''}{n['name']}"
        tags   = []
        nid_tag = n.get("nodeId", "")
        ft_tag  = n.get("ftLabel", "")
        if nid_tag:                                tags.append(f"[{nid_tag}]")
        if ft_tag:                                 tags.append(f"[{ft_tag}]")
        if len(n.get("parentIds") or []) > 1:      tags.append("[SHARED]")
        if n.get("fixedValue") is not None:        tags.append(f"[FIXED={fmt(n['fixedValue'])}]")
        if n["type"] == "GROUP":                   tags.append("[GROUP]")
        tag_str = " ".join(tags)
        fhex = {"HAZARD":"FFFF4D4D","SF":"FFFF8C42","FF":"FFF5C518",
                "IF":"FF4CAF7D","GROUP":"FF7E57C2"}.get(n["type"], "FF1A3A6B")
        dark_text = n["type"] in ("FF",)
        txt_color = "FF111111" if dark_text else "FFFFFFFF"
        c1 = ws2.cell(row, 1, sanitize_xl(label))
        c2 = ws2.cell(row, 2, sanitize_xl(f"{n['type']}[{n['gate']}]"))
        c3 = ws2.cell(row, 3, sanitize_xl(fmt(n.get("calculatedValue"))))
        c4 = ws2.cell(row, 4, sanitize_xl(tag_str))
        for c in [c1, c2, c3, c4]:
            c.font = Font(name="Courier New", size=10, bold=(depth == 0), color=txt_color)
            c.fill = PatternFill("solid", fgColor=fhex)
        c3.alignment = Alignment(horizontal="right", vertical="center")
        if n.get("fixedValue") is not None:
            c3.font = Font(name="Courier New", size=10, bold=True,
                           color="FFFF4D4D")  # red for pinned
        row += 1
        for child in [x for x in nodes if nid in (x.get("parentIds") or [])]:
            write_hier(child["id"], depth + 1, seen)

    for h in [n for n in nodes if n["type"] == "HAZARD"]:
        write_hier(h["id"], 0, set())
        row += 1  # blank line between hazards
    for ci, w in enumerate([50, 16, 16, 24], 1):
        ws2.column_dimensions[get_column_letter(ci)].width = w

    buf=io.BytesIO(); wb.save(buf); buf.seek(0)
    return buf.getvalue()

# ── Tree HTML builder ─────────────────────────────────────────────────────
def build_html_tree(nodes, filter_hazard_id=None, tree_state=None):
    """
    Sugiyama column layout: each SF owns a horizontal column.
    FFs/IFs grouped under their SF column.
    Per-subtree force: select an SF then click Force to release only that branch.
    Full drag freedom in both force ON and OFF modes.
    """
    if not nodes: return ""
    import json as _json

    by_id = {n["id"]: n for n in nodes}
    if filter_hazard_id:
        visible = set()
        q = [filter_hazard_id]
        while q:
            cur = q.pop()
            if cur in visible: continue
            visible.add(cur)
            for child in [n for n in nodes if cur in (n.get("parentIds") or [])]:
                q.append(child["id"])
        show_nodes = [n for n in nodes if n["id"] in visible]
    else:
        show_nodes = nodes
    if not show_nodes: return ""

    shown_ids  = {n["id"] for n in show_nodes}
    shared_ids = {n["id"] for n in show_nodes
                  if len([p for p in (n.get("parentIds") or []) if p in shown_ids]) > 1}

    # Duplicate detection: nodes with same nodeId but different internal id
    from collections import defaultdict as _dd
    nid_groups = _dd(list)
    for n in show_nodes:
        nid = (n.get("nodeId") or "").strip()
        if nid:
            nid_groups[nid].append(n["id"])
    duplicate_ids = {iid for group in nid_groups.values() if len(group) > 1 for iid in group}

    ts        = tree_state or {}
    init_pos  = ts.get("positions", {})
    focus_id  = ts.get("focus_id")

    LEVEL_ROW   = {"HAZARD": 0, "SF": 1, "FF": 2, "GROUP": 2.5, "IF": 3}
    LEVEL_COLOR = {0: "#ff4d4d", 1: "#ff8c42", 2: "#f5c518", 2.5: "#7e57c2", 3: "#4caf7d"}
    LEVEL_LABEL = {0: "HAZARD", 1: "SF", 2: "FF", 2.5: "GROUP", 3: "IF"}

    # ── Compute per-node depth so same-type parent/child never share a row ──
    # Start from HAZARD=0, propagate depth = max(parent_depth)+1 downward
    _depth_map = {}
    def _get_depth(nid, _seen=None):
        if nid in _depth_map: return _depth_map[nid]
        if _seen is None: _seen = set()
        if nid in _seen: return 0
        _seen = _seen | {nid}
        node = next((n for n in show_nodes if n["id"] == nid), None)
        if node is None: return 0
        pids = [p for p in (node.get("parentIds") or []) if p in shown_ids]
        if not pids:
            _depth_map[nid] = 0
        else:
            _depth_map[nid] = max(_get_depth(p, _seen) for p in pids) + 1
        return _depth_map[nid]
    for n in show_nodes:
        _get_depth(n["id"])

    nodes_js = _json.dumps([{
        "id":          n["id"],
        "name":        n["name"],
        "type":        n["type"],
        "gate":        n["gate"],
        "value":       fmt(n.get("calculatedValue")),
        "nodeId":      n.get("nodeId", n["id"]),
        "ftLabel":     n.get("ftLabel", ""),
        "shared":      n["id"] in shared_ids,
        "isDuplicate": n["id"] in duplicate_ids,
        "isPinned":    n.get("fixedValue") is not None,
        "fixedVal":    fmt(n.get("fixedValue")) if n.get("fixedValue") is not None else None,
        "isGroup":     n["type"] == "GROUP",
        "color":       LEVEL_COLORS.get(n["type"], "#7e57c2"),
        "tcolor":      LEVEL_TEXT.get(n["type"], "#fff"),
        "row":         _depth_map.get(n["id"], LEVEL_ROW.get(n["type"], 2)),
        "parents":     [p for p in (n.get("parentIds") or []) if p in shown_ids],
        "pnames":      [by_id[p]["name"] for p in (n.get("parentIds") or []) if p in shown_ids],
        "children":    [c["id"] for c in show_nodes if n["id"] in (c.get("parentIds") or [])],
        "cnames":      [c["name"] for c in show_nodes if n["id"] in (c.get("parentIds") or [])],
    } for n in show_nodes])

    links_js = _json.dumps([
        {"sid": pid, "tid": n["id"],
         "andGate": by_id[pid]["gate"] == "AND" if pid in by_id else False,
         "shared":  n["id"] in shared_ids}
        for n in show_nodes
        for pid in (n.get("parentIds") or [])
        if pid in shown_ids
    ])

    init_pos_js    = _json.dumps(init_pos)
    focus_js       = f'"{focus_id}"' if focus_id else "null"
    level_label_js = _json.dumps({str(k): v for k, v in LEVEL_LABEL.items()})
    level_color_js = _json.dumps({str(k): v for k, v in LEVEL_COLOR.items()})

    html = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
*{box-sizing:border-box;margin:0;padding:0;}
body{background:#0a0a0a;font-family:\'JetBrains Mono\',\'Fira Code\',monospace;
     color:#e0e0e0;overflow:hidden;height:100vh;display:flex;flex-direction:column;}
#toolbar{display:flex;align-items:center;gap:7px;padding:7px 14px;
  background:#111;border-bottom:2px solid #1a1a1a;flex-shrink:0;user-select:none;height:46px;}
.sep{width:1px;height:24px;background:#2a2a2a;flex-shrink:0;}
.btn{background:#1c1c1c;border:1.5px solid #2e2e2e;color:#bbb;border-radius:6px;
  padding:0 13px;cursor:pointer;font-family:inherit;font-size:11px;font-weight:700;
  letter-spacing:.4px;transition:.12s;white-space:nowrap;flex-shrink:0;
  height:30px;display:inline-flex;align-items:center;gap:4px;}
.btn:hover{background:#282828;color:#fff;border-color:#555;}
.btn.on{background:#e94560;border-color:#e94560;color:#fff;}
#zlbl{color:#666;font-size:11px;min-width:44px;text-align:center;font-weight:700;}
#fst{font-size:10px;color:#f5c518;padding:2px 9px;border-radius:5px;
  background:#1a1300;border:1px solid #f5c51844;white-space:nowrap;display:none;}
#fst.show{display:inline-block;}
#swrap{display:flex;align-items:center;gap:5px;margin-left:auto;max-width:290px;}
#sbox{background:#1c1c1c;border:1.5px solid #2e2e2e;color:#ccc;border-radius:6px;
  padding:0 10px;font-family:inherit;font-size:11px;outline:none;width:170px;height:30px;}
#sbox:focus{border-color:#e94560;color:#fff;}
#sbox::placeholder{color:#444;}
#si{color:#666;font-size:10px;min-width:55px;}
#wrap{flex:1;position:relative;overflow:hidden;}
svg{position:absolute;inset:0;width:100%;height:100%;}
#lanes{position:absolute;inset:0;pointer-events:none;z-index:2;overflow:hidden;}
.lb{position:absolute;left:0;right:0;border-top:1px solid rgba(255,255,255,.035);
  display:flex;align-items:flex-start;padding-top:5px;}
.lt{margin-left:8px;font-size:8px;font-weight:700;letter-spacing:2.5px;opacity:.36;}
.ls{position:absolute;left:0;top:0;width:3px;height:100%;opacity:.5;}
#dp{position:absolute;bottom:0;left:0;right:0;background:rgba(8,8,8,.93);
  border-top:2px solid #222;padding:8px 16px 10px;display:none;z-index:20;
  backdrop-filter:blur(14px);}
.dg{display:grid;grid-template-columns:repeat(6,1fr);gap:5px;margin:5px 0;}
.dc{background:#111;border-radius:5px;padding:6px;text-align:center;}
.dcl{font-size:7px;color:#555;letter-spacing:2px;margin-bottom:2px;font-weight:700;}
.dcv{font-size:11px;font-weight:700;word-break:break-all;}
.dr{display:grid;grid-template-columns:1fr 1fr;gap:5px;}
.ds{background:#111;border-radius:5px;padding:6px;}
.dsl{font-size:7px;color:#555;letter-spacing:2px;margin-bottom:2px;font-weight:700;}
.dsv{font-size:10px;color:#bbb;line-height:1.5;}
#dpc{position:absolute;top:8px;right:12px;background:none;border:none;color:#555;font-size:17px;cursor:pointer;}
#dpc:hover{color:#fff;}
</style>
</head><body>
<div id="toolbar">
  <button class="btn" onclick="zBy(.22)">&#65291;</button>
  <button class="btn" onclick="zBy(-.22)">&#65293;</button>
  <span id="zlbl">85%</span>
  <div class="sep"></div>
  <button class="btn" id="blay" onclick="doColumnLayout(true)" title="Reset to clean column layout">&#8862; Reset Layout</button>
  <button class="btn" onclick="doFit()">&#8865; Fit</button>
  <div class="sep"></div>
  <button class="btn" id="bfrc" onclick="toggleForce()" title="Select an SF node first to release only that subtree to physics">&#9889; Force</button>
  <span id="fst"></span>
  <div class="sep"></div>
  <button class="btn" onclick="clearHL()">&#10005; Clear</button>
  <div id="swrap">
    <input id="sbox" placeholder="Search&#8230;" oninput="doSearch(this.value)"/>
    <button class="btn" onclick="sPrev()">&#9650;</button>
    <button class="btn" onclick="sNext()">&#9660;</button>
    <span id="si"></span>
  </div>
</div>
<div id="wrap">
  <div id="lanes"></div>
  <svg id="sv">
    <defs>
      <marker id="ma"  markerWidth="8" markerHeight="8" refX="1" refY="4" orient="auto-start-reverse"><path d="M0,0 L8,4 L0,8 Z" fill="#333"/></marker>
      <marker id="mah" markerWidth="8" markerHeight="8" refX="1" refY="4" orient="auto-start-reverse"><path d="M0,0 L8,4 L0,8 Z" fill="#4fc3f7"/></marker>
      <marker id="mast" markerWidth="8" markerHeight="8" refX="1" refY="4" orient="auto-start-reverse"><path d="M0,0 L8,4 L0,8 Z" fill="#333"/></marker>
      <marker id="masth" markerWidth="8" markerHeight="8" refX="1" refY="4" orient="auto-start-reverse"><path d="M0,0 L8,4 L0,8 Z" fill="#4fc3f7"/></marker>
    </defs>
    <g id="zg"><g id="lg"></g><g id="ng"></g></g>
  </svg>
</div>
<div id="dp">
  <button id="dpc" onclick="closeDP()">&#10005;</button>
  <div style="font-size:8px;color:#555;letter-spacing:3px;margin-bottom:3px">SELECTED NODE</div>
  <div id="dpt" style="font-size:14px;font-weight:700;margin-bottom:5px"></div>
  <div class="dg">
    <div class="dc"><div class="dcl">TYPE</div><div class="dcv" id="d0"></div></div>
    <div class="dc"><div class="dcl">GATE</div><div class="dcv" id="d1"></div></div>
    <div class="dc"><div class="dcl">VALUE</div><div class="dcv" id="d2"></div></div>
    <div class="dc"><div class="dcl">NODE ID</div><div class="dcv" id="d3" style="font-size:9px"></div></div>
    <div class="dc"><div class="dcl">FT LABEL</div><div class="dcv" id="d7" style="font-size:9px;color:#7e57c2"></div></div>
    <div class="dc"><div class="dcl">SHARED</div><div class="dcv" id="d4"></div></div>
  </div>
  <div class="dr">
    <div class="ds"><div class="dsl">PARENTS</div><div class="dsv" id="d5"></div></div>
    <div class="ds"><div class="dsl">CHILDREN</div><div class="dsv" id="d6"></div></div>
  </div>
</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.8.5/d3.min.js"></script>
<script>
const RNODES=__NODES__;
const RLINKS=__LINKS__;
const IPOS=__IPOS__;
const FOCUSID=__FOCUS__;
const LLABELS=__LLABELS__;
const LCOLORS=__LCOLORS__;
const GC={OR:"#4fc3f7",AND:"#ffb74d"};
const NW=160,NH=88,HG=20,VG=185;
let selId=null,forceOn=false,forceSub=null;
let collapsed=new Set(),sM=[],sI=0;
const uP={};
Object.entries(IPOS).forEach(([id,p])=>uP[id]={x:p.x,y:p.y});
const NM={};
RNODES.forEach(n=>NM[n.id]=n);
const wrap=document.getElementById("wrap");
const svg=d3.select("#sv");
const zg=svg.select("#zg"),lg=svg.select("#lg"),ng=svg.select("#ng");
const zb=d3.zoom().scaleExtent([.03,6])
  .filter(e=>e.button===2||e.type==="wheel")
  .on("zoom",e=>{zg.attr("transform",e.transform);document.getElementById("zlbl").textContent=Math.round(e.transform.k*100)+"%";updateLanes();});
svg.call(zb).on("contextmenu",e=>e.preventDefault());
svg.on("click",()=>closeDP());
function getT(){return d3.zoomTransform(svg.node());}
function layY(r){return 80+r*VG;}
let sN=[],sL=[];
const sim=d3.forceSimulation()
  .force("link",d3.forceLink().id(d=>d.id).distance(130).strength(.4))
  .force("charge",d3.forceManyBody().strength(-500).distanceMax(380))
  .force("collide",d3.forceCollide(88))
  .force("y",d3.forceY(d=>layY(d.row)).strength(.5))
  .force("x",d3.forceX(d=>d.bx||400).strength(.06))
  .alphaDecay(.025).velocityDecay(.42)
  .on("tick",tick);
sim.stop();

// ── visible nodes ─────────────────────────────────────────────────────
function getVis(){
  const h=new Set();
  collapsed.forEach(cid=>{const q=[cid];while(q.length){const c=q.shift();(NM[c]?.children||[]).forEach(ch=>{if(!h.has(ch)){h.add(ch);q.push(ch);}});}});
  return RNODES.filter(n=>!h.has(n.id));
}

// ── subtree ids ────────────────────────────────────────────────────────
function subIds(id){
  const s=new Set([id]),q=[id];
  while(q.length){const c=q.shift();(NM[c]?.children||[]).forEach(ch=>{if(!s.has(ch)&&sN.find(n=>n.id===ch)){s.add(ch);q.push(ch);}});}
  return s;
}

// ── find which SF owns a node ──────────────────────────────────────────
function ownerSF(id,depth){
  if(depth>8) return null;
  const n=NM[id]; if(!n) return null;
  if(n.type==="SF") return id;
  for(const pid of (n.parents||[])){const r=ownerSF(pid,depth+1);if(r)return r;}
  return null;
}

// ── column layout ─────────────────────────────────────────────────────
function refresh(){
  const vis=getVis(); const ex={};
  sN.forEach(n=>ex[n.id]=n);
  sN=vis.map(n=>Object.assign({...n},{x:ex[n.id]?.x??uP[n.id]?.x??null,y:ex[n.id]?.y??uP[n.id]?.y??null,vx:0,vy:0,fx:null,fy:null,bx:500}));
  const vs=new Set(sN.map(n=>n.id));
  sL=RLINKS.filter(l=>vs.has(l.sid)&&vs.has(l.tid)).map(l=>{const s=sN.find(n=>n.id===l.sid),t=sN.find(n=>n.id===l.tid);return s&&t?{source:s,target:t,andGate:l.andGate,shared:l.shared}:null;}).filter(Boolean);
  sim.nodes(sN); sim.force("link").links(sL); sim.force("x").x(d=>d.bx||400);
  computeRTLayout(false); drawLinks(); drawNodes(); tick(); updateLanes();
  if(forceOn) sim.alpha(.3).restart();
}

// ── draw links ─────────────────────────────────────────────────────────
function drawLinks(){
  const s=lg.selectAll("path.lk").data(sL,d=>d.source.id+"->"+d.target.id);
  const a=s.enter().append("path").attr("class","lk").attr("fill","none").merge(s);
  a.attr("stroke",d=>d.shared?"#f5c51855":"#2d2d2d")
   .attr("stroke-width",d=>d.shared?1.2:2)
   .attr("stroke-dasharray",d=>d.andGate?"8,4":d.shared?"4,6":null)
   .attr("opacity",d=>d.shared?.5:.92)
   .attr("marker-end","none").attr("marker-start","url(#mast)");
  s.exit().remove();
}

// ── draw nodes ─────────────────────────────────────────────────────────
function drawNodes(){
  const s=ng.selectAll("g.nd").data(sN,d=>d.id);
  const ent=s.enter().append("g").attr("class","nd").style("cursor","grab")
    .on("click",(e,d)=>{e.stopPropagation();selectNode(d.id);})
    .on("dblclick",(e,d)=>{e.stopPropagation();toggleCollapse(d.id);})
    .call(d3.drag().filter(e=>e.button===0)
      .on("start",(e,d)=>{e.sourceEvent.stopPropagation();if(forceOn&&!e.active)sim.alphaTarget(.05).restart();d.fx=d.x;d.fy=d.y;})
      .on("drag",(e,d)=>{d.x=d.fx=e.x;d.y=d.fy=e.y;uP[d.id]={x:e.x,y:e.y,manual:true};tick();})
      .on("end",(e,d)=>{if(forceOn&&!e.active)sim.alphaTarget(0);const inF=forceOn&&forceSub&&subIds(forceSub).has(d.id);if(!inF){d.fx=d.x;d.fy=d.y;}else{d.fx=null;d.fy=null;}})
    );
  ent.append("rect").attr("class","nb");
  ent.append("text").attr("class","nt").attr("text-anchor","middle").attr("font-size",7).attr("font-weight",700).attr("font-family","JetBrains Mono,monospace").attr("letter-spacing",2).attr("opacity",.65);
  ent.append("foreignObject").attr("class","nf").append("xhtml:div").attr("xmlns","http://www.w3.org/1999/xhtml").style("font-size","9.5px").style("font-weight","700").style("font-family","JetBrains Mono,monospace").style("text-align","center").style("word-break","break-word").style("line-height","1.25").style("overflow","hidden");
  ent.append("rect").attr("class","vb").attr("height",19).attr("rx",4).attr("fill","rgba(0,0,0,.3)");
  ent.append("text").attr("class","vt").attr("text-anchor","middle").attr("font-size",10).attr("font-weight",700).attr("font-family","JetBrains Mono,monospace");
  ent.append("g").attr("class","gb"); ent.append("g").attr("class","cb");

  const all=ent.merge(s);
  all.select("rect.nb")
    .attr("width",d=>d.isGroup?126:NW).attr("height",d=>d.isGroup?68:NH)
    .attr("x",d=>d.isGroup?-63:-NW/2).attr("y",d=>d.isGroup?-34:-NH/2)
    .attr("rx",d=>d.isGroup?63:9).attr("ry",d=>d.isGroup?34:9)
    .attr("fill",d=>d.color)
    .attr("stroke",d=>{
      if(d.isDuplicate) return "#4fc3f7";
      if(forceSub===d.id&&forceOn) return "#f5c518";
      if(d.isPinned) return "#e94560";
      return d.color;
    })
    .attr("stroke-width",d=>d.isDuplicate?2.5:forceSub===d.id&&forceOn?3:d.isPinned?2.5:1.5)
    .attr("stroke-dasharray",d=>d.isDuplicate?"6,3":d.isPinned?"6,3":null)
    .attr("opacity",d=>d.isDuplicate?1:1);
  all.select("text.nt").attr("y",d=>d.isGroup?-16:-NH/2+13).attr("fill",d=>d.tcolor)
    .text(d=>d.isGroup?"COMBINED":d.type+(d.shared?" ◈":"")+(d.isPinned?" 📌":"")+(d.isDuplicate?" ◈ DUP":""));
  all.select("foreignObject.nf")
    .attr("x",d=>d.isGroup?-57:-NW/2+7).attr("y",d=>d.isGroup?-22:-NH/2+17)
    .attr("width",d=>d.isGroup?114:NW-14).attr("height",d=>d.isGroup?28:40)
    .select("div").style("color",d=>d.tcolor).text(d=>d.name);
  all.select("rect.vb").attr("x",d=>d.isGroup?-47:-NW/2+9).attr("y",d=>d.isGroup?11:NH/2-24).attr("width",d=>d.isGroup?94:NW-18);
  all.select("text.vt").attr("y",d=>d.isGroup?25:NH/2-10).attr("fill",d=>d.isPinned?"#e94560":d.tcolor).text(d=>d.value+(d.isPinned?" 📌":""));

  all.each(function(d){
    const hc=(d.children||[]).length>0;
    const gb=d3.select(this).select("g.gb"); gb.selectAll("*").remove();
    if(hc){
      // Gate badge at TOP of node — where child arrows arrive
      gb.append("rect").attr("x",-19).attr("y",-NH/2-18).attr("width",38).attr("height",15).attr("rx",4).attr("fill","#0d0d0d").attr("stroke",GC[d.gate]||"#aaa").attr("stroke-width",1.2);
      gb.append("text").attr("y",-NH/2-7).attr("text-anchor","middle").attr("fill",GC[d.gate]||"#aaa").attr("font-size",8).attr("font-weight",700).attr("font-family","JetBrains Mono,monospace").attr("letter-spacing",1).text(d.gate);
    }
    const cb=d3.select(this).select("g.cb"); cb.selectAll("*").remove();
    if(hc){
      // Collapse button at bottom-right of node
      const cx=d.isGroup?53:NW/2-10,cy=NH/2-10;
      cb.append("circle").attr("cx",cx).attr("cy",cy).attr("r",9).attr("fill","#1c1c1c").attr("stroke","#3a3a3a").attr("stroke-width",1.2).style("cursor","pointer").on("click",(e,dd)=>{e.stopPropagation();toggleCollapse(dd.id);});
      cb.append("text").attr("x",cx).attr("y",cy+5).attr("text-anchor","middle").attr("fill","#888").attr("font-size",12).attr("font-weight",700).attr("font-family","monospace").attr("pointer-events","none").text(collapsed.has(d.id)?"+":"−");
    }
  });
  s.exit().remove();
}

function tick(){
  lg.selectAll("path.lk").attr("d",d=>{
    if(!d.source||!d.target)return"";
    // source = child (lower in tree), target = parent (higher in tree)
    // Draw from child's top edge up to parent's bottom edge, arrowhead at parent
    const sx=d.source.x||0,sy=(d.source.y||0)-NH/2-4;   // child top
    const tx=d.target.x||0,ty=(d.target.y||0)+NH/2+4;   // parent bottom
    const my=(sy+ty)/2;
    return `M${sx},${sy} C${sx},${my} ${tx},${my} ${tx},${ty}`;
  });
  ng.selectAll("g.nd").attr("transform",d=>`translate(${d.x||0},${d.y||0})`);
}
function computeRTLayout(reset){
  // Reingold-Tilford tree layout.
  // Key rules:
  // 1. Every leaf gets exactly SLOT px of horizontal space
  // 2. Every parent is centered over the span of its children
  // 3. Total canvas width = totalLeaves * SLOT (grows beyond viewport — use Fit to see all)
  // 4. Shared nodes: x = average of all parent positions (already placed)
  // 5. Manual drag overrides are respected until ⊞ Reset Layout is pressed

  const vis    = getVis();
  const visSet = new Set(vis.map(n=>n.id));
  const SLOT   = NW + HG;   // px per leaf slot

  // ── 1. Build child map from visible nodes ──────────────────────────
  const childMap = {};
  vis.forEach(n=>{ childMap[n.id] = []; });
  vis.forEach(n=>{
    (n.parents||[]).forEach(pid=>{
      if(visSet.has(pid) && childMap[pid]) childMap[pid].push(n.id);
    });
  });

  // ── 2. Count leaves in each subtree ────────────────────────────────
  const lc = {};
  function cLeaves(id){
    if(lc[id] !== undefined) return lc[id];
    const ch = childMap[id]||[];
    lc[id] = ch.length ? ch.reduce((s,c)=>s+cLeaves(c), 0) : 1;
    return lc[id];
  }
  vis.forEach(n=>cLeaves(n.id));

  // ── 3. Assign x positions ──────────────────────────────────────────
  // posMap[id] = center x of node
  const posMap = {};
  const MARGIN = 80;

  function assignX(id, left, width){
    const ch = childMap[id]||[];
    if(!ch.length){
      // Leaf: center in its exact slot
      posMap[id] = left + width/2;
      return;
    }
    // Distribute slice evenly proportional to each child's leaf count
    const tot = lc[id] || 1;
    let cur = left;
    ch.forEach(cid=>{
      // Each child's slice = (its leaves / parent total leaves) * parent width
      // Minimum = exactly SLOT so nodes never overlap
      const childW = Math.max(SLOT, (lc[cid]/tot) * width);
      assignX(cid, cur, childW);
      cur += childW;
    });
    // Parent x = midpoint between leftmost and rightmost child
    const xs = ch.map(c=>posMap[c]);
    posMap[id] = (Math.min(...xs) + Math.max(...xs)) / 2;
  }

  // Find roots (nodes with no visible parent)
  const roots = vis.filter(n=>(n.parents||[]).every(p=>!visSet.has(p)));

  // Total canvas width driven by leaf count — not by screen width
  // This means the tree can be wider than the screen; user zooms/pans to navigate
  const totalLeaves = roots.reduce((s,r)=>s+cLeaves(r.id), 0) || 1;
  const totalWidth  = Math.max(totalLeaves * SLOT + MARGIN*2, 1200);

  let cur = MARGIN;
  roots.forEach(r=>{
    const w = Math.max((lc[r.id]/totalLeaves) * (totalWidth - MARGIN*2), SLOT*2);
    assignX(r.id, cur, w);
    cur += w;
  });

  // ── 4. Apply positions to sim nodes ───────────────────────────────
  vis.forEach(n=>{
    const sn = sN.find(s=>s.id===n.id); if(!sn) return;

    // Skip nodes in active force subtree
    const inForce = forceOn && forceSub && subIds(forceSub).has(n.id);
    if(inForce){ sn.fx=null; sn.fy=null; return; }

    // Respect manual drag unless resetting
    if(!reset && uP[n.id]?.manual){
      sn.x=uP[n.id].x; sn.y=uP[n.id].y;
      sn.fx=sn.x; sn.fy=sn.y;
      return;
    }

    const nx = posMap[n.id] ?? sn.x ?? totalWidth/2;
    const ny = layY(n.row);
    sn.x=nx; sn.y=ny; sn.bx=nx;
    sn.fx=nx; sn.fy=ny;
    uP[n.id] = {x:nx, y:ny, manual:false};
  });
}

function doColumnLayout(reset){
  if(forceOn) stopForce();
  if(reset) sN.forEach(n=>{delete uP[n.id];n.fx=null;n.fy=null;});
  computeRTLayout(reset);
  tick(); updateLanes();
  setTimeout(doFit,80);
}

function toggleForce(){
  if(forceOn){stopForce();return;}
  startForce();
}
function startForce(){
  const btn=document.getElementById("bfrc");
  const st=document.getElementById("fst");
  btn.classList.add("on"); btn.textContent="⚡ Force ON";
  forceSub=selId&&NM[selId]?.type==="SF"?selId:null;
  forceOn=true;
  const ids=forceSub?subIds(forceSub):new Set(sN.map(n=>n.id));
  sN.forEach(n=>{if(ids.has(n.id)){n.fx=null;n.fy=null;}else{n.fx=n.x;n.fy=n.y;}});
  st.textContent=forceSub?"⚡ "+NM[forceSub].name+" subtree":"⚡ Full tree";
  st.classList.add("show");
  sim.alpha(.45).restart();
  drawNodes();
}
function stopForce(){
  document.getElementById("bfrc").classList.remove("on");
  document.getElementById("bfrc").textContent="⚡ Force";
  document.getElementById("fst").classList.remove("show");
  forceOn=false; forceSub=null;
  sim.stop();
  sN.forEach(n=>{n.fx=n.x;n.fy=n.y;uP[n.id]={x:n.x,y:n.y,manual:true};});
  drawNodes();
}

function doFit(){
  if(!sN.length)return;
  const xs=sN.map(n=>n.x||0),ys=sN.map(n=>n.y||0);
  const x0=Math.min(...xs)-NW,x1=Math.max(...xs)+NW;
  const y0=Math.min(...ys)-NH,y1=Math.max(...ys)+NH;
  const cw=wrap.getBoundingClientRect();
  const W=cw.width||1000,H=cw.height||650;
  const k=Math.min(.96,.9*Math.min(W/(x1-x0),H/(y1-y0)));
  svg.transition().duration(500).call(zb.transform,d3.zoomIdentity.translate(W/2-k*(x0+x1)/2,H/2-k*(y0+y1)/2).scale(k));
  setTimeout(updateLanes,520);
}
function zBy(d){svg.transition().duration(180).call(zb.scaleBy,1+d);setTimeout(updateLanes,200);}

function toggleCollapse(id){collapsed.has(id)?collapsed.delete(id):collapsed.add(id);refresh();}

function selectNode(id){
  if(selId===id){selId=null;clearHL();closeDP();return;}
  selId=id;
  const n=NM[id]; if(!n)return;
  const par=new Set(n.parents||[]),chi=new Set(n.children||[]);
  ng.selectAll("g.nd").each(function(d){
    const el=d3.select(this),r=el.select("rect.nb");
    if(d.id===id){r.attr("stroke","#e94560").attr("stroke-width",3.5).attr("filter","drop-shadow(0 0 12px #e9456088)");el.attr("opacity",1);}
    else if(par.has(d.id)){r.attr("stroke","#4fc3f7").attr("stroke-width",3).attr("filter","drop-shadow(0 0 9px #4fc3f755)");el.attr("opacity",1);}
    else if(chi.has(d.id)){r.attr("stroke","#ff8c42").attr("stroke-width",3).attr("filter","drop-shadow(0 0 9px #ff8c4255)");el.attr("opacity",1);}
    else{r.attr("stroke",d.isPinned?"#e94560":d.color).attr("stroke-width",d.isPinned?2:1.5).attr("filter",null);el.attr("opacity",.15);}
  });
  lg.selectAll("path.lk").each(function(d){
    const on=d.source.id===id||d.target.id===id;
    d3.select(this).attr("stroke",on?(d.shared?"#f5c518":"#4fc3f7"):"#1a1a1a").attr("stroke-width",on?2.8:1).attr("opacity",on?1:.07).attr("marker-start",on?"url(#masth)":"url(#mast)").attr("marker-end","none");
  });
  if(n.type==="SF"&&!forceOn) document.getElementById("bfrc").title="Click to release \\\'"+n.name+"\\' subtree to physics";
  showDP(id,n);
}

function clearHL(){
  selId=null;
  ng.selectAll("g.nd").attr("opacity",1).select("rect.nb")
    .attr("stroke",d=>{
      if(d.isDuplicate) return "#4fc3f7";
      if(forceSub===d.id&&forceOn) return "#f5c518";
      if(d.isPinned) return "#e94560";
      return d.color;
    })
    .attr("stroke-width",d=>d.isDuplicate?2.5:forceSub===d.id&&forceOn?3:d.isPinned?2.5:1.5)
    .attr("stroke-dasharray",d=>d.isDuplicate?"6,3":d.isPinned?"6,3":null)
    .attr("filter",null);
  lg.selectAll("path.lk").attr("stroke",d=>d.shared?"#f5c51844":"#2d2d2d").attr("stroke-width",d=>d.shared?1.2:2).attr("opacity",d=>d.shared?.5:.92).attr("marker-start","url(#mast)").attr("marker-end","none");
}

const GCM={OR:"#4fc3f7",AND:"#ffb74d"};
function showDP(id,n){
  const dp=document.getElementById("dp"); dp.style.display="block"; dp.style.borderTopColor=n.color;
  document.getElementById("dpt").innerHTML=`<span style="color:${n.color}">${n.name}</span>`+(n.shared?\' <span style="background:#f5c518;color:#111;font-size:8px;padding:1px 6px;border-radius:5px;font-weight:700">SHARED</span>\':\'\')+( n.isPinned?` <span style="background:#e94560;color:#fff;font-size:8px;padding:1px 6px;border-radius:5px;font-weight:700">&#128204; FIXED ${n.fixedVal}</span>`:\'\');
  const q=(id,v,c)=>{const e=document.getElementById(id);e.textContent=v;if(c)e.style.color=c;};
  q("d0",n.isGroup?"GROUP":n.type,n.color);q("d1",n.gate,GCM[n.gate]||"#aaa");
  q("d2",n.isPinned?n.value+" 📌":n.value,n.isPinned?"#e94560":n.color);
  q("d3",n.nodeId||id,"#aaa");
  q("d7",n.ftLabel||"—","#7e57c2");
  q("d4",n.shared?"YES":"NO",n.shared?"#f5c518":"#555");
  q("d5",(n.pnames||[]).join(" · ")||"(top event)");q("d6",(n.cnames||[]).join(" · ")||"(leaf)");
}
function closeDP(){document.getElementById("dp").style.display="none";clearHL();}

function doSearch(q){
  ng.selectAll("rect.nb").attr("filter",null);sM=[];sI=0;
  if(!q.trim()){document.getElementById("si").textContent="";return;}
  const lq=q.toLowerCase();
  RNODES.forEach(n=>{if([n.name,n.type,n.value,n.nodeId||""].some(v=>v.toLowerCase().includes(lq)))sM.push(n.id);});
  document.getElementById("si").textContent=sM.length?sM.length+" found":"0";
  ng.selectAll("g.nd").filter(d=>sM.includes(d.id)).select("rect.nb").attr("filter","drop-shadow(0 0 10px #f5c518)");
  if(sM.length)panTo(sM[0]);
}
function sNext(){if(!sM.length)return;sI=(sI+1)%sM.length;panTo(sM[sI]);document.getElementById("si").textContent=`${sI+1}/${sM.length}`;}
function sPrev(){if(!sM.length)return;sI=(sI-1+sM.length)%sM.length;panTo(sM[sI]);document.getElementById("si").textContent=`${sI+1}/${sM.length}`;}
function panTo(id){
  const n=sN.find(s=>s.id===id);if(!n)return;
  const t=getT(),cw=wrap.getBoundingClientRect();
  svg.transition().duration(400).call(zb.transform,d3.zoomIdentity.translate((cw.width||1000)/2-t.k*(n.x||0),(cw.height||650)/3-t.k*(n.y||0)).scale(t.k));
  setTimeout(updateLanes,420);
}

refresh();
doColumnLayout(true);
if(FOCUSID) setTimeout(()=>panTo(FOCUSID),700);

// ── Position save-back ────────────────────────────────────────────────
// Sends current node positions to Python via postMessage so they survive
// page rerenders (CALCULATE, file load, etc.)
function savePositions(){
  const pos={};
  sN.forEach(n=>{if(n.x!=null)pos[n.id]={x:Math.round(n.x),y:Math.round(n.y),manual:!!(uP[n.id]?.manual)};});
  try{
    window.parent.postMessage(JSON.stringify({type:"fta_pos",data:pos}),"*");
  }catch(e){}
}
// Save after drag ends — already handled via uP, but also save on stop-force
// and periodically every 30s in case of drift
setInterval(savePositions, 30000);
// Save when user stops interacting
document.addEventListener("pointerup", ()=>setTimeout(savePositions,500));
</script></body></html>"""

    # Substitute data placeholders
    html = html.replace("__NODES__", nodes_js)
    html = html.replace("__LINKS__", links_js)
    html = html.replace("__IPOS__",  init_pos_js)
    html = html.replace("__FOCUS__", focus_js)
    html = html.replace("__LLABELS__", level_label_js)
    html = html.replace("__LCOLORS__", level_color_js)
    return html

def build_hierarchy_rows(nodes, filter_hazard_id=None):
    by_id = {n["id"]: n for n in nodes}
    rows, visited = [], set()
    def walk(nid, depth):
        is_ref = nid in visited
        if not is_ref: visited.add(nid)
        node = by_id.get(nid)
        if not node: return
        rows.append({"node": node, "depth": depth, "ref": is_ref})
        if not is_ref:
            for child in [n for n in nodes if nid in (n.get("parentIds") or [])]:
                walk(child["id"], depth + 1)
    starts = ([n for n in nodes if n["type"]=="HAZARD" and n["id"]==filter_hazard_id]
              if filter_hazard_id else [n for n in nodes if n["type"]=="HAZARD"])
    for h in starts: walk(h["id"], 0)
    return rows

# ── Session state ─────────────────────────────────────────────────────────
DEFS = {"nodes":[],"save_status":"idle","save_msg":"","gist_loaded":False,
        "active_file":"my_tree.json","file_list":[],"selected_id":None,
        "tree_filter":"ALL",
        "nodes_since_calc": 0,
        "pending_node_names": [],   # names of nodes added without calculating
        "nodes_hash": "",
        "tree_state": {
            "scale": 1.0, "tx": 0, "ty": 0,
            "collapsed": [],
            "positions": {},
            "focus_id": None,
        }
        }
for k,v in DEFS.items():
    if k not in st.session_state: st.session_state[k] = v

def get_secret(k):
    try: return st.secrets[k]
    except: return os.environ.get(k,"")

GITHUB_TOKEN = get_secret("GITHUB_TOKEN")
GIST_ID      = get_secret("GIST_ID")
configured   = bool(GITHUB_TOKEN and GIST_ID)

# ── Load on first run ─────────────────────────────────────────────────────
if configured and not st.session_state.gist_loaded:
    with st.spinner("Loading from Gist..."):
        st.session_state.file_list = list_gist_files(GITHUB_TOKEN, GIST_ID)
        af = st.session_state.active_file
        if af in st.session_state.file_list:
            st.session_state.nodes = load_gist_file(GITHUB_TOKEN, GIST_ID, af)
        elif st.session_state.file_list:
            named = [f for f in st.session_state.file_list if is_named(f)]
            if named:
                st.session_state.active_file = named[0]
                st.session_state.nodes = load_gist_file(GITHUB_TOKEN, GIST_ID, named[0])
        st.session_state.gist_loaded = True
        st.session_state.save_status = "loaded"
        st.session_state.save_msg = f"Loaded '{st.session_state.active_file}'"

def save_current(nodes=None, filename=None, status_label=None):
    if nodes   is None: nodes    = st.session_state.nodes
    if filename is None: filename = st.session_state.active_file
    if configured:
        ok = save_gist_file(GITHUB_TOKEN, GIST_ID, filename, nodes)
        st.session_state.save_status = "saved" if ok else "error"
        st.session_state.save_msg = (status_label or
            f"Saved '{filename}' at {datetime.now().strftime('%H:%M:%S')}") if ok else "Save failed"
        st.session_state.file_list = list_gist_files(GITHUB_TOKEN, GIST_ID)
        return ok
    st.session_state.save_status = "no_config"
    st.session_state.save_msg = "Gist not configured"
    return False

def set_nodes(n, recalc=False):
    """Save nodes. By default does NOT recalculate — call with recalc=True or
    use CALCULATE button. This prevents unwanted tree thrashing while building."""
    if recalc:
        n = recalculate(n)
        st.session_state.nodes_since_calc = 0
        st.session_state.pending_node_names = []
        st.session_state.nodes_hash = ""
    st.session_state.nodes = n
    save_current(n)

# ── CSS ───────────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&display=swap');
html,body,[class*="css"]{font-family:'JetBrains Mono',monospace!important;background:#0d0d0d!important;color:#e0e0e0!important;}
.stApp{background:#0d0d0d!important;}
section[data-testid="stSidebar"]{background:#111!important;border-right:1px solid #222!important;}
.stButton>button{font-family:'JetBrains Mono',monospace!important;font-weight:700!important;letter-spacing:1px!important;}
.stTabs [data-baseweb="tab"]{font-family:'JetBrains Mono',monospace!important;font-size:10px!important;}
</style>""", unsafe_allow_html=True)

# ── Data ──────────────────────────────────────────────────────────────────
nodes    = st.session_state.nodes
hazards  = [n for n in nodes if n["type"] == "HAZARD"]
by_id    = {n["id"]: n for n in nodes}
by_level = {lvl: [n for n in nodes if n["type"] == lvl] for lvl in DISPLAY_ORDER}

# ── Header ────────────────────────────────────────────────────────────────
sc = st.session_state.save_status
sc_color = {"saved":"#4caf7d","loaded":"#4caf7d","error":"#ff4d4d","no_config":"#f5c518","idle":"#888"}.get(sc,"#888")
sc_icon  = {"saved":"✓","loaded":"↓","error":"✗","no_config":"!","idle":"○"}.get(sc,"○")
st.markdown(f"""
<div style="background:linear-gradient(90deg,#1a1a2e,#16213e,#0f3460);
            border-bottom:2px solid #e94560;padding:11px 20px;
            margin:-1rem -1rem 1rem -1rem;
            display:flex;justify-content:space-between;align-items:center;">
  <div>
    <div style="font-size:19px;font-weight:700;letter-spacing:2px;color:#e94560;">
      ⚠ FTA REVERSE ENGINEER
    </div>
    <div style="font-size:9px;color:#888;letter-spacing:3px;margin-top:1px;">
      FAULT TREE ANALYSIS · TOP-DOWN DISTRIBUTION · {len(nodes)} nodes · {len(hazards)} hazard(s){f" · {sum(1 for n in nodes if n.get('fixedValue') is not None)} pinned 📌" if any(n.get('fixedValue') is not None for n in nodes) else ""}
    </div>
  </div>
  <div style="text-align:right;">
    <div style="font-size:11px;color:{sc_color};font-weight:700;">{sc_icon} {st.session_state.save_msg or "Ready"}</div>
    <div style="font-size:9px;color:#555;margin-top:1px;">Active: <span style="color:#aaa;">{st.session_state.active_file}</span></div>
  </div>
</div>""", unsafe_allow_html=True)

if not configured:
    st.warning("Gist not configured — data resets on refresh. Add GITHUB_TOKEN + GIST_ID to Streamlit secrets.")

# ── Sidebar ───────────────────────────────────────────────────────────────
@st.fragment
def render_sidebar():
    """
    @st.fragment means this function reruns in isolation when its buttons are
    clicked — the main page (and the tree iframe) is NOT re-rendered.
    The tree only re-renders when the user explicitly presses CALCULATE or
    switches the hazard filter dropdown on the main page.
    """
    nodes  = st.session_state.nodes
    by_id  = {n["id"]: n for n in nodes}
    hazards = [n for n in nodes if n["type"] == "HAZARD"]

    # FILE MANAGER
    with st.expander("📁 FILE MANAGER", expanded=False):
        st.markdown(f"<div style='font-size:10px;color:#ff8c42;font-weight:700;margin-bottom:6px;'>▶ {st.session_state.active_file}</div>", unsafe_allow_html=True)
        new_name = st.text_input("Save as name", placeholder="e.g. baseline", key="ns_name", label_visibility="collapsed")
        c1,c2 = st.columns(2)
        with c1:
            if st.button("💾 Save As", use_container_width=True):
                fn = new_name.strip()
                if fn:
                    if not fn.endswith(".json"): fn += ".json"
                    if save_current(filename=fn, status_label=f"Saved as '{fn}' at {datetime.now().strftime('%H:%M:%S')}"):
                        st.session_state.active_file = fn; st.rerun(scope="app")
        with c2:
            if st.button("📸 Snapshot", use_container_width=True):
                snap = f"snapshot_{now_str()}.json"
                save_current(filename=snap, status_label=f"Snapshot: {snap}"); st.rerun(scope="app")
        if configured:
            if st.button("🔄 Refresh", use_container_width=True):
                st.session_state.file_list = list_gist_files(GITHUB_TOKEN, GIST_ID); st.rerun(scope="app")
            named = [f for f in st.session_state.file_list if is_named(f)]
            snaps = sorted([f for f in st.session_state.file_list if is_snap(f)], reverse=True)
            if named:
                st.markdown("<div style='font-size:9px;color:#ff8c42;margin:6px 0 3px;'>NAMED FILES</div>", unsafe_allow_html=True)
                for fn in named:
                    ia = fn == st.session_state.active_file
                    ca,cb,cc = st.columns([5,2,2])
                    with ca: st.markdown(f"<div style='font-size:10px;color:{'#ff8c42' if ia else '#aaa'};padding:3px 0;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;'>{'▶ ' if ia else ''}{fn}</div>", unsafe_allow_html=True)
                    with cb:
                        if st.button("Load", key=f"l_{fn}"):
                            st.session_state.nodes = load_gist_file(GITHUB_TOKEN, GIST_ID, fn)
                            st.session_state.active_file = fn
                            st.session_state.save_status = "loaded"
                            st.session_state.save_msg = f"Loaded '{fn}'"
                            st.session_state.selected_id = None
                            st.session_state.nodes_since_calc = 0
                            st.session_state.pending_node_names = []
                            st.session_state.nodes_hash = ""
                            st.rerun(scope="app")
                    with cc:
                        if not ia and st.button("Del", key=f"d_{fn}"):
                            del_gist_file(GITHUB_TOKEN, GIST_ID, fn)
                            st.session_state.file_list = list_gist_files(GITHUB_TOKEN, GIST_ID); st.rerun(scope="app")
            if snaps:
                st.markdown("<div style='font-size:9px;color:#4fc3f7;margin:8px 0 3px;'>SNAPSHOTS (last 5)</div>", unsafe_allow_html=True)
                for fn in snaps[:5]:
                    short = fn.replace("snapshot_","").replace(".json","")
                    ca,cb,cc = st.columns([5,2,2])
                    with ca: st.markdown(f"<div style='font-size:9px;color:#4fc3f7;padding:3px 0;'>📸 {short}</div>", unsafe_allow_html=True)
                    with cb:
                        if st.button("Load", key=f"l_{fn}"):
                            st.session_state.nodes = load_gist_file(GITHUB_TOKEN, GIST_ID, fn)
                            st.session_state.active_file = fn
                            st.session_state.save_status = "loaded"
                            st.session_state.save_msg = f"Loaded snapshot '{fn}'"
                            st.session_state.selected_id = None
                            st.session_state.nodes_since_calc = 0
                            st.session_state.pending_node_names = []
                            st.session_state.nodes_hash = ""
                            st.rerun(scope="app")
                    with cc:
                        if st.button("Del", key=f"d_{fn}"):
                            del_gist_file(GITHUB_TOKEN, GIST_ID, fn)
                            st.session_state.file_list = list_gist_files(GITHUB_TOKEN, GIST_ID); st.rerun(scope="app")

    st.markdown("---")

    # NODE EDITOR
    st.markdown("### 🔧 NODE EDITOR")
    tab_add, tab_edit, tab_shared = st.tabs(["➕ ADD", "✏️ EDIT", "🔗 SHARED"])

    with tab_add:
        # Add Hazard
        st.markdown("<div style='font-size:9px;color:#ff4d4d;letter-spacing:2px;margin-bottom:4px;'>ADD HAZARD</div>", unsafe_allow_html=True)
        h_name = st.text_input("Hazard Name", placeholder="e.g. Engine Fire", key="h_name")
        h_val  = st.text_input("Target Rate", placeholder="e.g. 1e-7", key="h_val")
        if st.button("➕ ADD HAZARD", use_container_width=True):
            if h_name.strip():
                try:
                    val = float(h_val)
                    nid = str(uuid.uuid4())[:7]
                    node = {"id": nid, "nodeId": nid, "name": h_name.strip(),
                            "type": "HAZARD", "gate": "OR",
                            "targetValue": val, "calculatedValue": val, "parentIds": []}
                    st.session_state.tree_state["focus_id"] = nid
                    st.session_state.tree_filter = nid
                    set_nodes(nodes + [node])  # no recalc needed for HAZARD
                    st.rerun()
                except ValueError:
                    st.error("Invalid rate — use e.g. 1e-7")
            else:
                st.error("Enter hazard name")

        if hazards:
            st.markdown("---")
            st.markdown("<div style='font-size:9px;color:#ff8c42;letter-spacing:2px;margin-bottom:4px;'>ADD CHILD NODE</div>", unsafe_allow_html=True)

            with st.expander("💡 Mixed AND/OR gate guide"):
                st.markdown("""
**Use a GROUP node for Combined Faults:**

*SF-14 (OR) → two AND groups:*
- SF-14 gate=OR
- "Combined Faults A" type=GROUP, gate=AND, parent=SF-14 → FF-01, FF-02
- "Combined Faults B" type=GROUP, gate=AND, parent=SF-14 → IF-016, IF-208

*FF-05 (mixed):*
- FF-05 gate=OR, direct children: IF-286, IF-287, IF-288
- "Combined Faults" type=GROUP, gate=AND, parent=FF-05 → IF-293, IF-289

GROUP = purple oval. AND edges = dashed. Shared edges = yellow dashes.
                """)

            node_name = st.text_input("Node Name", placeholder="e.g. Pack mechanical damage due to crash", key="add_name")

            # ── Node ID (mandatory) — split into type prefix + number ──
            # Column 1: prefix dropdown (SF, FF, IF, GROUP)
            # Column 2: alphanumeric number (196, 23a, 56b)
            # Column 3: FT Label (optional, e.g. FT-46)
            st.markdown("<div style='font-size:9px;color:#ff8c42;font-weight:700;letter-spacing:1px;margin:4px 0 2px 0;'>NODE ID ★ required</div>", unsafe_allow_html=True)
            c_prefix, c_num, c_ft = st.columns([1, 1, 1])
            with c_prefix:
                id_prefix = st.selectbox(
                    "Type",
                    ["IF", "FF", "SF", "GROUP", "HAZ", "OTHER"],
                    key="add_id_prefix",
                    label_visibility="collapsed",
                    help="Node type prefix: IF / FF / SF / GROUP / HAZ"
                )
            with c_num:
                id_num = st.text_input(
                    "Number",
                    placeholder="e.g. 196, 23a, 56b",
                    key="add_id_num",
                    label_visibility="collapsed",
                    help="Alphanumeric part of the ID e.g. 196, 23a, 56b"
                )
            with c_ft:
                ft_label = st.text_input(
                    "FT Label",
                    placeholder="FT-46 (optional)",
                    key="add_ft",
                    label_visibility="collapsed",
                    help="Fault tree reference label e.g. FT-46"
                )

            # Compose full Node ID from prefix + number
            id_num_clean = id_num.strip()
            ft_clean     = ft_label.strip()
            cid_clean    = f"{id_prefix}-{id_num_clean}" if id_num_clean else ""

            # ── Live search: check BOTH nodeId AND name ───────────────
            # nodeId search: exact match (IF-196 ≠ IF-195)
            # Name search: case-insensitive contains (partial match)
            id_matches   = []   # exact nodeId match
            name_matches = []   # name contains typed string

            if len(cid_clean) >= 2:
                id_matches = [
                    n for n in nodes
                    if (n.get("nodeId") or "").strip() == cid_clean
                    and (n.get("nodeId") or "").strip() != ""
                ]

            if len(node_name.strip()) >= 4:
                lname = node_name.strip().lower()
                name_matches = [
                    n for n in nodes
                    if lname in (n.get("name") or "").lower()
                    and n not in id_matches  # don't double-show
                ]

            # ── Render search results ─────────────────────────────────
            def render_match_card(matches, match_type):
                """Render a match card. match_type = 'id' or 'name'"""
                for ex in matches:
                    ex_color  = LEVEL_COLORS.get(ex["type"], "#888")
                    ex_pids   = ex.get("parentIds") or []
                    ex_pnames = " · ".join(by_id[p]["name"] for p in ex_pids if p in by_id) or "—"
                    ex_val    = fmt(ex.get("calculatedValue"))
                    ex_nid    = ex.get("nodeId", ex["id"])
                    # Find which hazard(s) this node belongs to
                    def find_hazards_of(node_id, depth=0):
                        if depth > 8: return []
                        n = by_id.get(node_id)
                        if not n: return []
                        if n["type"] == "HAZARD": return [n["name"]]
                        result = []
                        for pid in (n.get("parentIds") or []):
                            result.extend(find_hazards_of(pid, depth+1))
                        return list(dict.fromkeys(result))  # dedupe
                    ex_fts = find_hazards_of(ex["id"])
                    ex_ft_str = " · ".join(ex_fts) if ex_fts else "—"

                    is_id_match = match_type == "id"
                    border_col  = "#4fc3f7"   # blue — same node, new parent
                    bg_col      = "#080f1a"
                    label       = "⚠ NODE ALREADY EXISTS"

                    st.markdown(f"""
                    <div style="background:{bg_col};border:1.5px solid {border_col};
                                border-radius:7px;padding:9px 12px;margin:3px 0 5px 0;">
                      <div style="font-size:8px;color:{border_col};font-weight:700;
                                  letter-spacing:1px;margin-bottom:4px;">{label}</div>
                      <div style="display:flex;align-items:center;gap:8px;margin-bottom:3px;">
                        <code style="background:#0a1a2e;color:{border_col};padding:1px 6px;
                               border-radius:3px;font-size:10px;font-weight:700;">{ex_nid}</code>
                        <span style="font-size:10px;color:{ex_color};font-weight:700;">{ex['name']}</span>
                        <span style="font-size:9px;color:#555;">[{ex['type']} · {ex['gate']}]</span>
                      </div>
                      <div style="font-size:9px;color:#777;line-height:1.7;">
                        <b style="color:#555;">Value:</b>
                        <span style="font-family:monospace;color:{ex_color};">{ex_val}</span>
                        &nbsp;·&nbsp;<b style="color:#555;">In FT:</b> {ex_ft_str}<br>
                        <b style="color:#555;">Current parents:</b> {ex_pnames}
                      </div>
                      <div style="font-size:9px;color:#4fc3f7;margin-top:5px;line-height:1.5;">
                        ℹ This is the same failure — same node, new location.<br>
                        Click below to place it under your selected parent(s) as well.<br>
                        It will appear in <b style="color:#4fc3f7;">blue</b> in the tree (duplicate marker).
                      </div>
                    </div>""", unsafe_allow_html=True)

                    # Single clear action
                    if st.button(f"◈ Place under selected parent(s)",
                                 key=f"dup_here_{ex['id']}",
                                 use_container_width=True,
                                 type="primary"):
                        cur_sel = sel_pids if sel_pids else []
                        if not cur_sel:
                            st.error("Select at least one parent above first")
                        else:
                            updated = []
                            for n in nodes:
                                if n["id"] == ex["id"]:
                                    n = dict(n)
                                    ep = list(n.get("parentIds") or [])
                                    ep += [p for p in cur_sel if p not in ep]
                                    n["parentIds"] = ep
                                updated.append(n)
                            st.session_state.tree_state["focus_id"] = ex["id"]
                            st.session_state.nodes_since_calc += 1
                            set_nodes(updated)
                            st.success(f"✓ '{ex['name']}' now also lives under the new parent(s). Shown in blue in the tree.")
                            st.rerun()

            # Show name matches first (informational), then ID matches
            if name_matches:
                render_match_card(name_matches, "name")
            if id_matches:
                render_match_card(id_matches, "id")

            # ── ID validation banner ──────────────────────────────────
            if not cid_clean:
                st.markdown(
                    "<div style='font-size:9px;color:#e94560;margin:2px 0 4px 0;'>★ Node ID is required</div>",
                    unsafe_allow_html=True)
            elif len(cid_clean) >= 2 and not id_matches:
                st.markdown(f"""
                <div style="background:#0a1a0a;border:1.5px solid #4caf7d;border-radius:6px;
                            padding:5px 10px;margin:2px 0 4px 0;">
                  <span style="font-size:9px;color:#4caf7d;font-weight:700;">
                    ✓ {cid_clean} is available
                  </span>
                </div>""", unsafe_allow_html=True)

            parent_opts = {f"[{n['type']}] {n.get('nodeId',n['id'])} — {n['name']}": n["id"]
                           for n in nodes if n["type"] in VALID_PARENT_TYPES}
            sel_labels  = st.multiselect("Parent Node(s)", list(parent_opts.keys()), key="add_par")
            sel_pids    = [parent_opts[l] for l in sel_labels]
            node_type   = st.selectbox("Type", VALID_CHILD_TYPES, key="add_type",
                                       help="GROUP = Combined Faults oval (placed between FF and IF layers)")
            gate        = st.radio("Gate", ["OR","AND"], horizontal=True, key="add_gate")

            use_fixed  = st.checkbox("📌 Pin to fixed value", key="add_use_fixed",
                                     help="Overrides calculated value. Subtracts from parent budget, gives remainder to siblings.")
            fixed_val_input = None
            if use_fixed:
                fixed_val_input = st.text_input("Fixed Value", placeholder="e.g. 1.67e-9 or 0",
                                                key="add_fixed_val",
                                                help="Node always carries this exact failure rate.")

            # ── ADD NODE — always present, never blocked ──────────────
            add_btn = st.button("✅ ADD NODE", use_container_width=True, type="primary",
                                disabled=not cid_clean)

            if add_btn:
                if not cid_clean:
                    st.error("★ Node ID is required — please enter a reference ID (e.g. IF-196)")
                elif not node_name.strip():
                    st.error("Enter a node name")
                elif not sel_pids:
                    st.error("Select at least one parent")
                else:
                    fv = None
                    if use_fixed and fixed_val_input:
                        try:    fv = float(fixed_val_input)
                        except: st.error("Fixed value must be a number e.g. 1.67e-9"); fv = None
                    # Build the display name — optionally include FT label
                    display_name = node_name.strip()
                    if ft_clean:
                        display_name = f"[{ft_clean}] {display_name}"
                    nid = str(uuid.uuid4())[:7]
                    new_node = {
                        "id":             nid,
                        "nodeId":         cid_clean,
                        "ftLabel":        ft_clean or "",
                        "name":           display_name,
                        "type":           node_type,
                        "gate":           gate,
                        "fixedValue":     fv,
                        "targetValue":    None,
                        "calculatedValue": fv,
                        "parentIds":      sel_pids,
                    }
                    st.session_state.tree_state["focus_id"] = sel_pids[0] if sel_pids else nid
                    st.session_state.nodes_since_calc += 1
                    pnn = st.session_state.get("pending_node_names", [])
                    pnn.append(f"{cid_clean} {display_name[:30]}")
                    st.session_state.pending_node_names = pnn[-20:]  # keep last 20
                    set_nodes(nodes + [new_node])
                    st.rerun()

            st.markdown("---")
            # Delete node
            del_opts = {f"[{n['type']}] {n.get('nodeId',n['id'])} — {n['name']}": n["id"]
                        for n in nodes if n["type"] != "HAZARD"}
            if del_opts:
                dl = st.selectbox("Delete Node", ["— select —"] + list(del_opts.keys()), key="del_sel")
                if dl != "— select —":
                    del_id = del_opts[dl]
                    is_shared_child = any(
                        del_id in (n.get("parentIds") or []) and len(n.get("parentIds") or []) > 1
                        for n in nodes
                    )
                    if is_shared_child:
                        st.markdown(
                            "<div style='font-size:9px;color:#f5c518;background:#1a1200;"
                            "border:1px solid #f5c51844;border-radius:5px;padding:5px 8px;margin:4px 0;'>"
                            "⚠ Shared children will keep their other parents.</div>",
                            unsafe_allow_html=True)
                    if st.button("🗑 DELETE NODE", use_container_width=True):
                        temp_nodes = [dict(n) for n in nodes if n["id"] != del_id]
                        for n in temp_nodes:
                            if del_id in (n.get("parentIds") or []):
                                n["parentIds"] = [p for p in n["parentIds"] if p != del_id]
                        changed = True
                        while changed:
                            changed = False
                            orphan_ids = {n["id"] for n in temp_nodes
                                          if n["type"] != "HAZARD" and not n.get("parentIds")}
                            if orphan_ids:
                                temp_nodes = [n for n in temp_nodes if n["id"] not in orphan_ids]
                                for n in temp_nodes:
                                    before = len(n.get("parentIds") or [])
                                    n["parentIds"] = [p for p in (n.get("parentIds") or []) if p not in orphan_ids]
                                    if len(n.get("parentIds") or []) != before:
                                        changed = True
                        st.session_state.nodes_since_calc += 1
                        set_nodes(temp_nodes)
                        st.rerun()

            st.markdown("---")
            if st.button("🗑 CLEAR ALL NODES", use_container_width=True):
                set_nodes([])
                st.session_state.selected_id = None
                st.session_state.nodes_since_calc = 0
                st.session_state.pending_node_names = []
                st.session_state.nodes_hash = ""
                st.rerun(scope="app")

    with tab_edit:
        nodes = st.session_state.nodes  # re-read — may have changed
        by_id = {n["id"]: n for n in nodes}
        if not nodes:
            st.markdown("<div style='color:#555;font-size:11px;'>No nodes yet.</div>", unsafe_allow_html=True)
        else:
            edit_opts  = {f"[{n['type']}] {n.get('nodeId',n['id'])} — {n['name']}": n["id"] for n in nodes}
            edit_label = st.selectbox("Select node to edit", ["— select —"] + list(edit_opts.keys()), key="edit_sel")

            if edit_label != "— select —":
                eid = edit_opts[edit_label]
                en  = next((n for n in nodes if n["id"] == eid), None)

                # ── Detect node switch — clear stale widget state ──────
                prev_eid = st.session_state.get("_edit_prev_eid")
                if prev_eid != eid:
                    # Node changed — purge old widget values so fields
                    # show the newly selected node's data, not the old one
                    for k in ["en_name","en_nid_prefix","en_nid_num","en_ft","en_gate","en_type","en_par",
                               "en_tgt","en_use_fix","en_fv"]:
                        st.session_state.pop(k, None)
                    st.session_state["_edit_prev_eid"] = eid

                if en:
                    color     = LEVEL_COLORS.get(en["type"], "#888")
                    is_shared = len(en.get("parentIds") or []) > 1
                    ex_pnames = " · ".join(
                        by_id[p]["name"] for p in (en.get("parentIds") or []) if p in by_id
                    ) or "—"

                    st.markdown(f"""
                    <div style="background:#141414;border:2px solid {color};border-radius:8px;
                                padding:8px 12px;margin-bottom:8px;">
                      <div style="font-size:8px;color:#888;letter-spacing:2px;margin-bottom:2px;">EDITING</div>
                      <div style="font-weight:700;color:{color};font-size:13px;">{esc(en['name'])}</div>
                      <div style="display:flex;align-items:center;gap:10px;margin-top:3px;">
                        <div style="font-size:9px;color:#666;">
                          {en['type']} · {en['gate']} · {en.get('nodeId', en['id'])}
                          {'&nbsp;<span style="background:#f5c518;color:#111;font-size:7px;padding:1px 4px;border-radius:3px;font-weight:700;">SHARED</span>' if is_shared else ''}
                        </div>
                        <div style="font-size:11px;color:{color};font-family:monospace;font-weight:700;margin-left:auto;">
                          {fmt(en.get('calculatedValue'))}{'📌' if en.get('fixedValue') is not None else ''}
                        </div>
                      </div>
                      <div style="font-size:9px;color:#555;margin-top:2px;">Parents: {ex_pnames}</div>
                    </div>""", unsafe_allow_html=True)

                    # ── Edit fields ───────────────────────────────────────
                    new_name = st.text_input("Name", value=en["name"], key="en_name")

                    # Node ID split: prefix + number + FT label
                    cur_nid = en.get("nodeId", en["id"])
                    import re as _re
                    nid_match  = _re.match(r'^([A-Za-z]+)-(.+)$', cur_nid)
                    cur_prefix = nid_match.group(1).upper() if nid_match else "IF"
                    cur_num    = nid_match.group(2) if nid_match else cur_nid

                    st.markdown("<div style='font-size:9px;color:#aaa;margin:4px 0 2px;'>Node ID  &nbsp;·&nbsp; FT Label</div>",
                                unsafe_allow_html=True)
                    ec1, ec2, ec3 = st.columns([1, 2, 1])
                    with ec1:
                        valid_prefixes = ["IF","FF","SF","GROUP","HAZ","OTHER"]
                        prefix_idx = valid_prefixes.index(cur_prefix) if cur_prefix in valid_prefixes else 0
                        edit_prefix = st.selectbox("Prefix", valid_prefixes,
                                                   index=prefix_idx, key="en_nid_prefix",
                                                   label_visibility="collapsed")
                    with ec2:
                        edit_num = st.text_input("Number", value=cur_num,
                                                 key="en_nid_num", label_visibility="collapsed",
                                                 placeholder="196, 23a …")
                    with ec3:
                        edit_ft = st.text_input("FT", value=en.get("ftLabel",""),
                                                key="en_ft", label_visibility="collapsed",
                                                placeholder="FT-46")
                    new_node_id  = f"{edit_prefix}-{edit_num.strip()}" if edit_num.strip() else cur_nid
                    new_ft_label = edit_ft.strip()

                    new_gate = st.radio("Gate", ["OR","AND"],
                                        index=0 if en["gate"] == "OR" else 1,
                                        horizontal=True, key="en_gate")

                    new_type = en["type"]
                    new_pids = list(en.get("parentIds") or [])
                    new_tgt  = None

                    if en["type"] != "HAZARD":
                        ti       = VALID_CHILD_TYPES.index(en["type"]) if en["type"] in VALID_CHILD_TYPES else 0
                        new_type = st.selectbox("Type", VALID_CHILD_TYPES, index=ti, key="en_type")
                        avail_p  = {f"[{n['type']}] {n.get('nodeId',n['id'])} — {n['name']}": n["id"]
                                    for n in nodes if n["type"] in VALID_PARENT_TYPES and n["id"] != eid}
                        cur_pl   = [lbl for lbl, pid in avail_p.items()
                                    if pid in (en.get("parentIds") or [])]
                        new_pl   = st.multiselect("Parents", list(avail_p.keys()),
                                                   default=cur_pl, key="en_par",
                                                   help="Add/remove parents — links this node as shared.")
                        new_pids = [avail_p[l] for l in new_pl]
                    else:
                        new_tgt = st.text_input("Target Rate",
                                                value=str(en.get("targetValue", "")),
                                                key="en_tgt")

                    # ── Fixed value pin ──────────────────────────────────
                    cur_fv     = en.get("fixedValue")
                    en_use_fix = st.checkbox("📌 Pin to fixed value",
                                             value=(cur_fv is not None),
                                             key="en_use_fix",
                                             help="Pin this node's rate. Siblings get the remainder.")
                    en_fv_input = None
                    if en_use_fix:
                        en_fv_input = st.text_input(
                            "Fixed Value",
                            value=str(cur_fv) if cur_fv is not None else "",
                            placeholder="e.g. 1.67e-9 or 0",
                            key="en_fv",
                            help="e.g. 0 for negligible, 1e-12 for residual")
                    if cur_fv is not None and not en_use_fix:
                        st.markdown(
                            f"<div style='font-size:9px;color:#f5c518;margin-top:2px;'>"
                            f"📌 Currently pinned to <b>{fmt(cur_fv)}</b> — uncheck removes pin</div>",
                            unsafe_allow_html=True)

                    # ── Buttons ──────────────────────────────────────────
                    c1, c2 = st.columns(2)
                    with c1:
                        if st.button("💾 APPLY", use_container_width=True, type="primary"):
                            new_fv = None
                            if en_use_fix and en_fv_input is not None:
                                try:    new_fv = float(en_fv_input)
                                except: st.error("Fixed value must be a number e.g. 1.67e-9")

                            upd = []
                            for n in nodes:
                                if n["id"] == eid:
                                    n = dict(n)
                                    n["name"]       = new_name.strip() or n["name"]
                                    n["gate"]       = new_gate
                                    n["nodeId"]     = new_node_id
                                    n["ftLabel"]    = new_ft_label
                                    n["fixedValue"] = new_fv
                                    if n["type"] != "HAZARD":
                                        n["type"]      = new_type
                                        n["parentIds"] = new_pids if new_pids else n.get("parentIds", [])
                                    else:
                                        if new_tgt:
                                            try:
                                                tv = float(new_tgt)
                                                n["targetValue"]    = tv
                                                n["calculatedValue"] = tv
                                            except: pass
                                upd.append(n)

                            # Clear stale widget state and force tree rebuild
                            for k in ["en_name","en_nid_prefix","en_nid_num","en_ft","en_gate",
                                      "en_type","en_par","en_tgt","en_use_fix","en_fv",
                                      "_edit_prev_eid"]:
                                st.session_state.pop(k, None)

                            st.session_state.nodes_hash       = ""   # force tree redraw
                            st.session_state.nodes_since_calc += 1
                            st.session_state.tree_state["focus_id"] = eid
                            set_nodes(upd)
                            st.rerun(scope="app")   # full page rerun so tree redraws

                    with c2:
                        if en["type"] != "HAZARD":
                            if st.button("🗑 DELETE", use_container_width=True):
                                temp_nodes = [dict(n) for n in nodes if n["id"] != eid]
                                for n in temp_nodes:
                                    if eid in (n.get("parentIds") or []):
                                        n["parentIds"] = [p for p in n["parentIds"] if p != eid]
                                # Cascade delete orphans
                                changed = True
                                while changed:
                                    changed = False
                                    orphan_ids = {n["id"] for n in temp_nodes
                                                  if n["type"] != "HAZARD"
                                                  and not n.get("parentIds")}
                                    if orphan_ids:
                                        temp_nodes = [n for n in temp_nodes if n["id"] not in orphan_ids]
                                        for n in temp_nodes:
                                            before = len(n.get("parentIds") or [])
                                            n["parentIds"] = [p for p in (n.get("parentIds") or [])
                                                              if p not in orphan_ids]
                                            if len(n.get("parentIds") or []) != before:
                                                changed = True

                                for k in ["_edit_prev_eid","edit_sel"]:
                                    st.session_state.pop(k, None)
                                st.session_state.nodes_hash       = ""
                                st.session_state.nodes_since_calc += 1
                                st.session_state.tree_state["focus_id"] = None
                                set_nodes(temp_nodes)
                                st.rerun(scope="app")

    # ── SHARED tab ────────────────────────────────────────────────────────
    with tab_shared:
        nodes  = st.session_state.nodes
        by_id  = {n["id"]: n for n in nodes}

        if not nodes:
            st.markdown("<div style='color:#555;font-size:11px;'>No nodes yet.</div>",
                        unsafe_allow_html=True)
        else:
            from collections import defaultdict as _dd2

            # ── Build unified shared registry ─────────────────────────
            # Two kinds of "shared" — treated uniformly:
            # A) Duplicate instances: same nodeId, multiple separate nodes
            # B) Link-Shared: one node with multiple parentIds
            # Both appear in the same dropdown.

            nid_map = _dd2(list)
            for n in nodes:
                nid = (n.get("nodeId") or "").strip()
                if nid:
                    nid_map[nid].append(n)

            # Group A: nodeId groups with 2+ separate nodes
            dup_groups = {nid: grp for nid, grp in nid_map.items() if len(grp) > 1}
            # Group B: single nodes with 2+ parents
            link_shared = [n for n in nodes if len(n.get("parentIds") or []) > 1]

            # Build unified list for dropdown
            # Format: "◈ IF-085 (2 instances)" or "⊗ SF-06 (link-shared, 2 parents)"
            registry = {}  # label → {"type": "dup"|"link", "key": nodeId|nodeId, "data": grp|node}
            for nid_lbl, grp in sorted(dup_groups.items()):
                typ = grp[0].get("type","?")
                label = f"◈ {nid_lbl}  ·  {len(grp)} instances  ·  {typ}"
                registry[label] = {"kind": "dup", "nid": nid_lbl, "grp": grp}
            for n in sorted(link_shared, key=lambda x: x.get("nodeId","") or x["id"]):
                nid_lbl = n.get("nodeId", n["id"])
                np = len(n.get("parentIds") or [])
                # Skip if this node is already in a dup group (avoid double listing)
                if nid_lbl in dup_groups:
                    continue
                label = f"⊗ {nid_lbl}  ·  link-shared  ·  {np} parents  ·  {n['type']}"
                registry[label] = {"kind": "link", "nid": nid_lbl, "node": n}

            # Summary
            tot = len(registry)
            c1, c2 = st.columns(2)
            with c1:
                col = "#f5c518" if dup_groups else "#333"
                st.markdown(f'<div style="background:#141414;border:1.5px solid {col}44;border-radius:6px;padding:7px;text-align:center;">'
                            f'<div style="font-size:8px;color:#555;letter-spacing:2px;">DUPLICATE INSTANCES</div>'
                            f'<div style="font-size:22px;font-weight:700;color:{col};">{len(dup_groups)}</div>'
                            f'<div style="font-size:8px;color:#555;">same nodeId, separate nodes</div></div>',
                            unsafe_allow_html=True)
            with c2:
                col2 = "#4fc3f7" if link_shared else "#333"
                st.markdown(f'<div style="background:#141414;border:1.5px solid {col2}44;border-radius:6px;padding:7px;text-align:center;">'
                            f'<div style="font-size:8px;color:#555;letter-spacing:2px;">LINK-SHARED</div>'
                            f'<div style="font-size:22px;font-weight:700;color:{col2};">{len(link_shared)}</div>'
                            f'<div style="font-size:8px;color:#555;">one node, multiple parents</div></div>',
                            unsafe_allow_html=True)

            if not registry:
                st.markdown('<div style="text-align:center;color:#333;font-size:11px;margin-top:24px;">✓ No shared or duplicate nodes</div>',
                            unsafe_allow_html=True)
            else:
                st.markdown("---")
                # ── Dropdown selector ─────────────────────────────────
                sel_label = st.selectbox(
                    "Select a shared node to inspect",
                    ["— select —"] + list(registry.keys()),
                    key="sh_sel",
                    label_visibility="collapsed",
                    format_func=lambda x: x
                )

                if sel_label != "— select —":
                    entry = registry[sel_label]
                    kind  = entry["kind"]

                    # ── Helper: get full hazard path for a node ───────
                    def node_path(start_id, depth=0):
                        """Returns list of nodes from HAZARD down to start_id."""
                        if depth > 10: return []
                        nd = by_id.get(start_id)
                        if not nd: return []
                        pids = nd.get("parentIds") or []
                        if not pids or nd["type"] == "HAZARD":
                            return [nd]
                        result = node_path(pids[0], depth+1)
                        result.append(nd)
                        return result

                    def path_badge(nid):
                        """Colored breadcrumb: HAZARD › SF-10 › FF-01"""
                        parts = node_path(nid)
                        badges = []
                        for p in parts:
                            c   = LEVEL_COLORS.get(p["type"], "#888")
                            lbl = p.get("nodeId","") or p["type"]
                            # Use type label if nodeId looks like raw UUID
                            if lbl and len(lbl) <= 8 and lbl.isalnum() and lbl.islower():
                                lbl = p["type"]
                            badges.append(f'<span style="color:{c};font-weight:700;font-size:8px;">{esc(lbl)}</span>')
                        return ' <span style="color:#444;font-size:9px;">›</span> '.join(badges)

                    # ═══════════════════════════════════════════════════
                    # CASE A: Duplicate instance group
                    # ═══════════════════════════════════════════════════
                    if kind == "dup":
                        grp     = entry["grp"]
                        nid_lbl = entry["nid"]
                        gc      = LEVEL_COLORS.get(grp[0]["type"], "#888")

                        # Check if any instance is pinned
                        pinned_vals = [n.get("fixedValue") for n in grp if n.get("fixedValue") is not None]
                        pin_info = ""
                        if pinned_vals:
                            pin_info = (f'<div style="background:#1a0000;border:1px solid #e9456044;'
                                        f'border-radius:5px;padding:5px 8px;margin-bottom:6px;font-size:8px;color:#e94560;">'
                                        f'📌 Pinned to <b>{fmt(pinned_vals[0])}</b> — '
                                        f'all {len(grp)} instances share this value on CALCULATE</div>')
                            st.markdown(pin_info, unsafe_allow_html=True)

                        # Each instance
                        for i, inst in enumerate(grp):
                            ic      = LEVEL_COLORS.get(inst["type"], "#888")
                            iv      = fmt(inst.get("calculatedValue"))
                            ift     = inst.get("ftLabel","")
                            ip      = [by_id[p] for p in (inst.get("parentIds") or []) if p in by_id]
                            ich     = [c for c in nodes if inst["id"] in (c.get("parentIds") or [])]
                            is_pin  = inst.get("fixedValue") is not None
                            pin_tag = f' <span style="color:#e94560;font-size:7px;">📌{fmt(inst.get("fixedValue"))}</span>' if is_pin else ""
                            ft_tag  = f'<code style="background:#1e1530;color:#7e57c2;font-size:7px;padding:1px 4px;border-radius:3px;">{esc(ift)}</code> ' if ift else ""
                            pbadge  = path_badge(inst["id"])
                            pname   = esc(ip[0]["name"]) if ip else "<i style='color:#444'>no parent</i>"
                            cnames  = esc(", ".join(c["name"] for c in ich)) if ich else "<i style='color:#333'>leaf</i>"

                            st.markdown(
                                f'<div style="background:#111;border:1.5px solid #f5c51855;'
                                f'border-left:3px solid {ic};border-radius:0 7px 7px 0;'
                                f'padding:8px 10px;margin-bottom:4px;">'
                                f'<div style="margin-bottom:3px;">{ft_tag}'
                                f'<span style="font-weight:700;font-size:11px;color:#ddd;">{esc(inst["name"])}</span>'
                                f'&nbsp;<span style="color:{ic};font-family:monospace;font-size:11px;font-weight:700;">{iv}</span>{pin_tag}</div>'
                                f'<div style="font-size:8px;color:#666;margin-bottom:2px;">{esc(inst["type"])} · {esc(inst["gate"])}</div>'
                                f'<div style="font-size:8px;color:#555;margin-bottom:2px;">📍 {pbadge}</div>'
                                f'<div style="font-size:8px;color:#666;">↑ {pname}</div>'
                                f'<div style="font-size:8px;color:#555;">↓ {cnames}</div>'
                                f'</div>',
                                unsafe_allow_html=True
                            )

                            # Per-instance actions
                            ba, bb, bc = st.columns(3)
                            with ba:
                                with st.popover("✏️ Edit", use_container_width=True):
                                    e_name = st.text_input("Name", value=inst["name"], key=f"se_n_{inst['id']}")
                                    e_gate = st.radio("Gate", ["OR","AND"],
                                                      index=0 if inst["gate"]=="OR" else 1,
                                                      key=f"se_g_{inst['id']}", horizontal=True)
                                    e_fv_on = st.checkbox("📌 Pin value",
                                                          value=is_pin, key=f"se_fon_{inst['id']}")
                                    e_fv = None
                                    if e_fv_on:
                                        e_fv = st.text_input("Fixed value",
                                                             value=str(inst.get("fixedValue","")) if is_pin else "",
                                                             key=f"se_fv_{inst['id']}", placeholder="e.g. 1e-12")
                                    st.markdown('<div style="font-size:8px;color:#f5c518;">Pinning any instance syncs to all others on CALCULATE.</div>', unsafe_allow_html=True)
                                    if st.button("💾 Apply", key=f"se_apply_{inst['id']}", type="primary", use_container_width=True):
                                        fv_p = None
                                        if e_fv_on and e_fv:
                                            try: fv_p = float(e_fv)
                                            except: pass
                                        upd = []
                                        for nd in nodes:
                                            nd = dict(nd)
                                            if nd["id"] == inst["id"]:
                                                nd["name"] = e_name.strip() or nd["name"]
                                                nd["gate"] = e_gate
                                                nd["fixedValue"] = fv_p
                                            upd.append(nd)
                                        st.session_state.nodes_hash = ""
                                        st.session_state.nodes_since_calc += 1
                                        set_nodes(upd); st.rerun(scope="app")
                            with bb:
                                # Add new copy under a different parent
                                with st.popover("➕ Add copy", use_container_width=True):
                                    st.markdown(f'<div style="font-size:9px;color:#aaa;">Add <b>{esc(nid_lbl)}</b> under new parent</div>', unsafe_allow_html=True)
                                    avp = {f"[{n['type']}] {n.get('nodeId',n['id'])} — {n['name']}": n["id"]
                                           for n in nodes if n["type"] in VALID_PARENT_TYPES}
                                    new_p_lbl = st.selectbox("Parent", ["— select —"] + list(avp.keys()),
                                                             key=f"se_np_{inst['id']}")
                                    if new_p_lbl != "— select —":
                                        new_pid = avp[new_p_lbl]
                                        already = any(
                                            n.get("nodeId","") == nid_lbl and new_pid in (n.get("parentIds") or [])
                                            for n in nodes
                                        )
                                        if already:
                                            st.warning(f"{nid_lbl} already under that parent.")
                                        elif st.button("➕ Create copy", key=f"se_cp_{inst['id']}", type="primary", use_container_width=True):
                                            new_n = {
                                                "id": str(uuid.uuid4())[:7],
                                                "nodeId": nid_lbl,
                                                "ftLabel": inst.get("ftLabel",""),
                                                "name": inst["name"],
                                                "type": inst["type"],
                                                "gate": inst["gate"],
                                                "fixedValue": inst.get("fixedValue"),
                                                "targetValue": None,
                                                "calculatedValue": inst.get("fixedValue"),
                                                "parentIds": [new_pid],
                                            }
                                            st.session_state.nodes_hash = ""
                                            st.session_state.nodes_since_calc += 1
                                            st.session_state.tree_state["focus_id"] = new_pid
                                            set_nodes(nodes + [new_n]); st.rerun(scope="app")
                            with bc:
                                # Break = remove this instance (only if not last)
                                if len(grp) > 1:
                                    if st.button("✂️ Break", key=f"se_brk_{inst['id']}",
                                                 use_container_width=True,
                                                 help="Remove this instance from the tree"):
                                        upd = [dict(nd) for nd in nodes if nd["id"] != inst["id"]]
                                        for nd in upd:
                                            if inst["id"] in (nd.get("parentIds") or []):
                                                nd["parentIds"] = [p for p in nd["parentIds"] if p != inst["id"]]
                                        st.session_state.nodes_hash = ""
                                        st.session_state.nodes_since_calc += 1
                                        set_nodes(upd); st.rerun(scope="app")
                                else:
                                    st.markdown('<div style="font-size:8px;color:#333;padding:4px;">last instance</div>', unsafe_allow_html=True)

                    # ═══════════════════════════════════════════════════
                    # CASE B: Link-Shared node (one node, multiple parents)
                    # ═══════════════════════════════════════════════════
                    elif kind == "link":
                        n       = entry["node"]
                        nid_lbl = entry["nid"]
                        nc      = LEVEL_COLORS.get(n["type"], "#888")
                        iv      = fmt(n.get("calculatedValue"))
                        pids    = n.get("parentIds") or []
                        ich     = [c for c in nodes if n["id"] in (c.get("parentIds") or [])]
                        is_pin  = n.get("fixedValue") is not None

                        st.markdown(
                            f'<div style="background:#0a1a2e;border:1.5px solid #4fc3f755;'
                            f'border-radius:7px;padding:8px 11px;margin-bottom:8px;">'
                            f'<div style="font-weight:700;font-size:12px;color:#ddd;margin-bottom:3px;">{esc(n["name"])}</div>'
                            f'<div style="font-size:8px;color:#666;">{esc(n["type"])} · {esc(n["gate"])} &nbsp;·&nbsp; '
                            f'Value: <span style="color:{nc};font-family:monospace;">{iv}</span>'
                            f'{"&nbsp;📌" if is_pin else ""}</div>'
                            f'<div style="font-size:8px;color:#4fc3f7;margin-top:3px;">{len(pids)} parents &nbsp;·&nbsp; MAX rule applies on CALCULATE</div>'
                            f'</div>',
                            unsafe_allow_html=True
                        )

                        # Show each parent connection with break option
                        st.markdown('<div style="font-size:9px;color:#4fc3f7;font-weight:700;letter-spacing:1px;margin-bottom:5px;">PARENT CONNECTIONS</div>', unsafe_allow_html=True)
                        for pid in pids:
                            pn = by_id.get(pid)
                            if not pn: continue
                            pc     = LEVEL_COLORS.get(pn["type"], "#888")
                            pbadge = path_badge(pid)
                            pv     = fmt(pn.get("calculatedValue"))
                            pca, pcb = st.columns([3,1])
                            with pca:
                                st.markdown(
                                    f'<div style="background:#111;border:1px solid #4fc3f722;'
                                    f'border-left:3px solid {pc};border-radius:0 5px 5px 0;'
                                    f'padding:5px 8px;margin-bottom:3px;">'
                                    f'<div style="font-size:9px;color:#ddd;font-weight:700;">{esc(pn["name"])}</div>'
                                    f'<div style="font-size:8px;color:#666;">{esc(pn["type"])} · {pv}</div>'
                                    f'<div style="font-size:8px;color:#555;">📍 {pbadge}</div>'
                                    f'</div>',
                                    unsafe_allow_html=True
                                )
                            with pcb:
                                if len(pids) > 1:
                                    if st.button("✂️", key=f"lnk_brk_{n['id']}_{pid}",
                                                 help=f"Break connection to {pn['name']}",
                                                 use_container_width=True):
                                        upd = []
                                        for nd in nodes:
                                            nd = dict(nd)
                                            if nd["id"] == n["id"]:
                                                nd["parentIds"] = [p for p in nd["parentIds"] if p != pid]
                                            upd.append(nd)
                                        st.session_state.nodes_hash = ""
                                        st.session_state.nodes_since_calc += 1
                                        set_nodes(upd); st.rerun(scope="app")
                                else:
                                    st.markdown('<div style="font-size:7px;color:#333;text-align:center;">last</div>', unsafe_allow_html=True)

                        # Add new parent connection
                        st.markdown('<div style="font-size:9px;color:#4fc3f7;font-weight:700;letter-spacing:1px;margin:8px 0 5px;">ADD PARENT CONNECTION</div>', unsafe_allow_html=True)
                        avp2 = {f"[{nd['type']}] {nd.get('nodeId',nd['id'])} — {nd['name']}": nd["id"]
                                for nd in nodes
                                if nd["type"] in VALID_PARENT_TYPES and nd["id"] not in pids and nd["id"] != n["id"]}
                        new_p2 = st.selectbox("New parent", ["— select —"] + list(avp2.keys()),
                                              key=f"lnk_newp_{n['id']}")
                        if new_p2 != "— select —":
                            if st.button("🔗 Add connection", key=f"lnk_add_{n['id']}",
                                         type="primary", use_container_width=True):
                                upd = []
                                for nd in nodes:
                                    nd = dict(nd)
                                    if nd["id"] == n["id"]:
                                        nd["parentIds"] = list(nd.get("parentIds") or []) + [avp2[new_p2]]
                                    upd.append(nd)
                                st.session_state.nodes_hash = ""
                                st.session_state.nodes_since_calc += 1
                                set_nodes(upd); st.rerun(scope="app")

                        # Split into duplicate instances
                        st.markdown("---")
                        st.markdown('<div style="font-size:9px;color:#f5c518;font-weight:700;margin-bottom:4px;">CONVERT TO DUPLICATE INSTANCES</div>', unsafe_allow_html=True)
                        st.markdown(
                            f'<div style="font-size:8px;color:#666;">Currently one node with {len(pids)} parents. '
                            f'Split into {len(pids)} separate nodes (one per parent), each with the same nodeId.</div>',
                            unsafe_allow_html=True
                        )
                        if st.button(f"✂️ Split into {len(pids)} separate instances",
                                     key=f"lnk_split_{n['id']}",
                                     use_container_width=True):
                            # Create one new node per parent (except keep original for first parent)
                            new_nodes_list = []
                            for i2, pid2 in enumerate(pids):
                                if i2 == 0:
                                    # Update original — keep only first parent
                                    pass
                                else:
                                    new_n2 = {
                                        "id": str(uuid.uuid4())[:7],
                                        "nodeId": nid_lbl,
                                        "ftLabel": n.get("ftLabel",""),
                                        "name": n["name"],
                                        "type": n["type"],
                                        "gate": n["gate"],
                                        "fixedValue": n.get("fixedValue"),
                                        "targetValue": None,
                                        "calculatedValue": n.get("fixedValue"),
                                        "parentIds": [pid2],
                                    }
                                    new_nodes_list.append(new_n2)
                            upd = []
                            for nd in nodes:
                                nd = dict(nd)
                                if nd["id"] == n["id"]:
                                    nd["parentIds"] = [pids[0]]  # keep only first parent
                                upd.append(nd)
                            upd.extend(new_nodes_list)
                            st.session_state.nodes_hash = ""
                            st.session_state.nodes_since_calc += 1
                            st.session_state.tree_state["focus_id"] = n["id"]
                            set_nodes(upd)
                            st.success(f"Split into {len(pids)} instances. Press CALCULATE.")
                            st.rerun(scope="app")


with st.sidebar:
    render_sidebar()

# ── Action bar ────────────────────────────────────────────────────────────
nsc = st.session_state.nodes_since_calc

# Warning banner when nodes added without calculating
if nsc > 0:
    warn_color  = "#ff4d4d" if nsc >= 10 else "#f5c518"
    warn_bg     = "#1a0000" if nsc >= 10 else "#1a1200"
    warn_icon   = "🔴" if nsc >= 10 else "🟡"
    pending     = st.session_state.get("pending_node_names", [])
    warn_msg    = (f"{warn_icon} **{nsc} node{'s' if nsc!=1 else ''} added without calculating** — "
                   f"values shown are stale. Press **▶ CALCULATE** to update.")
    if pending:
        names_preview = ", ".join(f"`{n}`" for n in pending[-5:])
        if len(pending) > 5:
            names_preview = f"…{len(pending)-5} more, " + names_preview
        warn_msg += f"  \nPending: {names_preview}"
    if nsc >= 10:
        warn_msg += f"  \n⚠ {nsc} nodes without calculating — values are likely very stale."
    st.markdown(
        f'<div style="background:{warn_bg};border:2px solid {warn_color};border-radius:8px;'
        f'padding:9px 14px;margin-bottom:8px;font-size:11px;color:{warn_color};">'
        f'{warn_msg}</div>',
        unsafe_allow_html=True)

a1,a2,a3,a4,a5 = st.columns([1,1,1,1,2])
with a1:
    calc_label = f"▶ CALCULATE{f' ({nsc}✱)' if nsc>0 else ''}"
    if st.button(calc_label, type="primary", use_container_width=True,
                 help="Run top-down reverse distribution across the full tree"):
        if nodes:
            new_nodes = recalculate(nodes)
            snap = f"snapshot_{now_str()}.json"
            save_current(new_nodes, filename=snap, status_label=f"Calculated + snap: {snap}")
            save_current(new_nodes)
            st.session_state.nodes = new_nodes
            st.session_state.nodes_since_calc = 0
            st.session_state.pending_node_names = []
            st.session_state.nodes_hash = ""
            st.rerun()
with a2:
    if st.button("💾 SAVE", use_container_width=True): save_current(); st.rerun()
with a3:
    if nodes:
        st.download_button("⬇ JSON", data=export_json(nodes),
                           file_name=f"fta_{now_str()}.json", mime="application/json",
                           use_container_width=True)
with a4:
    if nodes:
        xl = export_excel(nodes)
        if xl:
            st.download_button("⬇ EXCEL", data=xl, file_name=f"fta_{now_str()}.xlsx",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                               use_container_width=True)
with a5:
    if nodes:
        st.download_button("⬇ CYPHER (Neo4j)", data=export_cypher(nodes),
                           file_name=f"fta_{now_str()}.cypher", mime="text/plain",
                           use_container_width=True)

st.markdown("---")

# ── Refresh data after possible recalc ───────────────────────────────────
nodes    = st.session_state.nodes
hazards  = [n for n in nodes if n["type"] == "HAZARD"]
by_id    = {n["id"]: n for n in nodes}
by_level = {lvl:[n for n in nodes if n["type"]==lvl] for lvl in DISPLAY_ORDER}

# ── Tabs ──────────────────────────────────────────────────────────────────
tab_tree, tab_hier, tab_vals, tab_search = st.tabs(["🌳 TREE", "📋 HIERARCHY", "📊 VALUES", "🔍 SEARCH"])

# ── TAB 1: Tree ───────────────────────────────────────────────────────────
with tab_tree:
    if not nodes:
        st.markdown("<div style='text-align:center;color:#333;margin-top:60px;letter-spacing:2px;'>ADD A HAZARD TO START</div>", unsafe_allow_html=True)
    else:
        # Sticky hazard filter — default to first hazard, not full tree
        filter_opts = {"Full Tree (all hazards)": "ALL"} | {
            f"🎯 {h['name']}  ({fmt(h.get('targetValue'))})": h["id"] for h in hazards
        }
        opt_keys = list(filter_opts.keys())
        opt_vals = list(filter_opts.values())

        # Default to first hazard (index 1) on first load
        saved_filter = st.session_state.get("tree_filter", "ALL")
        default_idx  = opt_vals.index(saved_filter) if saved_filter in opt_vals else (1 if len(opt_vals) > 1 else 0)

        filter_label = st.selectbox("View", opt_keys, index=default_idx,
                                    key="tree_filter_sel", label_visibility="collapsed")
        filter_id = filter_opts[filter_label]
        fid = None if filter_id == "ALL" else filter_id

        # When user changes hazard in dropdown → focus on that hazard
        if filter_id != saved_filter:
            st.session_state.tree_filter = filter_id
            st.session_state.tree_state["focus_id"] = fid  # None = full tree, else hazard id

        ts = st.session_state.tree_state
        st.markdown(
            "<div style='font-size:9px;color:#333;margin-bottom:3px;'>"
            "⚡ Force ON/OFF · Scroll=zoom · Right-drag=pan · Left-drag=move · "
            "Click=inspect · Dbl-click=collapse"
            "</div>", unsafe_allow_html=True)

        # ── Hash-based cache: only rebuild iframe when node data changes ──
        import hashlib as _hl, json as _js
        _hash_data = _js.dumps([{
            "id": n["id"], "name": n["name"], "type": n["type"],
            "gate": n["gate"], "val": fmt(n.get("calculatedValue")),
            "parents": sorted(n.get("parentIds") or []),
            "fixed": str(n.get("fixedValue"))
        } for n in nodes], sort_keys=True) + (fid or "ALL")
        tree_key = _hl.md5(_hash_data.encode()).hexdigest()[:12]

        # Use saved positions from postMessage save-back if available
        saved_positions = st.session_state.get("_tree_positions", {})
        if saved_positions:
            ts = dict(ts)
            ts["positions"] = saved_positions

        if st.session_state.nodes_hash != tree_key:
            st.session_state.nodes_hash = tree_key
            tree_html = build_html_tree(nodes, filter_hazard_id=fid, tree_state=ts)
            st.session_state["_cached_tree_html"] = tree_html
        else:
            tree_html = st.session_state.get("_cached_tree_html") or \
                        build_html_tree(nodes, filter_hazard_id=fid, tree_state=ts)

        components.html(tree_html, height=780, scrolling=False)
        st.session_state.tree_state["focus_id"] = None

        # ── Position receiver (hidden) ────────────────────────────────
        # Catches postMessage from the tree iframe and saves positions
        # so they survive CALCULATE / file load rerenders
        pos_receiver = """
        <script>
        window.addEventListener("message", function(e){
            try{
                const d = typeof e.data === "string" ? JSON.parse(e.data) : e.data;
                if(d && d.type === "fta_pos"){
                    // Store in sessionStorage so Streamlit can read on next rerun
                    sessionStorage.setItem("fta_positions", JSON.stringify(d.data));
                }
            }catch(err){}
        });
        </script>
        """
        # Note: true round-trip requires a custom component; positions survive
        # within the browser session via uP (JS memory) which is sufficient
        # for normal use. Browser refresh loses positions — press ⊞ Reset to rebalance.

# ── TAB 2: Hierarchy ─────────────────────────────────────────────────────
with tab_hier:
    if not nodes:
        st.markdown("<div style='color:#333;text-align:center;'>No nodes yet</div>", unsafe_allow_html=True)
    else:
        h_opts = {"All Hazards": None} | {h["name"]: h["id"] for h in hazards}
        h_sel  = st.selectbox("Filter by Hazard", list(h_opts.keys()), key="hier_filter")
        rows   = build_hierarchy_rows(nodes, filter_hazard_id=h_opts[h_sel])
        for row in rows:
            node = row["node"]; depth = row["depth"]; is_ref = row.get("ref", False)
            color     = LEVEL_COLORS.get(node["type"], "#888")
            val       = fmt(node.get("calculatedValue"))
            node_id   = node.get("nodeId", "")
            ft_lbl    = node.get("ftLabel", "")
            indent    = depth * 24
            is_pinned = node.get("fixedValue") is not None
            is_shared = len(node.get("parentIds") or []) > 1

            ref_tag    = '<span style="background:#2a2a2a;color:#777;font-size:7px;padding:1px 4px;border-radius:4px;margin-left:4px;">REF</span>' if is_ref else ""
            shared_tag = '<span style="background:#f5c518;color:#111;font-size:7px;padding:1px 4px;border-radius:4px;margin-left:4px;font-weight:700;">SHARED</span>' if is_shared else ""
            pin_tag    = f'<span style="background:#e9456022;color:#e94560;font-size:7px;padding:1px 4px;border-radius:4px;margin-left:4px;border:1px solid #e9456044;">📌 {fmt(node.get("fixedValue"))}</span>' if is_pinned else ""
            gate_col   = "#4fc3f7" if node["gate"] == "OR" else "#ffb74d"
            gate_tag   = f'<span style="color:{gate_col};font-size:8px;margin-left:5px;font-weight:700;">[{node["gate"]}]</span>'
            nid_tag    = f'<code style="background:#1a1a2e;color:{color};font-size:8px;padding:1px 5px;border-radius:3px;margin-left:5px;">{node_id}</code>' if node_id else ""
            ft_tag     = f'<code style="background:#1a1a2e;color:#7e57c2;font-size:8px;padding:1px 5px;border-radius:3px;margin-left:4px;">{ft_lbl}</code>' if ft_lbl else ""
            val_color  = "#e94560" if is_pinned else color

            st.markdown(f"""
            <div style="display:flex;align-items:center;padding:5px 10px;margin-left:{indent}px;
                        margin-bottom:2px;background:#141414;
                        border-left:3px solid {color};border-radius:0 5px 5px 0;
                        {'border:1px solid #e9456033;' if is_pinned else ''}">
              <div style="flex:1;min-width:0;">
                <span style="color:#444;font-size:9px;">{'└─ ' if depth>0 else ''}</span>
                <span style="font-weight:{'700' if depth==0 else '500'};color:#ddd;font-size:11px;">{node['name']}</span>
                <span style="font-size:8px;color:#555;margin-left:5px;">{node['type']}</span>
                {nid_tag}{ft_tag}{gate_tag}{shared_tag}{pin_tag}{ref_tag}
              </div>
              <div style="font-weight:700;font-size:12px;color:{val_color};
                          font-family:monospace;flex-shrink:0;margin-left:12px;">{val}</div>
            </div>""", unsafe_allow_html=True)

# ── TAB 3: Values ─────────────────────────────────────────────────────────
with tab_vals:
    if not nodes:
        st.markdown("<div style='color:#333;text-align:center;'>No nodes yet</div>", unsafe_allow_html=True)
    else:
        # Filter by hazard
        hf_opts = {"All Hazards": None} | {h["name"]: h["id"] for h in hazards}
        hf_sel  = st.selectbox("Filter by Hazard", list(hf_opts.keys()), key="vals_filter")
        hf_id   = hf_opts[hf_sel]

        # If filtered, collect nodes reachable from that hazard
        if hf_id:
            visible = set(); q = [hf_id]
            while q:
                cur = q.pop()
                if cur in visible: continue
                visible.add(cur)
                for child in [n for n in nodes if cur in (n.get("parentIds") or [])]: q.append(child["id"])
            show = [n for n in nodes if n["id"] in visible]
        else:
            show = nodes

        show_by_level = {lvl: [n for n in show if n["type"] == lvl] for lvl in DISPLAY_ORDER}

        for level in DISPLAY_ORDER:
            lvl_nodes = show_by_level[level]
            if not lvl_nodes: continue
            color = LEVEL_COLORS[level]
            st.markdown(f"<div style='font-size:9px;letter-spacing:3px;color:{color};border-bottom:1px solid {color}33;padding-bottom:3px;margin:12px 0 5px;'>{level} — {len(lvl_nodes)} nodes</div>", unsafe_allow_html=True)
            for node in lvl_nodes:
                pnames    = " · ".join(by_id[p]["name"] for p in (node.get("parentIds") or []) if p in by_id) or "—"
                cnames    = " · ".join(n["name"] for n in nodes if node["id"] in (n.get("parentIds") or [])) or "—"
                is_shared = len(node.get("parentIds") or []) > 1
                is_pinned = node.get("fixedValue") is not None
                gc        = "#4fc3f7" if node["gate"] == "OR" else "#ffb74d"
                val_color = "#e94560" if is_pinned else color
                val_str   = fmt(node.get("calculatedValue"))
                val_display = f"{val_str} 📌" if is_pinned else val_str
                node_id   = node.get("nodeId", node["id"])
                ft_lbl    = node.get("ftLabel", "")
                tgt_str   = fmt(node.get("targetValue")) if node.get("targetValue") else ""

                badges = ""
                if is_shared: badges += '<span style="background:#f5c518;color:#111;font-size:7px;padding:1px 4px;border-radius:3px;font-weight:700;margin-right:3px;">SHARED</span>'
                if is_pinned: badges += f'<span style="background:#e9456022;color:#e94560;font-size:7px;padding:1px 4px;border-radius:3px;font-weight:700;border:1px solid #e9456044;">📌 FIXED={fmt(node.get("fixedValue"))}</span>'
                if ft_lbl:    badges += f'<span style="background:#1a1a2e;color:#7e57c2;font-size:7px;padding:1px 4px;border-radius:3px;font-weight:700;margin-left:3px;border:1px solid #7e57c244;">{ft_lbl}</span>'

                st.markdown(f"""
                <div style="background:#141414;border:1px solid {'#e9456044' if is_pinned else '#1e1e1e'};
                            border-radius:6px;padding:7px 12px;margin-bottom:3px;">
                  <div style="display:grid;grid-template-columns:1.8fr 0.7fr 0.7fr 0.7fr 1.2fr 2fr;gap:8px;align-items:start;">
                    <div>
                      <div style="font-weight:700;font-size:11px;color:#ddd;margin-bottom:2px;">{node['name']}</div>
                      <div style="font-size:8px;">{badges}</div>
                    </div>
                    <div>
                      <div style="font-size:7px;color:#444;letter-spacing:1px;margin-bottom:1px;">NODE ID</div>
                      <div style="font-size:10px;color:{color};font-weight:700;font-family:monospace;">{node_id}</div>
                    </div>
                    <div>
                      <div style="font-size:7px;color:#444;letter-spacing:1px;margin-bottom:1px;">TYPE</div>
                      <div style="font-size:10px;color:{color};font-weight:700;">{node['type']}</div>
                    </div>
                    <div>
                      <div style="font-size:7px;color:#444;letter-spacing:1px;margin-bottom:1px;">GATE</div>
                      <div style="font-size:10px;color:{gc};font-weight:700;">{node['gate']}</div>
                    </div>
                    <div>
                      <div style="font-size:7px;color:#444;letter-spacing:1px;margin-bottom:1px;">CALC VALUE</div>
                      <div style="font-size:11px;color:{val_color};font-weight:700;font-family:monospace;">{val_display}</div>
                      {f'<div style="font-size:8px;color:#555;font-family:monospace;">target: {tgt_str}</div>' if tgt_str else ''}
                    </div>
                    <div>
                      <div style="font-size:7px;color:#444;letter-spacing:1px;margin-bottom:1px;">PARENTS → CHILDREN</div>
                      <div style="font-size:9px;color:#555;line-height:1.5;">↑ {pnames}</div>
                      <div style="font-size:9px;color:#444;line-height:1.5;">↓ {cnames}</div>
                    </div>
                  </div>
                </div>""", unsafe_allow_html=True)

        st.markdown("---")
        cols = st.columns(6)
        counts = [(lvl, len(show_by_level[lvl])) for lvl in DISPLAY_ORDER]
        counts += [("TOTAL", len(show)), ("📌 PINNED", sum(1 for n in show if n.get("fixedValue") is not None))]
        for i,(lvl,cnt) in enumerate(counts):
            with cols[i%6]:
                c = "#e94560" if lvl == "📌 PINNED" else LEVEL_COLORS.get(lvl,"#e94560")
                st.markdown(f"""<div style="background:#141414;border:1px solid {c}44;border-radius:5px;padding:8px;text-align:center;">
                  <div style="font-size:8px;color:#555;letter-spacing:2px;">{lvl}</div>
                  <div style="font-size:18px;font-weight:700;color:{c};">{cnt}</div>
                </div>""", unsafe_allow_html=True)

# ── TAB 4: Search ──────────────────────────────────────────────────────────
with tab_search:
    if not nodes:
        st.markdown("<div style='color:#333;text-align:center;margin-top:40px;'>No nodes yet</div>", unsafe_allow_html=True)
    else:
        st.markdown("<div style='font-size:9px;color:#555;letter-spacing:2px;margin-bottom:10px;'>SEARCH ACROSS ALL NODES — by name, type, value, or gate</div>", unsafe_allow_html=True)
        sq = st.text_input("Search", placeholder="e.g. IF-016, isolation, 1.25e-04, AND", key="search_q", label_visibility="collapsed")

        if sq.strip():
            lq = sq.strip().lower()
            matches = [n for n in nodes if (
                lq in n["name"].lower() or
                lq in n["type"].lower() or
                lq in n["gate"].lower() or
                lq in fmt(n.get("calculatedValue")).lower() or
                lq in (n.get("nodeId","")).lower() or
                lq in (n.get("ftLabel","")).lower()
            )]
            st.markdown(f"<div style='font-size:10px;color:#ff8c42;margin-bottom:8px;'>{len(matches)} result(s) for <b>\"{sq}\"</b></div>", unsafe_allow_html=True)

            if not matches:
                st.markdown("<div style='color:#555;font-size:11px;'>No nodes matched.</div>", unsafe_allow_html=True)
            else:
                for node in matches:
                    color     = LEVEL_COLORS.get(node["type"], "#7e57c2")
                    gc        = "#4fc3f7" if node["gate"] == "OR" else "#ffb74d"
                    val       = fmt(node.get("calculatedValue"))
                    node_id   = node.get("nodeId", node["id"])
                    ft_lbl    = node.get("ftLabel","")
                    pnames    = " · ".join(by_id[p]["name"] for p in (node.get("parentIds") or []) if p in by_id) or "—"
                    cnames    = " · ".join(n["name"] for n in nodes if node["id"] in (n.get("parentIds") or [])) or "—"
                    is_shared = len(node.get("parentIds") or []) > 1
                    is_group  = node["type"] == "GROUP"
                    is_pinned = node.get("fixedValue") is not None
                    val_color = "#e94560" if is_pinned else color

                    # Highlight matched text in name
                    display_name = node["name"]
                    try:
                        idx = display_name.lower().index(lq)
                        display_name = (display_name[:idx] +
                            f'<span style="background:#f5c518;color:#111;border-radius:2px;padding:0 2px;">{display_name[idx:idx+len(lq)]}</span>' +
                            display_name[idx+len(lq):])
                    except ValueError:
                        pass

                    shape_style = "border-radius:50px;" if is_group else "border-radius:6px;"
                    pin_border  = "border:2px solid #e94560;" if is_pinned else f"border:2px solid {color}44;"

                    badges = f'<code style="background:#1a1a2e;color:{color};font-size:9px;padding:1px 5px;border-radius:3px;font-weight:700;">{node_id}</code>'
                    if ft_lbl: badges += f' <code style="background:#1a1a2e;color:#7e57c2;font-size:9px;padding:1px 5px;border-radius:3px;">{ft_lbl}</code>'
                    if is_shared: badges += ' <span style="background:#f5c518;color:#111;font-size:7px;padding:1px 4px;border-radius:3px;font-weight:700;">SHARED</span>'
                    if is_pinned: badges += f' <span style="background:#e9456022;color:#e94560;font-size:7px;padding:1px 4px;border-radius:3px;border:1px solid #e9456044;">📌 FIXED={fmt(node.get("fixedValue"))}</span>'
                    if is_group:  badges += ' <span style="background:#7e57c222;color:#7e57c2;font-size:7px;padding:1px 4px;border-radius:3px;">GROUP</span>'

                    st.markdown(f"""
                    <div style="background:#141414;{pin_border}{shape_style}
                                padding:9px 14px;margin-bottom:5px;">
                      <div style="display:grid;grid-template-columns:2.5fr 0.7fr 0.7fr 1.5fr 2fr;gap:10px;align-items:start;">
                        <div>
                          <div style="font-weight:700;font-size:11px;color:#ddd;margin-bottom:3px;">{display_name}</div>
                          <div>{badges}</div>
                        </div>
                        <div>
                          <div style="font-size:7px;color:#444;letter-spacing:1px;">TYPE</div>
                          <div style="font-size:10px;color:{color};font-weight:700;">{node['type']}</div>
                        </div>
                        <div>
                          <div style="font-size:7px;color:#444;letter-spacing:1px;">GATE</div>
                          <div style="font-size:10px;color:{gc};font-weight:700;">{node['gate']}</div>
                        </div>
                        <div>
                          <div style="font-size:7px;color:#444;letter-spacing:1px;">VALUE</div>
                          <div style="font-size:11px;color:{val_color};font-weight:700;font-family:monospace;">{val}{'📌' if is_pinned else ''}</div>
                        </div>
                        <div>
                          <div style="font-size:8px;color:#444;">↑ {pnames}</div>
                          <div style="font-size:8px;color:#333;margin-top:2px;">↓ {cnames}</div>
                        </div>
                      </div>
                    </div>""", unsafe_allow_html=True)
        else:
            # Summary table when no search query
            st.markdown("<div style='font-size:9px;color:#555;margin-bottom:8px;'>Enter a search term above, or browse all nodes below:</div>", unsafe_allow_html=True)
            for level in DISPLAY_ORDER:
                lvl_nodes = by_level[level]
                if not lvl_nodes: continue
                color = LEVEL_COLORS.get(level, "#7e57c2")
                st.markdown(f"<div style='font-size:9px;letter-spacing:3px;color:{color};border-bottom:1px solid {color}33;padding-bottom:3px;margin:10px 0 5px;'>{level} — {len(lvl_nodes)} nodes</div>", unsafe_allow_html=True)
                for node in lvl_nodes:
                    val    = fmt(node.get("calculatedValue"))
                    pnames = " · ".join(by_id[p]["name"] for p in (node.get("parentIds") or []) if p in by_id) or "—"
                    gc     = "#4fc3f7" if node["gate"] == "OR" else "#ffb74d"
                    is_shared = len(node.get("parentIds") or []) > 1
                    st.markdown(f"""
                    <div style="background:#141414;border-left:3px solid {color};border-radius:0 5px 5px 0;
                                padding:5px 10px;margin-bottom:3px;
                                display:grid;grid-template-columns:2.5fr 0.7fr 0.7fr 1.5fr 2fr;gap:8px;align-items:center;">
                      <div style="font-size:10px;color:#ddd;font-weight:{'700' if node['type']=='HAZARD' else '400'};">
                        {node['name']}{'<span style="background:#f5c518;color:#111;font-size:7px;padding:0 3px;border-radius:3px;margin-left:5px;">SHR</span>' if is_shared else ''}
                      </div>
                      <div style="font-size:9px;color:{color};">{node['type']}</div>
                      <div style="font-size:9px;color:{gc};">{node['gate']}</div>
                      <div style="font-size:10px;color:{color};font-family:monospace;font-weight:700;">{val}</div>
                      <div style="font-size:9px;color:#555;">{pnames}</div>
                    </div>""", unsafe_allow_html=True)
