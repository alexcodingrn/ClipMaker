"""
ClipMaker engine: webcam detection, layout and rendering.
The browser page (ui.html) previews with the same layout math as plan() below.
"""
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np

from app import (OUT_W, OUT_H, OUTPUT_DIR, ffmpeg_bin, even, probe, get_cascades,
                 build_filter, write_ass, write_srt, transcribe, unique_out)

SPOTS = {"seam": "Middle", "low": "Lower", "cam": "On facecam"}

DEFAULTS = {
    "cam_on": True, "cam": None, "face": None, "cam_pct": 36, "cam_zoom": 1.0,
    "game_zoom": 45, "game_off": 0, "game_off_y": 0, "hide_cam": True,
    "subs": True, "model": "small", "lang": "Auto", "per_chunk": 3, "spot": "seam",
    "upper": True, "color": "#FFD400", "font_size": 88, "font": "Arial Black",
    "trim_start": None, "trim_end": None, "speed": 1.0, "volume": 100,
    "hook": "", "hook_secs": 3, "vivid": False, "fades": True, "words": None, "cam_src": None, "cam_sure": False, "overlays": None,
}


def settings_with_defaults(s):
    out = dict(DEFAULTS)
    out.update({k: v for k, v in (s or {}).items() if k in DEFAULTS})
    return out


# ------------------------------------------------------------ webcam detection
def _frames(path, n=24):
    cap = cv2.VideoCapture(str(path))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    out = []
    for i in range(n):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int((i + 0.5) * total / n))
        ok, fr = cap.read()
        if ok:
            out.append(fr)
    cap.release()
    return out


def _faces(frames, W):
    cas = get_cascades()
    if not cas:
        return []
    front, profile = cas
    s = 960.0 / W if W > 960 else 1.0
    boxes = []
    for fr in frames:
        g = cv2.equalizeHist(cv2.cvtColor(cv2.resize(fr, None, fx=s, fy=s), cv2.COLOR_BGR2GRAY))
        found = list(front.detectMultiScale(g, 1.1, 6, minSize=(22, 22)))
        if not found:
            found = list(profile.detectMultiScale(g, 1.1, 6, minSize=(22, 22)))
            gw = g.shape[1]
            for (x, y, w, h) in profile.detectMultiScale(cv2.flip(g, 1), 1.1, 6, minSize=(22, 22)):
                found.append((gw - x - w, y, w, h))
        boxes += [(x / s, y / s, w / s, h / s) for (x, y, w, h) in found]
    return boxes


def _cluster(boxes, need):
    best = []
    for b in boxes:
        cx, cy = b[0] + b[2] / 2, b[1] + b[3] / 2
        m = [o for o in boxes if abs(o[0] + o[2] / 2 - cx) < 0.8 * b[2] and abs(o[1] + o[3] / 2 - cy) < 0.8 * b[3]]
        if len(m) > len(best):
            best = m
    if len(best) < need:
        return None, []
    return tuple(float(v) for v in np.median(np.array(best), axis=0)), best


