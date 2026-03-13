import streamlit as st
import json, math, os, requests, uuid, io, re
from datetime import datetime
import streamlit.components.v1 as components

st.set_page_config(page_title="FTA Reverse Engineer", page_icon="⚠️",
                   layout="wide", initial_sidebar_state="expanded")

# ── Constants ─────────────────────────────────────────────────────────────
LEVEL_ORDER        = ["HAZARD", "SF", "FF", "IF"]
LEVEL_COLORS       = {"HAZARD":"#ff4d4d","SF":"#ff8c42","FF":"#f5c518","IF":"#4caf7d"}
LEVEL_TEXT         = {"HAZARD":"#fff","SF":"#fff","FF":"#111","IF":"#fff"}
VALID_PARENT_TYPES = ["HAZARD","SF","FF"]
VALID_CHILD_TYPES  = ["SF","FF","IF"]

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
    if n == 0: return parent_val
    return parent_val / n if gate == "OR" else parent_val ** (1.0 / n)

def recalculate(nodes):
    """
    Robust BFS top-down reverse distribution.
    - Supports multiple HAZARDs
    - Shared nodes receive WORST (MAX) value from all parent paths
    - Handles cycles gracefully via visit count limit
    - Scales to 500+ nodes
    """
    updated  = [dict(n) for n in nodes]
    by_id    = {n["id"]: n for n in updated}

    # Reset all non-HAZARD calculated values
    for n in updated:
        if n["type"] != "HAZARD":
            n["calculatedValue"] = None

    # BFS from all HAZARDs simultaneously
    queue    = [n for n in updated if n["type"] == "HAZARD"]
    # Track how many times each node has been visited (cycle guard)
    visit_count = {}

    while queue:
        parent = queue.pop(0)
        pid    = parent["id"]
        visit_count[pid] = visit_count.get(pid, 0) + 1
        if visit_count[pid] > len(updated) + 1:
            # cycle detected — skip to prevent infinite loop
            continue

        children = [n for n in updated if pid in (n.get("parentIds") or [])]
        if not children:
            continue

        # Use the node's own calculated value (already set from its parents)
        if parent["type"] == "HAZARD":
            parent_val = parent.get("targetValue") or 1e-7
        else:
            parent_val = parent.get("calculatedValue")
            if parent_val is None:
                # Parent not yet resolved — re-queue at end
                queue.append(parent)
                continue

        child_val = reverse_distribute(parent_val, parent["gate"], len(children))

        for child in children:
            existing = child.get("calculatedValue")
            # Shared nodes: WORST (MAX) value — highest failure rate is most conservative
            if existing is None:
                child["calculatedValue"] = child_val
            else:
                child["calculatedValue"] = max(existing, child_val)
            queue.append(child)

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
def build_html_tree(nodes, filter_hazard_id=None):
    """
    Build interactive zoomable/pannable/draggable tree.
    filter_hazard_id=None  → full tree (all hazards)
    filter_hazard_id=<id>  → only that hazard subtree
    """
    if not nodes: return ""
    by_id   = {n["id"]: n for n in nodes}

    # Determine which nodes to show
    if filter_hazard_id:
        # BFS from chosen hazard to collect all reachable nodes
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

    nodes_js = json.dumps({
        n["id"]: {
            "name":      n["name"],
            "type":      n["type"],
            "gate":      n["gate"],
            "value":     fmt(n.get("calculatedValue")),
            "parentIds": [p for p in (n.get("parentIds") or []) if p in by_id],
            "children":  [c["id"] for c in show_nodes if n["id"] in (c.get("parentIds") or [])],
            "childNames":[c["name"] for c in show_nodes if n["id"] in (c.get("parentIds") or [])],
            "parents":   [by_id[p]["name"] for p in (n.get("parentIds") or []) if p in by_id],
            "shared":    len(n.get("parentIds") or []) > 1,
            "color":     LEVEL_COLORS.get(n["type"], "#888"),
            "tcolor":    LEVEL_TEXT.get(n["type"], "#fff"),
        }
        for n in show_nodes
    })
    edges_js = json.dumps([
        [pid, n["id"]]
        for n in show_nodes
        for pid in (n.get("parentIds") or [])
        if pid in by_id and pid in {x["id"] for x in show_nodes}
    ])
    level_groups_js = json.dumps({
        lvl: [n["id"] for n in show_nodes if n["type"] == lvl]
        for lvl in LEVEL_ORDER
    })

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{background:#0a0a0a;font-family:'JetBrains Mono','Fira Code',monospace;color:#e0e0e0;overflow:hidden;height:100vh;display:flex;flex-direction:column;}}
#toolbar{{display:flex;align-items:center;gap:5px;flex-wrap:wrap;padding:5px 10px;background:#111;border-bottom:1px solid #1e1e1e;flex-shrink:0;font-size:10px;color:#555;user-select:none;}}
.tb-btn{{background:#1a1a1a;border:1px solid #2a2a2a;color:#aaa;border-radius:4px;padding:3px 9px;cursor:pointer;font-family:inherit;font-size:10px;transition:all 0.1s;white-space:nowrap;}}
.tb-btn:hover{{background:#252525;color:#fff;border-color:#555;}}
#zoom-lbl{{color:#555;min-width:36px;text-align:center;}}
#hint{{color:#2a2a2a;font-size:9px;margin-left:4px;}}
#viewport{{flex:1;overflow:hidden;position:relative;cursor:default;}}
#canvas{{position:absolute;top:0;left:0;transform-origin:0 0;will-change:transform;}}
svg#edges{{position:absolute;top:0;left:0;pointer-events:none;overflow:visible;}}
.nw{{position:absolute;display:flex;flex-direction:column;align-items:center;cursor:grab;transition:opacity 0.2s;}}
.fn{{border-radius:8px;padding:7px 11px;min-width:125px;max-width:160px;user-select:none;border:2px solid transparent;transition:filter 0.12s,box-shadow 0.12s;}}
.fn:hover{{filter:brightness(1.18);}}
.fn.dimmed{{opacity:0.2;}}
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
  <button class="tb-btn" onclick="resetView()">⌂ Reset</button>
  <button class="tb-btn" onclick="expandAll()">⊞ Expand</button>
  <button class="tb-btn" onclick="collapseAll()">⊟ Collapse</button>
  <button class="tb-btn" onclick="clearHL()">✕ Clear</button>
  <span id="hint">Scroll=zoom · Right-drag=pan · Left-drag=move node · Click=inspect · ▼=collapse subtree</span>
</div>
<div id="viewport"><div id="canvas"><svg id="edges"></svg></div></div>
<div id="dp">
  <button id="dp-close" onclick="closeDP()">✕</button>
  <div style="font-size:8px;color:#888;letter-spacing:3px;margin-bottom:2px;">SELECTED NODE</div>
  <div id="dp-title" style="font-size:13px;font-weight:700;margin-bottom:3px;"></div>
  <div class="dg">
    <div class="dc"><div class="dcl">TYPE</div><div class="dcv" id="dp-type"></div></div>
    <div class="dc"><div class="dcl">GATE</div><div class="dcv" id="dp-gate"></div></div>
    <div class="dc"><div class="dcl">VALUE</div><div class="dcv" id="dp-value"></div></div>
    <div class="dc"><div class="dcl">SHARED</div><div class="dcv" id="dp-shared"></div></div>
  </div>
  <div class="dr">
    <div class="ds"><div class="dsl">PARENTS</div><div class="dsv" id="dp-par"></div></div>
    <div class="ds"><div class="dsl">CHILDREN</div><div class="dsv" id="dp-chi"></div></div>
  </div>
</div>
<script>
const NODES={nodes_js};
const EDGES={edges_js};
const LEVELS={level_groups_js};
const NW=145,NH=82,HGAP=28,VGAP=110;
const LCOLORS={{HAZARD:"#ff4d4d",SF:"#ff8c42",FF:"#f5c518",IF:"#4caf7d"}};
const GCOLORS={{OR:"#4fc3f7",AND:"#ffb74d"}};
let scale=1,tx=0,ty=0,pos={{}};
let collapsed=new Set(),selId=null;
let panning=false,panSt={{x:0,y:0}};
let dragId=null,dragOff={{x:0,y:0}},didDrag=false;
const canvas=document.getElementById("canvas");
const vp=document.getElementById("viewport");
const svg=document.getElementById("edges");

// ── layout ────────────────────────────────────────────────────────────
function buildLayout(){{
  const LYS={{HAZARD:30,SF:210,FF:390,IF:570}};
  ["HAZARD","SF","FF","IF"].forEach(lvl=>{{
    const ids=LEVELS[lvl]||[];
    ids.forEach((id,i)=>{{
      pos[id]={{x:60+i*(NW+HGAP),y:LYS[lvl]}};
    }});
  }});
  // centre each hazard over its direct children
  (LEVELS.HAZARD||[]).forEach(hid=>{{
    const kids=EDGES.filter(e=>e[0]===hid).map(e=>e[1]);
    if(kids.length){{
      const xs=kids.map(k=>pos[k]?pos[k].x+NW/2:400);
      pos[hid].x=(xs.reduce((a,b)=>a+b,0)/xs.length)-NW/2;
    }}
  }});
}}

// ── transform ─────────────────────────────────────────────────────────
function applyT(){{
  canvas.style.transform=`translate(${{tx}}px,${{ty}}px) scale(${{scale}})`;
  document.getElementById("zoom-lbl").textContent=Math.round(scale*100)+"%";
  drawEdges();
}}
function zoomBy(d,cx,cy){{
  const vr=vp.getBoundingClientRect();
  cx=cx??vr.width/2; cy=cy??vr.height/2;
  const ns=Math.min(3.5,Math.max(0.1,scale+d)),r=ns/scale;
  tx=cx-r*(cx-tx); ty=cy-r*(cy-ty); scale=ns; applyT();
}}
function resetView(){{
  scale=1;
  const vr=vp.getBoundingClientRect();
  const allX=Object.values(pos).map(p=>p.x+NW);
  const treeW=Math.max(...allX)-Math.min(...Object.values(pos).map(p=>p.x));
  tx=Math.max(20,(vr.width-treeW)/2); ty=20; applyT();
}}
vp.addEventListener("wheel",e=>{{e.preventDefault();zoomBy(e.deltaY<0?0.1:-0.1,e.clientX,e.clientY);}},{{passive:false}});
vp.addEventListener("mousedown",e=>{{
  if(e.button===2){{panning=true;panSt={{x:e.clientX-tx,y:e.clientY-ty}};vp.style.cursor="grabbing";e.preventDefault();}}
}});
window.addEventListener("mousemove",e=>{{
  if(panning){{tx=e.clientX-panSt.x;ty=e.clientY-panSt.y;applyT();}}
  if(dragId){{
    didDrag=true;
    const nx=e.clientX/scale-tx/scale-dragOff.x;
    const ny=e.clientY/scale-ty/scale-dragOff.y;
    pos[dragId]={{x:nx,y:ny}};
    const el=document.getElementById("nw-"+dragId);
    if(el){{el.style.left=nx+"px";el.style.top=ny+"px";}}
    drawEdges();
  }}
}});
window.addEventListener("mouseup",e=>{{
  if(e.button===2){{panning=false;vp.style.cursor="default";}}
  if(e.button===0&&dragId){{
    const id=dragId; dragId=null; document.body.style.userSelect="";
    if(!didDrag) selectNode(id);
  }}
}});
vp.addEventListener("contextmenu",e=>e.preventDefault());

// ── touch pinch ───────────────────────────────────────────────────────
let ltd=null;
vp.addEventListener("touchstart",e=>{{if(e.touches.length===2)ltd=Math.hypot(e.touches[0].clientX-e.touches[1].clientX,e.touches[0].clientY-e.touches[1].clientY);}},{{passive:true}});
vp.addEventListener("touchmove",e=>{{
  if(e.touches.length===2&&ltd){{
    const d=Math.hypot(e.touches[0].clientX-e.touches[1].clientX,e.touches[0].clientY-e.touches[1].clientY);
    const mx=(e.touches[0].clientX+e.touches[1].clientX)/2;
    const my=(e.touches[0].clientY+e.touches[1].clientY)/2;
    zoomBy((d-ltd)*0.008,mx,my); ltd=d; e.preventDefault();
  }}
}},{{passive:false}});

// ── collapse ──────────────────────────────────────────────────────────
function getDesc(id){{
  const r=new Set(),q=[id];
  while(q.length){{const c=q.shift();EDGES.filter(e=>e[0]===c).forEach(e=>{{if(!r.has(e[1])){{r.add(e[1]);q.push(e[1]);}}}});}};
  return r;
}}
function getAnc(id){{
  const r=new Set(),q=[id];
  while(q.length){{const c=q.shift();(NODES[c]?.parentIds||[]).forEach(p=>{{if(!r.has(p)){{r.add(p);q.push(p);}}}});}};
  return r;
}}
function toggleCollapse(e,id){{
  e.stopPropagation();
  collapsed.has(id)?collapsed.delete(id):collapsed.add(id);
  updateVis(); drawEdges();
}}
function updateVis(){{
  const hidden=new Set();
  collapsed.forEach(cid=>getDesc(cid).forEach(d=>hidden.add(d)));
  Object.keys(NODES).forEach(id=>{{
    const el=document.getElementById("nw-"+id);
    if(!el) return;
    el.style.opacity=hidden.has(id)?"0":"1";
    el.style.pointerEvents=hidden.has(id)?"none":"";
  }});
  Object.keys(NODES).forEach(id=>{{
    const btn=document.getElementById("cb-"+id);
    if(btn) btn.textContent=collapsed.has(id)?"+":"-";
  }});
}}
function expandAll(){{collapsed.clear();updateVis();drawEdges();}}
function collapseAll(){{
  Object.keys(NODES).forEach(id=>{{if((NODES[id].children||[]).length)collapsed.add(id);}});
  updateVis();drawEdges();
}}

// ── selection + path highlight ────────────────────────────────────────
function selectNode(id){{
  if(didDrag){{didDrag=false;return;}}
  clearNodeStyles();
  if(selId===id){{selId=null;closeDP();return;}}
  selId=id;
  const node=NODES[id]; if(!node) return;
  const anc=getAnc(id), desc=getDesc(id);
  Object.keys(NODES).forEach(nid=>{{
    const el=document.getElementById("nw-"+nid)?.querySelector(".fn");
    if(!el) return;
    if(nid===id) el.style.boxShadow="0 0 0 3px #e94560,0 0 24px #e9456099";
    else if(anc.has(nid))  el.style.boxShadow="0 0 0 2px #4fc3f7,0 0 14px #4fc3f755";
    else if(desc.has(nid)) el.style.boxShadow="0 0 0 2px #ff8c42,0 0 14px #ff8c4255";
    else el.classList.add("dimmed");
  }});
  drawEdges(new Set([...anc,...desc,id]));
  showDP(id,node);
}}
function clearNodeStyles(){{
  document.querySelectorAll(".fn").forEach(el=>{{el.style.boxShadow="";el.classList.remove("dimmed");}});
}}
function clearHL(){{selId=null;clearNodeStyles();drawEdges();closeDP();}}

// ── detail panel ──────────────────────────────────────────────────────
function showDP(id,node){{
  const p=document.getElementById("dp");
  p.style.display="block"; p.style.borderTopColor=node.color;
  document.getElementById("dp-title").innerHTML=
    `<span style="color:${{node.color}}">${{node.name}}</span>`+
    (node.shared?' <span style="background:#f5c518;color:#111;font-size:8px;padding:1px 5px;border-radius:5px;font-weight:700;">SHARED</span>':'');
  document.getElementById("dp-type").style.color=node.color;
  document.getElementById("dp-type").textContent=node.type;
  document.getElementById("dp-gate").style.color=GCOLORS[node.gate]||"#aaa";
  document.getElementById("dp-gate").textContent=node.gate;
  document.getElementById("dp-value").style.color=node.color;
  document.getElementById("dp-value").textContent=node.value;
  document.getElementById("dp-shared").textContent=node.shared?"YES":"NO";
  document.getElementById("dp-shared").style.color=node.shared?"#f5c518":"#555";
  document.getElementById("dp-par").textContent=node.parents.join(" · ")||"(top event)";
  document.getElementById("dp-chi").textContent=node.childNames.join(" · ")||"(leaf node)";
}}
function closeDP(){{document.getElementById("dp").style.display="none";}}

// ── SVG edges ─────────────────────────────────────────────────────────
function drawEdges(hl){{
  const hidden=new Set();
  collapsed.forEach(cid=>getDesc(cid).forEach(d=>hidden.add(d)));
  let s="";
  EDGES.forEach(([pid,cid])=>{{
    if(hidden.has(cid)||hidden.has(pid)) return;
    const pp=pos[pid],cp=pos[cid]; if(!pp||!cp) return;
    const x1=pp.x+NW/2,y1=pp.y+NH,x2=cp.x+NW/2,y2=cp.y,my=(y1+y2)/2;
    const isHl=hl&&(hl.has(pid)||hl.has(cid));
    s+=`<path d="M${{x1}},${{y1}} C${{x1}},${{my}} ${{x2}},${{my}} ${{x2}},${{y2}}"
      fill="none" stroke="${{isHl?"#4fc3f7":"#252525"}}" stroke-width="${{isHl?2:1.5}}"
      opacity="${{isHl?1:0.8}}"/>`;
  }});
  const maxX=Math.max(...Object.values(pos).map(p=>p.x+NW+80),800);
  const maxY=Math.max(...Object.values(pos).map(p=>p.y+NH+80),600);
  svg.setAttribute("width",maxX); svg.setAttribute("height",maxY);
  svg.style.width=maxX+"px"; svg.style.height=maxY+"px";
  svg.innerHTML=s;
}}

// ── render nodes ──────────────────────────────────────────────────────
function renderNodes(){{
  Object.entries(NODES).forEach(([id,node])=>{{
    const p=pos[id]||{{x:200,y:200}};
    const gc=GCOLORS[node.gate]||"#aaa";
    const hasCh=(node.children||[]).length>0;
    const sb=node.shared?`<span style="background:#f5c518;color:#111;font-size:6px;padding:1px 3px;border-radius:3px;font-weight:700;margin-left:3px;">SHR</span>`:"";
    const wrap=document.createElement("div");
    wrap.id="nw-"+id; wrap.className="nw";
    wrap.style.cssText=`left:${{p.x}}px;top:${{p.y}}px;width:${{NW}}px;`;
    wrap.onmousedown=e=>{{
      if(e.button!==0) return;
      e.stopPropagation(); didDrag=false;
      dragId=id; dragOff={{x:e.clientX/scale-tx/scale-p.x,y:e.clientY/scale-ty/scale-p.y}};
      document.body.style.userSelect="none";
    }};
    wrap.innerHTML=`
      <div class="fn" style="background:${{node.color}};color:${{node.tcolor}};border-color:${{node.color}};width:100%;">
        <div style="font-size:7px;opacity:0.75;letter-spacing:1px;margin-bottom:2px;display:flex;align-items:center;justify-content:center;">
          ${{node.type}}${{sb}}
        </div>
        <div style="font-size:10px;font-weight:700;text-align:center;word-break:break-word;margin-bottom:4px;line-height:1.3;">
          ${{node.name}}
        </div>
        <div style="background:rgba(0,0,0,0.26);border-radius:3px;padding:2px 5px;font-size:11px;font-weight:700;text-align:center;font-family:monospace;">
          ${{node.value}}
        </div>
      </div>
      ${{hasCh?`<div style="display:flex;align-items:center;gap:4px;margin-top:2px;">
        <div class="gt" style="color:${{gc}};border-color:${{gc}};">${{node.gate}}</div>
        <button class="cb" id="cb-${{id}}" onclick="toggleCollapse(event,'${{id}}')">-</button>
      </div>`:"__LEAF__"}}`;
    canvas.appendChild(wrap);
  }});
}}

buildLayout();
renderNodes();
drawEdges();
// centre on load
(function(){{
  const vr=vp.getBoundingClientRect();
  const allX=Object.values(pos).map(p=>p.x+NW);
  const minX=Math.min(...Object.values(pos).map(p=>p.x));
  const treeW=Math.max(...allX)-minX;
  tx=Math.max(20,(vr.width-treeW)/2)-minX; ty=20; applyT();
}})();
</script></body></html>"""

# ── Hierarchy rows ────────────────────────────────────────────────────────
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
        "tree_filter":"ALL"}
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

def set_nodes(n):
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
by_level = {lvl: [n for n in nodes if n["type"] == lvl] for lvl in LEVEL_ORDER}

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
with st.sidebar:

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
                        st.session_state.active_file = fn; st.rerun()
        with c2:
            if st.button("📸 Snapshot", use_container_width=True):
                snap = f"snapshot_{now_str()}.json"
                save_current(filename=snap, status_label=f"Snapshot: {snap}"); st.rerun()
        if configured:
            if st.button("🔄 Refresh", use_container_width=True):
                st.session_state.file_list = list_gist_files(GITHUB_TOKEN, GIST_ID); st.rerun()
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
                            st.session_state.selected_id = None; st.rerun()
                    with cc:
                        if not ia and st.button("Del", key=f"d_{fn}"):
                            del_gist_file(GITHUB_TOKEN, GIST_ID, fn)
                            st.session_state.file_list = list_gist_files(GITHUB_TOKEN, GIST_ID); st.rerun()
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
                            st.session_state.selected_id = None; st.rerun()
                    with cc:
                        if st.button("Del", key=f"d_{fn}"):
                            del_gist_file(GITHUB_TOKEN, GIST_ID, fn); st.session_state.file_list = list_gist_files(GITHUB_TOKEN, GIST_ID); st.rerun()

    st.markdown("---")

    # NODE EDITOR
    st.markdown("### 🔧 NODE EDITOR")
    tab_add, tab_edit = st.tabs(["➕ ADD", "✏️ EDIT"])

    with tab_add:
        # Add Hazard section (always available)
        st.markdown("<div style='font-size:9px;color:#ff4d4d;letter-spacing:2px;margin-bottom:4px;'>ADD HAZARD</div>", unsafe_allow_html=True)
        h_name = st.text_input("Hazard Name", placeholder="e.g. Engine Fire", key="h_name")
        h_val  = st.text_input("Target Rate", placeholder="e.g. 1e-7", key="h_val")
        if st.button("➕ ADD HAZARD", use_container_width=True):
            if h_name.strip():
                try:
                    val = float(h_val)
                    node = {"id":str(uuid.uuid4())[:7],"name":h_name.strip(),
                            "type":"HAZARD","gate":"OR",
                            "targetValue":val,"calculatedValue":val,"parentIds":[]}
                    set_nodes(nodes + [node]); st.rerun()
                except ValueError:
                    st.error("Invalid rate — use e.g. 1e-7")
            else:
                st.error("Enter hazard name")

        if hazards:
            st.markdown("---")
            st.markdown("<div style='font-size:9px;color:#ff8c42;letter-spacing:2px;margin-bottom:4px;'>ADD CHILD NODE</div>", unsafe_allow_html=True)
            node_name = st.text_input("Node Name", placeholder="e.g. Power Failure", key="add_name")
            parent_opts = {f"[{n['type']}] {n['name']}": n["id"]
                           for n in nodes if n["type"] in VALID_PARENT_TYPES}
            sel_labels  = st.multiselect("Parent Node(s)", list(parent_opts.keys()),
                                         help="Multiple = shared node. SF→SF supported.", key="add_par")
            sel_pids    = [parent_opts[l] for l in sel_labels]
            node_type   = st.selectbox("Type", VALID_CHILD_TYPES, key="add_type")
            gate        = st.radio("Gate", ["OR","AND"], horizontal=True, key="add_gate")
            if st.button("✅ ADD NODE", use_container_width=True, type="primary"):
                if not node_name.strip(): st.error("Enter node name")
                elif not sel_pids:       st.error("Select at least one parent")
                else:
                    new_node = {"id":str(uuid.uuid4())[:7],"name":node_name.strip(),
                                "type":node_type,"gate":gate,
                                "targetValue":None,"calculatedValue":None,"parentIds":sel_pids}
                    set_nodes(nodes + [new_node]); st.rerun()

            st.markdown("---")
            del_opts = {f"[{n['type']}] {n['name']}": n["id"]
                        for n in nodes if n["type"] != "HAZARD"}
            if del_opts:
                dl = st.selectbox("Delete Node", ["— select —"] + list(del_opts.keys()))
                if dl != "— select —" and st.button("🗑 DELETE NODE", use_container_width=True):
                    did = del_opts[dl]
                    to_del = {did}; chg = True
                    while chg:
                        chg = False
                        for n in nodes:
                            if n["id"] not in to_del and any(p in to_del for p in (n.get("parentIds") or [])):
                                to_del.add(n["id"]); chg = True
                    set_nodes([n for n in nodes if n["id"] not in to_del]); st.rerun()

            st.markdown("---")
            if st.button("🗑 CLEAR ALL NODES", use_container_width=True):
                set_nodes([]); st.session_state.selected_id = None; st.rerun()

    with tab_edit:
        if not nodes:
            st.markdown("<div style='color:#555;font-size:11px;'>No nodes yet.</div>", unsafe_allow_html=True)
        else:
            edit_opts  = {f"[{n['type']}] {n['name']}": n["id"] for n in nodes}
            edit_label = st.selectbox("Select node to edit", ["— select —"] + list(edit_opts.keys()), key="edit_sel")
            if edit_label != "— select —":
                eid  = edit_opts[edit_label]
                en   = next((n for n in nodes if n["id"] == eid), None)
                if en:
                    color = LEVEL_COLORS.get(en["type"], "#888")
                    st.markdown(f"""<div style="background:#141414;border:2px solid {color};border-radius:8px;padding:8px 12px;margin-bottom:8px;">
                      <div style="font-size:8px;color:#888;letter-spacing:2px;">EDITING</div>
                      <div style="font-weight:700;color:{color};">{en['name']}</div>
                      <div style="font-size:9px;color:#666;">{en['type']} · {en['gate']}</div>
                    </div>""", unsafe_allow_html=True)
                    new_name  = st.text_input("Name", value=en["name"], key="en_name")
                    new_gate  = st.radio("Gate", ["OR","AND"], index=0 if en["gate"]=="OR" else 1, horizontal=True, key="en_gate")
                    if en["type"] != "HAZARD":
                        ti = VALID_CHILD_TYPES.index(en["type"]) if en["type"] in VALID_CHILD_TYPES else 0
                        new_type = st.selectbox("Type", VALID_CHILD_TYPES, index=ti, key="en_type")
                        avail_p  = {f"[{n['type']}] {n['name']}": n["id"]
                                    for n in nodes if n["type"] in VALID_PARENT_TYPES and n["id"] != eid}
                        cur_pl   = [lbl for lbl,pid in avail_p.items() if pid in (en.get("parentIds") or [])]
                        new_pl   = st.multiselect("Parents", list(avail_p.keys()), default=cur_pl, key="en_par")
                        new_pids = [avail_p[l] for l in new_pl]
                    else:
                        new_type = "HAZARD"; new_pids = []
                        new_tgt  = st.text_input("Target Rate", value=str(en.get("targetValue","")), key="en_tgt")
                    if st.button("💾 APPLY CHANGES", use_container_width=True, type="primary"):
                        upd = []
                        for n in nodes:
                            if n["id"] == eid:
                                n = dict(n)
                                n["name"] = new_name.strip() or n["name"]
                                n["gate"] = new_gate
                                if n["type"] != "HAZARD":
                                    n["type"] = new_type; n["parentIds"] = new_pids
                                else:
                                    try:
                                        tv = float(new_tgt); n["targetValue"] = tv; n["calculatedValue"] = tv
                                    except: pass
                            upd.append(n)
                        set_nodes(upd); st.success(f"Updated '{new_name}'"); st.rerun()

# ── Action bar ────────────────────────────────────────────────────────────
a1,a2,a3,a4,a5 = st.columns([1,1,1,1,2])
with a1:
    if st.button("▶ CALCULATE", type="primary", use_container_width=True):
        if nodes:
            new_nodes = recalculate(nodes)
            snap = f"snapshot_{now_str()}.json"
            save_current(new_nodes, filename=snap, status_label=f"Calculated + snap: {snap}")
            save_current(new_nodes)
            st.session_state.nodes = new_nodes; st.rerun()
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
by_level = {lvl:[n for n in nodes if n["type"]==lvl] for lvl in LEVEL_ORDER}

# ── Tabs ──────────────────────────────────────────────────────────────────
tab_tree, tab_hier, tab_vals = st.tabs(["🌳 TREE", "📋 HIERARCHY", "📊 VALUES"])

# ── TAB 1: Tree ───────────────────────────────────────────────────────────
with tab_tree:
    if not nodes:
        st.markdown("<div style='text-align:center;color:#333;margin-top:60px;letter-spacing:2px;'>ADD A HAZARD TO START</div>", unsafe_allow_html=True)
    else:
        # Tree filter selector
        filter_opts = {"Full Tree (all hazards)": "ALL"} | {f"Hazard: {h['name']} ({fmt(h['targetValue'])})": h["id"] for h in hazards}
        filter_label = st.selectbox("View", list(filter_opts.keys()), key="tree_filter_sel",
                                    label_visibility="collapsed")
        filter_id = filter_opts[filter_label]
        fid = None if filter_id == "ALL" else filter_id

        st.markdown("<div style='font-size:9px;color:#444;margin-bottom:4px;'>Scroll=zoom · Right-drag=pan · Left-drag=move · Click=inspect · ▼=collapse</div>", unsafe_allow_html=True)
        tree_html = build_html_tree(nodes, filter_hazard_id=fid)
        components.html(tree_html, height=700, scrolling=False)

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

        show_by_level = {lvl: [n for n in show if n["type"] == lvl] for lvl in LEVEL_ORDER}

        for level in LEVEL_ORDER:
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
        counts = [(lvl, len(show_by_level[lvl])) for lvl in LEVEL_ORDER] + [("TOTAL", len(show))]
        for i,(lvl,cnt) in enumerate(counts):
            with cols[i%5]:
                c = LEVEL_COLORS.get(lvl,"#e94560")
                st.markdown(f"""<div style="background:#141414;border:1px solid {c}44;border-radius:5px;padding:8px;text-align:center;">
                  <div style="font-size:8px;color:#555;letter-spacing:2px;">{lvl}</div>
                  <div style="font-size:18px;font-weight:700;color:{c};">{cnt}</div>
                </div>""", unsafe_allow_html=True)
