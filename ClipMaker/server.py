"""
ClipMaker local website. Run by start.bat; open http://127.0.0.1:7860
"""
import json
import os
import re
import shutil
import socket
import sys
import threading
import time
import uuid
import webbrowser
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

import ai  # noqa: E402
import engine  # noqa: E402
from app import OUTPUT_DIR, VIDEO_EXTS, download_urls, load_settings, probe, save_settings  # noqa: E402
import twitch_auto  # noqa: E402
import updater  # noqa: E402
from twitch_auto import Watcher  # noqa: E402

PORT = 7860
WORK = APP_DIR / "work"
app = FastAPI()
clips = {}      # id -> {path, name, temp}
jobs = {}       # id -> {status, log, progress, output, error}
lock = threading.Lock()


def err(msg, code=400):
    return JSONResponse({"error": msg}, status_code=code)


INBOX = APP_DIR / "inbox"   # clips from Twitch auto mode, waiting for you to edit them


def add_clip(path, name, temp, source=None):
    cid = uuid.uuid4().hex[:10]
    W, H, n, fps = probe(path)
    info = {"id": cid, "name": name, "w": W, "h": H, "dur": round(n / fps, 3) if fps else 0, "source": source}
    clips[cid] = {"path": Path(path), "name": name, "temp": temp, "info": info}
    return info


def clean_title(stem):
    return re.sub(r"\s*\[[^\]]+\]$", "", stem).strip() or "Twitch clip"


def load_inbox():
    INBOX.mkdir(exist_ok=True)
    known = {c["path"].resolve() for c in clips.values()}
    for f in sorted(INBOX.glob("*.mp4"), key=lambda p: p.stat().st_mtime):
        if f.resolve() not in known:
            try:
                add_clip(f, clean_title(f.stem), True, source="twitch")
            except Exception:
                pass


@app.get("/api/clips")
def list_clips():
    return [c["info"] for c in clips.values() if c["path"].exists()]


# ------------------------------------------------------------------ pages
@app.get("/favicon.ico")
def favicon():
    return FileResponse(APP_DIR / "clipmaker.ico", media_type="image/x-icon")


def make_shortcuts():
    """Windows: a real ClipMaker shortcut (own icon, black window starts minimized) on the Desktop
    and in the Start menu, so it can be pinned to the taskbar."""
    if os.name != "nt" or not (APP_DIR / "clipmaker.ico").exists():
        return
    import subprocess
    ps = r"""
$sh = New-Object -ComObject WScript.Shell
$app = '%s'
foreach ($dir in @([Environment]::GetFolderPath('Desktop'), (Join-Path ([Environment]::GetFolderPath('Programs')) ''))) {
  $lnk = Join-Path $dir 'ClipMaker.lnk'
  $s = $sh.CreateShortcut($lnk)
  $s.TargetPath = "$env:WINDIR\System32\cmd.exe"
  $s.Arguments = '/c "' + (Join-Path $app 'start.bat') + '"'
  $s.WorkingDirectory = $app
  $s.IconLocation = (Join-Path $app 'clipmaker.ico') + ',0'
  $s.WindowStyle = 7
  $s.Description = 'ClipMaker'
  $s.Save()
}
""" % str(APP_DIR).replace("'", "''")
    try:
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                       timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass


@app.get("/")
def index():
    return HTMLResponse((APP_DIR / "ui.html").read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-store"})


def ranged(path, request, mime="video/mp4", download_name=None):
    """Send (part of) a video. The file is opened and closed for every chunk, so Windows
    never sees it as 'in use' and you can always delete it."""
    size = path.stat().st_size
    rng = request.headers.get("range")
    start, end, status = 0, size - 1, 200
    if rng:
        m = re.match(r"bytes=(\d*)-(\d*)", rng)
        start = int(m.group(1)) if m and m.group(1) else 0
        end = min(int(m.group(2)) if m and m.group(2) else size - 1, size - 1)
        status = 206
        if start > end:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})

    def gen():
        pos, left = start, end - start + 1
        while left > 0:
            try:
                with open(path, "rb") as f:
                    f.seek(pos)
                    chunk = f.read(min(1 << 20, left))
            except OSError:
                break
            if not chunk:
                break
            pos += len(chunk)
            left -= len(chunk)
            yield chunk
    headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1), "Cache-Control": "no-store"}
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    if download_name:
        from urllib.parse import quote
        headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(download_name)}"
    return StreamingResponse(gen(), status_code=status, media_type=mime, headers=headers)


