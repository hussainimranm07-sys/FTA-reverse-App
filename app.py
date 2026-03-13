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
def reverse_distribute(parent_val, gate, n):
    """
    Reverse-distribute a parent's required failure rate to its n children.

    OR gate:  children are independent alternatives.
              Each child needs: parent_val / n
              (sum approximation: λ_parent ≈ Σ λ_children for small rates)

    AND gate: children must ALL fail simultaneously (combined fault).
              Each child needs: parent_val ^ (1/n)
              (product law: λ_parent ≈ Π λ_children for independent events)

    GROUP nodes (Combined Faults ovals) use their own gate to distribute
    down to their children, then pass their own calculated value up to their
    parent via the parent's gate — exactly like any other node.
    """
    if n == 0: return parent_val
    return parent_val / n if gate == "OR" else parent_val ** (1.0 / n)

def recalculate(nodes):
    """
    Robust top-down reverse distribution using Kahn's topological sort.

    Why topo-sort instead of plain BFS?
    - A shared node can have 10+ parents across multiple hazards.
    - With BFS + visit-count guards, complex shared nets can get wrong values
      if a node is processed before ALL its parents are resolved.
    - Kahn's algorithm guarantees every node is processed exactly once,
      AFTER all its parents have been resolved. This is O(N+E) and handles
      any DAG of 500+ nodes with N-way sharing correctly.

    Standard FTA rules applied:
    - OR gate parent: child_val = parent_val / n_children
      (sum rule: λ_parent ≈ Σλ_children for small λ)
    - AND gate parent: child_val = parent_val ^ (1/n_children)
      (product rule: λ_parent = Πλ_children for independent events)
    - Shared node (multiple parents): receives MAX value across all paths.
      MAX = most conservative (worst-case) allocation — standard FTA practice.
      This is correct for top-down budgeting: the node must meet the strictest
      requirement imposed on it from any path.

    GROUP nodes (Combined Faults ovals) work transparently:
      They are normal nodes with their own gate. A GROUP(AND) under an OR
      parent receives parent_val/n from the parent's OR distribution, then
      distributes (parent_val/n)^(1/k) to each of its k children.
    """
    if not nodes:
        return nodes

    updated = [dict(n) for n in nodes]
    by_id   = {n["id"]: n for n in updated}

    # Reset all non-HAZARD calculated values
    for n in updated:
        if n["type"] != "HAZARD":
            n["calculatedValue"] = None
        else:
            # Ensure HAZARD has its targetValue as calculatedValue
            n["calculatedValue"] = n.get("targetValue") or 1e-7

    # Build adjacency: parent_id -> [child nodes]
    children_of = {n["id"]: [] for n in updated}
    # In-degree: number of parents for each non-HAZARD node
    # For shared nodes this counts each unique parent once
    in_degree = {}
    for n in updated:
        nid = n["id"]
        pids = [p for p in (n.get("parentIds") or []) if p in by_id]
        in_degree[nid] = len(pids)
        for pid in pids:
            if pid in children_of:
                children_of[pid].append(nid)

    # Kahn's algorithm: start from HAZARDs (in_degree handled — they have no parents)
    # Use a queue of nodes whose ALL parents are resolved
    from collections import deque
    resolved = set()
    queue = deque()

    # Seed with HAZARDs
    for n in updated:
        if n["type"] == "HAZARD":
            resolved.add(n["id"])
            queue.append(n["id"])

    # Track how many parents of each node have been resolved
    parents_resolved = {n["id"]: 0 for n in updated}

    # Process in topological order
    processed = 0
    while queue:
        pid = queue.popleft()
        parent = by_id[pid]
        parent_val = parent.get("calculatedValue")

        if parent_val is None:
            # This shouldn't happen in a well-formed tree, but skip safely
            continue

        child_ids = children_of.get(pid, [])
        if not child_ids:
            continue

        # Distribute to each direct child
        # n = number of children (for equal distribution)
        n_ch = len(child_ids)
        child_val = reverse_distribute(parent_val, parent["gate"], n_ch)

        for cid in child_ids:
            child = by_id[cid]
            existing = child.get("calculatedValue")

            # Apply MAX rule for shared nodes — most conservative allocation wins
            if existing is None:
                child["calculatedValue"] = child_val
            else:
                child["calculatedValue"] = max(existing, child_val)

            # Mark this parent as resolved for this child
            parents_resolved[cid] += 1

            # Enqueue child only when ALL its parents have been resolved
            # This guarantees the MAX has been fully computed before we propagate down
            total_parents = in_degree.get(cid, 0)
            if parents_resolved[cid] >= total_parents and cid not in resolved:
                resolved.add(cid)
                queue.append(cid)

        processed += 1

    # Safety: any unresolved nodes (disconnected, cycles) — leave calculatedValue=None
    return updated

