#!/usr/bin/env python3
"""
AV Compare — 音视频文件夹对比工具
一个文件搞定：FastAPI 后端 + 内嵌 HTML/CSS/JS 前端 + SQLite
启动: python app.py [--port 8765] [--host 0.0.0.0] [--db av_compare.db]
"""

import argparse
import json
import os
import re
import sqlite3
import mimetypes
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
import uvicorn

# ─────────────────────────── CLI Args ───────────────────────────

parser = argparse.ArgumentParser(description="AV Compare Server")
parser.add_argument("--port", type=int, default=8765)
parser.add_argument("--host", type=str, default="0.0.0.0")
parser.add_argument("--db", type=str, default="my_database.db")
args = parser.parse_args()

def _find_writable_db_path(requested_path: str) -> str:
    """
    SQLite 在 JuiceFS/NFS 等网络文件系统上可能因 journal 文件锁问题无法打开。
    自动检测：先尝试原路径，不行就 fallback 到 /tmp。
    """
    import tempfile
    # 如果是相对路径，基于脚本所在目录解析（避免 cwd 不存在的问题）
    if not os.path.isabs(requested_path):
        script_dir = os.path.dirname(os.path.abspath(__file__))
        requested_path = os.path.join(script_dir, requested_path)
    # 1. 尝试原路径
    try:
        _test = sqlite3.connect(requested_path)
        _test.execute("CREATE TABLE IF NOT EXISTS _db_write_test (id INTEGER)")
        _test.execute("DROP TABLE _db_write_test")
        _test.commit()
        _test.close()
        return requested_path
    except Exception:
        pass
    # 2. Fallback 到 /tmp
    fallback = os.path.join(tempfile.gettempdir(), os.path.basename(requested_path))
    print(f"⚠️  SQLite 无法在 {requested_path} 创建/写入数据库（可能是网络文件系统不支持文件锁）")
    print(f"    自动 fallback 到: {fallback}")
    return fallback

DB_PATH = _find_writable_db_path(args.db)

# ─────────────────────────── Database ───────────────────────────

def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS folders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                path TEXT NOT NULL UNIQUE,
                desc TEXT DEFAULT '',
                content_type TEXT DEFAULT 'wav',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS ratings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                folder_id INTEGER NOT NULL,
                filename TEXT NOT NULL,
                rating INTEGER DEFAULT 0,
                note TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (folder_id) REFERENCES folders(id),
                UNIQUE(folder_id, filename)
            );
            CREATE TABLE IF NOT EXISTS collections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                desc TEXT DEFAULT '',
                folder_ids TEXT DEFAULT '[]',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def row_to_dict(row):
    return dict(row) if row else None

def rows_to_list(rows):
    return [dict(r) for r in rows]

# ─────────────────────────── Helpers ────────────────────────────

def natural_sort_key(s):
    """Natural sort key: splits string into text/number segments."""
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', s)]

def get_folder_name(folder_path: str) -> str:
    p = folder_path.rstrip("/")
    return p.split("/")[-1] if "/" in p else p

AUDIO_EXTS = ('.wav', '.mp3', '.flac', '.aac', '.ogg', '.m4a', '.wma', '.opus')
VIDEO_EXTS = ('.mp4', '.avi', '.mkv', '.webm', '.mov', '.flv', '.wmv', '.m4v')
MEDIA_EXTS = AUDIO_EXTS + VIDEO_EXTS

def scan_media_files(folder_path: str) -> list:
    """Scan folder for audio/video files, naturally sorted."""
    if not os.path.isdir(folder_path):
        return []
    files = [f for f in os.listdir(folder_path)
             if f.lower().endswith(MEDIA_EXTS)]
    files.sort(key=natural_sort_key)
    return files