@app.get("/media/{cid}")
def media(cid: str, request: Request):
    c = clips.get(cid)
    if not c or not c["path"].exists():
        return err("Clip not found", 404)
    return ranged(c["path"], request)


def out_path(name):
    p = (OUTPUT_DIR / name).resolve()
    return p if p.parent == OUTPUT_DIR.resolve() and p.suffix.lower() == ".mp4" and p.exists() else None


@app.get("/out/{name}")
def out_file(name: str, request: Request, dl: int = 0):
    p = out_path(name)
    if not p:
        return err("Not found", 404)
    pretty = re.sub(r"_vertical(_\d+)?\.mp4$", ".mp4", p.name)
    return ranged(p, request, download_name=pretty if dl else None)


# ---- downloaded copies: ClipMaker remembers where it saved each one, so "Delete everywhere" can find it
DL_FILE = OUTPUT_DIR / "downloaded.json"
DOWNLOADS = Path.home() / "Downloads"


def dl_map():
    try:
        return json.loads(DL_FILE.read_text())
    except Exception:
        return {}


def dl_save(m):
    try:
        DL_FILE.write_text(json.dumps(m, indent=1))
    except Exception:
        pass


def live_copies(name):
    return [c for c in dl_map().get(name, []) if Path(c).exists()]


def to_recycle_bin(path):
    """Send a file to the Recycle Bin (so it can be restored). Falls back to deleting it."""
    if os.name == "nt":
        import subprocess
        ps = ("Add-Type -AssemblyName Microsoft.VisualBasic; "
              "[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile($env:CM_DEL, 'OnlyErrorDialogs', 'SendToRecycleBin')")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], env=dict(os.environ, CM_DEL=str(path)),
                           capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode == 0 and not Path(path).exists():
            return
    Path(path).unlink(missing_ok=True)


@app.post("/api/download")
def download(body: dict):
    p = out_path(str(body.get("name", "")))
    if not p:
        return err("That clip is gone.", 404)
    DOWNLOADS.mkdir(exist_ok=True)
    base = re.sub(r"_vertical(_\d+)?$", "", p.stem) or "clip"
    dest = DOWNLOADS / f"{base}.mp4"
    k = 2
    while dest.exists():
        dest = DOWNLOADS / f"{base} ({k}).mp4"; k += 1
    shutil.copy2(p, dest)
    m = dl_map(); m.setdefault(p.name, []).append(str(dest)); dl_save(m)
    return {"ok": True, "saved_as": dest.name}


@app.post("/api/delete-output")
def delete_output(body: dict):
    name = str(body.get("name", ""))
    everywhere = bool(body.get("everywhere"))
    p = out_path(name)
    if not p:
        return err("That clip is already gone.", 404)
    removed_copies = 0
    if everywhere:
        for c in live_copies(name):
            try:
                to_recycle_bin(c); removed_copies += 1
            except Exception:
                pass
    for attempt in range(10):
        try:
            p.unlink()
            p.with_suffix(".srt").unlink(missing_ok=True)
            m = dl_map(); m.pop(name, None); dl_save(m)
            return {"ok": True, "removed_copies": removed_copies}
        except PermissionError:
            time.sleep(0.3)
    return err("Windows says the clip is still open somewhere. Close any app playing it, then try again.", 409)


BOOT = uuid.uuid4().hex[:8]
last_ping = {"t": 0.0}


@app.get("/api/version")
def version():
    last_ping["t"] = time.time()
    return {"v": int((APP_DIR / "ui.html").stat().st_mtime), "boot": BOOT}


