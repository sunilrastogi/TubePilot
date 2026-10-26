# Copyright 2026 Sunil Rastogi
# SPDX-License-Identifier: Apache-2.0
"""TubePilot command-line video editing helper (stdlib only; needs ffmpeg + ffprobe on PATH).

  python edit.py analyze <video_folder>   transcribe (Whisper API) and propose cuts for pauses
  python edit.py add-cut <video_folder> START END "reason"   add a cut (repetition / fumble) to cuts.json
  python edit.py cut <video_folder>       apply approved cuts -> claude_assets/rough_cut.mp4
  python edit.py render <video_folder>    add user_assets overlays -> final_edit/final.mp4

Add  --channel <id>  anywhere to work on a channel other than the first one (ids are in data/channels.json).

<video_folder> is a folder name under data/edits (e.g. sample_video).
Overlay files in user_assets are named  overlay_MM_SS[_LEN].ext  (e.g. overlay_05_25_20.png =
full-frame at 5:25 for 20s; LEN optional: images default to 3s, videos play to their end).
Overlay times refer to the CUT video (rough_cut.mp4), not the raw one.
Main-video audio keeps playing under overlays.
"""
import json, mimetypes, os, re, secrets, shutil, subprocess, sys, urllib.request
from pathlib import Path

ROOT = Path(__file__).parent


def channel_edits(cid=None):
    """data/channels/<id>/edits for the given channel id (default: the first channel in data/channels.json)."""
    f = ROOT / "data" / "channels.json"
    ids = [c["id"] for c in json.loads(f.read_text(encoding="utf-8"))] if f.exists() else []
    if cid and ids and cid not in ids:
        sys.exit(f"Unknown channel '{cid}'. Known: {', '.join(ids)}")
    return ROOT / "data" / "channels" / (cid or (ids[0] if ids else "main")) / "edits"


EDITS = channel_edits()
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".wmv"}
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}
MIN_PAUSE = 0.8      # gaps between words longer than this are proposed as cuts
KEEP_GAP = 0.4       # natural pause left in place of a cut pause
CHUNK_SEC = 600      # Whisper upload chunk length (API limit is 25 MB per file)


def die(msg):
    sys.exit(f"error: {msg}")


def env_key():
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("OPENAI_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"')
    die("OPENAI_API_KEY missing from .env")


def need_ffmpeg():
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        die("ffmpeg not found. Run: winget install Gyan.FFmpeg  (then restart the terminal)")


def folder(name):
    d = EDITS / name
    if not d.is_dir():
        die(f"{d} does not exist")
    return d


def raw_video(d):
    vids = [f for f in (d / "raw_video").iterdir() if f.suffix.lower() in VIDEO_EXT]
    if len(vids) != 1:
        die(f"expected exactly one video in {d / 'raw_video'}, found {len(vids)}")
    return vids[0]


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode:
        die(f"{cmd[0]} failed:\n{r.stderr[-1500:]}")
    return r.stdout


def duration(path):
    return float(run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)]))


def fmt(t):
    return f"{int(t // 60):02d}:{t % 60:05.2f}"


