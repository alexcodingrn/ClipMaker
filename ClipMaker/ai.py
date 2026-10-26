"""
ClipMaker AI helper: a small free AI that runs on this PC (llama.cpp + Qwen2.5 3B).
No account, no cost, works offline after the one-time download (~2.2 GB).
"""
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import zipfile
from pathlib import Path

import requests

APP_DIR = Path(__file__).resolve().parent
AI_DIR = APP_DIR / "ai"
BIN_DIR = AI_DIR / "bin"
MODEL = AI_DIR / "qwen2.5-3b-instruct-q4_k_m.gguf"
MODEL_URL = "https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/qwen2.5-3b-instruct-q4_k_m.gguf"
RELEASES = "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=15"
PORT = 7861

state = {"status": "idle", "progress": 0.0, "message": "", "error": None}
_proc = None
_lock = threading.Lock()


def server_exe():
    hits = list(BIN_DIR.rglob("llama-server.exe")) + list(BIN_DIR.rglob("llama-server"))
    return hits[0] if hits else None


def installed():
    return MODEL.exists() and MODEL.stat().st_size > 1_000_000_000 and server_exe() is not None


def port_open():
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def status():
    st = dict(state)
    st["installed"] = installed()
    st["running"] = port_open()
    if st["installed"] and state["status"] in ("idle", "done"):
        st["status"] = "ready"
    return st


# ------------------------------------------------------------------ one-time setup
def _download(url, dest, label, part, parts):
    tmp = dest.with_suffix(dest.suffix + ".part")
    with requests.get(url, stream=True, timeout=60, headers={"User-Agent": "ClipMaker"}) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0)) or 1
        got = 0
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
                got += len(chunk)
                state["progress"] = (part + got / total) / parts
                state["message"] = f"{label}: {got / 1048576:.0f} of {total / 1048576:.0f} MB"
    tmp.replace(dest)


def _setup():
    try:
        AI_DIR.mkdir(exist_ok=True)
        if not server_exe():
            state["message"] = "Finding the AI engine..."
            rels = requests.get(RELEASES, timeout=30, headers={"User-Agent": "ClipMaker"}).json()
            if isinstance(rels, dict):
                rels = [rels]
            pick = None
            for want in ("bin-win-vulkan-x64.zip", "bin-win-cpu-x64.zip"):
                for rel in rels:
                    pick = next((a for a in rel.get("assets", []) if a["name"].endswith(want)), None)
                    if pick:
                        break
                if pick:
                    break
            if not pick:
                raise RuntimeError("Couldn't find the AI engine download.")
            z = AI_DIR / "engine.zip"
            _download(pick["browser_download_url"], z, "Downloading the AI engine", 0, 2)
            shutil.rmtree(BIN_DIR, ignore_errors=True)
            with zipfile.ZipFile(z) as zz:
                zz.extractall(BIN_DIR)
            z.unlink(missing_ok=True)
        if not (MODEL.exists() and MODEL.stat().st_size > 1_000_000_000):
            _download(MODEL_URL, MODEL, "Downloading the AI brain", 1, 2)
        state.update(status="starting", progress=1.0, message="Starting the AI...")
        start()
        state.update(status="done", message="The AI helper is ready.")
    except Exception as e:
        state.update(status="error", error=str(e)[:300], message="Setup didn't finish.")


def setup():
    if state["status"] == "downloading":
        return
    state.update(status="downloading", progress=0.0, error=None, message="Starting the download...")
    threading.Thread(target=_setup, daemon=True).start()


# ------------------------------------------------------------------ run the AI
def start(timeout=120):
    global _proc
    with _lock:
        if port_open():
            return True
        exe = server_exe()
        if not exe or not MODEL.exists():
            return False
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        for gpu_layers in ("99", "0"):  # use the graphics card if it can, otherwise the processor
            _proc = subprocess.Popen([str(exe), "-m", str(MODEL), "--port", str(PORT), "--host", "127.0.0.1",
                                      "-c", "8192", "-ngl", gpu_layers],
                                     cwd=str(exe.parent), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     creationflags=flags)
            t0 = time.time()
            while time.time() - t0 < timeout:
                if _proc.poll() is not None:
                    break
                try:
                    if requests.get(f"http://127.0.0.1:{PORT}/health", timeout=2).status_code == 200:
                        return True
                except Exception:
                    pass
                time.sleep(1)
            try:
                _proc.kill()
            except Exception:
                pass
        return False


def stop():
    try:
        if _proc:
            _proc.kill()
    except Exception:
        pass