# ── Formatters ────────────────────────────────────────────────────────────
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
    Run in Neo4j Browser or via neo4j-shell.
    """
    by_id = {n["id"]: n for n in nodes}
    lines = [
        "// ── FTA Fault Tree — Cypher Export ──────────────────────────",
        f"// Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"// Total nodes: {len(nodes)}",
        "// Run this in Neo4j Browser to create the full graph.",
        "",
        "// STEP 1: Clear existing FTA nodes (optional — comment out to merge)",
        "// MATCH (n:FTANode) DETACH DELETE n;",
        "",
        "// STEP 2: Create all nodes",
    ]
    for n in nodes:
        val   = n.get("calculatedValue")
        val_s = fmt(val) if val is not None else "null"
        name  = n["name"].replace("'", "\\'")
        is_shared = len(n.get("parentIds") or []) > 1
        lines.append(
            f"CREATE (:FTANode {{id:'{n['id']}', name:'{name}', "
            f"type:'{n['type']}', gate:'{n['gate']}', "
            f"calculatedValue:{val_s if val is not None else 'null'}, "
            f"valueStr:'{val_s}', shared:{str(is_shared).lower()}}});"
        )

    lines += ["", "// STEP 3: Create relationships (FEEDS_INTO = child → parent)"]
    for n in nodes:
        for pid in (n.get("parentIds") or []):
            if pid in by_id:
                pname = by_id[pid]["name"].replace("'", "\\'")
                cname = n["name"].replace("'", "\\'")
                lines.append(
                    f"MATCH (c:FTANode {{id:'{n['id']}'}}), (p:FTANode {{id:'{pid}'}}) "
                    f"CREATE (c)-[:FEEDS_INTO {{gate:'{by_id[pid]['gate']}'}}]->(p);"
                )

    lines += [
        "",
        "// STEP 4: Useful queries",
        "// Show full graph:",
        "// MATCH (n:FTANode)-[r]->(m) RETURN n,r,m;",
        "",
        "// Show all shared nodes:",
        "// MATCH (n:FTANode) WHERE n.shared=true RETURN n.name, n.type, n.valueStr;",
        "",
        "// Show path from IF to HAZARD:",
        "// MATCH p=(i:FTANode {type:'IF'})-[:FEEDS_INTO*]->(h:FTANode {type:'HAZARD'})",
        "// RETURN p LIMIT 5;",
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
    fills    = {lvl: PatternFill("solid", fgColor=c.replace("#","FF")) for lvl, c in
                [("HAZARD","FFFF4D4D"),("SF","FFFF8C42"),("FF","FFF5C518"),("IF","FF4CAF7D")]}
    hdr_fill = PatternFill("solid", fgColor="FF0F3460")

    def hdr_font(c): return Font(bold=True,color="FFFFFFFF",name="Courier New")
    def row_font(lvl): return Font(name="Courier New",size=10,
                                   color="FF111111" if lvl=="FF" else "FFFFFFFF")
    def ctr(): return Alignment(horizontal="center",vertical="center")
    def lft(): return Alignment(horizontal="left",  vertical="center")

    # ── Sheet 1: All nodes ──
    ws = wb.active; ws.title = "FTA Nodes"
    hdrs = ["Level","Type","Node Name","Gate","Calc. Value","Parent Nodes","Child Nodes","Shared"]
    for ci,h in enumerate(hdrs,1):
        c=ws.cell(1,ci,h); c.font=hdr_font(None); c.fill=hdr_fill; c.alignment=ctr()
    row=2
    for lvl in LEVEL_ORDER:
        for n in [x for x in nodes if x["type"]==lvl]:
            pnames = " | ".join(by_id[p]["name"] for p in (n.get("parentIds") or []) if p in by_id)
            cnames = " | ".join(x["name"] for x in nodes if n["id"] in (x.get("parentIds") or []))
            vals   = [LEVEL_ORDER.index(lvl)+1, sanitize_xl(lvl), sanitize_xl(n["name"]),
                      sanitize_xl(n["gate"]), n.get("calculatedValue"),
                      sanitize_xl(pnames or "-"), sanitize_xl(cnames or "-"),
                      "YES" if len(n.get("parentIds") or [])>1 else "NO"]
            for ci,v in enumerate(vals,1):
                cell=ws.cell(row,ci,sanitize_xl(v) if isinstance(v,str) else v)
                cell.font=row_font(lvl); cell.fill=fills.get(lvl,hdr_fill)
                cell.alignment=lft() if ci==3 else ctr()
            row+=1
    for ci,w in enumerate([8,10,28,8,16,30,30,8],1):
        ws.column_dimensions[get_column_letter(ci)].width=w

    # ── Sheet 2: Per-hazard hierarchy ──
    ws2=wb.create_sheet("Hierarchy"); ws2.sheet_view.showGridLines=False
    ws2.cell(1,1,"FTA HIERARCHY - TOP TO BOTTOM").font=Font(bold=True,size=14,name="Courier New",color="FFE94560")
    ws2.cell(2,1,f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}").font=Font(size=9,name="Courier New",color="FF888888")
    row=4
    def write_hier(nid, depth, seen):
        nonlocal row
        if nid in seen: return
        seen.add(nid)
        n=by_id.get(nid)
        if not n: return
        label=f"{'    '*depth}{'  -> ' if depth else ''}{n['name']}"
        shared=" [SHARED]" if len(n.get("parentIds") or [])>1 else ""
        c1=ws2.cell(row,1,sanitize_xl(label))
        c2=ws2.cell(row,2,sanitize_xl(f"{n['type']}[{n['gate']}]"))
        c3=ws2.cell(row,3,sanitize_xl(fmt(n.get("calculatedValue"))))
        c4=ws2.cell(row,4,sanitize_xl(shared))
        fhex={"HAZARD":"FFFF4D4D","SF":"FFFF8C42","FF":"FFF5C518","IF":"FF4CAF7D"}.get(n["type"],"FF222222")
        for c in [c1,c2,c3,c4]:
            c.font=Font(name="Courier New",size=10,bold=(depth==0),
                        color="FF111111" if n["type"]=="FF" else "FFFFFFFF")
            c.fill=PatternFill("solid",fgColor=fhex)
        c3.alignment=Alignment(horizontal="right",vertical="center")
        row+=1
        for child in [x for x in nodes if nid in (x.get("parentIds") or [])]:
            write_hier(child["id"],depth+1,seen)
    for h in [n for n in nodes if n["type"]=="HAZARD"]:
        write_hier(h["id"],0,set())
        row+=1  # blank line between hazards
    for ci,w in enumerate([50,16,16,12],1):
        ws2.column_dimensions[get_column_letter(ci)].width=w

    buf=io.BytesIO(); wb.save(buf); buf.seek(0)
    return buf.getvalue()

# ── Tree HTML builder ─────────────────────────────────────────────────────
def build_html_tree(nodes, filter_hazard_id=None, tree_state=None):
    """
    Interactive tree with persistent state across Streamlit reruns.
    tree_state = {scale, tx, ty, collapsed:[], positions:{id:{x,y}}, focus_id}
    Shared-node edges rendered as dashed lines through the background.
    """
    if not nodes: return ""
    by_id   = {n["id"]: n for n in nodes}

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

    hazards = [n for n in show_nodes if n["type"] == "HAZARD"]
    if not hazards: return ""

    ts = tree_state or {}
    init_scale     = ts.get("scale", 1.0)
    init_tx        = ts.get("tx", 0)
    init_ty        = ts.get("ty", 0)
    init_collapsed = ts.get("collapsed", [])
    init_positions = ts.get("positions", {})
    focus_id       = ts.get("focus_id", None)

    # Identify shared nodes (appear in multiple parents within shown nodes)
    shown_ids = {n["id"] for n in show_nodes}
    shared_ids = {
        n["id"] for n in show_nodes
        if len([p for p in (n.get("parentIds") or []) if p in shown_ids]) > 1
    }

    import json as _json
    nodes_js = _json.dumps({
        n["id"]: {
            "name":      n["name"],
            "type":      n["type"],
            "gate":      n["gate"],
            "value":     fmt(n.get("calculatedValue")),
            "nodeId":    n.get("nodeId", n["id"]),
            "parentIds": [p for p in (n.get("parentIds") or []) if p in by_id],
            "children":  [c["id"] for c in show_nodes if n["id"] in (c.get("parentIds") or [])],
            "childNames":[c["name"] for c in show_nodes if n["id"] in (c.get("parentIds") or [])],
            "parents":   [by_id[p]["name"] for p in (n.get("parentIds") or []) if p in by_id],
            "shared":    n["id"] in shared_ids,
            "color":     LEVEL_COLORS.get(n["type"], "#7e57c2"),
            "tcolor":    LEVEL_TEXT.get(n["type"], "#fff"),
            "isGroup":   n["type"] == "GROUP",
        }
        for n in show_nodes
    })

    # Split edges: primary (first parent) vs shared (secondary parents)
    primary_edges = []
    shared_edges  = []
    for n in show_nodes:
        pids = [p for p in (n.get("parentIds") or []) if p in by_id and p in shown_ids]
        for i, pid in enumerate(pids):
            if i == 0:
                primary_edges.append([pid, n["id"]])
            else:
                shared_edges.append([pid, n["id"]])

    edges_js        = _json.dumps(primary_edges)
    shared_edges_js = _json.dumps(shared_edges)
    level_groups_js = _json.dumps({
        lvl: [n["id"] for n in show_nodes if n["type"] == lvl]
        for lvl in LEVEL_ORDER
    })
    init_collapsed_js = _json.dumps(init_collapsed)
    init_positions_js = _json.dumps(init_positions)
    focus_js = f'"{focus_id}"' if focus_id else "null"

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{background:#0a0a0a;font-family:'JetBrains Mono','Fira Code',monospace;color:#e0e0e0;overflow:hidden;height:100vh;display:flex;flex-direction:column;}}
#toolbar{{display:flex;align-items:center;gap:5px;flex-wrap:wrap;padding:5px 10px;background:#111;border-bottom:1px solid #1e1e1e;flex-shrink:0;font-size:10px;color:#555;user-select:none;}}
.tb-btn{{background:#1a1a1a;border:1px solid #2a2a2a;color:#aaa;border-radius:4px;padding:3px 9px;cursor:pointer;font-family:inherit;font-size:10px;transition:all 0.1s;white-space:nowrap;}}
.tb-btn:hover{{background:#252525;color:#fff;border-color:#555;}}
#zoom-lbl{{color:#555;min-width:36px;text-align:center;}}
#search-box{{background:#1a1a1a;border:1px solid #2a2a2a;color:#ccc;border-radius:4px;padding:3px 8px;font-family:inherit;font-size:10px;width:150px;outline:none;}}
#search-box:focus{{border-color:#e94560;}}
#search-box::placeholder{{color:#444;}}
#search-info{{color:#666;font-size:9px;min-width:55px;}}
#viewport{{flex:1;overflow:hidden;position:relative;cursor:default;}}
#canvas{{position:absolute;top:0;left:0;transform-origin:0 0;will-change:transform;}}
svg#edges{{position:absolute;top:0;left:0;pointer-events:none;overflow:visible;}}
.nw{{position:absolute;display:flex;flex-direction:column;align-items:center;cursor:grab;}}
.fn{{border-radius:8px;padding:7px 11px;min-width:128px;max-width:162px;user-select:none;border:2px solid transparent;transition:filter 0.12s,box-shadow 0.12s;}}
.fn:hover{{filter:brightness(1.18);}}
.fn.dimmed{{opacity:0.18;}}
.fn-group{{border-radius:50%;padding:10px 14px;min-width:100px;max-width:128px;text-align:center;user-select:none;border:2px solid transparent;transition:filter 0.12s,box-shadow 0.12s;}}
.fn-group:hover{{filter:brightness(1.18);}}
.fn-group.dimmed{{opacity:0.18;}}
.fn.search-match,.fn-group.search-match{{outline:2px solid #f5c518;outline-offset:2px;}}
.gt{{font-size:8px;font-weight:700;padding:1px 6px;border-radius:3px;margin-top:3px;border:1px solid;letter-spacing:1px;background:#0d0d0d;}}
.cb{{width:18px;height:18px;border-radius:50%;border:1px solid #333;background:#1a1a1a;color:#888;font-size:9px;cursor:pointer;display:flex;align-items:center;justify-content:center;margin-top:2px;transition:all 0.1s;flex-shrink:0;font-family:monospace;font-weight:700;}}
.cb:hover{{background:#252525;color:#fff;border-color:#555;}}
#dp{{position:fixed;bottom:0;left:0;right:0;background:#141414f2;border-top:2px solid #333;padding:7px 14px 9px;display:none;backdrop-filter:blur(10px);z-index:200;}}
.dg{{display:grid;grid-template-columns:repeat(4,1fr);gap:5px;margin:5px 0 4px;}}
.dc{{background:#0a0a0a;border-radius:5px;padding:5px;text-align:center;}}
.dcl{{font-size:7px;color:#555;letter-spacing:2px;margin-bottom:2px;}}
.dcv{{font-size:11px;font-weight:700;}}
.dr{{display:grid;grid-template-columns:1fr 1fr;gap:5px;}}
.ds{{background:#0a0a0a;border-radius:5px;padding:5px;}}
.dsl{{font-size:7px;color:#555;letter-spacing:2px;margin-bottom:2px;}}
.dsv{{font-size:10px;color:#ccc;line-height:1.4;}}
#dp-close{{position:absolute;top:6px;right:10px;background:none;border:none;color:#555;font-size:15px;cursor:pointer;}}
#dp-close:hover{{color:#fff;}}
</style></head><body>
<div id="toolbar">
  <button class="tb-btn" onclick="zoomBy(0.15)">＋</button>
  <button class="tb-btn" onclick="zoomBy(-0.15)">－</button>
  <span id="zoom-lbl">100%</span>
  <span style="color:#2a2a2a">|</span>
  <button class="tb-btn" onclick="resetView()">&#8962; Reset</button>
  <button class="tb-btn" onclick="expandAll()">&#8862; Expand</button>
  <button class="tb-btn" onclick="collapseAll()">&#8863; Collapse</button>
  <button class="tb-btn" onclick="clearHL()">&#10005; Clear</button>
  <span style="color:#2a2a2a">|</span>
  <input id="search-box" type="text" placeholder="Search nodes..." oninput="doSearch(this.value)" />
  <button class="tb-btn" onclick="searchNext()">&#x25BC;</button>
  <button class="tb-btn" onclick="searchPrev()">&#x25B2;</button>
  <span id="search-info"></span>
</div>
<div id="viewport"><div id="canvas"><svg id="edges"></svg></div></div>
<div id="dp">
  <button id="dp-close" onclick="closeDP()">&#10005;</button>
  <div style="font-size:8px;color:#888;letter-spacing:3px;margin-bottom:2px;">SELECTED NODE</div>
  <div id="dp-title" style="font-size:13px;font-weight:700;margin-bottom:3px;"></div>
  <div class="dg">
    <div class="dc"><div class="dcl">TYPE</div><div class="dcv" id="dp-type"></div></div>
    <div class="dc"><div class="dcl">GATE</div><div class="dcv" id="dp-gate"></div></div>
    <div class="dc"><div class="dcl">VALUE</div><div class="dcv" id="dp-value"></div></div>
    <div class="dc"><div class="dcl">NODE ID</div><div class="dcv" id="dp-nid" style="font-size:9px;"></div></div>
  </div>
  <div class="dr">
    <div class="ds"><div class="dsl">PARENTS</div><div class="dsv" id="dp-par"></div></div>
    <div class="ds"><div class="dsl">CHILDREN</div><div class="dsv" id="dp-chi"></div></div>
  </div>
</div>
<script>
const NODES={nodes_js};
const EDGES={edges_js};
const SHARED_EDGES={shared_edges_js};
const LEVELS={level_groups_js};
const NW=145,NH=82,GW=118,GH=64;
const GCOLORS={{OR:"#4fc3f7",AND:"#ffb74d"}};

// ── Persistent state restored from Python ────────────────────────────
let scale={init_scale},tx={init_tx},ty={init_ty};
let collapsed=new Set({init_collapsed_js});
let savedPos={init_positions_js};  // user-dragged positions
let pos={{}};   // working positions (layout + overrides)
let selId=null,panning=false,panSt={{x:0,y:0}};
let dragId=null,dragOff={{x:0,y:0}},didDrag=false;
let searchMatches=[],searchIdx=0;
const FOCUS_ID={focus_js};

const canvas=document.getElementById("canvas");
const vp=document.getElementById("viewport");
const svg=document.getElementById("edges");

function nodeW(id){{return NODES[id]?.isGroup?GW:NW;}}
function nodeH(id){{return NODES[id]?.isGroup?GH:NH;}}

// ── Save state back to Streamlit parent ──────────────────────────────
function saveState(){{
  const state={{
    scale:scale, tx:tx, ty:ty,
    collapsed:[...collapsed],
    positions:Object.fromEntries(Object.entries(pos).filter(([id])=>id in savedPos||Object.keys(savedPos).length>0? true:false)),
  }};
  // Only persist manually-moved node positions
  state.positions = savedPos;
  try{{ window.parent.postMessage({{type:"fta_tree_state",state}}, "*"); }}catch(e){{}}
}}

// ── Layout ────────────────────────────────────────────────────────────
function buildLayout(){{
  const LYS={{HAZARD:30,SF:200,FF:370,GROUP:450,IF:540}};
  ["HAZARD","SF","FF","GROUP","IF"].forEach(lvl=>{{
    const ids=LEVELS[lvl]||[];
    ids.forEach((id,i)=>{{
      pos[id]={{x:60+i*(NW+28), y:LYS[lvl]||400}};
    }});
  }});
  // Centre hazards over direct children
  (LEVELS.HAZARD||[]).forEach(hid=>{{
    const kids=EDGES.filter(e=>e[0]===hid).map(e=>e[1]);
    if(kids.length){{
      const xs=kids.map(k=>(pos[k]?pos[k].x+nodeW(k)/2:400));
      pos[hid].x=(xs.reduce((a,b)=>a+b,0)/xs.length)-NW/2;
    }}
  }});
  // Apply any saved dragged positions
  Object.entries(savedPos).forEach(([id,p])=>{{ if(id in pos) pos[id]=p; }});
}}

// ── Transform ─────────────────────────────────────────────────────────
function applyT(){{
  canvas.style.transform=`translate(${{tx}}px,${{ty}}px) scale(${{scale}})`;
  document.getElementById("zoom-lbl").textContent=Math.round(scale*100)+"%";
  drawEdges();
}}
function zoomBy(d,cx,cy){{
  const vr=vp.getBoundingClientRect();
  cx=cx??vr.width/2; cy=cy??vr.height/2;
  const ns=Math.min(3.5,Math.max(0.1,scale+d)),r=ns/scale;
  tx=cx-r*(cx-tx); ty=cy-r*(cy-ty); scale=ns; applyT(); saveState();
}}
function resetView(){{
  scale=1;
  const vr=vp.getBoundingClientRect();
  const allX=Object.keys(pos).map(id=>pos[id].x+nodeW(id));
  const minX=Math.min(...Object.values(pos).map(p=>p.x));
  const treeW=Math.max(...allX)-minX;
  tx=Math.max(20,(vr.width-treeW)/2)-minX; ty=20; applyT(); saveState();
}}
function panToNode(id,animate){{
  const p=pos[id]; if(!p) return;
  const vr=vp.getBoundingClientRect();
  const nw=nodeW(id),nh=nodeH(id);
  tx=vr.width/2-(p.x+nw/2)*scale;
  ty=vr.height/3-(p.y+nh/2)*scale;
  applyT();
}}

// Events
vp.addEventListener("wheel",e=>{{e.preventDefault();zoomBy(e.deltaY<0?0.1:-0.1,e.clientX,e.clientY);}},{{passive:false}});
vp.addEventListener("mousedown",e=>{{
  if(e.button===2){{panning=true;panSt={{x:e.clientX-tx,y:e.clientY-ty}};vp.style.cursor="grabbing";e.preventDefault();}}
}});
window.addEventListener("mousemove",e=>{{
  if(panning){{tx=e.clientX-panSt.x;ty=e.clientY-panSt.y;applyT();}}
  if(dragId){{
    didDrag=true;
    const cp=pos[dragId];
    const nx=e.clientX/scale-tx/scale-dragOff.x;
    const ny=e.clientY/scale-ty/scale-dragOff.y;
    pos[dragId]={{x:nx,y:ny}};
    savedPos[dragId]={{x:nx,y:ny}};
    const el=document.getElementById("nw-"+dragId);
    if(el){{el.style.left=nx+"px";el.style.top=ny+"px";}}
    drawEdges();
  }}
}});
window.addEventListener("mouseup",e=>{{
  if(e.button===2){{panning=false;vp.style.cursor="default";saveState();}}
  if(e.button===0&&dragId){{const id=dragId;dragId=null;document.body.style.userSelect="";saveState();if(!didDrag)selectNode(id);}}
}});
vp.addEventListener("contextmenu",e=>e.preventDefault());

// Touch pinch
let ltd=null;
vp.addEventListener("touchstart",e=>{{if(e.touches.length===2)ltd=Math.hypot(e.touches[0].clientX-e.touches[1].clientX,e.touches[0].clientY-e.touches[1].clientY);}},{{passive:true}});
vp.addEventListener("touchmove",e=>{{
  if(e.touches.length===2&&ltd){{
    const d=Math.hypot(e.touches[0].clientX-e.touches[1].clientX,e.touches[0].clientY-e.touches[1].clientY);
    const mx=(e.touches[0].clientX+e.touches[1].clientX)/2,my=(e.touches[0].clientY+e.touches[1].clientY)/2;
    zoomBy((d-ltd)*0.008,mx,my); ltd=d; e.preventDefault();
  }}
}},{{passive:false}});

// ── Collapse ──────────────────────────────────────────────────────────
function getDesc(id){{
  const r=new Set(),q=[id];
  while(q.length){{const c=q.shift();EDGES.filter(e=>e[0]===c).forEach(e=>{{if(!r.has(e[1])){{r.add(e[1]);q.push(e[1]);}}}});}}
  return r;
}}
function getAnc(id){{
  const r=new Set(),q=[id];
  while(q.length){{const c=q.shift();(NODES[c]?.parentIds||[]).forEach(p=>{{if(!r.has(p)){{r.add(p);q.push(p);}}}});}}
  return r;
}}
function toggleCollapse(e,id){{
  e.stopPropagation();
  collapsed.has(id)?collapsed.delete(id):collapsed.add(id);
  updateVis(); drawEdges(); saveState();
}}
function updateVis(){{
  const hidden=getHidden();
  Object.keys(NODES).forEach(id=>{{
    const el=document.getElementById("nw-"+id); if(!el)return;
    el.style.opacity=hidden.has(id)?"0":"1";
    el.style.pointerEvents=hidden.has(id)?"none":"";
  }});
  Object.keys(NODES).forEach(id=>{{
    const btn=document.getElementById("cb-"+id);
    if(btn) btn.textContent=collapsed.has(id)?"+":"-";
  }});
}}
function expandAll(){{collapsed.clear();updateVis();drawEdges();saveState();}}
function collapseAll(){{
  Object.keys(NODES).forEach(id=>{{if((NODES[id].children||[]).length)collapsed.add(id);}});
  updateVis();drawEdges();saveState();
}}

// ── Search ────────────────────────────────────────────────────────────
function doSearch(q){{
  document.querySelectorAll(".search-match").forEach(el=>el.classList.remove("search-match"));
  searchMatches=[];searchIdx=0;
  if(!q.trim()){{document.getElementById("search-info").textContent="";return;}}
  const lq=q.toLowerCase();
  Object.entries(NODES).forEach(([id,node])=>{{
    if(node.name.toLowerCase().includes(lq)||node.type.toLowerCase().includes(lq)||
       node.value.toLowerCase().includes(lq)||(node.nodeId||"").toLowerCase().includes(lq)){{
      searchMatches.push(id);
      const el=document.getElementById("nw-"+id)?.querySelector(".fn,.fn-group");
      if(el) el.classList.add("search-match");
    }}
  }});
  document.getElementById("search-info").textContent=searchMatches.length?`${{searchMatches.length}} found`:"0";
  if(searchMatches.length) panToNode(searchMatches[0]);
}}
function searchNext(){{if(!searchMatches.length)return;searchIdx=(searchIdx+1)%searchMatches.length;panToNode(searchMatches[searchIdx]);document.getElementById("search-info").textContent=`${{searchIdx+1}}/${{searchMatches.length}}`;}}
function searchPrev(){{if(!searchMatches.length)return;searchIdx=(searchIdx-1+searchMatches.length)%searchMatches.length;panToNode(searchMatches[searchIdx]);document.getElementById("search-info").textContent=`${{searchIdx+1}}/${{searchMatches.length}}`;}}

// ── Selection + path highlight ────────────────────────────────────────
function selectNode(id){{
  if(didDrag){{didDrag=false;return;}}
  clearNodeStyles();
  if(selId===id){{selId=null;closeDP();return;}}
  selId=id;
  const node=NODES[id]; if(!node)return;
  const anc=getAnc(id),desc=getDesc(id);
  Object.keys(NODES).forEach(nid=>{{
    const el=document.getElementById("nw-"+nid)?.querySelector(".fn,.fn-group");
    if(!el)return;
    if(nid===id)           el.style.boxShadow="0 0 0 3px #e94560,0 0 24px #e9456099";
    else if(anc.has(nid))  el.style.boxShadow="0 0 0 2px #4fc3f7,0 0 14px #4fc3f755";
    else if(desc.has(nid)) el.style.boxShadow="0 0 0 2px #ff8c42,0 0 14px #ff8c4255";
    else el.classList.add("dimmed");
  }});
  drawEdges(new Set([...anc,...desc,id]));
  const p=document.getElementById("dp");
  p.style.display="block"; p.style.borderTopColor=node.color;
  document.getElementById("dp-title").innerHTML=
    `<span style="color:${{node.color}}">${{node.name}}</span>`+
    (node.shared?' <span style="background:#f5c518;color:#111;font-size:8px;padding:1px 5px;border-radius:5px;font-weight:700;">SHARED</span>':'');
  document.getElementById("dp-type").style.color=node.color;
  document.getElementById("dp-type").textContent=node.isGroup?"GROUP":node.type;
  document.getElementById("dp-gate").style.color=GCOLORS[node.gate]||"#aaa";
  document.getElementById("dp-gate").textContent=node.gate;
  document.getElementById("dp-value").style.color=node.color;
  document.getElementById("dp-value").textContent=node.value;
  document.getElementById("dp-nid").textContent=node.nodeId||id;
  document.getElementById("dp-par").textContent=node.parents.join(" · ")||"(top event)";
  document.getElementById("dp-chi").textContent=node.childNames.join(" · ")||"(leaf node)";
}}
function clearNodeStyles(){{document.querySelectorAll(".fn,.fn-group").forEach(el=>{{el.style.boxShadow="";el.classList.remove("dimmed");}});}}
function clearHL(){{selId=null;clearNodeStyles();drawEdges();closeDP();}}
function closeDP(){{document.getElementById("dp").style.display="none";}}

// ── SVG edges ─────────────────────────────────────────────────────────
function drawEdges(hl){{
  const hidden=getHidden();
  let s="";
  // Primary edges
  EDGES.forEach(([pid,cid])=>{{
    if(hidden.has(cid)||hidden.has(pid))return;
    const pp=pos[pid],cp=pos[cid]; if(!pp||!cp)return;
    const x1=pp.x+nodeW(pid)/2,y1=pp.y+nodeH(pid),x2=cp.x+nodeW(cid)/2,y2=cp.y,my=(y1+y2)/2;
    const isHl=hl&&(hl.has(pid)||hl.has(cid));
    const isAnd=NODES[pid]?.gate==="AND";
    s+=`<path d="M${{x1}},${{y1}} C${{x1}},${{my}} ${{x2}},${{my}} ${{x2}},${{y2}}"
      fill="none" stroke="${{isHl?"#4fc3f7":"#2a2a2a"}}" stroke-width="${{isHl?2.5:1.5}}"
      ${{isAnd?'stroke-dasharray="6,3"':''}} opacity="${{isHl?1:0.9}}"/>`;
  }});
  // Shared edges — drawn faint/dashed in background to show the connection
  SHARED_EDGES.forEach(([pid,cid])=>{{
    if(hidden.has(cid)||hidden.has(pid))return;
    const pp=pos[pid],cp=pos[cid]; if(!pp||!cp)return;
    const x1=pp.x+nodeW(pid)/2,y1=pp.y+nodeH(pid),x2=cp.x+nodeW(cid)/2,y2=cp.y,my=(y1+y2)/2;
    const isHl=hl&&(hl.has(pid)||hl.has(cid));
    s+=`<path d="M${{x1}},${{y1}} C${{x1}},${{my}} ${{x2}},${{my}} ${{x2}},${{y2}}"
      fill="none" stroke="${{isHl?"#f5c518":"#f5c51833"}}" stroke-width="${{isHl?2:1}}"
      stroke-dasharray="3,5" opacity="${{isHl?0.9:0.4}}"/>`;
  }});
  const allX=Object.keys(pos).map(id=>pos[id].x+nodeW(id)+80);
  const allY=Object.keys(pos).map(id=>pos[id].y+nodeH(id)+80);
  const maxX=Math.max(...allX,800),maxY=Math.max(...allY,600);
  svg.setAttribute("width",maxX);svg.setAttribute("height",maxY);
  svg.style.width=maxX+"px";svg.style.height=maxY+"px";
  svg.innerHTML=s;
}}

// ── Render nodes ──────────────────────────────────────────────────────
function renderNodes(){{
  Object.entries(NODES).forEach(([id,node])=>{{
    const p=pos[id]||{{x:200,y:200}};
    const gc=GCOLORS[node.gate]||"#aaa";
    const hasCh=(node.children||[]).length>0;
    const sb=node.shared?`<span style="background:#f5c518;color:#111;font-size:6px;padding:1px 3px;border-radius:3px;font-weight:700;margin-left:3px;">SHR</span>`:"";
    const nid_badge=node.nodeId&&node.nodeId!==id?`<span style="font-size:6px;color:rgba(255,255,255,0.4);margin-left:2px;">${{node.nodeId}}</span>`:"";
    const nw=nodeW(id),nh=nodeH(id);
    const wrap=document.createElement("div");
    wrap.id="nw-"+id; wrap.className="nw";
    wrap.style.cssText=`left:${{p.x}}px;top:${{p.y}}px;width:${{nw}}px;`;
    wrap.onmousedown=e=>{{
      if(e.button!==0)return;
      e.stopPropagation(); didDrag=false;
      const cp=pos[id];
      dragId=id; dragOff={{x:e.clientX/scale-tx/scale-cp.x,y:e.clientY/scale-ty/scale-cp.y}};
      document.body.style.userSelect="none";
    }};
    const colBtn=hasCh?`<div style="display:flex;align-items:center;gap:4px;margin-top:2px;">
      <div class="gt" style="color:${{gc}};border-color:${{gc}};">${{node.gate}}</div>
      <button class="cb" id="cb-${{id}}" onclick="toggleCollapse(event,'${{id}}')">${{collapsed.has(id)?"+":"-"}}</button>
    </div>`:"";
    if(node.isGroup){{
      wrap.innerHTML=`
        <div class="fn-group" style="background:${{node.color}};color:${{node.tcolor}};border-color:${{node.color}};width:${{nw}}px;height:${{nh}}px;display:flex;flex-direction:column;align-items:center;justify-content:center;">
          <div style="font-size:7px;letter-spacing:1px;opacity:0.7;margin-bottom:1px;">COMBINED</div>
          <div style="font-size:9px;font-weight:700;text-align:center;word-break:break-word;line-height:1.2;">${{node.name}}</div>
          <div style="font-size:9px;font-weight:700;font-family:monospace;margin-top:2px;background:rgba(0,0,0,0.25);border-radius:3px;padding:1px 4px;">${{node.value}}</div>
        </div>${{colBtn}}`;
    }}else{{
      wrap.innerHTML=`
        <div class="fn" style="background:${{node.color}};color:${{node.tcolor}};border-color:${{node.color}};width:100%;">
          <div style="font-size:7px;opacity:0.75;letter-spacing:1px;margin-bottom:2px;display:flex;align-items:center;justify-content:center;">
            ${{node.type}}${{sb}}${{nid_badge}}
          </div>
          <div style="font-size:10px;font-weight:700;text-align:center;word-break:break-word;margin-bottom:4px;line-height:1.3;">${{node.name}}</div>
          <div style="background:rgba(0,0,0,0.26);border-radius:3px;padding:2px 5px;font-size:11px;font-weight:700;text-align:center;font-family:monospace;">${{node.value}}</div>
        </div>${{colBtn}}`;
    }}
    canvas.appendChild(wrap);
  }});
}}

function getHidden(){{
  const hidden=new Set(); collapsed.forEach(cid=>getDesc(cid).forEach(d=>hidden.add(d)));
  return hidden;
}}

buildLayout();
renderNodes();
// On load: collapse everything except the focus hazard's direct subtree
(function(){{
  // Find all hazard IDs
  const hazardIds = LEVELS.HAZARD || [];
  if(FOCUS_ID) {{
    // Collapse all hazards that are NOT the focus
    hazardIds.forEach(hid => {{
      if(hid !== FOCUS_ID) collapsed.add(hid);
    }});
    // Also collapse focus hazard's children-of-children to keep it tidy
    // (user can expand as needed)
    const directKids = EDGES.filter(e=>e[0]===FOCUS_ID).map(e=>e[1]);
    directKids.forEach(kid => {{
      // collapse grandchildren level
      const grandkids = EDGES.filter(e=>e[0]===kid).map(e=>e[1]);
      if(grandkids.length > 0) collapsed.add(kid);
    }});
  }} else if(hazardIds.length > 1) {{
    // Full tree: collapse all except first hazard's first level
    hazardIds.slice(1).forEach(hid => collapsed.add(hid));
  }}
  // Apply any saved collapsed overrides from Python
  {init_collapsed_js}.forEach(id => collapsed.add(id));
  updateVis();
}})();
// Apply saved transform if any
if({init_scale}!==1||{init_tx}!==0||{init_ty}!==0){{
  applyT();
}}else{{
  // Auto-centre
  const vr=vp.getBoundingClientRect();
  const visIds=Object.keys(pos).filter(id=>!{{...getHidden()}}.has(id));
  const allX=visIds.map(id=>pos[id].x+nodeW(id));
  const allY=visIds.map(id=>pos[id].y);
  if(allX.length){{
    const minX=Math.min(...visIds.map(id=>pos[id].x));
    const maxX=Math.max(...allX);
    const treeW=maxX-minX;
    tx=Math.max(20,(vr.width-treeW)/2)-minX; ty=20;
  }}
  applyT();
}}
// Pan to focus node after layout
if(FOCUS_ID&&pos[FOCUS_ID]){{ setTimeout(()=>panToNode(FOCUS_ID),80); }}
drawEdges();
</script></body></html>"""

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
        "nodes_since_calc": 0,   # how many nodes added without recalculating
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
      FAULT TREE ANALYSIS · TOP-DOWN DISTRIBUTION · {len(nodes)} nodes · {len(hazards)} hazard(s)
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
                        nid = cid_clean if cid_clean and not any(n["id"]==cid_clean for n in nodes) else str(uuid.uuid4())[:7]
                        new_node = {"id": nid, "nodeId": cid_clean or nid,
                                    "name": node_name.strip(), "type": node_type, "gate": gate,
                                    "targetValue": None, "calculatedValue": None, "parentIds": sel_pids}
                        # Keep tree focused on the parent being worked on
                        st.session_state.tree_state["focus_id"] = sel_pids[0] if sel_pids else nid
                        st.session_state.nodes_since_calc += 1
                        set_nodes(nodes + [new_node])  # NO recalculate — user presses CALCULATE
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

                    c1, c2 = st.columns(2)
                    with c1:
                        if st.button("💾 APPLY", use_container_width=True, type="primary"):
                            upd = []
                            for n in nodes:
                                if n["id"] == eid:
                                    n = dict(n)
                                    n["name"]   = new_name.strip() or n["name"]
                                    n["gate"]   = new_gate
                                    n["nodeId"] = new_node_id.strip() or n.get("nodeId", n["id"])
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
                            set_nodes(upd)  # no auto-recalc
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
            "Scroll=zoom · Right-drag=pan · Left-drag=move · Click=inspect · ▼/+= collapse/expand"
            "</div>", unsafe_allow_html=True)
        tree_html = build_html_tree(nodes, filter_hazard_id=fid, tree_state=ts)
        components.html(tree_html, height=700, scrolling=False)
        # Clear focus_id after render so next rerun doesn't keep jumping
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
                pnames = " · ".join(by_id[p]["name"] for p in (node.get("parentIds") or []) if p in by_id) or "—"
                cnames = " · ".join(n["name"] for n in nodes if node["id"] in (n.get("parentIds") or [])) or "—"
                is_shared = len(node.get("parentIds") or []) > 1
                gc = "#4fc3f7" if node["gate"] == "OR" else "#ffb74d"
                st.markdown(f"""
                <div style="background:#141414;border:1px solid #222;border-radius:5px;padding:7px 11px;margin-bottom:3px;
                            display:grid;grid-template-columns:2fr 1fr 1fr 2fr 2fr;gap:8px;align-items:center;">
                  <div>
                    <div style="font-weight:700;font-size:11px;color:#ddd;">{node['name']}</div>
                    {'<div style="font-size:8px;color:#f5c518;">◈ SHARED</div>' if is_shared else ''}
                  </div>
                  <div style="font-size:9px;color:{color};font-weight:700;">{node['type']}</div>
                  <div style="font-size:9px;color:{gc};font-weight:700;">{node['gate']}</div>
                  <div style="font-size:10px;color:{color};font-weight:700;font-family:monospace;">{fmt(node.get('calculatedValue'))}</div>
                  <div style="font-size:9px;color:#555;">↑ {pnames}<br>↓ {cnames}</div>
                </div>""", unsafe_allow_html=True)

        st.markdown("---")
        cols = st.columns(5)
        counts = [(lvl, len(show_by_level[lvl])) for lvl in DISPLAY_ORDER] + [("TOTAL", len(show))]
        for i,(lvl,cnt) in enumerate(counts):
            with cols[i%5]:
                c = LEVEL_COLORS.get(lvl,"#e94560")
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
                    st.markdown(f"""
                    <div style="background:#141414;border:2px solid {color}55;{shape_style}
                                padding:9px 14px;margin-bottom:5px;
                                display:grid;grid-template-columns:2.5fr 0.8fr 0.8fr 1.5fr 2.5fr;gap:10px;align-items:center;">
                      <div>
                        <div style="font-weight:700;font-size:11px;color:#ddd;">{display_name}</div>
                        <div style="font-size:8px;color:#555;margin-top:1px;">id: {node['id']}</div>
                        {'<div style="font-size:8px;color:#f5c518;">◈ SHARED</div>' if is_shared else ''}
                        {'<div style="font-size:8px;color:#7e57c2;">◉ GROUP (Combined Faults)</div>' if is_group else ''}
                      </div>
                      <div style="font-size:9px;color:{color};font-weight:700;">{node['type']}</div>
                      <div style="font-size:9px;color:{gc};font-weight:700;">{node['gate']}</div>
                      <div style="font-size:10px;color:{color};font-weight:700;font-family:monospace;">{val}</div>
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
