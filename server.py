"""Lightweight YouTube research utility. Zero dependencies (stdlib only).

Run:  python server.py   ->  http://127.0.0.1:8791
Keys live in .env (YOUTUBE_API_KEY, ANTHROPIC_API_KEY, CHANNEL).
"""
import base64, http.client, io, json, mimetypes, os, re, secrets, threading, time, hashlib, statistics
import shutil, subprocess, sys, tempfile
import urllib.request, urllib.parse, urllib.error
from collections import Counter
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).parent
ENV_PATH = ROOT / ".env"
CACHE_DIR = ROOT / ".cache"
CACHE_DIR.mkdir(exist_ok=True)
DATA = ROOT / "data"
CHANNELS_F = DATA / "channels.json"
CH_SUBS = ("style_reference", "master_assets", "edits", "thumbs", "projects")  # per-channel folders under data/channels/<id>/
CTX = threading.local()  # the channel a request (or background job) is working for


class ChPath:
    """A path inside the ACTIVE channel's folder, resolved on every use, so code can keep writing EDITS / name, MASTER.rglob(...), TOKENS.exists() ..."""
    def __init__(self, sub):
        self.sub = sub

    def _p(self):
        return DATA / "channels" / cur_id() / self.sub

    def __truediv__(self, other):
        return self._p() / other

    def __fspath__(self):
        return str(self._p())

    def __str__(self):
        return str(self._p())

    def __getattr__(self, name):  # resolve(), iterdir(), glob(), exists(), read_text() ...
        return getattr(self._p(), name)


THUMBS, PROJECTS = ChPath("thumbs"), ChPath("projects")
EDITS, STYLE_DIR, MASTER = ChPath("edits"), ChPath("style_reference"), ChPath("master_assets")
SUBS = ("raw_video", "claude_assets", "user_assets", "thumbnail and metadata", "final_edit")  # one set per folder in data/edits
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}
TOKENS = ChPath("tokens.json")  # each channel signs in with its own Google account
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".wmv"}
CACHE_TTL = 3600  # seconds; protects the 10k units/day YouTube quota
CLAUDE_MODEL = "claude-sonnet-5-5"
YT = "https://www.googleapis.com/youtube/v3/"

STOP = set("""a an the and or but if of to in on at for with from by is are was were be been it its this that these those
i you he she we they my your our their me us them how why what when where who which do does did not no yes can will just
so as than then too very more most about into out up down over under again here there all any each few some such only own
same s t don now vs ft new video official full part episode""".split())


# ---------- config ----------
def load_env():
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def save_env(updates):
    env = load_env()
    env.update({k: v for k, v in updates.items() if v is not None})
    ENV_PATH.write_text("\n".join(f"{k}={v}" for k, v in env.items()) + "\n", encoding="utf-8")


# ---------- channels ----------
def load_channels():
    return json.loads(CHANNELS_F.read_text(encoding="utf-8")) if CHANNELS_F.exists() else []


def save_channels(chans):
    CHANNELS_F.write_text(json.dumps(chans, indent=2), encoding="utf-8")


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:30] or "channel"


def cur_id():
    chans = load_channels() or migrate_legacy()
    ids = [c["id"] for c in chans]
    ch = getattr(CTX, "ch", None)
    return ch if ch in ids else ids[0]


def active_channel():
    cid = cur_id()
    return next(c for c in load_channels() if c["id"] == cid)


def ensure_channel(cid, sample=False):
    base = DATA / "channels" / cid
    for sub in CH_SUBS:
        (base / sub).mkdir(parents=True, exist_ok=True)
    if sample and not any((base / "edits").iterdir()):  # template folder showing the per-video layout
        for sub in SUBS:
            (base / "edits" / "sample_video" / sub).mkdir(parents=True, exist_ok=True)


def migrate_legacy():
    """First run: build the channel list from .env and move the old single-channel data/ folders into the first channel."""
    chans = load_channels()
    if not chans:
        env = load_env()
        handle = env.get("CHANNEL", "")
        name = handle.lstrip("@") or "My channel"
        chans = [{"id": slugify(name), "name": name, "handle": handle, "niche": env.get("NICHE", "")}]
        save_channels(chans)
    base = DATA / "channels" / chans[0]["id"]
    base.mkdir(parents=True, exist_ok=True)
    for sub in CH_SUBS + ("tokens.json",):
        old, new = DATA / sub, base / sub
        if old.exists() and not new.exists():
            shutil.move(str(old), str(new))
    for c in chans:
        ensure_channel(c["id"], sample=(c["id"] == chans[0]["id"]))
    return chans


def channels_info():
    return {"channels": load_channels(), "active": cur_id()}


def upsert_channel(b):
    name, handle, niche = (b.get("name") or "").strip(), (b.get("handle") or "").strip(), (b.get("niche") or "").strip()
    if not name:
        raise ApiError("Give the channel a name.")
    if handle and not handle.startswith("@") and not handle.startswith("UC"):
        handle = "@" + handle
    chans = load_channels()
    c = next((x for x in chans if x["id"] == b.get("id")), None)
    if c:
        c.update(name=name, handle=handle, niche=niche)
    else:
        ids = {x["id"] for x in chans}
        cid, n = slugify(name), 2
        while cid in ids:
            cid, n = f"{slugify(name)}-{n}", n + 1
        c = {"id": cid, "name": name, "handle": handle, "niche": niche}
        chans.append(c)
        ensure_channel(cid, sample=True)
    save_channels(chans)
    return {"id": c["id"], **channels_info()}


def delete_channel(b):
    """Removes the channel from the list only; its folder under data/channels/ is left on disk."""
    chans = load_channels()
    if len(chans) <= 1:
        raise ApiError("You need at least one channel.")
    save_channels([c for c in chans if c["id"] != b.get("id")])
    return channels_info()


# ---------- YouTube quota tracker ----------
# The YouTube API cannot report remaining quota, so units are counted locally using Google's published costs.
# Quota belongs to the Google Cloud project (the API key), so it is shared by all channels, and resets at midnight Pacific time.
QUOTA_F = DATA / "quota.json"
QUOTA_LOCK = threading.Lock()
YT_COST = {"search": 100, "videos": 1, "channels": 1, "playlistItems": 1}


def _nth_sunday(year, month, n):
    first = datetime(year, month, 1)
    return first + timedelta(days=(6 - first.weekday()) % 7, weeks=n - 1)


def pt_offset(utc):
    """US Pacific offset from UTC (DST: 2nd Sunday of March 02:00 PST to 1st Sunday of November 02:00 PDT)."""
    start = _nth_sunday(utc.year, 3, 2).replace(hour=10, tzinfo=timezone.utc)
    end = _nth_sunday(utc.year, 11, 1).replace(hour=9, tzinfo=timezone.utc)
    return timedelta(hours=-7 if start <= utc < end else -8)


def quota_limit():
    try:
        return max(int(load_env().get("YT_QUOTA_LIMIT") or 10000), 1)
    except ValueError:
        return 10000