def detect_cam(path):
    """Find the webcam overlay: returns ({x,y,w,h}, {x,y}) normalized, or (None, None)."""
    W, H, _, _ = probe(path)
    frames = _frames(path)
    if not frames:
        return None, None
    face, members = _cluster(_faces(frames, W), max(3, len(frames) // 6))
    if not face:
        return None, None
    fx, fy, fw, fh = face
    # how far the face wanders (leaning), so the box always fits it
    arr = np.array(members)
    mx0, my0 = arr[:, 0].min(), arr[:, 1].min()
    mx1, my1 = (arr[:, 0] + arr[:, 2]).max(), (arr[:, 1] + arr[:, 3]).max()

    # Average all frames: moving gameplay blurs out, the webcam's border stays sharp.
    mean = np.mean([cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32) for f in frames], axis=0)
    gx = np.abs(np.diff(mean, axis=1))   # vertical borders
    gy = np.abs(np.diff(mean, axis=0))   # horizontal borders
    cx, cy = fx + fw / 2, fy + fh / 2
    ry0, ry1 = int(max(0, my0)), int(min(H - 1, my1 + fh))      # rows the face covers
    rx0, rx1 = int(max(0, mx0 - fw)), int(min(W - 1, mx1 + fw))  # columns around the face

    def best(profile, lo, hi, toward_face_first):
        lo, hi = int(max(0, lo)), int(min(len(profile), hi))
        if hi - lo < 3:
            return None
        seg = profile[lo:hi]
        i = int(np.argmax(seg))
        base = float(np.median(profile)) + 1e-3
        return lo + i if seg[i] > max(12.0, base * 4) else None

    colp = gx[ry0:ry1 + 1].mean(axis=0) if ry1 > ry0 else np.zeros(W - 1)
    rowp = gy[:, rx0:rx1 + 1].mean(axis=1) if rx1 > rx0 else np.zeros(H - 1)
    L = best(colp, cx - fw * 5, cx - fw * 0.7, True)
    R = best(colp, cx + fw * 0.7, cx + fw * 5, True)
    T = best(rowp, cy - fh * 4, cy - fh * 0.7, True)
    B = best(rowp, cy + fh * 1.2, cy + fh * 5, True)
    sc = 1.0
    if L is not None: L += 1
    if T is not None: T += 1

    # fallbacks: a webcam-shaped box around the face
    x0 = L / sc if L is not None else min(mx0 - fw * 1.6, cx - fw * 2.4)
    x1 = R / sc if R is not None else max(mx1 + fw * 1.6, cx + fw * 2.4)
    y0 = T / sc if T is not None else min(my0 - fh * 1.0, cy - fh * 1.8)
    y1 = B / sc if B is not None else max(my1 + fh * 1.8, cy + fh * 2.6)
    # snap to frame edges when close
    if x0 < W * 0.04: x0 = 0
    if y0 < H * 0.04: y0 = 0
    if x1 > W * 0.96: x1 = W
    if y1 > H * 0.96: y1 = H
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
    # a face cam taking the whole frame (just-chatting) - use it all
    cam = {"x": x0 / W, "y": y0 / H, "w": (x1 - x0) / W, "h": (y1 - y0) / H}
    return cam, {"x": cx / W, "y": (cy + fh * 0.15) / H}


# ------------------------------------------------------------------ layout
def plan(W, H, s):
    """Same math as plan() in ui.html."""
    cam = s.get("cam")
    cam_on = bool(s.get("cam_on")) and bool(cam)
    face_h = even(OUT_H * float(s["cam_pct"]) / 100) if cam_on else 0
    game_h = OUT_H - face_h
    p = {"face": None, "face_h": face_h, "game_h": game_h, "src": (W, H), "hide": None}
    if cam_on:
        rx, ry, rw, rh = cam["x"] * W, cam["y"] * H, cam["w"] * W, cam["h"] * H
        A = OUT_W / face_h
        if rw / rh > A:
            ch = rh; cw = ch * A
        else:
            cw = rw; ch = cw / A
        z = max(1.0, float(s.get("cam_zoom") or 1))
        cw /= z; ch /= z
        f = s.get("face")
        fx = f["x"] * W if f else rx + rw / 2
        fy = f["y"] * H if f else ry + rh / 2
        x = min(max(fx - cw / 2, rx), rx + rw - cw)
        y = min(max(fy - ch / 2, ry), ry + rh - ch)
        p["face"] = (even(x) if x >= 2 else 0, even(y) if y >= 2 else 0, even(cw), even(ch))
        fx_, fy_, fw_, fh_ = p["face"]
        p["face"] = (min(fx_, W - fw_), min(fy_, H - fh_), fw_, fh_)
        if s.get("hide_cam", True):
            pad = min(W, H) * 0.008
            x0, y0 = max(0, rx - pad), max(0, ry - pad)
            x1, y1 = min(W, rx + rw + pad), min(H, ry + rh + pad)
            hw, hh = even(x1 - x0), even(y1 - y0)
            p["hide"] = (int(x0), int(y0), min(hw, W - int(x0)), min(hh, H - int(y0)))
    GA = OUT_W / game_h
    fill_w = min(W, H * GA)
    z = float(s["game_zoom"])
    t = max(0.0, min(1.0, z / 100))
    gw = W - t * (W - fill_w)
    if z > 100:   # past "fill": zoom in further, up to 2.5x closer at 200
        gw = fill_w / (1 + (min(z, 200) - 100) / 100 * 1.5)
    gh = min(H, gw / GA)
    gcx = W / 2 + (float(s["game_off"]) / 100) * (W - gw) / 2
    gx = min(max(0, gcx - gw / 2), W - gw)
    gcy = H / 2 + (float(s.get("game_off_y") or 0) / 100) * (H - gh) / 2
    gy = min(max(0, gcy - gh / 2), H - gh)
    p["game"] = (int(gx), int(gy), even(gw), even(gh))
    _, _, ggw, ggh = p["game"]
    p["fit"] = (OUT_W, min(game_h, even(OUT_W * ggh / ggw)))
    return p


# ------------------------------------------------------------------ combine clips
def has_audio(path):
    r = subprocess.run([ffmpeg_bin(), "-hide_banner", "-i", str(path)], capture_output=True, text=True)
    return "Audio:" in r.stderr


def combine(pieces, out):
    """pieces: [(path, start, end)]. Joins them into one horizontal clip the size of the first piece."""
    if not pieces:
        raise RuntimeError("Add at least one piece to combine.")
    W, H, n, fps = probe(pieces[0][0])
    fps = 60 if fps > 45 else 30
    cmd = [ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error"]
    parts, labels = [], ""
    for i, (path, a, b) in enumerate(pieces):
        a = max(0.0, float(a or 0))
        if b is not None and float(b) <= a:
            raise RuntimeError(f"Piece {i + 1}: the end has to be after the start.")
        cmd += ["-ss", f"{a:.3f}"] + (["-to", f"{float(b):.3f}"] if b is not None else []) + ["-i", str(Path(path).resolve())]
        parts.append(f"[{i}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,"
                     f"setsar=1,fps={fps},format=yuv420p,setpts=PTS-STARTPTS[v{i}]")
        if has_audio(path):
            parts.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo,asetpts=PTS-STARTPTS[a{i}]")
        else:
            seg = (float(b) - a) if b is not None else 1.0
            parts.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{seg:.3f}[a{i}]")
        labels += f"[v{i}][a{i}]"
    parts.append(f"{labels}concat=n={len(pieces)}:v=1:a=1[v][a]")
    cmd += ["-filter_complex", ";".join(parts), "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-c:a", "aac", "-b:a", "192k", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("Couldn't combine the clips: " + r.stderr.strip()[-400:])
    return Path(out)


# ------------------------------------------------------------------ sounds / memes / pictures
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
SOUND_EXT = {".mp3", ".wav", ".m4a", ".ogg", ".aac", ".flac"}
GIF_EXT = {".gif"}


def asset_kind(path):
    e = Path(path).suffix.lower()
    return "image" if e in IMAGE_EXT else "sound" if e in SOUND_EXT else "gif" if e in GIF_EXT else "video"


def overlay_times(ov, s, total):
    """Overlay time is picked on the original clip; move it onto the trimmed/sped-up video."""
    a = s.get("trim_start") or 0.0
    sp = float(s.get("speed") or 1)
    t = (float(ov.get("t", 0)) - a) / sp
    d = float(ov.get("dur") or 2)
    if t < -0.05 or t >= total:
        return None
    return max(0.0, t), min(d, total - max(0.0, t))


def overlay_graph(p, s, overlays, first_input, base_audio):
    """Returns (extra input args, video filter steps applied to [stk], audio filter producing [aout])."""
    args, vsteps, asteps, amix = [], [], [], []
    cur = "[stk]"
    idx = first_input
    total = p["dur"]
    for k, ov in enumerate(overlays or []):
        path = Path(ov["path"])
        if not path.exists():
            continue
        tt = overlay_times(ov, s, total)
        if not tt:
            continue
        t, d = tt
        kind = asset_kind(path)
        vol = max(0.0, float(ov.get("vol", 100)) / 100)
        if kind == "image":
            args += ["-loop", "1", "-t", f"{d:.3f}", "-i", str(path)]
        elif kind == "gif":
            args += ["-ignore_loop", "0", "-t", f"{d:.3f}", "-i", str(path)]
        else:
            args += ["-t", f"{d:.3f}", "-i", str(path)]
        if kind in ("image", "gif", "video"):
            pos = ov.get("pos", "center")
            if pos == "full":
                bw, bh, yc = OUT_W, OUT_H, OUT_H / 2
            elif pos == "game":
                bw, bh, yc = OUT_W, p["game_h"], p["face_h"] + p["game_h"] / 2
            else:
                bw, bh, yc = int(OUT_W * 0.8), int(OUT_H * 0.45), OUT_H / 2
            vsteps.append(f"[{idx}:v]scale=w={bw}:h={bh}:force_original_aspect_ratio=decrease,format=rgba,"
                          f"setpts=PTS-STARTPTS+{t:.3f}/TB[ov{k}]")
            vsteps.append(f"{cur}[ov{k}]overlay=x=(main_w-overlay_w)/2:y={yc:.0f}-overlay_h/2:eof_action=pass[st{k}]")
            cur = f"[st{k}]"
        if kind == "sound" or (kind == "video" and has_audio(path)):
            ms = int(t * 1000)
            asteps.append(f"[{idx}:a]aresample=48000,aformat=channel_layouts=stereo,volume={vol:.2f},adelay={ms}|{ms}[oa{k}]")
            amix.append(f"[oa{k}]")
        idx += 1
    if amix:
        asteps.append(f"{base_audio}{''.join(amix)}amix=inputs={len(amix) + 1}:normalize=0:duration=first[aout]")
    return args, vsteps, cur, asteps, bool(amix)


# ------------------------------------------------------------------ render
def pre_edit(src, s, tmp):
    a, b = s.get("trim_start"), s.get("trim_end")
    sp, vol = float(s.get("speed") or 1), int(s.get("volume") or 100)
    if a is None and b is None and sp == 1 and vol == 100:
        return Path(src)
    if a is not None and b is not None and b <= a:
        raise RuntimeError("The end point has to be after the start point.")
    cmd = [ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error"]
    if a is not None:
        cmd += ["-ss", f"{a:.3f}"]
    if b is not None:
        cmd += ["-to", f"{b:.3f}"]
    cmd += ["-i", str(Path(src).resolve()), "-vf", f"setpts=PTS/{sp}" if sp != 1 else "null"]
    af = ([f"atempo={sp}"] if sp != 1 else []) + ([f"volume={vol / 100:.2f}"] if vol != 100 else [])
    if af:
        cmd += ["-af", ",".join(af)]
    out = Path(tmp) / "edited.mp4"
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-c:a", "aac", "-b:a", "192k", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("Couldn't trim the clip: " + r.stderr.strip()[-300:])
    return out


def shift_words(words, s):
    """Words were timed on the original clip; move them onto the trimmed/sped-up clip."""
    a = s.get("trim_start") or 0.0
    b = s.get("trim_end")
    sp = float(s.get("speed") or 1)
    out = []
    for w0, w1, t in words:
        if w1 <= a or (b is not None and w0 >= b):
            continue
        w0 = max(w0, a)
        w1 = min(w1, b) if b is not None else w1
        out.append(((w0 - a) / sp, (w1 - a) / sp, t))
    return out


def render(src, name, s, log=print, progress=lambda f: None):
    s = settings_with_defaults(s)
    work = Path(tempfile.mkdtemp())
    try:
        if s["trim_start"] is not None or s["trim_end"] is not None or float(s["speed"]) != 1 or int(s["volume"]) != 100:
            log("Trimming and editing...")
        clip = pre_edit(src, s, work)
        W, H, n, fps = probe(clip)
        if s["cam_on"] and not s["cam"]:
            log("Finding your webcam...")
            s["cam"], s["face"] = detect_cam(clip)
            if not s["cam"]:
                log("Couldn't find a webcam - making it full-screen gameplay.")
        p = plan(W, H, s)
        p["dur"] = n / fps if fps else 0
        out = unique_out(name)
        words = []
        if s["words"] is not None:
            words = shift_words([tuple(w) for w in s["words"]], s)
        elif s["subs"]:
            log("Writing subtitles...")
            words = transcribe(clip, s["model"], s["lang"])
        opts = dict(s, spot=SPOTS.get(s["spot"], "Middle"))
        subs_name = None
        if words or s["hook"]:
            subs_name = "subs.ass"
            chunks = write_ass(words, work / subs_name, p, opts)
            if chunks:
                write_srt(chunks, out.with_suffix(".srt"), s["upper"])
        log("Making the vertical video...")
        fc = build_filter(p, subs_name, opts)
        fade_a = f"afade=t=in:d=0.25,afade=t=out:st={max(0, p['dur'] - 0.4):.2f}:d=0.4" if s["fades"] and p["dur"] > 1.5 else None
        extra, vsteps, last, asteps, mixed = overlay_graph(p, s, s.get("overlays"), 1, "[base]")
        if vsteps or mixed:
            log("Adding your sounds and memes...")
            # put overlays between the stacked picture and the subtitles/fades
            if vsteps:
                steps = fc.split(";")
                post = steps.pop()                      # "[stk]...subtitles/fades...[outv]"
                fc = ";".join(steps).replace("[stk]", "[stk0]")
                ov = ";".join(x.replace("[stk]", "[stk0]") for x in vsteps)
                last0 = "[stk0]" if last == "[stk]" else last
                fc = fc + ";" + ov + ";" + last0 + post[len("[stk]"):]
            parts = [fc]
            if mixed:
                base = "[0:a]aresample=48000,aformat=channel_layouts=stereo[base]" if has_audio(clip) else \
                    f"anullsrc=r=48000:cl=stereo,atrim=0:{p['dur']:.3f}[base]"
                parts += [base] + asteps
                if fade_a:
                    parts[-1] = parts[-1].replace("[aout]", "[amx]")
                    parts.append(f"[amx]{fade_a}[aout]")
            fc = ";".join(parts)
        cmd = [ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error", "-progress", "pipe:1", "-nostats",
               "-i", str(Path(clip).resolve())] + extra + ["-filter_complex", fc, "-map", "[outv]"]
        if mixed:
            cmd += ["-map", "[aout]"]
        else:
            cmd += ["-map", "0:a?"]
            if fade_a:
                cmd += ["-af", fade_a]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out.resolve())]
        proc = subprocess.Popen(cmd, cwd=work, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for line in proc.stdout:
            if line.startswith("out_time_us=") and p["dur"]:
                try:
                    progress(min(1.0, int(line.split("=")[1]) / 1e6 / p["dur"]))
                except ValueError:
                    pass
        err = proc.stderr.read()
        if proc.wait() != 0:
            raise RuntimeError(err.strip()[-600:])
        progress(1.0)
        return out
    finally:
        shutil.rmtree(work, ignore_errors=True)


def words_for(src, model, lang):
    return [list(w) for w in transcribe(src, model, lang)]