def load_name2text(folder_path: str) -> dict:
    """Load name2text.json from folder if it exists."""
    fp = os.path.join(folder_path, "name2text.json")
    if os.path.exists(fp):
        try:
            with open(fp, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def load_ratings_from_folder(folder_path: str) -> dict:
    """Load name2json.json (ratings/notes/context/syn_text) from folder if it exists."""
    fp = os.path.join(folder_path, "name2json.json")
    if os.path.exists(fp):
        try:
            with open(fp, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_ratings_to_folder(folder_path: str, folder_id: int):
    """Persist all ratings for a folder to name2json.json in the folder directory."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT filename, rating, note FROM ratings WHERE folder_id = ?", (folder_id,)
        ).fetchall()
    data = {}
    for r in rows:
        entry = {}
        if r["rating"]:
            entry["rating"] = r["rating"]
        if r["note"]:
            entry["note"] = r["note"]
        if entry:
            data[r["filename"]] = entry
    fp = os.path.join(folder_path, "name2json.json")
    try:
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Warning: failed to save name2json.json to {fp}: {e}")

def is_path_under_registered_folder(file_path: str) -> bool:
    """Security check: ensure the file path is under a registered folder."""
    real_path = os.path.realpath(file_path)
    with get_db() as conn:
        folders = conn.execute("SELECT path FROM folders").fetchall()
        for folder in folders:
            folder_real = os.path.realpath(folder["path"])
            if real_path.startswith(folder_real + "/") or real_path == folder_real:
                return True
    return False

# ─────────────────────────── FastAPI App ────────────────────────

app = FastAPI(title="AV Compare")

# ── Folder CRUD ──

@app.post("/api/folders")
async def create_folder(request: Request):
    data = await request.json()
    folder_path = data.get("path", "").strip()
    if not folder_path:
        raise HTTPException(400, "path is required")
    if not os.path.isdir(folder_path):
        raise HTTPException(400, f"Directory not found: {folder_path}")
    name = data.get("name") or get_folder_name(folder_path)
    desc = data.get("desc", "")
    # Detect or use user-provided content type
    files = scan_media_files(folder_path)
    has_video = any(f.lower().endswith(VIDEO_EXTS) for f in files)
    content_type = data.get("content_type", "video" if has_video else "audio")
    if content_type not in ("audio", "video"):
        content_type = "video" if has_video else "audio"
    with get_db() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO folders (name, path, desc, content_type) VALUES (?, ?, ?, ?)",
                (name, folder_path, desc, content_type)
            )
            folder_id = cur.lastrowid
            folder = conn.execute("SELECT * FROM folders WHERE id = ?", (folder_id,)).fetchone()
            # Import existing name2json.json ratings into SQLite
            existing_ratings = load_ratings_from_folder(folder_path)
            for fn, info in existing_ratings.items():
                r = info.get("rating", 0)
                n = info.get("note", "")
                if r or n:
                    conn.execute("""
                        INSERT INTO ratings (folder_id, filename, rating, note)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(folder_id, filename) DO UPDATE SET rating=excluded.rating, note=excluded.note
                    """, (folder_id, fn, r, n))
        except sqlite3.IntegrityError:
            raise HTTPException(400, "Folder already added")
    return JSONResponse(row_to_dict(folder))

@app.get("/api/folders")
async def list_folders():
    with get_db() as conn:
        folders = conn.execute("SELECT * FROM folders ORDER BY created_at DESC").fetchall()
    return JSONResponse(rows_to_list(folders))

@app.delete("/api/folders/{folder_id}")
async def delete_folder(folder_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM ratings WHERE folder_id = ?", (folder_id,))
        conn.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
    return JSONResponse({"ok": True})

@app.put("/api/folders/{folder_id}")
async def update_folder(folder_id: int, request: Request):
    data = await request.json()
    with get_db() as conn:
        folder = conn.execute("SELECT * FROM folders WHERE id = ?", (folder_id,)).fetchone()
        if not folder:
            raise HTTPException(404, "Folder not found")
        name = data.get("name", folder["name"]).strip()
        desc = data.get("desc", folder["desc"]).strip()
        path = data.get("path", folder["path"]).strip()
        if not name:
            raise HTTPException(400, "name cannot be empty")
        if not path:
            raise HTTPException(400, "path cannot be empty")
        conn.execute("UPDATE folders SET name=?, desc=?, path=? WHERE id=?", (name, desc, path, folder_id))
        updated = conn.execute("SELECT * FROM folders WHERE id = ?", (folder_id,)).fetchone()
    return JSONResponse(row_to_dict(updated))

@app.get("/api/folders/{folder_id}/files")
async def get_folder_files(folder_id: int):
    with get_db() as conn:
        folder = conn.execute("SELECT * FROM folders WHERE id = ?", (folder_id,)).fetchone()
    if not folder:
        raise HTTPException(404, "Folder not found")
    folder_dict = row_to_dict(folder)
    files = scan_media_files(folder_dict["path"])
    name2text = load_name2text(folder_dict["path"])
    # Load ratings: prefer name2json.json from folder, fallback to SQLite
    folder_ratings = load_ratings_from_folder(folder_dict["path"])
    with get_db() as conn:
        ratings_rows = conn.execute(
            "SELECT filename, rating, note FROM ratings WHERE folder_id = ?", (folder_id,)
        ).fetchall()
    db_ratings = {r["filename"]: {"rating": r["rating"], "note": r["note"]} for r in ratings_rows}
    # Merge: folder json is base, SQLite overwrites (SQLite is always up-to-date)
    ratings = {}
    all_filenames = set(list(folder_ratings.keys()) + list(db_ratings.keys()))
    for fn in all_filenames:
        fr = folder_ratings.get(fn, {})
        dr = db_ratings.get(fn, {})
        ratings[fn] = {
            "rating": dr.get("rating") or fr.get("rating", 0),
            "note": dr.get("note") or fr.get("note", ""),
        }
    # Extract context and syn_text from name2json.json (ratings)
    name2info = {}
    for fn in files:
        if fn in folder_ratings or fn in db_ratings:
            merged = {}
            fr = folder_ratings.get(fn, {})
            dr = db_ratings.get(fn, {})
            merged.update(fr)
            merged.update(dr)
            if "context" in merged or "syn_text" in merged:
                info = {}
                if "context" in merged:
                    info["context"] = merged["context"]
                if "syn_text" in merged:
                    info["syn_text"] = merged["syn_text"]
                if info:
                    name2info[fn] = info
        else:
            # Also check without extension
            base = fn.rsplit(".", 1)[0] if "." in fn else fn
            if base in folder_ratings or base in db_ratings:
                merged = {}
                fr = folder_ratings.get(base, {})
                dr = db_ratings.get(base, {})
                merged.update(fr)
                merged.update(dr)
                if "context" in merged or "syn_text" in merged:
                    info = {}
                    if "context" in merged:
                        info["context"] = merged["context"]
                    if "syn_text" in merged:
                        info["syn_text"] = merged["syn_text"]
                    if info:
                        name2info[fn] = info

    return JSONResponse({
        "folder": folder_dict,
        "files": files,
        "name2text": name2text,
        "ratings": ratings,
        "name2info": name2info,
    })

# ── Ratings ──

@app.post("/api/ratings")
async def set_rating(request: Request):
    data = await request.json()
    folder_id = data.get("folder_id")
    filename = data.get("filename")
    rating = data.get("rating", 0)
    note = data.get("note", "")
    if not folder_id or not filename:
        raise HTTPException(400, "folder_id and filename required")
    with get_db() as conn:
        conn.execute("""
            INSERT INTO ratings (folder_id, filename, rating, note)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(folder_id, filename) DO UPDATE SET rating=excluded.rating, note=excluded.note
        """, (folder_id, filename, rating, note))
        # Sync to name2json.json in the folder directory
        folder = conn.execute("SELECT path FROM folders WHERE id = ?", (folder_id,)).fetchone()
    if folder:
        save_ratings_to_folder(folder["path"], folder_id)
    return JSONResponse({"ok": True})

@app.get("/api/ratings/{folder_id}")
async def get_ratings(folder_id: int):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT filename, rating, note FROM ratings WHERE folder_id = ?", (folder_id,)
        ).fetchall()
    return JSONResponse({r["filename"]: {"rating": r["rating"], "note": r["note"]} for r in rows})

# ── Collections ──

@app.post("/api/collections")
async def create_collection(request: Request):
    data = await request.json()
    name = data.get("name", "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    desc = data.get("desc", "")
    folder_ids = json.dumps(data.get("folder_ids", []))
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO collections (name, desc, folder_ids) VALUES (?, ?, ?)",
            (name, desc, folder_ids)
        )
        coll = conn.execute("SELECT * FROM collections WHERE id = ?", (cur.lastrowid,)).fetchone()
    return JSONResponse(row_to_dict(coll))

@app.get("/api/collections")
async def list_collections():
    with get_db() as conn:
        colls = conn.execute("SELECT * FROM collections ORDER BY created_at DESC").fetchall()
    result = []
    for c in colls:
        d = row_to_dict(c)
        d["folder_ids"] = json.loads(d["folder_ids"])
        result.append(d)
    return JSONResponse(result)

@app.get("/api/collections/{coll_id}")
async def get_collection(coll_id: int):
    with get_db() as conn:
        coll = conn.execute("SELECT * FROM collections WHERE id = ?", (coll_id,)).fetchone()
    if not coll:
        raise HTTPException(404, "Collection not found")
    d = row_to_dict(coll)
    d["folder_ids"] = json.loads(d["folder_ids"])
    return JSONResponse(d)

@app.put("/api/collections/{coll_id}")
async def update_collection(coll_id: int, request: Request):
    data = await request.json()
    with get_db() as conn:
        coll = conn.execute("SELECT * FROM collections WHERE id = ?", (coll_id,)).fetchone()
        if not coll:
            raise HTTPException(404, "Collection not found")
        name = data.get("name", coll["name"])
        desc = data.get("desc", coll["desc"])
        folder_ids = json.dumps(data.get("folder_ids", json.loads(coll["folder_ids"])))
        conn.execute(
            "UPDATE collections SET name=?, desc=?, folder_ids=? WHERE id=?",
            (name, desc, folder_ids, coll_id)
        )
        updated = conn.execute("SELECT * FROM collections WHERE id = ?", (coll_id,)).fetchone()
    d = row_to_dict(updated)
    d["folder_ids"] = json.loads(d["folder_ids"])
    return JSONResponse(d)

@app.delete("/api/collections/{coll_id}")
async def delete_collection(coll_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM collections WHERE id = ?", (coll_id,))
    return JSONResponse({"ok": True})

# ── File serving with Range support ──

@app.get("/files/{file_path:path}")
async def serve_file(file_path: str, request: Request):
    if not file_path.startswith("/"):
        file_path = "/" + file_path
    file_path = os.path.realpath(file_path)

    if not os.path.isfile(file_path):
        raise HTTPException(404, "File not found")
    if not is_path_under_registered_folder(file_path):
        raise HTTPException(403, "Access denied: path not under any registered folder")

    file_size = os.path.getsize(file_path)
    content_type, _ = mimetypes.guess_type(file_path)
    fl = file_path.lower()
    if fl.endswith(('.wav',)):
        content_type = 'audio/wav'
    elif fl.endswith(('.mp3',)):
        content_type = 'audio/mpeg'
    elif fl.endswith(('.flac',)):
        content_type = 'audio/flac'
    elif fl.endswith(('.ogg',)):
        content_type = 'audio/ogg'
    elif fl.endswith(('.m4a',)):
        content_type = 'audio/mp4'
    elif fl.endswith(('.mp4',)):
        content_type = 'video/mp4'
    elif fl.endswith(('.webm',)):
        content_type = 'video/webm'
    elif fl.endswith(('.mov',)):
        content_type = 'video/quicktime'
    elif fl.endswith(('.avi',)):
        content_type = 'video/x-msvideo'
    elif fl.endswith(('.mkv',)):
        content_type = 'video/x-matroska'
    content_type = content_type or 'application/octet-stream'

    range_header = request.headers.get("range")

    if range_header:
        # Parse range header
        m = re.match(r'bytes=(\d+)-(\d*)', range_header)
        if not m:
            raise HTTPException(416, "Invalid range")
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else file_size - 1
        end = min(end, file_size - 1)
        if start > end or start >= file_size:
            raise HTTPException(416, "Range not satisfiable")
        length = end - start + 1

        def iter_range():
            with open(file_path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return StreamingResponse(
            iter_range(),
            status_code=206,
            media_type=content_type,
            headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(length),
            },
        )
    else:
        def iter_file():
            with open(file_path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    yield chunk

        return StreamingResponse(
            iter_file(),
            media_type=content_type,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Length": str(file_size),
            },
        )

# ── Frontend ──

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AV Compare</title>
<style>
:root {
    --bg: #f5f5f5;
    --bg2: #ffffff;
    --bg3: #e8e8e8;
    --text: #1a1a2e;
    --text2: #666680;
    --accent: #e94560;
    --accent2: #7c3aed;
    --border: #d0d0d8;
    --success: #4caf50;
    --warn: #ff9800;
    --overlay-bg: rgba(0,0,0,0.3);
}
:root.dark {
    --bg: #1a1a2e;
    --bg2: #16213e;
    --bg3: #0f3460;
    --text: #e0e0e0;
    --text2: #a0a0b0;
    --accent: #e94560;
    --accent2: #533483;
    --border: #2a2a4a;
    --success: #4caf50;
    --warn: #ff9800;
    --overlay-bg: rgba(0,0,0,0.7);
}

.help-icon:hover {
    background: var(--accent);
    color: #fff;
}


.theme-toggle {
    margin-left: auto;
    background: none;
    border: 1px solid var(--border);
    color: var(--text);
    padding: 6px 12px;
    border-radius: 20px;
    cursor: pointer;
    font-size: 13px;
    transition: all 0.2s;
    display: flex;
    align-items: center;
    gap: 4px;
    white-space: nowrap;
}
.theme-toggle:hover { border-color: var(--accent); color: var(--accent); opacity: 1; }
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
}
a { color: var(--accent); text-decoration: none; }

/* ── Header / Tabs ── */
.header {
    background: var(--bg2);
    border-bottom: 1px solid var(--border);
    padding: 0 24px;
    display: flex;
    align-items: center;
    gap: 0;
}
.header h1 {
    font-size: 18px;
    margin-right: 32px;
    padding: 16px 0;
    color: var(--accent);
}
.tab-btn {
    padding: 16px 20px;
    background: none;
    border: none;
    color: var(--text2);
    font-size: 14px;
    cursor: pointer;
    border-bottom: 2px solid transparent;
    transition: all 0.2s;
}
.tab-btn:hover { color: var(--text); }
.tab-btn.active {
    color: var(--accent);
    border-bottom-color: var(--accent);
}
.tab-content { display: none; padding: 24px; }
.tab-content.active { display: block; }

/* ── Common ── */
.card {
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 20px;
    margin-bottom: 16px;
}
.input-row {
    display: flex;
    gap: 12px;
    margin-bottom: 16px;
    flex-wrap: wrap;
}
input[type="text"], textarea {
    background: var(--bg);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 10px 14px;
    border-radius: 6px;
    font-size: 14px;
    outline: none;
    flex: 1;
    min-width: 200px;
}
input[type="text"]:focus, textarea:focus {
    border-color: var(--accent);
}
button {
    background: var(--accent);
    color: #fff;
    border: none;
    padding: 10px 20px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 14px;
    transition: opacity 0.2s;
    white-space: nowrap;
}
button:hover { opacity: 0.85; }
button.secondary {
    background: var(--bg3);
    color: var(--text);
    border: 1px solid var(--border);
}
:root:not(.dark) button.secondary {
    background: #f0f0f4;
    color: #444;
    border: 1px solid #ccc;
}
:root:not(.dark) button.secondary:hover {
    background: #e0e0e8;
    border-color: #aaa;
}
button.danger {
    background: #c62828;
}
button.small {
    padding: 4px 10px;
    font-size: 12px;
}

/* ── Folder list ── */
.folder-item {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    padding: 12px 16px;
    border-bottom: 1px solid var(--border);
    gap: 16px;
}
.folder-item:last-child { border-bottom: none; }
.folder-item .actions { display: flex; gap: 6px; flex-shrink: 0; }
.folder-info { flex: 1; min-width: 0; }
.folder-info .name { font-weight: 600; font-size: 15px; }
.folder-info .path {
    font-size: 12px;
    color: var(--text2);
    word-break: break-all;
    margin-top: 2px;
}
.folder-info .desc-badge {
    display: inline-block;
    margin-top: 4px;
    padding: 2px 10px;
    background: linear-gradient(135deg, #7c3aed, #a855f7);
    color: #fff;
    border-radius: 12px;
    font-size: 12px;
    line-height: 1.4;
    max-width: 100%;
    word-break: break-word;
}
:root.dark .folder-info .desc-badge {
    background: #533483;
}
.folder-info .path {
    font-size: 11px;
    color: var(--text2);
    word-break: break-all;
    margin-top: 4px;
    font-family: ui-monospace, 'Cascadia Code', monospace;
    opacity: 0.7;
}
.folder-info .meta {
    font-size: 11px;
    color: var(--text2);
    margin-top: 4px;
}

/* ── Compare View ── */
.compare-layout {
    display: flex;
    gap: 20px;
    align-items: flex-start;
}
.compare-sidebar {
    min-width: 260px;
    max-width: 300px;
    flex-shrink: 0;
}
.compare-main {
    flex: 1;
    overflow-x: auto;
    min-width: 0;
}
.folder-check-item {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    padding: 10px 12px;
    border-bottom: 1px solid var(--border);
    cursor: pointer;
}
.folder-check-item input[type="checkbox"] { margin-top: 4px; flex-shrink: 0; }
.folder-check-item:hover { background: rgba(255,255,255,0.03); }
.folder-check-item label {
    cursor: pointer;
    font-size: 13px;
    word-break: break-all;
    flex: 1;
    line-height: 1.4;
}
.folder-check-item .fc-folder-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
}
.folder-check-item .fc-folder-name { font-weight: 500; }
.folder-check-item .fc-folder-time { font-size: 11px; color: var(--accent); font-family: monospace; flex-shrink: 0; }
.folder-check-item .fc-folder-desc { color: var(--text2); font-size: 12px; display: block; margin-top: 2px; }

/* Compare panels */
.compare-panels {
    display: flex;
    gap: 16px;
    overflow-x: auto;
    padding-bottom: 16px;
    scroll-snap-type: x proximity;
}
.panel-resize-handle {
    position: absolute;
    right: -8px;
    top: 0;
    bottom: 0;
    width: 16px;
    cursor: col-resize;
    z-index: 10;
    display: flex;
    align-items: center;
    justify-content: center;
}
.panel-resize-handle::after {
    content: '';
    width: 4px;
    height: 40px;
    background: var(--border);
    border-radius: 2px;
    transition: all 0.2s;
}
.panel-resize-handle:hover::after,
.panel-resize-handle.dragging::after {
    background: var(--accent);
    height: 60px;
}
.panel-resize-handle.dragging {
    pointer-events: auto;
}
.compare-panel {
    width: 380px;
    min-width: 280px;
    max-width: 100%;
    flex-shrink: 0;
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    position: relative;
}
.panel-header {
    background: var(--bg3);
    padding: 12px 16px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    border-bottom: 2px solid var(--accent);
    position: sticky;
    top: 0;
    z-index: 2;
}
.panel-title-row {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
    flex: 1;
}
.panel-name {
    font-weight: 700;
    font-size: 15px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    color: var(--accent);
}
.panel-desc {
    display: block;
    font-size: 11px;
    color: var(--text2);
    line-height: 1.4;
    margin-top: 2px;
    white-space: normal;
    word-break: break-word;
    max-width: 100%;
    padding: 2px 8px;
    background: var(--bg);
    border-radius: 4px;
    border-left: 2px solid var(--accent2);
}
.panel-end {
    font-size: 11px;
    color: var(--text2);
    font-family: monospace;
    opacity: 0.7;
    margin-top: 2px;
}
.panel-end {
    font-size: 11px;
    color: var(--text2);
}
.panel-actions {
    display: flex;
    gap: 4px;
    flex-shrink: 0;
}
.panel-actions button {
    background: none;
    border: 1px solid transparent;
    cursor: pointer;
    font-size: 14px;
    padding: 2px 6px;
    border-radius: 3px;
    opacity: 0.5;
    transition: all 0.2s;
}
.panel-actions button:hover { opacity: 1; border-color: var(--border); background: var(--bg); }
.panel-actions .btn-close:hover { opacity: 1; border-color: #c62828; background: #fff3f3; }
.panel-files {
    max-height: calc(100vh - 260px);
    overflow-y: auto;
    padding: 8px 0;
}
.panel-file-item {
    padding: 10px 16px;
    border-bottom: 1px solid var(--border);
    word-wrap: break-word;
    overflow-wrap: break-word;
}
.panel-file-item:last-child { border-bottom: none; }
.panel-file-item:hover { background: rgba(255,255,255,0.02); }
.note-input {
    background: var(--bg);
    border: 1px solid var(--border);
    color: var(--text);
    font-size: 12px;
    padding: 6px 8px;
    border-radius: 4px;
    width: 100%;
    min-height: 32px;
    max-height: 120px;
    resize: none;
    overflow-y: auto;
    word-wrap: break-word;
    overflow-wrap: break-word;
    line-height: 1.5;
    outline: none;
    font-family: inherit;
}
.note-input:focus {
    border-color: var(--accent);
}

/* Compare table */
.compare-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
}
.compare-table th {
    background: var(--bg3);
    color: var(--text);
    padding: 10px 12px;
    text-align: left;
    position: sticky;
    top: 0;
    z-index: 2;
    font-size: 12px;
    border-bottom: 2px solid var(--accent);
    vertical-align: top;
}
.compare-folder-header {
    display: flex;
    align-items: center;
    gap: 8px;
    position: relative;
}
.compare-folder-header .drag-handle {
    cursor: grab;
    font-size: 16px;
    opacity: 0.3;
    flex-shrink: 0;
    user-select: none;
    padding: 0 2px;
}
.compare-folder-header .drag-handle:hover { opacity: 0.7; }
.compare-folder-header .folder-title-group {
    display: flex;
    flex-direction: column;
    gap: 2px;
    flex: 1;
    min-width: 0;
}
.compare-folder-header .folder-name {
    font-weight: 600;
    font-size: 13px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.compare-folder-header .folder-actions {
    display: flex;
    gap: 2px;
    flex-shrink: 0;
}
.compare-folder-header .folder-actions button {
    background: none;
    border: 1px solid transparent;
    cursor: pointer;
    font-size: 14px;
    padding: 2px 4px;
    border-radius: 3px;
    opacity: 0.3;
    transition: all 0.2s;
    line-height: 1;
}
.compare-folder-header .folder-actions button:hover { opacity: 1; border-color: var(--border); background: var(--bg); }
.compare-folder-header .folder-actions .btn-close:hover { opacity: 1; border-color: #c62828; background: #fff3f3; }
.compare-folder-desc { color: var(--text2); font-size: 11px; font-weight: normal; white-space: normal; }

/* Dragging state */
th.dragging { opacity: 0.5; }
th.drag-over { border-left: 3px solid var(--accent) !important; }
.compare-table td {
    padding: 8px 12px;
    border-bottom: 1px solid var(--border);
    vertical-align: top;
    min-width: 280px;
}
.compare-table tr:hover td {
    background: rgba(255,255,255,0.02);
}
.file-label {
    font-weight: 600;
    font-size: 12px;
    color: var(--accent);
    word-break: break-all;
}
.file-text {
    font-size: 11px;
    color: var(--text2);
    margin-top: 4px;
    line-height: 1.4;
    max-height: 60px;
    overflow-y: auto;
}
.file-context, .file-syn-text {
    background: var(--bg3);
    padding: 4px 8px;
    border-radius: 4px;
    margin-top: 4px;
    font-size: 11px;
    line-height: 1.5;
    max-height: 80px;
    overflow-y: auto;
}
.file-context strong, .file-syn-text strong {
    color: var(--accent);
    font-size: 10px;
    margin-right: 4px;
}
.cell-content { display: flex; flex-direction: column; gap: 4px; }
.rating-note-row { display: flex; align-items: center; gap: 8px; }
.rating-note-row .rating-row { flex-shrink: 0; }
.rating-note-row .note-input { flex: 1; min-width: 80px; }
audio { width: 100%; min-width: 250px; height: 40px; }
video { width: 100%; min-width: 250px; max-width: 400px; border-radius: 4px; }
.rating-row { display: flex; gap: 2px; align-items: center; }
.star-btn {
    background: none;
    border: none;
    cursor: pointer;
    padding: 2px;
    transition: all 0.15s;
    color: #ccc;
    display: inline-flex;
    align-items: center;
}
.star-btn svg {
    width: 20px;
    height: 20px;
}
.star-btn.active {
    color: #f59e0b;
}
:root.dark .star-btn {
    color: #555;
}
:root.dark .star-btn.active {
    color: #f59e0b;
}
.star-btn:hover { transform: scale(1.2); }
.note-input {
    background: var(--bg);
    border: 1px solid var(--border);
    color: var(--text2);
    font-size: 11px;
    padding: 4px 8px;
    border-radius: 4px;
    width: 100%;
    outline: none;
}
.note-input:focus { border-color: var(--accent); color: var(--text); }

/* ── Collection ── */
.coll-list { display: flex; flex-direction: column; gap: 0; }
.coll-list-item {
    background: var(--bg2);
    border-bottom: 1px solid var(--border);
    padding: 16px 20px;
    cursor: pointer;
    transition: background 0.15s;
    display: flex;
    align-items: center;
    gap: 16px;
}
.coll-list-item:first-child { border-top: 1px solid var(--border); border-radius: 8px 8px 0 0; }
.coll-list-item:last-child { border-radius: 0 0 8px 8px; }
.coll-list-item:hover { background: var(--bg3); }
.coll-list-item .coll-row-main { flex: 1; min-width: 0; }
.coll-list-item .coll-row-header {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 4px;
}
.coll-list-item .coll-row-name { font-size: 15px; font-weight: 600; }
.coll-list-item .coll-row-desc { color: var(--text2); font-size: 13px; }
.coll-list-item .coll-row-tags {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-top: 6px;
}
.coll-folder-tag {
    background: var(--bg3);
    padding: 3px 10px;
    border-radius: 12px;
    font-size: 12px;
    display: inline-flex;
    align-items: center;
    gap: 4px;
}
.coll-list-item .coll-row-actions {
    display: flex;
    gap: 4px;
    flex-shrink: 0;
    opacity: 0;
    transition: opacity 0.2s;
}
.coll-list-item:hover .coll-row-actions,
.coll-list-item.touch-active .coll-row-actions { opacity: 1; }

/* ── Toast ── */
.toast {
    position: fixed;
    bottom: 24px;
    right: 24px;
    background: var(--success);
    color: #fff;
    padding: 12px 20px;
    border-radius: 8px;
    font-size: 14px;
    opacity: 0;
    transition: opacity 0.3s;
    z-index: 9999;
    pointer-events: none;
}
.toast.show { opacity: 1; }
.toast.error { background: #c62828; }

/* ── Loading ── */
.loading {
    text-align: center;
    padding: 40px;
    color: var(--text2);
    font-size: 14px;
}

/* ── Responsive ── */
@media (max-width: 768px) {
    .compare-layout { flex-direction: column; }
    .compare-sidebar { max-width: 100%; min-width: auto; }
    .header { flex-wrap: wrap; }
    .header h1 { margin-right: 16px; }
}

/* ── Readonly mode ── */
body.readonly .add-folder-card,
body.readonly button.danger,
body.readonly button[onclick*="addFolder"],
body.readonly button[onclick*="deleteFolder"],
body.readonly button[onclick*="editFolder"],
body.readonly button[onclick*="createCollection()"],
body.readonly button[onclick*="deleteCollection"],
body.readonly button[onclick*="editCollectionPrompt"],
body.readonly button[onclick*="createCollectionFromCompare"],
body.readonly #tab-folders .input-row,
body.readonly #tab-collections .input-row,
body.readonly .coll-card .coll-actions,
body.readonly .edit-folder-btn,
body.readonly button[onclick*="editFolderFromCompare"],
body.readonly button[onclick*="addFolderPrompt"],
body.readonly .coll-edit-form input {
    pointer-events: none;
    opacity: 0.6;
}
body.readonly .star-btn {
    pointer-events: none;
    cursor: default;
}
body.readonly .note-input {
    pointer-events: none;
    opacity: 0.6;
}

/* ── Share button ── */
.share-btn {
    background: none;
    border: 1px solid var(--border);
    color: var(--text2);
    padding: 4px 10px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 12px;
    transition: all 0.2s;
    white-space: nowrap;
}
.share-btn:hover {
    border-color: var(--accent);
    color: var(--accent);
}

/* Compare top bar */
.compare-topbar {
    margin-bottom: 16px;
}
.coll-edit-form {
    display: flex;
    align-items: flex-end;
    gap: 16px;
    flex-wrap: wrap;
}
.coll-field {
    display: flex;
    flex-direction: column;
    gap: 4px;
}
.coll-field-name { min-width: 180px; }
.coll-field-desc { min-width: 260px; max-width: 400px; flex: 1; }
.coll-label {
    font-size: 12px;
    color: var(--text2);
    font-weight: 500;
}
.coll-field input[type="text"] {
    margin: 0;
    padding: 8px 12px;
    font-size: 14px;
    border-radius: 6px;
    width: 100%;
    box-sizing: border-box;
}
.compare-help {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 22px;
    height: 22px;
    border-radius: 50%;
    background: var(--bg3);
    color: var(--text2);
    font-size: 13px;
    cursor: help;
    position: relative;
    margin-right: 8px;
    flex-shrink: 0;
    transition: all 0.2s;
}
.compare-help:hover {
    background: var(--accent);
    color: #fff;
}
.compare-help::after {
    content: attr(data-tip);
    position: absolute;
    left: 50%;
    top: calc(100% + 8px);
    transform: translateX(-50%);
    padding: 10px 14px;
    background: var(--bg2);
    color: var(--text);
    border: 1px solid var(--accent);
    border-radius: 8px;
    font-size: 12px;
    line-height: 1.6;
    white-space: pre-line;
    width: 300px;
    z-index: 1000;
    opacity: 0;
    pointer-events: none;
    transition: opacity 0.2s;
    box-shadow: 0 4px 12px rgba(0,0,0,0.15);
}
.compare-help:hover::after {
    opacity: 1;
}
.coll-actions {
    display: flex;
    align-items: center;
    gap: 4px;
    margin-left: auto;
    padding-bottom: 1px;
}
.btn-save {
    background: var(--accent);
    color: #fff;
    border: none;
    padding: 8px 18px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 13px;
    font-weight: 500;
    white-space: nowrap;
    transition: opacity 0.2s;
}
.btn-save:hover { opacity: 0.85; }
.btn-sep {
    color: var(--border);
    font-size: 16px;
    margin: 0 6px;
    user-select: none;
}
.btn-secondary {
    background: none;
    border: 1px solid var(--border);
    color: var(--text2);
    padding: 7px 14px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 13px;
    white-space: nowrap;
    transition: all 0.2s;
}
.btn-secondary:hover { border-color: var(--accent); color: var(--accent); }
.compare-topbar .active-folders {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    flex: 1;
}
.compare-topbar .active-folder-tag {
    background: var(--bg3);
    padding: 4px 10px;
    border-radius: 12px;
    font-size: 12px;
    display: flex;
    align-items: center;
    gap: 4px;
}
.compare-topbar .active-folder-tag .remove {
    cursor: pointer;
    opacity: 0.5;
}
.compare-topbar .active-folder-tag .remove:hover { opacity: 1; }

/* Compare add-folder overlay */
.compare-folder-overlay {
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: var(--overlay-bg, rgba(0,0,0,0.5));
    z-index: 100;
    display: flex;
    align-items: center;
    justify-content: center;
}
.compare-folder-overlay .overlay-card {
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 24px;
    max-width: 500px;
    width: 90%;
    max-height: 80vh;
    overflow-y: auto;
}
.compare-folder-overlay .selected-tags {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-bottom: 12px;
}
.compare-folder-overlay .selected-tag {
    background: var(--accent);
    color: #fff;
    padding: 4px 12px;
    border-radius: 12px;
    font-size: 12px;
    display: flex;
    align-items: center;
    gap: 4px;
}
.compare-folder-overlay .selected-tag .remove { cursor: pointer; opacity: 0.7; }
.compare-folder-overlay .selected-tag .remove:hover { opacity: 1; }

/* Empty state */
.empty-state {
    text-align: center;
    padding: 40px 20px;
    color: var(--text2);
}
.empty-state .icon { font-size: 48px; margin-bottom: 12px; }

/* Pagination / filter */
.toolbar {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 12px;
    flex-wrap: wrap;
}
.toolbar .count {
    font-size: 12px;
    color: var(--text2);
}

/* Scroll wrapper for table */
.table-wrapper {
    overflow-x: auto;
    max-height: 80vh;
    overflow-y: auto;
}
</style>
</head>
<body>

<div class="header">
    <h1>📂 AV Compare</h1>
    <button class="tab-btn active" onclick="switchTab('collections')">Collection</button>
    <button class="tab-btn" onclick="switchTab('compare')">对比视图</button>
    <button class="tab-btn" onclick="switchTab('folders')">文件夹管理</button>
    <button class="theme-toggle" id="theme-toggle" onclick="toggleTheme()">🌙 夜间</button>
</div>

<!-- ═══════════════ Tab 1: Collection ═══════════════ -->
<div id="tab-collections" class="tab-content active">
    <div style="display:flex; gap:12px; align-items:center; margin-bottom:16px;">
        <button onclick="startNewCollection()" style="font-size:15px; padding:10px 20px;">＋ 新建 Collection</button>
    </div>
    <input type="text" id="coll-search" placeholder="搜索 Collection 名称或描述..." style="width:100%;margin-bottom:16px;" oninput="onCollSearchInput()" />
    <div id="collections-list"></div>
</div>

<!-- ═══════════════ Tab 2: 对比视图 ═══════════════ -->
<div id="tab-compare" class="tab-content">
    <div class="compare-topbar">
        <div class="coll-edit-form" id="coll-edit-form">
            <div class="coll-field coll-field-name">
                <label class="coll-label">名称</label>
                <input type="text" id="compare-coll-name" placeholder="Collection 名称" />
            </div>
            <div class="coll-field coll-field-desc">
                <label class="coll-label">描述</label>
                <input type="text" id="compare-coll-desc" placeholder="可选描述" />
            </div>
            <div class="coll-actions">
                <span class="compare-help" data-tip="＋ 添加：从已注册的文件夹中选择，加入对比面板。
📂 新建：添加服务器上的文件夹路径。

每个文件夹以独立面板展示，支持播放、打分和备注。
增删文件夹会自动保存当前 Collection。">❓</span>
                <button class="btn-secondary" onclick="showCompareFolderPicker()">＋ 添加</button>
                <button class="btn-secondary" onclick="addFolderPrompt()">📂 新建</button>
            </div>
        </div>
    </div>
    <div class="compare-main" style="margin-top: 0;">
        <div id="compare-toolbar" class="toolbar" style="display:none;">
            <span class="count" id="compare-count"></span>
            <input type="text" id="compare-filter" placeholder="搜索文件名..." style="max-width: 240px; flex: none;" oninput="filterCompare()" />
        </div>
        <div id="compare-content">
            <div class="empty-state">
                <div class="icon">🎧</div>
                <p>点击上方「＋ 添加文件夹」开始对比</p>
            </div>
        </div>
    </div>
</div>

<!-- ═══════════════ Tab 3: 文件夹管理 ═══════════════ -->
<div id="tab-folders" class="tab-content">
    <div class="card" style="display:flex; align-items:center; justify-content:space-between;">
        <h3>文件夹管理</h3>
        <button onclick="addFolderPrompt()" style="font-size:14px; padding:6px 14px;">＋ 新建文件夹</button>
    </div>
    <div style="display:flex; gap:12px; margin-bottom:16px; align-items:center;">
        <select id="folder-type-filter" onchange="renderFolderList()" style="background:var(--bg);border:1px solid var(--border);color:var(--text);padding:6px 12px;border-radius:6px;font-size:13px;">
            <option value="all">全部</option>
            <option value="audio">🎵 仅音频</option>
            <option value="video">🎬 仅视频</option>
        </select>
        <input type="text" id="folder-search" placeholder="搜索文件夹名称、路径或描述..." style="flex:1;" oninput="renderFolderList()" />
    </div>
    <div id="folder-list"></div>
</div>

<div id="toast" class="toast"></div>

<script>
// ═══════════════════════════════════════════════════════════════
// State
// ═══════════════════════════════════════════════════════════════
let allFolders = [];
let compareData = null;
let compareSelectedFolderIds = []; // track selected folder ids in compare view
const VIDEO_EXTS_JS = ['.mp4', '.avi', '.mkv', '.webm', '.mov', '.flv', '.wmv', '.m4v'];
const AUDIO_EXTS_JS = ['.wav', '.mp3', '.flac', '.aac', '.ogg', '.m4a', '.wma', '.opus'];

function isVideoFile(name) {
    const lower = name.toLowerCase();
    return VIDEO_EXTS_JS.some(ext => lower.endsWith(ext));
}
function isAudioFile(name) {
    const lower = name.toLowerCase();
    return AUDIO_EXTS_JS.some(ext => lower.endsWith(ext));
}

// ═══════════════════════════════════════════════════════════════
// Utils
// ═══════════════════════════════════════════════════════════════
function $(id) { return document.getElementById(id); }

async function api(method, url, body) {
    const opts = { method, headers: { 'Content-Type': 'application/json' } };
    if (body) opts.body = JSON.stringify(body);
    const res = await fetch(url, opts);
    if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        throw new Error(err.detail || JSON.stringify(err));
    }
    return res.json();
}

function toast(msg, isError) {
    const t = $('toast');
    t.textContent = msg;
    t.className = 'toast show' + (isError ? ' error' : '');
    setTimeout(() => t.className = 'toast', 2500);
}

function naturalSort(arr) {
    return arr.slice().sort((a, b) => {
        const ax = [], bx = [];
        a.replace(/(\d+)|(\D+)/g, (_, n, s) => ax.push([n || Infinity, s || '']));
        b.replace(/(\d+)|(\D+)/g, (_, n, s) => bx.push([n || Infinity, s || '']));
        for (let i = 0; i < Math.max(ax.length, bx.length); i++) {
            const ai = ax[i] || [Infinity, ''], bi = bx[i] || [Infinity, ''];
            if (ai[1] !== bi[1]) return ai[1].localeCompare(bi[1]);
            const an = Number(ai[0]), bn = Number(bi[0]);
            if (an !== bn) return an - bn;
        }
        return 0;
    });
}

function esc(s) {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
}

function fileUrl(path) {
    return '/files/' + encodeURIComponent(path).replace(/%2F/g, '/');
}

// ═══════════════════════════════════════════════════════════════
// Tab switching
// ═══════════════════════════════════════════════════════════════
function switchTab(name) {
    const tabNames = ['collections', 'compare', 'folders'];
    const idx = tabNames.indexOf(name);
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
    // Activate the right tab button (skip theme-toggle)
    const tabBtns = document.querySelectorAll('.tab-btn');
    if (idx >= 0 && tabBtns[idx]) tabBtns[idx].classList.add('active');
    $('tab-' + name).classList.add('active');
    if (name === 'folders') loadFolders();
    if (name === 'compare') {
                // If no collection being edited, clear form
        if (!_currentEditCollId) {
            $('compare-coll-name').value = '';
            $('compare-coll-desc').value = '';
        }
    }
    if (name === 'collections') {
        // Clear collection/compare/readonly from URL when returning to collections tab
        const url = new URL(window.location);
        url.searchParams.delete('collection');
        url.searchParams.delete('compare');
        url.searchParams.delete('readonly');
        window.history.replaceState({}, '', url);
        loadCollections();
    }
}

// Theme toggle (light/dark)
function initTheme() {
    const saved = localStorage.getItem('fc-theme') || 'light';
    applyTheme(saved);
}
function applyTheme(theme) {
    if (theme === 'dark') {
        document.documentElement.classList.add('dark');
    } else {
        document.documentElement.classList.remove('dark');
    }
    localStorage.setItem('fc-theme', theme);
    const btn = document.getElementById('theme-toggle');
    if (btn) btn.innerHTML = theme === 'dark' ? '☀️ 白天' : '🌙 夜间';
}
function toggleTheme() {
    const current = localStorage.getItem('fc-theme') || 'light';
    applyTheme(current === 'dark' ? 'light' : 'dark');
}
initTheme();

// ═══════════════════════════════════════════════════════════════
// URL params & Readonly mode
// ═══════════════════════════════════════════════════════════════
const _urlParams = new URLSearchParams(window.location.search);
const _paramCollection = _urlParams.get('collection');
const _paramCompare = _urlParams.get('compare');
const _paramReadonly = _urlParams.get('readonly');

if (_paramReadonly === '1') {
    document.body.classList.add('readonly');
}

// Update URL without reload
function updateUrlParams(params) {
    const url = new URL(window.location);
    url.searchParams.delete('collection');
    url.searchParams.delete('compare');
    url.searchParams.delete('readonly');
    for (const [k, v] of Object.entries(params)) {
        if (v !== null && v !== undefined) url.searchParams.set(k, v);
    }
    window.history.replaceState({}, '', url);
}

// ═══════════════════════════════════════════════════════════════
// Tab 1: Folders
// ═══════════════════════════════════════════════════════════════
async function loadFolders() {
    try {
        allFolders = await api('GET', '/api/folders');
        renderFolderList();
    } catch (e) { toast(e.message, true); }
}

// Format created_at to [YYMMDD]
function fmtTime(ts) {
    if (!ts) return '';
    const d = new Date(ts);
    const yy = String(d.getFullYear()).slice(2);
    const mm = String(d.getMonth() + 1).padStart(2, '0');
    const dd = String(d.getDate()).padStart(2, '0');
    return `[${yy}${mm}${dd}]`;
}

function renderFolderList() {
    const el = $('folder-list');
    const searchEl = $('folder-search');
    const typeEl = $('folder-type-filter');
    const q = searchEl ? searchEl.value.toLowerCase() : '';
    const typeFilter = typeEl ? typeEl.value : 'all';
    const filtered = allFolders.filter(f => {
        // Type filter
        if (typeFilter !== 'all' && f.content_type !== typeFilter) return false;
        // Search filter
        if (!q) return true;
        return (f.name || '').toLowerCase().includes(q) ||
               (f.path || '').toLowerCase().includes(q) ||
               (f.desc || '').toLowerCase().includes(q);
    });
    if (!allFolders.length) {
        el.innerHTML = '<div class="empty-state"><div class="icon">📁</div><p>还没有文件夹，添加一个吧</p></div>';
        return;
    }
    if (!filtered.length) {
        el.innerHTML = '<div class="empty-state"><div class="icon">🔍</div><p>没有找到匹配的文件夹</p></div>';
        return;
    }
    el.innerHTML = filtered.map(f => `
        <div class="folder-item">
            <div class="folder-info">
                <div class="name">${esc(f.name)}</div>
                ${f.desc ? `<div class="desc-badge">${esc(f.desc)}</div>` : ''}
                <div class="path">${esc(f.path)}</div>
                <div class="meta">${f.content_type === 'video' ? '🎬 视频' : '🎵 音频'} · ${f.created_at}</div>
            </div>
            <div class="actions">
                <button class="secondary small" onclick="editFolder(${f.id}, '${esc(f.name)}')">编辑</button>
                <button class="danger small" onclick="deleteFolder(${f.id}, '${esc(f.name)}')">删除</button>
            </div>
        </div>
    `).join('');
}

function addFolderPrompt() {
    // Open the same edit dialog but for adding a new folder
    const overlay = document.createElement('div');
    overlay.id = 'edit-folder-overlay';
    overlay.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;background:var(--overlay-bg, rgba(0,0,0,0.5));z-index:100;display:flex;align-items:center;justify-content:center;';
    overlay.innerHTML = `
    <div class="card" style="max-width:640px; width:90%;">
        <h3 style="margin-bottom:12px;">添加文件夹</h3>
        <div style="margin-bottom:12px;">
            <label style="display:block;font-size:13px;color:var(--text2);margin-bottom:4px;">路径</label>
            <textarea id="ef-path" rows="4" placeholder="服务器上的绝对路径, 如 /data/audio/exp1" style="width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);padding:10px 14px;border-radius:6px;font-size:14px;outline:none;resize:vertical;font-family:inherit;"></textarea>
        </div>
        <div style="margin-bottom:12px;">
            <label style="display:block;font-size:13px;color:var(--text2);margin-bottom:4px;">名称</label>
            <input type="text" id="ef-name" placeholder="默认取目录名" style="width:100%;" />
        </div>
        <div style="margin-bottom:12px;">
            <label style="display:block;font-size:13px;color:var(--text2);margin-bottom:4px;">类型</label>
            <select id="ef-type" style="width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);padding:8px 12px;border-radius:6px;font-size:14px;">
                <option value="audio">🎵 音频</option>
                <option value="video">🎬 视频</option>
            </select>
        </div>
        <div style="margin-bottom:16px;">
            <label style="display:block;font-size:13px;color:var(--text2);margin-bottom:4px;">描述（可选）</label>
            <textarea id="ef-desc" rows="2" placeholder="简要说明这个文件夹的内容" style="width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);padding:10px 14px;border-radius:6px;font-size:14px;outline:none;resize:vertical;font-family:inherit;"></textarea>
        </div>
        <div style="display:flex; gap:8px; justify-content:flex-end;">
            <button class="secondary" onclick="closeEditOverlay()">取消</button>
            <button onclick="addFolderFromDialog()">添加</button>
        </div>
    </div>`;
    document.body.appendChild(overlay);
    overlay.addEventListener('click', e => { if (e.target === overlay) closeEditOverlay(); });
    document.getElementById('ef-path').focus();
}

async function addFolderFromDialog() {
    const path = document.getElementById('ef-path').value.trim();
    const name = document.getElementById('ef-name').value.trim();
    const desc = document.getElementById('ef-desc').value.trim();
    const type = document.getElementById('ef-type').value;
    if (!path) { toast('请输入文件夹路径', true); return; }
    try {
        const newFolder = await api('POST', '/api/folders', { path, name: name || undefined, desc: desc || '', content_type: type });
        toast('✅ 添加成功');
        closeEditOverlay();
        loadFolders();
        // 如果当前正在编辑 collection, 自动将新文件夹加入
        if (_currentEditCollId && newFolder && newFolder.id) {
            try {
                const coll = await api('GET', '/api/collections/' + _currentEditCollId);
                if (coll && coll.folder_ids) {
                    const newFolderIds = [...coll.folder_ids, newFolder.id];
                    await api('PUT', '/api/collections/' + _currentEditCollId, {
                        name: coll.name,
                        desc: coll.desc,
                        folder_ids: newFolderIds
                    });
                    toast('✅ 已自动添加到 Collection');
                    // 刷新 compare 视图
                    compareSelectedFolderIds = newFolderIds;
                    await loadCompare();
                }
            } catch (e) { console.log('Auto-add to collection failed:', e); }
        }
    } catch (e) { toast(e.message, true); }
}

async function addFolder() {
    const path = $('folder-path').value.trim();
    const name = $('folder-name').value.trim();
    if (!path) { toast('请输入文件夹路径', true); return; }
    try {
        await api('POST', '/api/folders', { path, name: name || undefined });
        $('folder-path').value = '';
        $('folder-name').value = '';
        toast('✅ 添加成功');
        loadFolders();
    } catch (e) { toast(e.message, true); }
}

async function editFolder(id, currentName) {
    const f = allFolders.find(f => f.id === id);
    const overlay = document.createElement('div');
    overlay.id = 'edit-folder-overlay';
    overlay.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;background:var(--overlay-bg, rgba(0,0,0,0.5));z-index:100;display:flex;align-items:center;justify-content:center;';
    overlay.innerHTML = `
    <div class="card" style="max-width:640px; width:90%;">
        <h3 style="margin-bottom:12px;">编辑文件夹</h3>
        <div style="margin-bottom:12px;">
            <label style="display:block;font-size:13px;color:var(--text2);margin-bottom:4px;">名称</label>
            <input type="text" id="ef-name" value="${esc(f ? f.name : currentName)}" style="width:100%;" />
        </div>
        <div style="margin-bottom:12px;">
            <label style="display:block;font-size:13px;color:var(--text2);margin-bottom:4px;">路径</label>
            <textarea id="ef-path" rows="4" style="width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);padding:10px 14px;border-radius:6px;font-size:14px;outline:none;resize:vertical;font-family:inherit;">${esc(f ? f.path : '')}</textarea>
        </div>
        <div style="margin-bottom:16px;">
            <label style="display:block;font-size:13px;color:var(--text2);margin-bottom:4px;">描述（可选）</label>
            <textarea id="ef-desc" rows="2" style="width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);padding:10px 14px;border-radius:6px;font-size:14px;outline:none;resize:vertical;font-family:inherit;">${esc(f ? f.desc || '' : '')}</textarea>
        </div>
        <div style="display:flex; gap:8px; justify-content:flex-end;">
            <button class="secondary" onclick="closeEditOverlay()">取消</button>
            <button onclick="saveFolder(${id})">保存</button>
        </div>
    </div>`;
    document.body.appendChild(overlay);
    overlay.addEventListener('click', e => { if (e.target === overlay) closeEditOverlay(); });
    document.getElementById('ef-name').focus();
}

function closeEditOverlay() {
    const el = document.getElementById('edit-folder-overlay');
    if (el) el.remove();
}

async function saveFolder(id) {
    const name = document.getElementById('ef-name').value.trim();
    const path = document.getElementById('ef-path').value.trim();
    const desc = document.getElementById('ef-desc').value.trim();
    if (!name) { toast('名称不能为空', true); return; }
    if (!path) { toast('路径不能为空', true); return; }
    try {
        await api('PUT', `/api/folders/${id}`, { name, path, desc });
        toast('✅ 已保存');
        closeEditOverlay();
        loadFolders();
    } catch (e) { toast(e.message, true); }
}

function editFolderFromCompare(id) {
    const f = allFolders.find(f => f.id === id);
    if (!f) { toast('文件夹信息未加载', true); return; }
    const overlay = document.createElement('div');
    overlay.id = 'edit-folder-overlay';
    overlay.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;background:var(--overlay-bg, rgba(0,0,0,0.5));z-index:100;display:flex;align-items:center;justify-content:center;';
    overlay.innerHTML = `
    <div class="card" style="max-width:640px; width:90%;">
        <h3 style="margin-bottom:12px;">编辑文件夹</h3>
        <div style="margin-bottom:12px;">
            <label style="display:block;font-size:13px;color:var(--text2);margin-bottom:4px;">名称</label>
            <input type="text" id="ef-name" value="${esc(f.name)}" style="width:100%;" />
        </div>
        <div style="margin-bottom:12px;">
            <label style="display:block;font-size:13px;color:var(--text2);margin-bottom:4px;">路径</label>
            <div style="display:flex;gap:8px;align-items:center;">
                <input type="text" id="ef-path" value="${esc(f.path)}" readonly style="width:100%;opacity:0.7;cursor:not-allowed;" />
                <button class="secondary small" onclick="copyToClipboard(document.getElementById('ef-path').value)" title="复制路径" style="flex-shrink:0;padding:8px 12px;">📋 复制</button>
            </div>
        </div>
        <div style="margin-bottom:16px;">
            <label style="display:block;font-size:13px;color:var(--text2);margin-bottom:4px;">描述（可选）</label>
            <textarea id="ef-desc" rows="2" style="width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);padding:10px 14px;border-radius:6px;font-size:14px;outline:none;resize:vertical;font-family:inherit;">${esc(f.desc || '')}</textarea>
        </div>
        <div style="display:flex; gap:8px; justify-content:flex-end;">
            <button class="secondary" onclick="closeEditOverlay()">取消</button>
            <button onclick="saveFolderFromCompare(${id})">保存</button>
        </div>
    </div>`;
    document.body.appendChild(overlay);
    overlay.addEventListener('click', e => { if (e.target === overlay) closeEditOverlay(); });
    document.getElementById('ef-name').focus();
}

async function saveFolderFromCompare(id) {
    const name = document.getElementById('ef-name').value.trim();
    const path = document.getElementById('ef-path').value.trim();
    const desc = document.getElementById('ef-desc').value.trim();
    if (!name) { toast('名称不能为空', true); return; }
    if (!path) { toast('路径不能为空', true); return; }
    try {
        await api('PUT', `/api/folders/${id}`, { name, path, desc });
        toast('✅ 已保存');
        closeEditOverlay();
        // Refresh all folder data and re-render compare
        allFolders = await api('GET', '/api/folders');
                loadCompare();
    } catch (e) { toast(e.message, true); }
}

async function deleteFolder(id, name) {
    if (!confirm(`确认删除文件夹「${name}」？（不会删除实际文件）`)) return;
    try {
        await api('DELETE', `/api/folders/${id}`);
        toast('已删除');
        loadFolders();
    } catch (e) { toast(e.message, true); }
}

// ═══════════════════════════════════════════════════════════════
// Tab 2: Compare
// ═══════════════════════════════════════════════════════════════
// refreshCompareUI removed - tags bar removed

function showCompareFolderPicker() {
    if (document.getElementById('compare-folder-overlay')) return;
    const overlay = document.createElement('div');
    overlay.id = 'compare-folder-overlay';
    overlay.className = 'compare-folder-overlay';

    const selectedTags = compareSelectedFolderIds.map(id => {
        const f = allFolders.find(f => f.id === id);
        const name = f ? f.name : `#${id}`;
        return `<span class="selected-tag" title="${esc(f ? f.path : '')}">${esc(name)}<span class="remove" onclick="pickerRemoveFolder(${id})">✕</span></span>`;
    }).join('');

    const folderChecks = allFolders.map(f => {
        const checked = compareSelectedFolderIds.includes(f.id) ? 'checked' : '';
        return `<div class="folder-check-item">
            <input type="checkbox" id="cpf-${f.id}" value="${f.id}" ${checked} onchange="pickerToggle(${f.id}, this.checked)" />
            <label for="cpf-${f.id}">
                <div class="fc-folder-header">
                    <span class="fc-folder-name">${esc(f.name)}</span>
                    ${fmtTime(f.created_at) ? `<span class="fc-folder-time">${esc(fmtTime(f.created_at))}</span>` : ''}
                </div>
                ${f.desc ? `<span class="fc-folder-desc">${esc(f.desc)}</span>` : ''}
            </label>
        </div>`;
    }).join('');

    overlay.innerHTML = `
    <div class="overlay-card">
        <h3 style="margin-bottom:12px;">选择文件夹</h3>
        <div class="selected-tags" id="picker-selected">${selectedTags}</div>
        ${allFolders.length ? `
        <div style="max-height:300px; overflow-y:auto; border:1px solid var(--border); border-radius:6px; padding:4px 0;">
            ${folderChecks}
        </div>` : '<p style="color:var(--text2); text-align:center; padding:20px;">没有文件夹，先去添加</p>'}
        <div style="display:flex; gap:8px; margin-top:16px; justify-content:flex-end;">
            <button class="secondary" onclick="closeCompareFolderPicker()">取消</button>
            <button onclick="applyCompareFolders()">确定</button>
        </div>
    </div>`;

    document.body.appendChild(overlay);
    overlay.addEventListener('click', e => { if (e.target === overlay) closeCompareFolderPicker(); });
}

function closeCompareFolderPicker() {
    const el = document.getElementById('compare-folder-overlay');
    if (el) el.remove();
}

function pickerToggle(id, checked) {
    if (checked) {
        if (!compareSelectedFolderIds.includes(id)) compareSelectedFolderIds.push(id);
    } else {
        compareSelectedFolderIds = compareSelectedFolderIds.filter(x => x !== id);
    }
    const tagsEl = document.getElementById('picker-selected');
    if (tagsEl) {
        tagsEl.innerHTML = compareSelectedFolderIds.map(fid => {
            const f = allFolders.find(f => f.id === fid);
            const name = f ? f.name : `#${fid}`;
            return `<span class="selected-tag" title="${esc(fd ? fd.path : '')}">${esc(name)}<span class="remove" onclick="pickerRemoveFolder(${fid})">✕</span></span>`;
        }).join('');
    }
}

function pickerRemoveFolder(id) {
    compareSelectedFolderIds = compareSelectedFolderIds.filter(x => x !== id);
    const cb = document.getElementById('cpf-' + id);
    if (cb) cb.checked = false;
    const tagsEl = document.getElementById('picker-selected');
    if (tagsEl) {
        tagsEl.innerHTML = compareSelectedFolderIds.map(fid => {
            const f = allFolders.find(f => f.id === fid);
            const name = f ? f.name : `#${fid}`;
            return `<span class="selected-tag" title="${esc(fd ? fd.path : '')}">${esc(name)}<span class="remove" onclick="pickerRemoveFolder(${fid})">✕</span></span>`;
        }).join('');
    }
}

function applyCompareFolders() {
    closeCompareFolderPicker();
    loadCompare();
    autoSaveCompareCollection();
}

// 自动保存对比视图为/更新 collection
// 新建 Collection: 弹出对话框
function startNewCollection() {
    const overlay = document.createElement('div');
    overlay.id = 'new-coll-overlay';
    overlay.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.5);z-index:100;display:flex;align-items:center;justify-content:center;';
    overlay.innerHTML = `
    <div class="card" style="max-width:480px;width:90%;">
        <h3 style="margin-bottom:12px;">新建 Collection</h3>
        <div style="margin-bottom:12px;">
            <label style="display:block;font-size:13px;color:var(--text2);margin-bottom:4px;">名称</label>
            <input type="text" id="new-coll-name" placeholder="Collection 名称" style="width:100%;" />
        </div>
        <div style="margin-bottom:16px;">
            <label style="display:block;font-size:13px;color:var(--text2);margin-bottom:4px;">描述（可选）</label>
            <textarea id="new-coll-desc" rows="2" style="width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);padding:10px 14px;border-radius:6px;font-size:14px;outline:none;resize:vertical;font-family:inherit;" placeholder="简要说明"></textarea>
        </div>
        <div style="display:flex;gap:8px;justify-content:flex-end;">
            <button class="secondary" onclick="document.getElementById('new-coll-overlay').remove()">取消</button>
            <button onclick="createCollectionFromDialog()">创建</button>
        </div>
    </div>`;
    document.body.appendChild(overlay);
    overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });
    setTimeout(() => document.getElementById('new-coll-name').focus(), 100);
}

async function createCollectionFromDialog() {
    const name = document.getElementById('new-coll-name').value.trim();
    const desc = document.getElementById('new-coll-desc').value.trim();
    if (!name) { toast('请输入名称', true); return; }
    try {
        await api('POST', '/api/collections', { name, desc, folder_ids: [] });
        toast('✅ 创建成功');
        document.getElementById('new-coll-overlay').remove();
        loadCollections();
    } catch (e) { toast(e.message, true); }
}

async function doCreateCollection() {
    const name = document.getElementById('coll-name').value.trim();
    const desc = document.getElementById('coll-desc').value.trim();
    if (!name) { toast('请输入名称', true); return; }
    try {
        await api('POST', '/api/collections', { name, desc, folder_ids: [] });
        toast('✅ 创建成功');
        document.getElementById('coll-prompt-overlay').remove();
        loadCollections();
    } catch (e) { toast(e.message, true); }
}

async function autoSaveCompareCollection() {
    if (!_currentEditCollId) return; // 没有正在编辑的 collection 就不保存
    if (!compareSelectedFolderIds.length) return;
    try {
        const coll = await api('GET', '/api/collections/' + _currentEditCollId);
        if (coll) {
            await api('PUT', '/api/collections/' + _currentEditCollId, {
                name: coll.name,
                desc: coll.desc,
                folder_ids: compareSelectedFolderIds
            });
        }
    } catch (e) { console.log('Auto-save collection failed:', e); }
}

function removeCompareFolder(id) {
    compareSelectedFolderIds = compareSelectedFolderIds.filter(x => x !== id);
    if (compareSelectedFolderIds.length > 0) {
        loadCompare();
        autoSaveCompareCollection();
    } else {
        compareData = null;
        $('compare-content').innerHTML = '<div class="empty-state"><div class="icon">🎧</div><p>点击上方「＋ 添加文件夹」开始对比</p></div>';
        $('compare-toolbar').style.display = 'none';
    }
}

// Drag to reorder columns
function initColumnDrag() {
    const table = document.querySelector('.compare-table');
    if (!table) return;
    const thead = table.querySelector('thead');
    let dragIdx = null;

    thead.querySelectorAll('th.compare-th').forEach((th, idx) => {
        th.addEventListener('dragstart', (e) => {
            dragIdx = idx;
            th.classList.add('dragging');
            e.dataTransfer.effectAllowed = 'move';
        });
        th.addEventListener('dragend', () => {
            th.classList.remove('dragging');
            thead.querySelectorAll('th').forEach(t => t.classList.remove('drag-over'));
        });
        th.addEventListener('dragover', (e) => {
            e.preventDefault();
            e.dataTransfer.dropEffect = 'move';
            thead.querySelectorAll('th').forEach(t => t.classList.remove('drag-over'));
            if (idx !== dragIdx) th.classList.add('drag-over');
        });
        th.addEventListener('drop', (e) => {
            e.preventDefault();
            th.classList.remove('drag-over');
            if (dragIdx !== null && dragIdx !== idx) {
                const newOrder = [...compareSelectedFolderIds];
                const [moved] = newOrder.splice(dragIdx, 1);
                newOrder.splice(idx, 0, moved);
                compareSelectedFolderIds = newOrder;
                loadCompare();
            }
        });
    });
}

async function loadFolderChecks() {
    try {
        allFolders = await api('GET', '/api/folders');
    } catch (e) { toast(e.message, true); return; }
}

async function loadCompare() {
    const ids = compareSelectedFolderIds;
    if (!ids.length) {
        $('compare-content').innerHTML = '<div class="empty-state"><div class="icon">🎧</div><p>点击上方「＋ 添加文件夹」开始对比</p></div>';
        $('compare-toolbar').style.display = 'none';
        return;
    }

    $('compare-content').innerHTML = '<div class="loading">加载中...</div>';

    try {
        const results = await Promise.all(ids.map(id => api('GET', `/api/folders/${id}/files`)));
        // Collect all unique filenames (union)
        const nameSet = new Set();
        results.forEach(r => r.files.forEach(f => nameSet.add(f)));
        const allNames = naturalSort(Array.from(nameSet));

        compareData = { folders: results, allNames };
        renderCompare();
    } catch (e) {
        toast(e.message, true);
        $('compare-content').innerHTML = '<div class="empty-state"><p>加载失败</p></div>';
    }
}

// Load compare from collection
async function loadCollectionCompare(folderIds, collId) {
    // Fetch collection details
    try {
        const coll = await api('GET', `/api/collections/${collId}`);
        if (coll) {
            _currentEditCollId = collId;
            $('compare-coll-name').value = coll.name || '';
            $('compare-coll-desc').value = coll.desc || '';
        }
    } catch (e) {}

    // Switch to compare tab
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-btn')[1].classList.add('active');
    $('tab-compare').classList.add('active');

    // Update URL
    if (collId) {
        updateUrlParams({ collection: collId, readonly: _paramReadonly || null });
    }

    compareSelectedFolderIds = [...folderIds];
    await loadFolderChecks();
        await loadCompare();
}

function renderCompare() {
    if (!compareData) return;
    const { folders } = compareData;
    const filter = ($('compare-filter').value || '').toLowerCase();

    // Build independent folder panels
    let html = '<div class="compare-panels">';
    folders.forEach((f, idx) => { f._idx = idx;
        const desc = f.folder.desc ? `<span class="panel-desc">${esc(f.folder.desc)}</span>` : '';
        html += `<div class="compare-panel">`;
        html += `<div class="panel-header">
            <div class="panel-title-row">
                <span class="panel-name">${esc(f.folder.name)}</span>
                ${desc}
            </div>
            <div class="panel-actions">
                <button class="btn-edit" onclick="editFolderFromCompare(${f.folder.id})" title="编辑">✏️</button>
                <button class="btn-close" onclick="removeCompareFolder(${f.folder.id})" title="移除">❌</button>
            </div>
        </div>`;
        html += `<div class="panel-files">`;

        // Filter files for this folder
        let folderFiles = f.files;
        if (filter) {
            folderFiles = folderFiles.filter(n => n.toLowerCase().includes(filter));
        }

        folderFiles.forEach(name => {
            const filePath = f.folder.path + '/' + name;
            const url = fileUrl(filePath);
            const isVideo = isVideoFile(name);
            const rating = (f.ratings[name] || {}).rating || 0;
            const note = (f.ratings[name] || {}).note || '';
            const folderId = f.folder.id;
            const cellId = `cell-${folderId}-${name.replace(/[^a-zA-Z0-9]/g, '_')}`;

            html += `<div class="panel-file-item" id="${cellId}">`;
            html += `<div class="file-label">${esc(name)}</div>`;
            // Show context and syn_text from name2info if available
            const fileInfo = (f.name2info && f.name2info[name]) ? f.name2info[name] : null;
            if (fileInfo) {
                if (fileInfo.context) {
                    html += `<div class="file-text file-context"><strong>Context:</strong> ${esc(fileInfo.context)}</div>`;
                }
                if (fileInfo.syn_text) {
                    html += `<div class="file-text file-syn-text"><strong>文本:</strong> ${esc(fileInfo.syn_text)}</div>`;
                }
            }
            if (isVideo) {
                html += `<video controls preload="metadata" src="${url}"></video>`;
            } else {
                html += `<audio controls preload="metadata" src="${url}"></audio>`;
            }
            html += `<div class="rating-note-row">`;
            html += `<div class="rating-row">`;
            for (let s = 1; s <= 5; s++) {
                html += `<span class="star-btn ${s <= rating ? 'active' : ''}"
                    onclick="setRating(${folderId}, '${esc(name)}', ${s}, '${cellId}')"
                    title="${s}分"><svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor"><path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/></svg></span>`;
            }
            html += `</div>`;
            html += `<textarea class="note-input" placeholder="备注..."
                rows="1"
                oninput="this.style.height='auto';this.style.height=this.scrollHeight+'px'"
                onchange="setNote(${folderId}, '${esc(name)}', this.value)">${esc(note)}</textarea>`;
            html += `</div>`;
            html += `</div>`;
        });

        html += `<div class="panel-resize-handle" data-panel-idx="${f._idx}"></div></div></div>`;
    });
    html += '</div>';

    $('compare-content').innerHTML = html;
    initPanelResize();
}

// 面板拖拽调整宽度
function initPanelResize() {
    const handles = document.querySelectorAll('.panel-resize-handle');
    const panels = document.querySelectorAll('.compare-panel');
    let activeHandle = null;
    let startX = 0;
    let startWidth = 0;

    handles.forEach(handle => {
        handle.addEventListener('mousedown', (e) => {
            e.preventDefault();
            activeHandle = handle;
            handle.classList.add('dragging');
            startX = e.clientX;
            const panelIdx = parseInt(handle.dataset.panelIdx);
            startWidth = panels[panelIdx].offsetWidth;
        });
    });

    document.addEventListener('mousemove', (e) => {
        if (!activeHandle) return;
        const dx = e.clientX - startX;
        const panelIdx = parseInt(activeHandle.dataset.panelIdx);
        const newWidth = Math.max(280, startWidth + dx);
        panels[panelIdx].style.width = newWidth + 'px';
    });

    document.addEventListener('mouseup', () => {
        if (activeHandle) {
            activeHandle.classList.remove('dragging');
            activeHandle = null;
        }
    });
}

function filterCompare() {
    renderCompare();
}

async function setRating(folderId, filename, rating, cellId) {
    try {
        // If clicking same star, toggle off
        const cell = document.getElementById(cellId);
        const stars = cell ? cell.querySelectorAll('.star-btn') : [];
        let currentRating = 0;
        stars.forEach((s, i) => { if (s.classList.contains('active')) currentRating = i + 1; });
        const newRating = (currentRating === rating) ? 0 : rating;

        // Get current note
        const noteInput = cell ? cell.querySelector('.note-input') : null;
        const note = noteInput ? noteInput.value : '';

        await api('POST', '/api/ratings', { folder_id: folderId, filename, rating: newRating, note });

        // Update stars visually
        stars.forEach((s, i) => {
            s.classList.toggle('active', i < newRating);
        });

        // Update local data
        if (compareData) {
            const f = compareData.folders.find(f => f.folder.id === folderId);
            if (f) f.ratings[filename] = { rating: newRating, note };
        }
    } catch (e) { toast(e.message, true); }
}

async function setNote(folderId, filename, note) {
    try {
        // Get current rating
        let rating = 0;
        if (compareData) {
            const f = compareData.folders.find(f => f.folder.id === folderId);
            if (f && f.ratings[filename]) rating = f.ratings[filename].rating;
        }
        await api('POST', '/api/ratings', { folder_id: folderId, filename, rating, note });
        if (compareData) {
            const f = compareData.folders.find(f => f.folder.id === folderId);
            if (f) f.ratings[filename] = { rating, note };
        }
    } catch (e) { toast(e.message, true); }
}






// ═══════════════════════════════════════════════════════════════
// Tab 3: Collections
// ═══════════════════════════════════════════════════════════════

// Cache: avoid re-fetching folders if already loaded recently
let _collectionsCache = { folders: null, colls: null, ts: 0 };
const COLL_CACHE_TTL = 30000; // 30s

async function loadCollections() {
    const now = Date.now();
    try {
        // Always refresh folders for dup check
        const [folders, colls] = await Promise.all([
            api('GET', '/api/folders'),
            api('GET', '/api/collections')
        ]);
        allFolders = folders;
        _collectionsCache = { folders, colls, ts: now };
        renderCollections(_collectionsCache.colls);
    } catch (e) { toast(e.message, true); }
}

// Debounce timer for collection search
let _collSearchTimer = null;

function renderCollections(colls) {
    const el = $('collections-list');
    const searchEl = $('coll-search');
    const q = searchEl ? searchEl.value.toLowerCase() : '';
    const filtered = colls.filter(c => {
        if (!q) return true;
        return (c.name || '').toLowerCase().includes(q) ||
               (c.desc || '').toLowerCase().includes(q);
    });
    if (!colls.length) {
        el.innerHTML = '<div class="empty-state"><div class="icon">📦</div><p>还没有 Collection，先创建一个吧</p></div>';
        return;
    }
    if (!filtered.length) {
        el.innerHTML = '<div class="empty-state"><div class="icon">🔍</div><p>没有找到匹配的 Collection</p></div>';
        return;
    }
    el.innerHTML = '<div class="coll-list">' + filtered.map(c => {
        const folderNames = c.folder_ids.map(id => {
            const f = allFolders.find(f => f.id === id);
            return f ? f.name : `#${id}`;
        });
        return `
        <div class="coll-list-item" onclick="openCollection(${c.id})">
            <div class="coll-row-main">
                <div class="coll-row-header">
                    <span class="coll-row-name">${esc(c.name)}</span>
                    ${c.desc ? `<span class="coll-row-desc">${esc(c.desc)}</span>` : ''}
                </div>
                <div class="coll-row-tags">
                    ${folderNames.slice(0, 8).map(n => `<span class="coll-folder-tag">${esc(n)}</span>`).join('')}
                    ${folderNames.length > 8 ? `<span class="coll-folder-tag">+${folderNames.length - 8}</span>` : ''}
                    ${!folderNames.length ? '<span style="color:var(--text2); font-size:12px;">暂无文件夹</span>' : ''}
                </div>
            </div>
            <div class="coll-row-actions">
                <button class="share-btn" onclick="event.stopPropagation(); shareCollection(${c.id})" title="分享">🔗</button>
                <button class="share-btn" onclick="event.stopPropagation(); editCollectionPrompt(${c.id})" title="编辑">✏️</button>
                <button class="share-btn" onclick="event.stopPropagation(); deleteCollection(${c.id})" title="删除">🗑️</button>
            </div>
        </div>`;
    }).join('') + '</div>';
}

function onCollSearchInput() {
    if (_collSearchTimer) clearTimeout(_collSearchTimer);
    _collSearchTimer = setTimeout(() => {
        loadCollections();
    }, 300);
}

async function openCollection(collId) {
    try {
        const coll = await api('GET', `/api/collections/${collId}`);
        if (coll && coll.folder_ids && coll.folder_ids.length) {
            await loadCollectionCompare(coll.folder_ids, collId);
        } else {
            toast('Collection 为空或不存在', true);
        }
    } catch (e) { toast('无法加载 Collection: ' + e.message, true); }
}

// createCollection removed - use startNewCollection instead

async function deleteCollection(id) {
    if (!confirm('确认删除这个 Collection？')) return;
    try {
        await api('DELETE', `/api/collections/${id}`);
        toast('已删除');
        loadCollections();
    } catch (e) { toast(e.message, true); }
}

async function editCollectionPrompt(id) {
    try {
        const coll = await api('GET', `/api/collections/${id}`);
        const overlay = document.createElement('div');
        overlay.id = 'edit-collection-overlay';
        overlay.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.7);z-index:100;display:flex;align-items:center;justify-content:center;';

        const folderOptions = allFolders.map(f => {
            const checked = coll.folder_ids.includes(f.id) ? 'checked' : '';
            const timeTag = fmtTime(f.created_at);
            const descHtml = f.desc ? `<span class="fc-folder-desc">${esc(f.desc)}</span>` : '';
            return `<div class="folder-check-item">
                <input type="checkbox" id="ec-${f.id}" value="${f.id}" ${checked} />
                <label for="ec-${f.id}">
                    <div class="fc-folder-header">
                        <span class="fc-folder-name">${esc(f.name)}</span>
                        ${timeTag ? `<span class="fc-folder-time">${esc(timeTag)}</span>` : ''}
                    </div>
                    ${descHtml}
                </label>
            </div>`;
        }).join('');

        overlay.innerHTML = `
        <div class="card" style="max-width:550px; width:90%; max-height:80vh; overflow-y:auto;">
            <h3 style="margin-bottom:12px;">编辑 Collection</h3>
            <div style="margin-bottom:12px;">
                <label style="display:block;font-size:13px;color:var(--text2);margin-bottom:4px;">名称</label>
                <input type="text" id="ec-name" value="${esc(coll.name)}" style="width:100%;" />
            </div>
            <div style="margin-bottom:12px;">
                <label style="display:block;font-size:13px;color:var(--text2);margin-bottom:4px;">描述</label>
                <input type="text" id="ec-desc" value="${esc(coll.desc || '')}" style="width:100%;" />
            </div>
            <h4 style="margin:12px 0 8px; font-size:14px;">选择文件夹</h4>
            <input type="text" id="ec-folder-search" placeholder="搜索文件夹..." style="width:100%;margin-bottom:8px;" oninput="filterEditCollectionFolders()" />
            <div id="ec-folder-list" style="max-height:300px; overflow-y:auto; border:1px solid var(--border); border-radius:6px; padding:4px 0;">
                ${folderOptions}
            </div>
            <div style="display:flex; gap:8px; margin-top:16px; justify-content:flex-end;">
                <button class="secondary" onclick="closeCollectionOverlay()">取消</button>
                <button onclick="saveCollection(${id})">保存</button>
            </div>
        </div>`;

        document.body.appendChild(overlay);
        overlay.addEventListener('click', e => { if (e.target === overlay) closeCollectionOverlay(); });
    } catch (e) { toast(e.message, true); }
}

function closeCollectionOverlay() {
    const el = document.getElementById('edit-collection-overlay');
    if (el) el.remove();
}

function filterEditCollectionFolders() {
    const q = document.getElementById('ec-folder-search').value.toLowerCase();
    document.querySelectorAll('#ec-folder-list .folder-check-item').forEach(item => {
        const label = item.querySelector('label').textContent.toLowerCase();
        item.style.display = label.includes(q) ? '' : 'none';
    });
}

async function saveCollection(id) {
    const name = document.getElementById('ec-name').value.trim();
    const desc = document.getElementById('ec-desc').value.trim();
    const folderIds = Array.from(document.querySelectorAll('#ec-folder-list input[type="checkbox"]:checked'))
        .map(cb => parseInt(cb.value));

    try {
        await api('PUT', `/api/collections/${id}`, { name, desc, folder_ids: folderIds });
        toast('✅ 已保存');
        closeCollectionOverlay();
        loadCollections();
    } catch (e) { toast(e.message, true); }
}

// ═══════════════════════════════════════════════════════════════
// Share Collection
// ═══════════════════════════════════════════════════════════════
function shareCollection(collId) {
    const url = `${window.location.origin}/?collection=${collId}&readonly=1`;
    copyToClipboard(url);
}

function copyToClipboard(text) {
    // 优先用 textarea fallback（兼容 HTTP 页面）
    try {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.cssText = 'position:fixed;left:-9999px;top:-9999px;opacity:0;';
        document.body.appendChild(ta);
        ta.focus();
        ta.select();
        const ok = document.execCommand('copy');
        document.body.removeChild(ta);
        if (ok) { toast('✅ 已复制分享链接: ' + text); return; }
    } catch(e) {}
    // fallback 2: clipboard API（仅 HTTPS 可用）
    if (navigator.clipboard) {
        navigator.clipboard.writeText(text).then(() => {
            toast('✅ 已复制分享链接');
        }).catch(() => {
            prompt('自动复制失败，请手动复制:', text);
        });
    } else {
        prompt('自动复制失败，请手动复制:', text);
    }
}

// ═══════════════════════════════════════════════════════════════
// URL param auto-load
// ═══════════════════════════════════════════════════════════════
async function handleUrlParams() {
    if (_paramCollection) {
        const collId = parseInt(_paramCollection);
        if (!isNaN(collId)) {
            // Load collection data first, THEN switch tab (switchTab clears URL)
            try {
                const coll = await api('GET', `/api/collections/${collId}`);
                if (coll && coll.folder_ids && coll.folder_ids.length) {
                    _currentEditCollId = collId;
                    // Set compare state before switching tab
                    compareSelectedFolderIds = coll.folder_ids;
                    await loadFolderChecks();
                    // Now switch to compare tab directly (skip collections)
                    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
                    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
                    document.querySelectorAll('.tab-btn')[1].classList.add('active');
                    $('tab-compare').classList.add('active');
                    updateUrlParams({ collection: collId, readonly: _paramReadonly || null });
                                        await loadCompare();
                    return;
                } else {
                    toast('Collection 为空或不存在', true);
                }
            } catch (e) {
                toast('无法加载 Collection: ' + e.message, true);
            }
        }
    } else if (_paramCompare) {
        const ids = _paramCompare.split(',').map(s => parseInt(s.trim())).filter(n => !isNaN(n));
        if (ids.length) {
            compareSelectedFolderIds = ids;
            await loadFolderChecks();
                        await loadCompare();
        }
    }
}

// ═══════════════════════════════════════════════════════════════
// Init
// ═════════════��═════════════════════════════════════════════════
// Touch support for collection list item actions
function initTouchSupport() {
    document.querySelectorAll('.coll-list-item').forEach(item => {
        item.addEventListener('touchstart', function(e) {
            this.classList.toggle('touch-active');
        }, { passive: true });
        item.addEventListener('touchend', function() {
            setTimeout(() => this.classList.remove('touch-active'), 1500);
        }, { passive: true });
    });
}

loadCollections().then(() => {
    handleUrlParams();
    // Re-init touch support after collections render
    setTimeout(initTouchSupport, 200);
});
</script>
</body>
</html>"""

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PAGE

# ─────────────────────────── Main ───────────────────────────────

if __name__ == "__main__":
    init_db()
    print(f"🚀 AV Compare running at http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
