"""
ClipMaker - turn stream clips into vertical TikTok / Reels / Shorts videos.

  * facecam auto-detected and placed on top, centered on your face
  * gameplay on the bottom, with a zoom slider (whole screen <-> fill the space)
  * word-by-word animated subtitles (AI transcription with faster-whisper)
  * input: upload files, a folder path, or clip URLs (Twitch, YouTube, Kick...)

Run start.bat (Windows) or start.command (Mac). A page opens in your browser.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import cv2
import numpy as np
import gradio as gr

OUT_W, OUT_H = 1080, 1920
APP_DIR = Path(__file__).resolve().parent
# Finished clips live in ClipMaker's own library (not in Downloads). Use the Download button to save one.
OUTPUT_DIR = (Path(os.environ["LOCALAPPDATA"]) / "ClipMaker" / "clips") if os.environ.get("LOCALAPPDATA") else APP_DIR / "output"
SETTINGS_FILE = APP_DIR / "settings.json"
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".flv", ".m4v", ".ts"}

FACE_MODES = ["Auto-detect", "Top-left", "Top-right", "Bottom-left", "Bottom-right", "No facecam"]
CAPTION_SPOTS = ["Middle (between face & game)", "Lower (on gameplay)", "On facecam"]


# ----------------------------------------------------------------- helpers
def ffmpeg_bin():
    local = APP_DIR / "runtime" / "ffmpeg.exe"
    if local.exists():
        return str(local)
    p = shutil.which("ffmpeg")
    if p:
        return p
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def even(v):
    v = int(round(v))
    return max(2, v - (v % 2))


def probe(path):
    cap = cv2.VideoCapture(str(path))
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    cap.release()
    if W == 0 or H == 0:
        raise RuntimeError("Couldn't read this video file.")
    return W, H, n, fps


def grab_frame(path, where=0.5):
    cap = cv2.VideoCapture(str(path))
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, int(n * where)))
    ok, fr = cap.read()
    cap.release()
    return fr if ok else None


def hex_to_ass(color):
    color = (color or "#FFD400").strip()
    m = re.match(r"rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)", color)
    if m:
        r, g, b = (int(float(x)) for x in m.groups())
    else:
        c = color.lstrip("#")[:6].ljust(6, "0")
        r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    return f"&H00{b:02X}{g:02X}{r:02X}"


# ------------------------------------------------------ facecam detection
_cascades = None


def get_cascades():
    global _cascades
    if _cascades is None:
        try:
            base = cv2.data.haarcascades
            _cascades = (cv2.CascadeClassifier(base + "haarcascade_frontalface_default.xml"),
                         cv2.CascadeClassifier(base + "haarcascade_profileface.xml"))
        except AttributeError:
            _cascades = False  # OpenCV 5+ has no Haar detector; requirements pin 4.x
    return _cascades


def detect_face(path, samples=40):
    """Look at frames across the clip and return the face box that shows up
    most consistently (x, y, w, h) in source pixels, or None."""
    cas = get_cascades()
    if not cas:
        return None
    _front, _profile = cas
    W, H, n, _ = probe(path)
    cap = cv2.VideoCapture(str(path))
    s = 960.0 / W if W > 960 else 1.0
    boxes = []
    for i in range(samples):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int((i + 0.5) * n / samples))
        ok, fr = cap.read()
        if not ok:
            continue
        gray = cv2.cvtColor(cv2.resize(fr, None, fx=s, fy=s), cv2.COLOR_BGR2GRAY)
        gray = cv2.equalizeHist(gray)
        mn = (int(22), int(22))
        found = list(_front.detectMultiScale(gray, 1.1, 6, minSize=mn))
        if not found:
            found = list(_profile.detectMultiScale(gray, 1.1, 6, minSize=mn))
            flipped = cv2.flip(gray, 1)
            gw = gray.shape[1]
            for (x, y, w, h) in _profile.detectMultiScale(flipped, 1.1, 6, minSize=mn):
                found.append((gw - x - w, y, w, h))
        for (x, y, w, h) in found:
            boxes.append((x / s, y / s, w / s, h / s))
    cap.release()
    if not boxes:
        return None

    # cluster: pick the spot where a face appears in the most frames
    best, best_members = None, []
    for b in boxes:
        cx, cy = b[0] + b[2] / 2, b[1] + b[3] / 2
        members = [o for o in boxes
                   if abs(o[0] + o[2] / 2 - cx) < 0.6 * b[2] and abs(o[1] + o[3] / 2 - cy) < 0.6 * b[3]]
        if len(members) > len(best_members):
            best, best_members = b, members
    if len(best_members) < max(3, int(samples * 0.12)):
        return None
    arr = np.array(best_members)
    return tuple(float(v) for v in np.median(arr, axis=0))


# ------------------------------------------------------------ the layout
def clamp_rect(cx, cy, cw, ch, W, H):
    cw, ch = min(cw, W), min(ch, H)
    x = min(max(0, cx - cw / 2), W - cw)
    y = min(max(0, cy - ch / 2), H - ch)
    return int(x), int(y), even(cw), even(ch)


def plan_layout(W, H, face_mode, face_box, opts):
    has_face = face_mode != "No facecam" and (face_mode != "Auto-detect" or face_box is not None)
    face_h = even(OUT_H * opts["face_pct"] / 100) if has_face else 0
    game_h = OUT_H - face_h
    plan = {"face": None, "face_h": face_h, "game_h": game_h, "src": (W, H), "hide": None}

    if has_face:
        A = OUT_W / face_h
        zoom = opts["face_zoom"]
        if face_mode == "Auto-detect":
            fx, fy, fw, fh = face_box
            cw = fw * zoom
            ch = cw / A
            if ch > H:
                ch = H; cw = ch * A
            if cw > W:
                cw = W; ch = cw / A
            # aim a little below the face center so head + shoulders sit nicely
            plan["face"] = clamp_rect(fx + fw / 2, fy + fh / 2 + 0.12 * fh, cw, ch, W, H)
            # guess the webcam overlay around the face, snapping to nearby frame edges
            hx0, hx1 = fx + fw / 2 - fw * 1.9, fx + fw / 2 + fw * 1.9
            hy0, hy1 = fy + fh / 2 - fh * 1.5, fy + fh / 2 + fh * 1.9
            if hx0 < W * 0.08: hx0 = 0
            if hx1 > W * 0.92: hx1 = W
            if hy0 < H * 0.08: hy0 = 0
            if hy1 > H * 0.92: hy1 = H
            hide = (max(0, hx0), max(0, hy0), min(W, hx1), min(H, hy1))
        else:
            cw = W * 0.6 / zoom
            ch = cw / A
            if ch > H:
                ch = H; cw = ch * A
            left = "left" in face_mode.lower()
            top = face_mode.lower().startswith("top")
            cx = cw / 2 if left else W - cw / 2
            cy = ch / 2 if top else H - ch / 2
            plan["face"] = clamp_rect(cx, cy, cw, ch, W, H)
            # cut out generously from the corner, since overlays are often bigger than the crop
            hw, hh = cw * 1.3, ch * 1.5
            hx0 = 0 if left else W - hw
            hy0 = 0 if top else H - hh
            hide = (hx0, hy0, hx0 + hw, hy0 + hh)
        if opts.get("hide_cam", True):
            x0, y0, x1, y1 = hide
            pad = min(W, H) * 0.012
            x0, y0, x1, y1 = max(0, x0 - pad), max(0, y0 - pad), min(W, x1 + pad), min(H, y1 + pad)
            plan["hide"] = (int(x0), int(y0), even(x1 - x0), even(y1 - y0))

    # gameplay: zoom 0 = whole screen, 1 = fills the panel (crops the sides)
    GA = OUT_W / game_h
    fill_w = min(W, H * GA)
    t = max(0.0, min(1.0, opts["game_zoom"] / 100))
    cw = W - t * (W - fill_w)
    ch = min(H, cw / GA)
    off = opts["game_offset"]
    cx = W / 2 + off * (W - cw) / 2
    plan["game"] = clamp_rect(cx, H / 2, cw, ch, W, H)
    gx, gy, gw, gh = plan["game"]
    plan["fit"] = (OUT_W, min(game_h, even(OUT_W * gh / gw)))
    return plan


def caption_y(plan, spot):
    fh, gh = plan["face_h"], plan["game_h"]
    if fh == 0:
        return int(OUT_H * 0.70)
    if spot.startswith("Lower"):
        return int(fh + gh * 0.70)
    if spot.startswith("On facecam"):
        return int(fh * 0.82)
    return int(fh)


def build_filter(plan, subs_file, opts=None):
    opts = opts or {}
    fw, fh = plan["fit"]
    gx, gy, gw, gh = plan["game"]
    need_bg = fh < plan["game_h"] - 2
    parts = []
    if plan["face"]:
        parts.append("[0:v]split=2[vf][vg]")
        x, y, w, h = plan["face"]
        parts.append(f"[vf]crop={w}:{h}:{x}:{y},scale={OUT_W}:{plan['face_h']}:flags=lanczos,setsar=1[top]")
        game = "[vg]"
    else:
        game = "[0:v]"
    if plan["hide"]:
        # paint the facecam out of the gameplay with a blurred mirror of the game beside it
        W, H = plan["src"]
        x, y, w, h = plan["hide"]
        left, top = x + w / 2 < W / 2, y + h / 2 < H / 2
        side = (W - x - w) if left else x
        vert = (H - y - h) if top else y
        if side / w >= vert / h and side >= 8:
            sw = even(min(w, side)); sx = x + w if left else x - sw
            patch = f"crop={sw}:{h}:{sx}:{y},hflip"
        elif vert >= 8:
            sh = even(min(h, vert)); sy = y + h if top else y - sh
            patch = f"crop={w}:{sh}:{x}:{sy},vflip"
        else:
            patch = None
        if patch:
            sigma = max(4, int(min(w, h) * 0.04))
            parts.append(f"{game}split=2[gb0][gp0]")
            parts.append(f"[gp0]{patch},scale={w}:{h},gblur=sigma={sigma}[patch]")
            parts.append(f"[gb0][patch]overlay={x}:{y}[gclean]")
            game = "[gclean]"
    if need_bg:
        parts.append(f"{game}split=2[gbg][gfg]")
        parts.append(f"[gbg]scale={OUT_W}:{plan['game_h']}:force_original_aspect_ratio=increase,"
                     f"crop={OUT_W}:{plan['game_h']},boxblur=25:3,eq=brightness=-0.08[bg]")
        parts.append(f"[gfg]crop={gw}:{gh}:{gx}:{gy},scale={fw}:{fh}:flags=lanczos[fg]")
        parts.append("[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[bot]")
    else:
        parts.append(f"{game}crop={gw}:{gh}:{gx}:{gy},scale={OUT_W}:{plan['game_h']}:flags=lanczos,setsar=1[bot]")
    parts.append("[top][bot]vstack=inputs=2[stk]" if plan["face"] else "[bot]null[stk]")
    post = []
    if opts.get("vivid"):
        post.append("eq=saturation=1.25:contrast=1.06")
    if subs_file:
        post.append(f"ass={subs_file}")
    d = plan.get("dur", 0)
    if opts.get("fades") and d > 1.5:
        post.append(f"fade=t=in:d=0.25,fade=t=out:st={max(0, d - 0.4):.2f}:d=0.4")
    post.append("format=yuv420p")
    parts.append(f"[stk]{','.join(post)}[outv]")
    return ";".join(parts)


def clean_frame(frame, plan):
    """Numpy version of the facecam cut-out, for the preview."""
    if not plan["hide"]:
        return frame
    H, W = frame.shape[:2]
    x, y, w, h = plan["hide"]
    left, top = x + w / 2 < W / 2, y + h / 2 < H / 2
    side = (W - x - w) if left else x
    vert = (H - y - h) if top else y
    f = frame.copy()
    if side / w >= vert / h and side >= 8:
        sw = even(min(w, side)); sx = x + w if left else x - sw
        patch = cv2.flip(frame[y:y + h, sx:sx + sw], 1)
    elif vert >= 8:
        sh = even(min(h, vert)); sy = y + h if top else y - sh
        patch = cv2.flip(frame[sy:sy + sh, x:x + w], 0)
    else:
        return f
    patch = cv2.resize(patch, (w, h))
    f[y:y + h, x:x + w] = cv2.GaussianBlur(patch, (0, 0), max(4, int(min(w, h) * 0.04)))
    return f


def preview_image(frame, plan, opts):
    out = np.zeros((OUT_H, OUT_W, 3), np.uint8)
    if plan["face"]:
        x, y, w, h = plan["face"]
        out[:plan["face_h"]] = cv2.resize(frame[y:y + h, x:x + w], (OUT_W, plan["face_h"]), interpolation=cv2.INTER_AREA)
    frame = clean_frame(frame, plan)
    gh_panel = plan["game_h"]
    sc = max(OUT_W / frame.shape[1], gh_panel / frame.shape[0])
    big = cv2.resize(frame, (int(frame.shape[1] * sc) + 1, int(frame.shape[0] * sc) + 1))
    y0 = (big.shape[0] - gh_panel) // 2; x0 = (big.shape[1] - OUT_W) // 2
    bot = (cv2.GaussianBlur(big[y0:y0 + gh_panel, x0:x0 + OUT_W], (0, 0), 25) * 0.85).astype(np.uint8)
    gx, gy, gw, gh = plan["game"]
    fw, fh = plan["fit"]
    fg = cv2.resize(frame[gy:gy + gh, gx:gx + gw], (fw, fh), interpolation=cv2.INTER_AREA)
    oy = (gh_panel - fh) // 2
    bot[oy:oy + fh] = fg
    out[plan["face_h"]:] = bot
    if opts["subs"]:
        y = caption_y(plan, opts["spot"])
        txt = "YOUR SUBTITLES"
        scale = opts["font_size"] / 34
        (tw, th), _ = cv2.getTextSize(txt, cv2.FONT_HERSHEY_DUPLEX, scale, 3)
        org = ((OUT_W - tw) // 2, y + th // 2)
        cv2.putText(out, txt, org, cv2.FONT_HERSHEY_DUPLEX, scale, (0, 0, 0), 14, cv2.LINE_AA)
        cv2.putText(out, txt, org, cv2.FONT_HERSHEY_DUPLEX, scale, (255, 255, 255), 3, cv2.LINE_AA)
    return cv2.cvtColor(out, cv2.COLOR_BGR2RGB)


# ------------------------------------------------------------- subtitles
_model_cache = {}


def load_audio(path, sr=16000):
    """Decode the clip's audio with our own ffmpeg (16 kHz mono float32)."""
    r = subprocess.run([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-i", str(path),
                        "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"], capture_output=True)
    if r.returncode != 0 or not r.stdout:
        return np.zeros(0, np.float32)
    return np.frombuffer(r.stdout, np.int16).astype(np.float32) / 32768.0