def open_window_if_needed(url):
    # a ClipMaker window that's already open checks in every few seconds; if one does, just let it refresh
    if time.time() - last_ping["t"] > 9.5:
        open_window(url)


# ------------------------------------------------------------------ adding clips
@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...)):
    WORK.mkdir(exist_ok=True)
    out = []
    for f in files:
        dest = WORK / f"{uuid.uuid4().hex[:8]}_{Path(f.filename).name}"
        with open(dest, "wb") as fh:
            while chunk := await f.read(1 << 20):
                fh.write(chunk)
        try:
            out.append(add_clip(dest, Path(f.filename).stem, True))
        except Exception:
            dest.unlink(missing_ok=True)
            return err(f"{f.filename} doesn't look like a video ClipMaker can open.")
    return out


@app.post("/api/links")
def links(body: dict):
    WORK.mkdir(exist_ok=True)
    try:
        files = download_urls(body.get("urls", ""), WORK)
    except Exception as e:
        return err(f"Couldn't download that link: {str(e)[:200]}")
    if not files:
        return err("No clips found at that link.")
    return [add_clip(f, re.sub(r"\s*\[[^\]]+\]$", "", f.stem), True) for f in files]


@app.post("/api/folder")
def folder(body: dict):
    d = Path(str(body.get("path", "")).strip().strip('"'))
    if not d.is_dir():
        return err("That folder wasn't found. Copy the path from the File Explorer address bar.")
    vids = sorted(p for p in d.iterdir() if p.suffix.lower() in VIDEO_EXTS)
    if not vids:
        return err("No videos in that folder.")
    return [add_clip(p, p.stem, False) for p in vids[:100]]


@app.post("/api/remove/{cid}")
def remove(cid: str):
    c = clips.pop(cid, None)
    if c and c["temp"]:
        c["path"].unlink(missing_ok=True)
    return {"ok": True}


# ------------------------------------------------------------------ combine clips
combine_count = {"n": 0}


@app.post("/api/combine")
def combine(body: dict):
    pieces = []
    for pc in body.get("pieces", []):
        c = clips.get(pc.get("id"))
        if not c:
            return err("One of the clips in the list isn't loaded anymore.")
        pieces.append((c["path"], pc.get("start") or 0, pc.get("end")))
    if not pieces:
        return err("Add at least one piece first.")
    WORK.mkdir(exist_ok=True)
    combine_count["n"] += 1
    name = (body.get("name") or "").strip() or f"Combined clip {combine_count['n']}"
    out = WORK / f"{uuid.uuid4().hex[:8]}_combined.mp4"
    try:
        engine.combine(pieces, out)
    except Exception as e:
        return err(str(e)[:400])
    return add_clip(out, re.sub(r'[<>:"/\\|?*]', "", name)[:60], True)


# ------------------------------------------------------------------ sound effects / memes / pictures library
LIB = APP_DIR / "library"
LIB_EXT = engine.IMAGE_EXT | engine.SOUND_EXT | engine.GIF_EXT | {".mp4", ".mov", ".webm", ".mkv", ".m4v"}
MIME = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4", ".ogg": "audio/ogg", ".aac": "audio/aac",
        ".flac": "audio/flac", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
        ".bmp": "image/bmp", ".gif": "image/gif", ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
        ".mkv": "video/x-matroska", ".m4v": "video/mp4"}


def lib_path(name):
    p = (LIB / str(name)).resolve()
    return p if p.parent == LIB.resolve() and p.exists() and p.suffix.lower() in LIB_EXT else None


def media_len(p):
    import re as _re
    r = __import__("subprocess").run([engine.ffmpeg_bin(), "-hide_banner", "-i", str(p)], capture_output=True, text=True)
    m = _re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr)
    return round(int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)), 2) if m else 0


@app.get("/api/library")
def library():
    LIB.mkdir(exist_ok=True)
    out = []
    for p in sorted(LIB.iterdir(), key=lambda x: x.name.lower()):
        if p.suffix.lower() in LIB_EXT:
            k = engine.asset_kind(p)
            out.append({"name": p.name, "kind": k, "len": 0 if k == "image" else media_len(p)})
    return out