SYSTEM = """You are the helper inside ClipMaker, an app that turns a streamer's gaming clips into vertical TikTok / Reels / Shorts videos.
Be friendly, short and casual. The user is a new streamer and not techy.

You can DO things in the app by returning actions. Always answer with JSON only, in this exact shape:
{"reply": "<one or two short sentences to the user>", "actions": [ ... ]}

Actions you can use (seconds are numbers, clips are numbered like in CLIPS below):
{"do":"combine","pieces":[{"clip":1,"start":0,"end":5},{"clip":2,"start":0,"end":3}]}   join parts of clips, in order, into one new clip
{"do":"trim","start":2,"end":9}                    keep only this part of the CURRENT clip
{"do":"add_effect","name":"<exact name from LIBRARY>","at":7.5,"seconds":2,"where":"center"}   where = center, full or game
{"do":"hook","text":"he did NOT see that coming"}  big text at the top for the first seconds
{"do":"speed","value":1.25}                         1, 1.1, 1.25 or 1.5
{"do":"seek","at":12}                               jump the player to a moment
{"do":"setting","key":"game_zoom","value":60}       keys: game_zoom 0-100, cam_pct 25-50, font_size 56-140, per_chunk 1-5, subs true/false, vivid true/false
{"do":"caption","text":"post caption with 3-5 hashtags"}
{"do":"make_clip"}                                   render the current clip (only when the user asks to make/export it)

Rules:
- ONLY do what the user asked for. Never add a hook, caption, effect or setting they didn't ask for.
- "the first clip" means clip 1, "second clip" means clip 2, and so on. "this clip" means the CURRENT clip.
- "3 seconds of clip 2" with no start means start 0, end 3.
- To find a moment by what was said, use the WORDS list (word: time in seconds of the CURRENT clip).
- Only use LIBRARY names that exist. If the user wants a sound that isn't there, say so and suggest one that is.
- If nothing needs doing (just a question), return "actions": [].
- Never invent clips that aren't in CLIPS.
- Captions: short and hype, written like the streamer talking, about what actually happens in the clip (use WORDS), plus 3-5 hashtags like #gaming #fyp and the game's name if you know it.
- For a sound effect, "where" doesn't matter; leave it out.

Example:
User: combine the first clip 0-5 seconds, then 3 seconds of the second clip, then the first clip from 11 to 15
{"reply": "On it! Combining those parts for you.", "actions": [{"do":"combine","pieces":[{"clip":1,"start":0,"end":5},{"clip":2,"start":0,"end":3},{"clip":1,"start":11,"end":15}]}]}
"""


def describe(ctx):
    lines = ["CLIPS:"]
    for i, c in enumerate(ctx.get("clips", []), 1):
        mark = "  <- CURRENT" if c.get("current") else ""
        lines.append(f"{i}. {c.get('name')} ({c.get('dur', 0):.1f} s){mark}")
    if not ctx.get("clips"):
        lines.append("(no clips loaded yet)")
    lines.append(f"PLAYER TIME: {ctx.get('time', 0):.1f} s")
    trim = ctx.get("trim")
    if trim:
        lines.append(f"CURRENT TRIM: {trim}")
    lib = ctx.get("library", [])
    lines.append("LIBRARY: " + (", ".join(f"{x['name']} ({x['kind']})" for x in lib) if lib else "(empty - the user can add sounds/memes in the Sounds & memes tab)"))
    words = ctx.get("words") or []
    if words:
        lines.append("WORDS: " + " ".join(f"{w[2]}:{w[0]:.1f}" for w in words[:250]))
    return "\n".join(lines)


def chat(messages, ctx):
    if not port_open() and not start():
        raise RuntimeError("The AI helper isn't set up yet.")
    prompt_file = APP_DIR / "ai_prompt.txt"
    system = prompt_file.read_text(encoding="utf-8") if prompt_file.exists() else SYSTEM
    msgs = [{"role": "system", "content": system + "\n\nAPP RIGHT NOW:\n" + describe(ctx)}]
    msgs += [m for m in messages[-10:] if m.get("role") in ("user", "assistant") and m.get("content")]
    r = requests.post(f"http://127.0.0.1:{PORT}/v1/chat/completions", timeout=180, json={
        "messages": msgs, "temperature": 0.2, "max_tokens": 700,
        "response_format": {"type": "json_object"}})
    r.raise_for_status()
    text = r.json()["choices"][0]["message"]["content"]
    try:
        data = json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        data = json.loads(m.group(0)) if m else {"reply": text, "actions": []}
    if not isinstance(data.get("actions"), list):
        data["actions"] = []
    data["reply"] = str(data.get("reply") or "Done.")
    return data
