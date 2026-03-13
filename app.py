import streamlit as st
import json
import math
import os
import requests
import uuid
import io
from datetime import datetime
import streamlit.components.v1 as components

# ── Page config ───────────────────────────────────────────────────────────
st.set_page_config(
    page_title="FTA Reverse Engineer",
    page_icon="⚠️",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ── Constants ─────────────────────────────────────────────────────────────
LEVEL_ORDER        = ["HAZARD", "SF", "FF", "IF"]
LEVEL_COLORS       = {"HAZARD": "#ff4d4d", "SF": "#ff8c42", "FF": "#f5c518", "IF": "#4caf7d"}
LEVEL_TEXT         = {"HAZARD": "#fff",    "SF": "#fff",    "FF": "#111",    "IF": "#fff"}
VALID_PARENT_TYPES = ["HAZARD", "SF", "FF"]
VALID_CHILD_TYPES  = ["SF", "FF", "IF"]

# ── Gist helpers ──────────────────────────────────────────────────────────
def gist_headers(token):
    return {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}

def get_gist(token, gist_id):
    try:
        r = requests.get(f"https://api.github.com/gists/{gist_id}",
                         headers=gist_headers(token), timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None

def list_save_files(token, gist_id):
    gist = get_gist(token, gist_id)
    if not gist:
        return []
    return sorted(gist.get("files", {}).keys())

def load_file_from_gist(token, gist_id, filename):
    gist = get_gist(token, gist_id)
    if not gist:
        return []
    content = gist.get("files", {}).get(filename, {}).get("content", "[]")
    try:
        return json.loads(content)
    except Exception:
        return []

def save_file_to_gist(token, gist_id, filename, nodes):
    try:
        r = requests.patch(
            f"https://api.github.com/gists/{gist_id}",
            headers=gist_headers(token),
            json={"files": {filename: {"content": json.dumps(nodes, indent=2)}}},
            timeout=10
        )
        return r.status_code == 200
    except Exception:
        return False

def delete_file_from_gist(token, gist_id, filename):
    try:
        r = requests.patch(
            f"https://api.github.com/gists/{gist_id}",
            headers=gist_headers(token),
            json={"files": {filename: None}},
            timeout=10
        )
        return r.status_code == 200
    except Exception:
        return False

# ── Calculation ───────────────────────────────────────────────────────────
def reverse_distribute(parent_val, gate, n_children):
    if n_children == 0:
        return parent_val
    if gate == "OR":
        return parent_val / n_children
    else:
        return parent_val ** (1.0 / n_children)

def recalculate(nodes):
    updated = [dict(n) for n in nodes]
    for n in updated:
        if n["type"] != "HAZARD":
            n["calculatedValue"] = None
    queue   = [n for n in updated if n["type"] == "HAZARD"]
    visited = set()
    while queue:
        parent = queue.pop(0)
        if parent["id"] in visited:
            continue
        visited.add(parent["id"])
        children = [n for n in updated if parent["id"] in (n.get("parentIds") or [])]
        if not children:
            continue
        parent_val = (
            parent.get("targetValue") or 1e-7
            if parent["type"] == "HAZARD"
            else (parent.get("calculatedValue") or parent.get("targetValue") or 1e-7)
        )
        child_val = reverse_distribute(parent_val, parent["gate"], len(children))
        for child in children:
            existing = child.get("calculatedValue")
            # Shared nodes: use MAX (worst/highest failure rate wins)
            # Higher failure rate = worse constraint = more conservative
            child["calculatedValue"] = child_val if existing is None else max(existing, child_val)
            queue.append(child)
    return updated

def fmt(v):
    """Safe format for Excel/JSON - ASCII only."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "-"
    return f"{v:.2e}"

def fmtd(v):
    """Display format for HTML - uses em-dash."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "&mdash;"
    return f"{v:.2e}"

def now_str():
    return datetime.now().strftime("%Y-%m-%d_%H-%M")

def is_snapshot(name):
    return name.startswith("snapshot_")

def is_active_file(name):
    return not is_snapshot(name)

# ── Export helpers ────────────────────────────────────────────────────────
def export_json(nodes):
    return json.dumps(nodes, indent=2).encode("utf-8")

def sanitize_xl(val):
    """Make any value safe for openpyxl cell writes.
    Uses openpyxl's own ILLEGAL_CHARACTERS_RE to strip bad chars,
    then falls back to ASCII-only encoding if anything remains problematic.
    Numbers are passed through unchanged.
    """
    if val is None:
        return "-"
    if isinstance(val, (int, float)):
        return val  # numbers always safe
    s = str(val)
    try:
        # Use openpyxl's own checker to strip illegal chars
        from openpyxl.utils.exceptions import IllegalCharacterError
        from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
        s = ILLEGAL_CHARACTERS_RE.sub('', s)
    except ImportError:
        pass
    # Final fallback: encode to ascii, replacing anything non-ascii
    try:
        s.encode('utf-8')
    except Exception:
        s = s.encode('ascii', 'replace').decode('ascii')
    return s or "-"

def export_excel(nodes):
    try:
        import openpyxl
        from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        return None

    by_id = {n["id"]: n for n in nodes}
    wb    = openpyxl.Workbook()

    # ── Sheet 1: Full node list ──
    ws1 = wb.active
    ws1.title = "FTA Nodes"

    headers = ["Level", "Type", "Node Name", "Gate", "Calculated Value",
               "Parent Nodes", "Child Nodes", "Shared Node"]
    col_fills = {
        "HAZARD": "FFFF4D4D", "SF": "FFFF8C42",
        "FF":     "FFF5C518", "IF": "FF4CAF7D"
    }

    # Header row
    for ci, h in enumerate(headers, 1):
        cell = ws1.cell(row=1, column=ci, value=h)
        cell.font      = Font(bold=True, color="FFFFFFFF", name="Courier New")
        cell.fill      = PatternFill("solid", fgColor="FF0F3460")
        cell.alignment = Alignment(horizontal="center", vertical="center")

    ws1.row_dimensions[1].height = 22

    # Data rows — ordered by level
    row = 2
    for level in LEVEL_ORDER:
        for node in [n for n in nodes if n["type"] == level]:
            parent_names = " | ".join(
                by_id[p]["name"] for p in (node.get("parentIds") or []) if p in by_id
            )
            child_names = " | ".join(
                n["name"] for n in nodes if node["id"] in (n.get("parentIds") or [])
            )
            is_shared = len(node.get("parentIds") or []) > 1
            values = [
                LEVEL_ORDER.index(level) + 1,
                sanitize_xl(node["type"]),
                sanitize_xl(node["name"]),
                sanitize_xl(node["gate"]),
                node.get("calculatedValue"),   # float — safe
                sanitize_xl(parent_names or "-"),
                sanitize_xl(child_names  or "-"),
                "YES" if is_shared else "NO",
            ]
            fill_hex = col_fills.get(level, "FF333333")
            for ci, val in enumerate(values, 1):
                cell           = ws1.cell(row=row, column=ci, value=sanitize_xl(val) if isinstance(val, str) else val)
                cell.font      = Font(name="Courier New", size=10,
                                      color="FF111111" if level == "FF" else "FFFFFFFF")
                cell.fill      = PatternFill("solid", fgColor=fill_hex)
                cell.alignment = Alignment(horizontal="center" if ci != 3 else "left",
                                           vertical="center")
            row += 1

    # Column widths
    for ci, w in enumerate([8, 10, 28, 8, 18, 30, 30, 12], 1):
        ws1.column_dimensions[get_column_letter(ci)].width = w

    # ── Sheet 2: Hierarchy view ──
    ws2 = wb.create_sheet("Hierarchy")
    ws2.sheet_view.showGridLines = False

    ws2.cell(row=1, column=1, value="FTA HIERARCHY - TOP TO BOTTOM").font = Font(
        bold=True, size=14, name="Courier New", color="FFE94560")
    ws2.cell(row=2, column=1, value=f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}").font = Font(
        size=9, name="Courier New", color="FF888888")

    row = 4
    thin = Border(
        left=Side(style="thin", color="FF333333"),
        bottom=Side(style="thin", color="FF333333")
    )

    def write_hierarchy(node_id, depth, visited_ids):
        nonlocal row
        if node_id in visited_ids:
            return
        visited_ids.add(node_id)
        node = by_id.get(node_id)
        if not node:
            return

        indent     = "    " * depth
        val_str    = fmt(node.get("calculatedValue"))
        is_shared  = len(node.get("parentIds") or []) > 1
        label      = f"{indent}{'  -> ' if depth > 0 else ''}{node['name']}"
        gate_label = f"[{node['gate']}]" if depth < 3 else ""
        shared_tag = " [SHARED]" if is_shared else ""

        c1 = ws2.cell(row=row, column=1, value=sanitize_xl(label))
        c2 = ws2.cell(row=row, column=2, value=sanitize_xl(node["type"] + gate_label))
        c3 = ws2.cell(row=row, column=3, value=sanitize_xl(val_str))
        c4 = ws2.cell(row=row, column=4, value=sanitize_xl(shared_tag))

        fill_hex = col_fills.get(node["type"], "FF222222")
        for c in [c1, c2, c3, c4]:
            c.font      = Font(name="Courier New", size=10,
                               bold=(depth == 0),
                               color="FF111111" if node["type"] == "FF" else "FFFFFFFF")
            c.fill      = PatternFill("solid", fgColor=fill_hex)
            c.alignment = Alignment(vertical="center")
        c3.alignment = Alignment(horizontal="right", vertical="center")
        row += 1

        children = [n for n in nodes if node_id in (n.get("parentIds") or [])]
        for child in children:
            write_hierarchy(child["id"], depth + 1, visited_ids)

    hazard_nodes = [n for n in nodes if n["type"] == "HAZARD"]
    for h in hazard_nodes:
        write_hierarchy(h["id"], 0, set())

    ws2.column_dimensions["A"].width = 50
    ws2.column_dimensions["B"].width = 14
    ws2.column_dimensions["C"].width = 16
    ws2.column_dimensions["D"].width = 14

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.getvalue()

# ── HTML Tree builder ─────────────────────────────────────────────────────
def build_html_tree(nodes, selected_id=None):
    """Interactive tree: zoom, pan, collapse/expand, drag nodes, path highlight."""
    if not nodes:
        return ""
    by_id   = {n["id"]: n for n in nodes}
    hazards = [n for n in nodes if n["type"] == "HAZARD"]
    if not hazards:
        return ""

    nodes_js = __import__('json').dumps({
        n["id"]: {
            "name":     n["name"],
            "type":     n["type"],
            "gate":     n["gate"],
            "value":    fmt(n.get("calculatedValue")),
            "parents":  [by_id[p]["name"] for p in (n.get("parentIds") or []) if p in by_id],
            "parentIds":[p for p in (n.get("parentIds") or []) if p in by_id],
            "children": [c["id"] for c in nodes if n["id"] in (c.get("parentIds") or [])],
            "childNames":[c["name"] for c in nodes if n["id"] in (c.get("parentIds") or [])],
            "shared":   len(n.get("parentIds") or []) > 1,
            "color":    LEVEL_COLORS.get(n["type"], "#888"),
            "tcolor":   LEVEL_TEXT.get(n["type"], "#fff"),
        }
        for n in nodes
    })

    # Build adjacency for JS: list of [parentId, childId] edges
    edges_js = __import__('json').dumps([
        [pid, n["id"]]
        for n in nodes
        for pid in (n.get("parentIds") or [])
        if pid in by_id
    ])

    # Build level groups for layout
    level_groups_js = __import__('json').dumps({
        lvl: [n["id"] for n in nodes if n["type"] == lvl]
        for lvl in ["HAZARD","SF","FF","IF"]
    })

    init_sel = f'selectNode("{selected_id}");' if selected_id else ""

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{
  background:#0a0a0a;
  font-family:'JetBrains Mono','Fira Code',monospace;
  color:#e0e0e0;
  overflow:hidden;
  height:100vh;
  display:flex;flex-direction:column;
}}
#toolbar{{
  display:flex;align-items:center;gap:6px;flex-wrap:wrap;
  padding:5px 10px;background:#111;
  border-bottom:1px solid #1e1e1e;
  flex-shrink:0;font-size:10px;color:#555;
  user-select:none;
}}
.tb-btn{{
  background:#1a1a1a;border:1px solid #2a2a2a;color:#aaa;
  border-radius:4px;padding:3px 9px;cursor:pointer;
  font-family:inherit;font-size:10px;letter-spacing:0.5px;
  transition:background 0.1s,color 0.1s;white-space:nowrap;
}}
.tb-btn:hover{{background:#252525;color:#fff;border-color:#444;}}
.tb-sep{{color:#2a2a2a;}}
#zoom-label{{color:#555;font-size:10px;min-width:38px;text-align:center;}}
#hint{{color:#383838;font-size:9px;letter-spacing:0.5px;margin-left:4px;}}
#viewport{{
  flex:1;overflow:hidden;position:relative;
  cursor:default;
}}
#canvas{{
  position:absolute;top:0;left:0;
  transform-origin:0 0;
  will-change:transform;
}}
svg#edges{{
  position:absolute;top:0;left:0;
  pointer-events:none;
  overflow:visible;
}}
.node-wrap{{
  position:absolute;
  display:flex;flex-direction:column;align-items:center;
  cursor:grab;
  transition:opacity 0.2s;
}}
.node-wrap.collapsed-hidden{{opacity:0;pointer-events:none;}}
.fta-node{{
  border-radius:8px;padding:8px 12px;
  min-width:120px;max-width:155px;
  user-select:none;
  border:2px solid transparent;
  transition:filter 0.12s,box-shadow 0.12s,transform 0.12s;
  position:relative;
}}
.fta-node:hover{{filter:brightness(1.18);}}
.fta-node.dimmed{{opacity:0.25;}}
.fta-node.highlighted{{}}
.collapse-btn{{
  width:20px;height:20px;border-radius:50%;
  border:1px solid #333;background:#1a1a1a;
  color:#888;font-size:10px;cursor:pointer;
  display:flex;align-items:center;justify-content:center;
  margin-top:4px;transition:background 0.1s,color 0.1s;
  flex-shrink:0;
}}
.collapse-btn:hover{{background:#252525;color:#fff;border-color:#555;}}
.gate-tag{{
  font-size:8px;font-weight:700;font-family:monospace;
  padding:1px 6px;border-radius:3px;margin-top:3px;
  border:1px solid;letter-spacing:1px;
}}
#detail-panel{{
  position:fixed;bottom:0;left:0;right:0;
  background:#141414f0;border-top:2px solid #333;
  padding:8px 14px 10px;display:none;
  backdrop-filter:blur(10px);z-index:200;
}}
.dg{{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin:6px 0 5px;}}
.dc{{background:#0a0a0a;border-radius:5px;padding:5px;text-align:center;}}
.dcl{{font-size:7px;color:#555;letter-spacing:2px;margin-bottom:2px;}}
.dcv{{font-size:11px;font-weight:700;}}
.dr{{display:grid;grid-template-columns:1fr 1fr;gap:6px;}}
.ds{{background:#0a0a0a;border-radius:5px;padding:5px;}}
.dsl{{font-size:7px;color:#555;letter-spacing:2px;margin-bottom:2px;}}
.dsv{{font-size:10px;color:#ccc;line-height:1.4;}}
#close-dp{{
  position:absolute;top:6px;right:10px;
  background:none;border:none;color:#555;font-size:15px;
  cursor:pointer;font-family:inherit;
}}
#close-dp:hover{{color:#fff;}}
</style>
</head>
<body>

<div id="toolbar">
  <button class="tb-btn" onclick="zoomBy(0.15)">＋</button>
  <button class="tb-btn" onclick="zoomBy(-0.15)">－</button>
  <span id="zoom-label">100%</span>
  <span class="tb-sep">|</span>
  <button class="tb-btn" onclick="resetView()">⌂ Reset</button>
  <button class="tb-btn" onclick="expandAll()">⊞ Expand All</button>
  <button class="tb-btn" onclick="collapseAll()">⊟ Collapse All</button>
  <button class="tb-btn" onclick="clearHighlight()">✕ Clear Highlight</button>
  <span class="tb-sep">|</span>
  <span id="hint">Scroll=zoom &nbsp; Right-drag=pan &nbsp; Left-drag=move node &nbsp; Click=inspect &nbsp; ▼ button=collapse</span>
</div>

<div id="viewport">
  <div id="canvas">
    <svg id="edges"></svg>
  </div>
</div>

<div id="detail-panel">
  <button id="close-dp" onclick="closeDetail()">✕</button>
  <div style="font-size:8px;color:#888;letter-spacing:3px;margin-bottom:3px;">SELECTED NODE</div>
  <div id="dp-title" style="font-size:14px;font-weight:700;margin-bottom:3px;"></div>
  <div class="dg">
    <div class="dc"><div class="dcl">TYPE</div><div class="dcv" id="dp-type"></div></div>
    <div class="dc"><div class="dcl">GATE</div><div class="dcv" id="dp-gate"></div></div>
    <div class="dc"><div class="dcl">VALUE</div><div class="dcv" id="dp-value"></div></div>
    <div class="dc"><div class="dcl">SHARED</div><div class="dcv" id="dp-shared"></div></div>
  </div>
  <div class="dr">
    <div class="ds"><div class="dsl">PARENTS</div><div class="dsv" id="dp-parents"></div></div>
    <div class="ds"><div class="dsl">CHILDREN</div><div class="dsv" id="dp-children"></div></div>
  </div>
</div>

<script>
// ── data ──────────────────────────────────────────────────────────────
const NODES   = {nodes_js};
const EDGES   = {edges_js};
const LEVELS  = {level_groups_js};
const COLORS  = {{HAZARD:"#ff4d4d",SF:"#ff8c42",FF:"#f5c518",IF:"#4caf7d"}};
const TCOLORS = {{HAZARD:"#fff",SF:"#fff",FF:"#111",IF:"#fff"}};
const GATE_COLORS = {{OR:"#4fc3f7",AND:"#ffb74d"}};
const LEVEL_Y = {{HAZARD:40, SF:200, FF:360, IF:520}};
const NODE_W  = 145, NODE_H = 80, H_GAP = 30, V_GAP = 120;

// ── state ──────────────────────────────────────────────────────────────
let scale     = 1, tx = 0, ty = 0;
const MIN_S   = 0.12, MAX_S = 3.5;
let positions = {{}};      // id -> {{x,y}}
let collapsed = new Set(); // collapsed node ids
let selectedId= null;

// ── build initial layout ───────────────────────────────────────────────
function buildLayout() {{
  const order = ["HAZARD","SF","FF","IF"];
  const ys    = {{HAZARD:40, SF:220, FF:400, IF:580}};
  order.forEach(lvl => {{
    const ids = LEVELS[lvl] || [];
    const total = ids.length;
    ids.forEach((id,i) => {{
      positions[id] = {{
        x: total === 1 ? 400 : 60 + i*(NODE_W+H_GAP),
        y: ys[lvl]
      }};
    }});
  }});
  // centre HAZARD over its children
  const hid = (LEVELS.HAZARD||[])[0];
  if (hid) {{
    const kids = EDGES.filter(e=>e[0]===hid).map(e=>e[1]);
    if (kids.length) {{
      const xs = kids.map(k=>positions[k]?.x||400);
      positions[hid].x = xs.reduce((a,b)=>a+b,0)/xs.length;
    }}
  }}
}}

// ── viewport transform ─────────────────────────────────────────────────
const canvas   = document.getElementById('canvas');
const viewport = document.getElementById('viewport');

function applyT() {{
  canvas.style.transform = `translate(${{tx}}px,${{ty}}px) scale(${{scale}})`;
  document.getElementById('zoom-label').textContent = Math.round(scale*100)+'%';
  drawEdges();
}}

function zoomBy(d, cx, cy) {{
  const vr  = viewport.getBoundingClientRect();
  cx = cx ?? vr.width/2; cy = cy ?? vr.height/2;
  const ns  = Math.min(MAX_S, Math.max(MIN_S, scale+d));
  const r   = ns/scale;
  tx = cx - r*(cx-tx); ty = cy - r*(cy-ty);
  scale = ns; applyT();
}}

function resetView() {{
  scale=1; tx=60; ty=20; applyT();
}}

viewport.addEventListener('wheel', e => {{
  e.preventDefault();
  zoomBy(e.deltaY<0?0.1:-0.1, e.clientX, e.clientY);
}}, {{passive:false}});

// ── right-click pan ────────────────────────────────────────────────────
let panning=false, panStart={{x:0,y:0}};
viewport.addEventListener('mousedown', e => {{
  if (e.button===2) {{
    panning=true; panStart={{x:e.clientX-tx, y:e.clientY-ty}};
    viewport.style.cursor='grabbing'; e.preventDefault();
  }}
}});
window.addEventListener('mousemove', e => {{
  if (!panning) return;
  tx=e.clientX-panStart.x; ty=e.clientY-panStart.y; applyT();
}});
window.addEventListener('mouseup', e => {{
  if (e.button===2) {{ panning=false; viewport.style.cursor='default'; }}
}});
viewport.addEventListener('contextmenu', e=>e.preventDefault());

// ── node drag ─────────────────────────────────────────────────────────
let draggingId=null, dragOff={{x:0,y:0}}, didDrag=false;

function onNodeMouseDown(e, id) {{
  if (e.button!==0) return;
  e.stopPropagation();
  draggingId=id; didDrag=false;
  const pos = positions[id];
  dragOff = {{
    x: e.clientX/scale - tx/scale - pos.x,
    y: e.clientY/scale - ty/scale - pos.y
  }};
  document.body.style.userSelect='none';
}}
window.addEventListener('mousemove', e => {{
  if (!draggingId) return;
  didDrag=true;
  const nx = e.clientX/scale - tx/scale - dragOff.x;
  const ny = e.clientY/scale - ty/scale - dragOff.y;
  positions[draggingId] = {{x:nx, y:ny}};
  const el = document.getElementById('node-'+draggingId);
  if (el) {{ el.style.left=nx+'px'; el.style.top=ny+'px'; }}
  drawEdges();
}});
window.addEventListener('mouseup', e => {{
  if (e.button===0 && draggingId) {{
    const id = draggingId;
    draggingId=null;
    document.body.style.userSelect='';
    if (!didDrag) selectNode(id);
  }}
}});

// ── collapse / expand ──────────────────────────────────────────────────
function getDescendants(id) {{
  const result=new Set();
  const queue=[id];
  while(queue.length) {{
    const cur=queue.shift();
    const kids=EDGES.filter(e=>e[0]===cur).map(e=>e[1]);
    kids.forEach(k=>{{ if(!result.has(k)){{ result.add(k); queue.push(k); }} }});
  }}
  return result;
}}

function toggleCollapse(e, id) {{
  e.stopPropagation();
  if (collapsed.has(id)) {{
    collapsed.delete(id);
  }} else {{
    collapsed.add(id);
  }}
  updateVisibility();
  drawEdges();
}}

function updateVisibility() {{
  const hidden=new Set();
  collapsed.forEach(cid => {{
    getDescendants(cid).forEach(d=>hidden.add(d));
  }});
  Object.keys(NODES).forEach(id => {{
    const el=document.getElementById('node-'+id);
    if (!el) return;
    if (hidden.has(id)) {{ el.style.opacity='0'; el.style.pointerEvents='none'; }}
    else {{ el.style.opacity='1'; el.style.pointerEvents=''; }}
  }});
  // update collapse button label
  collapsed.forEach(cid=>{{
    const btn=document.getElementById('cbtn-'+cid);
    if (btn) btn.textContent='＋';
  }});
  Object.keys(NODES).forEach(id=>{{
    if (!collapsed.has(id)) {{
      const btn=document.getElementById('cbtn-'+id);
      if (btn) btn.textContent='－';
    }}
  }});
}}

function expandAll()  {{ collapsed.clear(); updateVisibility(); drawEdges(); }}
function collapseAll(){{
  Object.keys(NODES).forEach(id=>{{
    if((NODES[id].children||[]).length>0) collapsed.add(id);
  }});
  updateVisibility(); drawEdges();
}}

// ── get all ancestors of a node ────────────────────────────────────────
function getAncestors(id) {{
  const result=new Set();
  const queue=[id];
  while(queue.length) {{
    const cur=queue.shift();
    const parents=(NODES[cur]?.parentIds||[]);
    parents.forEach(p=>{{ if(!result.has(p)){{ result.add(p); queue.push(p); }} }});
  }}
  return result;
}}

// ── selection + path highlighting ─────────────────────────────────────
function selectNode(id) {{
  if (didDrag) return;

  // clear old highlights
  document.querySelectorAll('.fta-node').forEach(el=>{{
    el.style.boxShadow=''; el.classList.remove('dimmed','highlighted');
  }});

  if (selectedId===id) {{ selectedId=null; closeDetail(); clearHighlight(); return; }}
  selectedId=id;

  const node=NODES[id];
  if (!node) return;

  // compute full ancestor path to root
  const ancestors  = getAncestors(id);
  const descendants= getDescendants(id);
  const connected  = new Set([...ancestors,...descendants,id]);

  // dim all, highlight path
  Object.keys(NODES).forEach(nid=>{{
    const el=document.getElementById('node-'+nid)?.querySelector('.fta-node');
    if (!el) return;
    if (nid===id) {{
      el.style.boxShadow='0 0 0 3px #e94560,0 0 24px #e9456088';
    }} else if (ancestors.has(nid)) {{
      el.style.boxShadow='0 0 0 2px #4fc3f7,0 0 14px #4fc3f744';  // blue = ancestors
    }} else if (descendants.has(nid)) {{
      el.style.boxShadow='0 0 0 2px #ff8c42,0 0 14px #ff8c4244';  // orange = descendants
    }} else {{
      el.classList.add('dimmed');
    }}
  }});

  // highlight edges in path
  drawEdges(connected);

  // detail panel
  const panel=document.getElementById('detail-panel');
  panel.style.display='block';
  panel.style.borderTopColor=node.color;

  document.getElementById('dp-title').innerHTML =
    `<span style="color:${{node.color}}">${{node.name}}</span>` +
    (node.shared?' <span style="background:#f5c518;color:#111;font-size:8px;padding:1px 5px;border-radius:5px;font-weight:700;">SHARED</span>':'');

  document.getElementById('dp-type').style.color  = node.color;
  document.getElementById('dp-type').textContent  = node.type;
  document.getElementById('dp-gate').style.color  = GATE_COLORS[node.gate]||'#aaa';
  document.getElementById('dp-gate').textContent  = node.gate;
  document.getElementById('dp-value').style.color = node.color;
  document.getElementById('dp-value').textContent = node.value;
  document.getElementById('dp-shared').textContent= node.shared?'YES':'NO';
  document.getElementById('dp-shared').style.color= node.shared?'#f5c518':'#555';
  document.getElementById('dp-parents').textContent  = node.parents.join(' · ')||'(top event)';
  document.getElementById('dp-children').textContent = node.childNames.join(' · ')||'(leaf node)';
}}

function clearHighlight() {{
  selectedId=null;
  document.querySelectorAll('.fta-node').forEach(el=>{{
    el.style.boxShadow=''; el.classList.remove('dimmed');
  }});
  drawEdges();
  closeDetail();
}}

function closeDetail() {{
  document.getElementById('detail-panel').style.display='none';
}}

// ── SVG edge drawing ───────────────────────────────────────────────────
const svgEl = document.getElementById('edges');

function drawEdges(highlighted) {{
  const hidden=new Set();
  collapsed.forEach(cid=>getDescendants(cid).forEach(d=>hidden.add(d)));

  let svgContent='';
  EDGES.forEach(([pid,cid])=>{{
    if (hidden.has(cid)||hidden.has(pid)) return;
    const pp=positions[pid], cp=positions[cid];
    if (!pp||!cp) return;
    const x1=pp.x+NODE_W/2, y1=pp.y+NODE_H;
    const x2=cp.x+NODE_W/2, y2=cp.y;
    const my=(y1+y2)/2;
    const isHl = highlighted && (highlighted.has(pid)||highlighted.has(cid));
    const color = isHl ? '#4fc3f7' : '#2a2a2a';
    const width = isHl ? 2 : 1.5;
    svgContent += `<path d="M${{x1}},${{y1}} C${{x1}},${{my}} ${{x2}},${{my}} ${{x2}},${{y2}}"
      fill="none" stroke="${{color}}" stroke-width="${{width}}" opacity="${{isHl?1:0.7}}"/>`;
  }});
  svgEl.innerHTML=svgContent;
  // size SVG to cover all nodes
  const allX=Object.values(positions).map(p=>p.x+NODE_W);
  const allY=Object.values(positions).map(p=>p.y+NODE_H+40);
  const maxX=Math.max(...allX,800), maxY=Math.max(...allY,600);
  svgEl.setAttribute('width',maxX); svgEl.setAttribute('height',maxY);
  svgEl.style.width=maxX+'px'; svgEl.style.height=maxY+'px';
}}

// ── render nodes ────────────────────────────────────────────────────────
function renderNodes() {{
  Object.entries(NODES).forEach(([id,node])=>{{
    const pos=positions[id]||{{x:200,y:200}};
    const color=node.color, tc=node.tcolor;
    const hasChildren=(node.children||[]).length>0;
    const gateColor=GATE_COLORS[node.gate]||'#aaa';
    const sharedBadge=node.shared
      ?`<span style="background:#f5c518;color:#111;font-size:6px;padding:1px 3px;border-radius:4px;font-weight:700;margin-left:3px;">SHARED</span>`:'';

    const wrap=document.createElement('div');
    wrap.id='node-'+id;
    wrap.className='node-wrap';
    wrap.style.cssText=`left:${{pos.x}}px;top:${{pos.y}}px;width:${{NODE_W}}px;`;
    wrap.onmousedown=e=>onNodeMouseDown(e,id);

    wrap.innerHTML=`
      <div class="fta-node" style="background:${{color}};color:${{tc}};border-color:${{color}};width:100%;">
        <div style="font-size:7px;opacity:0.75;letter-spacing:1px;margin-bottom:2px;display:flex;align-items:center;justify-content:center;">
          ${{node.type}}${{sharedBadge}}
        </div>
        <div style="font-size:10px;font-weight:700;text-align:center;word-break:break-word;margin-bottom:4px;line-height:1.3;">
          ${{node.name}}
        </div>
        <div style="background:rgba(0,0,0,0.25);border-radius:3px;padding:2px 5px;
                    font-size:11px;font-weight:700;text-align:center;font-family:monospace;">
          ${{node.value}}
        </div>
      </div>
      ${{hasChildren?`
        <div style="display:flex;align-items:center;gap:5px;margin-top:3px;">
          <div class="gate-tag" style="color:${{gateColor}};border-color:${{gateColor}};background:#111;">
            ${{node.gate}}
          </div>
          <button class="collapse-btn" id="cbtn-${{id}}" onclick="toggleCollapse(event,'${{id}}')">-</button>
        </div>`:'__EMPTY__'}}
    `;
    canvas.appendChild(wrap);
  }});
}}

// ── init ────────────────────────────────────────────────────────────────
buildLayout();
renderNodes();
drawEdges();

// centre view
const vr=viewport.getBoundingClientRect();
const allX2=Object.values(positions).map(p=>p.x+NODE_W);
const allY2=Object.values(positions).map(p=>p.y+NODE_H);
const treeW=Math.max(...allX2)-Math.min(...Object.values(positions).map(p=>p.x));
tx=Math.max(20,(vr.width-treeW*scale)/2); ty=20;
applyT();

{init_sel}
</script>
</body>
</html>"""
    return html

# ── Hierarchy text builder ────────────────────────────────────────────────
def build_hierarchy_rows(nodes):
    """Return flat list of rows for hierarchy panel, top→bottom with indentation."""
    by_id   = {n["id"]: n for n in nodes}
    rows    = []
    visited = set()

    def walk(node_id, depth):
        if node_id in visited:
            rows.append({"node": by_id[node_id], "depth": depth, "ref": True})
            return
        visited.add(node_id)
        node = by_id.get(node_id)
        if not node:
            return
        rows.append({"node": node, "depth": depth, "ref": False})
        for child in [n for n in nodes if node_id in (n.get("parentIds") or [])]:
            walk(child["id"], depth + 1)

    for h in [n for n in nodes if n["type"] == "HAZARD"]:
        walk(h["id"], 0)
    return rows


# ── Session state ─────────────────────────────────────────────────────────
defaults = {
    "nodes": [], "save_status": "idle", "save_msg": "",
    "gist_loaded": False, "active_file": "my_tree.json",
    "file_list": [], "selected_id": None,
}
for k, v in defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v

# ── Secrets ───────────────────────────────────────────────────────────────
def get_secret(key):
    try:
        return st.secrets[key]
    except Exception:
        return os.environ.get(key, "")

GITHUB_TOKEN = get_secret("GITHUB_TOKEN")
GIST_ID      = get_secret("GIST_ID")
configured   = bool(GITHUB_TOKEN and GIST_ID)

# ── Load on first run ─────────────────────────────────────────────────────
if configured and not st.session_state.gist_loaded:
    with st.spinner("Loading from Gist..."):
        st.session_state.file_list = list_save_files(GITHUB_TOKEN, GIST_ID)
        active = st.session_state.active_file
        if active in st.session_state.file_list:
            st.session_state.nodes = load_file_from_gist(GITHUB_TOKEN, GIST_ID, active)
        elif st.session_state.file_list:
            named = [f for f in st.session_state.file_list if is_active_file(f)]
            if named:
                st.session_state.active_file = named[0]
                st.session_state.nodes = load_file_from_gist(GITHUB_TOKEN, GIST_ID, named[0])
        st.session_state.gist_loaded = True
        st.session_state.save_status = "loaded"
        st.session_state.save_msg    = f"Loaded '{st.session_state.active_file}'"

def save_current(nodes=None, filename=None, status_label=None):
    if nodes   is None: nodes    = st.session_state.nodes
    if filename is None: filename = st.session_state.active_file
    if configured:
        ok = save_file_to_gist(GITHUB_TOKEN, GIST_ID, filename, nodes)
        st.session_state.save_status = "saved" if ok else "error"
        st.session_state.save_msg    = (
            status_label or f"Saved '{filename}' at {datetime.now().strftime('%H:%M:%S')}"
        ) if ok else "❌ Save failed"
        st.session_state.file_list = list_save_files(GITHUB_TOKEN, GIST_ID)
        return ok
    st.session_state.save_status = "no_config"
    st.session_state.save_msg    = "Gist not configured"
    return False

def set_nodes(n):
    st.session_state.nodes = n
    save_current(n)

# ── CSS ───────────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&display=swap');
html, body, [class*="css"] {
    font-family: 'JetBrains Mono', monospace !important;
    background-color: #0d0d0d !important;
    color: #e0e0e0 !important;
}
.stApp { background-color: #0d0d0d !important; }
section[data-testid="stSidebar"] {
    background: #111 !important;
    border-right: 1px solid #222 !important;
}
.stButton > button {
    font-family: 'JetBrains Mono', monospace !important;
    font-weight: 700 !important; letter-spacing: 1px !important;
}
.stTabs [data-baseweb="tab"] {
    font-family: 'JetBrains Mono', monospace !important;
    font-size: 11px !important; letter-spacing: 1px !important;
}
</style>
""", unsafe_allow_html=True)

# ── Header ────────────────────────────────────────────────────────────────
nodes    = st.session_state.nodes
hazard   = next((n for n in nodes if n["type"] == "HAZARD"), None)
by_level = {lvl: [n for n in nodes if n["type"] == lvl] for lvl in LEVEL_ORDER}

sc = st.session_state.save_status
save_color = {"saved":"#4caf7d","loaded":"#4caf7d","error":"#ff4d4d","no_config":"#f5c518","idle":"#888"}.get(sc,"#888")
save_icon  = {"saved":"✓","loaded":"⬇","error":"✗","no_config":"⚠","idle":"○"}.get(sc,"○")

st.markdown(f"""
<div style="background:linear-gradient(90deg,#1a1a2e,#16213e,#0f3460);
            border-bottom:2px solid #e94560;padding:12px 20px;
            margin:-1rem -1rem 1rem -1rem;
            display:flex;justify-content:space-between;align-items:center;">
  <div>
    <div style="font-size:20px;font-weight:700;letter-spacing:2px;color:#e94560;">
      ⚠ FTA REVERSE ENGINEER
    </div>
    <div style="font-size:9px;color:#888;letter-spacing:3px;margin-top:2px;">
      FAULT TREE ANALYSIS · TOP-DOWN DISTRIBUTION
    </div>
  </div>
  <div style="text-align:right;">
    <div style="font-size:12px;color:{save_color};font-weight:700;">
      {save_icon} {st.session_state.save_msg or "Ready"}
    </div>
    <div style="font-size:9px;color:#555;margin-top:2px;">
      Active: <span style="color:#aaa;">{st.session_state.active_file}</span>
      &nbsp;·&nbsp; {len(nodes)} nodes
    </div>
  </div>
</div>
""", unsafe_allow_html=True)

if not configured:
    st.warning("⚠️ Gist not configured — data resets on refresh. Add GITHUB_TOKEN + GIST_ID to Streamlit secrets.")

# ── Sidebar ───────────────────────────────────────────────────────────────
with st.sidebar:

    # FILE MANAGER
    with st.expander("📁 FILE MANAGER", expanded=False):
        st.markdown(f"<div style='font-size:10px;color:#ff8c42;font-weight:700;"
                    f"margin-bottom:8px;'>▶ {st.session_state.active_file}</div>",
                    unsafe_allow_html=True)

        new_name = st.text_input("Save as name", placeholder="e.g. baseline",
                                 key="new_save_name", label_visibility="collapsed")
        c1, c2 = st.columns(2)
        with c1:
            if st.button("💾 Save As", use_container_width=True):
                fname = new_name.strip()
                if fname:
                    if not fname.endswith(".json"): fname += ".json"
                    ok = save_current(filename=fname,
                                      status_label=f"Saved as '{fname}' at {datetime.now().strftime('%H:%M:%S')}")
                    if ok:
                        st.session_state.active_file = fname
                        st.rerun()
        with c2:
            if st.button("📸 Snapshot", use_container_width=True):
                snap = f"snapshot_{now_str()}.json"
                save_current(filename=snap, status_label=f"Snapshot: {snap}")
                st.rerun()

        if configured:
            if st.button("🔄 Refresh", use_container_width=True):
                st.session_state.file_list = list_save_files(GITHUB_TOKEN, GIST_ID)
                st.rerun()

            named = [f for f in st.session_state.file_list if is_active_file(f)]
            snaps = sorted([f for f in st.session_state.file_list if is_snapshot(f)], reverse=True)

            if named:
                st.markdown("<div style='font-size:9px;color:#ff8c42;margin:6px 0 3px;'>NAMED FILES</div>",
                            unsafe_allow_html=True)
                for fname in named:
                    is_act = fname == st.session_state.active_file
                    ca, cb, cc = st.columns([5, 2, 2])
                    with ca:
                        st.markdown(
                            f"<div style='font-size:10px;color:{'#ff8c42' if is_act else '#aaa'};"
                            f"padding:3px 0;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;'>"
                            f"{'▶ ' if is_act else ''}{fname}</div>",
                            unsafe_allow_html=True)
                    with cb:
                        if st.button("Load", key=f"l_{fname}"):
                            data = load_file_from_gist(GITHUB_TOKEN, GIST_ID, fname)
                            st.session_state.nodes       = data
                            st.session_state.active_file = fname
                            st.session_state.save_status = "loaded"
                            st.session_state.save_msg    = f"Loaded '{fname}'"
                            st.session_state.selected_id = None
                            st.rerun()
                    with cc:
                        if not is_act and st.button("Del", key=f"d_{fname}"):
                            delete_file_from_gist(GITHUB_TOKEN, GIST_ID, fname)
                            st.session_state.file_list = list_save_files(GITHUB_TOKEN, GIST_ID)
                            st.rerun()

            if snaps:
                st.markdown("<div style='font-size:9px;color:#4fc3f7;margin:8px 0 3px;'>SNAPSHOTS (last 5)</div>",
                            unsafe_allow_html=True)
                for fname in snaps[:5]:
                    short = fname.replace("snapshot_", "").replace(".json", "")
                    ca, cb, cc = st.columns([5, 2, 2])
                    with ca:
                        st.markdown(f"<div style='font-size:9px;color:#4fc3f7;padding:3px 0;'>📸 {short}</div>",
                                    unsafe_allow_html=True)
                    with cb:
                        if st.button("Load", key=f"l_{fname}"):
                            data = load_file_from_gist(GITHUB_TOKEN, GIST_ID, fname)
                            st.session_state.nodes       = data
                            st.session_state.active_file = fname
                            st.session_state.save_status = "loaded"
                            st.session_state.save_msg    = f"Loaded snapshot '{fname}'"
                            st.session_state.selected_id = None
                            st.rerun()
                    with cc:
                        if st.button("Del", key=f"d_{fname}"):
                            delete_file_from_gist(GITHUB_TOKEN, GIST_ID, fname)
                            st.session_state.file_list = list_save_files(GITHUB_TOKEN, GIST_ID)
                            st.rerun()

    st.markdown("---")

    # NODE EDITOR
    st.markdown("### 🔧 NODE EDITOR")

    editor_tab_add, editor_tab_edit = st.tabs(["➕ ADD", "✏️ EDIT"])

    with editor_tab_add:
        if not hazard:
            st.markdown("**STEP 1 — TOP EVENT**")
            h_name = st.text_input("Hazard Name", placeholder="e.g. Aircraft Crash")
            h_val  = st.text_input("Target Failure Rate", placeholder="e.g. 1e-7")
            if st.button("➕ ADD HAZARD", use_container_width=True):
                try:
                    val  = float(h_val)
                    node = {
                        "id": str(uuid.uuid4())[:7], "name": h_name.strip(),
                        "type": "HAZARD", "gate": "OR",
                        "targetValue": val, "calculatedValue": val, "parentIds": []
                    }
                    set_nodes([node])
                    st.rerun()
                except ValueError:
                    st.error("Invalid value — use format like 1e-7")
        else:
            col = LEVEL_COLORS["HAZARD"]
            st.markdown(f"""
            <div style="background:#141414;border:1px solid {col};border-radius:8px;
                        padding:10px;margin-bottom:10px;">
              <div style="font-size:9px;color:#888;letter-spacing:2px;">TOP EVENT</div>
              <div style="font-weight:700;color:{col};margin:3px 0;">{hazard['name']}</div>
              <div style="font-size:11px;color:#aaa;">
                Target: <span style="color:#fff">{fmt(hazard['targetValue'])}</span>
              </div>
            </div>""", unsafe_allow_html=True)

            node_name = st.text_input("Node Name", placeholder="e.g. Power Failure", key="add_name")
            parent_options = {f"[{n['type']}] {n['name']}": n["id"]
                              for n in nodes if n["type"] in VALID_PARENT_TYPES}
            sel_labels     = st.multiselect("Parent Node(s)", list(parent_options.keys()),
                                            help="Select multiple → shared node. SF→SF supported.",
                                            key="add_parents")
            sel_parent_ids = [parent_options[l] for l in sel_labels]
            node_type      = st.selectbox("Node Type", VALID_CHILD_TYPES, key="add_type",
                                          help="SF→SF allowed for transfer events")
            gate           = st.radio("Gate (for this node's children)", ["OR", "AND"],
                                      horizontal=True, key="add_gate")

            if st.button("✅ ADD NODE", use_container_width=True, type="primary"):
                if not node_name.strip():
                    st.error("Enter a node name")
                elif not sel_parent_ids:
                    st.error("Select at least one parent")
                else:
                    new_node = {
                        "id": str(uuid.uuid4())[:7], "name": node_name.strip(),
                        "type": node_type, "gate": gate,
                        "targetValue": None, "calculatedValue": None,
                        "parentIds": sel_parent_ids
                    }
                    set_nodes(nodes + [new_node])
                    st.rerun()

            st.markdown("---")
            del_options = {f"[{n['type']}] {n['name']}": n["id"]
                           for n in nodes if n["type"] != "HAZARD"}
            if del_options:
                del_label = st.selectbox("Delete Node", ["— select —"] + list(del_options.keys()))
                if del_label != "— select —" and st.button("🗑 DELETE NODE", use_container_width=True):
                    del_id    = del_options[del_label]
                    to_delete = {del_id}
                    changed   = True
                    while changed:
                        changed = False
                        for n in nodes:
                            if n["id"] not in to_delete and any(
                                p in to_delete for p in (n.get("parentIds") or [])
                            ):
                                to_delete.add(n["id"])
                                changed = True
                    set_nodes([n for n in nodes if n["id"] not in to_delete])
                    st.rerun()

            st.markdown("---")
            if st.button("🗑 CLEAR ALL", use_container_width=True):
                set_nodes([])
                st.session_state.selected_id = None
                st.rerun()

    with editor_tab_edit:
        if not nodes:
            st.markdown("<div style='color:#555;font-size:11px;'>No nodes to edit yet.</div>",
                        unsafe_allow_html=True)
        else:
            edit_options = {f"[{n['type']}] {n['name']}": n["id"] for n in nodes}
            edit_label   = st.selectbox("Select node to edit",
                                        ["— select —"] + list(edit_options.keys()),
                                        key="edit_select")

            if edit_label != "— select —":
                edit_id   = edit_options[edit_label]
                edit_node = next((n for n in nodes if n["id"] == edit_id), None)

                if edit_node:
                    color = LEVEL_COLORS.get(edit_node["type"], "#888")
                    st.markdown(f"""
                    <div style="background:#141414;border:2px solid {color};
                                border-radius:8px;padding:8px 12px;margin-bottom:10px;">
                      <div style="font-size:8px;color:#888;letter-spacing:2px;">EDITING</div>
                      <div style="font-weight:700;color:{color};">{edit_node['name']}</div>
                      <div style="font-size:9px;color:#666;">{edit_node['type']} · {edit_node['gate']}</div>
                    </div>""", unsafe_allow_html=True)

                    # ── Edit name ──
                    new_name = st.text_input("Name", value=edit_node["name"], key="edit_name")

                    # ── Edit gate ──
                    gate_idx = 0 if edit_node["gate"] == "OR" else 1
                    new_gate = st.radio("Gate", ["OR", "AND"], index=gate_idx,
                                        horizontal=True, key="edit_gate")

                    # ── Edit type (non-hazard only) ──
                    if edit_node["type"] != "HAZARD":
                        type_idx = VALID_CHILD_TYPES.index(edit_node["type"]) if edit_node["type"] in VALID_CHILD_TYPES else 0
                        new_type = st.selectbox("Node Type", VALID_CHILD_TYPES,
                                                index=type_idx, key="edit_type")
                    else:
                        new_type = "HAZARD"
                        # Hazard: allow editing target value
                        new_target_str = st.text_input(
                            "Target Failure Rate",
                            value=str(edit_node.get("targetValue", "")),
                            key="edit_target"
                        )

                    # ── Edit parents (non-hazard only) ──
                    if edit_node["type"] != "HAZARD":
                        avail_parents  = {f"[{n['type']}] {n['name']}": n["id"]
                                          for n in nodes
                                          if n["type"] in VALID_PARENT_TYPES and n["id"] != edit_id}
                        current_parent_labels = [
                            lbl for lbl, pid in avail_parents.items()
                            if pid in (edit_node.get("parentIds") or [])
                        ]
                        new_parent_labels = st.multiselect(
                            "Parent Node(s)", list(avail_parents.keys()),
                            default=current_parent_labels, key="edit_parents"
                        )
                        new_parent_ids = [avail_parents[l] for l in new_parent_labels]
                    else:
                        new_parent_ids = []

                    # ── Apply changes ──
                    if st.button("💾 APPLY CHANGES", use_container_width=True, type="primary"):
                        updated = []
                        for n in nodes:
                            if n["id"] == edit_id:
                                n = dict(n)
                                n["name"] = new_name.strip() or n["name"]
                                n["gate"] = new_gate
                                if n["type"] != "HAZARD":
                                    n["type"]      = new_type
                                    n["parentIds"] = new_parent_ids
                                else:
                                    try:
                                        tv = float(new_target_str)
                                        n["targetValue"]    = tv
                                        n["calculatedValue"] = tv
                                    except ValueError:
                                        pass
                            updated.append(n)
                        set_nodes(updated)
                        st.success(f"Updated '{new_name}'")
                        st.rerun()

# ── Action bar ────────────────────────────────────────────────────────────
a1, a2, a3, a4 = st.columns([1, 1, 1, 3])
with a1:
    if st.button("▶ CALCULATE", type="primary", use_container_width=True):
        if nodes:
            new_nodes = recalculate(nodes)
            snap      = f"snapshot_{now_str()}.json"
            save_current(new_nodes, filename=snap,
                         status_label=f"Calculated + snapshot: {snap}")
            save_current(new_nodes)
            st.session_state.nodes = new_nodes
            st.rerun()
with a2:
    if st.button("💾 SAVE NOW", use_container_width=True):
        save_current()
        st.rerun()
with a3:
    # Export buttons
    if nodes:
        json_bytes = export_json(nodes)
        st.download_button(
            "⬇ JSON", data=json_bytes,
            file_name=f"fta_{now_str()}.json",
            mime="application/json",
            use_container_width=True
        )
with a4:
    if nodes:
        xl = export_excel(nodes)
        if xl:
            st.download_button(
                "⬇ EXCEL", data=xl,
                file_name=f"fta_{now_str()}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True
            )
        else:
            st.info("Install openpyxl for Excel export")

st.markdown("---")

# ── Tabs: Tree | Hierarchy | Values ──────────────────────────────────────
tab_tree, tab_hier, tab_vals = st.tabs(["🌳 TREE VISUALIZATION", "📋 HIERARCHY & VALUES", "📊 NODE VALUES"])

# ── TAB 1: Interactive HTML Tree ──────────────────────────────────────────
with tab_tree:
    if not hazard:
        st.markdown(
            "<div style='text-align:center;color:#333;margin-top:60px;letter-spacing:2px;'>"
            "ADD A HAZARD IN THE SIDEBAR TO START</div>",
            unsafe_allow_html=True
        )
    else:
        st.markdown(
            "<div style='font-size:9px;color:#555;margin-bottom:8px;'>"
            "Click node to inspect · Hover for tooltip · Connected nodes highlight in orange</div>",
            unsafe_allow_html=True
        )
        tree_html = build_html_tree(nodes, st.session_state.selected_id)
        components.html(tree_html, height=680, scrolling=False)

# ── TAB 2: Hierarchy panel ────────────────────────────────────────────────
with tab_hier:
    if not nodes:
        st.markdown("<div style='color:#333;margin-top:40px;text-align:center;'>No nodes yet</div>",
                    unsafe_allow_html=True)
    else:
        st.markdown(
            "<div style='font-size:9px;color:#555;margin-bottom:12px;letter-spacing:2px;'>"
            "TOP → BOTTOM HIERARCHY WITH CALCULATED VALUES</div>",
            unsafe_allow_html=True
        )
        rows = build_hierarchy_rows(nodes)
        for row in rows:
            node  = row["node"]
            depth = row["depth"]
            is_ref = row.get("ref", False)
            color  = LEVEL_COLORS.get(node["type"], "#888")
            tcolor = LEVEL_TEXT.get(node["type"], "#fff")
            val    = fmt(node.get("calculatedValue"))
            indent = depth * 28

            connector = ""
            if depth > 0:
                connector = "└─ "

            ref_tag = (
                '<span style="background:#333;color:#888;font-size:7px;'
                'padding:1px 4px;border-radius:4px;margin-left:4px;">REF</span>'
                if is_ref else ""
            )
            shared_tag = (
                '<span style="background:#f5c518;color:#111;font-size:7px;'
                'padding:1px 4px;border-radius:4px;margin-left:4px;">SHARED</span>'
                if len(node.get("parentIds") or []) > 1 else ""
            )
            gate_tag = (
                f'<span style="color:{"#4fc3f7" if node["gate"]=="OR" else "#ffb74d"};'
                f'font-size:8px;margin-left:6px;">[{node["gate"]}]</span>'
            )

            st.markdown(f"""
            <div style="display:flex;align-items:center;padding:5px 8px;
                        margin-left:{indent}px;margin-bottom:2px;
                        background:#141414;border-left:3px solid {color};
                        border-radius:0 6px 6px 0;">
              <div style="flex:1;min-width:0;">
                <span style="color:#555;font-size:10px;">{connector}</span>
                <span style="font-weight:{'700' if depth==0 else '400'};
                             color:#ddd;font-size:11px;">{node['name']}</span>
                <span style="font-size:8px;color:#666;margin-left:6px;">{node['type']}</span>
                {gate_tag}{shared_tag}{ref_tag}
              </div>
              <div style="font-weight:700;font-size:12px;color:{color};
                          font-family:monospace;flex-shrink:0;margin-left:12px;">
                {val}
              </div>
            </div>
            """, unsafe_allow_html=True)

# ── TAB 3: Node values table ──────────────────────────────────────────────
with tab_vals:
    if not nodes:
        st.markdown("<div style='color:#333;margin-top:40px;text-align:center;'>No nodes yet</div>",
                    unsafe_allow_html=True)
    else:
        for level in LEVEL_ORDER:
            level_nodes = by_level[level]
            if not level_nodes:
                continue
            color = LEVEL_COLORS[level]
            st.markdown(
                f"<div style='font-size:9px;letter-spacing:3px;color:{color};"
                f"border-bottom:1px solid {color}33;padding-bottom:4px;"
                f"margin:14px 0 6px 0;'>{level} — {len(level_nodes)} nodes</div>",
                unsafe_allow_html=True
            )
            for node in level_nodes:
                by_id_local = {n["id"]: n for n in nodes}
                parent_names = " · ".join(
                    by_id_local[p]["name"] for p in (node.get("parentIds") or [])
                    if p in by_id_local
                ) or "—"
                child_names = " · ".join(
                    n["name"] for n in nodes
                    if node["id"] in (n.get("parentIds") or [])
                ) or "—"
                is_shared = len(node.get("parentIds") or []) > 1
                gate_color = "#4fc3f7" if node["gate"] == "OR" else "#ffb74d"

                st.markdown(f"""
                <div style="background:#141414;border:1px solid #222;border-radius:6px;
                            padding:8px 12px;margin-bottom:4px;
                            display:grid;grid-template-columns:2fr 1fr 1fr 2fr 2fr;gap:8px;
                            align-items:center;">
                  <div>
                    <div style="font-weight:700;font-size:11px;color:#ddd;">{node['name']}</div>
                    {'<div style="font-size:8px;color:#f5c518;">◈ SHARED</div>' if is_shared else ''}
                  </div>
                  <div style="font-size:9px;color:{color};font-weight:700;">{node['type']}</div>
                  <div style="font-size:9px;color:{gate_color};font-weight:700;">{node['gate']}</div>
                  <div style="font-size:10px;color:{color};font-weight:700;font-family:monospace;">
                    {fmt(node.get('calculatedValue'))}
                  </div>
                  <div style="font-size:9px;color:#555;">
                    ↑ {parent_names}<br>↓ {child_names}
                  </div>
                </div>
                """, unsafe_allow_html=True)

        # Summary counts
        st.markdown("---")
        cols = st.columns(5)
        all_counts = [(lvl, len(by_level[lvl])) for lvl in LEVEL_ORDER] + [("TOTAL", len(nodes))]
        for i, (lvl, count) in enumerate(all_counts):
            with cols[i % 5]:
                color = LEVEL_COLORS.get(lvl, "#e94560")
                st.markdown(f"""
                <div style="background:#141414;border:1px solid {color}44;border-radius:6px;
                            padding:10px;text-align:center;">
                  <div style="font-size:8px;color:#555;letter-spacing:2px;">{lvl}</div>
                  <div style="font-size:20px;font-weight:700;color:{color};">{count}</div>
                </div>
                """, unsafe_allow_html=True)