@app.post("/api/library/upload")
async def library_upload(files: list[UploadFile] = File(...)):
    LIB.mkdir(exist_ok=True)
    added = []
    for f in files:
        nm = re.sub(r'[<>:"/\\|?*]', "", Path(f.filename).name)
        if Path(nm).suffix.lower() not in LIB_EXT:
            continue
        dest = LIB / nm
        k = 2
        while dest.exists():
            dest = LIB / f"{Path(nm).stem} ({k}){Path(nm).suffix}"; k += 1
        with open(dest, "wb") as fh:
            while chunk := await f.read(1 << 20):
                fh.write(chunk)
        added.append(dest.name)
    if not added:
        return err("Those files aren't sounds, pictures or videos ClipMaker can use (try mp3, wav, png, jpg, gif, mp4).")
    return {"added": added}


@app.post("/api/library/delete")
def library_delete(body: dict):
    p = lib_path(body.get("name", ""))
    if p:
        p.unlink(missing_ok=True)
    return {"ok": True}


@app.get("/lib/{name}")
def lib_file(name: str, request: Request):
    p = lib_path(name)
    if not p:
        return err("Not found", 404)
    return ranged(p, request, mime=MIME.get(p.suffix.lower(), "application/octet-stream"))


# ------------------------------------------------------------------ GIPHY search (GIFs + stickers)
def _giphy_item(g):
    im = g.get("images", {})
    prev = im.get("fixed_width", {}) or im.get("fixed_width_small", {})
    orig = im.get("original", {})
    return {"id": g.get("id"), "title": (g.get("title") or "gif").strip(),
            "preview": prev.get("webp") or prev.get("url"), "gif": orig.get("url"), "mp4": orig.get("mp4"),
            "w": int(orig.get("width") or 0), "h": int(orig.get("height") or 0)}


@app.get("/api/giphy")
def giphy(q: str = "", kind: str = "gifs", offset: int = 0):
    import requests
    key = load_settings().get("giphy_key")
    if not key:
        return err("Add your free GIPHY key first.", 400)
    kind = "stickers" if kind == "stickers" else "gifs"
    url = f"https://api.giphy.com/v1/{kind}/" + ("search" if q.strip() else "trending")
    try:
        r = requests.get(url, timeout=15, params={"api_key": key, "q": q.strip(), "limit": 24, "offset": offset, "rating": "pg-13"})
    except Exception:
        return err("Couldn't reach GIPHY. Check your internet.", 502)
    if r.status_code in (401, 403):
        return err("GIPHY didn't accept that key. Copy it again from developers.giphy.com.", 400)
    if r.status_code == 429:
        return err("That's a lot of searches! GIPHY's free key allows 100 an hour. Try again in a bit.", 429)
    if r.status_code != 200:
        return err(f"GIPHY said {r.status_code}.", 502)
    return [_giphy_item(g) for g in r.json().get("data", [])]


@app.post("/api/giphy/save")
def giphy_save(body: dict):
    import requests
    kind = "stickers" if body.get("kind") == "stickers" else "gifs"
    url = body.get("gif") if kind == "stickers" or not body.get("mp4") else body.get("mp4")
    if not url or "giphy.com" not in url:
        return err("That GIF can't be downloaded.")
    LIB.mkdir(exist_ok=True)
    ext = ".gif" if url.split("?")[0].endswith(".gif") else ".mp4"
    title = re.sub(r"(?i)\s*(gif|sticker)\s+by\s+.*$", "", body.get("title") or "")
    base = re.sub(r'[<>:"/\\|?*#%]', "", title).strip()[:40] or ("sticker" if kind == "stickers" else "gif")
    if kind == "stickers":
        base += " (sticker)"
    dest = LIB / f"{base}{ext}"
    k = 2
    while dest.exists():
        dest = LIB / f"{base} {k}{ext}"; k += 1
    try:
        with requests.get(url, stream=True, timeout=30) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    f.write(chunk)
    except Exception:
        dest.unlink(missing_ok=True)
        return err("Couldn't download that one. Try another.")
    return {"name": dest.name}


