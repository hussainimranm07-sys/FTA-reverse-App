# FTA Reverse Engineer — Streamlit + GitHub Gist Persistence

## How persistence works
Your fault tree is saved as a **private GitHub Gist** (`fta_tree.json`).
Every time you add a node or press Calculate, it auto-saves.
When you reopen the app tomorrow, it loads from the Gist automatically.

---

## Deployment Steps

### 1. Upload files to GitHub
1. Go to **github.com** → **+** → **New repository**
2. Name: `fta-reverse-engineer`, set **Private**
3. Click **Add file → Upload files**
4. Upload: `app.py`, `requirements.txt`
5. Commit

### 2. Create a GitHub Personal Access Token
1. github.com → **Settings** → **Developer settings**
   → **Personal access tokens** → **Tokens (classic)**
2. **Generate new token (classic)**
3. Name: `fta-app`, scope: ✅ **gist** only
4. Generate → **copy the token immediately**

### 3. Create a Secret Gist
1. Go to **gist.github.com**
2. Filename: `fta_tree.json`, content: `[]`
3. Click **Create secret gist**
4. Copy the **Gist ID** from the URL:
   `https://gist.github.com/yourname/` → **`abc123def456`** ← this part

### 4. Deploy on Streamlit Cloud
1. Go to **share.streamlit.io** → sign in with GitHub
2. **New app** → select repo `fta-reverse-engineer`
3. Branch: `main`, file: `app.py`
4. Click **Deploy**

### 5. Add Secrets (critical for persistence)
1. On your app page → **⋮ menu → Settings → Secrets**
2. Paste exactly:
```toml
GITHUB_TOKEN = "ghp_xxxxxxxxxxxxxxxxxxxx"
GIST_ID = "abc123def456yourGistId"
```
3. **Save** — app restarts and loads your tree

---

## That's it!
Your app is now live at:
`https://yourname-fta-reverse-engineer-app-xxxx.streamlit.app`

Bookmark it. Every session auto-saves to your Gist. ✓
