import streamlit as st
import json, math, os, requests, uuid, io, re
from datetime import datetime
import streamlit.components.v1 as components

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
    try: return json.loads(g.get("files",{}).get(fname,{}).get("content","[]"))
    except: return []

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

    fixedValue support:
    - A node with fixedValue set is PINNED — calculatedValue always = fixedValue
    - When an OR-gate parent distributes:
        1. Fixed children claim their pinned value from budget first
        2. Remainder shared equally among unfixed children
      Example: SF-10=2e-8 (OR), FF-46 pinned=1.67e-9, SF-06 unfixed
               → FF-46 keeps 1.67e-9, SF-06 gets (2e-8 - 1.67e-9) = 1.833e-8
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
    return json.dumps(nodes, indent=2).encode("utf-8")

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
        is_shared = len(n.get("parentIds") or []) > 1
        is_pinned = fv is not None
        is_group  = n["type"] == "GROUP"
        lines.append(
            f"CREATE (:FTANode {{"
            f"id:'{n['id']}', nodeId:'{node_id}', name:'{name}', "
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
    hdrs = ["Level", "Type", "Node Name", "Node ID", "Gate",
            "Calc. Value", "Fixed Value (📌)", "Shared", "Parent Nodes", "Child Nodes"]
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
            vals = [
                DISPLAY_ORDER.index(lvl) + 1,
                sanitize_xl(lvl),
                sanitize_xl(n["name"]),
                sanitize_xl(n.get("nodeId", n["id"])),
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
                cell.alignment = lft() if ci in (3, 9, 10) else ctr()
            # Mark fixed value cell red text
            if is_pinned:
                ws.cell(row, 7).font = Font(name="Courier New", size=10,
                                            color="FFFF4D4D", bold=True)
            row += 1
    for ci, w in enumerate([8, 10, 28, 12, 8, 16, 16, 8, 30, 30], 1):
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
        if len(n.get("parentIds") or []) > 1: tags.append("[SHARED]")
        if n.get("fixedValue") is not None:    tags.append(f"[FIXED={fmt(n['fixedValue'])}]")
        if n["type"] == "GROUP":               tags.append("[GROUP]")
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
    D3-force interactive fault tree.
    - Force simulation: nodes repel, edges attract, gravity towards hazard top
    - Toggle force on/off (free drag mode when off)
    - Click node: highlight all direct parents + children (connected nodes)
    - Double-click: expand/collapse subtree
    - Right-drag: pan  |  Scroll: zoom  |  Left-drag: move node
    - Tree state (positions, zoom, pan) never resets unless data changes
    """
    if not nodes: return ""
    import json as _json, hashlib

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

    shown_ids = {n["id"] for n in show_nodes}
    shared_ids = {n["id"] for n in show_nodes
                  if len([p for p in (n.get("parentIds") or []) if p in shown_ids]) > 1}

    ts            = tree_state or {}
    init_scale    = ts.get("scale", 0.9)
    init_tx       = ts.get("tx", 0)
    init_ty       = ts.get("ty", 0)
    init_positions = ts.get("positions", {})
    focus_id      = ts.get("focus_id")

    # Level ordering for Y gravity
    LEVEL_Y = {"HAZARD": 0, "SF": 1, "FF": 2, "GROUP": 2.5, "IF": 3}

    nodes_js = _json.dumps([{
        "id":       n["id"],
        "name":     n["name"],
        "type":     n["type"],
        "gate":     n["gate"],
        "value":    fmt(n.get("calculatedValue")),
        "nodeId":   n.get("nodeId", n["id"]),
        "shared":   n["id"] in shared_ids,
        "isPinned": n.get("fixedValue") is not None,
        "fixedVal": fmt(n.get("fixedValue")) if n.get("fixedValue") is not None else None,
        "isGroup":  n["type"] == "GROUP",
        "color":    LEVEL_COLORS.get(n["type"], "#7e57c2"),
        "tcolor":   LEVEL_TEXT.get(n["type"], "#fff"),
        "levelY":   LEVEL_Y.get(n["type"], 2),
        "parents":  [p for p in (n.get("parentIds") or []) if p in shown_ids],
        "pnames":   [by_id[p]["name"] for p in (n.get("parentIds") or []) if p in shown_ids],
        "children": [c["id"] for c in show_nodes if n["id"] in (c.get("parentIds") or [])],
        "cnames":   [c["name"] for c in show_nodes if n["id"] in (c.get("parentIds") or [])],
    } for n in show_nodes])

    links_js = _json.dumps([
        {"source": pid, "target": n["id"],
         "andGate": by_id[pid]["gate"] == "AND" if pid in by_id else False,
         "shared": n["id"] in shared_ids}
        for n in show_nodes
        for pid in (n.get("parentIds") or [])
        if pid in shown_ids
    ])

    init_pos_js  = _json.dumps(init_positions)
    focus_js     = f'"{focus_id}"' if focus_id else "null"

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{background:#0a0a0a;font-family:'JetBrains Mono','Fira Code',monospace;color:#e0e0e0;
      overflow:hidden;height:100vh;display:flex;flex-direction:column;}}
#toolbar{{display:flex;align-items:center;gap:5px;flex-wrap:wrap;padding:5px 10px;
          background:#111;border-bottom:1px solid #1e1e1e;flex-shrink:0;font-size:10px;
          color:#555;user-select:none;}}
.tb-btn{{background:#1a1a1a;border:1px solid #2a2a2a;color:#aaa;border-radius:4px;
         padding:3px 9px;cursor:pointer;font-family:inherit;font-size:10px;
         transition:all 0.1s;white-space:nowrap;}}
.tb-btn:hover{{background:#252525;color:#fff;border-color:#555;}}
.tb-btn.active{{background:#e94560;border-color:#e94560;color:#fff;}}
#zoom-lbl{{color:#555;min-width:38px;text-align:center;}}
#search-box{{background:#1a1a1a;border:1px solid #2a2a2a;color:#ccc;border-radius:4px;
             padding:3px 8px;font-family:inherit;font-size:10px;width:150px;outline:none;}}
#search-box:focus{{border-color:#e94560;}}
#search-box::placeholder{{color:#444;}}
#srch-info{{color:#666;font-size:9px;min-width:55px;}}
#hint{{color:#252525;font-size:9px;margin-left:4px;flex:1;text-align:right;}}
svg#tree{{position:absolute;top:0;left:0;width:100%;height:100%;}}
#dp{{position:fixed;bottom:0;left:0;right:0;background:#141414f2;
     border-top:2px solid #333;padding:7px 14px 9px;display:none;
     backdrop-filter:blur(10px);z-index:200;}}
.dg{{display:grid;grid-template-columns:repeat(5,1fr);gap:5px;margin:5px 0 4px;}}
.dc{{background:#0a0a0a;border-radius:5px;padding:5px;text-align:center;}}
.dcl{{font-size:7px;color:#555;letter-spacing:2px;margin-bottom:2px;}}
.dcv{{font-size:11px;font-weight:700;}}
.dr{{display:grid;grid-template-columns:1fr 1fr;gap:5px;}}
.ds{{background:#0a0a0a;border-radius:5px;padding:5px;}}
.dsl{{font-size:7px;color:#555;letter-spacing:2px;margin-bottom:2px;}}
.dsv{{font-size:10px;color:#ccc;line-height:1.4;}}
#dp-close{{position:absolute;top:6px;right:10px;background:none;border:none;
            color:#555;font-size:15px;cursor:pointer;}}
#dp-close:hover{{color:#fff;}}
</style>
</head><body>
<div id="toolbar">
  <button class="tb-btn" onclick="zoomBy(0.15)">＋</button>
  <button class="tb-btn" onclick="zoomBy(-0.15)">－</button>
  <span id="zoom-lbl">90%</span>
  <span style="color:#2a2a2a">|</span>
  <button class="tb-btn" id="btn-force" onclick="toggleForce()" title="Toggle physics simulation">⚡ Force</button>
  <button class="tb-btn" onclick="resetLayout()" title="Re-run layout from scratch">↺ Layout</button>
  <button class="tb-btn" onclick="fitView()" title="Fit all nodes in view">⊡ Fit</button>
  <button class="tb-btn" onclick="clearHL()" title="Clear selection">✕ Clear</button>
  <span style="color:#2a2a2a">|</span>
  <input id="search-box" type="text" placeholder="Search nodes…" oninput="doSearch(this.value)"/>
  <button class="tb-btn" onclick="searchNext()">▼</button>
  <button class="tb-btn" onclick="searchPrev()">▲</button>
  <span id="srch-info"></span>
  <span id="hint">Scroll=zoom · Right-drag=pan · Left-drag=move · Click=inspect · Dbl-click=expand/collapse</span>
</div>
<svg id="tree">
  <defs>
    <marker id="arr" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto">
      <path d="M0,0 L6,3 L0,6 Z" fill="#2a2a2a"/>
    </marker>
    <marker id="arr-hl" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto">
      <path d="M0,0 L6,3 L0,6 Z" fill="#4fc3f7"/>
    </marker>
    <marker id="arr-sh" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto">
      <path d="M0,0 L6,3 L0,6 Z" fill="#f5c51888"/>
    </marker>
  </defs>
  <g id="zoom-layer">
    <g id="links-layer"></g>
    <g id="nodes-layer"></g>
  </g>
</svg>
<div id="dp">
  <button id="dp-close" onclick="closeDP()">✕</button>
  <div style="font-size:8px;color:#888;letter-spacing:3px;margin-bottom:2px;">SELECTED NODE</div>
  <div id="dp-title" style="font-size:13px;font-weight:700;margin-bottom:3px;"></div>
  <div class="dg">
    <div class="dc"><div class="dcl">TYPE</div><div class="dcv" id="dp-type"></div></div>
    <div class="dc"><div class="dcl">GATE</div><div class="dcv" id="dp-gate"></div></div>
    <div class="dc"><div class="dcl">VALUE</div><div class="dcv" id="dp-value"></div></div>
    <div class="dc"><div class="dcl">NODE ID</div><div class="dcv" id="dp-nid" style="font-size:9px;"></div></div>
    <div class="dc"><div class="dcl">SHARED</div><div class="dcv" id="dp-shared"></div></div>
  </div>
  <div class="dr">
    <div class="ds"><div class="dsl">PARENTS</div><div class="dsv" id="dp-par"></div></div>
    <div class="ds"><div class="dsl">CHILDREN</div><div class="dsv" id="dp-chi"></div></div>
  </div>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.8.5/d3.min.js"></script>
<script>
// ── Data ──────────────────────────────────────────────────────────────
const RAW_NODES = {nodes_js};
const RAW_LINKS = {links_js};
const INIT_POS  = {init_pos_js};
const FOCUS_ID  = {focus_js};
const GCOLORS   = {{OR:"#4fc3f7", AND:"#ffb74d"}};
const NW = 148, NH = 80;  // node box dimensions

// ── State ─────────────────────────────────────────────────────────────
let forceOn     = true;
let selId       = null;
let collapsed   = new Set();
let searchMatches = [], searchIdx = 0;
let transform   = d3.zoomIdentity.scale({init_scale}).translate({init_tx}/{init_scale}, {init_ty}/{init_scale});

// Build node/link maps
const nodeMap = {{}};
RAW_NODES.forEach(n => nodeMap[n.id] = n);

// ── SVG setup ─────────────────────────────────────────────────────────
const svg    = d3.select("svg#tree");
const zoomG  = svg.select("#zoom-layer");
const linksG = svg.select("#links-layer");
const nodesG = svg.select("#nodes-layer");

const zoom = d3.zoom()
  .scaleExtent([0.05, 4])
  .filter(e => e.button === 2 || e.type === "wheel")
  .on("zoom", e => {{
    transform = e.transform;
    zoomG.attr("transform", transform);
    document.getElementById("zoom-lbl").textContent = Math.round(transform.k * 100) + "%";
  }});

svg.call(zoom)
   .call(zoom.transform, transform)
   .on("contextmenu", e => e.preventDefault());

// ── Force simulation ──────────────────────────────────────────────────
function getVisible() {{
  const hidden = new Set();
  collapsed.forEach(cid => {{
    // collect descendants
    const q = [cid];
    while (q.length) {{
      const cur = q.shift();
      nodeMap[cur]?.children?.forEach(ch => {{
        if (!hidden.has(ch)) {{ hidden.add(ch); q.push(ch); }}
      }});
    }}
  }});
  return RAW_NODES.filter(n => !hidden.has(n.id));
}}

function getVisibleLinks(visibleIds) {{
  const s = new Set(visibleIds);
  return RAW_LINKS.filter(l => s.has(l.source.id||l.source) && s.has(l.target.id||l.target));
}}

let simNodes = [], simLinks = [];

const simulation = d3.forceSimulation()
  .force("link", d3.forceLink().id(d => d.id).distance(120).strength(0.6))
  .force("charge", d3.forceManyBody().strength(-600).distanceMax(500))
  .force("collide", d3.forceCollide(95))
  .force("x", d3.forceX(d => {{
    // Spread nodes horizontally by their index among same-level nodes
    const sameLevel = simNodes.filter(n => n.levelY === d.levelY);
    const idx = sameLevel.indexOf(d);
    const total = sameLevel.length;
    const svgW = svg.node().getBoundingClientRect().width || 1200;
    return (idx + 0.5) / total * svgW;
  }}).strength(0.08))
  .force("y", d3.forceY(d => {{
    const svgH = svg.node().getBoundingClientRect().height || 700;
    return (d.levelY / 3.5) * (svgH * 0.85) + 40;
  }}).strength(0.35))
  .alphaDecay(0.025)
  .velocityDecay(0.4)
  .on("tick", ticked);

function initSimulation() {{
  const visible = getVisible();
  simNodes = visible.map(n => {{
    const saved = INIT_POS[n.id];
    const existing = simNodes.find(s => s.id === n.id);
    const svgW = svg.node().getBoundingClientRect().width || 1200;
    const svgH = svg.node().getBoundingClientRect().height || 700;
    return Object.assign(n, {{
      x: saved?.x ?? existing?.x ?? (Math.random() * svgW),
      y: saved?.y ?? existing?.y ?? (n.levelY / 3.5 * svgH * 0.85 + 40),
      vx: 0, vy: 0,
      fx: saved?.fx ?? null,
      fy: saved?.fy ?? null,
    }});
  }});

  const visIds = new Set(simNodes.map(n => n.id));
  simLinks = RAW_LINKS
    .filter(l => visIds.has(l.source.id||l.source) && visIds.has(l.target.id||l.target))
    .map(l => ({{...l}}));

  simulation.nodes(simNodes);
  simulation.force("link").links(simLinks);

  if (!forceOn) simulation.stop();
  else simulation.alpha(0.3).restart();

  renderLinks();
  renderNodes();
}}

// ── Render links ──────────────────────────────────────────────────────
function renderLinks() {{
  const sel = linksG.selectAll("line.link").data(simLinks, d =>
    (d.source.id||d.source) + "--" + (d.target.id||d.target));

  sel.enter().append("line")
    .attr("class", "link")
    .attr("stroke-width", d => d.shared ? 1 : 1.8)
    .attr("stroke-dasharray", d => d.andGate ? "6,3" : (d.shared ? "3,5" : null))
    .attr("opacity", d => d.shared ? 0.45 : 0.85)
    .attr("marker-end", d => d.shared ? "url(#arr-sh)" : "url(#arr)")
    .merge(sel);

  sel.exit().remove();
}}

// ── Render nodes ──────────────────────────────────────────────────────
function renderNodes() {{
  const sel = nodesG.selectAll("g.node-g").data(simNodes, d => d.id);

  const entered = sel.enter().append("g")
    .attr("class", "node-g")
    .attr("cursor", "grab")
    .on("click", (e, d) => {{ e.stopPropagation(); selectNode(d.id); }})
    .on("dblclick", (e, d) => {{ e.stopPropagation(); toggleCollapse(d.id); }})
    .call(d3.drag()
      .filter(e => e.button === 0)
      .on("start", (e, d) => {{
        e.sourceEvent.stopPropagation();
        if (!e.active && forceOn) simulation.alphaTarget(0.05).restart();
        d.fx = d.x; d.fy = d.y;
      }})
      .on("drag", (e, d) => {{ d.fx = e.x; d.fy = e.y; }})
      .on("end", (e, d) => {{
        if (!e.active && forceOn) simulation.alphaTarget(0);
        // Keep position pinned only if force is off
        if (forceOn) {{ d.fx = null; d.fy = null; }}
        savePositions();
      }})
    );

  // Background rect (for GROUP: oval via rx/ry; else rectangle)
  entered.append("rect")
    .attr("class", "node-rect")
    .attr("width",  d => d.isGroup ? 120 : NW)
    .attr("height", d => d.isGroup ? 64  : NH)
    .attr("x",      d => d.isGroup ? -60 : -NW/2)
    .attr("y",      d => d.isGroup ? -32 : -NH/2)
    .attr("rx",     d => d.isGroup ? 60  : 8)
    .attr("ry",     d => d.isGroup ? 32  : 8)
    .attr("fill",   d => d.color)
    .attr("stroke", d => d.isPinned ? "#e94560" : d.color)
    .attr("stroke-width",    d => d.isPinned ? 2.5 : 1.5)
    .attr("stroke-dasharray", d => d.isPinned ? "5,3" : null);

  // Node type label
  entered.append("text")
    .attr("class", "node-type")
    .attr("y", d => d.isGroup ? -14 : -NH/2 + 12)
    .attr("text-anchor", "middle")
    .attr("fill", d => d.tcolor)
    .attr("opacity", 0.7)
    .attr("font-size", 7)
    .attr("font-family", "JetBrains Mono, monospace")
    .attr("letter-spacing", 1.5)
    .text(d => d.isGroup ? "COMBINED" : d.type + (d.shared ? " ◈" : "") + (d.isPinned ? " 📌" : ""));

  // Node name (wrapped)
  entered.append("foreignObject")
    .attr("class", "node-fo")
    .attr("x",      d => d.isGroup ? -55 : -NW/2 + 6)
    .attr("y",      d => d.isGroup ? -22 : -NH/2 + 16)
    .attr("width",  d => d.isGroup ? 110 : NW - 12)
    .attr("height", d => d.isGroup ? 28  : 36)
    .append("xhtml:div")
    .attr("xmlns", "http://www.w3.org/1999/xhtml")
    .style("font-size", "9.5px")
    .style("font-weight", "700")
    .style("font-family", "JetBrains Mono, monospace")
    .style("color", d => d.tcolor)
    .style("text-align", "center")
    .style("word-break", "break-word")
    .style("line-height", "1.2")
    .style("overflow", "hidden")
    .text(d => d.name);

  // Value box
  entered.append("rect")
    .attr("class", "val-bg")
    .attr("x",      d => d.isGroup ? -44 : -NW/2 + 8)
    .attr("y",      d => d.isGroup ? 10  : NH/2 - 22)
    .attr("width",  d => d.isGroup ? 88  : NW - 16)
    .attr("height", 18)
    .attr("rx", 3)
    .attr("fill", "rgba(0,0,0,0.28)");

  entered.append("text")
    .attr("class", "val-txt")
    .attr("y",    d => d.isGroup ? 23 : NH/2 - 8)
    .attr("text-anchor", "middle")
    .attr("fill", d => d.isPinned ? "#e94560" : d.tcolor)
    .attr("font-size", 10)
    .attr("font-weight", 700)
    .attr("font-family", "JetBrains Mono, monospace")
    .text(d => d.value + (d.isPinned ? " 📌" : ""));

  // Gate badge
  entered.filter(d => d.children && d.children.length > 0)
    .append("rect")
    .attr("class", "gate-bg")
    .attr("x", -16).attr("y", d => NH/2 + 2)
    .attr("width", 32).attr("height", 13)
    .attr("rx", 3)
    .attr("fill", "#0d0d0d")
    .attr("stroke", d => GCOLORS[d.gate] || "#aaa")
    .attr("stroke-width", 1);

  entered.filter(d => d.children && d.children.length > 0)
    .append("text")
    .attr("class", "gate-txt")
    .attr("y", d => NH/2 + 12)
    .attr("text-anchor", "middle")
    .attr("fill", d => GCOLORS[d.gate] || "#aaa")
    .attr("font-size", 8)
    .attr("font-weight", 700)
    .attr("font-family", "JetBrains Mono, monospace")
    .attr("letter-spacing", 1)
    .text(d => d.gate);

  // Collapse indicator for nodes with children
  entered.filter(d => d.children && d.children.length > 0)
    .append("circle")
    .attr("class", "collapse-btn")
    .attr("cx", d => d.isGroup ? 50 : NW/2 - 8)
    .attr("cy", d => d.isGroup ? -28 : -NH/2 + 8)
    .attr("r", 7)
    .attr("fill", "#1a1a1a")
    .attr("stroke", "#333")
    .attr("cursor", "pointer")
    .on("click", (e, d) => {{ e.stopPropagation(); toggleCollapse(d.id); }});

  entered.filter(d => d.children && d.children.length > 0)
    .append("text")
    .attr("class", "collapse-icon")
    .attr("x", d => d.isGroup ? 50 : NW/2 - 8)
    .attr("y", d => d.isGroup ? -24 : -NH/2 + 12)
    .attr("text-anchor", "middle")
    .attr("fill", "#888")
    .attr("font-size", 10)
    .attr("font-weight", 700)
    .attr("font-family", "monospace")
    .attr("pointer-events", "none")
    .text(d => collapsed.has(d.id) ? "+" : "−");

  sel.exit().remove();

  // Update collapse icon state after re-render
  nodesG.selectAll("text.collapse-icon")
    .text(d => collapsed.has(d.id) ? "+" : "−");
}}

// ── Tick ──────────────────────────────────────────────────────────────
function ticked() {{
  linksG.selectAll("line.link")
    .attr("x1", d => (d.source.x||0))
    .attr("y1", d => (d.source.y||0) + NH/2)
    .attr("x2", d => (d.target.x||0))
    .attr("y2", d => (d.target.y||0) - NH/2 - 6);

  nodesG.selectAll("g.node-g")
    .attr("transform", d => `translate(${{d.x||0}},${{d.y||0}})`);
}}

// ── Force toggle ──────────────────────────────────────────────────────
function toggleForce() {{
  forceOn = !forceOn;
  const btn = document.getElementById("btn-force");
  if (forceOn) {{
    btn.classList.add("active");
    btn.textContent = "⚡ Force ON";
    // Release all fx/fy pins (except manually dragged when force is off)
    simNodes.forEach(n => {{ n.fx = null; n.fy = null; }});
    simulation.alpha(0.3).restart();
  }} else {{
    btn.classList.remove("active");
    btn.textContent = "⚡ Force OFF";
    simulation.stop();
    // Pin everything at current position
    simNodes.forEach(n => {{ n.fx = n.x; n.fy = n.y; }});
  }}
}}

// ── Save positions back (for Python to persist) ───────────────────────
function savePositions() {{
  const pos = {{}};
  simNodes.forEach(n => {{ pos[n.id] = {{x: n.x, y: n.y, fx: n.fx, fy: n.fy}}; }});
  try {{ window.parent.postMessage({{type:"fta_positions", positions: pos}}, "*"); }} catch(e) {{}}
}}

// ── Collapse / expand ─────────────────────────────────────────────────
function toggleCollapse(id) {{
  if (collapsed.has(id)) collapsed.delete(id);
  else collapsed.add(id);
  initSimulation();
}}

// ── Reset layout from scratch ─────────────────────────────────────────
function resetLayout() {{
  // Clear saved positions and re-randomise
  simNodes.forEach(n => {{ n.fx = null; n.fy = null; n.x = undefined; n.y = undefined; }});
  initSimulation();
  if (!forceOn) {{ forceOn = true; toggleForce(); }}
  else simulation.alpha(1).restart();
}}

// ── Fit all nodes in view ─────────────────────────────────────────────
function fitView() {{
  if (!simNodes.length) return;
  const xs = simNodes.map(n => n.x||0);
  const ys = simNodes.map(n => n.y||0);
  const minX = Math.min(...xs) - NW, maxX = Math.max(...xs) + NW;
  const minY = Math.min(...ys) - NH, maxY = Math.max(...ys) + NH;
  const svgEl = svg.node();
  const W = svgEl.getBoundingClientRect().width  || 1000;
  const H = svgEl.getBoundingClientRect().height || 700;
  const k  = Math.min(0.95, Math.min(W/(maxX-minX), H/(maxY-minY)));
  const tx_ = W/2 - k*(minX+maxX)/2;
  const ty_ = H/2 - k*(minY+maxY)/2;
  svg.transition().duration(500)
    .call(zoom.transform, d3.zoomIdentity.translate(tx_, ty_).scale(k));
}}

// ── Zoom helpers ──────────────────────────────────────────────────────
function zoomBy(delta) {{
  svg.transition().duration(200).call(zoom.scaleBy, 1 + delta);
}}

// ── Selection + path highlight ────────────────────────────────────────
function selectNode(id) {{
  if (selId === id) {{ selId = null; clearHL(); closeDP(); return; }}
  selId = id;
  const node = nodeMap[id]; if (!node) return;

  // Direct parents and children of clicked node only
  const directParents  = new Set(node.parents || []);
  const directChildren = new Set(node.children || []);
  const connected      = new Set([...directParents, ...directChildren, id]);

  // Style nodes
  nodesG.selectAll("g.node-g").each(function(d) {{
    const el   = d3.select(this);
    const rect = el.select("rect.node-rect");
    if (d.id === id) {{
      rect.attr("stroke", "#e94560").attr("stroke-width", 3)
          .attr("filter", "drop-shadow(0 0 8px #e9456099)");
    }} else if (directParents.has(d.id)) {{
      rect.attr("stroke", "#4fc3f7").attr("stroke-width", 2.5)
          .attr("filter", "drop-shadow(0 0 6px #4fc3f755)");
    }} else if (directChildren.has(d.id)) {{
      rect.attr("stroke", "#ff8c42").attr("stroke-width", 2.5)
          .attr("filter", "drop-shadow(0 0 6px #ff8c4255)");
    }} else {{
      rect.attr("filter", null);
      el.attr("opacity", 0.25);
    }}
  }});

  // Style links
  linksG.selectAll("line.link").each(function(d) {{
    const src = d.source.id || d.source;
    const tgt = d.target.id || d.target;
    const isPath = (src === id || tgt === id);
    d3.select(this)
      .attr("stroke", isPath ? (d.shared ? "#f5c518" : "#4fc3f7") : "#1a1a1a")
      .attr("stroke-width", isPath ? 2.5 : 1)
      .attr("opacity", isPath ? 1 : 0.15)
      .attr("marker-end", isPath ? "url(#arr-hl)" : "url(#arr)");
  }});

  showDP(id, node);
}}

function clearHL() {{
  selId = null;
  nodesG.selectAll("g.node-g")
    .attr("opacity", 1)
    .select("rect.node-rect")
    .attr("stroke", d => d.isPinned ? "#e94560" : d.color)
    .attr("stroke-width", d => d.isPinned ? 2.5 : 1.5)
    .attr("filter", null);

  linksG.selectAll("line.link")
    .attr("stroke", d => d.shared ? "#f5c51866" : "#2a2a2a")
    .attr("stroke-width", d => d.shared ? 1 : 1.8)
    .attr("opacity", d => d.shared ? 0.45 : 0.85)
    .attr("marker-end", d => d.shared ? "url(#arr-sh)" : "url(#arr)");
}}

// ── Detail panel ──────────────────────────────────────────────────────
function showDP(id, node) {{
  const dp = document.getElementById("dp");
  dp.style.display = "block";
  dp.style.borderTopColor = node.color;
  document.getElementById("dp-title").innerHTML =
    `<span style="color:${{node.color}}">${{node.name}}</span>` +
    (node.shared ? ' <span style="background:#f5c518;color:#111;font-size:8px;padding:1px 5px;border-radius:5px;font-weight:700;">SHARED</span>' : '') +
    (node.isPinned ? ` <span style="background:#e94560;color:#fff;font-size:8px;padding:1px 5px;border-radius:5px;font-weight:700;">📌 FIXED ${{node.fixedVal}}</span>` : '');
  document.getElementById("dp-type").textContent  = node.isGroup ? "GROUP" : node.type;
  document.getElementById("dp-type").style.color  = node.color;
  document.getElementById("dp-gate").textContent  = node.gate;
  document.getElementById("dp-gate").style.color  = GCOLORS[node.gate] || "#aaa";
  document.getElementById("dp-value").textContent = node.isPinned ? `${{node.value}} 📌` : node.value;
  document.getElementById("dp-value").style.color = node.isPinned ? "#e94560" : node.color;
  document.getElementById("dp-nid").textContent   = node.nodeId || id;
  document.getElementById("dp-shared").textContent = node.shared ? "YES" : "NO";
  document.getElementById("dp-shared").style.color = node.shared ? "#f5c518" : "#555";
  document.getElementById("dp-par").textContent   = (node.pnames||[]).join(" · ") || "(top event)";
  document.getElementById("dp-chi").textContent   = (node.cnames||[]).join(" · ") || "(leaf node)";
}}
function closeDP() {{
  document.getElementById("dp").style.display = "none";
  clearHL();
}}

// ── Search ────────────────────────────────────────────────────────────
function doSearch(q) {{
  nodesG.selectAll("rect.node-rect").attr("outline", null);
  searchMatches = []; searchIdx = 0;
  if (!q.trim()) {{ document.getElementById("srch-info").textContent = ""; return; }}
  const lq = q.toLowerCase();
  RAW_NODES.forEach(n => {{
    if (n.name.toLowerCase().includes(lq) || n.type.toLowerCase().includes(lq) ||
        n.value.toLowerCase().includes(lq) || (n.nodeId||"").toLowerCase().includes(lq)) {{
      searchMatches.push(n.id);
    }}
  }});
  document.getElementById("srch-info").textContent = searchMatches.length ? `${{searchMatches.length}} found` : "0";
  highlightSearch();
  if (searchMatches.length) panToNode(searchMatches[0]);
}}
function highlightSearch() {{
  nodesG.selectAll("g.node-g").select("rect.node-rect")
    .attr("outline", d => searchMatches.includes(d.id) ? "3px solid #f5c518" : null)
    .attr("filter",  d => searchMatches.includes(d.id) ? "drop-shadow(0 0 6px #f5c518)" : null);
}}
function searchNext() {{
  if (!searchMatches.length) return;
  searchIdx = (searchIdx + 1) % searchMatches.length;
  panToNode(searchMatches[searchIdx]);
  document.getElementById("srch-info").textContent = `${{searchIdx+1}}/${{searchMatches.length}}`;
}}
function searchPrev() {{
  if (!searchMatches.length) return;
  searchIdx = (searchIdx - 1 + searchMatches.length) % searchMatches.length;
  panToNode(searchMatches[searchIdx]);
  document.getElementById("srch-info").textContent = `${{searchIdx+1}}/${{searchMatches.length}}`;
}}
function panToNode(id) {{
  const n = simNodes.find(s => s.id === id); if (!n) return;
  const svgEl = svg.node();
  const W = svgEl.getBoundingClientRect().width  || 1000;
  const H = svgEl.getBoundingClientRect().height || 700;
  const k  = transform.k;
  svg.transition().duration(400).call(
    zoom.transform,
    d3.zoomIdentity.translate(W/2 - k*n.x, H/2 - k*n.y).scale(k)
  );
}}

// ── Init ──────────────────────────────────────────────────────────────
svg.on("click", () => {{ closeDP(); }});
initSimulation();

// Focus pan if requested
if (FOCUS_ID) {{
  setTimeout(() => panToNode(FOCUS_ID), 800);
}}

// Auto-fit on first load if no saved positions
if (!Object.keys(INIT_POS).length) {{
  setTimeout(() => fitView(), 1200);
}}

// Start with force button showing correct state
document.getElementById("btn-force").classList.add("active");
document.getElementById("btn-force").textContent = "⚡ Force ON";
</script>
</body></html>"""

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
        "nodes_hash": "",      # hash of last rendered tree — only rebuild when changed
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
                            st.session_state.nodes_hash = ""
                            st.rerun(scope="app")
                    with cc:
                        if st.button("Del", key=f"d_{fn}"):
                            del_gist_file(GITHUB_TOKEN, GIST_ID, fn)
                            st.session_state.file_list = list_gist_files(GITHUB_TOKEN, GIST_ID); st.rerun(scope="app")

    st.markdown("---")

    # NODE EDITOR
    st.markdown("### 🔧 NODE EDITOR")
    tab_add, tab_edit = st.tabs(["➕ ADD", "✏️ EDIT"])

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

            node_name  = st.text_input("Node Name", placeholder="e.g. Power Failure", key="add_name")
            custom_id  = st.text_input("Node ID (optional)", placeholder="e.g. FF-01, IF-286",
                                       key="add_cid",
                                       help="If this ID already exists, you'll be asked to link as shared.")
            parent_opts = {f"[{n['type']}] {n.get('nodeId',n['id'])} — {n['name']}": n["id"]
                           for n in nodes if n["type"] in VALID_PARENT_TYPES}
            sel_labels  = st.multiselect("Parent Node(s)", list(parent_opts.keys()), key="add_par")
            sel_pids    = [parent_opts[l] for l in sel_labels]
            node_type   = st.selectbox("Type", VALID_CHILD_TYPES, key="add_type",
                                       help="GROUP = Combined Faults oval")
            gate        = st.radio("Gate", ["OR","AND"], horizontal=True, key="add_gate")

            # Fixed value option
            use_fixed  = st.checkbox("📌 Pin to fixed value", key="add_use_fixed",
                                     help="Overrides calculated value. The engine will subtract this node's contribution from the parent budget and give the remainder to siblings. Use for negligible or known-value nodes.")
            fixed_val_input = None
            if use_fixed:
                fixed_val_input = st.text_input("Fixed Value", placeholder="e.g. 1.67e-9 or 0",
                                                key="add_fixed_val",
                                                help="This node will always carry this exact failure rate regardless of tree distribution.")

            # Duplicate ID detection
            cid_clean = custom_id.strip()
            existing_with_id = [n for n in nodes if n.get("nodeId","") == cid_clean and cid_clean != ""]
            duplicate_found  = len(existing_with_id) > 0

            if duplicate_found:
                ex = existing_with_id[0]
                ex_color  = LEVEL_COLORS.get(ex["type"], "#888")
                ex_parents = " · ".join(by_id[p]["name"] for p in (ex.get("parentIds") or []) if p in by_id) or "none"
                st.markdown(f"""
                <div style="background:#1a1200;border:2px solid #f5c518;border-radius:8px;padding:10px 12px;margin:8px 0;">
                  <div style="font-size:9px;color:#f5c518;font-weight:700;letter-spacing:1px;margin-bottom:4px;">
                    ⚠ NODE ID ALREADY EXISTS
                  </div>
                  <div style="font-size:10px;color:#ddd;">
                    <b style="color:{ex_color};">{ex['name']}</b> [{ex['type']} · {ex['gate']}]
                  </div>
                  <div style="font-size:9px;color:#888;margin-top:3px;">
                    Parents: {ex_parents} · Value: <span style="color:{ex_color};font-family:monospace;">{fmt(ex.get('calculatedValue'))}</span>
                  </div>
                </div>""", unsafe_allow_html=True)
                col_share, col_new = st.columns(2)
                with col_share:
                    if st.button("🔗 LINK SHARED", use_container_width=True, type="primary"):
                        if not sel_pids:
                            st.error("Select at least one parent")
                        else:
                            updated = []
                            for n in nodes:
                                if n["id"] == ex["id"]:
                                    n = dict(n)
                                    existing_pids = list(n.get("parentIds") or [])
                                    new_pids_to_add = [p for p in sel_pids if p not in existing_pids]
                                    n["parentIds"] = existing_pids + new_pids_to_add
                                updated.append(n)
                            st.session_state.tree_state["focus_id"] = ex["id"]
                            st.session_state.nodes_since_calc += 1
                            set_nodes(updated)  # no auto-recalc
                            st.success(f"Linked as shared. Press CALCULATE to update values.")
                            st.rerun()
                with col_new:
                    if st.button("➕ NEW NODE", use_container_width=True):
                        if not node_name.strip(): st.error("Enter node name")
                        elif not sel_pids:        st.error("Select at least one parent")
                        else:
                            nid = str(uuid.uuid4())[:7]
                            new_node = {"id": nid, "nodeId": cid_clean,
                                        "name": node_name.strip(), "type": node_type, "gate": gate,
                                        "targetValue": None, "calculatedValue": None, "parentIds": sel_pids}
                            st.session_state.tree_state["focus_id"] = nid
                            st.session_state.nodes_since_calc += 1
                            set_nodes(nodes + [new_node])
                            st.rerun()
            else:
                if st.button("✅ ADD NODE", use_container_width=True, type="primary"):
                    if not node_name.strip(): st.error("Enter node name")
                    elif not sel_pids:        st.error("Select at least one parent")
                    else:
                        fv = None
                        if use_fixed and fixed_val_input:
                            try:    fv = float(fixed_val_input)
                            except: st.error("Fixed value must be a number e.g. 1.67e-9"); fv = None
                        nid = cid_clean if cid_clean and not any(n["id"]==cid_clean for n in nodes) else str(uuid.uuid4())[:7]
                        new_node = {"id": nid, "nodeId": cid_clean or nid,
                                    "name": node_name.strip(), "type": node_type, "gate": gate,
                                    "fixedValue": fv,
                                    "targetValue": None, "calculatedValue": fv, "parentIds": sel_pids}
                        st.session_state.tree_state["focus_id"] = sel_pids[0] if sel_pids else nid
                        st.session_state.nodes_since_calc += 1
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
                eid  = edit_opts[edit_label]
                en   = next((n for n in nodes if n["id"] == eid), None)
                if en:
                    color = LEVEL_COLORS.get(en["type"], "#888")
                    is_shared = len(en.get("parentIds") or []) > 1
                    st.markdown(f"""<div style="background:#141414;border:2px solid {color};border-radius:8px;padding:8px 12px;margin-bottom:8px;">
                      <div style="font-size:8px;color:#888;letter-spacing:2px;">EDITING</div>
                      <div style="font-weight:700;color:{color};">{en['name']}</div>
                      <div style="font-size:9px;color:#666;">{en['type']} · {en['gate']}
                        {'&nbsp;<span style="background:#f5c518;color:#111;font-size:7px;padding:1px 4px;border-radius:3px;font-weight:700;">SHARED</span>' if is_shared else ''}
                      </div></div>""", unsafe_allow_html=True)
                    new_name    = st.text_input("Name", value=en["name"], key="en_name")
                    new_node_id = st.text_input("Node ID", value=en.get("nodeId", en["id"]), key="en_nid")
                    new_gate    = st.radio("Gate", ["OR","AND"], index=0 if en["gate"]=="OR" else 1,
                                           horizontal=True, key="en_gate")
                    if en["type"] != "HAZARD":
                        ti       = VALID_CHILD_TYPES.index(en["type"]) if en["type"] in VALID_CHILD_TYPES else 0
                        new_type = st.selectbox("Type", VALID_CHILD_TYPES, index=ti, key="en_type")
                        avail_p  = {f"[{n['type']}] {n.get('nodeId',n['id'])} — {n['name']}": n["id"]
                                    for n in nodes if n["type"] in VALID_PARENT_TYPES and n["id"] != eid}
                        cur_pl   = [lbl for lbl,pid in avail_p.items() if pid in (en.get("parentIds") or [])]
                        new_pl   = st.multiselect("Parents", list(avail_p.keys()), default=cur_pl, key="en_par",
                                                   help="Add/remove parents to link as shared node.")
                        new_pids = [avail_p[l] for l in new_pl]
                    else:
                        new_type = "HAZARD"; new_pids = []
                        new_tgt  = st.text_input("Target Rate", value=str(en.get("targetValue","")), key="en_tgt")

                    # ── Fixed value pin ──────────────────────────────────
                    cur_fv     = en.get("fixedValue")
                    en_use_fix = st.checkbox("📌 Pin to fixed value", value=(cur_fv is not None), key="en_use_fix",
                                             help="Pin this node's rate. Siblings in the same OR gate receive the remainder of the parent budget.")
                    en_fv_input = None
                    if en_use_fix:
                        en_fv_input = st.text_input("Fixed Value", value=str(cur_fv) if cur_fv is not None else "",
                                                     placeholder="e.g. 1.67e-9 or 0", key="en_fv",
                                                     help="e.g. 0 for negligible, or a known component rate")
                    if cur_fv is not None and not en_use_fix:
                        st.markdown(f"<div style='font-size:9px;color:#f5c518;'>📌 Currently pinned to <b>{fmt(cur_fv)}</b> — uncheck removes pin</div>",
                                    unsafe_allow_html=True)

                    c1, c2 = st.columns(2)
                    with c1:
                        if st.button("💾 APPLY", use_container_width=True, type="primary"):
                            new_fv = None
                            if en_use_fix and en_fv_input is not None:
                                try:    new_fv = float(en_fv_input)
                                except: new_fv = None
                            upd = []
                            for n in nodes:
                                if n["id"] == eid:
                                    n = dict(n)
                                    n["name"]       = new_name.strip() or n["name"]
                                    n["gate"]       = new_gate
                                    n["nodeId"]     = new_node_id.strip() or n.get("nodeId", n["id"])
                                    n["fixedValue"] = new_fv
                                    if n["type"] != "HAZARD":
                                        n["type"]      = new_type
                                        n["parentIds"] = new_pids
                                    else:
                                        try:
                                            tv = float(new_tgt)
                                            n["targetValue"] = tv; n["calculatedValue"] = tv
                                        except: pass
                                upd.append(n)
                            st.session_state.tree_state["focus_id"] = eid
                            st.session_state.nodes_since_calc += 1
                            set_nodes(upd)
                            st.success("Updated. Press CALCULATE to refresh values.")
                            st.rerun()
                    with c2:
                        if en["type"] != "HAZARD":
                            if st.button("🗑 DELETE", use_container_width=True):
                                temp_nodes = [dict(n) for n in nodes if n["id"] != eid]
                                for n in temp_nodes:
                                    if eid in (n.get("parentIds") or []):
                                        n["parentIds"] = [p for p in n["parentIds"] if p != eid]
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
                                st.session_state.tree_state["focus_id"] = None
                                set_nodes(temp_nodes)
                                st.rerun()

with st.sidebar:
    render_sidebar()

# ── Action bar ────────────────────────────────────────────────────────────
nsc = st.session_state.nodes_since_calc

# Warning banner when nodes added without calculating
if nsc > 0:
    warn_color  = "#ff4d4d" if nsc >= 10 else "#f5c518"
    warn_bg     = "#1a0000" if nsc >= 10 else "#1a1200"
    warn_icon   = "🔴" if nsc >= 10 else "🟡"
    warn_msg    = (f"{warn_icon} **{nsc} node{'s' if nsc!=1 else ''} added without calculating** — "
                   f"values shown are stale. Press **▶ CALCULATE** to update.")
    if nsc >= 10:
        warn_msg += f"  \n⚠ {nsc} nodes is a lot to add without calculating — please press CALCULATE now."
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
            st.session_state.nodes_hash = ""
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
        # Sidebar actions (add node before CALCULATE) do NOT change
        # calculatedValue, so the hash stays the same and the tree iframe
        # is NOT destroyed — your zoom, positions, collapse state survive.
        import hashlib as _hl, json as _js
        _hash_data = _js.dumps([{
            "id": n["id"], "name": n["name"], "type": n["type"],
            "gate": n["gate"], "val": fmt(n.get("calculatedValue")),
            "parents": sorted(n.get("parentIds") or []),
            "fixed": str(n.get("fixedValue"))
        } for n in nodes], sort_keys=True) + (fid or "ALL")
        tree_key = _hl.md5(_hash_data.encode()).hexdigest()[:12]

        if st.session_state.nodes_hash != tree_key:
            st.session_state.nodes_hash = tree_key
            tree_html = build_html_tree(nodes, filter_hazard_id=fid, tree_state=ts)
            st.session_state["_cached_tree_html"] = tree_html
        else:
            tree_html = st.session_state.get("_cached_tree_html") or \
                        build_html_tree(nodes, filter_hazard_id=fid, tree_state=ts)

        components.html(tree_html, height=720, scrolling=False)
        st.session_state.tree_state["focus_id"] = None

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
            color = LEVEL_COLORS.get(node["type"], "#888")
            val   = fmt(node.get("calculatedValue"))
            indent = depth * 26
            ref_tag    = '<span style="background:#333;color:#888;font-size:7px;padding:1px 4px;border-radius:4px;margin-left:4px;">REF</span>' if is_ref else ""
            shared_tag = '<span style="background:#f5c518;color:#111;font-size:7px;padding:1px 4px;border-radius:4px;margin-left:4px;">SHARED</span>' if len(node.get("parentIds") or []) > 1 else ""
            gate_tag   = f'<span style="color:{"#4fc3f7" if node["gate"]=="OR" else "#ffb74d"};font-size:8px;margin-left:5px;">[{node["gate"]}]</span>'
            st.markdown(f"""
            <div style="display:flex;align-items:center;padding:4px 8px;margin-left:{indent}px;
                        margin-bottom:2px;background:#141414;border-left:3px solid {color};border-radius:0 5px 5px 0;">
              <div style="flex:1;min-width:0;">
                <span style="color:#555;font-size:10px;">{"└─ " if depth>0 else ""}</span>
                <span style="font-weight:{'700' if depth==0 else '400'};color:#ddd;font-size:11px;">{node['name']}</span>
                <span style="font-size:8px;color:#666;margin-left:5px;">{node['type']}</span>
                {gate_tag}{shared_tag}{ref_tag}
              </div>
              <div style="font-weight:700;font-size:12px;color:{color};font-family:monospace;flex-shrink:0;margin-left:10px;">{val}</div>
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
                pin_note  = f'<div style="font-size:8px;color:#e94560;">📌 FIXED = {fmt(node.get("fixedValue"))}</div>' if is_pinned else ''
                st.markdown(f"""
                <div style="background:#141414;border:1px solid {'#e9456044' if is_pinned else '#222'};border-radius:5px;padding:7px 11px;margin-bottom:3px;
                            display:grid;grid-template-columns:2fr 1fr 1fr 2fr 2fr;gap:8px;align-items:center;">
                  <div>
                    <div style="font-weight:700;font-size:11px;color:#ddd;">{node['name']}</div>
                    {'<div style="font-size:8px;color:#f5c518;">◈ SHARED</div>' if is_shared else ''}
                    {pin_note}
                  </div>
                  <div style="font-size:9px;color:{color};font-weight:700;">{node['type']}</div>
                  <div style="font-size:9px;color:{gc};font-weight:700;">{node['gate']}</div>
                  <div style="font-size:10px;color:{val_color};font-weight:700;font-family:monospace;">{val_display}</div>
                  <div style="font-size:9px;color:#555;">↑ {pnames}<br>↓ {cnames}</div>
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
                lq in (n.get("id","")).lower()
            )]
            st.markdown(f"<div style='font-size:10px;color:#ff8c42;margin-bottom:8px;'>{len(matches)} result(s) for <b>\"{sq}\"</b></div>", unsafe_allow_html=True)

            if not matches:
                st.markdown("<div style='color:#555;font-size:11px;'>No nodes matched.</div>", unsafe_allow_html=True)
            else:
                for node in matches:
                    color     = LEVEL_COLORS.get(node["type"], "#7e57c2")
                    gc        = "#4fc3f7" if node["gate"] == "OR" else "#ffb74d"
                    val       = fmt(node.get("calculatedValue"))
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
                    pin_border  = "border:2px solid #e94560;" if is_pinned else f"border:2px solid {color}55;"
                    st.markdown(f"""
                    <div style="background:#141414;{pin_border}{shape_style}
                                padding:9px 14px;margin-bottom:5px;
                                display:grid;grid-template-columns:2.5fr 0.8fr 0.8fr 1.5fr 2.5fr;gap:10px;align-items:center;">
                      <div>
                        <div style="font-weight:700;font-size:11px;color:#ddd;">{display_name}</div>
                        <div style="font-size:8px;color:#555;margin-top:1px;">id: {node['id']}</div>
                        {'<div style="font-size:8px;color:#f5c518;">◈ SHARED</div>' if is_shared else ''}
                        {'<div style="font-size:8px;color:#7e57c2;">◉ GROUP (Combined Faults)</div>' if is_group else ''}
                        {f'<div style="font-size:8px;color:#e94560;">📌 FIXED = {fmt(node.get("fixedValue"))}</div>' if is_pinned else ''}
                      </div>
                      <div style="font-size:9px;color:{color};font-weight:700;">{node['type']}</div>
                      <div style="font-size:9px;color:{gc};font-weight:700;">{node['gate']}</div>
                      <div style="font-size:10px;color:{val_color};font-weight:700;font-family:monospace;">{val}{'📌' if is_pinned else ''}</div>
                      <div style="font-size:9px;color:#555;">↑ {pnames}<br>↓ {cnames}</div>
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