# ------------------------------------------------------------------ AI helper
@app.get("/api/ai/status")
def ai_status():
    return ai.status()


@app.post("/api/ai/setup")
def ai_setup():
    ai.setup()
    return ai.status()


@app.post("/api/ai/chat")
def ai_chat(body: dict):
    ctx = body.get("context", {})
    ctx["library"] = library()
    try:
        return ai.chat(body.get("messages", []), ctx)
    except Exception as e:
        return err(f"The AI had a hiccup: {str(e)[:200]}", 500)


# ------------------------------------------------------------------ smarts
@app.post("/api/detect/{cid}")
def detect(cid: str):
    c = clips.get(cid)
    if not c:
        return err("Clip not found", 404)
    cam, face = engine.detect_cam(c["path"])
    return {"cam": cam, "face": face}


@app.post("/api/subs/{cid}")
def subs(cid: str, body: dict):
    c = clips.get(cid)
    if not c:
        return err("Clip not found", 404)
    try:
        return {"words": engine.words_for(c["path"], body.get("model", "small"), body.get("lang", "Auto"))}
    except Exception as e:
        return err(f"Subtitles failed: {str(e)[:300]}", 500)


# ------------------------------------------------------------------ rendering
def run_job(jid, cid, settings):
    j = jobs[jid]
    c = clips[cid]

    def log(m):
        j["log"].append(m)

    def prog(f):
        j["progress"] = f

    ovs = []
    for o in settings.get("overlays") or []:
        lp = lib_path(o.get("name", ""))
        if lp:
            ovs.append(dict(o, path=str(lp)))
    settings = dict(settings, overlays=ovs)
    try:
        out = engine.render(c["path"], c["name"], settings, log, prog)
        j.update(status="done", output=out.name)
    except Exception as e:
        j.update(status="error", error=str(e)[:600])


@app.post("/api/render/{cid}")
def render(cid: str, body: dict):
    if cid not in clips:
        return err("Clip not found", 404)
    s = body.get("settings", {})
    jid = uuid.uuid4().hex[:10]
    jobs[jid] = {"status": "running", "log": [], "progress": 0, "output": None, "error": None}
    threading.Thread(target=run_job, args=(jid, cid, s), daemon=True).start()
    return {"job": jid}


@app.get("/api/job/{jid}")
def job(jid: str):
    return jobs.get(jid) or err("No such job", 404)


@app.get("/api/outputs")
def outputs():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    vids = sorted(OUTPUT_DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)[:40]
    return [{"name": p.name, "mb": round(p.stat().st_size / 1048576, 1), "time": p.stat().st_mtime,
             "downloaded": len(live_copies(p.name))} for p in vids]


@app.post("/api/open-output")
def open_output():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if hasattr(os, "startfile"):
        os.startfile(str(OUTPUT_DIR))
    return {"ok": True}


# ------------------------------------------------------------------ settings + twitch
watcher = Watcher(APP_DIR, None)


def auto_process(url):
    st = load_settings()
    if not st.get("twitch_auto_render"):
        # default: put the clip in Make clips so you can edit it before making it
        INBOX.mkdir(exist_ok=True)
        files = download_urls(url, INBOX)
        if not files:
            raise RuntimeError("couldn't download that clip")
        for f in files:
            add_clip(f, clean_title(f.stem), True, source="twitch")
        return Path(files[-1].name + " (in Make clips, ready to edit)")
    WORK.mkdir(exist_ok=True)
    files = download_urls(url, WORK)
    if not files:
        raise RuntimeError("couldn't download that clip")
    s = dict(st.get("layout", {}))
    s.update(trim_start=None, trim_end=None, hook="", words=None)
    if not s.get("cam_sure"):
        s["cam"] = s["face"] = None   # let it find the webcam itself
    out = None
    for f in files:
        try:
            out = engine.render(f, re.sub(r"\s*\[[^\]]+\]$", "", f.stem), s, lambda m: None)
        finally:
            f.unlink(missing_ok=True)
    return out


