import streamlit as st
import json
import math
import os
import requests
import uuid

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
CHILD_TYPE   = {"HAZARD": "SF", "SF": "FF", "FF": "IF"}
GIST_FILENAME = "fta_tree.json"

# ── Gist helpers ──────────────────────────────────────────────────────────
def gist_headers(token):
    return {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}

def load_from_gist(token, gist_id):
    """Load tree data from GitHub Gist. Returns list of nodes or []."""
    try:
        r = requests.get(
            f"https://api.github.com/gists/{gist_id}",
            headers=gist_headers(token), timeout=10
        )
        if r.status_code == 200:
            content = r.json()["files"].get(GIST_FILENAME, {}).get("content", "[]")
            return json.loads(content)
    except Exception:
        pass
    return []

def save_to_gist(token, gist_id, nodes):
    """Save tree data to GitHub Gist. Returns True on success."""
    try:
        r = requests.patch(
            f"https://api.github.com/gists/{gist_id}",
            headers=gist_headers(token),
            json={"files": {GIST_FILENAME: {"content": json.dumps(nodes, indent=2)}}},
            timeout=10
        )
        return r.status_code == 200
    except Exception:
        return False

def create_gist(token):
    """Create a new private Gist and return its ID."""
    try:
        r = requests.post(
            "https://api.github.com/gists",
            headers=gist_headers(token),
            json={
                "description": "FTA Reverse Engineer — saved tree",
                "public": False,
                "files": {GIST_FILENAME: {"content": "[]"}}
            },
            timeout=10
        )
        if r.status_code == 201:
            return r.json()["id"]
    except Exception:
        pass
    return None

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

# ── Session state init ─────────────────────────────────────────────────────
for key, default in [
    ("nodes", None), ("save_status", "idle"),
    ("gist_loaded", False), ("setup_done", False)
]:
    if key not in st.session_state:
        st.session_state[key] = default

# ── Secrets / config ──────────────────────────────────────────────────────
def get_secret(key):
    try:
        return st.secrets[key]
    except Exception:
        return os.environ.get(key, "")

GITHUB_TOKEN = get_secret("GITHUB_TOKEN")
GIST_ID      = get_secret("GIST_ID")
configured   = bool(GITHUB_TOKEN and GIST_ID)

# ── Load from Gist on first run ───────────────────────────────────────────
if configured and not st.session_state.gist_loaded:
    with st.spinner("Loading saved tree..."):
        st.session_state.nodes = load_from_gist(GITHUB_TOKEN, GIST_ID)
        st.session_state.gist_loaded = True

if st.session_state.nodes is None:
    st.session_state.nodes = []

def get_nodes():
    return st.session_state.nodes

def set_nodes(n, autosave=True):
    st.session_state.nodes = n
    if autosave and configured:
        ok = save_to_gist(GITHUB_TOKEN, GIST_ID, n)
        st.session_state.save_status = "saved" if ok else "error"
    elif not configured:
        st.session_state.save_status = "no_config"

# ── Custom CSS ────────────────────────────────────────────────────────────
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
    background: #f5c518; color: #111;
    font-size: 8px; padding: 2px 5px;
    border-radius: 10px; font-weight: 700; margin-left: 6px;
}
</style>
""", unsafe_allow_html=True)

# ── Header ────────────────────────────────────────────────────────────────
save_color = {"saved": "#4caf7d", "error": "#ff4d4d",
              "no_config": "#f5c518", "idle": "#888"}.get(st.session_state.save_status, "#888")
save_label = {"saved": "✓ SAVED TO GIST", "error": "✗ SAVE FAILED",
              "no_config": "⚠ NOT CONFIGURED", "idle": "○ READY"}.get(st.session_state.save_status, "")

st.markdown(f"""
<div style="background:linear-gradient(90deg,#1a1a2e,#16213e,#0f3460);
            border-bottom:2px solid #e94560;padding:14px 20px;margin:-1rem -1rem 1rem -1rem;
            display:flex;justify-content:space-between;align-items:center;">
  <div>
    <div style="font-size:22px;font-weight:700;letter-spacing:2px;color:#e94560;">
      ⚠ FTA REVERSE ENGINEER
    </div>
    <div style="font-size:10px;color:#888;letter-spacing:3px;margin-top:2px;">
      FAULT TREE ANALYSIS · TOP-DOWN DISTRIBUTION
    </div>
  </div>
  <div style="font-size:11px;color:{save_color};letter-spacing:2px;">{save_label}</div>