# ---------- Whisper ----------
def whisper(audio, key):
    b = secrets.token_hex(8)
    fields = [("model", "whisper-1"), ("response_format", "verbose_json"), ("timestamp_granularities[]", "word")]
    body = b"".join(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields)
    body += (f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{audio.name}"\r\n'
             f'Content-Type: audio/mpeg\r\n\r\n').encode() + audio.read_bytes() + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request("https://api.openai.com/v1/audio/transcriptions", body,
                                 {"Authorization": f"Bearer {key}", "Content-Type": f"multipart/form-data; boundary={b}"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.load(r)


def cmd_analyze(name):
    need_ffmpeg()
    d, key = folder(name), env_key()
    video, out = raw_video(d), d / "claude_assets"
    tmp = out / "_audio"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    print("extracting audio…")
    run(["ffmpeg", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k",
         "-f", "segment", "-segment_time", str(CHUNK_SEC), str(tmp / "c%03d.mp3")])
    words = []
    for i, chunk in enumerate(sorted(tmp.glob("c*.mp3"))):
        print(f"transcribing chunk {i + 1}…")
        off = i * CHUNK_SEC
        for w in whisper(chunk, key).get("words", []):
            words.append({"w": w["word"], "s": round(w["start"] + off, 3), "e": round(w["end"] + off, 3)})
    shutil.rmtree(tmp, ignore_errors=True)
    (out / "transcript.json").write_text(json.dumps(words, ensure_ascii=False), encoding="utf-8")

    # human-readable transcript: new line at every gap > 0.4s, so repetitions/fumbles are easy to spot
    lines, cur = [], []
    for w in words:
        if cur and w["s"] - cur[-1]["e"] > 0.4:
            lines.append(cur)
            cur = []
        cur.append(w)
    if cur:
        lines.append(cur)
    (out / "transcript.txt").write_text(
        "\n".join(f"[{fmt(l[0]['s'])}-{fmt(l[-1]['e'])}] " + " ".join(x["w"].strip() for x in l) for l in lines),
        encoding="utf-8")

    cuts = []
    for a, b in zip(words, words[1:]):
        if b["s"] - a["e"] > MIN_PAUSE:
            pad = KEEP_GAP / 2
            cuts.append({"start": round(a["e"] + pad, 3), "end": round(b["s"] - pad, 3),
                         "reason": f"pause {b['s'] - a['e']:.1f}s", "text": f"…{a['w'].strip()} | {b['w'].strip()}…",
                         "approved": False})
    (out / "cuts.json").write_text(json.dumps(cuts, indent=1, ensure_ascii=False), encoding="utf-8")
    write_cut_plan(out, cuts, duration(video))
    print(f"{len(words)} words, {len(cuts)} pause cuts proposed.\nReview {out / 'transcript.txt'} for repetitions/fumbles, "
          f"then approve cuts in {out / 'cut_plan.md'} / cuts.json.")


def write_cut_plan(out, cuts, total):
    saved = sum(c["end"] - c["start"] for c in cuts)
    rows = [f"# Cut plan\n\nRaw length {fmt(total)}; cuts remove {saved:.1f}s. Set `\"approved\": true` in cuts.json (or tell Claude).\n",
            "| # | Start | End | Len | Reason | Context |", "|---|---|---|---|---|---|"]
    for i, c in enumerate(cuts):
        rows.append(f"| {i} | {fmt(c['start'])} | {fmt(c['end'])} | {c['end'] - c['start']:.1f}s | {c['reason']} | {c['text']} |")
    (out / "cut_plan.md").write_text("\n".join(rows), encoding="utf-8")


def cmd_add_cut(name, start, end, reason):
    out = folder(name) / "claude_assets"
    f = out / "cuts.json"
    cuts = json.loads(f.read_text(encoding="utf-8"))
    words = json.loads((out / "transcript.json").read_text(encoding="utf-8"))
    s, e = float(start), float(end)
    text = " ".join(w["w"].strip() for w in words if s - 0.05 <= w["s"] and w["e"] <= e + 0.05)
    cuts.append({"start": s, "end": e, "reason": reason, "text": text, "approved": False})
    cuts.sort(key=lambda c: c["start"])
    f.write_text(json.dumps(cuts, indent=1, ensure_ascii=False), encoding="utf-8")
    write_cut_plan(out, cuts, duration(raw_video(folder(name))))


# ---------- apply cuts ----------
def keep_segments(cuts, total):
    segs, pos = [], 0.0
    for c in sorted((c for c in cuts if c.get("approved")), key=lambda c: c["start"]):
        if c["start"] > pos:
            segs.append((pos, c["start"]))
        pos = max(pos, c["end"])
    if pos < total:
        segs.append((pos, total))
    return segs


def cmd_cut(name):
    need_ffmpeg()
    d = folder(name)
    video, out = raw_video(d), d / "claude_assets"
    cuts = json.loads((out / "cuts.json").read_text(encoding="utf-8"))
    segs = keep_segments(cuts, duration(video))
    if not any(c.get("approved") for c in cuts):
        die("no approved cuts in cuts.json")
    parts = [f"[0:v]trim={s:.3f}:{e:.3f},setpts=PTS-STARTPTS[v{i}];[0:a]atrim={s:.3f}:{e:.3f},asetpts=PTS-STARTPTS,"
             f"afade=t=in:d=0.01,afade=t=out:st={max(e - s - 0.01, 0):.3f}:d=0.01[a{i}]" for i, (s, e) in enumerate(segs)]
    cat = "".join(f"[v{i}][a{i}]" for i in range(len(segs))) + f"concat=n={len(segs)}:v=1:a=1[v][a]"
    script = out / "_cut_filter.txt"
    script.write_text(";".join(parts + [cat]), encoding="utf-8")
    dest = out / "rough_cut.mp4"
    print(f"cutting {len(segs)} segments…")
    run(["ffmpeg", "-y", "-i", str(video), "-filter_complex_script", str(script), "-map", "[v]", "-map", "[a]",
         "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-c:a", "aac", "-b:a", "192k", str(dest)])
    script.unlink()
    print(f"wrote {dest} ({fmt(duration(dest))})")


# ---------- overlays ----------
OVERLAY_RE = re.compile(r"^overlay_(\d+)_(\d+)(?:_(\d+))?$", re.I)


def find_overlays(d):
    res = []
    for f in sorted((d / "user_assets").iterdir()):
        m = OVERLAY_RE.match(f.stem)
        if m and f.suffix.lower() in VIDEO_EXT | IMG_EXT:
            start = int(m[1]) * 60 + int(m[2])
            length = int(m[3]) if m[3] else (3 if f.suffix.lower() in IMG_EXT else duration(f))
            res.append((f, start, length))
    return res


def cmd_render(name):
    need_ffmpeg()
    d = folder(name)
    base = d / "claude_assets" / "rough_cut.mp4"
    if not base.exists():
        die("run `cut` first")
    ovs = find_overlays(d)
    dest = d / "final_edit" / "final.mp4"
    if not ovs:
        shutil.copy(base, dest)
        return print(f"no overlays found; copied rough cut to {dest}")
    w, h = map(int, run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                         "-of", "csv=s=x:p=0", str(base)]).strip().split("x"))
    cmd, filt, last = ["ffmpeg", "-y", "-i", str(base)], [], "[0:v]"
    for i, (f, s, ln) in enumerate(ovs, 1):
        if f.suffix.lower() in IMG_EXT:
            cmd += ["-loop", "1", "-t", f"{ln}", "-i", str(f)]
        else:
            cmd += ["-t", f"{ln}", "-i", str(f)]
        filt.append(f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
                    f"setsar=1,format=yuva420p,setpts=PTS-STARTPTS+{s}/TB,fade=t=in:st={s}:d=0.25:alpha=1,"
                    f"fade=t=out:st={s + ln - 0.25}:d=0.25:alpha=1[o{i}]")
        filt.append(f"{last}[o{i}]overlay=eof_action=pass:enable='between(t,{s},{s + ln})'[v{i}]")
        last = f"[v{i}]"
    cmd += ["-filter_complex", ";".join(filt), "-map", last, "-map", "0:a", "-c:v", "libx264", "-crf", "18",
            "-c:a", "copy", str(dest)]
    print("overlays:", *[f"{f.name} @ {fmt(s)} for {ln:.0f}s" for f, s, ln in ovs], sep="\n  ")
    run(cmd)
    print(f"wrote {dest}")


if __name__ == "__main__":
    a = sys.argv[1:]
    if "--channel" in a:
        i = a.index("--channel")
        EDITS = channel_edits(a[i + 1] if i + 1 < len(a) else None)
        del a[i:i + 2]
    try:
        {"analyze": lambda: cmd_analyze(a[1]), "cut": lambda: cmd_cut(a[1]), "render": lambda: cmd_render(a[1]),
         "add-cut": lambda: cmd_add_cut(*a[1:5])}[a[0]]()
    except (IndexError, KeyError):
        print(__doc__)