watcher.process_fn = auto_process


def twitch_creds(s):
    """(client_id, secret-or-login-tokens, username) or None. 'Log in with Twitch' wins over developer keys."""
    if s.get("twitch_tokens") and s.get("twitch_login"):
        return twitch_auto.PUBLIC_CLIENT_ID, s["twitch_tokens"], s["twitch_login"]
    if s.get("twitch_login") and s.get("twitch_client_id") and s.get("twitch_secret"):
        return s["twitch_client_id"], s["twitch_secret"], s["twitch_login"]
    return None


twitch_auto.TOKEN_SAVER = lambda toks: save_settings(twitch_tokens=toks)
_login = {}


@app.post("/api/twitch/login")
def twitch_login_start():
    try:
        j = twitch_auto.start_login()
    except Exception as e:
        return err(str(e)[:200], 500)
    _login.clear()
    _login.update(device=j["device_code"], until=time.time() + int(j.get("expires_in", 900)))
    try:
        webbrowser.open(j.get("verification_uri", "https://www.twitch.tv/activate"))   # their normal browser, where they're signed in
    except Exception:
        pass
    return {"code": j["user_code"], "url": j.get("verification_uri", "https://www.twitch.tv/activate")}


@app.get("/api/twitch/login/check")
def twitch_login_check():
    if not _login.get("device"):
        return {"done": False, "waiting": False}
    try:
        toks = twitch_auto.finish_login(_login["device"])
    except Exception as e:
        _login.clear()
        return err(str(e)[:200])
    if not toks:
        return {"done": False, "waiting": True}
    who = twitch_auto.whoami(toks["access"]) or {}
    _login.clear()
    save_settings(twitch_tokens=toks, twitch_login=who.get("login", ""))
    return {"done": True, "login": who.get("login", "")}


@app.post("/api/twitch/logout")
def twitch_logout():
    watcher.stop()
    save_settings(twitch_tokens=None, twitch_watching=False)
    return {"ok": True}


@app.get("/api/settings")
def get_settings():
    s = load_settings()
    return {"layout": s.get("layout", {}), "twitch_login": s.get("twitch_login", ""),
            "twitch_client_id": s.get("twitch_client_id", ""), "twitch_secret_saved": bool(s.get("twitch_secret")),
            "twitch_mode": s.get("twitch_mode", "after"), "giphy_saved": bool(s.get("giphy_key")),
            "twitch_auto_render": bool(s.get("twitch_auto_render")),
            "twitch_logged_in": bool(s.get("twitch_tokens")), "twitch_ready": bool(twitch_creds(s)),
            "ai_installed": ai.installed(), "setup_done": bool(s.get("setup_done"))}


@app.post("/api/settings")
def set_settings(body: dict):
    upd = {}
    for k in ("twitch_login", "twitch_client_id", "twitch_mode", "twitch_auto_render", "setup_done"):
        if k in body:
            upd[k] = body[k]
    if body.get("twitch_secret"):
        upd["twitch_secret"] = body["twitch_secret"]
    if "giphy_key" in body:
        upd["giphy_key"] = str(body["giphy_key"]).strip()
    if "layout" in body:
        upd["layout"] = {k: v for k, v in body["layout"].items() if k in engine.DEFAULTS}
    save_settings(**upd)
    return {"ok": True}


@app.post("/api/twitch/start")
def twitch_start():
    s = load_settings()
    c = twitch_creds(s)
    if not c:
        return err("Press Log in with Twitch first.")
    watcher.start(*c, "during" if s.get("twitch_mode") == "during" else "after")
    save_settings(twitch_watching=True)
    return {"ok": True}


@app.post("/api/twitch/stop")
def twitch_stop():
    watcher.stop()
    save_settings(twitch_watching=False)
    return {"ok": True}


@app.post("/api/twitch/grab")
def twitch_grab(body: dict):
    s = load_settings()
    c = twitch_creds(s)
    if not c:
        return err("Press Log in with Twitch first.")
    watcher.grab_recent(*c, body.get("hours", 12))
    return {"ok": True}