def _quota_load():
    now = datetime.now(timezone.utc)
    today = (now + pt_offset(now)).strftime("%Y-%m-%d")
    try:
        q = json.loads(QUOTA_F.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        q = {}
    if q.get("date") != today:
        q = {"date": today, "used": 0, "by": {}}
    return q


def quota_add(label, units):
    with QUOTA_LOCK:
        q = _quota_load()
        q["used"] += units
        q["by"][label] = q["by"].get(label, 0) + units
        QUOTA_F.write_text(json.dumps(q), encoding="utf-8")


def quota_exhausted():
    """Google said the quota is gone: sync the counter to the limit for today."""
    with QUOTA_LOCK:
        q = _quota_load()
        q["used"] = max(q["used"], quota_limit())
        QUOTA_F.write_text(json.dumps(q), encoding="utf-8")


def quota_info():
    with QUOTA_LOCK:
        q = _quota_load()
    now = datetime.now(timezone.utc)
    local = now + pt_offset(now)
    reset = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0) - pt_offset(now)
    limit = quota_limit()
    return {"used": q["used"], "limit": limit, "remaining": max(limit - q["used"], 0), "by": q["by"],
            "resetsInMin": int((reset.replace(tzinfo=timezone.utc) - now).total_seconds() // 60), "date": q["date"]}


# ---------- http helpers ----------
class ApiError(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


def http_json(url, data=None, headers=None, timeout=90):
    """POST when data is given: dict -> JSON body, bytes -> sent as-is (caller sets Content-Type)."""
    body = data if isinstance(data, bytes) else json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            msg = json.loads(body).get("error", {})
            msg = msg.get("message", body) if isinstance(msg, dict) else str(msg)
        except Exception:
            msg = body[:300]
        raise ApiError(f"{e.code}: {msg}", 502)
    except urllib.error.URLError as e:
        raise ApiError(f"Network error: {e.reason}", 502)


def yt_get(endpoint, **params):
    key = load_env().get("YOUTUBE_API_KEY")
    if not key:
        raise ApiError("Add your YOUTUBE_API_KEY in Settings first.", 400)
    params = {k: v for k, v in params.items() if v not in (None, "")}
    cache_file = CACHE_DIR / (hashlib.md5((endpoint + json.dumps(params, sort_keys=True)).encode()).hexdigest() + ".json")
    if cache_file.exists() and time.time() - cache_file.stat().st_mtime < CACHE_TTL:
        return json.loads(cache_file.read_text(encoding="utf-8"))
    try:
        data = http_json(YT + endpoint + "?" + urllib.parse.urlencode({**params, "key": key}))
    except ApiError as e:
        if "quota" in str(e).lower():
            quota_exhausted()
        raise
    quota_add(endpoint, YT_COST.get(endpoint, 1))
    cache_file.write_text(json.dumps(data), encoding="utf-8")
    return data


# ---------- youtube helpers ----------
def parse_duration(iso):
    m = re.match(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    d, h, mi, s = (int(x or 0) for x in m.groups()) if m else (0, 0, 0, 0)
    return d * 86400 + h * 3600 + mi * 60 + s


def parse_dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fetch_videos(ids):
    out = []
    for i in range(0, len(ids), 50):
        data = yt_get("videos", part="snippet,statistics,contentDetails", id=",".join(ids[i:i + 50]))
        now = datetime.now(timezone.utc)
        for it in data.get("items", []):
            st, sn = it.get("statistics", {}), it["snippet"]
            views, likes, comments = (int(st.get(k, 0)) for k in ("viewCount", "likeCount", "commentCount"))
            pub = parse_dt(sn["publishedAt"])
            age = max((now - pub).total_seconds() / 86400, 0.5)
            dur = parse_duration(it["contentDetails"].get("duration"))
            out.append({
                "id": it["id"], "title": sn["title"], "channelId": sn["channelId"], "channel": sn["channelTitle"],
                "published": sn["publishedAt"], "thumb": (sn["thumbnails"].get("medium") or sn["thumbnails"]["default"])["url"],
                "tags": sn.get("tags", []), "desc": sn.get("description", "")[:2000],
                "views": views, "likes": likes, "comments": comments, "duration": dur, "short": dur <= 60,
                "ageDays": round(age, 1), "viewsPerDay": round(views / age, 1),
                "engagement": round((likes + comments) / views * 100, 2) if views else 0,
            })
    return out


def keywords(videos, n=15, weight="views"):
    """Top words + bigrams in titles, weighted by views/day (so fast movers outrank old hits)."""
    score = Counter()
    for v in videos:
        w = [t for t in re.findall(r"[a-zA-Z0-9']+", v["title"].lower()) if t not in STOP and len(t) > 2]
        wt = v["viewsPerDay"] if weight == "views" else 1
        for t in set(w):
            score[t] += wt
        for a, b in zip(w, w[1:]):
            score[f"{a} {b}"] += wt * 1.5
    return [{"term": t, "score": round(s)} for t, s in score.most_common(n)]


# ---------- 1. channel analysis ----------
def channel_analysis(handle, count=50):
    handle = (handle or active_channel().get("handle", "")).strip()
    if not handle:
        raise ApiError("Enter your channel handle (e.g. @mychannel) or channel ID.")
    if "youtube.com/" in handle:
        handle = handle.rstrip("/").split("/")[-1]
    if handle.startswith("UC") and len(handle) == 24:
        ch = yt_get("channels", part="snippet,statistics,contentDetails", id=handle)
    else:
        ch = yt_get("channels", part="snippet,statistics,contentDetails", forHandle=handle if handle.startswith("@") else "@" + handle)
    if not ch.get("items"):
        raise ApiError(f"Channel '{handle}' not found.", 404)
    c = ch["items"][0]
    uploads = c["contentDetails"]["relatedPlaylists"]["uploads"]
    ids, token = [], None
    while len(ids) < count:
        pl = yt_get("playlistItems", part="contentDetails", playlistId=uploads, maxResults=50, pageToken=token)
        ids += [i["contentDetails"]["videoId"] for i in pl.get("items", [])]
        token = pl.get("nextPageToken")
        if not token:
            break
    vids = fetch_videos(ids[:count])
    vids.sort(key=lambda v: v["published"], reverse=True)
    st = c["statistics"]
    subs = int(st.get("subscriberCount", 0))
    longs = [v for v in vids if not v["short"]] or vids
    med = statistics.median(v["views"] for v in vids) if vids else 0

    # upload cadence
    dates = sorted(parse_dt(v["published"]) for v in vids)
    gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
    per_month = round(30 / statistics.mean(gaps), 1) if gaps and statistics.mean(gaps) > 0 else None

    # what works: weekday + duration buckets
    def bucket(v):
        m = v["duration"] / 60
        return "Shorts (<=60s)" if v["short"] else "<5 min" if m < 5 else "5-10 min" if m < 10 else "10-20 min" if m < 20 else "20+ min"
    def group(keyfn):
        g = {}
        for v in vids:
            g.setdefault(keyfn(v), []).append(v)
        return [{"label": k, "count": len(x), "medianViews": round(statistics.median(y["views"] for y in x)),
                 "avgEngagement": round(statistics.mean(y["engagement"] for y in x), 2)} for k, x in g.items()]
    weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    by_day = sorted(group(lambda v: weekdays[parse_dt(v["published"]).weekday()]), key=lambda r: weekdays.index(r["label"]))
    by_len = sorted(group(bucket), key=lambda r: ["Shorts (<=60s)", "<5 min", "5-10 min", "10-20 min", "20+ min"].index(r["label"]))

    for v in vids:
        v["vsMedian"] = round(v["views"] / med, 2) if med else 0

    top = sorted(vids, key=lambda v: v["views"], reverse=True)
    insights = []
    if by_len:
        best = max(by_len, key=lambda r: r["medianViews"])
        insights.append(f"Best-performing format: {best['label']} (median {best['medianViews']:,} views over {best['count']} videos).")
    if len(by_day) > 1:
        bd = max((r for r in by_day if r["count"] >= 2), key=lambda r: r["medianViews"], default=None)
        if bd:
            insights.append(f"{bd['label']} uploads get the highest median views ({bd['medianViews']:,}).")
    overperf = [v for v in vids if v["vsMedian"] >= 2]
    if overperf:
        insights.append(f"{len(overperf)} videos did 2x+ your median; study their titles/hooks (see Outliers).")
    if per_month:
        insights.append(f"You upload about {per_month} videos/month based on your last {len(vids)}.")
    recent, older = vids[:10], vids[10:20]
    if len(older) >= 5:
        r, o = statistics.median(v["viewsPerDay"] for v in recent), statistics.median(v["viewsPerDay"] for v in older)
        if o:
            insights.append(f"Recent 10 videos' median views/day is {'up' if r >= o else 'down'} {abs(r / o - 1) * 100:.0f}% vs the 10 before.")

    return {
        "channel": {"id": c["id"], "title": c["snippet"]["title"], "thumb": c["snippet"]["thumbnails"]["default"]["url"],
                    "subs": subs, "views": int(st.get("viewCount", 0)), "videos": int(st.get("videoCount", 0)),
                    "hiddenSubs": st.get("hiddenSubscriberCount", False)},
        "summary": {"analyzed": len(vids), "medianViews": round(med), "avgEngagement": round(statistics.mean(v["engagement"] for v in vids), 2) if vids else 0,
                    "medianViewsPerDay": round(statistics.median(v["viewsPerDay"] for v in vids), 1) if vids else 0,
                    "avgDurationMin": round(statistics.mean(v["duration"] for v in longs) / 60, 1) if longs else 0,
                    "uploadsPerMonth": per_month},
        "insights": insights, "byWeekday": by_day, "byLength": by_len,
        "top": top[:5], "bottom": top[-5:][::-1], "outliers": [v for v in top if v["vsMedian"] >= 2][:8],
        "keywords": keywords(top[:25]), "videos": vids,
    }


# ---------- 2. trends ----------
def trends(q, days=7, region="US", max_results=30, order="viewCount"):
    if not q.strip():
        raise ApiError("Enter niche keywords, e.g. 'home espresso, coffee gear'.")
    after = (datetime.now(timezone.utc) - timedelta(days=int(days))).strftime("%Y-%m-%dT%H:%M:%SZ")
    ids = []
    for term in [t.strip() for t in q.split(",") if t.strip()][:4]:  # each search costs 100 quota units
        s = yt_get("search", part="snippet", q=term, type="video", order=order, publishedAfter=after,
                   regionCode=region or None, maxResults=min(int(max_results), 50))
        ids += [i["id"]["videoId"] for i in s.get("items", [])]
    ids = list(dict.fromkeys(ids))
    vids = fetch_videos(ids)
    chans = {}
    cids = list({v["channelId"] for v in vids})
    for i in range(0, len(cids), 50):
        for c in yt_get("channels", part="statistics", id=",".join(cids[i:i + 50])).get("items", []):
            chans[c["id"]] = int(c["statistics"].get("subscriberCount", 0))
    for v in vids:
        subs = chans.get(v["channelId"], 0)
        v["subs"] = subs
        v["outlier"] = round(v["views"] / subs, 2) if subs else None  # views relative to audience size
    vids.sort(key=lambda v: v["viewsPerDay"], reverse=True)
    small_winners = [v for v in vids if v["outlier"] and v["outlier"] >= 1 and v["subs"] < 100000][:8]
    return {"query": q, "days": days, "videos": vids, "keywords": keywords(vids, 20), "smallChannelWinners": small_winners,
            "ideas": [f"Videos on '{k['term']}' are gaining traction right now." for k in keywords(vids, 5)]}


# ---------- 3. script ----------
def ts(sec):
    sec = int(round(sec))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def make_script(p):
    if not claude_mode():
        raise ApiError("Add an ANTHROPIC_API_KEY in Settings, or install Claude Code and log in.")
    topic = (p.get("topic") or "").strip()
    if not topic:
        raise ApiError("Enter a topic.")
    minutes, wpm = float(p.get("minutes") or 8), int(p.get("wpm") or 150)
    words = int(minutes * wpm)
    system = (
        "You are a YouTube scriptwriter. Write spoken-word scripts: short sentences, concrete examples, open loops, "
        "no stage directions inside narration. Return ONLY valid JSON, no markdown fences."
    )
    user = f"""Topic: {topic}
Audience/niche: {p.get('niche') or 'general'}
Tone: {p.get('tone') or 'conversational, energetic'}
Target length: {minutes} minutes (~{words} spoken words total at {wpm} wpm)
Extra notes / channel context: {p.get('notes') or 'none'}

Return JSON:
{{"titles": [5 click-worthy but honest title options, <=65 chars],
 "thumbnail_ideas": [3 short concepts],
 "hook": "first 15-30 seconds narration, pattern-interrupt + promise",
 "sections": [{{"heading": "chapter title", "narration": "spoken script", "visuals": "B-roll/on-screen notes"}}],
 "outro": "CTA + next-video tease narration",
 "description": "YouTube description (2-3 short paragraphs, no timestamps)",
 "tags": [10-15 tags]}}
Use 4-7 sections. Total words across hook+sections+outro should be about {words}."""
    return add_timestamps(claude_json(system, user), wpm)


def add_timestamps(s, wpm):
    """Timestamps come from word counts, so they match real pacing."""
    def secs(t): return len(re.findall(r"\S+", t or "")) / wpm * 60
    blocks = [{"heading": "Hook", "narration": s.get("hook", ""), "visuals": ""}] + s.get("sections", []) + \
             [{"heading": "Outro", "narration": s.get("outro", ""), "visuals": ""}]
    t = 0.0
    for b in blocks:
        b["start"], b["startLabel"] = round(t), ts(t)
        b["seconds"] = round(secs(b["narration"]))
        b["words"] = len(re.findall(r"\S+", b["narration"]))
        t += secs(b["narration"])
    s["blocks"] = blocks
    s["totalSeconds"], s["totalLabel"] = round(t), ts(t)
    s["chapters"] = "\n".join(f"{b['startLabel']} {b['heading']}" for b in blocks)
    s["fullScript"] = "\n\n".join(f"[{b['startLabel']}] {b['heading'].upper()}\n{b['narration']}" for b in blocks)
    return s


# ---------- AI helpers ----------
def local_image_bytes(path, size=768):
    """Downscaled JPEG bytes of a local image (keeps prompts small); raw bytes if Pillow is missing."""
    try:
        from PIL import Image
        im = Image.open(path).convert("RGB")
        im.thumbnail((size, size))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80)
        return buf.getvalue()
    except ImportError:
        return Path(path).read_bytes()


def claude_mode():
    """'api' when an ANTHROPIC_API_KEY is saved, else 'cli' (the logged-in Claude Code / Claude desktop login), else None."""
    if load_env().get("ANTHROPIC_API_KEY"):
        return "api"
    return "cli" if shutil.which("claude") else None


def claude_cli(system, content):
    """Run a prompt through `claude -p` so it uses the user's Claude login instead of an API key."""
    tmp = Path(tempfile.mkdtemp(prefix="cli_", dir=CACHE_DIR))
    try:
        prompt, has_images = content, False
        if isinstance(content, list):  # [{text}, {image url}, ...] -> download images, let Claude Read them
            parts = []
            for b in content:
                if b["type"] == "text":
                    parts.append(b["text"])
                    continue
                f = tmp / f"img{len(parts)}.jpg"
                if "path" in b:
                    f.write_bytes(local_image_bytes(b["path"]))
                else:
                    with urllib.request.urlopen(b["source"]["url"], timeout=30) as r:
                        f.write_bytes(r.read())
                parts.append(f"(image file: {f})")
                has_images = True
            prompt = "\n".join(parts) + ("\n\nOpen each image file above with the Read tool and use what you see." if has_images else "")
        cmd = ["claude", "-p", "--system-prompt", system, "--model", load_env().get("CLAUDE_CLI_MODEL", "sonnet"),
               "--no-session-persistence", "--disable-slash-commands", "--strict-mcp-config"]
        cmd += ["--tools", "Read", "--allowedTools", "Read", "--add-dir", str(tmp)] if has_images else ["--tools", ""]
        try:
            r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", timeout=420, cwd=tmp)
        except subprocess.TimeoutExpired:
            raise ApiError("Claude took too long (over 7 minutes); try a shorter script.", 502)
        out = (r.stdout or "").strip()
        if "not logged in" in out.lower() or "/login" in out.lower():
            raise ApiError("Claude CLI isn't logged in. Open a terminal, run `claude`, type /login, then retry (or add an Anthropic API key in Settings).", 400)
        if r.returncode != 0 or not out:
            raise ApiError(f"Claude CLI failed: {(r.stderr or out)[:300]}", 502)
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def claude(system, content, max_tokens=8000):
    key = load_env().get("ANTHROPIC_API_KEY")
    if not key:
        if claude_mode() == "cli":
            return claude_cli(system, content)
        raise ApiError("Add an ANTHROPIC_API_KEY in Settings, or install Claude Code and log in.")
    if isinstance(content, list):  # local image blocks -> base64 for the API
        content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                    "data": base64.b64encode(local_image_bytes(b["path"])).decode()}} if "path" in b else b for b in content]
    r = http_json("https://api.anthropic.com/v1/messages",
                  {"model": CLAUDE_MODEL, "max_tokens": max_tokens, "system": system, "messages": [{"role": "user", "content": content}]},
                  {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}, timeout=240)
    return "".join(b.get("text", "") for b in r["content"])


