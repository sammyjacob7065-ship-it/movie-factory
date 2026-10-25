#!/usr/bin/env python3
"""Clip Engine: long video -> one finished clip (cut, reframe, captions, commentary).
Runs on GitHub Actions. n8n sends the order (JOB_JSON) and polls clips/<jobId>.json in Supabase."""
import os, sys, json, re, subprocess, shutil, tempfile, time, glob
import requests

SB_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
SB_KEY = os.environ.get("SUPABASE_SERVICE_KEY") or ""
BUCKET = os.environ.get("SUPABASE_BUCKET") or "betrayer-assets"
GROQ_KEY = os.environ.get("GROQ_API_KEY") or ""
GROQ_LLM = "openai/gpt-oss-120b"
GROQ_STT = "whisper-large-v3-turbo"
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]
YELLOW = (255, 212, 0, 255)
WHITE = (255, 255, 255, 255)
BLACK = (0, 0, 0, 255)

JOB = {}
JOB_ID = "test"


# ---------------------------------------------------------------- helpers
def sh(cmd, check=True, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if check and r.returncode != 0:
        tail = (r.stderr or r.stdout or "")[-1500:]
        raise RuntimeError(f"{cmd[0]} failed: {tail}")
    return r


def sb_put(path, data, ctype):
    r = requests.post(
        f"{SB_URL}/storage/v1/object/{BUCKET}/{path}",
        headers={"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}",
                 "Content-Type": ctype, "x-upsert": "true"},
        data=data, timeout=(30, 900))
    if r.status_code == 413 or "maximum allowed size" in r.text:
        raise RuntimeError("Supabase refused the file: it is bigger than the storage size limit (Free plan = 50 MB).")
    r.raise_for_status()
    return f"{SB_URL}/storage/v1/object/public/{BUCKET}/{path}"


def status(stage, msg="", state="running", **extra):
    body = {"status": state, "stage": stage, "message": msg, "ts": int(time.time())}
    body.update(extra)
    print(f"[{state}] {stage}: {msg}", flush=True)
    if not SB_URL or not SB_KEY:
        return
    try:
        sb_put(f"clips/{JOB_ID}.json", json.dumps(body).encode(), "application/json")
    except Exception as e:  # never let a status write kill the job
        print("::warning::status write failed:", e)


def fmt_t(s):
    s = int(s)
    return f"{s // 60}:{s % 60:02d}"


def groq_chat(system, user, max_tokens=1500):
    last = None
    for i in range(4):
        r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                          headers={"Authorization": "Bearer " + GROQ_KEY, "Content-Type": "application/json"},
                          json={"model": GROQ_LLM, "reasoning_effort": "low", "temperature": 0.6,
                                "max_tokens": max_tokens,
                                "messages": [{"role": "system", "content": system},
                                             {"role": "user", "content": user}]},
                          timeout=120)
        if r.status_code == 200:
            raw = r.json()["choices"][0]["message"]["content"] or ""
            a, b = raw.find("{"), raw.rfind("}")
            if a < 0 or b < 0:
                last = "no JSON in answer"
                continue
            try:
                return json.loads(raw[a:b + 1])
            except Exception as e:
                last = str(e)
                continue
        last = f"{r.status_code} {r.text[:200]}"
        time.sleep(8 * (i + 1) if r.status_code == 429 else 3)
    raise RuntimeError("Groq did not answer: " + str(last))


def groq_whisper(path):
    """returns (segments[{start,end,text}], words[{word,start,end}])"""
    last = None
    for i in range(4):
        with open(path, "rb") as f:
            r = requests.post("https://api.groq.com/openai/v1/audio/transcriptions",
                              headers={"Authorization": "Bearer " + GROQ_KEY},
                              data={"model": GROQ_STT, "response_format": "verbose_json",
                                    "timestamp_granularities[]": ["word", "segment"]},
                              files={"file": (os.path.basename(path), f, "audio/mpeg")}, timeout=300)
        if r.status_code == 200:
            j = r.json()
            segs = [{"start": s["start"], "end": s["end"], "text": s["text"].strip()} for s in j.get("segments", [])]
            words = [{"word": w["word"].strip(), "start": w["start"], "end": w["end"]} for w in j.get("words", [])]
            return segs, words
        last = f"{r.status_code} {r.text[:200]}"
        time.sleep(10 * (i + 1) if r.status_code == 429 else 4)
    raise RuntimeError("Groq speech-to-text failed: " + str(last))