@app.get("/api/twitch/debug")
def twitch_debug():
    from twitch_auto import Twitch
    s = load_settings()
    try:
        c = twitch_creds(s) or (s.get("twitch_client_id"), s.get("twitch_secret"), s.get("twitch_login"))
        tw = Twitch(c[0], c[1])
        uid, name = tw.user_id(s.get("twitch_login"))
        return dict(tw.debug(uid), channel=name, live=bool(tw.live_stream(uid)))
    except Exception as e:
        return err(str(e)[:300], 500)


@app.get("/api/twitch/status")
def twitch_status():
    running = bool(watcher.thread and watcher.thread.is_alive())
    return {"running": running, "status": watcher.status, "log": list(reversed(watcher.lines[-60:]))}


# ------------------------------------------------------------------ updates (friends copy)
@app.get("/api/update")
def update_status():
    return updater.status()


@app.post("/api/update")
def update_apply():
    try:
        updater.apply()
    except Exception as e:
        return err("The update didn't download. Check your internet and try again. (" + str(e)[:120] + ")", 500)
    # exit code 42 tells start.bat to start ClipMaker again in the same window
    threading.Timer(1.0, lambda: os._exit(42)).start()
    return {"ok": True}


def _update_loop():
    while True:
        updater.check()
        time.sleep(3 * 3600)


# ------------------------------------------------------------------ start
def open_window(url):
    """Open ClipMaker in its own app window using a clean browser profile (no extensions),
    so Chrome extensions can't interfere with video playback."""
    import subprocess
    profile = APP_DIR / "browser-profile"
    candidates = []
    for base in (os.environ.get("PROGRAMFILES", r"C:\Program Files"), os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
                 os.environ.get("LOCALAPPDATA", "")):
        if base:
            candidates += [Path(base) / "Google/Chrome/Application/chrome.exe",
                           Path(base) / "Microsoft/Edge/Application/msedge.exe"]
    for exe in candidates:
        if exe.exists():
            try:
                subprocess.Popen([str(exe), f"--app={url}", f"--user-data-dir={profile}", "--window-size=1500,950",
                                  "--no-first-run", "--no-default-browser-check", "--disable-extensions"])
                return
            except Exception:
                pass
    webbrowser.open(url)


def already_running():
    with socket.socket() as sk:
        sk.settimeout(0.5)
        return sk.connect_ex(("127.0.0.1", PORT)) == 0


class _QuietResets:
    """Hide harmless 'connection was closed' noise from Windows when the video player skips around."""
    def filter(self, record):
        exc = record.exc_info[1] if record.exc_info else None
        return not isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)) \
            and "WinError 10054" not in record.getMessage()


if __name__ == "__main__":
    import logging
    logging.getLogger("asyncio").addFilter(_QuietResets())
    url = f"http://127.0.0.1:{PORT}"
    if "--open" in sys.argv or already_running():
        open_window(url)
        raise SystemExit(0)
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(exist_ok=True)
    load_inbox()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    old = APP_DIR / "output"
    if old.is_dir() and old.resolve() != OUTPUT_DIR.resolve():
        for f in list(old.glob("*.mp4")) + list(old.glob("*.srt")):
            try:
                shutil.move(str(f), str(OUTPUT_DIR / f.name))
            except Exception:
                pass
        shutil.rmtree(old, ignore_errors=True)
    print(f"\n  ClipMaker is running at {url}\n  Keep this window open - closing it turns ClipMaker off.\n")
    threading.Timer(9.5, lambda: open_window_if_needed(url)).start()
    threading.Thread(target=make_shortcuts, daemon=True).start()
    threading.Thread(target=_update_loop, daemon=True).start()
    if ai.installed():
        threading.Thread(target=ai.start, daemon=True).start()
    st = load_settings()
    if st.get("twitch_watching") and twitch_creds(st):
        watcher.start(*twitch_creds(st), "during" if st.get("twitch_mode") == "during" else "after")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