def transcribe(path, model_size, language):
    from faster_whisper import WhisperModel
    if model_size not in _model_cache:
        try:
            import ctranslate2
            gpu = ctranslate2.get_cuda_device_count() > 0
        except Exception:
            gpu = False
        try:
            _model_cache[model_size] = WhisperModel(model_size, device="cuda" if gpu else "cpu",
                                                    compute_type="float16" if gpu else "int8")
        except Exception:
            _model_cache[model_size] = WhisperModel(model_size, device="cpu", compute_type="int8")
    model = _model_cache[model_size]
    audio = load_audio(path)
    if audio.size < 1600:
        return []
    segments, _ = model.transcribe(audio, word_timestamps=True, vad_filter=True, beam_size=5,
                                   language=None if language == "Auto" else language)
    words = []
    for seg in segments:
        for w in (seg.words or []):
            t = w.word.strip()
            if t:
                words.append((float(w.start), float(w.end), t))
    return words


def chunk_words(words, per_chunk, max_chars=20):
    chunks, cur = [], []
    for w in words:
        if cur and (len(cur) >= per_chunk
                    or len(" ".join(x[2] for x in cur + [w])) > max_chars
                    or w[0] - cur[-1][1] > 0.6):
            chunks.append(cur); cur = []
        cur.append(w)
        if re.search(r"[.!?]$", w[2]):
            chunks.append(cur); cur = []
    if cur:
        chunks.append(cur)
    return chunks


