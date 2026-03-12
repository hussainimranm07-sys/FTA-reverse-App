import streamlit as st
import json
import math
import os
import requests
import uuid
from datetime import datetime

# ── Page config ──────────────────────────────────────────────────────────
st.set_page_config(
    page_title="FTA Reverse Engineer",
    page_icon="⚠️",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ── Constants ─────────────────────────────────────────────────────────────
LEVEL_ORDER  = ["HAZARD", "SF", "FF", "IF"]
LEVEL_COLORS = {
    "HAZARD": "#ff4d4d",
    "SF":     "#ff8c42",
    "FF":     "#f5c518",
    "IF":     "#4caf7d",
}
# Any non-IF node can be a parent; any non-HAZARD node can be a child
VALID_PARENT_TYPES = ["HAZARD", "SF", "FF"]
VALID_CHILD_TYPES  = ["SF", "FF", "IF"]

# ── Gist helpers ──────────────────────────────────────────────────────────
def gist_headers(token):
    return {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}

def get_gist(token, gist_id):
    """Fetch full gist object."""
    try:
        r = requests.get(f"https://api.github.com/gists/{gist_id}",
                         headers=gist_headers(token), timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None

def list_save_files(token, gist_id):
    """Return sorted list of filenames in gist."""
    gist = get_gist(token, gist_id)
    if not gist:
        return []
    return sorted(gist.get("files", {}).keys())

def load_file_from_gist(token, gist_id, filename):
    """Load a specific file from gist. Returns list or []."""
    gist = get_gist(token, gist_id)
    if not gist:
        return []
    content = gist.get("files", {}).get(filename, {}).get("content", "[]")
    try:
        return json.loads(content)
    except Exception:
        return []

def save_file_to_gist(token, gist_id, filename, nodes):
    """Save nodes to a specific filename in the gist."""
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
    """Delete a file from gist by setting it to null."""
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
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    return f"{v:.2e}"

def now_str():
    return datetime.now().strftime("%Y-%m-%d_%H-%M")

def is_snapshot(name):
    return name.startswith("snapshot_")

def is_active_file(name):
    return not is_snapshot(name)

# ── Session state init ────────────────────────────────────────────────────
defaults = {
    "nodes": [],
    "save_status": "idle",
    "save_msg": "",
    "gist_loaded": False,
    "active_file": "my_tree.json",
    "file_list": [],
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
        # Try to load active file; fall back to first available
        if st.session_state.active_file in st.session_state.file_list:
            st.session_state.nodes = load_file_from_gist(
                GITHUB_TOKEN, GIST_ID, st.session_state.active_file)
        elif st.session_state.file_list:
            active = [f for f in st.session_state.file_list if is_active_file(f)]
            if active:
                st.session_state.active_file = active[0]
                st.session_state.nodes = load_file_from_gist(
                    GITHUB_TOKEN, GIST_ID, active[0])
        st.session_state.gist_loaded = True
        st.session_state.save_status = "loaded"
        st.session_state.save_msg = f"Loaded '{st.session_state.active_file}'"

# ── Save helpers ──────────────────────────────────────────────────────────
def save_current(nodes=None, filename=None, status_label=None):
    if nodes is None:
        nodes = st.session_state.nodes
    if filename is None:
        filename = st.session_state.active_file
    if configured:
        ok = save_file_to_gist(GITHUB_TOKEN, GIST_ID, filename, nodes)
        st.session_state.save_status = "saved" if ok else "error"
        st.session_state.save_msg = (
            status_label or f"Saved to '{filename}' at {datetime.now().strftime('%H:%M:%S')}"
        ) if ok else "❌ Save failed — check Gist credentials"
        # Refresh file list
        st.session_state.file_list = list_save_files(GITHUB_TOKEN, GIST_ID)
        return ok
    else:
        st.session_state.save_status = "no_config"
        st.session_state.save_msg = "Not configured"
        return False

def set_nodes(n, autosave=True):
    st.session_state.nodes = n
    if autosave:
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
    font-weight: 700 !important;
    letter-spacing: 1px !important;
}
.level-header {
    font-size: 9px; letter-spacing: 3px; color: #444;
    margin: 12px 0 6px 0; border-bottom: 1px dashed #1e1e1e;
    padding-bottom: 4px;
}
.shared-badge {
    background: #f5c518; color: #111; font-size: 8px;
    padding: 2px 5px; border-radius: 10px;
    font-weight: 700; margin-left: 4px;
}
.snap-badge {
    background: #0f3460; color: #4fc3f7; font-size: 8px;
    padding: 2px 5px; border-radius: 10px;
    font-weight: 700; margin-left: 4px;
}
</style>
""", unsafe_allow_html=True)

# ── Header ────────────────────────────────────────────────────────────────
status = st.session_state.save_status
save_color = {"saved": "#4caf7d", "loaded": "#4caf7d", "error": "#ff4d4d",
              "no_config": "#f5c518", "idle": "#888"}.get(status, "#888")
save_icon  = {"saved": "✓", "loaded": "⬇", "error": "✗",
              "no_config": "⚠", "idle": "○"}.get(status, "○")

st.markdown(f"""
<div style="background:linear-gradient(90deg,#1a1a2e,#16213e,#0f3460);
            border-bottom:2px solid #e94560;padding:14px 20px;
            margin:-1rem -1rem 1rem -1rem;
            display:flex;justify-content:space-between;align-items:center;">
  <div>
    <div style="font-size:22px;font-weight:700;letter-spacing:2px;color:#e94560;">
      ⚠ FTA REVERSE ENGINEER
    </div>
    <div style="font-size:10px;color:#888;letter-spacing:3px;margin-top:2px;">
      FAULT TREE ANALYSIS · TOP-DOWN DISTRIBUTION
    </div>
  </div>
  <div style="text-align:right;">
    <div style="font-size:13px;color:{save_color};letter-spacing:1px;font-weight:700;">
      {save_icon} {st.session_state.save_msg or "Ready"}
    </div>
    <div style="font-size:10px;color:#555;margin-top:2px;">
      Active file: <span style="color:#aaa;">{st.session_state.active_file}</span>
    </div>
  </div>
</div>
""", unsafe_allow_html=True)

# ── Setup notice ──────────────────────────────────────────────────────────
if not configured:
    st.warning("⚠️ Gist not configured — data won't persist. See README for setup.")

# ── Data ──────────────────────────────────────────────────────────────────
nodes    = st.session_state.nodes
hazard   = next((n for n in nodes if n["type"] == "HAZARD"), None)
by_level = {lvl: [n for n in nodes if n["type"] == lvl] for lvl in LEVEL_ORDER}

# ── Sidebar ───────────────────────────────────────────────────────────────
with st.sidebar:

    # ── FILE MANAGER ──────────────────────────────────────────────────────
    with st.expander("📁 FILE MANAGER", expanded=True):

        # Active file indicator
        st.markdown(f"<div style='font-size:10px;color:#888;margin-bottom:6px;'>"
                    f"Working on: <span style='color:#ff8c42;font-weight:700;'>"
                    f"{st.session_state.active_file}</span></div>", unsafe_allow_html=True)

        # ── Named save ──
        st.markdown("<div style='font-size:9px;color:#555;letter-spacing:2px;margin-bottom:4px;'>SAVE AS NEW FILE</div>", unsafe_allow_html=True)
        new_name = st.text_input("File name", placeholder="e.g. baseline or experiment_1",
                                 key="new_save_name", label_visibility="collapsed")
        if st.button("💾 SAVE AS", use_container_width=True):
            fname = new_name.strip()
            if not fname:
                st.error("Enter a file name")
            else:
                if not fname.endswith(".json"):
                    fname += ".json"
                ok = save_current(filename=fname,
                                  status_label=f"Saved as '{fname}' at {datetime.now().strftime('%H:%M:%S')}")
                if ok:
                    st.session_state.active_file = fname
                    st.success(f"Saved as {fname}")
                    st.rerun()

        # ── Auto snapshot ──
        if st.button("📸 SNAPSHOT NOW", use_container_width=True,
                     help="Save a timestamped backup of current state"):
            snap_name = f"snapshot_{now_str()}.json"
            ok = save_current(filename=snap_name,
                              status_label=f"Snapshot saved: {snap_name}")
            if ok:
                st.success(f"Snapshot: {snap_name}")
                st.rerun()

        st.markdown("---")

        # ── Load file ──
        st.markdown("<div style='font-size:9px;color:#555;letter-spacing:2px;margin-bottom:4px;'>LOAD FILE</div>", unsafe_allow_html=True)

        if configured:
            # Refresh file list
            if st.button("🔄 Refresh file list", use_container_width=True):
                st.session_state.file_list = list_save_files(GITHUB_TOKEN, GIST_ID)
                st.rerun()

            file_list = st.session_state.file_list
            if file_list:
                # Separate named files and snapshots
                named_files = [f for f in file_list if is_active_file(f)]
                snapshots   = [f for f in file_list if is_snapshot(f)]

                if named_files:
                    st.markdown("<div style='font-size:9px;color:#ff8c42;margin:6px 0 3px;'>NAMED FILES</div>", unsafe_allow_html=True)
                    for fname in named_files:
                        is_active = fname == st.session_state.active_file
                        col1, col2, col3 = st.columns([5, 2, 2])
                        with col1:
                            label = f"{'▶ ' if is_active else ''}{fname}"
                            st.markdown(
                                f"<div style='font-size:10px;color:{'#ff8c42' if is_active else '#aaa'};"
                                f"padding:4px 0;overflow:hidden;text-overflow:ellipsis;"
                                f"white-space:nowrap;' title='{fname}'>{label}</div>",
                                unsafe_allow_html=True)
                        with col2:
                            if st.button("Load", key=f"load_{fname}"):
                                data = load_file_from_gist(GITHUB_TOKEN, GIST_ID, fname)
                                st.session_state.nodes = data
                                st.session_state.active_file = fname
                                st.session_state.save_status = "loaded"
                                st.session_state.save_msg = f"Loaded '{fname}'"
                                st.rerun()
                        with col3:
                            if not is_active:
                                if st.button("Del", key=f"del_{fname}"):
                                    delete_file_from_gist(GITHUB_TOKEN, GIST_ID, fname)
                                    st.session_state.file_list = list_save_files(GITHUB_TOKEN, GIST_ID)
                                    st.rerun()

                if snapshots:
                    st.markdown("<div style='font-size:9px;color:#4fc3f7;margin:8px 0 3px;'>SNAPSHOTS</div>", unsafe_allow_html=True)
                    # Show last 5 snapshots
                    for fname in sorted(snapshots, reverse=True)[:5]:
                        col1, col2, col3 = st.columns([5, 2, 2])
                        with col1:
                            short = fname.replace("snapshot_", "").replace(".json", "")
                            st.markdown(
                                f"<div style='font-size:9px;color:#4fc3f7;"
                                f"padding:4px 0;' title='{fname}'>📸 {short}</div>",
                                unsafe_allow_html=True)
                        with col2:
                            if st.button("Load", key=f"load_{fname}"):
                                data = load_file_from_gist(GITHUB_TOKEN, GIST_ID, fname)
                                st.session_state.nodes = data
                                st.session_state.active_file = fname
                                st.session_state.save_status = "loaded"
                                st.session_state.save_msg = f"Loaded snapshot '{fname}'"
                                st.rerun()
                        with col3:
                            if st.button("Del", key=f"del_{fname}"):
                                delete_file_from_gist(GITHUB_TOKEN, GIST_ID, fname)
                                st.session_state.file_list = list_save_files(GITHUB_TOKEN, GIST_ID)
                                st.rerun()
                    if len(snapshots) > 5:
                        st.markdown(f"<div style='font-size:9px;color:#555;'>+ {len(snapshots)-5} older snapshots hidden</div>", unsafe_allow_html=True)
            else:
                st.markdown("<div style='font-size:10px;color:#555;'>No files found in Gist</div>", unsafe_allow_html=True)
        else:
            st.markdown("<div style='font-size:10px;color:#555;'>Configure Gist to enable file management</div>", unsafe_allow_html=True)

    st.markdown("---")

    # ── NODE EDITOR ───────────────────────────────────────────────────────
    st.markdown("### 🔧 NODE EDITOR")

    if not hazard:
        st.markdown("**STEP 1 — TOP EVENT**")
        h_name = st.text_input("Hazard Name", placeholder="e.g. Aircraft Crash")
        h_val  = st.text_input("Target Failure Rate", placeholder="e.g. 1e-7")
        if st.button("➕ ADD HAZARD", use_container_width=True):
            try:
                val = float(h_val)
                node = {
                    "id": str(uuid.uuid4())[:7],
                    "name": h_name.strip(),
                    "type": "HAZARD",
                    "gate": "OR",
                    "targetValue": val,
                    "calculatedValue": val,
                    "parentIds": []
                }
                set_nodes([node])
                st.rerun()
            except ValueError:
                st.error("Invalid value — use format like 1e-7")
    else:
        col = LEVEL_COLORS["HAZARD"]
        st.markdown(f"""
        <div style="background:#141414;border:1px solid {col};border-radius:8px;
                    padding:10px;margin-bottom:12px;">
          <div style="font-size:9px;color:#888;letter-spacing:2px;">TOP EVENT</div>
          <div style="font-weight:700;color:{col};margin:4px 0;">{hazard['name']}</div>
          <div style="font-size:11px;color:#aaa;">
            Target: <span style="color:#fff">{fmt(hazard['targetValue'])}</span>
          </div>
        </div>
        """, unsafe_allow_html=True)

        st.markdown("---")
        st.markdown("**➕ ADD NODE**")

        node_name = st.text_input("Node Name", placeholder="e.g. Power Failure")

        # All non-IF nodes can be parents
        possible_parents = [n for n in nodes if n["type"] in VALID_PARENT_TYPES]
        parent_options   = {f"[{n['type']}] {n['name']}": n["id"] for n in possible_parents}
        sel_labels       = st.multiselect(
            "Parent Node(s)", list(parent_options.keys()),
            help="Ctrl+click for multiple (shared node). SF→SF is supported."
        )
        sel_parent_ids = [parent_options[l] for l in sel_labels]

        # Free choice of child type — no strict enforcement
        node_type = st.selectbox("Node Type", VALID_CHILD_TYPES,
                                 help="SF→SF is allowed for transfer events")
        gate = st.radio("Gate (for this node's children)", ["OR", "AND"], horizontal=True)

        if st.button("✅ ADD NODE", use_container_width=True, type="primary"):
            if not node_name.strip():
                st.error("Enter a node name")
            elif not sel_parent_ids:
                st.error("Select at least one parent")
            else:
                new_node = {
                    "id": str(uuid.uuid4())[:7],
                    "name": node_name.strip(),
                    "type": node_type,
                    "gate": gate,
                    "targetValue": None,
                    "calculatedValue": None,
                    "parentIds": sel_parent_ids
                }
                set_nodes(nodes + [new_node])
                st.rerun()

        st.markdown("---")

        # Delete
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
        if st.button("🗑 CLEAR ALL TREE", use_container_width=True):
            set_nodes([])
            st.rerun()

# ── Main panels ───────────────────────────────────────────────────────────
col_tree, col_vals = st.columns([3, 1])

with col_tree:
    b1, b2, b3 = st.columns([1, 1, 3])
    with b1:
        if st.button("▶ CALCULATE", type="primary", use_container_width=True):
            if nodes:
                new_nodes = recalculate(nodes)
                # Auto snapshot on calculate
                snap_name = f"snapshot_{now_str()}.json"
                save_current(new_nodes, filename=snap_name,
                             status_label=f"Calculated + snapshot saved: {snap_name}")
                save_current(new_nodes)  # also save to active file
                st.session_state.nodes = new_nodes
                st.rerun()
    with b2:
        if st.button("💾 SAVE NOW", use_container_width=True):
            save_current()
            st.rerun()
    with b3:
        st.markdown(
            f"<div style='padding:8px 0;font-size:10px;color:#4caf7d;letter-spacing:1px;'>"
            f"Working on: <b>{st.session_state.active_file}</b> · {len(nodes)} nodes</div>",
            unsafe_allow_html=True
        )

    st.markdown("---")
    st.markdown('<div class="level-header">FAULT TREE VISUALIZATION</div>', unsafe_allow_html=True)

    if not hazard:
        st.markdown(
            "<div style='text-align:center;color:#333;margin-top:60px;letter-spacing:2px;'>"
            "START BY ADDING A HAZARD IN THE SIDEBAR →</div>",
            unsafe_allow_html=True
        )

    for level in LEVEL_ORDER:
        level_nodes = by_level[level]
        if not level_nodes:
            continue

        color  = LEVEL_COLORS[level]
        indent = {"HAZARD": 0, "SF": 20, "FF": 40, "IF": 60}[level]
        st.markdown(
            f'<div class="level-header" style="margin-left:{indent}px">{level}</div>',
            unsafe_allow_html=True
        )

        cols = st.columns(min(len(level_nodes), 4))
        for i, node in enumerate(level_nodes):
            is_shared    = len(node.get("parentIds") or []) > 1
            children     = [n for n in nodes if node["id"] in (n.get("parentIds") or [])]
            parent_names = [n["name"] for n in nodes if n["id"] in (node.get("parentIds") or [])]

            shared_html   = '<span class="shared-badge">SHARED</span>' if is_shared else ""
            children_html = (
                f'<div style="font-size:9px;color:#555;margin-top:3px;">'
                f'↓ {len(children)} child{"ren" if len(children)>1 else ""}</div>'
                if children else ""
            )
            parent_html = (
                f'<div style="font-size:9px;color:#555;">↑ {", ".join(parent_names)}</div>'
                if parent_names and level != "HAZARD" else ""
            )

            with cols[i % 4]:
                st.markdown(f"""
                <div style="background:#141414;border:2px solid {color};border-radius:8px;
                            padding:10px 14px;margin:4px 2px;min-height:100px;">
                  <div style="font-size:9px;letter-spacing:2px;color:{color};">
                    {level} · {node['gate']} {shared_html}
                  </div>
                  <div style="font-weight:700;font-size:12px;color:#ddd;
                              margin:4px 0;word-break:break-word;">{node['name']}</div>
                  <div style="background:#0a0a0a;border-radius:4px;padding:4px 8px;
                              font-size:13px;font-weight:700;color:{color};text-align:center;">
                    {fmt(node.get('calculatedValue'))}
                  </div>
                  {children_html}{parent_html}
                </div>
                """, unsafe_allow_html=True)

        if level != "IF":
            st.markdown(
                "<div style='text-align:center;color:#333;font-size:16px;margin:4px 0;'>▼</div>",
                unsafe_allow_html=True
            )

with col_vals:
    st.markdown("### 📊 VALUES")
    for level in LEVEL_ORDER:
        level_nodes = by_level[level]
        if not level_nodes:
            continue
        color = LEVEL_COLORS[level]
        st.markdown(
            f"<div style='font-size:9px;letter-spacing:3px;color:{color};"
            f"border-bottom:1px solid {color}33;padding-bottom:4px;"
            f"margin:10px 0 6px 0;'>{level}</div>",
            unsafe_allow_html=True
        )
        for node in level_nodes:
            st.markdown(f"""
            <div style="display:flex;justify-content:space-between;padding:4px 6px;
                        border-left:2px solid {color}44;margin-bottom:2px;font-size:10px;">
              <span style="color:#bbb;overflow:hidden;text-overflow:ellipsis;
                           white-space:nowrap;max-width:120px;">{node['name']}</span>
              <span style="font-weight:700;color:{color};flex-shrink:0;margin-left:4px;">
                {fmt(node.get('calculatedValue'))}
              </span>
            </div>
            """, unsafe_allow_html=True)

    if nodes:
        st.markdown("---")
        st.markdown(
            "<div style='font-size:9px;color:#555;letter-spacing:2px;margin-bottom:6px;'>SUMMARY</div>",
            unsafe_allow_html=True
        )
        for lvl in LEVEL_ORDER:
            c = len(by_level[lvl])
            if c:
                st.markdown(
                    f"<div style='display:flex;justify-content:space-between;"
                    f"font-size:10px;padding:2px 0;'>"
                    f"<span style='color:#666;'>{lvl}</span>"
                    f"<span style='color:#aaa;'>{c}</span></div>",
                    unsafe_allow_html=True
                )
        st.markdown(
            f"<div style='border-top:1px solid #222;margin-top:4px;padding-top:4px;"
            f"display:flex;justify-content:space-between;font-size:10px;'>"
            f"<span style='color:#666;'>TOTAL</span>"
            f"<span style='color:#e94560;font-weight:700;'>{len(nodes)}</span></div>",
            unsafe_allow_html=True
        )
