"""
ClipMaker for friends: one-click updates.
Checks a public GitHub repo (set in update.json) for a newer VERSION, downloads it,
swaps in the new code and restarts. Settings, logins, clips and the AI are never touched.
"""
import io
import json
import shutil
import threading
import zipfile
from pathlib import Path

import requests

APP_DIR = Path(__file__).resolve().parent
CONF = APP_DIR / "update.json"
KEEP = {"settings.json", "start.bat", "update.json", "downloaded.json"}   # never overwritten
CODE_EXT = {".py", ".html", ".txt", ".ps1", ".json", ".md"}
state = {"checking": False, "latest": None, "error": None, "updating": False}


def conf():
    try:
        c = json.loads(CONF.read_text())
        return c if c.get("repo") and "OWNER" not in c["repo"] else None
    except Exception:
        return None


def current():
    try:
        return int((APP_DIR / "VERSION").read_text().strip())
    except Exception:
        return 0


def _raw(c, name):
    return f"https://raw.githubusercontent.com/{c['repo']}/{c.get('branch', 'main')}/{name}"


def check():
    c = conf()
    if not c:
        return
    state.update(checking=True, error=None)
    try:
        r = requests.get(_raw(c, "VERSION"), timeout=15, headers={"Cache-Control": "no-cache"})
        r.raise_for_status()
        state["latest"] = int(r.text.strip())
    except Exception as e:
        state["error"] = str(e)[:200]
    state["checking"] = False


def check_later():
    threading.Thread(target=check, daemon=True).start()


def status():
    c = conf()
    latest = state["latest"]
    return {"enabled": bool(c), "current": current(), "latest": latest, "updating": state["updating"],
            "available": bool(c and latest and latest > current()), "error": state["error"]}


def apply():
    """Download and install the new version. Returns True when the files are in place (caller restarts)."""
    c = conf()
    if not c:
        raise RuntimeError("Updates aren't set up in this copy.")
    state["updating"] = True
    try:
        url = f"https://codeload.github.com/{c['repo']}/zip/refs/heads/{c.get('branch', 'main')}"
        r = requests.get(url, timeout=120)
        r.raise_for_status()
        z = zipfile.ZipFile(io.BytesIO(r.content))
        names = z.namelist()
        root = names[0].split("/")[0] + "/"
        sub = root + "ClipMaker/" if any(n.startswith(root + "ClipMaker/") for n in names) else root
        old_req = (APP_DIR / "requirements.txt").read_text() if (APP_DIR / "requirements.txt").exists() else ""
        for n in names:
            if not n.startswith(sub) or n.endswith("/"):
                continue
            rel = n[len(sub):]
            if not rel or rel in KEEP or rel.startswith(("runtime/", "ai/", "inbox/", "work/", ".")):
                continue
            dest = APP_DIR / rel
            if rel.startswith("library/"):
                if dest.exists():
                    continue          # never replace sounds they already have
            elif rel != "VERSION" and Path(rel).suffix.lower() not in CODE_EXT:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".new")
            tmp.write_bytes(z.read(n))
            tmp.replace(dest)
        new_req = (APP_DIR / "requirements.txt").read_text() if (APP_DIR / "requirements.txt").exists() else ""
        if new_req != old_req:
            import subprocess, sys
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", str(APP_DIR / "requirements.txt")],
                           cwd=str(APP_DIR), timeout=900)
        return True
    finally:
        state["updating"] = False