</div>
""", unsafe_allow_html=True)

# ── Setup instructions (if not configured) ────────────────────────────────
if not configured:
    st.warning("⚠️ **Gist persistence not configured.** Your tree will reset on page refresh until you complete setup below.")
    with st.expander("📋 ONE-TIME SETUP — Click to configure persistence", expanded=True):
        st.markdown("""
### Step 1 — Create a GitHub Personal Access Token
1. Go to **github.com → Settings → Developer settings → Personal access tokens → Tokens (classic)**
2. Click **Generate new token (classic)**
3. Give it a name: `fta-app`
4. Check the scope: ✅ **gist**
5. Click **Generate token** — **copy it immediately** (shown only once)

---

### Step 2 — Create a Gist
1. Go to **gist.github.com**
2. Create a new **secret gist** with any filename (e.g. `fta_tree.json`) and any content
3. Click **Create secret gist**
4. Copy the **Gist ID** from the URL: `gist.github.com/yourname/`**`THIS_PART`**

---

### Step 3 — Add Secrets to Streamlit Cloud
1. Go to your app on **share.streamlit.io**
2. Click **⋮ → Settings → Secrets**
3. Paste this (replace with your actual values):
```toml
GITHUB_TOKEN = "ghp_your_token_here"
GIST_ID = "your_gist_id_here"
```
4. Click **Save** — the app will restart and load your saved tree automatically

---
        """)

        st.markdown("**Test your credentials here first:**")
        test_token = st.text_input("GitHub Token", type="password", key="test_token")
        test_gist  = st.text_input("Gist ID", key="test_gist")
        if st.button("🔍 Test Connection"):
            if test_token and test_gist:
                data = load_from_gist(test_token, test_gist)
                if data is not None:
                    st.success(f"✅ Connection successful! Gist is accessible.")
                else:
                    st.error("❌ Could not connect. Check your token and Gist ID.")
            else:
                st.warning("Enter both token and Gist ID to test.")

    st.markdown("---")

# ── Main app ──────────────────────────────────────────────────────────────
nodes  = get_nodes()
hazard = next((n for n in nodes if n["type"] == "HAZARD"), None)
by_level = {lvl: [n for n in nodes if n["type"] == lvl] for lvl in LEVEL_ORDER}

# ── Sidebar ───────────────────────────────────────────────────────────────
with st.sidebar:
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

        possible_parents = [n for n in nodes if n["type"] != "IF"]
        parent_options   = {f"[{n['type']}] {n['name']}": n["id"] for n in possible_parents}
        sel_labels       = st.multiselect("Parent Node(s)", list(parent_options.keys()),
                                          help="Select multiple → shared node")
        sel_parent_ids   = [parent_options[l] for l in sel_labels]

        parent_types = list({
            n["type"] for n in nodes if n["id"] in sel_parent_ids
        })
        if len(parent_types) == 1 and parent_types[0] in CHILD_TYPE:
            node_type = st.selectbox("Node Type", [CHILD_TYPE[parent_types[0]]])
        else:
            node_type = st.selectbox("Node Type", ["SF", "FF", "IF"])

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

        # Delete node
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
    b1, b2 = st.columns([1, 3])
    with b1:
        if st.button("▶ CALCULATE", type="primary", use_container_width=True):
            if nodes:
                set_nodes(recalculate(nodes))
                st.rerun()
    with b2:
        st.markdown(
            f"<div style='padding:8px 0;font-size:10px;color:#4caf7d;"
            f"letter-spacing:2px;'>✓ {len(nodes)} nodes · auto-saved to Gist</div>",
            unsafe_allow_html=True
        )

    st.markdown("---")
    st.markdown('<div class="level-header">FAULT TREE VISUALIZATION</div>', unsafe_allow_html=True)

    if not hazard:
        st.markdown(
            "<div style='text-align:center;color:#333;margin-top:60px;"
            "letter-spacing:2px;'>START BY ADDING A HAZARD IN THE SIDEBAR →</div>",
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
            children_html = (f'<div style="font-size:9px;color:#555;margin-top:3px;">'
                             f'↓ {len(children)} child{"ren" if len(children)>1 else ""}</div>'
                             if children else "")
            parent_html   = (f'<div style="font-size:9px;color:#555;">↑ {", ".join(parent_names)}</div>'
                             if parent_names and level != "HAZARD" else "")

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
            "<div style='font-size:9px;color:#555;letter-spacing:2px;"
            "margin-bottom:6px;'>SUMMARY</div>",
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