def claude_json(system, content, max_tokens=8000, images=()):
    """Ask for JSON; thumbnails (YouTube image URLs) are attached for vision, retried without them if the fetch fails."""
    def run(c):
        text = claude(system + " Return ONLY valid JSON, no markdown fences.", c, max_tokens)
        m = re.search(r"\{.*\}", text, re.S)
        try:
            return json.loads(m.group(0))
        except Exception:
            raise ApiError("Model returned malformed JSON; try again.", 502)
    if images and not isinstance(content, list):
        blocks = [{"type": "text", "text": content}]
        for label, vid in images:
            blocks += [{"type": "text", "text": f"Thumbnail: {label}"},
                       {"type": "image", "source": {"type": "url", "url": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"}}]
        try:
            return run(blocks)
        except ApiError as e:
            if e.status != 502 or "malformed" in str(e):
                raise
    return run(content)


def slim(v, tags=10):
    d = {k: v.get(k) for k in ("id", "title", "channel", "subs", "views", "viewsPerDay", "outlier", "engagement")}
    d.update(minutes=round(v["duration"] / 60, 1), tags=v.get("tags", [])[:tags], ageDays=v["ageDays"])
    return d


def attach_subs(vids):
    cids = list({v["channelId"] for v in vids})
    subs = {}
    for i in range(0, len(cids), 50):
        for c in yt_get("channels", part="statistics", id=",".join(cids[i:i + 50])).get("items", []):
            subs[c["id"]] = int(c["statistics"].get("subscriberCount", 0))
    for v in vids:
        v["subs"] = subs.get(v["channelId"], 0)
        v["outlier"] = round(v["views"] / v["subs"], 2) if v["subs"] else None
    return vids


def tag_freq(vids):
    c = Counter()
    for v in vids:
        for t in {x.lower() for x in v.get("tags", [])}:
            c[t] += 1
    return c


# ---------- video-level analysis ----------
def extract_video_id(s):
    m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/)([\w-]{11})", s or "") or re.fullmatch(r"([\w-]{11})", (s or "").strip())
    if not m:
        raise ApiError("Couldn't find a video ID in that input.")
    return m.group(1)


def profile(v):
    t, d = v["title"], v.get("desc", "")
    return {"Title length (chars)": len(t), "Title words": len(t.split()), "Has a number in title (%)": int(bool(re.search(r"\d", t))),
            "Question title (%)": int("?" in t), "Number of tags": len(v.get("tags", [])), "Description length": len(d),
            "Chapters in description (%)": int(bool(re.search(r"\b\d{1,2}:\d{2}\b", d))), "Length (min)": round(v["duration"] / 60, 1)}


def similar_successful(v, n=8):
    words = [w for w in re.findall(r"[a-zA-Z0-9']+", v["title"].lower()) if w not in STOP and len(w) > 2][:5]
    q = " ".join(words) or v["title"]
    ids = []
    for order in ("relevance", "viewCount"):
        s = yt_get("search", part="snippet", q=q, type="video", order=order, maxResults=25)
        ids += [i["id"]["videoId"] for i in s.get("items", [])]
    vids = [x for x in fetch_videos(list(dict.fromkeys(ids))) if x["channelId"] != v["channelId"] and x["id"] != v["id"] and x["ageDays"] >= 3]
    attach_subs(vids)
    vids.sort(key=lambda x: x["viewsPerDay"], reverse=True)
    return vids[:n], q


def video_detail(vid_input):
    vid = extract_video_id(vid_input)
    found = fetch_videos([vid])
    if not found:
        raise ApiError("Video not found.", 404)
    v = found[0]
    sims, q = similar_successful(v)
    if not sims:
        raise ApiError("No comparable videos found for this title.", 404)
    mine, own_tags = profile(v), {t.lower() for t in v["tags"]}
    profs = [profile(s) for s in sims]
    compare = [{"metric": k, "you": mine[k], "benchmark": round(statistics.median(p[k] for p in profs), 1)} for k in mine]
    freq = tag_freq(sims)
    missing_tags = [t for t, c in freq.most_common(40) if c >= 2 and t not in own_tags][:15] or \
                   [t for t, c in freq.most_common(10) if t not in own_tags]
    haystack = (v["title"] + " " + " ".join(v["tags"])).lower()
    missing_kw = [k for k in keywords(sims, 20) if k["term"] not in haystack][:10]
    out = {"video": v, "similar": sims, "query": q, "compare": compare, "missingTags": missing_tags, "missingKeywords": missing_kw,
           "ai": None, "aiError": None}
    payload = {"your_video": {**slim(v, 30), "description_start": v["desc"][:1200]},
               "successful_similar_videos": [{**slim(s, 15), "description_start": s["desc"][:300]} for s in sims],
               "benchmark_medians_vs_you": compare, "tag_candidates_used_by_multiple_winners": missing_tags,
               "keyword_candidates": missing_kw}
    system = ("You are a YouTube growth analyst. Compare the creator's video with similar videos from other channels that perform well, "
              "using the attached thumbnails and metadata. Ground every recommendation in the data; mention which comparator pattern supports it. "
              "Schema: {\"summary\": str, \"improvements\": [{\"area\": \"title|thumbnail|tags|description|length|hook\", \"priority\": \"high|medium|low\", "
              "\"issue\": str, \"fix\": str}], \"keywords\": [{\"term\": str, \"why\": str}], \"tags\": [str, up to 20, total under 450 chars], "
              "\"title_options\": [str x5, <=65 chars], \"description_rewrite\": str, "
              "\"thumbnail\": {\"diagnosis\": str comparing yours to the winners, \"ideas\": [{\"concept\": str, \"text_overlay\": str (<=4 words), "
              "\"image_prompt\": str (detailed prompt for an AI image generator, 16:9 composition, key subject centered)} x3]}}")
    try:
        out["ai"] = claude_json(system, json.dumps(payload), 6000, images=[("YOURS", v["id"])] + [(f"top comparator '{s['title'][:60]}'", s["id"]) for s in sims[:4]])
    except ApiError as e:
        out["aiError"] = str(e)
    return out


# ---------- next-3 video ideas + full package ----------
def next_videos(p):
    ch = channel_analysis(p.get("handle"), 50)
    default_q = ", ".join(k["term"] for k in ch["keywords"] if " " not in k["term"])[:80]
    q = ", ".join([t.strip() for t in (p.get("q") or active_channel().get("niche") or default_q).split(",") if t.strip()][:2])
    tr = trends(q, 14, p.get("region") or "US", 25)
    own = lambda v: {"title": v["title"], "views": v["views"], "x_median": v["vsMedian"], "minutes": round(v["duration"] / 60, 1)}
    payload = {"channel": {"name": ch["channel"]["title"], "subs": ch["channel"]["subs"], "summary": ch["summary"], "insights": ch["insights"],
                           "best_formats_by_length": ch["byLength"], "top_videos": [own(v) for v in ch["top"]],
                           "weak_videos": [own(v) for v in ch["bottom"]], "title_keywords": ch["keywords"][:12],
                           "already_published_titles": [v["title"] for v in ch["videos"][:30]]},
               "niche_query": q, "trending_now": [slim(v) for v in tr["videos"][:15]], "rising_terms": tr["keywords"][:15],
               "small_channels_beating_their_size": [slim(v) for v in tr["smallChannelWinners"]]}
    system = ("You are a YouTube strategist. Propose EXACTLY 3 next-video ideas for this creator that combine (a) what already works on their channel "
              "with (b) what is trending in the niche right now. Do not repeat published titles. Each idea must cite at least two trending videos by id. "
              "Schema: {\"ideas\": [{\"title\": str, \"angle\": str, \"why\": str (evidence from the data), \"ref_ids\": [str], "
              "\"search_terms\": [str, 2 short queries to research this topic], \"suggested_minutes\": number, \"format\": str, \"potential\": \"high|medium\"}]}")
    res = claude_json(system, json.dumps(payload), 4000)
    refs = {v["id"]: v for v in tr["videos"]}
    for i in res.get("ideas", []):
        i["refs"] = [{"id": r, "title": refs[r]["title"], "channel": refs[r]["channel"], "views": refs[r]["views"], "thumb": refs[r]["thumb"]}
                     for r in i.get("ref_ids", []) if r in refs]
    return {"ideas": res.get("ideas", [])[:3], "query": q, "risingTerms": tr["keywords"][:15]}


def build_package(p):
    idea, inputs = p.get("idea") or {}, p.get("inputs") or {}
    if not idea.get("title"):
        raise ApiError("No idea selected.")
    minutes, wpm = float(p.get("minutes") or idea.get("suggested_minutes") or 8), int(p.get("wpm") or 150)
    q = ", ".join((idea.get("search_terms") or [idea["title"]])[:2])
    tr = trends(q, 30, p.get("region") or "US", 25)
    top = tr["videos"][:10]
    freq = tag_freq(top)
    try:
        ch = channel_analysis(p.get("handle"), 30)
        mine = {"channel": ch["channel"]["title"], "top_titles": [v["title"] for v in ch["top"]], "insights": ch["insights"]}
    except ApiError:
        mine = {}
    payload = {"idea": idea, "creator_inputs": {k: v for k, v in inputs.items() if str(v).strip()},
               "target": {"minutes": minutes, "spoken_words": int(minutes * wpm), "wpm": wpm},
               "creator_channel": mine, "trending_videos_on_this_topic": [{**slim(v, 15), "description_start": v["desc"][:250]} for v in top],
               "rising_terms": tr["keywords"][:20], "tags_used_by_multiple_top_videos": [{"tag": t, "count": c} for t, c in freq.most_common(25) if c >= 2]}
    system = ("You are a YouTube scriptwriter and packaging strategist. Using the trending-video data and attached thumbnails, produce a complete video package. "
              "Write spoken narration (short sentences, concrete examples, open loops, no stage directions in narration). Respect the creator's inputs. "
              "Prefer tags that real top videos use. Schema: {\"titles\": [str x5, <=65 chars, honest], \"hook\": str, "
              "\"sections\": [{\"heading\": str, \"narration\": str, \"visuals\": str}] (4-7), \"outro\": str, "
              "\"description\": str (2-3 short paragraphs, keywords early, NO timestamps), \"tags\": [str, 12-20, total under 450 chars], "
              "\"keywords\": [{\"term\": str, \"why\": str}] (8-12 trending keywords to use in title/script/description), "
              "\"thumbnail\": {\"analysis\": str (what the winning thumbnails share: colors, faces, text, composition), "
              "\"ideas\": [{\"concept\": str, \"text_overlay\": str (<=4 words), \"image_prompt\": str (detailed, 16:9, key subject centered)} x3]}}. "
              "Total words in hook+sections+outro should be about the spoken_words target.")
    pkg = claude_json(system, json.dumps(payload), 12000, images=[(f"trending '{v['title'][:60]}'", v["id"]) for v in top[:4]])
    add_timestamps(pkg, wpm)
    pkg["wpm"], pkg["query"] = wpm, q
    pkg["evidence"] = {"keywords": tr["keywords"][:20], "tags": [{"tag": t, "count": c} for t, c in freq.most_common(25) if c >= 2],
                       "videos": [slim(v, 6) | {"thumb": v["thumb"]} for v in top[:8]]}
    return pkg


# ---------- thumbnails (OpenAI) ----------
def to_16x9_jpeg(raw):
    """gpt-image-1 only makes 3:2; fit the whole picture into a 1280x720 frame (blurred side fill) instead of cropping, so no text/face is cut off."""
    try:
        from PIL import Image, ImageFilter, ImageEnhance
    except ImportError:
        return raw
    im = Image.open(io.BytesIO(raw)).convert("RGB")
    fg = im.resize((round(im.width * 720 / im.height), 720), Image.LANCZOS)
    bg = im.resize((1280, round(im.height * 1280 / im.width)), Image.LANCZOS)
    top = (bg.height - 720) // 2
    bg = ImageEnhance.Brightness(bg.crop((0, top, 1280, top + 720)).filter(ImageFilter.GaussianBlur(28))).enhance(.7)
    bg.paste(fg, ((1280 - fg.width) // 2, 0))
    buf = io.BytesIO()
    bg.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def art_direct(prompt, overlay, ctx):
    """Claude turns the rough idea + video context into one detailed, original brief for the image model."""
    system = ("You are an elite YouTube thumbnail art director. Write ONE prompt for an AI image model (gpt-image-1) for a 16:9 thumbnail. Rules: a single clear focal "
              "subject; a curiosity gap or strong emotion; a fresh, specific visual metaphor tied to THIS video's promise (avoid stock clichés like generic money piles, arrows "
              "and fake dashboards); 2-3 dominant colours with strong contrast; shallow depth, dramatic lighting; key subject and text inside the centre band; "
              "the picture is shown in a 16:9 frame with blurred side bars, so keep the subject and text centred horizontally and leave a small breathing margin (about 5%) around every edge; "
              "if text is requested, give the EXACT words (max 4) in huge bold lettering and nothing else textual; no logos, no UI mockups, no real people or celebrities. "
              "Describe subject, pose/expression, setting, lighting, colour palette, camera angle and text placement concretely. Output the prompt only, under 1200 characters.")
    user = json.dumps({"video_context": ctx, "rough_concept": prompt, "text_overlay": overlay or ""})
    return claude(system, user, 1500).strip().strip('"')[:1800]


def gen_thumbnail(b):
    key = load_env().get("OPENAI_API_KEY")
    if not key:
        raise ApiError("Add your OPENAI_API_KEY in Settings to generate thumbnails.")
    prompt, overlay, ctx = (b.get("prompt") or "").strip(), (b.get("overlay") or "").strip(), b.get("context") or {}
    if not prompt:
        raise ApiError("Empty thumbnail prompt.")
    used, directed = prompt, False
    if b.get("enhance", True):
        try:
            used, directed = art_direct(prompt, overlay, ctx), True
        except ApiError:
            used = prompt  # fall back to the raw concept if Claude is unavailable
    if overlay and overlay.lower() not in used.lower():
        used += f' Include this exact text in huge bold lettering: "{overlay}".'
    full = ("YouTube thumbnail, 16:9 landscape, cinematic, high contrast, vivid colours, uncluttered, key subject and all text centred with a small margin from every edge, "
            f"no watermarks, no logos. {used}")
    quality = b.get("quality") if b.get("quality") in ("low", "medium", "high") else "high"
    r = http_json("https://api.openai.com/v1/images/generations",
                  {"model": load_env().get("OPENAI_IMAGE_MODEL", "gpt-image-1"), "prompt": full[:4000], "size": "1536x1024",
                   "quality": quality, "output_format": "jpeg", "output_compression": 92, "n": 1},
                  {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, timeout=300)
    raw = to_16x9_jpeg(base64.b64decode(r["data"][0]["b64_json"]))
    name = f"{int(time.time())}_{hashlib.md5(raw).hexdigest()[:6]}.jpg"
    (THUMBS / name).write_bytes(raw)
    return {"name": name, "url": f"/thumbs/{name}?ch={cur_id()}", "prompt": used, "directed": directed, "quality": quality}


# ---------- projects ----------
def project_save(d):
    pid = re.sub(r"\W", "", str(d.get("id") or "")) or str(int(time.time()))
    d["id"], d["updated"] = pid, int(time.time())
    (PROJECTS / f"{pid}.json").write_text(json.dumps(d), encoding="utf-8")
    return {"id": pid}


def project_load(pid):
    f = PROJECTS / (re.sub(r"\W", "", pid or "") + ".json")
    if not f.exists():
        raise ApiError("Project not found.", 404)
    return json.loads(f.read_text(encoding="utf-8"))


def project_list():
    out = []
    for f in PROJECTS.glob("*.json"):
        d = json.loads(f.read_text(encoding="utf-8"))
        out.append({"id": d["id"], "title": d.get("title") or (d.get("idea") or {}).get("title", "Untitled"), "updated": d.get("updated", 0)})
    return sorted(out, key=lambda x: -x["updated"])


# ---------- publish (YouTube OAuth + resumable upload) ----------
SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"
OAUTH_STATE = {"v": None, "ch": None}
JOBS = {}
CHUNK = 32 * 262144  # 8 MB, must be a multiple of 256 KB


def http_form(url, form):
    return http_json(url, urllib.parse.urlencode(form).encode(), {"Content-Type": "application/x-www-form-urlencoded"})


def oauth_url(redirect):
    cid = load_env().get("GOOGLE_CLIENT_ID")
    if not cid:
        raise ApiError("Add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in Settings first.")
    OAUTH_STATE["v"] = secrets.token_urlsafe(16)
    OAUTH_STATE["ch"] = cur_id()
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": cid, "redirect_uri": redirect, "response_type": "code", "scope": SCOPES,
        "access_type": "offline", "prompt": "consent", "state": OAUTH_STATE["v"]})


def oauth_callback(code, state, redirect):
    if not state or state != OAUTH_STATE["v"]:
        raise ApiError("OAuth state mismatch; start the connection again.")
    OAUTH_STATE["v"], CTX.ch = None, OAUTH_STATE["ch"]  # tokens are saved for the channel that started the sign-in
    env = load_env()
    t = http_form("https://oauth2.googleapis.com/token", {"code": code, "client_id": env.get("GOOGLE_CLIENT_ID"),
                  "client_secret": env.get("GOOGLE_CLIENT_SECRET"), "redirect_uri": redirect, "grant_type": "authorization_code"})
    t["expires_at"] = time.time() + t.get("expires_in", 3600)
    if "refresh_token" not in t and TOKENS.exists():
        t["refresh_token"] = json.loads(TOKENS.read_text()).get("refresh_token")
    TOKENS.write_text(json.dumps(t), encoding="utf-8")


def google_token():
    if not TOKENS.exists():
        raise ApiError("Connect your Google account in the Publish tab first.")
    t = json.loads(TOKENS.read_text())
    if t["expires_at"] > time.time() + 60:
        return t["access_token"]
    env = load_env()
    n = http_form("https://oauth2.googleapis.com/token", {"client_id": env.get("GOOGLE_CLIENT_ID"), "client_secret": env.get("GOOGLE_CLIENT_SECRET"),
                  "refresh_token": t["refresh_token"], "grant_type": "refresh_token"})
    t.update(access_token=n["access_token"], expires_at=time.time() + n.get("expires_in", 3600))
    TOKENS.write_text(json.dumps(t), encoding="utf-8")
    return t["access_token"]


def connected_channel():
    if not TOKENS.exists():
        return None
    try:
        r = http_json("https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true", headers={"Authorization": f"Bearer {google_token()}"})
        return r["items"][0]["snippet"]["title"] if r.get("items") else "(no channel on this Google account)"
    except ApiError:
        return None


def job_status(jid):
    if jid not in JOBS:
        raise ApiError("Unknown job.", 404)
    return JOBS[jid]


# ---------- workspace: edit folders, style.md, master assets ----------
def start_job(label, fn):
    """Run a long task in a thread; the UI polls /api/publish/status?job=<id> for progress."""
    jid = secrets.token_hex(4)
    job = JOBS[jid] = {"state": "running", "progress": 0, "msg": label, "label": label}
    ch = cur_id()

    def run():
        CTX.ch = ch
        try:
            job["result"] = fn(job)
            job.update(state="done", progress=100)
        except ApiError as e:
            job.update(state="error", error=str(e))
        except Exception as e:  # noqa
            job.update(state="error", error=f"{type(e).__name__}: {e}")
    threading.Thread(target=run, daemon=True).start()
    return {"job": jid}


def edit_dir(name, create=False):
    name = re.sub(r"[^\w\- ]", "", name or "").strip()[:60]
    if not name:
        raise ApiError("Give the edit folder a name.")
    d = (EDITS / name).resolve()
    if d.parent != EDITS.resolve():
        raise ApiError("Bad folder name.")
    if not d.is_dir():
        if not create:
            raise ApiError(f"Edit folder '{name}' not found.", 404)
        d.mkdir()
    for s in SUBS:
        if not (d / s).exists():
            (d / s).mkdir()
    return d


def files_in(p, exts=None):
    if p.is_file():
        return [p.name]
    return sorted(f.name for f in p.iterdir() if f.is_file() and (exts is None or f.suffix.lower() in exts)) if p.is_dir() else []


def meta_file(d):
    t = d / "thumbnail and metadata"
    txts = sorted(t.glob("*.txt"))
    return next((f for f in txts if f.name == "metadata.txt"), txts[0] if txts else t / "metadata.txt")


def write_metadata(d, m):
    kw = ", ".join(k["term"] if isinstance(k, dict) else str(k) for k in m.get("keywords", []))
    meta_file(d).write_text(
        f"TITLE: {m.get('title', '')}\n\nDESCRIPTION:\n{m.get('description', '')}\n\nTAGS: {', '.join(m.get('tags', []))}\n\n"
        f"KEYWORDS: {kw}\n\nCHAPTERS:\n{m.get('chapters', '')}\n", encoding="utf-8")


def read_metadata(d):
    f = meta_file(d)
    out, cur = {"title": "", "description": "", "tags": [], "keywords": [], "chapters": ""}, None
    if not f.exists():
        return out
    for line in f.read_text(encoding="utf-8").splitlines():
        h = re.match(r"^(TITLE|DESCRIPTION|TAGS|KEYWORDS|CHAPTERS):\s*(.*)$", line)
        if h:
            cur, rest = h.group(1).lower(), h.group(2)
            if cur in ("tags", "keywords"):
                out[cur] = [x.strip() for x in rest.split(",") if x.strip()]
            else:
                out[cur] = rest
        elif cur in ("description", "chapters"):
            out[cur] += ("\n" if out[cur] else "") + line
    out["description"], out["chapters"] = out["description"].strip(), out["chapters"].strip()
    return out


def edit_detail(name):
    d = edit_dir(name)
    thumbs = files_in(d / "thumbnail and metadata", IMG_EXT)
    return {"name": d.name, "raw": files_in(d / "raw_video"), "user_assets": files_in(d / "user_assets"),
            "claude_assets": files_in(d / "claude_assets"), "final": files_in(d / "final_edit", VIDEO_EXT),
            "thumbnails": thumbs, "hasMetadata": meta_file(d).exists(), "hasPlan": (d / "edit_plan.md").exists(),
            "metadata": read_metadata(d)}


def list_edits():
    edits = []
    for d in sorted(EDITS.iterdir()):
        if d.is_dir():
            e = edit_detail(d.name)
            edits.append({k: (len(v) if isinstance(v, list) else v) for k, v in e.items() if k != "metadata"} |
                         {"title": e["metadata"]["title"], "finalFiles": e["final"]})
    style = STYLE_DIR / "style.md"
    imgs = [f for f in MASTER.rglob("*") if f.is_file() and f.suffix.lower() in IMG_EXT]
    indexed = md_index_rows(MASTER / "master_assets.md")
    return {"edits": edits,
            "style": {"exists": style.exists(), "videos": files_in(STYLE_DIR, VIDEO_EXT), "ffmpeg": bool(find_ffmpeg())},
            "assets": {"exists": (MASTER / "master_assets.md").exists(), "total": len(imgs),
                       "unindexed": sum(1 for f in imgs if f.relative_to(MASTER).as_posix() not in indexed)}}


def edit_create(b):
    return {"name": edit_dir(b.get("name"), create=True).name}


def edit_export(b):
    """Save a project into its edit folder: metadata.txt, script.md and the chosen thumbnail."""
    d = edit_dir(b.get("name"), create=True)
    write_metadata(d, b)
    script = "\n\n".join(f"## [{x.get('startLabel', '')}] {x['heading']}\n\n{x['narration']}" + (f"\n\n*Visuals:* {x['visuals']}" if x.get("visuals") else "")
                         for x in b.get("blocks", []))
    (d / "script.md").write_text(f"# {b.get('title', '')}\n\n{script}\n", encoding="utf-8")
    if b.get("thumbnail") and (THUMBS / Path(b["thumbnail"]).name).is_file():
        shutil.copy2(THUMBS / Path(b["thumbnail"]).name, d / "thumbnail and metadata" / "thumbnail.jpg")
    return {"name": d.name, "path": str(d)}


def md_text(which, edit=None):
    f = {"style": STYLE_DIR / "style.md", "assets": MASTER / "master_assets.md"}.get(which)
    if which == "plan":
        f = edit_dir(edit) / "edit_plan.md"
    if not f or not f.exists():
        raise ApiError("Not generated yet.", 404)
    return {"text": f.read_text(encoding="utf-8"), "path": str(f)}


# ---- master assets index (master_assets.md) ----
def md_index_rows(md):
    """{relative file: full row text}; rows are kept verbatim so hand edits to the md survive re-indexing."""
    rows = {}
    if md.exists():
        for line in md.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\|\s*`([^`]+)`\s*\|", line)
            if m:
                rows[m.group(1)] = line
    return rows


def esc_cell(s):
    return re.sub(r"\s+", " ", str(s or "")).replace("|", "/").strip()


def index_master_assets(job):
    md = MASTER / "master_assets.md"
    rows = md_index_rows(md)
    allf = sorted(f for f in MASTER.rglob("*") if f.is_file() and f.suffix.lower() != ".md")
    rel = {f.relative_to(MASTER).as_posix(): f for f in allf}
    new = [r for r in rel if r not in rows]
    imgs = [r for r in new if rel[r].suffix.lower() in IMG_EXT][:30]
    for r in new:
        if r not in imgs and rel[r].suffix.lower() not in IMG_EXT:
            rows[r] = f"| `{r}` | (not analyzed: {rel[r].suffix.lower().lstrip('.')} file) | | |"
    system = ("You catalogue a creator's reusable asset library so an editor can quickly pick what suits a video. For each image give a factual description "
              "(subject, setting, expression/mood, colors, orientation, background type, any text), what kinds of videos/scenes it suits, and usage notes "
              "(e.g. cut-out friendly, needs dark overlay, thumbnail-ready). Schema: {\"items\": [{\"file\": str, \"description\": str, \"best_for\": str, \"notes\": str}]}")
    for i in range(0, len(imgs), 6):
        batch = imgs[i:i + 6]
        job["msg"], job["progress"] = f"Describing images {i + 1}-{i + len(batch)} of {len(imgs)}…", int(i / max(len(imgs), 1) * 100)
        content = [{"type": "text", "text": "Describe these images, in order. File names:\n" + "\n".join(batch)}]
        for r in batch:
            content += [{"type": "text", "text": f"Image file name: {r}"}, {"type": "image", "path": str(rel[r])}]
        got = {x.get("file"): x for x in claude_json(system, content, 3000).get("items", [])}
        for r in batch:
            x = got.get(r, {})
            rows[r] = f"| `{r}` | {esc_cell(x.get('description'))} | {esc_cell(x.get('best_for'))} | {esc_cell(x.get('notes'))} |"
    kept = sorted(r for r in rows if r in rel)
    md.write_text("# Master assets\n\nReusable pictures and assets for any video. Hand-edit any row; existing rows are never overwritten when new files are indexed.\n"
                  "Drop new files in this folder and click **Index new assets**.\n\n| File | Description | Best for | Notes |\n|---|---|---|---|\n" +
                  "\n".join(rows[r] for r in kept) + "\n", encoding="utf-8")
    return {"indexed": len(imgs), "total": len(kept), "remaining": len([r for r in new if rel[r].suffix.lower() in IMG_EXT]) - len(imgs)}


# ---- style.md from style_reference videos ----
def run_quiet(cmd, timeout=600):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def find_ffmpeg():
    """System ffmpeg if present, else the binary bundled with the imageio-ffmpeg pip package."""
    if shutil.which("ffmpeg"):
        return shutil.which("ffmpeg")
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return None


def video_frames(video, out, n=10):
    ff = find_ffmpeg()
    if not ff:
        raise ApiError("ffmpeg isn't available. Run: python -m pip install imageio-ffmpeg  (then restart the app).")
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", run_quiet([ff, "-i", str(video)]).stderr)  # ffmpeg prints the duration when given no output
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0
    if dur <= 0:
        raise ApiError(f"Couldn't read {video.name}.")
    frames = []
    times = [t for t in (1, 6, 15, 30, 45) if t < dur * .5] + [dur * (i + .5) / n for i in range(n)]  # dense opening (the hook), then even spread
    for i, t in enumerate(times):
        f = out / f"{video.stem[:20]}_{i:02d}.jpg"
        run_quiet([ff, "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1", "-vf", "scale=640:-2", "-y", str(f)])
        if f.exists():
            frames.append((f, t))
    scan = min(dur, 300)  # scene-cut count over the first 5 minutes
    cuts = len(re.findall(r"pts_time:", run_quiet([ff, "-t", str(scan), "-i", str(video), "-vf", "select='gt(scene,0.3)',showinfo", "-an", "-f", "null", "-"]).stderr))
    return frames, dur, {"duration_sec": round(dur), "cuts_in_first_%ds" % scan: cuts, "avg_shot_length_sec": round(scan / (cuts + 1), 1)}


def build_style(job):
    vids = [f for f in sorted(STYLE_DIR.iterdir()) if f.is_file() and f.suffix.lower() in VIDEO_EXT]
    if not vids:
        raise ApiError("Put reference videos in data/style_reference first.")
    cache_f = STYLE_DIR / ".style_cache.json"
    cache = json.loads(cache_f.read_text(encoding="utf-8")) if cache_f.exists() else {}
    system = ("You are a video editor analysing a reference video from still frames (evenly spaced) plus cut statistics. You cannot hear audio, so do not claim anything about "
              "music or voice. Describe only what the frames and stats support. Schema: {\"pacing\": str, \"shot_types_and_framing\": str, \"on_screen_text_and_captions\": str, "
              "\"color_and_grade\": str, \"graphics_and_overlays\": str, \"b_roll_usage\": str, \"opening_structure\": str, \"signature_moves\": [str], \"uncertain\": str}")
    notes = {}
    for i, v in enumerate(vids):
        key = f"{v.name}|{v.stat().st_size}|{int(v.stat().st_mtime)}"
        job["msg"], job["progress"] = f"Analysing {v.name} ({i + 1}/{len(vids)})…", int(i / len(vids) * 85)
        if key not in cache:
            tmp = Path(tempfile.mkdtemp(prefix="style_", dir=CACHE_DIR))
            try:
                frames, dur, stats = video_frames(v, tmp)
                content = [{"type": "text", "text": f"Reference video: {v.name}\nStats: {json.dumps(stats)}\nFrames follow, with their timestamps."}]
                for f, t in frames:
                    content += [{"type": "text", "text": f"Frame at {ts(t)}"}, {"type": "image", "path": str(f)}]
                cache[key] = {**claude_json(system, content, 3000), "stats": stats}
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            cache_f.write_text(json.dumps(cache), encoding="utf-8")
        notes[v.name] = cache[key]
    job["msg"], job["progress"] = "Merging into style.md…", 90
    md = STYLE_DIR / "style.md"
    marker = "<!-- MANUAL NOTES BELOW: kept when style.md is regenerated -->"
    manual = md.read_text(encoding="utf-8").split(marker, 1)[1] if md.exists() and marker in md.read_text(encoding="utf-8") else "\n\n(Add your own style rules here.)\n"
    body = claude("You write a concise, actionable editing style guide in Markdown from per-video analyses. Find what the references share; call out differences separately. "
                  "Sections: Overview, Pacing & cuts (with numbers), Framing & shots, Text & captions, Color, Graphics & overlays, B-roll, Opening/hook structure, "
                  "Do / Don't checklist. Note clearly that audio/music was not analysed.", json.dumps(notes), 4000)
    md.write_text(f"# Editing style guide\n\n*Generated from: {', '.join(notes)}*\n\n{body.strip()}\n\n{marker}{manual}", encoding="utf-8")
    return {"videos": len(notes)}


# ---- per-video edit plan ----
def edit_plan(b):
    d = edit_dir(b.get("name"))
    e = edit_detail(d.name)
    script = (d / "script.md").read_text(encoding="utf-8") if (d / "script.md").exists() else ""
    if not script and not e["metadata"]["title"]:
        raise ApiError("No script/metadata in this folder yet. Use 'Save to edit folder' from the Next 3 videos tab, or add script.md.")
    style, assets = STYLE_DIR / "style.md", MASTER / "master_assets.md"
    payload = {"title": e["metadata"]["title"], "chapters": e["metadata"]["chapters"], "script_md": script[:14000],
               "style_guide": style.read_text(encoding="utf-8")[:8000] if style.exists() else "(none yet)",
               "master_assets_md": assets.read_text(encoding="utf-8")[:12000] if assets.exists() else "(none yet)",
               "files": {"raw_video": e["raw"], "user_assets": e["user_assets"], "claude_assets": e["claude_assets"]}}
    text = claude("You are a video editor's planner. Produce an edit plan in Markdown: (1) Overview; (2) a timeline table with columns Timestamp | Script beat | On screen | "
                  "Text/graphics | Notes (cuts, SFX, music cues); (3) 'Assets to use' listing exact file names; (4) 'Assets to generate' with a ready-to-use prompt each "
                  "(these go in claude_assets); (5) Style checklist drawn from the style guide. Use ONLY file names that appear in the provided lists or master_assets_md; "
                  "never invent files. Where nothing suitable exists, say so and add it to 'Assets to generate'.", json.dumps(payload), 6000)
    (d / "edit_plan.md").write_text(text.strip() + "\n", encoding="utf-8")
    return {"path": str(d / "edit_plan.md"), "text": text}


def clean_tags(tags):
    out, total = [], 0
    for t in tags:
        t = re.sub(r"[<>]", "", t).strip()[:100]
        cost = len(t) + 1 + (2 if " " in t else 0)
        if t and total + cost <= 480:
            out.append(t)
            total += cost
    return out


def start_publish(p):
    if p.get("confirm") is not True:
        raise ApiError("Confirmation required.")
    d = edit_dir(p.get("edit"))
    name = Path(p.get("file") or "").name
    path = d / "final_edit" / name
    if path.suffix.lower() not in VIDEO_EXT or not path.is_file():
        raise ApiError("Pick a video file from this edit folder's final_edit.")
    title = (p.get("title") or "").strip()[:100]
    if not title:
        raise ApiError("Title is required.")
    if p.get("privacy") not in ("private", "unlisted", "public"):
        raise ApiError("Pick a privacy setting.")
    if p.get("madeForKids") not in (True, False):
        raise ApiError("Declare whether the video is made for kids.")
    thumb = None
    if p.get("thumbnail"):
        thumb = d / "thumbnail and metadata" / Path(p["thumbnail"]).name
        if not thumb.is_file() or thumb.suffix.lower() not in IMG_EXT:
            raise ApiError("Selected thumbnail not found in 'thumbnail and metadata'.")
        if thumb.stat().st_size > 2_000_000:
            raise ApiError("YouTube thumbnails must be under 2 MB; shrink it or pick another.")
    token = google_token()
    meta = {"snippet": {"title": title, "description": re.sub(r"[<>]", "", p.get("description") or "")[:5000],
                        "tags": clean_tags(p.get("tags") or []), "categoryId": str(p.get("categoryId") or "22")},
            "status": {"privacyStatus": p["privacy"], "selfDeclaredMadeForKids": p["madeForKids"]}}
    if p.get("publishAt") and p["privacy"] == "private":
        meta["status"]["publishAt"] = p["publishAt"]
    job_id = secrets.token_hex(4)
    JOBS[job_id] = {"state": "starting", "progress": 0, "file": name, "title": title}
    threading.Thread(target=run_upload, args=(JOBS[job_id], token, path, meta, thumb), daemon=True).start()
    return {"job": job_id}


def run_upload(job, token, path, meta, thumb):
    try:
        size = path.stat().st_size
        if size == 0:
            raise ApiError("Video file is empty.")
        ctype = mimetypes.guess_type(path.name)[0] or "video/*"
        init = urllib.request.Request(
            "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
            data=json.dumps(meta).encode(), method="POST",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8",
                     "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": ctype})
        try:
            with urllib.request.urlopen(init, timeout=60) as r:
                loc = r.headers["Location"]
        except urllib.error.HTTPError as e:
            raise ApiError(f"YouTube rejected the upload request: {e.read().decode()[:300]}")
        quota_add("upload", 1600)
        u, pos, job["state"], result = urllib.parse.urlparse(loc), 0, "uploading", None
        with open(path, "rb") as f:
            while result is None:
                f.seek(pos)
                chunk = f.read(CHUNK)
                for attempt in range(5):
                    try:
                        conn = http.client.HTTPSConnection(u.netloc, timeout=300)
                        conn.request("PUT", u.path + "?" + u.query, body=chunk, headers={
                            "Content-Length": str(len(chunk)), "Content-Range": f"bytes {pos}-{pos + len(chunk) - 1}/{size}"})
                        r = conn.getresponse(); body = r.read(); conn.close()
                    except OSError:
                        time.sleep(2 ** attempt)
                        continue
                    if r.status in (200, 201):
                        result = json.loads(body)
                    elif r.status == 308:
                        rng = r.getheader("Range")
                        pos = int(rng.split("-")[1]) + 1 if rng else 0
                    elif r.status >= 500:
                        time.sleep(2 ** attempt)
                        continue
                    else:
                        raise ApiError(f"Upload failed ({r.status}): {body[:300].decode('utf-8', 'replace')}")
                    break
                else:
                    raise ApiError("Upload failed after retries (network).")
                job["progress"] = round(min(pos, size) / size * 100, 1)
        job.update(videoId=result["id"], url=f"https://youtu.be/{result['id']}", studio=f"https://studio.youtube.com/video/{result['id']}/edit", progress=100)
        if thumb:
            job["state"] = "thumbnail"
            try:
                req = urllib.request.Request(f"https://www.googleapis.com/upload/youtube/v3/thumbnails/set?videoId={result['id']}&uploadType=media",
                                             data=thumb.read_bytes(), method="POST",
                                             headers={"Authorization": f"Bearer {token}", "Content-Type": mimetypes.guess_type(thumb.name)[0] or "image/jpeg"})
                urllib.request.urlopen(req, timeout=120).read()
                quota_add("thumbnail", 50)
            except urllib.error.HTTPError as e:
                job["thumbnailError"] = e.read().decode()[:300]
        job["state"] = "done"
    except ApiError as e:
        job.update(state="error", error=str(e))
    except Exception as e:  # noqa
        job.update(state="error", error=f"{type(e).__name__}: {e}")


# ---------- server ----------
SETTINGS_KEYS = ("YOUTUBE_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "YT_QUOTA_LIMIT")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def send(self, code, body, ctype="application/json", headers=None):
        b = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def redirect(self, url):
        self.send(302, b"", "text/plain", {"Location": url})

    def guard(self, post=False):
        """Local-only app that can spend API credits and upload videos: refuse DNS-rebinding and cross-site requests."""
        host = self.headers.get("Host", "")
        if host.split(":")[0] not in ("127.0.0.1", "localhost"):
            self.send(403, {"error": "forbidden host"})
            return False
        origin = self.headers.get("Origin")
        if post and origin and origin != f"http://{host}":
            self.send(403, {"error": "cross-origin request blocked"})
            return False
        return True

    def pick_channel(self, q=None):
        CTX.ch = (q or {}).get("ch") or self.headers.get("X-Channel")
        ensure_channel(cur_id())

    def route(self, fn):
        try:
            self.send(200, fn())
        except ApiError as e:
            self.send(e.status, {"error": str(e)})
        except Exception as e:  # noqa
            self.send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_GET(self):
        if not self.guard():
            return
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        self.pick_channel(q)
        redirect_uri = f"http://{self.headers.get('Host')}/oauth/callback"
        if u.path == "/":
            return self.send(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        if u.path.startswith("/thumbs/"):
            f = THUMBS / Path(u.path).name
            return self.send(200, f.read_bytes(), mimetypes.guess_type(f.name)[0] or "image/jpeg") if f.is_file() else self.send(404, {"error": "not found"})
        if u.path == "/edit-file":  # thumbnail previews from an edit folder
            try:
                f = edit_dir(q.get("name")) / "thumbnail and metadata" / Path(q.get("file", "")).name
            except ApiError as e:
                return self.send(e.status, {"error": str(e)})
            ok = f.is_file() and f.suffix.lower() in IMG_EXT
            return self.send(200, f.read_bytes(), mimetypes.guess_type(f.name)[0] or "image/jpeg") if ok else self.send(404, {"error": "not found"})
        if u.path == "/api/ytthumb":  # proxy so any YouTube thumbnail can be downloaded
            vid = q.get("id", "")
            if not re.fullmatch(r"[\w-]{11}", vid):
                return self.send(400, {"error": "bad id"})
            for res in ("maxresdefault", "hqdefault"):
                try:
                    with urllib.request.urlopen(f"https://i.ytimg.com/vi/{vid}/{res}.jpg", timeout=20) as r:
                        return self.send(200, r.read(), "image/jpeg", {"Content-Disposition": f'attachment; filename="thumbnail_{vid}.jpg"'})
                except urllib.error.URLError:
                    continue
            return self.send(404, {"error": "thumbnail not found"})
        if u.path == "/oauth/start":
            try:
                return self.redirect(oauth_url(redirect_uri))
            except ApiError as e:
                return self.send(400, f"<p>{e}</p>".encode(), "text/html")
        if u.path == "/oauth/callback":
            try:
                if "code" not in q:
                    raise ApiError(f"Google returned: {q.get('error', 'no code')}")
                oauth_callback(q["code"], q.get("state"), redirect_uri)
                return self.redirect("/#publish")
            except ApiError as e:
                return self.send(400, f"<p>Connection failed: {e}</p><a href='/'>Back</a>".encode(), "text/html")
        if u.path == "/api/config":
            e = load_env()
            return self.send(200, {"youtube": bool(e.get("YOUTUBE_API_KEY")), "anthropic": claude_mode() is not None, "claudeMode": claude_mode(),
                                   "openai": bool(e.get("OPENAI_API_KEY")), "googleClient": bool(e.get("GOOGLE_CLIENT_ID") and e.get("GOOGLE_CLIENT_SECRET")),
                                   "googleConnected": TOKENS.exists(), "channel": active_channel()["handle"], "niche": active_channel()["niche"], "channelName": active_channel()["name"],
                                   "channels": load_channels(), "activeChannel": cur_id(), "quotaLimit": quota_limit()})
        routes = {
            "/api/channel": lambda: channel_analysis(q.get("handle"), int(q.get("count", 50))),
            "/api/trends": lambda: trends(q.get("q", ""), q.get("days", 7), q.get("region", "US"), q.get("max", 30), q.get("order", "viewCount")),
            "/api/video": lambda: video_detail(q.get("id", "")),
            "/api/projects": project_list,
            "/api/project": lambda: project_load(q.get("id")),
            "/api/channels": channels_info,
            "/api/quota": quota_info,
            "/api/workspace": list_edits,
            "/api/edit": lambda: edit_detail(q.get("name")),
            "/api/md": lambda: md_text(q.get("which"), q.get("edit")),
            "/api/google-channel": lambda: {"channel": connected_channel()},
            "/api/publish/status": lambda: job_status(q.get("job")),
        }
        if u.path in routes:
            return self.route(routes[u.path])
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if not self.guard(post=True):
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        self.pick_channel()
        if self.path == "/api/settings":
            save_env({k: body[k].strip() for k in SETTINGS_KEYS if body.get(k, "").strip()})
            return self.send(200, {"ok": True})
        routes = {"/api/script": make_script, "/api/next-videos": next_videos, "/api/package": build_package,
                  "/api/thumbnail": gen_thumbnail, "/api/project": project_save, "/api/publish": start_publish,
                  "/api/channels": upsert_channel, "/api/channels/delete": delete_channel,
                  "/api/edit/create": edit_create, "/api/edit/export": edit_export, "/api/edit/plan": edit_plan,
                  "/api/style/build": lambda b: start_job("Building style.md", build_style),
                  "/api/assets/index": lambda b: start_job("Indexing master assets", index_master_assets)}
        if self.path in routes:
            return self.route(lambda: routes[self.path](body))
        self.send(404, {"error": "not found"})


if __name__ == "__main__":
    migrate_legacy()
    if "--init" in sys.argv:  # used by setup.bat: create the channel list and folder skeleton, then exit
        for c in load_channels():
            print(f"channel '{c['name']}': {DATA / 'channels' / c['id']}")
        sys.exit(0)
    port = int(os.environ.get("PORT", 8791))
    print(f"YouTube research utility -> http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