# ---------------------------------------------------------------- source
def is_direct(url):
    return bool(re.search(r"\.(mp4|mov|mkv|webm|m4v|mp3|wav|m4a)(\?|$)", url, re.I))


def ytdlp(args, tmp):
    base = ["yt-dlp", "--no-playlist", "--retries", "5", "--no-warnings"]
    ck = os.environ.get("YT_COOKIES") or ""
    if ck.strip():
        cf = os.path.join(tmp, "cookies.txt")
        open(cf, "w").write(ck)
        base += ["--cookies", cf]
    r = sh(base + ["--js-runtimes", "node"] + args, check=False)
    if r.returncode != 0 and "no such option" in (r.stderr or "").lower():
        r = sh(base + args, check=False)
    if r.returncode != 0:
        err = (r.stderr or "")[-900:]
        if re.search(r"sign in|bot|cookies", err, re.I):
            raise RuntimeError("YouTube blocked the download from GitHub (it asked for a sign-in). "
                               "Add the YT_COOKIES secret, or use a direct video file link. Detail: " + err[-300:])
        raise RuntimeError("yt-dlp failed: " + err)
    return r


def source_info(url, tmp):
    if is_direct(url):
        r = sh(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", url])
        return {"duration": float(r.stdout.strip() or 0), "title": os.path.basename(url.split("?")[0])}
    r = ytdlp(["-J", "--skip-download", url], tmp)
    j = json.loads(r.stdout)
    return {"duration": float(j.get("duration") or 0), "title": j.get("title") or "", "channel": j.get("uploader") or ""}


def subs_transcript(url, tmp, limit_s):
    """YouTube's own captions (free, with timings). Returns segments or []"""
    try:
        ytdlp(["--skip-download", "--write-subs", "--write-auto-subs", "--sub-langs", "en.*,en",
               "--sub-format", "json3", "-o", os.path.join(tmp, "subs.%(ext)s"), url], tmp)
    except Exception as e:
        print("no captions:", str(e)[:200])
        return []
    fs = glob.glob(os.path.join(tmp, "subs*.json3"))
    if not fs:
        return []
    segs = []
    for ev in json.load(open(fs[0])).get("events", []):
        if "segs" not in ev:
            continue
        t = "".join(s.get("utf8", "") for s in ev["segs"]).replace("\n", " ").strip()
        if not t:
            continue
        st = ev.get("tStartMs", 0) / 1000
        if st > limit_s:
            break
        segs.append({"start": st, "end": st + ev.get("dDurationMs", 2000) / 1000, "text": t})
    return segs


def audio_transcript(url, tmp, limit_s):
    """fallback: download audio of the first limit_s seconds and use Groq Whisper"""
    full = os.path.join(tmp, "scan_src.m4a")
    if is_direct(url):
        sh(["ffmpeg", "-y", "-v", "error", "-t", str(limit_s), "-i", url, "-vn", "-ac", "1", "-ar", "16000",
            "-b:a", "32k", os.path.join(tmp, "scan.mp3")])
    else:
        ytdlp(["-f", "worstaudio/bestaudio", "--download-sections", f"*0-{int(limit_s)}",
               "-o", full.replace(".m4a", ".%(ext)s"), url], tmp)
        src = [p for p in glob.glob(os.path.join(tmp, "scan_src.*"))][0]
        sh(["ffmpeg", "-y", "-v", "error", "-i", src, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k",
            os.path.join(tmp, "scan.mp3")])
    pat = os.path.join(tmp, "scan_%03d.mp3")
    sh(["ffmpeg", "-y", "-v", "error", "-i", os.path.join(tmp, "scan.mp3"), "-f", "segment", "-segment_time", "600",
        "-c", "copy", pat])
    segs = []
    for i, p in enumerate(sorted(glob.glob(os.path.join(tmp, "scan_0*.mp3")))):
        s, _ = groq_whisper(p)
        for x in s:
            segs.append({"start": x["start"] + i * 600, "end": x["end"] + i * 600, "text": x["text"]})
    return segs


def transcript_lines(segs, max_chars=26000):
    """merge into ~12 second lines like [12:05] text"""
    lines, cur, cs = [], [], None
    for s in segs:
        if cs is None:
            cs = s["start"]
        cur.append(s["text"])
        if s["end"] - cs >= 12:
            lines.append((cs, " ".join(cur)))
            cur, cs = [], None
    if cur:
        lines.append((cs, " ".join(cur)))
    out, total, last_t = [], 0, 0
    for t, x in lines:
        row = f"[{fmt_t(t)}] {x}"
        if total + len(row) > max_chars:
            break
        out.append(row)
        total += len(row) + 1
        last_t = t
    return "\n".join(out), last_t



# ---------------------------------------------------------------- find a long video by itself
def sb_get_json(path):
    try:
        r = requests.get(f"{SB_URL}/storage/v1/object/{BUCKET}/{path}", headers={"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"}, timeout=30)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def discover(topics, cc_only, tmp):
    """search YouTube for long videos on the topics, return (url, title) of one not used before"""
    import random
    from urllib.parse import quote
    used = (sb_get_json("clips/used.json") or {}).get("ids", [])
    topics = list(topics) or ["motivation speech"]
    random.shuffle(topics)
    for topic in topics:
        if cc_only:
            q = "https://www.youtube.com/results?search_query=" + quote(topic) + "&sp=EgIwAQ%253D%253D"
        else:
            q = "ytsearch30:" + topic
        try:
            r = ytdlp(["--flat-playlist", "-J", "--playlist-end", "30", q], tmp)
            ents = json.loads(r.stdout).get("entries") or []
        except Exception as e:
            print("search failed for", topic, str(e)[:200])
            continue
        good = [x for x in ents if x and x.get("id") and x["id"] not in used and 600 <= float(x.get("duration") or 0) <= 7200]
        if good:
            v = random.choice(good[:15])
            used.append(v["id"])
            try:
                sb_put("clips/used.json", json.dumps({"ids": used[-500:]}).encode(), "application/json")
            except Exception as e:
                print("::warning::could not save used list", e)
            return "https://www.youtube.com/watch?v=" + v["id"], v.get("title") or ""
    raise RuntimeError("found no new long video for these topics. Add more topics or turn off 'Creative Commons only'.")

# ---------------------------------------------------------------- pick the moment
def pick_moment(segs, hint, short):
    lo, hi = (50, 60) if short else (60, 90)
    text, covered = transcript_lines(segs)
    system = (
        "You choose the single best clip from a video transcript for a YouTube channel. Timestamps are [m:ss]. "
        f"The clip must last between {lo} and {hi} seconds, start at the beginning of a sentence, end at the end of a "
        "sentence, make sense on its own without the rest of the video, and open with a strong hook "
        "(a surprising claim, strong emotion, a funny moment, or a story with a payoff). "
        'Reply JSON only: {"start": seconds, "end": seconds, "title": "short title", "reason": "one line"}. '
        "start and end are plain numbers of seconds from the start of the video."
    )
    user = (("What the owner wants: " + hint + "\n\n") if hint else "") + "TRANSCRIPT:\n" + text
    o = groq_chat(system, user)
    s, e = float(o["start"]), float(o["end"])
    # snap to sentence boundaries
    starts = [x["start"] for x in segs]
    ends = [x["end"] for x in segs]
    s2 = max([x for x in starts if x <= s + 1.0] or [s])
    e2 = min([x for x in ends if x >= e - 1.0] or [e])
    s, e = (s2, e2) if e2 > s2 else (s, e)
    if e - s > hi:
        e = s + hi
    if e - s < lo:
        e = s + lo
    return s, e, o.get("title") or "", o.get("reason") or ""


# ---------------------------------------------------------------- cut
def cut_section(url, s, e, tmp, total):
    pad0 = max(0.0, s - 2.0)
    pad1 = e + 1.0
    if total:
        pad1 = min(pad1, total)
    out = os.path.join(tmp, "src.mp4")
    if is_direct(url):
        sh(["ffmpeg", "-y", "-v", "error", "-ss", str(pad0), "-to", str(pad1), "-i", url,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac", out])
    else:
        ytdlp(["-f", "bv*[height<=1080]+ba/b[height<=1080]/b", "--download-sections", f"*{pad0:.2f}-{pad1:.2f}",
               "--force-keyframes-at-cuts", "--merge-output-format", "mp4",
               "-o", os.path.join(tmp, "src.%(ext)s"), url], tmp)
        if not os.path.exists(out):
            c = [p for p in glob.glob(os.path.join(tmp, "src.*")) if not p.endswith(".part")]
            if not c:
                raise RuntimeError("download gave no video file")
            out = c[0]
    return out, s - pad0


def probe_dims(path):
    r = sh(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
            "-of", "csv=p=0", path])
    w, h = r.stdout.strip().split(",")[:2]
    return int(w), int(h)


# ---------------------------------------------------------------- commentary
def write_commentary(clip_text, source_title, url, short):
    system = (
        "You write voice-over commentary for a clip on a YouTube channel. Use very simple English, short sentences, "
        "a warm friendly voice, no big words. Reply JSON only: "
        '{"hook":"one punchy opening line, max 14 words, makes people stay, never reveals the ending",'
        '"closing":"one closing line, max 16 words: a quick reaction plus a question that makes people comment",'
        '"title":"clickable YouTube title under 85 characters, no angle brackets",'
        '"description":"2 short sentences about the clip, no hashtags"}. '
        "Use only facts from the clip text."
    )
    o = groq_chat(system, json.dumps({"video": source_title, "clip_text": clip_text[:6000]}))
    strip = lambda t: re.sub(r"\s+", " ", re.sub(r"[<>]", "", str(t or ""))).strip()
    title = strip(o.get("title"))[:90] or strip(source_title)[:90]
    desc = strip(o.get("description"))
    desc += f"\n\nOriginal video: {url}"
    if short:
        desc += "\n#Shorts"
    return strip(o.get("hook")), strip(o.get("closing")), title, desc


def tts(text, voice, tts_url, path):
    last = None
    for i in range(4):
        try:
            r = requests.post(tts_url.rstrip("/") + "/v1/audio/speech",
                              headers={"Content-Type": "application/json"},
                              json={"input": text, "voice": voice, "response_format": "mp3", "speed": 1.1},
                              timeout=150)
            if r.status_code == 200 and len(r.content) > 1000:
                open(path, "wb").write(r.content)
                return
            last = f"{r.status_code} {r.text[:150]}"
        except Exception as e:
            last = str(e)
        time.sleep(10)
    raise RuntimeError("voice service failed: " + str(last))


def audio_len(path):
    r = sh(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path])
    return float(r.stdout.strip())


# ---------------------------------------------------------------- captions (PNG frames)
def find_font():
    for f in FONT_CANDIDATES:
        if os.path.exists(f):
            return f
    r = sh(["fc-match", "-f", "%{file}", "sans:bold"], check=False)
    return r.stdout.strip()


def group_words(words, per=3, gap=0.6):
    """split into groups of up to `per` words, also break on long pauses"""
    groups, cur = [], []
    for w in words:
        if cur and (len(cur) >= per or w["start"] - cur[-1]["end"] > gap):
            groups.append(cur)
            cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return groups


def render_caption_png(group, active, W, size, font_path, path, box_h_pad=0.22, max_w=0.86):
    from PIL import Image, ImageDraw, ImageFont
    words = [w["word"].upper() for w in group]
    while True:
        font = ImageFont.truetype(font_path, size)
        space = font.getlength(" ")
        widths = [font.getlength(t) for t in words]
        total = sum(widths) + space * (len(words) - 1)
        if total <= W * max_w or size <= 30:
            break
        size -= 4
    asc, desc = font.getmetrics()
    cap_top = font.getbbox("H")[1]  # distance from top of line to top of capitals
    cap_bot = font.getbbox("H")[3]
    cap_h = cap_bot - cap_top
    pad = int(size * box_h_pad)
    H = cap_h + pad * 2 + 24
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    x = (W - total) / 2
    for i, t in enumerate(words):
        wd = widths[i]
        if i == active:
            d.rounded_rectangle([x - pad * 0.7, 12, x + wd + pad * 0.7, 12 + cap_h + pad * 2], radius=int(size * 0.18),
                                fill=YELLOW)
        ty = 12 + pad - cap_top
        d.text((x, ty), t, font=font, fill=WHITE, stroke_width=max(3, size // 14), stroke_fill=BLACK)
        x += wd + space
    im.save(path)
    return H


def build_caption_track(words, dur, W, size, font_path, tmp, windows_off=None):
    """words relative to clip start. Returns (concat list path, height). windows_off: list of (a,b) times with no captions."""
    from PIL import Image
    cdir = os.path.join(tmp, "caps")
    os.makedirs(cdir, exist_ok=True)
    events = []  # (start, end, png)
    n = 0
    H = 0
    groups = group_words(words)
    for gi, g in enumerate(groups):
        for wi, w in enumerate(g):
            st = w["start"]
            if wi + 1 < len(g):
                en = g[wi + 1]["start"]
            elif gi + 1 < len(groups):
                en = min(groups[gi + 1][0]["start"], w["end"] + 0.35)
            else:
                en = w["end"] + 0.35
            en = max(en, st + 0.08)
            p = os.path.join(cdir, f"c{n:04d}.png")
            H = render_caption_png(g, wi, W, size, font_path, p)
            events.append((st, en, p))
            n += 1
    blank = os.path.join(cdir, "blank.png")
    # PIL image height must match all frames: use max height for the blank
    Image.new("RGBA", (W, H or 10), (0, 0, 0, 0)).save(blank)
    lst, t = [], 0.0
    for st, en, p in sorted(events):
        st = max(st, t)
        if st > t:
            lst.append((blank, st - t))
        if en > st:
            lst.append((p, en - st))
        t = max(t, en)
    if dur > t:
        lst.append((blank, dur - t))
    lp = os.path.join(tmp, "caps.txt")
    with open(lp, "w") as f:
        for p, d in lst:
            f.write(f"file '{p}'\nduration {d:.3f}\n")
        f.write(f"file '{lst[-1][0]}'\n")
    return lp, (H or 10)


def drop_overlap(words, windows):
    out = []
    for w in words:
        if any(w["start"] < b and w["end"] > a for a, b in windows):
            continue
        out.append(w)
    return out


def fake_words(text, start, dur):
    ws = text.split()
    if not ws:
        return []
    step = dur / len(ws)
    return [{"word": x, "start": start + i * step, "end": start + (i + 1) * step} for i, x in enumerate(ws)]


# ---------------------------------------------------------------- title card (top of short)
def render_title_png(text, W, font_path, path, size=62):
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype(font_path, size)
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if font.getlength(t) > W * 0.88 and cur:
            lines.append(cur)
            cur = w
        else:
            cur = t
    lines.append(cur)
    lines = lines[:3]
    lh = int(size * 1.25)
    im = Image.new("RGBA", (W, lh * len(lines) + 20), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for i, l in enumerate(lines):
        x = (W - font.getlength(l)) / 2
        d.text((x, 10 + i * lh), l, font=font, fill=WHITE, stroke_width=5, stroke_fill=BLACK)
    im.save(path)
    return im.size[1]


# ---------------------------------------------------------------- final render
def render(src, offset, dur, words, hook, closing, hook_mp3, close_mp3, title, short, tmp, mode):
    W, H = (1080, 1920) if short else (1280, 720)
    font = find_font()
    hook_len = audio_len(hook_mp3) if hook_mp3 else 0
    close_len = audio_len(close_mp3) if close_mp3 else 0
    hook_at = 0.3
    close_at = max(hook_at + hook_len + 1.0, dur - close_len - 0.4)
    windows = []
    if hook_mp3:
        windows.append((hook_at, hook_at + hook_len + 0.1))
    if close_mp3:
        windows.append((close_at, close_at + close_len + 0.1))
    # captions: original speech (not under commentary) + commentary words
    cw = drop_overlap(words, windows)
    if hook_mp3:
        cw += fake_words(hook, hook_at, hook_len)
    if close_mp3:
        cw += fake_words(closing, close_at, close_len)
    cw.sort(key=lambda w: w["start"])
    size = 84 if short else 52
    cap_lst, cap_h = build_caption_track(cw, dur, W, size, font, tmp) if cw else (None, 0)

    inputs = ["-ss", f"{offset:.3f}", "-t", f"{dur:.3f}", "-i", src]
    n_in = 1
    idx = {}
    if cap_lst:
        inputs += ["-f", "concat", "-safe", "0", "-i", cap_lst]
        idx["cap"] = n_in
        n_in += 1
    title_h = 0
    if short and title:
        tp = os.path.join(tmp, "title.png")
        title_h = render_title_png(title, W, font, tp)
        inputs += ["-loop", "1", "-t", f"{dur:.3f}", "-i", tp]
        idx["title"] = n_in
        n_in += 1
    if hook_mp3:
        inputs += ["-i", hook_mp3]
        idx["hook"] = n_in
        n_in += 1
    if close_mp3:
        inputs += ["-i", close_mp3]
        idx["close"] = n_in
        n_in += 1

    f = []
    if short:
        f.append(f"[0:v]split=2[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                 f"boxblur=30:5,eq=brightness=-0.12[bg];[b]scale={W}:-2:flags=lanczos[fg];"
                 f"[bg][fg]overlay=(W-w)/2:(H-h)/2[v0]")
        cap_y = int(H * 0.5 + (W * 9 / 16) / 2 + 60)  # just under the picture
    else:
        f.append(f"[0:v]scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black[v0]")
        cap_y = int(H * 0.78)
    last = "v0"
    if "cap" in idx:
        f.append(f"[{idx['cap']}:v]format=rgba,fps=30[cp];[{last}][cp]overlay=0:{cap_y}:eof_action=pass:format=auto[v1]")
        last = "v1"
    if "title" in idx:
        f.append(f"[{last}][{idx['title']}:v]overlay=0:{int(H * 0.5 - (W * 9 / 16) / 2 - title_h - 40)}:format=auto[v2]")
        last = "v2"
    # audio: original, ducked while commentary speaks
    duck = "".join(f",volume=0.18:enable='between(t,{a:.2f},{b:.2f})'" for a, b in windows)
    f.append(f"[0:a]aresample=44100,volume=1.0{duck}[oa]")
    mix_in, k = ["[oa]"], 0
    for key, at in (("hook", hook_at), ("close", close_at)):
        if key in idx:
            ms = int(at * 1000)
            f.append(f"[{idx[key]}:a]aresample=44100,volume=1.6,adelay={ms}|{ms}[c{k}]")
            mix_in.append(f"[c{k}]")
            k += 1
    f.append("".join(mix_in) + f"amix=inputs={len(mix_in)}:duration=first:normalize=0,alimiter=limit=0.95[aout]")
    fs = os.path.join(tmp, "graph.txt")
    open(fs, "w").write(";\n".join(f))
    final = os.path.join(tmp, "final.mp4")
    sh(["ffmpeg", "-y", "-v", "error"] + inputs + ["-filter_complex_script", fs, "-map", f"[{last}]", "-map", "[aout]",
        "-t", f"{dur:.3f}", "-r", "30", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-maxrate", ("5M" if short else "3M"),
        "-bufsize", ("10M" if short else "6M"), "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", final])
    prev = os.path.join(tmp, "preview.mp4")
    sh(["ffmpeg", "-y", "-v", "error", "-i", final, "-vf", "scale=-2:640", "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "30", "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", prev])
    return final, prev


# ---------------------------------------------------------------- main
def main():
    global JOB, JOB_ID
    JOB = json.loads(os.environ["JOB_JSON"])
    JOB_ID = str(JOB["jobId"])
    url = (JOB.get("url") or "").strip()
    short = JOB.get("format", "short") == "short"
    mode = JOB.get("style", "commentary")
    voice = JOB.get("voice") or "en-NG-AbeoNeural"
    tts_url = JOB.get("ttsUrl") or "https://openai-edge-tts-9mrp.onrender.com"
    hint = JOB.get("hint") or ""
    scan_min = int(JOB.get("scanMinutes") or 40)
    tmp = tempfile.mkdtemp(prefix="clip_")
    try:
        status("start", "Job started")
        if not url:
            status("search", "Looking for a long video")
            url, _t = discover(JOB.get("topics") or [], bool(JOB.get("ccOnly", True)), tmp)
            print("chosen video:", url, _t)
        info = source_info(url, tmp)
        total = info["duration"]
        s, e = JOB.get("start"), JOB.get("end")
        picked_title = ""
        if s is None or e is None:
            limit = min(total or scan_min * 60, scan_min * 60)
            status("transcript", f"Reading the first {int(limit // 60)} minutes of the video")
            segs = subs_transcript(url, tmp, limit)
            if len(segs) < 5:
                if not GROQ_KEY:
                    raise RuntimeError("GROQ_API_KEY secret is missing")
                segs = audio_transcript(url, tmp, limit)
            if not segs:
                raise RuntimeError("could not read the video's speech. Give a start and end time instead.")
            status("pick", "Choosing the best moment")
            s, e, picked_title, why = pick_moment(segs, hint, short)
            print("picked", s, e, why)
        s, e = float(s), float(e)
        if total and e > total:
            e = total
        if e - s < 5:
            raise RuntimeError("the clip is shorter than 5 seconds. Check the start and end times.")
        dur = e - s
        status("download", f"Cutting {fmt_t(s)} to {fmt_t(e)}")
        src, offset = cut_section(url, s, e, tmp, total)

        status("captions", "Listening to the clip for captions")
        ca = os.path.join(tmp, "clip_audio.mp3")
        sh(["ffmpeg", "-y", "-v", "error", "-ss", f"{offset:.3f}", "-t", f"{dur:.3f}", "-i", src, "-vn", "-ac", "1",
            "-ar", "16000", "-b:a", "48k", ca])
        words, segs2 = [], []
        if GROQ_KEY:
            segs2, words = groq_whisper(ca)
        clip_text = " ".join(x["text"] for x in segs2)

        hook = closing = ""
        hook_mp3 = close_mp3 = None
        title, desc = (picked_title or info.get("title", "Clip"))[:90], f"Original video: {url}" + ("\n#Shorts" if short else "")
        if GROQ_KEY:
            status("writing", "Writing title, description and commentary")
            h, c, t, d = write_commentary(clip_text, info.get("title", ""), url, short)
            title, desc = t or title, d
            if mode == "commentary":
                hook, closing = h, c
        if mode == "commentary" and hook:
            status("voice", "Recording the commentary voice")
            hook_mp3 = os.path.join(tmp, "hook.mp3")
            tts(hook, voice, tts_url, hook_mp3)
            if closing:
                close_mp3 = os.path.join(tmp, "close.mp3")
                tts(closing, voice, tts_url, close_mp3)

        status("render", "Building the video")
        final, prev = render(src, offset, dur, words, hook, closing, hook_mp3, close_mp3, title, short, tmp, mode)

        status("upload", "Uploading the result")
        with open(final, "rb") as f:
            url_final = sb_put(f"clips/{JOB_ID}.mp4", f, "video/mp4")
        with open(prev, "rb") as f:
            url_prev = sb_put(f"clips/{JOB_ID}_preview.mp4", f, "video/mp4")
        status("done", "Clip is ready", state="done", video_url=url_final, preview_url=url_prev, title=title,
               description=desc, source_url=url, source_title=info.get("title", ""), start=s, end=e, duration=round(dur, 1), hook=hook, closing=closing)
    except Exception as ex:
        status("error", str(ex)[:900], state="error")
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
