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
            child["calculatedValue"] = child_val if existing is None else min(existing, child_val)
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
    """Build an interactive HTML/CSS/JS tree with zoom + right-click pan."""
    if not nodes:
        return ""

    by_id   = {n["id"]: n for n in nodes}
    hazards = [n for n in nodes if n["type"] == "HAZARD"]
    if not hazards:
        return ""

    def node_html(node):
        nid       = node["id"]
        color     = LEVEL_COLORS.get(node["type"], "#888")
        tcolor    = LEVEL_TEXT.get(node["type"], "#fff")
        val       = fmt(node.get("calculatedValue"))
        is_shared = len(node.get("parentIds") or []) > 1
        children  = [n for n in nodes if nid in (n.get("parentIds") or [])]
        pnames    = [by_id[p]["name"] for p in (node.get("parentIds") or []) if p in by_id]
        cnames    = [n["name"] for n in children]
        gate_col  = "#4fc3f7" if node["gate"] == "OR" else "#ffb74d"
        is_sel    = nid == selected_id

        shared_badge = (
            '<span style="background:#f5c518;color:#111;font-size:7px;padding:1px 4px;'
            'border-radius:6px;font-weight:700;margin-left:4px;">SHARED</span>'
            if is_shared else ""
        )
        sel_outline = "box-shadow:0 0 0 3px #e94560,0 0 20px #e9456066;" if is_sel else ""

        gate_html = ""
        if children:
            gate_html = f"""
            <div style="display:flex;flex-direction:column;align-items:center;">
              <div style="width:2px;height:12px;background:#333;"></div>
              <div style="background:#1a1a1a;border:1px solid {gate_col};border-radius:4px;
                          padding:1px 7px;font-size:9px;color:{gate_col};font-weight:700;
                          font-family:monospace;letter-spacing:1px;">{node['gate']}</div>
              <div style="width:2px;height:12px;background:#333;"></div>
            </div>"""

        node_box = f"""
        <div style="display:flex;flex-direction:column;align-items:center;">
          <div class="fta-node" data-id="{nid}"
               onclick="selectNode(event,'{nid}')"
               style="background:{color};color:{tcolor};border-radius:8px;
                      padding:8px 14px;min-width:130px;max-width:170px;
                      cursor:pointer;user-select:none;
                      border:2px solid {color};transition:filter 0.15s,box-shadow 0.15s;
                      {sel_outline}">
            <div style="font-size:8px;opacity:0.75;letter-spacing:1px;margin-bottom:3px;
                        display:flex;align-items:center;justify-content:center;gap:4px;">
              {node['type']}{shared_badge}
            </div>
            <div style="font-size:11px;font-weight:700;line-height:1.3;
                        word-break:break-word;text-align:center;margin-bottom:5px;">
              {node['name']}
            </div>
            <div style="background:rgba(0,0,0,0.28);border-radius:4px;
                        padding:3px 6px;font-size:12px;font-weight:700;
                        text-align:center;font-family:monospace;letter-spacing:0.5px;">
              {val}
            </div>
          </div>
          {gate_html}
        </div>"""

        if not children:
            return f'<div class="tree-node-wrap" style="display:inline-flex;flex-direction:column;align-items:center;margin:0 8px;">{node_box}</div>'

        children_html = "".join(node_html(c) for c in children)
        return f"""
        <div class="tree-node-wrap" style="display:inline-flex;flex-direction:column;align-items:center;margin:0 8px;">
          {node_box}
          <div style="display:flex;flex-direction:row;align-items:flex-start;">
            <div class="connector-row" style="display:flex;flex-direction:row;align-items:flex-start;position:relative;">
              {children_html}
            </div>
          </div>
        </div>"""

    tree_html = "".join(node_html(h) for h in hazards)

    nodes_js = json.dumps({
        n["id"]: {
            "name":     n["name"],
            "type":     n["type"],
            "gate":     n["gate"],
            "value":    fmt(n.get("calculatedValue")),
            "parents":  [by_id[p]["name"] for p in (n.get("parentIds") or []) if p in by_id],
            "children": [c["name"] for c in nodes if n["id"] in (c.get("parentIds") or [])],
            "shared":   len(n.get("parentIds") or []) > 1,
            "color":    LEVEL_COLORS.get(n["type"], "#888"),
            "tcolor":   LEVEL_TEXT.get(n["type"], "#fff"),
        }
        for n in nodes
    })

    init_sel = f'selectNode(null,"{selected_id}");' if selected_id else ""

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

  /* ── toolbar ── */
  #toolbar{{
    display:flex;align-items:center;gap:8px;
    padding:6px 12px;
    background:#111;border-bottom:1px solid #1e1e1e;
    flex-shrink:0;font-size:10px;color:#555;letter-spacing:1px;
    user-select:none;
  }}
  .tb-btn{{
    background:#1a1a1a;border:1px solid #333;color:#aaa;
    border-radius:4px;padding:3px 10px;cursor:pointer;
    font-family:inherit;font-size:10px;letter-spacing:1px;
    transition:background 0.1s;
  }}
  .tb-btn:hover{{background:#252525;color:#fff;}}
  #zoom-label{{color:#666;font-size:10px;min-width:42px;text-align:center;}}

  /* ── viewport ── */
  #viewport{{
    flex:1;overflow:hidden;position:relative;
    cursor:grab;
  }}
  #viewport.dragging{{cursor:grabbing;}}
  #canvas{{
    position:absolute;
    transform-origin:0 0;
    padding:40px 60px 80px 60px;
    will-change:transform;
  }}

  /* ── nodes ── */
  .fta-node:hover{{
    filter:brightness(1.18);
    box-shadow:0 6px 20px rgba(0,0,0,0.6) !important;
  }}
  .connector-row>div{{position:relative;}}
  .connector-row>div:not(:only-child)::before{{
    content:'';position:absolute;top:0;left:50%;width:100%;
    border-top:2px solid #2a2a2a;
  }}
  .connector-row>div:first-child:not(:only-child)::before{{left:50%;width:50%;}}
  .connector-row>div:last-child:not(:only-child)::before{{left:0;width:50%;}}

  /* ── detail panel ── */
  #detail-panel{{
    position:fixed;bottom:0;left:0;right:0;
    background:#141414ee;border-top:2px solid #333;
    padding:10px 16px;display:none;
    backdrop-filter:blur(8px);
    z-index:100;
  }}
  .detail-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:8px 0 6px;}}
  .dc{{background:#0a0a0a;border-radius:6px;padding:6px;text-align:center;}}
  .dcl{{font-size:7px;color:#555;letter-spacing:2px;margin-bottom:2px;}}
  .dcv{{font-size:12px;font-weight:700;}}
  .detail-row{{display:grid;grid-template-columns:1fr 1fr;gap:8px;}}
  .ds{{background:#0a0a0a;border-radius:6px;padding:6px;}}
  .dsl{{font-size:7px;color:#555;letter-spacing:2px;margin-bottom:3px;}}
  .dsv{{font-size:10px;color:#ccc;}}
  #close-detail{{
    position:absolute;top:8px;right:12px;
    background:none;border:none;color:#555;font-size:16px;
    cursor:pointer;font-family:inherit;
  }}
  #close-detail:hover{{color:#fff;}}
</style>
</head>
<body>

<!-- toolbar -->
<div id="toolbar">
  <button class="tb-btn" onclick="zoom(0.15)">＋ Zoom In</button>
  <button class="tb-btn" onclick="zoom(-0.15)">－ Zoom Out</button>
  <button class="tb-btn" onclick="resetView()">⌂ Reset</button>
  <span id="zoom-label">100%</span>
  <span style="margin-left:8px;">Scroll to zoom &nbsp;·&nbsp; Right-click drag to pan &nbsp;·&nbsp; Click node to inspect</span>
</div>

<!-- zoomable viewport -->
<div id="viewport">
  <div id="canvas">
    {tree_html}
  </div>
</div>

<!-- detail panel (fixed bottom) -->
<div id="detail-panel">
  <button id="close-detail" onclick="closeDetail()">✕</button>
  <div style="font-size:8px;color:#888;letter-spacing:3px;margin-bottom:4px;">SELECTED NODE</div>
  <div id="detail-title" style="font-size:14px;font-weight:700;margin-bottom:4px;"></div>
  <div class="detail-grid">
    <div class="dc"><div class="dcl">TYPE</div><div class="dcv" id="d-type"></div></div>
    <div class="dc"><div class="dcl">GATE</div><div class="dcv" id="d-gate"></div></div>
    <div class="dc"><div class="dcl">VALUE</div><div class="dcv" id="d-value"></div></div>
    <div class="dc"><div class="dcl">SHARED</div><div class="dcv" id="d-shared"></div></div>
  </div>
  <div class="detail-row">
    <div class="ds"><div class="dsl">PARENTS</div><div class="dsv" id="d-parents"></div></div>
    <div class="ds"><div class="dsl">CHILDREN</div><div class="dsv" id="d-children"></div></div>
  </div>
</div>

<script>
const NODES = {nodes_js};

// ── transform state ──
let scale  = 1.0;
let tx     = 0;
let ty     = 0;
const MIN_SCALE = 0.15;
const MAX_SCALE = 3.0;
const canvas    = document.getElementById('canvas');
const viewport  = document.getElementById('viewport');

function applyTransform() {{
  canvas.style.transform = `translate(${{tx}}px,${{ty}}px) scale(${{scale}})`;
  document.getElementById('zoom-label').textContent = Math.round(scale*100)+'%';
}}

function zoom(delta, cx, cy) {{
  const vr  = viewport.getBoundingClientRect();
  const ocx = (cx !== undefined) ? cx - vr.left : vr.width  / 2;
  const ocy = (cy !== undefined) ? cy - vr.top  : vr.height / 2;
  const newScale = Math.min(MAX_SCALE, Math.max(MIN_SCALE, scale + delta));
  const ratio    = newScale / scale;
  tx = ocx - ratio * (ocx - tx);
  ty = ocy - ratio * (ocy - ty);
  scale = newScale;
  applyTransform();
}}

function resetView() {{
  scale = 1.0; tx = 0; ty = 0;
  applyTransform();
}}

// ── scroll to zoom ──
viewport.addEventListener('wheel', e => {{
  e.preventDefault();
  const delta = e.deltaY < 0 ? 0.1 : -0.1;
  zoom(delta, e.clientX, e.clientY);
}}, {{ passive: false }});

// ── right-click drag to pan ──
let isPanning = false;
let panStart  = {{ x:0, y:0 }};

viewport.addEventListener('mousedown', e => {{
  if (e.button === 2) {{
    isPanning = true;
    panStart  = {{ x: e.clientX - tx, y: e.clientY - ty }};
    viewport.classList.add('dragging');
    e.preventDefault();
  }}
}});

window.addEventListener('mousemove', e => {{
  if (!isPanning) return;
  tx = e.clientX - panStart.x;
  ty = e.clientY - panStart.y;
  applyTransform();
}});

window.addEventListener('mouseup', e => {{
  if (e.button === 2) {{
    isPanning = false;
    viewport.classList.remove('dragging');
  }}
}});

// Disable right-click context menu on viewport
viewport.addEventListener('contextmenu', e => e.preventDefault());

// ── touch pinch-to-zoom + drag ──
let lastTouchDist = null;
let lastTouchMid  = null;

viewport.addEventListener('touchstart', e => {{
  if (e.touches.length === 2) {{
    const dx = e.touches[0].clientX - e.touches[1].clientX;
    const dy = e.touches[0].clientY - e.touches[1].clientY;
    lastTouchDist = Math.hypot(dx, dy);
    lastTouchMid  = {{
      x: (e.touches[0].clientX + e.touches[1].clientX) / 2,
      y: (e.touches[0].clientY + e.touches[1].clientY) / 2,
    }};
  }}
}}, {{ passive: true }});

viewport.addEventListener('touchmove', e => {{
  if (e.touches.length === 2) {{
    const dx   = e.touches[0].clientX - e.touches[1].clientX;
    const dy   = e.touches[0].clientY - e.touches[1].clientY;
    const dist = Math.hypot(dx, dy);
    const mid  = {{
      x: (e.touches[0].clientX + e.touches[1].clientX) / 2,
      y: (e.touches[0].clientY + e.touches[1].clientY) / 2,
    }};
    if (lastTouchDist) {{
      const delta = (dist - lastTouchDist) * 0.01;
      zoom(delta, mid.x, mid.y);
    }}
    lastTouchDist = dist;
    lastTouchMid  = mid;
    e.preventDefault();
  }}
}}, {{ passive: false }});

// ── node selection ──
let selectedId = null;

function selectNode(event, id) {{
  // Don't trigger if we were panning
  if (isPanning) return;

  document.querySelectorAll('.fta-node').forEach(el => {{
    el.style.boxShadow = '';
  }});

  if (selectedId === id) {{
    selectedId = null;
    closeDetail();
    return;
  }}

  selectedId = id;
  const node = NODES[id];
  if (!node) return;

  // Highlight selected node
  const el = document.querySelector(`[data-id="${{id}}"]`);
  if (el) el.style.boxShadow = '0 0 0 3px #e94560,0 0 22px #e9456077';

  // Highlight connected nodes
  Object.entries(NODES).forEach(([nid, n]) => {{
    if (nid === id) return;
    const isParent = node.parents.includes(n.name);
    const isChild  = node.children.includes(n.name);
    if (isParent || isChild) {{
      const cel = document.querySelector(`[data-id="${{nid}}"]`);
      if (cel) cel.style.boxShadow = '0 0 0 2px #ff8c42,0 0 14px #ff8c4255';
    }}
  }});

  // Show detail panel
  const panel = document.getElementById('detail-panel');
  panel.style.display  = 'block';
  panel.style.borderTopColor = node.color;

  document.getElementById('detail-title').innerHTML =
    `<span style="color:${{node.color}}">${{node.name}}</span>` +
    (node.shared
      ? ' <span style="background:#f5c518;color:#111;font-size:8px;padding:1px 6px;border-radius:6px;font-weight:700;">SHARED</span>'
      : '');

  document.getElementById('d-type').style.color   = node.color;
  document.getElementById('d-type').textContent   = node.type;
  document.getElementById('d-gate').style.color   = node.gate === 'OR' ? '#4fc3f7' : '#ffb74d';
  document.getElementById('d-gate').textContent   = node.gate;
  document.getElementById('d-value').style.color  = node.color;
  document.getElementById('d-value').textContent  = node.value;
  document.getElementById('d-shared').textContent = node.shared ? 'YES' : 'NO';
  document.getElementById('d-shared').style.color = node.shared ? '#f5c518' : '#555';
  document.getElementById('d-parents').textContent  = node.parents.join(' · ') || '(top event)';
  document.getElementById('d-children').textContent = node.children.join(' · ') || '(leaf node)';
}}

function closeDetail() {{
  document.getElementById('detail-panel').style.display = 'none';
  document.querySelectorAll('.fta-node').forEach(el => el.style.boxShadow = '');
  selectedId = null;
}}

// centre tree on load
window.addEventListener('load', () => {{
  const vr = viewport.getBoundingClientRect();
  const cr = canvas.getBoundingClientRect();
  tx = (vr.width  - cr.width)  / 2;
  ty = 20;
  applyTransform();
  {init_sel}
}});
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

        st.markdown("**➕ ADD NODE**")
        node_name = st.text_input("Node Name", placeholder="e.g. Power Failure")
        parent_options = {f"[{n['type']}] {n['name']}": n["id"]
                          for n in nodes if n["type"] in VALID_PARENT_TYPES}
        sel_labels     = st.multiselect("Parent Node(s)", list(parent_options.keys()),
                                        help="Select multiple → shared node. SF→SF supported.")
        sel_parent_ids = [parent_options[l] for l in sel_labels]
        node_type      = st.selectbox("Node Type", VALID_CHILD_TYPES,
                                      help="SF→SF allowed for transfer events")
        gate           = st.radio("Gate (for this node's children)", ["OR", "AND"], horizontal=True)

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
            if del_label != "— select —" and st.button("🗑 DELETE", use_container_width=True):
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
