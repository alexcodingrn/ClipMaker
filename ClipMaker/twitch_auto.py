"""
Twitch auto-mode for ClipMaker.

Watches your channel. When your stream goes offline (or right away, if you
choose "while I stream"), it finds the clips made during that stream,
downloads each one, makes the vertical version, and deletes the download.

Uses either "Log in with Twitch" (easy) or your own developer app (Client ID + Secret).
"""
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

API = "https://api.twitch.tv/helix"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"


class TwitchError(Exception):
    pass


# The public "ClipMaker Easy Login" app: lets anyone log in with Twitch without making developer keys.
PUBLIC_CLIENT_ID = "yqtsn44202u9clf49rmau2ucigtep7"
LOGIN_SCOPES = "clips:edit"
TOKEN_SAVER = None   # set by the server: called with new tokens when they're refreshed


def start_login():
    """Step 1 of 'Log in with Twitch': returns the code the user approves on twitch.tv/activate."""
    r = requests.post("https://id.twitch.tv/oauth2/device", timeout=20,
                      data={"client_id": PUBLIC_CLIENT_ID, "scopes": LOGIN_SCOPES})
    if r.status_code != 200:
        raise TwitchError(f"Twitch login isn't available right now ({r.status_code}).")
    return r.json()


def finish_login(device_code):
    """Step 2: returns tokens once the user approved, None while still waiting."""
    r = requests.post(TOKEN_URL, timeout=20, data={
        "client_id": PUBLIC_CLIENT_ID, "scopes": LOGIN_SCOPES, "device_code": device_code,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
    if r.status_code == 200:
        j = r.json()
        return {"access": j["access_token"], "refresh": j.get("refresh_token"),
                "until": time.time() + int(j.get("expires_in", 3600))}
    msg = r.text.lower()
    if "pending" in msg:
        return None
    if "expired" in msg or "invalid device code" in msg:
        raise TwitchError("The login code expired. Press Log in with Twitch again.")
    raise TwitchError("Twitch login didn't go through. Try again.")


def whoami(access):
    r = requests.get("https://id.twitch.tv/oauth2/validate", timeout=20, headers={"Authorization": f"OAuth {access}"})
    if r.status_code != 200:
        return None
    j = r.json()
    return {"login": j.get("login"), "user_id": j.get("user_id")}


class Twitch:
    def __init__(self, client_id, client_secret):
        # client_secret can be a developer secret (string) or login tokens from "Log in with Twitch" (dict)
        self.tokens = client_secret if isinstance(client_secret, dict) else None
        self.cid = PUBLIC_CLIENT_ID if self.tokens else (client_id or "").strip()
        self.secret = "" if self.tokens else (client_secret or "").strip()
        self.token = self.tokens.get("access") if self.tokens else None
        self.token_until = float(self.tokens.get("until", 0)) if self.tokens else 0

    def _refresh_login(self):
        r = requests.post(TOKEN_URL, timeout=20, data={"client_id": PUBLIC_CLIENT_ID, "grant_type": "refresh_token",
                                                       "refresh_token": self.tokens.get("refresh", "")})
        if r.status_code != 200:
            raise TwitchError("Your Twitch login ran out. Open Setup and press Log in with Twitch again.")
        j = r.json()
        self.tokens = {"access": j["access_token"], "refresh": j.get("refresh_token", self.tokens.get("refresh")),
                       "until": time.time() + int(j.get("expires_in", 3600))}
        self.token, self.token_until = self.tokens["access"], self.tokens["until"]
        if TOKEN_SAVER:
            try:
                TOKEN_SAVER(self.tokens)
            except Exception:
                pass

    def _auth(self):
        if self.token and time.time() < self.token_until - 60:
            return
        if self.tokens:
            self._refresh_login()
            return
        if not self.cid or not self.secret:
            raise TwitchError("Log in with Twitch first (Setup button at the top).")
        r = requests.post(TOKEN_URL, data={"client_id": self.cid, "client_secret": self.secret,
                                           "grant_type": "client_credentials"}, timeout=20)
        if r.status_code != 200:
            raise TwitchError("Twitch didn't accept the Client ID / Secret. Copy them again from dev.twitch.tv.")
        j = r.json()
        self.token = j["access_token"]
        self.token_until = time.time() + int(j.get("expires_in", 3600))

    def get(self, path, **params):
        self._auth()
        r = requests.get(f"{API}/{path}", params=params, timeout=20,
                         headers={"Client-Id": self.cid, "Authorization": f"Bearer {self.token}"})
        if r.status_code == 401:
            self.token = None
            self._auth()
            r = requests.get(f"{API}/{path}", params=params, timeout=20,
                             headers={"Client-Id": self.cid, "Authorization": f"Bearer {self.token}"})
        if r.status_code != 200:
            raise TwitchError(f"Twitch said {r.status_code}: {r.text[:200]}")
        return r.json()

    def user_id(self, login):
        login = (login or "").strip().lstrip("@").split("/")[-1].lower()
        if not login:
            raise TwitchError("Type your Twitch username.")
        data = self.get("users", login=login).get("data", [])
        if not data:
            raise TwitchError(f"No Twitch channel called '{login}'.")
        return data[0]["id"], data[0]["login"]

    def live_stream(self, uid):
        data = self.get("streams", user_id=uid).get("data", [])
        return data[0] if data else None

    def _page(self, **params):
        out, cursor = [], None
        for _ in range(10):
            if cursor:
                params["after"] = cursor
            j = self.get("clips", **params)
            out += j.get("data", [])
            cursor = j.get("pagination", {}).get("cursor")
            if not cursor or not j.get("data"):
                break
        return out

    def clips(self, uid, since, until=None):
        """Clips made on this channel since `since`. Twitch's date search can be slow to list
        brand-new clips, so also look through the channel's clip list and filter it ourselves."""
        params = {"broadcaster_id": uid, "started_at": iso(since), "first": 100}
        if until:
            params["ended_at"] = iso(until)
        found = {c["id"]: c for c in self._page(**params)}
        try:
            for c in self._page(broadcaster_id=uid, first=100):
                t = parse_iso(c["created_at"])
                if t >= since and (until is None or t <= until):
                    found.setdefault(c["id"], c)
        except Exception:
            pass
        return sorted(found.values(), key=lambda c: c["created_at"])

    def debug(self, uid, hours=24):
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        by_date = self._page(broadcaster_id=uid, started_at=iso(since), first=100)
        latest = sorted(self._page(broadcaster_id=uid, first=100), key=lambda c: c["created_at"], reverse=True)[:5]
        return {"by_date": len(by_date),
                "newest_on_channel": [{"created_at": c["created_at"], "title": c["title"], "creator": c.get("creator_name")} for c in latest]}


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


class Watcher:
    """Background loop. process_fn(url) -> output path (raises on failure)."""

    def __init__(self, app_dir, process_fn):
        self.app_dir = Path(app_dir)
        self.done_file = self.app_dir / "processed_clips.json"
        self.process_fn = process_fn
        self.lines = []
        self.thread = None
        self.stop_flag = threading.Event()
        self.busy = threading.Lock()
        self.status = "Not watching"

    # ---- bookkeeping
    def log(self, msg):
        stamp = datetime.now().strftime("%H:%M")
        self.lines.append(f"[{stamp}] {msg}")
        self.lines = self.lines[-300:]

    def text(self):
        return f"Status: {self.status}\n\n" + "\n".join(reversed(self.lines))

    def done_ids(self):
        try:
            return set(json.loads(self.done_file.read_text()))
        except Exception:
            return set()

    def mark_done(self, cid):
        ids = self.done_ids()
        ids.add(cid)
        self.done_file.write_text(json.dumps(sorted(ids)))

    # ---- work
    def grab(self, tw, uid, since, until=None, delay_new=0):
        """Process every clip in [since, until] that isn't done yet."""
        with self.busy:
            clips = tw.clips(uid, since, until)
            done = self.done_ids()
            todo = [c for c in clips if c["id"] not in done]
            if delay_new:  # Twitch needs a moment after a clip is made before it can be downloaded
                cutoff = datetime.now(timezone.utc) - timedelta(seconds=delay_new)
                todo = [c for c in todo if parse_iso(c["created_at"]) <= cutoff]
            todo.sort(key=lambda c: c["created_at"])
            if not todo:
                return 0
            self.log(f"Found {len(todo)} new clip(s).")
            made = 0
            for i, c in enumerate(todo, 1):
                if self.stop_flag.is_set():
                    break
                self.log(f"({i}/{len(todo)}) {c['title']}")
                try:
                    out = self.process_fn(c["url"])
                    self.mark_done(c["id"])
                    made += 1
                    self.log(f"   done -> {Path(out).name}")
                except Exception as e:
                    self.log(f"   failed: {e}")
            return made

    def run(self, cid, secret, login, mode, poll_secs=60):
        tw = Twitch(cid, secret)
        try:
            uid, name = tw.user_id(login)
        except Exception as e:
            self.log(f"Problem: {e}")
            self.status = "Stopped (fix the problem above, then press Start again)"
            return
        self.log(f"Watching twitch.tv/{name}. Leave this window open while you stream.")
        live_since = None
        while not self.stop_flag.is_set():
            try:
                s = tw.live_stream(uid)
                if s:
                    if live_since is None:
                        live_since = parse_iso(s["started_at"])
                        self.log("You're live! Clip away with your Stream Deck.")
                    if mode == "during":
                        self.status = "Live - making clips as you go"
                        self.grab(tw, uid, live_since, delay_new=45)
                    else:
                        checks = getattr(self, "_checks", 0)
                        self._checks = checks + 1
                        if checks % 3 == 0:
                            try:
                                n_live = len([c for c in tw.clips(uid, live_since - timedelta(minutes=1)) if c["id"] not in self.done_ids()])
                                self._live_count = n_live
                            except Exception:
                                pass
                        n_live = getattr(self, "_live_count", 0)
                        self.status = (f"Live - {n_live} clip(s) so far, they'll be made when you end stream"
                                       if n_live else "Live - clips will be made when you end stream")
                else:
                    if live_since is not None:
                        self.log("Stream ended. Making your clips now...")
                        self.status = "Making clips from your stream"
                        time.sleep(30)  # let the last clips finish on Twitch's side
                        n = self.grab(tw, uid, live_since - timedelta(minutes=1))
                        for wait in (120, 240):   # Twitch can be a few minutes late listing new clips
                            if self.stop_flag.is_set():
                                break
                            self.log("Checking Twitch again in a couple of minutes for any late clips...")
                            self.stop_flag.wait(wait)
                            n += self.grab(tw, uid, live_since - timedelta(minutes=1))
                        self.log(f"All done: {n} clip(s) ready.")
                        live_since = None
                    self.status = "Offline - waiting for you to go live"
            except Exception as e:
                self.log(f"Problem talking to Twitch: {e} (will try again)")
            self.stop_flag.wait(poll_secs)
        self.status = "Not watching"
        self.log("Stopped watching.")

    def start(self, cid, secret, login, mode):
        if self.thread and self.thread.is_alive():
            return
        self.stop_flag.clear()
        self.status = "Starting..."
        self.thread = threading.Thread(target=self.run, args=(cid, secret, login, mode), daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_flag.set()
        self.status = "Stopping..."

    def grab_recent(self, cid, secret, login, hours):
        def job():
            try:
                tw = Twitch(cid, secret)
                uid, _ = tw.user_id(login)
                since = datetime.now(timezone.utc) - timedelta(hours=float(hours))
                self.log(f"Looking for clips from the last {hours} hour(s)...")
                n = self.grab(tw, uid, since)
                if n:
                    self.log(f"Done: {n} new clip(s) made.")
                else:
                    self.log("No new clips found yet. Brand-new clips can take a few minutes to show up on Twitch's side, so try again in a bit.")
            except Exception as e:
                self.log(f"Problem: {e}")
        threading.Thread(target=job, daemon=True).start()