def ass_time(t):
    t = max(0.0, t)
    h = int(t // 3600); m = int(t % 3600 // 60); s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def clean(t, upper):
    t = t.replace("{", "(").replace("}", ")").replace("\\", "")
    t = re.sub(r"[.,;:]+$", "", t)
    return t.upper() if upper else t


def write_ass(words, path, plan, opts):
    hl = hex_to_ass(opts["color"])
    y = caption_y(plan, opts["spot"])
    fs = int(opts["font_size"])
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {OUT_W}
PlayResY: {OUT_H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{opts['font']},{fs},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,1,0,1,{max(3, fs // 13)},3,5,60,60,0,1
Style: Hook,{opts['font']},{int(fs * 0.8)},&H00000000,&H00000000,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,3,18,0,5,80,80,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    if opts.get("hook"):
        hy = int(plan["face_h"] * 0.16) if plan["face_h"] else int(OUT_H * 0.12)
        hfs = int(fs * 0.85)
        txt = clean(opts["hook"], opts["upper"])
        lines.append(f"Dialogue: 1,{ass_time(0)},{ass_time(float(opts['hook_secs']))},Hook,,0,0,0,,"
                     f"{{\\pos({OUT_W // 2},{hy})\\fad(150,250)}}{txt}")
    chunks = chunk_words(words, int(opts["per_chunk"])) if words else []
    for ci, ch in enumerate(chunks):
        cend = ch[-1][1] + 0.3
        if ci + 1 < len(chunks):
            cend = min(cend, chunks[ci + 1][0][0])
        cend = max(cend, ch[-1][1])
        for wi, w in enumerate(ch):
            s = ch[0][0] if wi == 0 else w[0]
            e = ch[wi + 1][0] if wi + 1 < len(ch) else cend
            if e <= s:
                e = s + 0.05
            txt = []
            for j, o in enumerate(ch):
                t = clean(o[2], opts["upper"])
                txt.append("{\\c%s}%s{\\c&H00FFFFFF&}" % (hl, t) if j == wi else t)
            pop = "{\\fscx115\\fscy115\\t(0,90,\\fscx100\\fscy100)}" if wi == 0 else ""
            lines.append(f"Dialogue: 0,{ass_time(s)},{ass_time(e)},Cap,,0,0,0,,{{\\pos({OUT_W // 2},{y})}}{pop}{' '.join(txt)}")
    Path(path).write_text(head + "\n".join(lines) + "\n", encoding="utf-8")
    return chunks


def write_srt(chunks, path, upper):
    def ts(t):
        ms = int(round(t * 1000))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
    out = []
    for i, ch in enumerate(chunks, 1):
        out.append(f"{i}\n{ts(ch[0][0])} --> {ts(ch[-1][1])}\n{' '.join(clean(w[2], upper) for w in ch)}\n")
    Path(path).write_text("\n".join(out), encoding="utf-8")


# --------------------------------------------------------------- inputs
def download_urls(text, dest):
    urls = [u.strip() for u in (text or "").splitlines() if u.strip()]
    if not urls:
        return []
    import yt_dlp
    files = []
    opts = {"outtmpl": str(Path(dest) / "%(title).70s [%(id)s].%(ext)s"),
            "format": "bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b", "merge_output_format": "mp4",
            "ffmpeg_location": ffmpeg_bin(), "quiet": True, "noprogress": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        for u in urls:
            info = ydl.extract_info(u, download=True)
            entries = info.get("entries") or [info]
            for e in entries:
                if not e:
                    continue
                p = Path(ydl.prepare_filename(e)).with_suffix(".mp4")
                if not p.exists():
                    p = Path(ydl.prepare_filename(e))
                if p.exists():
                    files.append(p)
    return files


def gather_inputs(uploads, folder):
    files = [Path(f if isinstance(f, str) else f.name) for f in (uploads or [])]
    if folder and folder.strip():
        d = Path(folder.strip().strip('"'))
        if not d.is_dir():
            raise RuntimeError(f"Folder not found: {d}")
        files += sorted(p for p in d.rglob("*") if p.suffix.lower() in VIDEO_EXTS)
    return files


def unique_out(stem):
    OUTPUT_DIR.mkdir(exist_ok=True)
    stem = re.sub(r'[<>:"/\\|?*]', "", stem)[:80] or "clip"
    p = OUTPUT_DIR / f"{stem}_vertical.mp4"
    k = 2
    while p.exists():
        p = OUTPUT_DIR / f"{stem}_vertical_{k}.mp4"; k += 1
    return p


# ------------------------------------------------------------ processing
def parse_time(v):
    """'1:23.5', '83', '' -> seconds or None"""
    v = str(v or "").strip()
    if not v:
        return None
    try:
        parts = [float(x) for x in v.split(":")]
    except ValueError:
        raise RuntimeError(f"'{v}' isn't a time. Use minutes:seconds, like 1:05")
    t = 0.0
    for x in parts:
        t = t * 60 + x
    return max(0.0, t)


def make_opts(face_mode, face_zoom, face_pct, game_zoom, game_offset, subs, model_size,
              language, per_chunk, spot, upper, color, font_size, font, hide_cam=True,
              trim_start="", trim_end="", speed="1x", volume=100, hook="", hook_secs=3,
              vivid=False, fades=True):
    return dict(trim_start=parse_time(trim_start), trim_end=parse_time(trim_end),
                speed=float(str(speed).rstrip("x") or 1), volume=volume, hook=(hook or "").strip(),
                hook_secs=hook_secs, vivid=vivid, fades=fades, hide_cam=hide_cam, face_mode=face_mode, face_zoom=face_zoom, face_pct=face_pct, game_zoom=game_zoom,
                game_offset=game_offset, subs=subs, model=model_size, lang=language,
                per_chunk=per_chunk, spot=spot, upper=upper, color=color,
                font_size=font_size, font=font or "Arial Black")


def pre_edit(src, opts, tmp):
    """Trim / speed / volume into a temp file before the vertical layout."""
    a, b, sp, vol = opts["trim_start"], opts["trim_end"], opts["speed"], opts["volume"]
    if a is None and b is None and sp == 1 and vol == 100:
        return src
    W, H, n, fps = probe(src)
    dur = n / fps if fps else None
    if a is not None and b is not None and b <= a:
        raise RuntimeError("Trim end must be after trim start.")
    if dur and a is not None and a >= dur:
        raise RuntimeError(f"Trim start is past the end of the clip (it's {dur:.0f}s long).")
    cmd = [ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error"]
    if a is not None:
        cmd += ["-ss", f"{a:.3f}"]
    if b is not None:
        cmd += ["-to", f"{b:.3f}"]
    cmd += ["-i", str(Path(src).resolve())]
    vf = f"setpts=PTS/{sp}" if sp != 1 else "null"
    af = []
    if sp != 1:
        af.append(f"atempo={sp}")
    if vol != 100:
        af.append(f"volume={vol / 100:.2f}")
    cmd += ["-vf", vf]
    if af:
        cmd += ["-af", ",".join(af)]
    out = Path(tmp) / "edited.mp4"
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-c:a", "aac", "-b:a", "192k", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("Couldn't trim the clip: " + r.stderr.strip()[-300:])
    return out


def process_clip(src, opts, log):
    name = Path(src).stem
    edit_dir = tempfile.mkdtemp()
    try:
        if any([opts["trim_start"] is not None, opts["trim_end"] is not None, opts["speed"] != 1, opts["volume"] != 100]):
            log("  trimming / editing...")
        src = pre_edit(src, opts, edit_dir)
        return _process_clip(src, name, opts, log)
    finally:
        shutil.rmtree(edit_dir, ignore_errors=True)


def _process_clip(src, name, opts, log):
    W, H, _, _ = probe(src)
    face_box = None
    if opts["face_mode"] == "Auto-detect":
        log("  finding your facecam...")
        face_box = detect_face(src)
        log("  face found" if face_box else "  no face found - using full-screen gameplay "
            "(tip: pick your facecam corner in the settings)")
    plan = plan_layout(W, H, opts["face_mode"], face_box, opts)
    out = unique_out(name)
    _, _, n, fps = probe(src)
    plan["dur"] = n / fps if fps else 0

    with tempfile.TemporaryDirectory() as tmp:
        subs_name = None
        words = []
        if opts["subs"]:
            log(f"  writing subtitles (AI model: {opts['model']})...")
            words = transcribe(src, opts["model"], opts["lang"])
            if not words:
                log("  no speech heard - skipping subtitles")
        if words or opts.get("hook"):
            subs_name = "subs.ass"
            chunks = write_ass(words, Path(tmp) / subs_name, plan, opts)
            if chunks:
                write_srt(chunks, out.with_suffix(".srt"), opts["upper"])
        log("  rendering vertical video...")
        cmd = [ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(Path(src).resolve()),
               "-filter_complex", build_filter(plan, subs_name, opts), "-map", "[outv]", "-map", "0:a?",
               *(["-af", f"afade=t=in:d=0.25,afade=t=out:st={max(0, plan['dur'] - 0.4):.2f}:d=0.4"] if opts.get("fades") and plan["dur"] > 1.5 else []),
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out.resolve())]
        r = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip()[-800:])
    return out


def process_url(url, opts):
    """Download one clip link, make the vertical version, delete the download."""
    dl_dir = OUTPUT_DIR / "downloads"
    dl_dir.mkdir(parents=True, exist_ok=True)
    files = download_urls(url, dl_dir)
    if not files:
        raise RuntimeError("couldn't download that clip")
    out = None
    for f in files:
        try:
            out = process_clip(f, opts, lambda m: None)
        finally:
            f.unlink(missing_ok=True)
    return out


def load_settings():
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except Exception:
        return {}


def save_settings(**kw):
    cur = load_settings()
    cur.update(kw)
    try:
        SETTINGS_FILE.write_text(json.dumps(cur, indent=2))
    except Exception:
        pass


def run_all(uploads, folder, urls, *settings):
    opts = make_opts(*settings)
    log_lines = []

    def log(msg):
        log_lines.append(msg)

    done = []
    downloaded = set()
    try:
        files = gather_inputs(uploads, folder)
        dl_dir = OUTPUT_DIR / "downloads"
        dl_dir.mkdir(parents=True, exist_ok=True)
        if urls and urls.strip():
            log("Downloading clips from links...")
            yield "\n".join(log_lines), None, None
            got = download_urls(urls, dl_dir)
            downloaded.update(got)
            files += got
    except Exception as e:
        log(f"Problem: {e}")
        yield "\n".join(log_lines), None, None
        return

    if not files:
        yield "Add some clips first (upload, folder, or a link).", None, None
        return

    for i, f in enumerate(files, 1):
        log(f"[{i}/{len(files)}] {f.name}")
        yield "\n".join(log_lines), (str(done[-1]) if done else None), [str(d) for d in done] or None
        try:
            out = process_clip(f, opts, log)
            done.append(out)
            log(f"  done -> {out.name}")
            if f in downloaded:
                f.unlink(missing_ok=True)
        except Exception as e:
            log(f"  failed: {e}")
        yield "\n".join(log_lines), (str(done[-1]) if done else None), [str(d) for d in done] or None

    log(f"\nFinished {len(done)}/{len(files)} clips. Saved in: {OUTPUT_DIR}")
    yield "\n".join(log_lines), (str(done[-1]) if done else None), [str(d) for d in done] or None


def run_preview(uploads, folder, urls, *settings):
    opts = make_opts(*settings)
    try:
        files = gather_inputs(uploads, folder)
    except Exception as e:
        return None, str(e)
    if not files:
        return None, "Upload a clip or enter a folder to preview (links are previewed after download)."
    src = files[0]
    W, H, _, _ = probe(src)
    face_box = detect_face(src) if opts["face_mode"] == "Auto-detect" else None
    plan = plan_layout(W, H, opts["face_mode"], face_box, opts)
    frame = grab_frame(src, 0.4)
    note = f"Preview of {src.name}. "
    if opts["face_mode"] == "Auto-detect":
        note += "Face found." if face_box else "No face found - try picking your facecam corner."
    return preview_image(frame, plan, opts), note


# ------------------------------------------------------------------- UI
def build_ui():
    from twitch_auto import Watcher
    watcher = Watcher(APP_DIR, lambda url: process_url(url, watcher.opts))
    watcher.opts = None
    saved = load_settings()

    with gr.Blocks(title="ClipMaker", theme=gr.themes.Soft(primary_hue="violet")) as ui:
        gr.Markdown("# 🎬 ClipMaker\nStream clips → vertical TikTok / Reels / Shorts with facecam on top and animated subtitles.")
        with gr.Row():
            with gr.Column(scale=3):
                with gr.Tab("Upload clips"):
                    uploads = gr.File(file_count="multiple", file_types=["video"], label="Drop clips here")
                with gr.Tab("Folder"):
                    folder = gr.Textbox(label="Folder path", placeholder=r"C:\Users\you\Videos\Clips")
                with gr.Tab("Links"):
                    urls = gr.Textbox(lines=4, label="Clip links (one per line)",
                                      placeholder="https://www.twitch.tv/yourname/clip/...\nhttps://youtube.com/shorts/...")
                    gr.Markdown("Downloaded originals are deleted after the vertical version is made.")
                with gr.Tab("Auto from Twitch"):
                    gr.Markdown("Clip with your Stream Deck like normal. When you end stream, every clip from "
                                "that stream is turned vertical automatically and saved in the **output** folder. "
                                "Uses the facecam, gameplay and subtitle settings below. See README for the 2-minute Twitch setup.")
                    tw_login = gr.Textbox(label="Your Twitch username", value=saved.get("twitch_login", ""))
                    with gr.Row():
                        tw_id = gr.Textbox(label="Client ID", value=saved.get("twitch_client_id", ""))
                        tw_secret = gr.Textbox(label="Client Secret", type="password", value=saved.get("twitch_secret", ""))
                    tw_mode = gr.Radio(["When I end stream (recommended)", "While I stream"],
                                       value=saved.get("twitch_mode", "When I end stream (recommended)"),
                                       label="When should clips be made?")
                    with gr.Row():
                        tw_start = gr.Button("▶ Start watching", variant="primary")
                        tw_stop = gr.Button("■ Stop")
                    with gr.Row():
                        tw_hours = gr.Number(value=12, label="Hours back", precision=0, minimum=1, maximum=168)
                        tw_grab = gr.Button("Make clips from the last few hours now")
                    tw_log = gr.Textbox(label="Twitch auto-mode", lines=8, value="Status: Not watching")

                src_view = gr.Video(label="Your clip (play it to find the times you want)", height=300, interactive=False)
                with gr.Accordion("✂️ Edit: trim, speed, text", open=True):
                    with gr.Row():
                        trim_start = gr.Textbox(label="Start at", placeholder="0:00", value="")
                        trim_end = gr.Textbox(label="End at", placeholder="end of clip", value="")
                    gr.Markdown("Use minutes:seconds, like **0:42** to **1:15**. Leave empty to keep the whole clip.")
                    with gr.Row():
                        speed = gr.Radio(["1x", "1.1x", "1.25x", "1.5x"], value="1x", label="Speed")
                        volume = gr.Slider(50, 200, value=100, step=10, label="Volume %")
                    with gr.Row():
                        hook = gr.Textbox(label="Hook text at the top (optional)", placeholder="He did NOT see that coming 💀")
                        hook_secs = gr.Slider(1, 10, value=3, step=0.5, label="Show hook for (seconds)")
                    with gr.Row():
                        vivid = gr.Checkbox(value=False, label="Vivid colors (pop the game more)")
                        fades = gr.Checkbox(value=True, label="Smooth fade in / out")
                with gr.Accordion("Facecam & gameplay", open=True):
                    face_mode = gr.Dropdown(FACE_MODES, value="Auto-detect", label="Where's your facecam?")
                    face_zoom = gr.Slider(1.6, 5.0, value=2.6, step=0.1, label="Facecam zoom (higher = closer on your face)")
                    face_pct = gr.Slider(25, 50, value=36, step=1, label="Facecam height (% of screen)")
                    game_zoom = gr.Slider(0, 100, value=45, step=5,
                                          label="Gameplay zoom (0 = see the whole screen, 100 = fill the space)")
                    game_offset = gr.Slider(-1, 1, value=0, step=0.05, label="Gameplay left/right (when zoomed in)")
                    hide_cam = gr.Checkbox(value=True, label="Cut the facecam out of the gameplay (so it only shows once, on top)")
                with gr.Accordion("Subtitles", open=True):
                    subs = gr.Checkbox(value=True, label="Add subtitles")
                    with gr.Row():
                        model_size = gr.Dropdown(["tiny", "base", "small", "medium", "large-v3"], value="small",
                                                 label="Accuracy (bigger = better but slower)")
                        language = gr.Dropdown(["Auto", "en", "es", "fr", "pt", "de", "it", "ht", "ja", "ko"],
                                               value="Auto", label="Language")
                    with gr.Row():
                        per_chunk = gr.Slider(1, 5, value=3, step=1, label="Words on screen at once")
                        font_size = gr.Slider(50, 130, value=88, step=2, label="Text size")
                    with gr.Row():
                        spot = gr.Dropdown(CAPTION_SPOTS, value=CAPTION_SPOTS[0], label="Subtitle position")
                        color = gr.ColorPicker(value="#FFD400", label="Highlight color")
                    with gr.Row():
                        upper = gr.Checkbox(value=True, label="ALL CAPS")
                        font = gr.Textbox(value="Arial Black", label="Font")

                with gr.Row():
                    prev_btn = gr.Button("👀 Preview layout")
                    go_btn = gr.Button("🚀 Make my clips", variant="primary")

            with gr.Column(scale=2):
                preview = gr.Image(label="Layout preview", height=520)
                note = gr.Markdown()
                video = gr.Video(label="Latest finished clip", height=520)
                files_out = gr.File(label="Download finished clips", file_count="multiple")
                log = gr.Textbox(label="Progress", lines=10)

        settings = [face_mode, face_zoom, face_pct, game_zoom, game_offset, subs, model_size,
                    language, per_chunk, spot, upper, color, font_size, font, hide_cam,
                    trim_start, trim_end, speed, volume, hook, hook_secs, vivid, fades]

        def show_src(up):
            if not up:
                return None
            f = up[0]
            return f if isinstance(f, str) else f.name
        uploads.change(show_src, uploads, src_view)
        def tw_start_fn(login, cid, secret, mode, *st):
            save_settings(twitch_login=login, twitch_client_id=cid, twitch_secret=secret, twitch_mode=mode)
            watcher.opts = make_opts(*st)
            watcher.opts.update(trim_start=None, trim_end=None, hook="")
            watcher.start(cid, secret, login, "during" if mode.startswith("While") else "after")
            return watcher.text()

        def tw_grab_fn(login, cid, secret, hours, *st):
            save_settings(twitch_login=login, twitch_client_id=cid, twitch_secret=secret)
            watcher.opts = make_opts(*st)
            watcher.opts.update(trim_start=None, trim_end=None, hook="")
            watcher.grab_recent(cid, secret, login, hours or 12)
            return watcher.text()

        def tw_stop_fn():
            watcher.stop()
            return watcher.text()

        tw_inputs = [tw_login, tw_id, tw_secret]
        # remember Twitch details as soon as they're typed or pasted
        tw_login.change(lambda v: save_settings(twitch_login=v), tw_login, None)
        tw_id.change(lambda v: save_settings(twitch_client_id=v), tw_id, None)
        tw_secret.change(lambda v: save_settings(twitch_secret=v), tw_secret, None)
        tw_mode.change(lambda v: save_settings(twitch_mode=v), tw_mode, None)
        tw_start.click(tw_start_fn, tw_inputs + [tw_mode] + settings, tw_log)
        tw_grab.click(tw_grab_fn, tw_inputs + [tw_hours] + settings, tw_log)
        tw_stop.click(tw_stop_fn, None, tw_log)
        gr.Timer(3).tick(lambda: watcher.text(), None, tw_log)

        prev_btn.click(run_preview, [uploads, folder, urls] + settings, [preview, note])
        go_btn.click(run_all, [uploads, folder, urls] + settings, [log, video, files_out])
    return ui


def already_running(port=7860):
    import socket
    with socket.socket() as sk:
        sk.settimeout(0.5)
        return sk.connect_ex(("127.0.0.1", port)) == 0


if __name__ == "__main__":
    if already_running():
        import webbrowser
        print("ClipMaker is already running - opening it in your browser.")
        webbrowser.open("http://127.0.0.1:7860")
        raise SystemExit(0)
    OUTPUT_DIR.mkdir(exist_ok=True)
    build_ui().queue().launch(inbrowser=True, allowed_paths=[str(OUTPUT_DIR)], server_port=7860)
