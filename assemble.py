#!/usr/bin/env python3
"""
assemble.py - Betrayer episode assembler (polished version).

Polish added on top of the original pipeline:
  - images are fitted to the 9:16 frame on a blurred background (no stretching)
  - smooth eased zoom/pan, six different camera moves in rotation scene by scene
  - v68: cinema mode - 2.5D depth parallax camera (push, pull, truck, orbit, crane, roll) read from each scene's CAMERA line, and real transitions
    (zoom-through, whip, spin, light flash, dark push, passing-object wipe, dissolve) read from its TRANSITION line
  - timed subtitles in short chunks (not one giant block)
  - soft fades between scenes
  - consistent audio format per scene (no glitches on join)
  - narration loudness levelled, background music ducked under the voice
  - faststart + capped bitrate (smaller, quicker-to-open file)

NEW: animated clips.
  When the manifest says "useAnimation": true and gives a clip for a scene
  ({"scene_number": 3, "clip_url": "https://..."}), that scene is built from the
  moving clip instead of the still image (same blurred 9:16 frame, same subtitles,
  same fades, same voice). If a clip is missing or fails, the scene silently falls
  back to its still image, so assembly never gets stuck.
"""

import json
import math
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
import textwrap
import requests

# Supabase config
SUPABASE_URL = "https://njijwtcamdsorymzanpj.supabase.co".rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]  # from GitHub secret

BUCKET = "betrayer-assets"
WIDTH, HEIGHT = 1920, 1080   # default is now horizontal 16:9; a manifest with aspect "9:16" switches the whole film to vertical 1080x1920 (set in main)
SUBTITLES = True             # a manifest with subtitles:false removes the burned-in subtitles
FPS = 30

# ---------------- POLISH SETTINGS (edit here) ----------------
ZOOM_AMOUNT = 0.06          # how far the slow zoom goes (0.06 = 6%; was 12% and it cut heads)
HEAD_SAFE = 0.28            # 0.5 = zoom into the centre; smaller keeps the TOP of the picture (heads) and crops the bottom more
FADE_SECONDS = 0.30         # fade in/out at the start/end of each STILL-IMAGE scene (animated clips cut straight, like a real film)
CLIP_SCENE_TAIL_SECONDS = 0.12  # v66: pause after the voice in an ANIMATED scene (stills keep SCENE_TAIL_SECONDS). A long silent pause with a frozen picture was the 'hiccup' between scenes.
CLIP_MAX_STRETCH = 1.20     # v66: a clip a little shorter than its voice is slowed by up to 20% to fill the scene (almost unnoticeable) instead of freezing on its last frame
CLIP_JOIN_BLEND = 0.30     # v65: seconds. The end of an animated scene melts into the FIRST picture of the next animated scene, so the cut has no jump (0 = off). No black, no length change.
CLIP_SCENE_FADES = False    # True = animated clips fade in/out too
SCENE_TAIL_SECONDS = 0.4    # pause after each scene's narration
SUB_FONT_SIZE = 46
SUB_WRAP_CHARS = 46         # characters per subtitle line (horizontal film)
SUB_CHUNK_CHARS = 92        # characters per subtitle screen (about 2 lines)
SUB_BOTTOM_MARGIN = 90      # pixels from the bottom
# SHORTS CAPTIONS (vertical 9:16 films only), CapCut style: 3-4 words stay on screen in white, and a yellow box jumps from word to word
# exactly while that word is being spoken. v67: the timing comes from the REAL voice (Whisper word times when GROQ_API_KEY is set; otherwise the real
# start/end of every voice line and every pause measured in the audio), not from a guess over the whole scene.
SHORTS_CAPTIONS = True      # False = the older small subtitles also in vertical films
SHORTS_WORDS = 4            # most words on screen at once
SHORTS_FONT_SIZE = 96
SHORTS_Y = 0.60             # centre of the captions as a share of the picture height (the bottom 20% is covered by the TikTok/Shorts buttons)
SHORTS_BOX_COLOR = "0xFFD400"   # the moving box behind the word being spoken (yellow)
SHORTS_TEXT_COLOR = "white"
SHORTS_MAX_WIDTH = 0.86     # a caption line is never wider than this share of the picture width
_CAP_ADV = {"A":764,"B":650,"C":730,"D":711,"E":632,"F":605,"G":740,"H":727,"I":281,"J":585,"K":729,"L":575,"M":925,"N":738,"O":746,"P":648,"Q":746,"R":666,"S":685,"T":656,"U":705,"V":750,"W":1037,"X":741,"Y":730,"Z":652,"0":682,"1":413,"2":630,"3":638,"4":690,"5":618,"6":646,"7":579,"8":642,"9":646,"'":286,"!":270,"?":604,"&":680,"-":459}
_CAP_KERN = {"AA":15,"AC":-49,"AG":-48,"AO":-49,"AQ":-49,"AS":-28,"AT":-97,"AU":-49,"AV":-117,"AW":-112,"AY":-107,"A0":-39,"A1":-117,"A6":-39,"A'":-79,"A?":-62,"A-":-34,"BT":-19,"CA":-35,"CX":-23,"DA":-49,"DT":-34,"DV":-54,"DW":-39,"DX":-68,"DY":-48,"FA":-91,"FJ":-67,"GA":-39,"GT":-31,"GV":-22,"GX":-22,"GY":-33,"JA":-48,"KC":-64,"KG":-63,"KO":-63,"KQ":-63,"KS":-44,"K-":-51,"LC":-29,"LG":-28,"LO":-28,"LQ":-28,"LT":-86,"LV":-85,"LW":-68,"LY":-131,"L1":-45,"L'":-90,"L-":-51,"OA":-49,"OT":-33,"OV":-54,"OW":-39,"OX":-68,"OY":-48,"PA":-91,"PJ":-96,"P&":-34,"QA":-49,"QT":-33,"QV":-54,"QW":-39,"QX":-68,"QY":-48,"RC":-6,"RT":-25,"RV":-23,"RY":-34,"SA":-28,"SX":-34,"S7":-14,"TA":-97,"TC":-34,"TG":-33,"TJ":-90,"TO":-33,"TQ":-33,"T0":-34,"T4":-67,"T6":-34,"T7":-79,"T&":-34,"T-":-78,"UA":-49,"VA":-117,"VC":-54,"VG":-53,"VJ":-96,"VO":-54,"VQ":-54,"VV":15,"V0":-54,"V4":-33,"V6":-54,"V&":-51,"V-":-63,"WA":-112,"WC":-39,"WG":-39,"WJ":-62,"WO":-39,"WQ":-39,"WS":-11,"WW":20,"W4":-33,"W&":-51,"W-":-34,"XC":-69,"XG":-68,"XO":-68,"XQ":-68,"XS":-34,"X0":-49,"X3":-24,"X4":-39,"X6":-49,"X8":-25,"X-":-45,"YA":-107,"YC":-49,"YG":-48,"YJ":-33,"YO":-48,"YQ":-48,"Y4":-67,"Y&":-62,"Y-":-50,"ZC":-34,"ZG":-33,"ZO":-33,"ZQ":-33,"Z-":-33,"0A":-39,"0V":-54,"0X":-49,"0Y":-34,"07":-20,"24":-15,"3X":-24,"4A":-9,"41":-63,"47":-58,"51":-20,"7A":-108,"7J":-96,"70":-16,"73":-17,"74":-58,"75":-10,"76":-16,"77":20,"78":-14,"79":-10,"7&":-46,"8X":-25,"9A":-39,"9V":-54,"9X":-49,"9Y":-34,"97":-20,"'A":-79,"'J":-164,"'4":-62,"'&":-34,"?A":-62,"&T":-57,"&V":-63,"&W":-40,"&Y":-74,"&'":-34,"-A":-34,"-J":-45,"-T":-78,"-V":-40,"-W":-34,"-X":-45,"-Y":-50,"-2":-11,"-7":-34}

# ---- BRAND WATERMARK (edit here) ----
WATERMARK_TEXT = "SAMSON_SET"   # "" = no watermark
WATERMARK_OPACITY = 0.40            # 0.0 invisible ... 1.0 solid; 0.35-0.5 looks professional
WATERMARK_SIZE = 0.035              # letter height as a share of the short side of the frame (0.028 = small)
WATERMARK_POSITION = "top-right"    # top-right, top-left, bottom-right, bottom-left or center
WATERMARK_CENTER_FOR_WIDE = True    # v80: horizontal (16:9) films get the mark in the MIDDLE, harder to crop out; vertical films keep the position above
WATERMARK_CENTER_SIZE = 0.075       # letter height of the middle mark (share of the short side)
WATERMARK_CENTER_OPACITY = 0.20     # see-through, so it does not hide the picture but cannot be cropped out
SFX_ENABLED = True          # the film's sound effects (planned from the script by the "sound supervisor" in n8n) are mixed in
SFX_ONCE_REL_DB = -7.0      # loudness of a single event (explosion, door slam) compared with the narration
SFX_REPEAT_REL_DB = -10.0   # repeated hits (fight blows, sword clashes, knocks)
SFX_BED_REL_DB = -16.0      # continuous beds (engine, footsteps, rain, crowd) sit well under the voice
SFX_MUSIC_REL_DB = -8.0    # slow background music under the narration (was -21: too quiet to hear on a phone)
SFX_MUSIC_DUCK_RATIO = 1.5  # music dips while the narrator speaks (was 6: it almost disappeared)
MUSIC_DIR = "assets/music"  # optional: put calm.mp3, sad.mp3, tense.mp3, mysterious.mp3, hopeful.mp3 here for real tracks;
                            # a mood without a file gets a slow generated pad instead
SKIP_DEFAULT_MUSIC = False  # set to True automatically when the film has its own planned music
SFX_DUCK_RATIO = 2.5        # effects dip a little while the narrator speaks (1 = no dipping)
SFX_MAX_EVENTS = 120        # most hits one repeating cue may place
MUSIC_PATH = "assets/music/default.mp3"
MUSIC_VOLUME = 5          # before ducking; it dips automatically under the voice (was 0.25: too quiet)
VIDEO_CRF = 20              # lower = better quality, bigger file (v69: was 23)
VIDEO_MAXRATE = "6M"        # caps file size (v69: was 3M, which made long films soft)
CLIP_FILL_MODE = "hold"     # clip shorter than the scene's voice: "hold" its last frame (v64: never "loop" - looping made an action play twice)

# ---- YOUTUBE THUMBNAIL (edit here) ----
THUMB_ENABLED = True        # make a 1280x720 cover picture with the film title and upload it next to the video
THUMB_TAG = "NEW FILM"      # small label in the top-left corner ("" = none)
THUMB_ACCENT = "FFD400"     # colour of the label text and frame (hex, no #)
THUMB_COLOR_TOP = "FFE45A"  # title fill: top colour of the gold gradient (hex, no #)
THUMB_COLOR_BOTTOM = "F07A0A"  # title fill: bottom colour of the gradient (hex, no #)
THUMB_SQUEEZE = 0.80        # 1.0 = normal width letters, 0.8 = taller, narrower poster letters
# --------------------------------------------------------------

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}


def sb_get(path):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{path}", headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def sb_patch(path, body):
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/{path}",
        headers={**HEADERS, "Prefer": "return=representation"},
        data=json.dumps(body),
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def mark_failed(episode_id, message):
    try:
        sb_patch(f"episodes?id=eq.{episode_id}", {"status": "failed", "last_error": message[:500]})
    except Exception as e:
        print(f"::warning::also failed to record the failure in Supabase: {e}")


def run(cmd):
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True)


def download(url, dest):
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    with open(dest, "wb") as f:
        f.write(r.content)


def probe_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def find_font():
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


# ---------------------------------------------------------------- polish helpers

def mean_volume_db(path):
    """Average loudness of a file in dB (None if it cannot be measured)."""
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-af", "volumedetect", "-vn", "-f", "null", "-"],
                       capture_output=True, text=True)
    m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", p.stderr or "")
    return float(m.group(1)) if m else None


def peak_volume_db(path):
    """Loudest moment of a file in dB (None if it cannot be measured)."""
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-af", "volumedetect", "-vn", "-f", "null", "-"],
                       capture_output=True, text=True)
    m = re.search(r"max_volume:\s*(-?[\d.]+) dB", p.stderr or "")
    return float(m.group(1)) if m else None


def _fx_download(url, tmp, tag):
    raw = os.path.join(tmp, f"fx_{tag}.mp3")
    download(url, raw)
    length = probe_duration(raw)
    if length < 0.15:
        raise RuntimeError("sound file is empty")
    return raw, length, mean_volume_db(raw), peak_volume_db(raw)


def _fx_gain(mean, peak, target_mean, peak_cap):
    """Gain that brings the file's average to target_mean, but never lets its loudest moment pass peak_cap
    (a short punch in a long quiet file would otherwise be pushed far too loud)."""
    if mean is None:
        return -12.0
    g = target_mean - mean
    if peak is not None:
        g = min(g, peak_cap - peak)
    return max(-25.0, min(25.0, g))


MOOD_CHORDS = {   # slow pads: the notes (Hz) of each mood
    "calm": [130.81, 164.81, 196.00, 261.63],
    "hopeful": [146.83, 185.00, 220.00, 293.66],
    "sad": [110.00, 130.81, 164.81, 220.00],
    "mysterious": [73.42, 110.00, 174.61, 220.00],
    "tense": [82.41, 87.31, 123.47, 164.81],
}


def make_pad(mood, seconds, out_path):
    """A slow, soft generated pad (no music file needed): a few sine notes that swell and fade at different speeds,
    softened with a low-pass filter and a long echo."""
    notes = MOOD_CHORDS.get(mood) or MOOD_CHORDS["calm"]
    expr = "0"
    for i, f in enumerate(notes):
        r = 0.045 + 0.013 * i
        ph = i * 1.7
        expr += (f"+0.22*sin(2*PI*{f}*t+{ph:.2f})*(0.65+0.35*sin(2*PI*{r:.3f}*t+{ph:.2f}))"
                 f"+0.06*sin(2*PI*{f * 2.003:.2f}*t)*(0.5+0.5*sin(2*PI*{r * 1.3:.3f}*t))")
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"aevalsrc={expr}:s=44100:d={seconds:.2f}:c=stereo",
         "-af", "lowpass=f=1100,aecho=0.8:0.85:90|180:0.35|0.25,volume=0.5", "-ar", "44100", "-ac", "2", out_path])


def render_music_layer(cue, span, narr_mean, tmp, idx):
    """Slow background music for a stretch of the film: a track from assets/music/<mood>.mp3 if you added one, else a generated pad."""
    mood = str(cue.get("q") or "calm").strip().lower()
    span = min(max(span, 1.0), 900.0)
    vol = min(max(float(cue.get("vol") or 0.5), 0.2), 1.5)
    target = narr_mean + SFX_MUSIC_REL_DB + 20 * math.log10(vol / 0.5)
    out = os.path.join(tmp, f"fxlayer_{idx}.wav")
    fin = min(2.5, span / 3)
    fout = min(3.5, span / 3)
    src = os.path.join(MUSIC_DIR, f"{mood}.mp3")
    if not os.path.exists(src) and os.path.exists(MUSIC_PATH):
        src = MUSIC_PATH          # no track for this mood: use the default track instead of the faint generated pad
    cmd = ["ffmpeg", "-y"]
    if os.path.exists(src):
        mean = mean_volume_db(src)
        length = probe_duration(src)
        if length < span:
            cmd += ["-stream_loop", "-1"]
    else:
        src = os.path.join(tmp, f"pad_{idx}.wav")
        make_pad(mood, span + 1.0, src)
        mean = mean_volume_db(src)
    gain = max(-30.0, min(30.0, target - mean)) if mean is not None else -15.0
    cmd += ["-i", src, "-t", f"{span:.3f}", "-af",
            f"aformat=sample_rates=44100:channel_layouts=stereo,volume={gain:.1f}dB,afade=t=in:st=0:d={fin:.2f},afade=t=out:st={max(span - fout, 0):.3f}:d={fout:.2f}",
            "-ar", "44100", "-ac", "2", out]
    run(cmd)
    return out


def render_fx_layer(cue, t0, t1, narr_mean, tmp, idx):
    """Render ONE sound cue into a stereo wav that is exactly (t1 - t0) seconds long.
    once   = a single event;  bed = the sound runs without a gap (looped) until the action stops;
    repeat = the same kind of hit again and again with natural gaps (several different files are rotated)."""
    span = max(0.3, t1 - t0)
    kind = cue.get("kind") or "once"
    vol = min(max(float(cue.get("vol") or 0.7), 0.2), 1.5)
    vol_db = 20 * math.log10(vol / 0.7)
    if kind == "music":
        return render_music_layer(cue, span, narr_mean, tmp, idx)
    out = os.path.join(tmp, f"fxlayer_{idx}.wav")
    fmt = "aformat=sample_rates=44100:channel_layouts=stereo"
    urls = [u for u in (cue.get("urls") or []) if u][:3]
    files = []
    for k, u in enumerate(urls):
        try:
            files.append(_fx_download(u, tmp, f"{idx}_{k}"))
        except Exception as e:
            print(f"::warning::sound cue {idx}: file {k} skipped ({e})")
    if not files:
        raise RuntimeError("no usable sound file")
    if kind == "bed":
        raw, length, mean, peak = files[0]
        gain = _fx_gain(mean, peak, narr_mean + SFX_BED_REL_DB + vol_db, narr_mean + SFX_BED_REL_DB + 8 + vol_db)
        fin = min(1.0, span / 3)
        fout = min(1.5, span / 3)
        cmd = ["ffmpeg", "-y"]
        if length < span:
            cmd += ["-stream_loop", "-1"]            # a short sound repeats without a gap for the whole span
        cmd += ["-i", raw, "-t", f"{span:.3f}", "-af",
                f"{fmt},volume={gain:.1f}dB,afade=t=in:st=0:d={fin:.2f},afade=t=out:st={max(span - fout, 0):.3f}:d={fout:.2f}",
                "-ar", "44100", "-ac", "2", out]
        run(cmd)
    elif kind == "repeat":
        gap = min(max(float(cue.get("gap") or 1.3), 0.35), 8.0)
        rnd = random.Random(int(cue.get("id") or idx) * 7919)
        times, t = [], 0.0
        while t < span - 0.2 and len(times) < SFX_MAX_EVENTS:
            times.append(t)
            t += gap * (0.75 + 0.5 * rnd.random())     # natural, uneven gaps
        order = [rnd.randrange(len(files)) for _ in times]
        cmd = ["ffmpeg", "-y"]
        for raw, _, _, _ in files:
            cmd += ["-i", raw]
        parts, labels = [], []
        for vi, (raw, length, mean, peak) in enumerate(files):
            uses = [j for j, o in enumerate(order) if o == vi]
            if not uses:
                continue
            base = _fx_gain(mean, peak, narr_mean + SFX_REPEAT_REL_DB + vol_db, narr_mean + 3 + vol_db)
            if len(uses) == 1:
                parts.append(f"[{vi}:a]{fmt}[v{vi}_0]")
            else:
                parts.append(f"[{vi}:a]{fmt},asplit={len(uses)}" + "".join(f"[v{vi}_{n}]" for n in range(len(uses))))
            for n, j in enumerate(uses):
                play = min(length, 3.5)
                ms = int(times[j] * 1000)
                jit = base + rnd.uniform(-2.0, 1.5)    # every hit a little different in loudness
                parts.append(f"[v{vi}_{n}]atrim=0:{play:.3f},asetpts=PTS-STARTPTS,volume={jit:.1f}dB,"
                             f"afade=t=out:st={max(play - 0.25, 0):.3f}:d=0.25,adelay={ms}|{ms}[e{j}]")
                labels.append(f"[e{j}]")
        parts.append("".join(labels) + f"amix=inputs={len(labels)}:duration=longest:normalize=0,atrim=0:{span:.3f},apad=whole_dur={span:.3f}[o]")
        cmd += ["-filter_complex", ";".join(parts), "-map", "[o]", "-ar", "44100", "-ac", "2", out]
        run(cmd)
    else:  # once
        raw, length, mean, peak = files[0]
        gain = _fx_gain(mean, peak, narr_mean + SFX_ONCE_REL_DB + vol_db, narr_mean + 6 + vol_db)
        play = min(length, 6.0, span)
        fade = min(0.4, play / 2)
        run(["ffmpeg", "-y", "-i", raw, "-af",
             f"{fmt},atrim=0:{play:.3f},volume={gain:.1f}dB,afade=t=in:st=0:d=0.03,afade=t=out:st={max(play - fade, 0):.3f}:d={fade:.3f}",
             "-ar", "44100", "-ac", "2", out])
    return out


def apply_sfx(joined_path, cues, spans, tmp):
    """Add the film's sound effects to the finished picture. Every cue is laid on ONE timeline for the whole film, so a
    bed (engine, footsteps, rain) or a fight keeps going across scene cuts and stops when the action stops.
    Returns the path of the new file (or the original if there is nothing to add)."""
    total = probe_duration(joined_path)
    narr_mean = mean_volume_db(joined_path)
    if narr_mean is None:
        narr_mean = -20.0
    layers = []
    for idx, cue in enumerate(cues):
        try:
            a = int(cue.get("scene_from") or 0)
            b = int(cue.get("scene_to") or a)
            if a not in spans:
                continue
            if b not in spans or b < a:
                b = a
            t0 = spans[a][0] + min(max(float(cue.get("from") or 0), 0.0), 0.97) * spans[a][1]
            if (cue.get("kind") or "once") == "once":
                t1 = min(total, t0 + 6.0)
            else:
                t1 = spans[b][0] + min(max(float(cue.get("to") or 1), 0.05), 1.0) * spans[b][1]
            t1 = min(t1, total)
            if t1 - t0 < 0.3:
                continue
            wav = render_fx_layer(cue, t0, t1, narr_mean, tmp, idx + 1)
            layers.append((wav, t0, (cue.get('kind') == 'music')))
            print(f"sound cue {idx + 1} ({cue.get('kind')}, '{cue.get('q')}'): {t0:.1f}s - {t1:.1f}s")
        except Exception as e:
            print(f"::warning::sound cue {idx + 1} skipped ({e})")
    if not layers:
        return joined_path
    out_path = os.path.join(tmp, "joined_fx.mp4")
    cmd = ["ffmpeg", "-y", "-i", joined_path]
    parts = []
    fx_ids, mu_ids = [], []
    for i, (wav, t0, is_music) in enumerate(layers, start=1):
        cmd += ["-i", wav]
        ms = int(t0 * 1000)
        parts.append(f"[{i}:a]adelay={ms}|{ms}[l{i}]")
        (mu_ids if is_music else fx_ids).append(i)
    groups = (fx_ids and 1 or 0) + (mu_ids and 1 or 0)
    side_n = groups + 1
    parts.append("[0:a]aformat=sample_rates=44100:channel_layouts=stereo,asplit=%d[narr]%s" % (side_n, "".join(f"[nside{k}]" for k in range(groups))))
    final_in, k = ["[narr]"], 0
    for name, ids, ratio in (("sfx", fx_ids, SFX_DUCK_RATIO), ("mus", mu_ids, SFX_MUSIC_DUCK_RATIO)):
        if not ids:
            continue
        mix_in = "".join(f"[l{i}]" for i in ids)
        parts.append(f"{mix_in}amix=inputs={len(ids)}:duration=longest:normalize=0[{name}raw]")
        parts.append(f"[{name}raw][nside{k}]sidechaincompress=threshold=0.05:ratio={ratio}:attack=30:release=500[{name}d]")
        final_in.append(f"[{name}d]")
        k += 1
    parts.append("".join(final_in) + f"amix=inputs={len(final_in)}:duration=first:normalize=0,alimiter=limit=0.97[aout]")
    cmd += ["-filter_complex", ";".join(parts), "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2", "-t", f"{total:.3f}", out_path]
    run(cmd)
    if mu_ids:
        global SKIP_DEFAULT_MUSIC
        SKIP_DEFAULT_MUSIC = True        # the film has its own planned music: the always-on default track would double it
    print(f"{len(layers)} sound cue(s) mixed into the film")
    return out_path


def build_composite(image_path, out_path):
    """Fit the image inside the 9:16 frame on a blurred, darkened copy of itself.
    Nothing is stretched or badly cropped, whatever size the image is."""
    fc = (
        "[0:v]split=2[bgsrc][fgsrc];"
        f"[bgsrc]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={WIDTH}:{HEIGHT},boxblur=40:6,eq=brightness=-0.12[bg];"
        f"[fgsrc]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease:flags=lanczos[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p[out]"
    )
    run(["ffmpeg", "-y", "-i", image_path, "-filter_complex", fc,
         "-map", "[out]", "-frames:v", "1", out_path])


def zoom_expressions(scene_index, total_frames):
    """A different slow camera move for each scene (six moves in rotation). Eased start and end."""
    n = max(total_frames, 1)
    p = f"(on/{n})"
    ease = f"({p}*{p}*(3-2*{p}))"
    mode = scene_index % 6
    a = ZOOM_AMOUNT
    cx = "iw/2-(iw/zoom/2)"
    cy = f"(ih-ih/zoom)*{HEAD_SAFE}"      # v60: top-weighted so a zoom never slices off the top of a head
    if mode == 0:      # slow push in, centred
        z, x, y = f"1+{a}*{ease}", cx, cy
    elif mode == 1:    # slow pull out, centred
        z, x, y = f"1+{a}-{a}*{ease}", cx, cy
    elif mode == 2:    # push in while drifting upward
        z, x, y = f"1+{a}*{ease}", cx, f"(ih-ih/zoom)*(0.45-0.25*{ease})"
    elif mode == 3:    # pan left to right
        z, x, y = f"1+{a}", f"(iw-iw/zoom)*{ease}", cy
    elif mode == 4:    # pan right to left
        z, x, y = f"1+{a}", f"(iw-iw/zoom)*(1-{ease})", cy
    else:              # pull out while drifting downward
        z, x, y = f"1+{a}-{a}*{ease}", cx, f"(ih-ih/zoom)*(0.15+0.25*{ease})"
    return z, x, y


def split_subtitle_chunks(text):
    """Break narration into short on-screen chunks of roughly one or two lines."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return []
    # split into sentences first, then pack words into chunks
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks = []
    for s in sentences:
        words = s.split(" ")
        cur = ""
        for w in words:
            if cur and len(cur) + 1 + len(w) > SUB_CHUNK_CHARS:
                chunks.append(cur)
                cur = w
            else:
                cur = (cur + " " + w).strip()
        if cur:
            chunks.append(cur)
    return chunks


def _cap_word_width(word, size):
    """Width of one word in the caption font (letter widths plus the kerning pairs), in pixels."""
    w = sum(_CAP_ADV.get(c, 700) for c in word) + sum(_CAP_KERN.get(word[k:k + 2], 0) for k in range(len(word) - 1))
    return w * size / 1000.0



# ---------------- REAL WORD TIMING FOR THE YELLOW BOX (v67) ----------------
CAPTION_TIMING = {}          # caption tag -> [(WORD, s, e)] with the REAL time of every word inside the scene
CAPTION_WHISPER = True       # True = ask Whisper (Groq) for the exact time of every spoken word, when GROQ_API_KEY is set; False = only measure the audio
CAPTION_LEAD = 0.04          # seconds the box appears BEFORE the word starts (the eye needs the box a hair early)
STT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"


def speech_bursts(path, noise_db=-38, min_gap=0.12):
    """Measure the real speech in an audio file: [(start, end)] of the stretches that are not silent."""
    total = probe_duration(path)
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-af", f"silencedetect=noise={noise_db}dB:d={min_gap}", "-f", "null", "-"],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    sil, start = [], None
    for line in (p.stdout or "").splitlines():
        m = re.search(r"silence_start:\s*(-?[0-9.]+)", line)
        if m:
            start = max(float(m.group(1)), 0.0)
        m = re.search(r"silence_end:\s*([0-9.]+)", line)
        if m and start is not None:
            sil.append((start, float(m.group(1)))); start = None
    if start is not None:
        sil.append((start, total))
    bursts, cur = [], 0.0
    for a, b in sil:
        if a - cur > 0.04:
            bursts.append((cur, a))
        cur = b
    if total - cur > 0.04:
        bursts.append((cur, total))
    return bursts or [(0.0, total)]


def whisper_word_times(path):
    """Exact word times from Whisper (Groq). Returns [{w, s, e}] or [] when there is no key / no network."""
    keys = [k.strip() for k in (os.environ.get("GROQ_API_KEY", "") + "," + os.environ.get("GROQ_API_KEY_2", "")).split(",") if k.strip()]
    if not CAPTION_WHISPER or not keys:
        return []
    for attempt in range(3):
        try:
            with open(path, "rb") as f:
                r = requests.post(STT_URL, headers={"Authorization": "Bearer " + keys[attempt % len(keys)]}, files={"file": f},
                                  data={"model": "whisper-large-v3-turbo", "response_format": "verbose_json",
                                        "timestamp_granularities[]": "word", "temperature": "0"}, timeout=120)
            if r.status_code == 200:
                return [{"w": str(w.get("word", "")).strip(), "s": float(w.get("start", 0)), "e": float(w.get("end", 0))}
                        for w in r.json().get("words", []) if str(w.get("word", "")).strip()]
            print(f"::warning::whisper {r.status_code}: {r.text[:120]}")
        except Exception as e:
            print(f"::warning::whisper failed ({e})")
        time.sleep(3 * (attempt + 1))
    return []


def _norm(w):
    return re.sub(r"[^a-z0-9]", "", (w or "").lower())


def _syllables(w):
    w = _norm(w)
    if not w:
        return 1
    if w.isdigit():
        return len(w)
    n = len(re.findall(r"[aeiouy]+", w))
    if w.endswith("e") and not w.endswith(("le", "ee")) and n > 1:
        n -= 1
    return max(1, n)


def _weight(w):
    """How long a word takes to say: mostly syllables, a little by letters (better than letters alone)."""
    return 0.5 + 0.42 * _syllables(w) + 0.03 * len(_norm(w))


def align_to_whisper(words, heard, t0, t1):
    """Match the script words to what Whisper heard; matched words take Whisper's times, the rest are filled in between them."""
    import difflib
    a = [_norm(w) for w in words]
    b = [_norm(h["w"]) for h in heard]
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    pos = [None] * len(a)
    for blk in sm.get_matching_blocks():
        for k in range(blk.size):
            h = heard[blk.b + k]
            pos[blk.a + k] = (h["s"], max(h["e"], h["s"] + 0.04))
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "replace" and (i2 - i1) == (j2 - j1):          # same count, different spelling (numbers, 'gonna'): trust the position
            for k in range(i2 - i1):
                h = heard[j1 + k]
                pos[i1 + k] = (h["s"], max(h["e"], h["s"] + 0.04))
    if sum(1 for p in pos if p) < max(2, int(0.6 * len(a))):
        return None                                              # Whisper heard something else: do not trust it
    i = 0
    while i < len(pos):
        if pos[i] is None:
            j = i
            while j < len(pos) and pos[j] is None:
                j += 1
            left = pos[i - 1][1] if i > 0 else t0
            right = pos[j][0] if j < len(pos) else t1
            right = max(right, left + 0.08 * (j - i))
            wts = [_weight(words[k]) for k in range(i, j)]
            tot, c = sum(wts) or 1.0, left
            for k, wt in zip(range(i, j), wts):
                pos[k] = (c, c + (right - left) * wt / tot)
                c += (right - left) * wt / tot
            i = j
        else:
            i += 1
    return pos


def spread_on_bursts(words, bursts):
    """No Whisper: share the words over the REAL speech bursts of the audio (measured pauses are kept), by syllable weight."""
    wts = [_weight(w) for w in words]
    lens = [max(b - a, 0.01) for a, b in bursts]
    speech, tot = sum(lens), (sum(wts) or 1.0)

    def real(x):
        x, acc = max(0.0, min(x, speech)), 0.0
        for (a, b), L in zip(bursts, lens):
            if x <= acc + L + 1e-9:
                return a + (x - acc)
            acc += L
        return bursts[-1][1]
    def burst_of(t):
        for k, (a, b) in enumerate(bursts):
            if t <= b + 1e-6:
                return k
        return len(bursts) - 1
    out, cum = [], 0.0
    for wt in wts:
        s, e = real(cum / tot * speech), real((cum + wt) / tot * speech)
        ks, ke = burst_of(s), burst_of(e - 1e-6)
        if ks != ke:                                            # a word never sits across a real pause: it belongs to the burst where most of it is
            if bursts[ks][1] - s >= 0.06:
                e = bursts[ks][1]
            else:
                s = bursts[ke][0]
        out.append((s, max(e, s + 0.05)))
        cum += wt
    return out


def prepare_caption_timing(tag, blocks):
    """blocks = [(audio_file, prefix_words, text)] in scene order. Stores CAPTION_TIMING[tag] = [(WORD, s, e)] with real times in the scene audio."""
    try:
        res, cursor = [], 0.0
        for path, prefix, text in blocks:
            dur = probe_duration(path)
            bursts = speech_bursts(path)
            toks = []
            for w in re.sub(r"\s+", " ", (text or "").replace("’", "'")).strip().split(" "):
                cl = re.sub(r"[^A-Za-z0-9'!?&-]", "", w).upper()
                if cl:
                    toks.append(cl)
            if toks:
                times = None
                heard = whisper_word_times(path)
                if heard:
                    times = align_to_whisper(toks, heard, bursts[0][0], bursts[-1][1])
                    if times:
                        lo, hi = bursts[0][0], bursts[-1][1] + 0.05
                        times = [(max(s, lo), min(max(e, s + 0.04), hi)) for s, e in times]
                if not times:
                    times = spread_on_bursts(toks, bursts)
                fixed, prev = [], bursts[0][0]
                for s, e in times:
                    s = max(s, prev)
                    fixed.append([s, max(e, s + 0.05)])
                    prev = s
                for k in range(len(fixed) - 1):
                    if fixed[k + 1][0] - fixed[k][1] <= 0.25:         # the box stays on a word until the next one starts, unless a real pause is longer
                        fixed[k][1] = max(fixed[k + 1][0], fixed[k][0] + 0.05)
                    fixed[k][1] = min(fixed[k][1], max(fixed[k + 1][0], fixed[k][0] + 0.05))
                first = fixed[0][0]
                pre = [re.sub(r"[^A-Za-z0-9'!?&-]", "", p).upper() for p in prefix]
                pre = [p for p in pre if p]
                if pre:                                               # a speaker name that is shown but not spoken: a short slot just before the speech
                    slot = min(0.4, max(first, 0.15)) / len(pre)
                    base = max(first - slot * len(pre), 0.0)
                    for n, p in enumerate(pre):
                        res.append((p, cursor + base + n * slot, cursor + base + (n + 1) * slot))
                for w, (s, e) in zip(toks, fixed):
                    res.append((w, cursor + s, cursor + e))
            cursor += dur
        if res:
            CAPTION_TIMING[tag] = res
    except Exception as e:
        print(f"::warning::caption timing could not be measured ({e}) - using the estimate")
        CAPTION_TIMING.pop(tag, None)


def shorts_caption_filters(text, duration, tmp, tag):
    """CapCut-style captions: the phrase stays on screen in white and a yellow box moves to the word being spoken."""
    words = []
    for w in re.sub(r"\s+", " ", (text or "").replace("\u2019", "'")).strip().split(" "):
        raw = w
        w = re.sub(r"[^A-Za-z0-9'!?&-]", "", w).upper()
        if w:
            words.append((w, bool(re.search(r"[.!?,;:]$", raw))))
    if not words:
        return []
    size = SHORTS_FONT_SIZE
    max_px = WIDTH * SHORTS_MAX_WIDTH
    gap = size * 0.28
    cap_h = size * 0.74                      # measured: capitals are 0.74 x the font size high
    pitch = size * 1.08
    timed = CAPTION_TIMING.get(tag)
    if timed and [t[0] for t in timed] == [w for w, _ in words]:
        spans = [(max(s - CAPTION_LEAD, 0.0), min(e, duration)) for (_, s, e) in timed]
        spans = [(s, max(e, s + 0.05)) for s, e in spans]
    else:
        # fallback estimate (only when no real timing is known): syllable weights over the spoken part of the scene
        speech = max(duration - SCENE_TAIL_SECONDS, 0.5)
        weights = [_weight(w) for w, stop in words]
        total = float(sum(weights)) or 1.0
        t, spans = 0.0, []
        for wt in weights:
            spans.append((t, t + speech * wt / total))
            t += speech * wt / total
    # 1) group the words into screens of up to SHORTS_WORDS words (a screen also ends after . , ! ? ; :) with at most 2 lines
    def layout(group):
        lines, cur, cur_w = [], [], 0.0
        for idx in group:
            ww = _cap_word_width(words[idx][0], size)
            add = ww if not cur else gap + ww
            if cur and cur_w + add > max_px:
                lines.append(cur); cur, cur_w = [idx], ww
            else:
                cur.append(idx); cur_w += add
        if cur:
            lines.append(cur)
        return lines
    groups, cur = [], []
    for i, (w, stop) in enumerate(words):
        cur.append(i)
        if len(layout(cur)) > 2:
            cur.pop()
            groups.append(cur); cur = [i]
        if len(cur) >= SHORTS_WORDS or stop:
            groups.append(cur); cur = []
    if cur:
        groups.append(cur)
    fontfile = os.path.join(tmp, "title_font.otf")
    if not os.path.exists(fontfile):
        fontfile = _title_font_file(tmp)[0]
    padx, pady = size * 0.17, size * 0.13
    out = []
    for g, group in enumerate(groups):
        lines = layout(group)
        block_h = (len(lines) - 1) * pitch + cap_h
        top = HEIGHT * SHORTS_Y - block_h / 2.0
        c0, c1 = spans[group[0]][0], spans[group[-1]][1]
        c1 = c1 + (0.25 if g + 1 < len(groups) else 0.2)      # the white phrase stays a moment after its last word
        if g + 1 < len(groups):
            c1 = min(c1, spans[groups[g + 1][0]][0])
        boxes, texts = [], []
        for li, line in enumerate(lines):
            widths = [_cap_word_width(words[i][0], size) for i in line]
            line_w = sum(widths) + gap * (len(line) - 1)
            x = (WIDTH - line_w) / 2.0
            cap_top_y = top + li * pitch
            for i, ww in zip(line, widths):
                s0, s1 = spans[i]
                boxes.append("drawbox=x=%d:y=%d:w=%d:h=%d:color=%s:t=fill:enable='between(t,%.3f,%.3f)'" % (
                    round(x - padx), round(cap_top_y - pady), round(ww + 2 * padx), round(cap_h + 2 * pady), SHORTS_BOX_COLOR, s0, s1))
                tf = os.path.join(tmp, f"{tag}_cap{g}_{i}.txt")
                with open(tf, "w", encoding="utf-8") as f:
                    f.write(words[i][0])
                parts = []
                if fontfile:
                    parts.append(f"fontfile={fontfile}")
                parts += [f"textfile={tf}", "expansion=none", f"fontcolor={SHORTS_TEXT_COLOR}", f"fontsize={size}",
                          "borderw=8", "bordercolor=black",
                          f"x={round(x)}", f"y={round(cap_top_y)}",              # drawtext puts the TOP of the capitals at y
                          f"enable='between(t,{c0:.3f},{c1:.3f})'"]
                texts.append("drawtext=" + ":".join(parts))
                x += ww + gap
        out += boxes + texts       # boxes first, so the box sits behind the words
    return out


def subtitle_filters(text, duration, tmp, tag):
    """Return a list of drawtext filters, each shown only during its time slot."""
    if not SUBTITLES:
        return []
    if SHORTS_CAPTIONS and HEIGHT > WIDTH:
        return shorts_caption_filters(text, duration, tmp, tag)
    chunks = split_subtitle_chunks(text)
    if not chunks:
        return []
    speech = max(duration - SCENE_TAIL_SECONDS, 0.5)
    total_chars = sum(len(c) for c in chunks) or 1
    font = find_font()
    filters = []
    t = 0.0
    for k, chunk in enumerate(chunks):
        span = speech * (len(chunk) / total_chars)
        start, end = t, t + span
        t = end
        wrapped = "\n".join(textwrap.wrap(chunk, width=SUB_WRAP_CHARS))
        tf = os.path.join(tmp, f"{tag}_sub{k}.txt")
        with open(tf, "w", encoding="utf-8") as f:
            f.write(wrapped)
        parts = []
        if font:
            parts.append(f"fontfile={font}")
        parts += [
            f"textfile={tf}",
            "expansion=none",
            "fontcolor=white",
            f"fontsize={SUB_FONT_SIZE}",
            "borderw=3",
            "bordercolor=black@0.9",
            "box=1",
            "boxcolor=black@0.45",
            "boxborderw=18",
            "line_spacing=10",
            "x=(w-text_w)/2",
            f"y=h-th-{SUB_BOTTOM_MARGIN}",
            f"enable='between(t,{start:.3f},{end:.3f})'",
        ]
        filters.append("drawtext=" + ":".join(parts))
    return filters


def watermark_filter():
    """A small see-through brand name in one corner of every scene (same spot for the whole film)."""
    if not WATERMARK_TEXT:
        return []
    short = min(WIDTH, HEIGHT)
    size = max(18, int(short * WATERMARK_SIZE))
    m = max(16, int(short * 0.035))
    opacity = WATERMARK_OPACITY
    place = "center" if (WATERMARK_CENTER_FOR_WIDE and WIDTH > HEIGHT) else WATERMARK_POSITION
    if place == "center":
        size = max(24, int(short * WATERMARK_CENTER_SIZE))
        opacity = WATERMARK_CENTER_OPACITY
    pos = {
        "center": ("(w-text_w)/2", "(h-text_h)/2"),
        "top-right": (f"w-text_w-{m}", f"{m}"),
        "top-left": (f"{m}", f"{m}"),
        "bottom-right": (f"w-text_w-{m}", f"h-text_h-{m}"),
        "bottom-left": (f"{m}", f"h-text_h-{m}"),
    }.get(place, (f"w-text_w-{m}", f"{m}"))
    safe = WATERMARK_TEXT.replace("\\", "").replace("'", "").replace(":", "").replace("%", "")
    parts = []
    font = find_font()
    if font:
        parts.append(f"fontfile={font}")
    parts += [
        f"text='{safe}'",
        "expansion=none",
        f"fontsize={size}",
        f"fontcolor=white@{opacity}",
        f"shadowcolor=black@{opacity * 0.6:.2f}",
        "shadowx=2", "shadowy=2",
        f"x={pos[0]}", f"y={pos[1]}",
    ]
    return ["drawtext=" + ":".join(parts)]


# ---------------- CINEMA CAMERA: 2.5D depth parallax + film transitions (v68) ----------------
# Every still picture gets a depth map (what is near, what is far). The camera then really moves THROUGH the picture:
# near things slide/grow more than far things (parallax), far things go soft (depth of field), and the end of each scene
# is turned into a real transition into the next picture (zoom-through, whip, spin, light flash, dark push, passing-object wipe, dissolve).
# Needs: pip install numpy opencv-python-headless onnxruntime   (without them the film is built the old way, nothing breaks)
CINEMA = True                  # False = the older slow zoom on every scene (a manifest with "cinema": false does the same)
CINEMA_TRANSITION_SECONDS = 0.55   # length of the transition at the end of a scene
CINEMA_PARALLAX = 0.050        # sideways travel of the nearest parts, as a share of the picture width (bigger = more 3D but more stretching)
CINEMA_SOFT_EDGE = 70.0        # bigger number = sharper depth edges (more 3D, more tearing); smaller = softer, safer
CINEMA_BASE_ZOOM = 1.12        # the picture is enlarged this much so the camera can move without showing edges
CINEMA_DOF = False             # far parts (and a very near foreground) go soft, like a real lens
CINEMA_GRAIN = 0.015           # film grain strength (0 = off)
CINEMA_VIGNETTE = 0.22         # dark corners (0 = off)
CINEMA_GRADE = 1.0             # v75: cinematic colour grade - teal shadows, warm highlights, filmic contrast (0 = off, 1 = full, 0.5 = half)
CINEMA_SATURATION = 1.06       # colour richness after the grade (1.0 = unchanged)
CINEMA_HEAD_BIAS = 0.5           # v79: the camera window sits higher on the picture so heads are not cut off at the top (0 = centred, 1 = top edge)
CINEMA_SHARPEN = 0.6            # v78: sharpening of every frame (0 = off, 0.6 = clear faces, 1.0 = very crisp)
CINEMA_TRANSITION_RELAX = 0.85  # v77: before a transition the 3D camera shift is eased back to flat by this much (0 = off, 1 = fully flat). This stops heads growing and arms thinning while the scene changes
CINEMA_WHIP_BLUR = 0.05          # v81: blur of the slide/whip transition as a share of the picture size (was 0.18, which smeared faces: the 'melting'). 0 = no blur
CINEMA_LEADIN_SECONDS = 0.7     # v81: the camera starts calming this long BEFORE the transition, so the picture is already steady when the slide begins
CINEMA_LEADIN_RELAX = 0.45      # v81: how much of the 3D shift is removed during that lead-in (0 = none)
CINEMA_WHOOSH = True           # a quiet whoosh / boom is added at each transition
CINEMA_DEPTH_URLS = ["https://github.com/isl-org/MiDaS/releases/download/v2_1/model-small.onnx"]   # free depth model (runs on the CPU)
CINEMA_AUTO_CAMERAS = [["push"], ["truck_r", "push"], ["orbit_l"], ["crane_up", "push"], ["pull"], ["orbit_r", "push"], ["truck_l"], ["crane_down", "pull"]]
CINEMA_AUTO_TRANSITIONS = ["push_through", "whip", "dissolve", "flash", "spin", "cover"]
try:
    import numpy as np
    import cv2
    _CIN_LIBS = True
except Exception:
    _CIN_LIBS = False
_CIN = {"sess": None, "tried": False, "states": {}}

_CAM_DELTA = {
    "push": {"zoom": 0.05, "dz": 0.16}, "pull": {"zoom": -0.05, "dz": -0.16},
    "truck_l": {"tx": -1.0}, "truck_r": {"tx": 1.0},
    "orbit_l": {"tx": -1.25, "roll": 1.6, "zoom": 0.025}, "orbit_r": {"tx": 1.25, "roll": -1.6, "zoom": 0.025},
    "crane_up": {"ty": -1.0, "zoom": 0.02}, "crane_down": {"ty": 1.0, "zoom": 0.02},
    "roll_l": {"roll": -5.0, "zoom": 0.06}, "roll_r": {"roll": 5.0, "zoom": 0.06},
}
_CAM_WORDS = [
    ("orbit", r"orbit\w*|circl\w*|\barcs?\b|\barcing\b|curved|revolv\w*|rotate around|around (?:him|her|them|the)"),
    ("push", r"dolly|\bpush\w*|(?:travel|move|moves|moving|glide|glides|gliding|go|goes)\s+forward|\bforward\b|\bcloser\b|\btoward\w*|zoom\w*\s+in|\bcreep\w*|\bapproach\w*"),
    ("pull", r"pull\w*\s+(?:back|away|out)|\bpulls?\b|\bpulling\b|\bbackwards?\b|zoom\w*\s+out|\bretreat\w*"),
    ("truck", r"\bpan\w*|\btrack\w*|sideways|lateral\w*|\bslid\w+|\bslide\w*|\btruck\w*|\bpast\b|\bacross\b"),
    ("crane_up", r"\bcrane\w*|\brise\w*|\brising\b|upward\w*|tilt\w*\s+up|ascend\w*|\blift\w*"),
    ("crane_down", r"descend\w*|downward\w*|tilt\w*\s+down|\blower\w*|\bsink\w*"),
    ("roll", r"\broll\w*|\bdutch\b|\btilted\b|\brotat\w*|\bspin\w*|\btwist\w*"),
]


def _cin_side(window, idx, k):
    w = window.lower()
    if re.search(r"counter|anti-?clockwise|\bleft\b", w):
        return "l"
    if re.search(r"clockwise|\bright\b", w):
        return "r"
    return "r" if (idx + k) % 2 == 0 else "l"


def parse_camera(text, idx):
    """CAMERA line -> up to 3 camera moves in the order they are written. [(name, strength)]"""
    t = (text or "").lower()
    found = []
    for name, pat in _CAM_WORDS:
        for m in re.finditer(pat, t):
            found.append((m.start(), name))
    found.sort()
    moves, last = [], {}
    for pos, name in found:
        if name in last and pos - last[name] < 60:
            continue
        last[name] = pos
        k = len(moves)
        win = t[max(0, pos - 30): pos + 60]
        if name in ("push", "pull"):
            moves.append(name)
        elif name == "truck":
            moves.append("truck_" + _cin_side(win, idx, k))
        elif name == "orbit":
            moves.append("orbit_" + _cin_side(win, idx, k))
        elif name == "roll":
            moves.append("roll_" + _cin_side(win, idx, k))
        else:
            moves.append(name)
        if len(moves) >= 3:
            break
    if not moves:
        moves = list(CINEMA_AUTO_CAMERAS[idx % len(CINEMA_AUTO_CAMERAS)])
    strength = 1.0
    if re.search(r"\bslow\w*|\bgentle\w*|\bsubtle\w*|\bbarely\b", t):
        strength = 0.8
    if re.search(r"\bfast\b|\brapid\w*|\bsudden\w*|\bdramatic\w*|\baccelerat\w*|\baggressive\w*|\bsweep\w*", t):
        strength = 1.3
    return moves, strength


NO_TRANSITIONS_ALL = False


def parse_transition(text, idx):
    """TRANSITION line -> (kind, direction). kind: dark_push, cover, dissolve, whip, spin, flash, push_through."""
    t = (text or "").lower()
    d = None
    if re.search(r"\bleft\b", t):
        d = "l"
    elif re.search(r"\bright\b", t):
        d = "r"
    elif re.search(r"\bup(?:ward)?\b|\babove\b", t):
        d = "u"
    elif re.search(r"\bdown(?:ward)?\b|\bbelow\b", t):
        d = "d"
    if d is None:
        d = "l" if idx % 2 == 0 else "r"
    global NO_TRANSITIONS_ALL
    if re.search(r"\b(?:no|none|without|skip|off)\b.*\b(?:all|every|whole|entire)\b|\b(?:all|every|whole|entire)\b.*\b(?:no|none|without|skip|off)\b", t) and re.search(r"\b(?:no|none|without|skip|off|cut)\b", t):
        NO_TRANSITIONS_ALL = True                 # "TRANSITION: none for all" in any scene = a hard cut everywhere in this film
    if NO_TRANSITIONS_ALL:
        return "cut", d
    if re.search(r"^\s*(?:none|no transitions?|no|off|nothing|hard cut|straight cut|plain cut|just cut|cut)\s*[.!]?\s*$|\b(?:no|without) transitions?\b|\bhard cut\b|\bstraight cut\b|\bplain cut\b|\bjust cut\b", t):
        return "cut", d
    if not t.strip():
        return CINEMA_AUTO_TRANSITIONS[idx % len(CINEMA_AUTO_TRANSITIONS)], d
    if re.search(r"dark|black|void|shadow", t) and re.search(r"\bpush\w*|expand\w*|swallow\w*|engulf\w*", t):
        return "dark_push", d
    if re.search(r"match.?cut|dissolve|melt|morph|cross.?fade|blend", t):
        return "dissolve", d
    if re.search(r"vehicle|passing|foreground (?:wipe|object)|\bbus\b|\btruck passes", t):
        return "cover", d
    if re.search(r"whip|swipe|\bpan\b|wipe|sweep|track(?:s|ing)? across|slide|rapid(?:ly)? (?:pan|move)", t):
        return "whip", d
    if re.search(r"spin|rotat|\broll|twist|spiral|swirl|orbit", t):
        return "spin", d
    if re.search(r"flash|flare|bright|glow|burst|explo|sunlight|light fill|light rays?", t):
        return "flash", d
    if re.search(r"push|zoom|through|\binto\b|dive|plunge|dolly", t):
        return "push_through", d
    return "dissolve", d


def _ease(x):
    x = min(max(x, 0.0), 1.0)
    return x * x * (3 - 2 * x)


def cin_pose_at(moves, strength, t):
    """The camera position at time t (0..1 of the scene)."""
    pose = {"tx": 0.0, "ty": 0.0, "zoom": 0.0, "dz": 0.0, "roll": 0.0}
    n = len(moves)
    slices = {1: [(0.0, 1.0)], 2: [(0.0, 0.62), (0.38, 1.0)], 3: [(0.0, 0.46), (0.3, 0.76), (0.54, 1.0)]}[n]
    for name, (a, b) in zip(moves, slices):
        delta = _CAM_DELTA[name]
        u = _ease((t - a) / (b - a))
        for key, val in delta.items():
            val = val * strength
            pose[key] += val * u
            if val < 0 and key in ("zoom", "dz"):   # a pull-back starts from the big (zoomed in) value and comes down
                pose[key] += -val
    pose["tx"] = max(-1.7, min(1.7, pose["tx"]))
    pose["ty"] = max(-1.3, min(1.3, pose["ty"]))
    pose["roll"] = max(-7.0, min(7.0, pose["roll"]))
    return pose


def cin_pose_plan(plan, t):
    if plan.get("static"):
        return {"tx": 0.0, "ty": 0.0, "zoom": 0.0, "dz": 0.0, "roll": 0.0}
    return cin_pose_at(plan["moves"], plan["strength"], t)


def _cin_session(tmp):
    if _CIN["tried"]:
        return _CIN["sess"]
    _CIN["tried"] = True
    try:
        import onnxruntime as ort
    except Exception:
        print("::warning::onnxruntime is not installed - the camera uses a simple depth guess (add onnxruntime to the pip install line for real depth)")
        return None
    path = os.path.join(os.environ.get("CINEMA_MODEL_DIR") or tmp, "depth_midas_small.onnx")
    try:
        if not (os.path.exists(path) and os.path.getsize(path) > 10_000_000):
            ok = False
            for url in CINEMA_DEPTH_URLS:
                try:
                    r = requests.get(url, timeout=(20, 300), allow_redirects=True)
                    if r.status_code == 200 and len(r.content) > 10_000_000:
                        with open(path, "wb") as f:
                            f.write(r.content)
                        ok = True
                        break
                except Exception as e:
                    print(f"::warning::depth model download failed ({e})")
            if not ok:
                return None
        _CIN["sess"] = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
        print("depth model loaded")
    except Exception as e:
        print(f"::warning::depth model could not start ({e}) - using a simple depth guess")
        _CIN["sess"] = None
    return _CIN["sess"]


def _cin_fake_depth(img):
    """Fallback when there is no depth model: lower parts are nearer, the centre of interest is nearer."""
    h, w = img.shape[:2]
    yy = np.linspace(0, 1, h, dtype=np.float32)[:, None] * np.ones((1, w), np.float32)
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    detail = cv2.GaussianBlur(np.abs(g - cv2.GaussianBlur(g, (0, 0), 6)), (0, 0), max(h, w) / 25.0)
    detail = detail / (detail.max() + 1e-6)
    xx = (np.linspace(-1, 1, w, dtype=np.float32)[None, :] * np.ones((h, 1), np.float32))
    centre = np.exp(-(xx ** 2) / 0.35 - ((yy - 0.55) ** 2) / 0.25)
    return 0.35 * yy + 0.35 * detail + 0.30 * centre


def cin_depth(img, tmp):
    """Depth of a picture, 0 = far ... 1 = near."""
    h, w = img.shape[:2]
    d = None
    sess = _cin_session(tmp)
    if sess is not None:
        try:
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
            x = cv2.resize(rgb, (256, 256), interpolation=cv2.INTER_AREA)
            x = (x - np.array([0.485, 0.456, 0.406], np.float32)) / np.array([0.229, 0.224, 0.225], np.float32)
            x = np.ascontiguousarray(x.transpose(2, 0, 1)[None].astype(np.float32))
            out = sess.run(None, {sess.get_inputs()[0].name: x})[0]
            d = cv2.resize(np.squeeze(out).astype(np.float32), (w, h), interpolation=cv2.INTER_CUBIC)
        except Exception as e:
            print(f"::warning::depth estimation failed ({e}) - using a simple depth guess")
            d = None
    if d is None:
        d = _cin_fake_depth(img)
    lo, hi = np.percentile(d, [2, 98])
    d = np.clip((d - lo) / max(hi - lo, 1e-6), 0, 1).astype(np.float32)
    d = cv2.bilateralFilter(d, 9, 0.08, 15)
    d = cv2.GaussianBlur(d, (0, 0), max(h, w) / 160.0)
    return np.clip(d, 0, 1).astype(np.float32)


def _cin_dof(img, D):
    if not CINEMA_DOF:
        return img
    sc = img.shape[1] / 1080.0
    b1 = cv2.GaussianBlur(img, (0, 0), 2.5 * sc).astype(np.float32)
    b2 = cv2.GaussianBlur(img, (0, 0), 6.0 * sc).astype(np.float32)
    far = np.clip((0.38 - D) / 0.38, 0, 1) * 1.5
    near = np.clip((D - 0.9) / 0.1, 0, 1) * 1.2
    lvl = np.maximum(far, near)
    w0 = np.clip(1 - lvl, 0, 1)[..., None]
    w1 = np.clip(1 - np.abs(lvl - 1), 0, 1)[..., None]
    w2 = np.clip(lvl - 1, 0, 1)[..., None]
    out = img.astype(np.float32) * w0 + b1 * w1 + b2 * w2
    return np.clip(out, 0, 255).astype(np.uint8)


class _CinState:
    pass


def cin_state(scene_index, image_path, tmp, static=False):
    st = _CIN["states"].get(scene_index)
    if st is not None:
        return st
    if static:                                   # NO CAMERA scene: the picture is shown as it is (no depth blur, no zoom)
        composite = os.path.join(tmp, f"cinecomp_{scene_index:03d}.png")
        build_composite(image_path, composite)
        img = cv2.imread(composite)
        if img is None:
            raise RuntimeError("cinema: the picture could not be read")
        if img.shape[1] != WIDTH or img.shape[0] != HEIGHT:
            img = cv2.resize(img, (WIDTH, HEIGHT), interpolation=cv2.INTER_AREA)
        st = _CinState()
        st.W, st.H, st.src, st.static = WIDTH, HEIGHT, img, True
        for k in [k for k in _CIN["states"] if k < scene_index - 1]:
            del _CIN["states"][k]
        _CIN["states"][scene_index] = st
        return st
    composite = os.path.join(tmp, f"cinecomp_{scene_index:03d}.png")
    build_composite(image_path, composite)
    img = cv2.imread(composite)
    if img is None:
        raise RuntimeError("cinema: the picture could not be read")
    if img.shape[1] != WIDTH or img.shape[0] != HEIGHT:
        img = cv2.resize(img, (WIDTH, HEIGHT), interpolation=cv2.INTER_AREA)
    st = _CinState()
    st.W, st.H = WIDTH, HEIGHT
    st.D = cin_depth(img, tmp)
    st.src = _cin_dof(img, st.D)
    st.Dd = (0.25 + 0.6 * cv2.GaussianBlur(st.D, (0, 0), max(WIDTH, HEIGHT) / CINEMA_SOFT_EDGE)).astype(np.float32)   # softened + compressed depth that drives the camera shifts
    st.cx, st.cy = WIDTH / 2.0, HEIGHT / 2.0
    xs = np.arange(WIDTH, dtype=np.float32) - st.cx
    ys = np.arange(HEIGHT, dtype=np.float32) - st.cy
    st.Xr = np.ascontiguousarray(np.broadcast_to(xs[None, :], (HEIGHT, WIDTH)))
    st.Yr = np.ascontiguousarray(np.broadcast_to(ys[:, None], (HEIGHT, WIDTH)))
    for k in [k for k in _CIN["states"] if k < scene_index - 1]:
        del _CIN["states"][k]
    _CIN["states"][scene_index] = st
    return st


def cin_render(st, pose):
    """One frame of the picture seen from the camera position `pose`.
    The shifts follow a softened copy of the depth (st.Dd): hard depth edges would tear faces and buildings apart."""
    if getattr(st, "static", False):
        return st.src.copy()
    Dd = st.Dd
    ang = math.radians(pose["roll"])
    c, s = math.cos(ang), math.sin(ang)
    xr = st.Xr * c - st.Yr * s
    yr = st.Xr * s + st.Yr * c
    inv = 1.0 / (CINEMA_BASE_ZOOM + pose["zoom"] + pose["dz"] * Dd)
    par = Dd - 0.2
    mx = (xr * inv + st.cx + (pose["tx"] * CINEMA_PARALLAX * st.W) * par).astype(np.float32)
    _z = CINEMA_BASE_ZOOM + pose["zoom"] + pose["dz"] * 0.5
    _up = (1.0 - 1.0 / _z) / 2.0 * st.H * CINEMA_HEAD_BIAS     # v79: move the window up, so the top of heads stays in the picture
    my = (yr * inv + st.cy - _up + (pose["ty"] * CINEMA_PARALLAX * st.H * 0.55) * par).astype(np.float32)
    return cv2.remap(st.src, mx, my, cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT_101)


# ---- transitions: blend the last frames of scene A into the first picture of scene B ----
def _zoom_img(img, scale, cx=None, cy=None):
    h, w = img.shape[:2]
    cx = w / 2.0 if cx is None else cx
    cy = h / 2.0 if cy is None else cy
    M = np.array([[scale, 0, cx - scale * cx], [0, scale, cy - scale * cy]], np.float32)
    return cv2.warpAffine(img, M, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT_101)


def _zoom_blur(img, amount, n=6):
    if amount <= 0.004:
        return img
    acc = np.zeros(img.shape, np.float32)
    for k in range(n):
        acc += _zoom_img(img, 1.0 + amount * k / (n - 1)).astype(np.float32)
    return (acc / n).astype(np.uint8)


def _rot_img(img, deg, scale):
    h, w = img.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), deg, scale)
    return cv2.warpAffine(img, M, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT_101)


def _rot_blur(img, deg, scale, spread, n=5):
    if abs(spread) < 0.3:
        return _rot_img(img, deg, scale)
    acc = np.zeros(img.shape, np.float32)
    for k in range(n):
        acc += _rot_img(img, deg + spread * (k / (n - 1) - 0.5), scale).astype(np.float32)
    return (acc / n).astype(np.uint8)


def _mix(a, b, alpha):
    if alpha <= 0:
        return a
    if alpha >= 1:
        return b
    return cv2.addWeighted(a, 1.0 - alpha, b, alpha, 0)


def tr_dissolve(A, B, p, d):
    e = _ease(p)
    return _mix(_zoom_img(A, 1.0 + 0.04 * e), B, e)


def tr_whip(A, B, p, d):
    e = 1 - (1 - p) ** 2.2                      # fast start, soft landing
    h, w = A.shape[:2]
    if d in ("l", "r"):
        strip = np.hstack([A, B]) if d == "l" else np.hstack([B, A])
        off = int(round(e * w)) if d == "l" else int(round((1 - e) * w))
        out = strip[:, off: off + w]
        k = int(2 + CINEMA_WHIP_BLUR * w * math.sin(math.pi * min(max(p, 0), 1)) ** 1.2) | 1
        return cv2.blur(out, (k, 1)) if k > 3 else out
    strip = np.vstack([A, B]) if d == "u" else np.vstack([B, A])
    off = int(round(e * h)) if d == "u" else int(round((1 - e) * h))
    out = strip[off: off + h]
    k = int(2 + CINEMA_WHIP_BLUR * h * math.sin(math.pi * min(max(p, 0), 1)) ** 1.2) | 1
    return cv2.blur(out, (1, k)) if k > 3 else out


def tr_push_through(A, B, p, d):
    e = _ease(p)
    a = _zoom_blur(_zoom_img(A, 1.0 + 1.4 * e * e), 0.30 * math.sin(math.pi * min(p * 1.15, 1.0)))
    bs = 0.62 + 0.38 * e
    b = _zoom_blur(_zoom_img(B, bs), 0.22 * (1 - p))
    return _mix(a, b, _ease((p - 0.30) / 0.45))


def tr_dark_push(A, B, p, d):
    e = _ease(p)
    a = _zoom_img(A, 1.0 + 1.3 * e)
    a = cv2.convertScaleAbs(a, alpha=max(0.0, 1.0 - _ease(p / 0.6)))
    bq = _ease((p - 0.45) / 0.55)
    b = _zoom_img(B, 1.0 + 0.25 * (1 - bq))
    b = cv2.convertScaleAbs(b, alpha=bq)
    return cv2.add(a, b)


def tr_flash(A, B, p, d):
    white = np.empty_like(A)
    white[:] = (205, 235, 255)
    if p < 0.5:
        return _mix(_zoom_img(A, 1.0 + 0.08 * p), white, _ease(p / 0.5))
    return _mix(white, _zoom_img(B, 1.0 + 0.06 * (1 - p)), _ease((p - 0.5) / 0.5))


def tr_spin(A, B, p, d):
    sgn = 1.0 if d in ("l", "u") else -1.0
    uA, uB = _ease(p / 0.5), _ease((p - 0.5) / 0.5)
    a = _rot_blur(A, sgn * 45 * uA, 1.0 + 0.5 * uA, 26 * math.sin(math.pi * min(uA, 1)) * sgn)
    b = _rot_blur(B, -sgn * 45 * (1 - uB), 1.0 + 0.5 * (1 - uB), 26 * math.sin(math.pi * min(1 - uB, 1)) * -sgn)
    return _mix(a, b, _ease((p - 0.42) / 0.16))


def tr_cover(A, B, p, d):
    flip = d in ("l", "u")
    if flip:
        A, B = A[:, ::-1], B[:, ::-1]
    h, w = A.shape[:2]
    bw = 0.62 * w
    left = _ease(p) * (w + bw) - bw
    x = np.arange(w, dtype=np.float32)
    centre = left + bw / 2.0
    mB = np.clip((centre - x) / (0.12 * w) + 0.5, 0, 1)[None, :, None]
    band = np.clip(1.0 - np.abs(x - centre) / (bw / 2.0), 0, 1) ** 0.35
    dark = (1.0 - 0.93 * band)[None, :, None]
    out = (A.astype(np.float32) * (1 - mB) + B.astype(np.float32) * mB) * dark
    out = np.clip(out, 0, 255).astype(np.uint8)
    k = int(2 + 0.05 * w * math.sin(math.pi * min(max(p, 0), 1))) | 1
    if k > 3:
        out = cv2.blur(out, (k, 1))
    return np.ascontiguousarray(out[:, ::-1]) if flip else out


_TRANSITIONS = {"dissolve": tr_dissolve, "whip": tr_whip, "push_through": tr_push_through, "dark_push": tr_dark_push,
                "flash": tr_flash, "spin": tr_spin, "cover": tr_cover}


def cin_finish_tools(W, H):
    """Vignette and film grain, prepared once."""
    tools = {}
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    r = np.sqrt(((xx - W / 2) / (W / 2)) ** 2 + ((yy - H / 2) / (H / 2)) ** 2) / math.sqrt(2)
    v = np.clip(1.0 - CINEMA_VIGNETTE * r ** 2.2, 0, 1)
    tools["vig"] = np.repeat((v * 255).astype(np.uint8)[:, :, None], 3, axis=2)
    rng = np.random.RandomState(7)
    tools["grain"] = [(rng.randn(H, W, 1) * CINEMA_GRAIN * 255).astype(np.int16) for _ in range(6)]
    xs = np.arange(256, dtype=np.float32) / 255.0
    g = max(0.0, min(float(CINEMA_GRADE), 1.0))
    base = np.clip(0.5 + (xs - 0.5) * 1.06 + 0.012, 0, 1)         # a touch more contrast
    if g > 0:
        # filmic S-curve: deeper shadows, bright but rolled-off highlights, blacks slightly lifted like film stock
        sc = xs * xs * (3 - 2 * xs)
        curve = 0.55 * base + 0.45 * ((1 - 0.5 * g) * base + 0.5 * g * sc)
        curve = np.clip(0.014 * g + curve * (1 - 0.014 * g) - 0.02 * g * np.clip((xs - 0.85) / 0.15, 0, 1) ** 2, 0, 1)
        sh, hi = (1 - xs) ** 2, xs ** 2                            # shadow weight, highlight weight
        r = curve - 0.036 * g * sh + 0.040 * g * hi                # orange in the lights, less red in the dark
        gr = curve + 0.006 * g * sh - 0.006 * g * hi
        b = curve + 0.046 * g * sh - 0.042 * g * hi                # teal in the dark, less blue in the lights
        lut = np.stack([b, gr, r], axis=1)                         # OpenCV frames are B, G, R
        tools["lut"] = (np.clip(lut, 0, 1) * 255).astype(np.uint8).reshape(256, 1, 3)
    else:
        tools["lut"] = (base * 255).astype(np.uint8)
    return tools


def cin_finish(frame, tools, k, clean=False):
    if clean and CINEMA_STATIC_CLEAN:             # NO CAMERA scene: nothing that could soften small screen text
        return frame
    if CINEMA_SHARPEN > 0:
        blur = cv2.GaussianBlur(frame, (0, 0), 1.2 * max(frame.shape[1], frame.shape[0]) / 1920.0 + 0.4)
        frame = cv2.addWeighted(frame, 1.0 + CINEMA_SHARPEN, blur, -CINEMA_SHARPEN, 0)
    if CINEMA_VIGNETTE > 0:
        frame = cv2.multiply(frame, tools["vig"], scale=1 / 255.0)
    frame = cv2.LUT(frame, tools["lut"])
    if CINEMA_GRADE > 0 and abs(CINEMA_SATURATION - 1.0) > 0.01:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.merge([gray, gray, gray])
        frame = cv2.addWeighted(frame, CINEMA_SATURATION, gray, 1.0 - CINEMA_SATURATION, 0)
    if CINEMA_GRAIN > 0:
        frame = np.clip(frame.astype(np.int16) + tools["grain"][k % 6], 0, 255).astype(np.uint8)
    return frame


def _whoosh_audio(audio_path, duration, td, kind, tmp, tag):
    """The scene's voice plus a quiet whoosh (and a low boom for flashes and dark pushes) at the transition."""
    out = os.path.join(tmp, f"{tag}_whoosh.wav")
    t0 = max(duration - td - 0.05, 0.0)
    ms = int(t0 * 1000)
    parts = [f"[1:a]afade=t=in:d=0.30:curve=qsin,afade=t=out:st=0.30:d=0.40:curve=exp,highpass=f=350,lowpass=f=7000,volume=0.20,adelay={ms}:all=1[w]"]
    inputs = ["-i", audio_path, "-f", "lavfi", "-t", "0.8", "-i", "anoisesrc=d=0.8:c=pink:a=0.6:r=44100"]
    mix = "[0:a][w]"
    n = 2
    if kind in ("flash", "dark_push", "push_through"):
        parts.append(f"[2:a]afade=t=out:st=0.10:d=0.50:curve=exp,volume=0.30,adelay={ms + int(td * 450)}:all=1[b]")
        inputs += ["-f", "lavfi", "-t", "0.7", "-i", "sine=f=48:d=0.7:r=44100"]
        mix += "[b]"
        n = 3
    fc = ";".join(parts) + f";{mix}amix=inputs={n}:duration=first:dropout_transition=0:normalize=0[o]"
    try:
        run(["ffmpeg", "-y"] + inputs + ["-filter_complex", fc, "-map", "[o]", "-ar", "44100", "-ac", "2", out])
        return out
    except Exception:
        try:
            run(["ffmpeg", "-y"] + inputs + ["-filter_complex", fc.replace(":normalize=0", ""), "-map", "[o]", "-ar", "44100", "-ac", "2", out])
            return out
        except Exception as e:
            print(f"::warning::transition sound skipped ({e})")
            return audio_path


def _script_blocks(manifest):
    """Optional: if the manifest carries the whole script text, read each scene's CAMERA / TRANSITION lines from it."""
    found = {}
    texts = []
    for key in ("script", "script_text", "scriptText", "prompt", "raw_script", "story"):
        v = manifest.get(key)
        if isinstance(v, str) and len(v) > 200:
            texts.append(v)
    for text in texts:
        for m in re.finditer(r"(?im)^\s*\**\s*SCENE\s+(\d+)\s*[:\-]?\**\s*$(.*?)(?=^\s*\**\s*SCENE\s+\d+\s*[:\-]?\**\s*$|\Z)", text, re.S):
            n = int(m.group(1))
            body = m.group(2)
            cam = re.search(r"(?im)^\s*CAMERA\s*:\s*(.+?)(?=^\s*[A-Z_ ]{3,20}\s*:|\Z)", body, re.S)
            tr = re.search(r"(?im)^\s*TRANSITION\s*:\s*(.+?)(?=^\s*[A-Z_ ]{3,20}\s*:|\Z)", body, re.S)
            d = found.setdefault(n, {})
            if cam:
                d["camera"] = cam.group(1).strip()
            if tr:
                d["transition"] = tr.group(1).strip()
        if found:
            break
    return found


def _scene_field(scene, names):
    for n in names:
        v = scene.get(n) if isinstance(scene, dict) else None
        if isinstance(v, str) and v.strip():
            return v
    return ""


def _all_strings(obj, depth=0):
    """Every text value inside a scene (any field name, nested too), so a line is found wherever the parser stored it."""
    out = []
    if depth > 4:
        return out
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            out.extend(_all_strings(v, depth + 1))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out.extend(_all_strings(v, depth + 1))
    return out


def _debug_scene(idx, scene):
    try:
        if isinstance(scene, dict):
            keys = sorted(k for k, v in scene.items() if v not in (None, "", [], {}))
            print(f"scene {idx} fields: {', '.join(keys)}")
            for k in ("sound", "subscribe", "transition", "camera_instruction"):
                if scene.get(k):
                    print(f"scene {idx} {k} = {str(scene.get(k))[:260]!r}")
    except Exception:
        pass


def cinema_plan(idx, scene, meta):
    _debug_scene(idx, scene)
    cam = _scene_field(scene, ("camera", "camera_movement", "camera_motion", "camera_direction", "camera_text")) or (meta or {}).get("camera", "")
    if not cam:                                  # the n8n parser keeps the CAMERA line in camera_instruction / motion_prompt
        cam = _scene_field(scene, ("camera_instruction", "motion_prompt"))
        if cam.strip().lower().startswith("gentle natural movement"):     # that is the parser's own filler, not a script instruction
            cam = ""
    tr = _scene_field(scene, ("transition", "transition_out", "transition_text", "transition_note")) or (meta or {}).get("transition", "")
    if not tr and isinstance(scene, dict):       # the parser attaches the TRANSITION line to the end of the scene's SOUND text
        for _k in ("sound", "motion_prompt", "camera_instruction"):
            _m = re.search(r"(?is)\bTRANSITION\b\s*[:\-\u2013\u2014]\s*(.+?)(?=\s+(?:SUBSCRIBE|SOUND|CAMERA|IMAGE|NARRATION|SECONDS|VOICE|CONTINUITY)\s*[:\-\u2013\u2014]|$)", str(scene.get(_k) or ""))
            if _m:
                tr = _m.group(1).strip()
                break
    if not tr and isinstance(scene, dict):       # last chance: look in every text value of the scene
        for _t in _all_strings(scene):
            _m = re.search(r"(?is)\bTRANSITION\b\s*[:\-\u2013\u2014]\s*(.+?)(?=\s+(?:SUBSCRIBE|SOUND|CAMERA|IMAGE|NARRATION|SECONDS|VOICE|CONTINUITY)\s*[:\-\u2013\u2014]|$)", _t)
            if _m:
                tr = _m.group(1).strip()
                break
    _strip = lambda s: re.sub(r"(?is)\b(?:SUBSCRIBE|TRANSITION|SOUND|CONTINUITY|END)\b\s*[:\-\u2013\u2014].*$", "", s or "")
    cam, tr = _strip(cam), _strip(tr)
    moves, strength = parse_camera(cam, idx)
    kind, d = parse_transition(tr, idx)
    static = bool(re.search(r"(?i)\bno[\s_-]*camera\b|\bcamera\s*[:=-]?\s*(?:off|none|still|static|locked)\b|\b(?:static|fixed|locked[\s-]?off)\s+(?:shot|camera|frame|screen)\b|\bstay(?:s)? still\b|\bnothing moves\b", cam or ""))
    if not static and isinstance(scene, dict):
        _nc = scene.get("no_camera")
        static = (_nc is True) or (isinstance(_nc, str) and _nc.strip().lower() in ("1", "true", "yes"))
    if isinstance(scene, dict) and isinstance(scene.get("quiz"), dict) and scene["quiz"].get("questions"):
        _DEV["quiz"] = scene["quiz"]               # the quiz written in the script (any number of questions)
    dev = parse_dev(scene)
    if dev:
        static = True                             # a live page is never moved by the camera
    return {"dev": dev, "static": static, "overlays": parse_overlays(scene), "moves": moves, "strength": strength, "kind": kind, "dir": d, "cam_text": cam, "tr_text": tr, "subscribe": parse_subscribe(scene), "end": parse_end(scene)}


# ---------------- SUBSCRIBE LABEL (v71): an animated subscribe button, placed by the script ----------------
# In a scene write a line like   SUBSCRIBE: end   /   SUBSCRIBE: middle | Subscribe for more African stories   (start | middle | end)
# No SUBSCRIBE line = nothing is added. Works on the scenes that use the cinema camera.
CINEMA_SUBSCRIBE_SECONDS = 4.6   # how long the label stays on screen
CINEMA_SUBSCRIBE_Y = 0.28        # centre of the label as a share of the picture height (the yellow captions sit lower, around 0.60)
SUBSCRIBE_RED = (226, 28, 28)

_SUB_POS = ("start", "middle", "end")


def parse_subscribe(scene, extra_text=""):
    """Find the script's SUBSCRIBE instruction for a scene. Returns {"pos": ..., "msg": ...} or None."""
    spec = None
    if isinstance(scene, dict):
        v = scene.get("subscribe")
        if isinstance(v, str) and v.strip():
            spec = re.split(r"(?is)\s+(?:TRANSITION|SOUND|CAMERA|IMAGE|NARRATION|SECONDS|VOICE|LABEL)\s*[:\-\u2013\u2014]", v.strip())[0].strip()
        if spec is None:
            for key in ("sound", "transition", "camera_instruction", "motion_prompt", "camera", "label", "end_label"):
                s = scene.get(key)
                if isinstance(s, str):
                    m = re.search(r"(?is)\bSUBSCRIBE\b\s*[:\-–—]\s*(.+?)(?=\s+(?:TRANSITION|SOUND|CAMERA|IMAGE|NARRATION|SECONDS|VOICE|LABEL)\s*[:\-–—]|$)", s)
                    if m:
                        spec = m.group(1).strip()
                        break
    if spec is None and isinstance(scene, dict):
        for _t in _all_strings(scene):
            m = re.search(r"(?is)\bSUBSCRIBE\b\s*[:\-\u2013\u2014]\s*(.+?)(?=\s+(?:TRANSITION|SOUND|CAMERA|IMAGE|NARRATION|SECONDS|VOICE|LABEL|CONTINUITY)\s*[:\-\u2013\u2014]|$)", _t)
            if m:
                spec = m.group(1).strip()
                break
    if not spec:
        return None
    if re.match(r"(?i)^(off|none|no|false)\b", spec):
        return None
    pos, msg = "end", ""
    parts = [p.strip() for p in spec.split("|", 1)]
    head = parts[0].lower()
    m = re.match(r"^(start|begin\w*|middle|mid|centre|center|end|ending|last)\b[\s,.\-:]*(.*)$", head)
    if m:
        w = m.group(1)
        pos = "start" if w.startswith(("start", "begin")) else ("middle" if w in ("middle", "mid", "centre", "center") else "end")
        rest = parts[0][len(m.group(0)) - len(m.group(2)):].strip() if m.group(2) else ""
        msg = (parts[1] if len(parts) > 1 else rest).strip()
    else:
        msg = parts[0] if len(parts) == 1 else (parts[1] or parts[0])
    msg = re.sub(r"[<>{}]", "", msg)[:90].strip(" \"'“”")
    return {"pos": pos, "msg": msg}


def _ease_out_back(x):
    x = min(max(x, 0.0), 1.0)
    c1, c3 = 1.70158, 2.70158
    return 1 + c3 * (x - 1) ** 3 + c1 * (x - 1) ** 2


class _SubLabel:
    """Draws the animation: panel pops in, a cursor clicks SUBSCRIBE, it turns into SUBSCRIBED and the bell rings."""

    def __init__(self, msg, T, W, H):
        from PIL import Image, ImageDraw, ImageFont
        self.Image, self.ImageDraw = Image, ImageDraw
        u = self.u = min(W, H) / 1080.0
        self.T, self.W, self.H = T, W, H
        self.click_t = min(1.85, T * 0.5)
        fpath = find_font()
        SS = 2

        def font(sz):
            try:
                return ImageFont.truetype(fpath, int(sz * SS)) if fpath else ImageFont.load_default()
            except Exception:
                return ImageFont.load_default()

        def down(img):
            return img.resize((max(1, img.width // SS), max(1, img.height // SS)), Image.LANCZOS)

        # message (white, bold, soft shadow), wrapped to at most two lines
        self.msg_img = None
        mh = 0
        if msg:
            fnt = font(58 * u)
            maxw = 860 * u * SS
            words, lines, cur = msg.split(), [], ""
            probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
            for w_ in words:
                t_ = (cur + " " + w_).strip()
                if probe.textlength(t_, font=fnt) <= maxw or not cur:
                    cur = t_
                else:
                    lines.append(cur)
                    cur = w_
            lines.append(cur)
            lines = lines[:2]
            lh = int(78 * u * SS)
            img = Image.new("RGBA", (int(900 * u * SS), lh * len(lines) + int(30 * u * SS)), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            for i, ln in enumerate(lines):
                tw = d.textlength(ln, font=fnt)
                x = (img.width - tw) / 2
                y = int(8 * u * SS) + i * lh
                d.text((x + 3 * u * SS, y + 4 * u * SS), ln, font=fnt, fill=(0, 0, 0, 170))
                d.text((x, y), ln, font=fnt, fill=(255, 255, 255, 255))
            self.msg_img = down(img)
            mh = self.msg_img.height

        bw, bh = int(520 * u), int(120 * u)
        self.bw, self.bh = bw, bh

        def button(color, text, play):
            img = Image.new("RGBA", (bw * SS, bh * SS), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.rounded_rectangle([0, 0, bw * SS - 1, bh * SS - 1], radius=int(30 * u * SS), fill=color + (255,))
            f_ = font(52 * u)
            tw = d.textlength(text, font=f_)
            x0 = (bw * SS - tw) / 2 + (int(34 * u * SS) if play else 0)
            if play:                                        # little white play badge on the left, like YouTube's own button
                px, py, pw, ph = x0 - int(96 * u * SS), (bh * SS - int(44 * u * SS)) // 2, int(62 * u * SS), int(44 * u * SS)
                d.rounded_rectangle([px, py, px + pw, py + ph], radius=int(12 * u * SS), fill=(255, 255, 255, 255))
                d.polygon([(px + pw * 0.40, py + ph * 0.22), (px + pw * 0.40, py + ph * 0.78), (px + pw * 0.68, py + ph * 0.5)], fill=color + (255,))
            d.text((x0, (bh * SS - 52 * u * SS) / 2 - 6 * u * SS), text, font=f_, fill=(255, 255, 255, 255))
            return down(img)
        self.red = button(SUBSCRIBE_RED, "SUBSCRIBE", True)
        self.grey = button((74, 74, 78), "SUBSCRIBED", False)

        # bell on a dark round badge
        bs = int(120 * u)
        self.bs = bs
        circ = Image.new("RGBA", (bs * SS, bs * SS), (0, 0, 0, 0))
        ImageDraw.Draw(circ).ellipse([0, 0, bs * SS - 1, bs * SS - 1], fill=(40, 40, 44, 235))
        self.circle = down(circ)
        g = Image.new("RGBA", (bs * SS, bs * SS), (0, 0, 0, 0))
        d = ImageDraw.Draw(g)
        cx, S = bs * SS / 2, bs * SS
        d.ellipse([cx - S * 0.20, S * 0.24, cx + S * 0.20, S * 0.64], fill=(255, 255, 255, 255))                # dome
        d.polygon([(cx - S * 0.20, S * 0.44), (cx + S * 0.20, S * 0.44), (cx + S * 0.27, S * 0.68), (cx - S * 0.27, S * 0.68)], fill=(255, 255, 255, 255))
        d.rounded_rectangle([cx - S * 0.30, S * 0.64, cx + S * 0.30, S * 0.71], radius=int(S * 0.035), fill=(255, 255, 255, 255))
        d.ellipse([cx - S * 0.07, S * 0.70, cx + S * 0.07, S * 0.82], fill=(255, 255, 255, 255))              # clapper
        d.ellipse([cx - S * 0.04, S * 0.16, cx + S * 0.04, S * 0.25], fill=(255, 255, 255, 255))              # top knob
        self.bell = down(g)

        # cursor (white arrow with a dark outline)
        cs = int(96 * u)
        cur_ = Image.new("RGBA", (cs * SS, cs * SS), (0, 0, 0, 0))
        d = ImageDraw.Draw(cur_)
        pts = [(0.10, 0.04), (0.10, 0.80), (0.31, 0.62), (0.45, 0.93), (0.58, 0.87), (0.44, 0.57), (0.72, 0.57)]
        P = [(x * cs * SS, y * cs * SS) for x, y in pts]
        d.polygon(P, fill=(255, 255, 255, 255), outline=(20, 20, 20, 255))
        d.line(P + [P[0]], fill=(20, 20, 20, 255), width=int(4 * u * SS))
        self.cursor = down(cur_)

        gap = int(40 * u) if mh else 0
        self.CW = int(900 * u)
        self.row_y = mh + gap + int(36 * u)
        self.CH = self.row_y + bh + int(70 * u)
        self.mh = mh
        row_w = bw + int(20 * u) + bs
        self.bx = (self.CW - row_w) // 2
        self.bell_x = self.bx + bw + int(20 * u)

    def draw(self, frame, t):
        Image, u, T = self.Image, self.u, self.T
        A = min(_ease(t / 0.35), _ease((T - t) / 0.35))
        if A <= 0.01:
            return frame
        s = 0.72 + 0.28 * _ease_out_back(t / 0.5) if t < 0.5 else 1.0
        cv = Image.new("RGBA", (self.CW, self.CH), (0, 0, 0, 0))
        if self.msg_img is not None:
            cv.alpha_composite(self.msg_img, ((self.CW - self.msg_img.width) // 2, 0))
        tc = t - self.click_t
        clicked = tc >= 0
        btn = self.grey if clicked else self.red
        if 0 <= tc < 0.12:
            btn = Image.blend(self.red, self.grey, tc / 0.12)
        bx, by, bw, bh = self.bx, self.row_y, self.bw, self.bh
        if 0 <= tc < 0.14:                                    # the press: the button dips a little
            k = 0.93
            sm = btn.resize((int(bw * k), int(bh * k)), Image.LANCZOS)
            cv.alpha_composite(sm, (bx + (bw - sm.width) // 2, by + (bh - sm.height) // 2))
        else:
            cv.alpha_composite(btn, (bx, by))
        if 0 <= tc < 0.5:                                     # ripple ring
            d = self.ImageDraw.Draw(cv)
            r = (30 + 190 * (tc / 0.5)) * u
            a = int(150 * (1 - tc / 0.5))
            cxy = (bx + bw / 2, by + bh / 2)
            ring = Image.new("RGBA", (self.CW, self.CH), (0, 0, 0, 0))
            self.ImageDraw.Draw(ring).ellipse([cxy[0] - r, cxy[1] - r * 0.55, cxy[0] + r, cxy[1] + r * 0.55], outline=(255, 255, 255, a), width=max(2, int(5 * u)))
            cv.alpha_composite(ring)
        # bell: rings after the click
        cv.alpha_composite(self.circle, (self.bell_x, by))
        ang = 0.0
        if 0 <= tc < 1.5:
            ang = 26 * math.exp(-3.0 * tc) * math.sin(2 * math.pi * 5.5 * tc)
        bell = self.bell.rotate(ang, resample=Image.BICUBIC, center=(self.bs / 2, self.bs * 0.18))
        cv.alpha_composite(bell, (self.bell_x, by))
        if clicked:                                           # red notification dot
            ds = int(34 * u)
            dot = Image.new("RGBA", (ds * 2, ds * 2), (0, 0, 0, 0))
            self.ImageDraw.Draw(dot).ellipse([0, 0, ds * 2 - 1, ds * 2 - 1], fill=SUBSCRIBE_RED + (255,), outline=(255, 255, 255, 255), width=max(2, int(4 * u)))
            dot = dot.resize((ds, ds), Image.LANCZOS)
            cv.alpha_composite(dot, (self.bell_x + self.bs - int(ds * 0.8), by - int(ds * 0.2)))
        # cursor travels to the button and clicks
        t0c = self.click_t - 0.7
        if t0c <= t < self.click_t + 0.9:
            q = _ease((t - t0c) / 0.7)
            sx, sy = bx + bw * 0.95, by + bh * 2.2
            ex, ey = bx + bw * 0.62, by + bh * 0.42
            cx_, cy_ = sx + (ex - sx) * q, sy + (ey - sy) * q
            ca = min(1.0, (t - t0c) / 0.2, (self.click_t + 0.9 - t) / 0.3)
            cur = self.cursor.copy()
            cur.putalpha(cur.getchannel("A").point(lambda v: int(v * max(ca, 0))))
            cv.alpha_composite(cur, (int(cx_ - 0.1 * self.cursor.width), int(cy_ - 0.04 * self.cursor.height)))
        if s < 0.999:
            nw, nh = int(self.CW * s), int(self.CH * s)
            sm = cv.resize((nw, nh), Image.LANCZOS)
            cv = Image.new("RGBA", (self.CW, self.CH), (0, 0, 0, 0))
            cv.alpha_composite(sm, ((self.CW - nw) // 2, (self.CH - nh) // 2))
        arr = np.asarray(cv).astype(np.float32)
        al = (arr[..., 3:4] / 255.0) * A
        x0 = int(self.W / 2 - self.CW / 2)
        y0 = int(self.H * CINEMA_SUBSCRIBE_Y - self.CH / 2)
        xa, ya = max(x0, 0), max(y0, 0)
        xb, yb = min(x0 + self.CW, self.W), min(y0 + self.CH, self.H)
        if xb <= xa or yb <= ya:
            return frame
        sub = arr[ya - y0: yb - y0, xa - x0: xb - x0]
        a = al[ya - y0: yb - y0, xa - x0: xb - x0]
        roi = frame[ya:yb, xa:xb].astype(np.float32)
        frame[ya:yb, xa:xb] = np.clip(roi * (1 - a) + sub[..., 2::-1] * a, 0, 255).astype(np.uint8)
        return frame


CINEMA_END_SECONDS = 4.0          # how long the END title stays on screen (at the very end of the last scene)


def parse_end(scene):
    """The script's END line, e.g. 'END: TO BE CONTINUED'. Returns the text, or None."""
    txt = None
    if isinstance(scene, dict):
        v = scene.get("end")
        if isinstance(v, str) and v.strip():
            txt = v.strip()
        if txt is None:
            for _t in _all_strings(scene):
                m = re.search(r"(?:^|\s)END\s*[:\-\u2013\u2014]\s*(.+?)(?=\s+(?:SUBSCRIBE|TRANSITION|SOUND|CAMERA|IMAGE|NARRATION|SECONDS|VOICE|CONTINUITY)\s*[:\-\u2013\u2014]|$)", _t)
                if m:
                    txt = m.group(1).strip()
                    break
    if not txt:
        return None
    txt = re.split(r"(?is)\s+(?:SUBSCRIBE|TRANSITION|SOUND|CAMERA|IMAGE|NARRATION|SECONDS|VOICE)\s*[:\-\u2013\u2014]", txt)[0]
    txt = re.sub(r"[<>{}]", "", txt).strip(" \"'\u201c\u201d")[:60]
    if not txt or re.match(r"(?i)^(off|none|no|false)$", txt):
        return None
    return txt


class _EndTitle:
    """A big centred title (like a film's last card): the picture darkens a little, the words fade in, spread out and hold."""

    def __init__(self, text, T, W, H):
        from PIL import Image, ImageDraw, ImageFont
        self.T, self.W, self.H = T, W, H
        u = min(W, H) / 1080.0
        fpath = find_font()
        txt = text.upper()
        size = int(110 * u)
        while True:
            try:
                f = ImageFont.truetype(fpath, size) if fpath else ImageFont.load_default()
            except Exception:
                f = ImageFont.load_default()
            lines = [txt]
            if len(txt.split()) > 1 and f.getlength(txt) > W * 0.86:
                w = txt.split()
                k = len(w) // 2
                lines = [" ".join(w[:k]), " ".join(w[k:])]
            wide = max(f.getlength(l) for l in lines)
            if wide <= W * 0.86 or size <= 24:
                break
            size = int(size * 0.9)
        lh = int(size * 1.25)
        hh = lh * len(lines) + int(30 * u)
        layer = Image.new("RGBA", (W, hh), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        for i, l in enumerate(lines):
            tw = f.getlength(l)
            x, y = (W - tw) / 2, int(15 * u) + i * lh
            d.text((x + 3 * u, y + 4 * u), l, font=f, fill=(0, 0, 0, 200))
            d.text((x, y), l, font=f, fill=(255, 255, 255, 255))
        self.layer = np.asarray(layer).astype(np.float32)
        self.y0 = int(H * 0.5 - hh / 2)
        self.hh = hh

    def draw(self, frame, t):
        a_in = min(max(t / 1.0, 0.0), 1.0)
        a_in = a_in * a_in * (3 - 2 * a_in)
        frame = (frame.astype(np.float32) * (1.0 - 0.45 * a_in)).clip(0, 255)
        y0, hh = self.y0, self.hh
        ya, yb = max(y0, 0), min(y0 + hh, self.H)
        if yb > ya:
            lay = self.layer[ya - y0: yb - y0]
            al = lay[..., 3:4] / 255.0 * a_in
            roi = frame[ya:yb]
            frame[ya:yb] = roi * (1 - al) + lay[..., 2::-1] * al
        return frame.astype(np.uint8)


def _subscribe_audio(audio_path, t_click, duration, tmp, tag):
    """A small click and a bell 'ding' at the moment of the click."""
    out = os.path.join(tmp, f"{tag}_sub.wav")
    ms = int(max(t_click, 0) * 1000)
    fc = (f"[1:a]afade=t=out:st=0.03:d=0.05,volume=0.35,adelay={ms}:all=1[c];"
          f"[2:a]afade=t=out:st=0.05:d=1.0:curve=exp,volume=0.16,adelay={ms + 120}:all=1[b1];"
          f"[3:a]afade=t=out:st=0.05:d=0.9:curve=exp,volume=0.08,adelay={ms + 120}:all=1[b2];"
          "[0:a][c][b1][b2]amix=inputs=4:duration=first:dropout_transition=0:normalize=0[o]")
    inputs = ["-i", audio_path, "-f", "lavfi", "-t", "0.1", "-i", "sine=f=1700:d=0.1:r=44100",
              "-f", "lavfi", "-t", "1.2", "-i", "sine=f=1318:d=1.2:r=44100", "-f", "lavfi", "-t", "1.2", "-i", "sine=f=2637:d=1.2:r=44100"]
    try:
        run(["ffmpeg", "-y"] + inputs + ["-filter_complex", fc, "-map", "[o]", "-ar", "44100", "-ac", "2", out])
        return out
    except Exception:
        try:
            run(["ffmpeg", "-y"] + inputs + ["-filter_complex", fc.replace(":normalize=0", ""), "-map", "[o]", "-ar", "44100", "-ac", "2", out])
            return out
        except Exception as e:
            print(f"::warning::subscribe sound skipped ({e})")
            return audio_path


# ---------------- LABELS + ANIMATIONS (v82): on-screen text and motion, written in the script ----------------
# In a scene you can write (nothing written = nothing added):
#   LABEL: Create workflow | top right | box | 1.5s-4s          (text | where | style | when - all after the text are optional)
#   ANIMATION: cursor to top right, click ; type "n8n" at center ; pop "Done!" at top
#   ANIMATION: scroll up "long text of a book page ..."         (the page scrolls up while the voice reads)
# Where: top / upper / middle / lower / bottom + left / center / right  (or 70%,30%).  Styles: tag, title, lower third, box, highlight, arrow, circle, underline.
# Several LABEL / ANIMATION lines in one scene are allowed. The words are drawn by the video builder, so they are always sharp and exactly as written.
OVERLAYS = True
OVERLAY_ACCENT = (0, 196, 255)
OVERLAY_TEXT_BOOST = 1.45        # on-screen words (typed text, arrows, pop-ins, labels) are this much bigger: easy to read in a lesson video        # B, G, R of the accent colour (a warm yellow-orange)
CINEMA_STATIC_CLEAN = True            # NO CAMERA scenes: no vignette and no film grain, so small screen text stays clear

_ANIM_VERBS = r"scale|stagger|split|slide|fade|label|cursor|mouse|pointer|click|clicks|clicking|type|types|typing|type-on|pop|pop-in|appear|appears|highlight|highlights|scroll|scrolls|scrolling|arrow|reveal|underline|draw"


def _ov_clean(s, n=120):
    s = re.sub(r"[<>{}]", "", str(s or ""))
    return s.strip().strip("\"'“”‘’").strip()[:n]


def _ov_pos(text):
    """Words -> (x, y) as shares of the picture. None when no position is written."""
    t = (text or "").lower()
    m = re.search(r"(\d{1,3}(?:\.\d+)?)\s*%\s*[, /]\s*(\d{1,3}(?:\.\d+)?)\s*%", t)
    if m:
        return (min(max(float(m.group(1)) / 100.0, 0.03), 0.97), min(max(float(m.group(2)) / 100.0, 0.03), 0.97))
    x = y = None
    if re.search(r"\bleft\b", t):
        x = 0.18
    elif re.search(r"\bright\b", t):
        x = 0.82
    elif re.search(r"\b(?:center|centre|middle)\b", t):
        x = 0.5
    if re.search(r"\b(?:top|upper)\b", t):
        y = 0.16 if re.search(r"\btop\b", t) else 0.28
    elif re.search(r"\b(?:bottom)\b", t):
        y = 0.84
    elif re.search(r"\blower\b", t):
        y = 0.72
    elif re.search(r"\b(?:center|centre|middle)\b", t):
        y = 0.5
    if x is None and y is None:
        return None
    return (0.5 if x is None else x, 0.5 if y is None else y)


def _ov_time(text):
    """'1.5s-4s' / 'from 2s to 5s' / 'at 3s' -> (t0, t1 or None). None when not written."""
    t = (text or "").lower()
    m = re.search(r"(\d+(?:\.\d+)?)\s*s?\s*(?:-|–|to|until)\s*(\d+(?:\.\d+)?)\s*s\b", t)
    if m:
        return float(m.group(1)), float(m.group(2))
    m = re.search(r"\b(?:at|from|after)\s*(\d+(?:\.\d+)?)\s*s\b", t) or re.search(r"\b(\d+(?:\.\d+)?)\s*s\b", t)
    if m:
        return float(m.group(1)), None
    return None


def _ov_style(text):
    t = (text or "").lower()
    if re.search(r"lower[\s-]?third|banner", t):
        return "lower"
    if re.search(r"\btitle\b|heading", t):
        return "title"
    if re.search(r"\barrow\b|pointer|point", t):
        return "arrow"
    if re.search(r"highlight|marker", t):
        return "highlight"
    if re.search(r"circle|ring|oval", t):
        return "circle"
    if re.search(r"underline", t):
        return "underline"
    if re.search(r"\bbox\b|callout|frame|rectangle|outline", t):
        return "box"
    if re.search(r"\btag\b|pill|note|label|badge|chip", t):
        return "tag"
    return None


def parse_overlays(scene):
    """Reads the scene's LABEL and ANIMATION lines. Returns {"labels": [...], "anims": [...]} or None."""
    if not OVERLAYS or not isinstance(scene, dict):
        return None
    labels, anims = [], []
    raw_l = scene.get("overlay_labels")
    if isinstance(raw_l, str) and raw_l.strip():
        for part in re.split(r"\s*;;\s*", raw_l.strip()):
            if not part.strip():
                continue
            segs = [s.strip() for s in part.split("|")]
            text = _ov_clean(segs[0], 90)
            if not text:
                continue
            lab = {"text": text, "pos": None, "style": None, "t0": None, "t1": None}
            for s in segs[1:]:
                tm = _ov_time(s)
                if tm and not re.search(r"[a-z]{3,}", re.sub(r"\b(?:at|from|after|to|until|s)\b", "", s.lower())):
                    lab["t0"], lab["t1"] = tm
                    continue
                st = _ov_style(s)
                ps = _ov_pos(s)
                if st and not lab["style"]:
                    lab["style"] = st
                if ps and not lab["pos"]:
                    lab["pos"] = ps
                if tm and lab["t0"] is None:
                    lab["t0"], lab["t1"] = tm
            labels.append(lab)
    raw_a = scene.get("overlay_anims")
    if isinstance(raw_a, str) and raw_a.strip():
        for line in re.split(r"\s*;;\s*", raw_a.strip()):
            line = line.strip()
            if not line:
                continue
            if re.match(r"(?i)^(?:scroll|scrolls|scrolling|read|reading)\b", line):
                m = re.match(r"(?is)^(?:scroll\w*|read\w*)\s*(up|down)?\s*[:|\-–—]?\s*(.*)$", line)
                body = _ov_clean(m.group(2) if m else "", 4000)
                if body:
                    anims.append({"kind": "scroll", "dir": (m.group(1) or "up").lower() if m else "up", "text": body})
                continue
            for act in re.split(r"\s*(?:;|\bthen\b|\band then\b)\s*", line):
                act = act.strip(" ,.")
                if not act or not re.search(r"(?i)\b(?:" + _ANIM_VERBS + r")\b", act):
                    continue
                low = act.lower()
                # v85: words many lesson scripts use: scale / stagger / split / slide / fade in / label "TEXT"  -> shown as pop-in words
                if re.match(r"(?i)^(?:scale|stagger|split(?:\s+screen)?|slide|fade(?:\s+in)?|fade\s+into|label|zoom)\b", act):
                    qs_ = [_ov_clean(x, 90) for x in re.findall(r"[\"\u201c]([^\"\u201d]{1,90})[\"\u201d]", act)]
                    qs_ = [x for x in qs_ if x]
                    if re.match(r"(?i)^scale\b", act) and qs_:
                        norm = lambda t: re.sub(r"[^a-z0-9]+", "", t.lower())
                        if any(norm(qs_[0]) and (norm(qs_[0]) in norm(l["text"]) or norm(l["text"]) in norm(qs_[0])) for l in labels):
                            continue          # the LABEL line already shows this title
                        anims.append({"kind": "pop", "text": qs_[0], "pos": (0.5, 0.12)})
                        continue
                    n_ = len(qs_)
                    if n_ == 0:
                        continue
                    if re.match(r"(?i)^split", act) and n_ >= 2:
                        pts = [(0.27, 0.80), (0.73, 0.80)]
                    elif n_ == 1:
                        pts = [(0.5, 0.80)]
                    elif n_ <= 3:
                        pts = [((k + 1) / (n_ + 1), 0.80) for k in range(n_)]
                    else:
                        pts = [((k + 1) / (n_ + 1), 0.74 if k % 2 == 0 else 0.86) for k in range(n_)]
                    for k, t_ in enumerate(qs_[:6]):
                        anims.append({"kind": "pop", "text": t_, "pos": pts[min(k, len(pts) - 1)]})
                    continue
                quoted = re.search(r"[\"“](.+?)[\"”]", act) or re.search(r"'([^']{2,})'", act)
                qtext = _ov_clean(quoted.group(1), 90) if quoted else ""
                pos = _ov_pos(re.sub(r"[\"“].+?[\"”]", " ", act))
                if re.match(r"(?i)^(?:cursor|mouse|pointer|click|double)", act) or re.search(r"\bcursor\b|\bmouse\b|\bpointer\b", low):
                    anims.append({"kind": "cursor", "pos": pos, "click": bool(re.search(r"\bclick", low)), "double": "double" in low})
                elif re.match(r"(?i)^(?:type|types|typing)", act):
                    if not qtext:
                        qtext = _ov_clean(re.sub(r"(?i)^(?:type-on|types?|typing)\s*(?:text)?\s*", "", re.sub(r"(?i)\b(?:at|in|into)\b.*$", "", act)), 90)
                    if qtext:
                        anims.append({"kind": "type", "text": qtext, "pos": pos})
                elif re.match(r"(?i)^(?:pop|pop-in|appear|appears|reveal)", act):
                    if not qtext:
                        qtext = _ov_clean(re.sub(r"(?i)^(?:pop-in|pop|appears?|reveal)\s*(?:up)?\s*", "", re.sub(r"(?i)\b(?:at|in)\s+(?:the\s+)?(?:top|bottom|upper|lower|middle|center|centre|left|right).*$", "", act)), 90)
                    anims.append({"kind": "pop", "text": qtext, "pos": pos})
                elif re.match(r"(?i)^(?:highlight|underline|arrow|draw)", act):
                    if not qtext and not pos and not re.match(r"(?i)^arrow", act):
                        continue          # v85: "draw neural signals..." / "highlight each term" has nothing the builder can point at: it is skipped, not drawn as an empty box
                    anims.append({"kind": "highlight", "text": qtext, "pos": pos, "style": _ov_style(act) or "highlight"})
    if not labels and not anims:
        return None
    return {"labels": labels[:8], "anims": anims[:14]}


def _ov_font(size):
    from PIL import ImageFont
    try:
        fp = find_font()
        return ImageFont.truetype(fp, max(8, int(size))) if fp else ImageFont.load_default()
    except Exception:
        return ImageFont.load_default()


def _ov_text_rgba(text, size, fill=(255, 255, 255), bg=None, padx=0, pady=0, radius=0, border=None, shadow=True, SS=2):
    """Text (optionally on a rounded pill) as a smooth RGBA numpy picture."""
    from PIL import Image, ImageDraw
    f = _ov_font(size * SS)
    probe = ImageDraw.Draw(Image.new("RGBA", (4, 4)))
    tw = int(probe.textlength(text, font=f))
    asc, desc = f.getmetrics() if hasattr(f, "getmetrics") else (size * SS, 0)
    th = int(asc + desc)
    px, py = int(padx * SS), int(pady * SS)
    W_, H_ = tw + 2 * px + 8 * SS, th + 2 * py + 8 * SS
    img = Image.new("RGBA", (W_, H_), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if bg is not None:
        d.rounded_rectangle([4 * SS, 4 * SS, W_ - 4 * SS, H_ - 4 * SS], radius=int(radius * SS), fill=bg, outline=border, width=int(3 * SS) if border else 0)
    x, y = 4 * SS + px, 4 * SS + py
    if shadow and bg is None:
        d.text((x + 2 * SS, y + 3 * SS), text, font=f, fill=(0, 0, 0, 190))
    d.text((x, y), text, font=f, fill=tuple(fill) + (255,))
    img = img.resize((max(1, W_ // SS), max(1, H_ // SS)), Image.LANCZOS)
    return np.asarray(img).copy()


def _ov_blit(frame, rgba, cx, cy, scale=1.0, alpha=1.0, anchor="c"):
    """Lay an RGBA picture on a BGR frame (in place). anchor: c = centred on (cx, cy), tl = top-left corner at (cx, cy)."""
    if alpha <= 0.003 or scale <= 0.01:
        return
    if abs(scale - 1.0) > 0.01:
        rgba = cv2.resize(rgba, (max(1, int(rgba.shape[1] * scale)), max(1, int(rgba.shape[0] * scale))), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
    h, w = rgba.shape[:2]
    x0 = int(round(cx - w / 2.0)) if anchor == "c" else int(round(cx))
    y0 = int(round(cy - h / 2.0)) if anchor == "c" else int(round(cy))
    H_, W_ = frame.shape[:2]
    xa, ya, xb, yb = max(x0, 0), max(y0, 0), min(x0 + w, W_), min(y0 + h, H_)
    if xb <= xa or yb <= ya:
        return
    sub = rgba[ya - y0: yb - y0, xa - x0: xb - x0]
    a = (sub[..., 3:4].astype(np.float32) / 255.0) * min(alpha, 1.0)
    roi = frame[ya:yb, xa:xb].astype(np.float32)
    frame[ya:yb, xa:xb] = (roi * (1.0 - a) + sub[..., [2, 1, 0]].astype(np.float32) * a).astype(np.uint8)


def _ov_ob(x):
    return _ease_out_back(x)


class _OvItem:
    t0 = 0.0
    t1 = 1e9

    def active(self, t):
        return self.t0 <= t <= self.t1

    def fade(self, t, fin=0.25, fout=0.3):
        a = min(max((t - self.t0) / fin, 0.0), 1.0)
        b = min(max((self.t1 - t) / fout, 0.0), 1.0)
        a = a * a * (3 - 2 * a)
        return a * (b * b * (3 - 2 * b))


class _OvTag(_OvItem):
    """A pill with text that pops in."""

    def __init__(self, text, pos, W, H, u, t0, t1):
        self.pos, self.W, self.H, self.t0, self.t1 = pos, W, H, t0, t1
        self.img = _ov_text_rgba(text, 40 * u, fill=(20, 20, 20), bg=(255, 255, 255, 235), padx=22 * u, pady=10 * u, radius=26 * u, border=(OVERLAY_ACCENT[2], OVERLAY_ACCENT[1], OVERLAY_ACCENT[0], 255))

    def draw(self, frame, t):
        p = min(max((t - self.t0) / 0.38, 0.0), 1.0)
        _ov_blit(frame, self.img, self.pos[0] * self.W, self.pos[1] * self.H, scale=0.55 + 0.45 * _ov_ob(p), alpha=self.fade(t))


class _OvTitle(_OvItem):
    def __init__(self, text, pos, W, H, u, t0, t1):
        self.pos, self.W, self.H, self.t0, self.t1 = pos, W, H, t0, t1
        size = 92 * u
        while size > 24 * u:
            img = _ov_text_rgba(text, size, fill=(255, 255, 255))
            if img.shape[1] <= W * 0.9:
                break
            size *= 0.9
        self.img = img

    def draw(self, frame, t):
        p = min(max((t - self.t0) / 0.5, 0.0), 1.0)
        e = p * p * (3 - 2 * p)
        _ov_blit(frame, self.img, self.pos[0] * self.W, self.pos[1] * self.H + (1 - e) * 18, alpha=self.fade(t, 0.5, 0.35))


class _OvLower(_OvItem):
    def __init__(self, text, pos, W, H, u, t0, t1):
        self.pos, self.W, self.H, self.t0, self.t1 = pos, W, H, t0, t1
        txt = _ov_text_rgba(text, 44 * u, fill=(255, 255, 255), shadow=False)
        from PIL import Image
        pad = int(26 * u)
        h, w = txt.shape[0] + int(18 * u), txt.shape[1] + 2 * pad + int(14 * u)
        bar = np.zeros((h, w, 4), np.uint8)
        bar[..., :3] = (20, 20, 20)
        bar[..., 3] = 205
        bar[:, :max(4, int(8 * u)), :3] = (OVERLAY_ACCENT[2], OVERLAY_ACCENT[1], OVERLAY_ACCENT[0])
        bar[:, :max(4, int(8 * u)), 3] = 255
        y = (h - txt.shape[0]) // 2
        sub = bar[y:y + txt.shape[0], pad:pad + txt.shape[1]]
        a = txt[..., 3:4].astype(np.float32) / 255.0
        sub[..., :3] = (sub[..., :3] * (1 - a) + txt[..., :3] * a).astype(np.uint8)
        sub[..., 3] = np.maximum(sub[..., 3], txt[..., 3])
        self.img = bar

    def draw(self, frame, t):
        p = min(max((t - self.t0) / 0.45, 0.0), 1.0)
        e = 1 - (1 - p) ** 3
        x = self.W * 0.06 - (1 - e) * self.img.shape[1]
        y = self.pos[1] * self.H - self.img.shape[0] / 2.0
        _ov_blit(frame, self.img, x, y, alpha=self.fade(t, 0.2, 0.3), anchor="tl")


class _OvBox(_OvItem):
    """A box / highlight / circle / underline drawn around an area, with an optional word on it."""

    def __init__(self, text, pos, W, H, u, t0, t1, style="box", wfrac=0.26, hfrac=0.12):
        self.pos, self.W, self.H, self.t0, self.t1, self.style, self.u = pos, W, H, t0, t1, style, u
        self.bw, self.bh = int(W * wfrac), int(H * hfrac)
        self.tag = _ov_text_rgba(text, 34 * u, fill=(20, 20, 20), bg=(255, 255, 255, 235), padx=16 * u, pady=7 * u, radius=20 * u) if text else None

    def draw(self, frame, t):
        a = self.fade(t, 0.3, 0.3)
        if a <= 0.01:
            return
        p = min(max((t - self.t0) / 0.45, 0.0), 1.0)
        e = 1 - (1 - p) ** 3
        cx, cy = int(self.pos[0] * self.W), int(self.pos[1] * self.H)
        pulse = 1.0 + 0.025 * math.sin((t - self.t0) * 5.0)
        hw, hh = int(self.bw / 2 * (0.6 + 0.4 * e) * pulse), int(self.bh / 2 * (0.6 + 0.4 * e) * pulse)
        col = OVERLAY_ACCENT
        ov = frame.copy()
        th = max(3, int(5 * self.u))
        if self.style == "highlight":
            cv2.rectangle(ov, (cx - hw, cy - hh), (cx + hw, cy + hh), col, -1)
            frame[:] = cv2.addWeighted(ov, 0.30 * a, frame, 1 - 0.30 * a, 0)
            cv2.rectangle(frame, (cx - hw, cy - hh), (cx + hw, cy + hh), col, th, cv2.LINE_AA)
        elif self.style == "circle":
            cv2.ellipse(frame, (cx, cy), (hw, hh), 0, 0, 360 * e, col, th, cv2.LINE_AA)
        elif self.style == "underline":
            x1 = int(cx - hw + 2 * hw * e)
            cv2.line(frame, (cx - hw, cy + hh), (x1, cy + hh), col, th + 1, cv2.LINE_AA)
        else:
            cv2.rectangle(frame, (cx - hw, cy - hh), (cx + hw, cy + hh), col, th, cv2.LINE_AA)
        if self.tag is not None:
            _ov_blit(frame, self.tag, cx - hw + self.tag.shape[1] / 2.0, cy - hh - self.tag.shape[0] / 2.0 - 4, alpha=a)


class _OvArrow(_OvItem):
    def __init__(self, text, pos, W, H, u, t0, t1):
        self.pos, self.W, self.H, self.t0, self.t1, self.u = pos, W, H, t0, t1, u
        self.tag = _ov_text_rgba(text, 36 * u, fill=(20, 20, 20), bg=(255, 255, 255, 235), padx=16 * u, pady=8 * u, radius=22 * u) if text else None

    def draw(self, frame, t):
        a = self.fade(t, 0.3, 0.3)
        if a <= 0.01:
            return
        p = min(max((t - self.t0) / 0.5, 0.0), 1.0)
        e = 1 - (1 - p) ** 3
        tx, ty = self.pos[0] * self.W, self.pos[1] * self.H
        dx = -0.14 * self.W if self.pos[0] > 0.5 else 0.14 * self.W
        sx, sy = tx + dx, ty - 0.12 * self.H
        ex, ey = sx + (tx - sx) * e, sy + (ty - sy) * e
        col = OVERLAY_ACCENT
        th = max(4, int(7 * self.u))
        cv2.line(frame, (int(sx), int(sy)), (int(ex), int(ey)), col, th, cv2.LINE_AA)
        ang = math.atan2(ey - sy, ex - sx)
        hl = 26 * self.u
        for s_ in (-0.5, 0.5):
            cv2.line(frame, (int(ex), int(ey)), (int(ex - hl * math.cos(ang + s_)), int(ey - hl * math.sin(ang + s_))), col, th, cv2.LINE_AA)
        if self.tag is not None:
            _ov_blit(frame, self.tag, sx, sy - self.tag.shape[0] * 0.9, alpha=a)


class _OvType(_OvItem):
    """A text field where the words are typed one letter at a time."""

    def __init__(self, text, pos, W, H, u, t0, t1, tdur):
        from PIL import Image, ImageDraw
        self.text, self.pos, self.W, self.H, self.u, self.t0, self.t1, self.tdur = text, pos, W, H, u, t0, t1, max(tdur, 0.3)
        self.f = _ov_font(40 * u)
        probe = ImageDraw.Draw(Image.new("RGBA", (4, 4)))
        self.tw = int(probe.textlength(text, font=self.f))
        self.pad = int(22 * u)
        self.h = int(40 * u * 1.7)
        self.w = self.tw + 2 * self.pad + int(14 * u)
        self.u_ = u

    def draw(self, frame, t):
        from PIL import Image, ImageDraw
        a = self.fade(t, 0.2, 0.3)
        if a <= 0.01:
            return
        n = int(len(self.text) * min(max((t - self.t0) / self.tdur, 0.0), 1.0) + 0.0001)
        S = 2
        img = Image.new("RGBA", (self.w * S, self.h * S), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([2 * S, 2 * S, (self.w - 2) * S, (self.h - 2) * S], radius=int(14 * self.u * S), fill=(255, 255, 255, 240), outline=(OVERLAY_ACCENT[2], OVERLAY_ACCENT[1], OVERLAY_ACCENT[0], 255), width=3 * S)
        f2 = _ov_font(40 * self.u * S)
        shown = self.text[:n]
        d.text((self.pad * S, int(self.h * S * 0.2)), shown, font=f2, fill=(25, 25, 25, 255))
        if int((t - self.t0) * 2) % 2 == 0 or n < len(self.text):
            cx = self.pad * S + int(d.textlength(shown, font=f2)) + 3 * S
            d.rectangle([cx, int(self.h * S * 0.18), cx + 3 * S, int(self.h * S * 0.82)], fill=(25, 25, 25, 255))
        img = img.resize((self.w, self.h), Image.LANCZOS)
        _ov_blit(frame, np.asarray(img).copy(), self.pos[0] * self.W, self.pos[1] * self.H, alpha=a)


class _OvScroll(_OvItem):
    """A page of text that scrolls up while it is read; a bar marks the line being read."""

    def __init__(self, text, W, H, u, t0, t1):
        from PIL import Image, ImageDraw
        self.W, self.H, self.t0, self.t1 = W, H, t0, t1
        cw = int(W * (0.62 if W >= H else 0.88))
        ch = int(H * (0.80 if W >= H else 0.66))
        self.cw, self.ch = cw, ch
        S = 2
        fs = 36 * u * (1.0 if W >= H else 0.95)
        f = _ov_font(fs * S)
        pad = int(46 * u * S)
        probe = ImageDraw.Draw(Image.new("RGBA", (4, 4)))
        lines = []
        for para in re.split(r"\s*(?:\\n|\n|¶)\s*", text):
            words, cur = para.split(), ""
            for w_ in words:
                t_ = (cur + " " + w_).strip()
                if probe.textlength(t_, font=f) <= cw * S - 2 * pad or not cur:
                    cur = t_
                else:
                    lines.append(cur)
                    cur = w_
            lines.append(cur)
            lines.append("")
        while lines and lines[-1] == "":
            lines.pop()
        self.lh = int(fs * 1.62 * S)
        self.n_lines = len(lines)
        th = max(len(lines) * self.lh + 2 * pad, int(ch * S))
        big = Image.new("RGBA", (cw * S, th), (251, 247, 238, 255))
        d = ImageDraw.Draw(big)
        for i, l in enumerate(lines):
            d.text((pad, pad + i * self.lh), l, font=f, fill=(32, 32, 36, 255))
        self.big = np.asarray(big.resize((cw, th // S), Image.LANCZOS)).copy()
        self.pad_px = pad // S
        self.lh_px = self.lh / float(S)
        shadow = np.zeros((ch + 40, cw + 40, 4), np.uint8)
        shadow[..., 3] = 0
        cv2.rectangle(shadow, (20, 26), (cw + 20, ch + 26), (0, 0, 0, 120), -1)
        self.shadow = cv2.GaussianBlur(shadow, (0, 0), 12)

    def draw(self, frame, t):
        a = self.fade(t, 0.4, 0.4)
        if a <= 0.01:
            return
        p = min(max((t - self.t0 - 0.3) / max(self.t1 - self.t0 - 0.9, 0.5), 0.0), 1.0)
        total = self.big.shape[0]
        read_y = self.pad_px + p * max(total - 2 * self.pad_px, 1)
        band = 0.40 * self.ch
        off = int(max(0.0, min(read_y - band, total - self.ch)))
        crop = self.big[off: off + self.ch].copy()
        if crop.shape[0] < self.ch:
            crop = np.vstack([crop, np.full((self.ch - crop.shape[0], crop.shape[1], 4), (251, 247, 238, 255), np.uint8)])
        by = int(read_y - off)
        y0, y1 = max(by - int(self.lh_px * 0.15), 0), min(by + int(self.lh_px * 0.95), self.ch)
        if y1 > y0:
            seg = crop[y0:y1, :, :3].astype(np.float32)
            tint = np.array([60, 215, 255], np.float32)     # RGB of a yellow marker
            crop[y0:y1, :, :3] = (seg * 0.62 + tint * 0.38).astype(np.uint8)
        _ov_blit(frame, self.shadow, self.W / 2.0, self.H / 2.0 + 6, alpha=a)
        _ov_blit(frame, crop, self.W / 2.0, self.H / 2.0, alpha=a)


def _ov_arrow_img(u):
    from PIL import Image, ImageDraw
    S = 4
    sz = int(46 * u)
    img = Image.new("RGBA", (sz * S + 40, sz * S + 40), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pts = [(0, 0), (0, 0.80), (0.20, 0.62), (0.34, 0.95), (0.47, 0.89), (0.33, 0.57), (0.58, 0.57)]
    pts = [(20 + x * sz * S, 20 + y * sz * S) for x, y in pts]
    sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(sh).polygon([(x + 5 * S, y + 6 * S) for x, y in pts], fill=(0, 0, 0, 110))
    img = Image.alpha_composite(sh, img)
    d = ImageDraw.Draw(img)
    d.polygon(pts, fill=(255, 255, 255, 255), outline=(15, 15, 15, 255), width=int(2.6 * S))
    img = img.resize((img.width // S, img.height // S), Image.LANCZOS)
    return np.asarray(img).copy()


class _OvCursor(_OvItem):
    """A mouse arrow that glides (on a curve) to places and clicks, like a real laptop."""

    def __init__(self, steps, W, H, u, t_end):
        self.W, self.H, self.u = W, H, u
        self.steps = steps          # [{"t0","t1","a":(x,y),"b":(x,y),"click":t or None,"double":bool}]
        self.t0 = max(0.0, steps[0]["t0"] - 0.25)
        self.t1 = t_end
        self.img = _ov_arrow_img(u)
        self.off = 20.0

    def _pos(self, t):
        pos = self.steps[0]["a"]
        for s in self.steps:
            if t < s["t0"]:
                return pos
            if t <= s["t1"]:
                p = (t - s["t0"]) / max(s["t1"] - s["t0"], 1e-3)
                e = p * p * (3 - 2 * p)
                ax, ay = s["a"]
                bx, by = s["b"]
                dist = math.hypot(bx - ax, by - ay)
                nx, ny = (-(by - ay), (bx - ax))
                nl = math.hypot(nx, ny) or 1.0
                bend = 0.12 * dist
                cx, cy = (ax + bx) / 2.0 + nx / nl * bend, (ay + by) / 2.0 + ny / nl * bend
                x = (1 - e) ** 2 * ax + 2 * (1 - e) * e * cx + e ** 2 * bx
                y = (1 - e) ** 2 * ay + 2 * (1 - e) * e * cy + e ** 2 * by
                return (x, y)
            pos = s["b"]
        return pos

    def draw(self, frame, t):
        a = self.fade(t, 0.25, 0.35)
        if a <= 0.01:
            return
        x, y = self._pos(t)
        px, py = x * self.W, y * self.H
        press = 1.0
        for s in self.steps:
            for ct in ([s["click"], s["click"] + 0.28] if (s.get("click") is not None and s.get("double")) else ([s["click"]] if s.get("click") is not None else [])):
                dt = t - ct
                if 0 <= dt < 0.5:
                    r = (dt / 0.5)
                    rad = int((14 + 44 * r) * self.u)
                    ov = frame.copy()
                    cv2.circle(ov, (int(px), int(py)), rad, OVERLAY_ACCENT, max(2, int(5 * self.u * (1 - r))), cv2.LINE_AA)
                    frame[:] = cv2.addWeighted(ov, 0.85 * (1 - r) * a, frame, 1 - 0.85 * (1 - r) * a, 0)
                if 0 <= dt < 0.14:
                    press = 0.82
        _ov_blit(frame, self.img, px - self.off * 1.0, py - self.off * 1.0, scale=press, alpha=a, anchor="tl")


class _Overlay:
    def __init__(self, spec, W, H, duration, td):
        self.W, self.H = W, H
        u = min(W, H) / 1080.0 * OVERLAY_TEXT_BOOST
        avail = max(duration - (td + 0.12 if td else 0.25), 0.8)
        self.items = []
        # --- script labels: fixed words placed on the picture
        for i, lab in enumerate(spec.get("labels", [])):
            st = lab["style"] or ("tag")
            pos = lab["pos"] or ((0.5, 0.14) if st == "title" else ((0.5, 0.80) if st == "lower" else (0.5, 0.5)))
            t0 = lab["t0"] if lab["t0"] is not None else 0.4 + 0.6 * i
            t1 = lab["t1"] if lab["t1"] is not None else avail
            t0, t1 = min(t0, avail - 0.4), min(max(t1, t0 + 0.6), avail)
            self._add_label(st, lab["text"], pos, u, t0, t1)
        # --- animations: sequenced one after the other
        tc = 0.5
        steps, cur = [], (0.78, 0.84)
        plan = []
        for an in spec.get("anims", []):
            k = an["kind"]
            if k == "cursor":
                tgt = an.get("pos") or cur
                dist = math.hypot(tgt[0] - cur[0], tgt[1] - cur[1])
                dur = 0.55 + min(dist, 1.2) * 0.8 if tgt != cur else 0.0
                plan.append(("cursor", an, tc, dur, cur, tgt))
                tc += dur
                if an.get("click"):
                    tc += 0.45 + (0.28 if an.get("double") else 0.0)
                cur = tgt
            elif k == "type":
                dur = min(3.2, 0.07 * len(an["text"]) + 0.2)
                plan.append(("type", an, tc, dur, None, None))
                tc += dur + 0.3
            elif k == "pop":
                plan.append(("pop", an, tc, 0.4, None, None))
                tc += 0.55
            elif k == "highlight":
                plan.append(("highlight", an, tc, 0.5, None, None))
                tc += 0.6
            elif k == "scroll":
                plan.append(("scroll", an, 0.35, 0.0, None, None))
        f = 1.0
        if tc > avail - 0.4:
            f = max((avail - 0.5) / max(tc - 0.5, 0.1), 0.35)
        sc = lambda x: 0.5 + (x - 0.5) * f
        cur_steps = []
        for (k, an, t0, dur, a, b) in plan:
            t0 = sc(t0) if k != "scroll" else t0
            dur = dur * f
            if k == "cursor":
                if dur > 0:
                    st_ = {"t0": t0, "t1": t0 + dur, "a": a, "b": b, "click": None, "double": an.get("double", False)}
                    if an.get("click"):
                        st_["click"] = t0 + dur + 0.12 * f
                    cur_steps.append(st_)
                elif an.get("click"):
                    cur_steps.append({"t0": t0, "t1": t0, "a": a, "b": b, "click": t0 + 0.1, "double": an.get("double", False)})
            elif k == "type":
                self.items.append(_OvType(an["text"], an.get("pos") or (0.5, 0.5), W, H, u, t0, avail, dur))
            elif k == "pop":
                if an.get("text"):
                    self.items.append(_OvTag(an["text"], an.get("pos") or (0.5, 0.5), W, H, u, t0, avail))
            elif k == "highlight":
                self._add_label(an.get("style", "highlight"), an.get("text", ""), an.get("pos") or (0.5, 0.5), u, t0, avail)
            elif k == "scroll":
                self.items.append(_OvScroll(an["text"], W, H, u, t0, avail))
        if cur_steps:
            self.items.append(_OvCursor(cur_steps, W, H, u, avail))
        self.items.sort(key=lambda i: (isinstance(i, _OvCursor), i.t0))

    def _add_label(self, st, text, pos, u, t0, t1):
        W, H = self.W, self.H
        if st == "title":
            self.items.append(_OvTitle(text, pos, W, H, u, t0, t1))
        elif st == "lower":
            self.items.append(_OvLower(text, pos, W, H, u, t0, t1))
        elif st == "arrow":
            self.items.append(_OvArrow(text, pos, W, H, u, t0, t1))
        elif st in ("box", "highlight", "circle", "underline"):
            self.items.append(_OvBox(text, pos, W, H, u, t0, t1, style=st, hfrac=(0.05 if st == "underline" else 0.12)))
        else:
            if text:
                self.items.append(_OvTag(text, pos, W, H, u, t0, t1))

    def draw(self, frame, t):
        for it in self.items:
            if it.active(t):
                it.draw(frame, t)
        return frame


# ---------------- DEVELOPER SCREEN (v83): a real, live web page on the screen, driven by the script ----------------
# In a scene (or after /Developer at the top of the script, then every scene keeps the same screen) write:
#   SCREEN: https://github.com/YourName/your-lesson-repo            (a GitHub repo, a repo file, or any web page / GitHub Pages link)
#   STEPS: click "Start" ; type "Jacob" in "Name" ; scroll down ; click "Submit" ; wait 2s
# A real browser opens the page, the cursor glides and clicks, text is typed, the page scrolls - all while the voice is speaking.
# If the page cannot be opened, the scene simply keeps its normal picture. Scenes without SCREEN are not touched.
DEV_ENABLED = True
DEV_CAPTURE_EVERY = 2            # take a browser picture every 2nd frame (the cursor still moves every frame)
DEV_START_DELAY = 0.8            # seconds before the first step
DEV_GAP = 0.35                   # pause between two steps
_DEV = {"quiz": None, "pw": None, "browser": None, "ctx": None, "page": None, "url": None, "servers": {}, "dead": False, "size": None}


def _dev_split(text):
    """Split on ; or ;; or the word 'then', but not inside quotes."""
    parts, cur, q = [], "", None
    s = str(text or "")
    i = 0
    while i < len(s):
        ch = s[i]
        if q:
            cur += ch
            if ch == q:
                q = None
        elif ch in "\"“'" and (ch != "'" or not cur or not cur[-1].isalnum()):
            q = "”" if ch == "“" else ch
            cur += ch
        elif ch == ";":
            parts.append(cur)
            cur = ""
        elif s[i:i + 6].lower() == " then " :
            parts.append(cur)
            cur = ""
            i += 5
        else:
            cur += ch
        i += 1
    parts.append(cur)
    return [p.strip(" ,.\n") for p in parts if p.strip(" ,.\n")]


def _dev_q(s):
    m = re.search(r"[\"“](.+?)[\"”]", s) or re.search(r"(?<!\w)'([^']+)'(?!\w)", s)
    return m.group(1).strip() if m else None


def parse_dev_steps(text):
    steps = []
    for part in _dev_split(text):
        low = part.lower()
        q = _dev_q(part)
        rest = re.sub(r"^\w+\s*", "", part).strip()
        if re.match(r"answer\b|(?:give\s+a\s+)?(?:right|correct|wrong)\s+answer\b", low):
            steps.append({"do": "answer", "mode": "all" if re.search(r"\ball\b|every|each|whole|rest", low) else ("wrong" if re.search(r"wrong|incorrect", low) else "right")})
        elif re.match(r"(?:next|continue|finish|see (?:my )?score)\b", low):
            steps.append({"do": "next"})
        elif re.match(r"(?:click|tap|press the|choose|select|open|double[- ]click)\b", low):
            tgt = q or re.sub(r"(?i)^(?:the\s+)?(?:button|link|tab|menu|option)?\s*", "", rest)
            if tgt:
                steps.append({"do": "click", "target": tgt, "double": "double" in low})
        elif re.match(r"(?:type|enter|write|fill|input)\b", low):
            m = re.match(r"(?is)^\w+\s+[\"“](.+?)[\"”]\s*(?:(?:in|into|on|inside)\s+(?:the\s+)?(?:field\s+|box\s+)?(?:[\"“](.+?)[\"”]|(\S.*)))?$", part)
            if m:
                steps.append({"do": "type", "text": m.group(1), "target": (m.group(2) or m.group(3) or "").strip()})
        elif re.match(r"(?:hover|point|move)\b", low):
            tgt = q or re.sub(r"(?i)^(?:over|to|at)\s+(?:the\s+)?", "", rest)
            if tgt:
                steps.append({"do": "hover", "target": tgt})
        elif re.match(r"scroll", low):
            if q:
                steps.append({"do": "scroll", "to": q})
            else:
                n = re.search(r"(\d+)\s*(?:px|pixels)?", low)
                d = "up" if "up" in low or "top" in low else "down"
                steps.append({"do": "scroll", "dir": d, "amount": int(n.group(1)) if n else None, "full": bool(re.search(r"bottom|top|end", low))})
        elif re.match(r"wait|pause", low):
            n = re.search(r"(\d+(?:\.\d+)?)", low)
            steps.append({"do": "wait", "seconds": min(float(n.group(1)) if n else 1.0, 8.0)})
        elif re.match(r"zoom", low):
            n = re.search(r"(\d+)", low)
            if n:
                steps.append({"do": "zoom", "pct": min(max(int(n.group(1)), 60), 250)})
        elif re.match(r"(?:key|press|hit)\b", low):
            m = re.search(r"\b(enter|return|tab|escape|esc|space|arrowdown|arrowup|down|up|backspace)\b", low)
            if m:
                k = {"return": "Enter", "esc": "Escape", "down": "ArrowDown", "up": "ArrowUp", "arrowdown": "ArrowDown", "arrowup": "ArrowUp"}.get(m.group(1), m.group(1).capitalize())
                steps.append({"do": "key", "key": k})
    return steps[:40]


def parse_dev(scene):
    """Returns {"url","steps"} when the scene has a SCREEN line, else None."""
    if not DEV_ENABLED or not isinstance(scene, dict):
        return None
    url = str(scene.get("screen_url") or "").strip()
    if not url:
        return None
    m = re.search(r"https?://[^\s\"'<>)]+", url)
    if not m:
        return None
    return {"url": m.group(0).rstrip(".,;"), "steps": str(scene.get("screen_steps") or "")}


def _dev_serve(folder):
    import http.server, functools, threading
    if folder in _DEV["servers"]:
        return _DEV["servers"][folder]

    class _Q(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Q, directory=folder))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _DEV["servers"][folder] = srv.server_address[1]
    return srv.server_address[1]


def _dev_real_url(url, tmp):
    """A github.com repo / folder / file link is downloaded (git clone) and served from this machine - no GitHub Pages needed.
    Any other link is opened as it is."""
    m = re.match(r"^https?://(?:www\.)?github\.com/([^/\s]+)/([^/\s#?]+?)(?:\.git)?(?:/(?:tree|blob)/([^/\s]+)/?([^\s?#]*))?/?([?#].*)?$", url)
    if not m:
        return url
    user, repo, branch, sub = m.group(1), m.group(2), m.group(3), (m.group(4) or "").strip("/")
    qs = m.group(5) or ""                          # e.g. ?lesson=cold-email is passed on to the page
    dest = os.path.join(tmp, f"devrepo_{user}_{repo}")
    if not os.path.isdir(dest):
        cmd = ["git", "clone", "--depth", "1"] + (["--branch", branch] if branch else []) + [f"https://github.com/{user}/{repo}.git", dest]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if r.returncode != 0 and branch:                       # the branch name may have contained a slash: try the default branch
            shutil.rmtree(dest, ignore_errors=True)
            r = subprocess.run(["git", "clone", "--depth", "1", f"https://github.com/{user}/{repo}.git", dest], capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            raise RuntimeError("could not download the repo: " + (r.stderr or "")[-200:])
    port = _dev_serve(dest)
    path = sub
    full = os.path.join(dest, path)
    if os.path.isdir(full) or not path:
        idx = None
        for name in ("index.html", "index.htm"):
            if os.path.exists(os.path.join(full, name)):
                idx = name
                break
        if idx is None:
            for root, _d, files in os.walk(full):
                hs = sorted(f for f in files if f.lower().endswith((".html", ".htm")))
                if hs:
                    idx = os.path.relpath(os.path.join(root, hs[0]), full)
                    break
        if idx is None:
            raise RuntimeError("the repo has no .html page")
        path = (path + "/" + idx).strip("/")
    return f"http://127.0.0.1:{port}/{path}{qs}"


def _dev_page(url, tmp, W, H):
    if _DEV["dead"]:
        raise RuntimeError("browser not available")
    if _DEV["page"] is None or _DEV["size"] != (W, H):
        try:
            from playwright.sync_api import sync_playwright
            if _DEV["pw"] is None:
                _DEV["pw"] = sync_playwright().start()
                _DEV["browser"] = _DEV["pw"].chromium.launch(args=["--no-sandbox", "--disable-gpu"])
            _DEV["ctx"] = _DEV["browser"].new_context(viewport={"width": W, "height": H}, device_scale_factor=1, locale="en-US",
                                                      user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
            if _DEV.get("quiz"):                   # the lesson page shows the questions from the script instead of its own
                _DEV["ctx"].add_init_script(script="window.__LESSON__ = " + json.dumps(_DEV["quiz"]).replace("</", "<\\/") + ";")
            _DEV["page"] = _DEV["ctx"].new_page()
            _DEV["size"], _DEV["url"] = (W, H), None
        except Exception as e:
            _DEV["dead"] = True
            raise RuntimeError(f"playwright is not installed or the browser did not start ({e})")
    page = _DEV["page"]
    if _DEV["url"] != url:                     # the same link in the next scene keeps the page as it is (what was typed and clicked stays)
        real = _dev_real_url(url, tmp)
        page.goto(real, wait_until="domcontentloaded", timeout=40000)
        try:
            page.wait_for_load_state("load", timeout=9000)
        except Exception:
            pass                                   # slow trackers: the page is already there
        page.wait_for_timeout(900)
        _dev_clean_page(page)
        _DEV["url"] = url
    return page


def _dev_clean_page(page):
    """Closes cookie banners / pop-ups of other people's websites, and refuses block pages (then the scene keeps its picture)."""
    try:
        for name in ("Accept all", "Accept All", "Accept", "I agree", "Agree", "Got it", "Allow all", "OK", "Okay", "Close", "No thanks", "Dismiss"):
            b = page.get_by_role("button", name=name, exact=True)
            if b.count() and b.first.is_visible():
                b.first.click(timeout=1500)
                page.wait_for_timeout(300)
                break
    except Exception:
        pass
    try:
        head = (page.inner_text("body", timeout=3000) or "")[:1200].lower()
        if re.search(r"just a moment|verify you are human|access denied|attention required|enable javascript and cookies|are you a robot", head):
            raise RuntimeError("the site blocked the browser")
    except RuntimeError:
        raise
    except Exception:
        pass


def _dev_close():
    try:
        if _DEV["browser"]:
            _DEV["browser"].close()
        if _DEV["pw"]:
            _DEV["pw"].stop()
    except Exception:
        pass


def _dev_find(page, target, want_input=False):
    t = (target or "").strip()
    if not t:
        return None
    cands = []
    if re.match(r"^(?:#|\.|\[|css=|xpath=|//)", t) or re.search(r"\s>\s|\[[a-z\-]+[=~^$*|]", t):
        cands.append(page.locator(t))
    else:
        if want_input:
            cands += [page.get_by_label(t), page.get_by_placeholder(t), page.get_by_role("textbox", name=t), page.get_by_role("combobox", name=t)]
        cands += [page.get_by_role("button", name=t), page.get_by_role("link", name=t), page.get_by_role("tab", name=t),
                  page.get_by_label(t), page.get_by_placeholder(t), page.get_by_text(t, exact=True), page.get_by_text(t), page.get_by_role("radio", name=t),
                  page.get_by_role("checkbox", name=t), page.locator(f"[title*='{t[:40]}' i], [aria-label*='{t[:40]}' i], [value*='{t[:40]}' i]")]
    for c in cands:
        try:
            n = min(c.count(), 6)
            for i in range(n):
                e = c.nth(i)
                if e.is_visible():
                    return e
        except Exception:
            continue
    return None


def _dev_center(el, W, H):
    el.scroll_into_view_if_needed(timeout=2500)
    bb = el.bounding_box()
    if not bb:
        return None
    return (min(max(bb["x"] + bb["width"] / 2.0, 4), W - 4), min(max(bb["y"] + bb["height"] / 2.0, 4), H - 4))


def _dev_est(steps):
    tot = DEV_START_DELAY
    for s in steps:
        k = s["do"]
        tot += {"click": 1.5, "hover": 1.1, "scroll": 1.4, "zoom": 0.3, "key": 0.5, "answer": 2.4, "next": 1.4}.get(k, 0.0)
        if k == "answer" and s.get("mode") == "all":
            tot += 3.4 * max(len((_DEV.get("quiz") or {}).get("questions") or []) - 1, 4)
        if k == "type":
            tot += 1.3 + 0.07 * len(s["text"])
        if k == "wait":
            tot += s["seconds"]
        tot += DEV_GAP
    return tot


def dev_scene_frames(dev, duration, td, W, H, fps, tmp):
    """Yields one BGR frame per video frame: the live page with the cursor on top. Raises before the first frame if the page cannot be opened."""
    page = _dev_page(dev["url"], tmp, W, H)
    steps = parse_dev_steps(dev.get("steps", ""))
    avail = max(duration - (td + 0.3 if td else 0.35), 1.0)
    sp = 1.0
    est = _dev_est(steps)
    if steps and est > avail:
        sp = max(avail / est, 0.45)                    # long list of steps: everything moves faster so it all fits in the voice
    u = min(W, H) / 1080.0
    cur = [(0.66, 0.80)]
    cursor = None
    if steps:
        cursor = _OvCursor([{"t0": 0.0, "t1": 0.0, "a": cur[0], "b": cur[0], "click": None, "double": False}], W, H, u, duration)
        cursor.t0 = 0.0
    now = [0.0]

    def glide(tgt, base=0.75):
        d = math.hypot(tgt[0] - cur[0][0], tgt[1] - cur[0][1])
        dur = (0.4 + min(d, 1.2) * base) * sp
        t0 = now[0]
        cursor.steps.append({"t0": t0, "t1": t0 + dur, "a": cur[0], "b": tgt, "click": None, "double": False})
        cur[0] = tgt
        return t0 + dur

    def wait_until(t):
        while now[0] < t:
            yield

    def do_click(s):
        el = _dev_find(page, s["target"])
        c = _dev_center(el, W, H) if el else None
        if not c:
            print(f"::warning::developer screen: could not find '{s['target']}' to click")
            return
        t_arr = glide((c[0] / W, c[1] / H))
        yield from wait_until(t_arr + 0.08)
        cursor.steps[-1]["click"] = now[0]
        cursor.steps[-1]["double"] = bool(s.get("double"))
        page.mouse.move(c[0], c[1])
        if s.get("double"):
            page.mouse.dblclick(c[0], c[1])
        else:
            page.mouse.click(c[0], c[1])
        yield from wait_until(now[0] + 0.55 * sp)

    def do_hover(s):
        el = _dev_find(page, s["target"])
        c = _dev_center(el, W, H) if el else None
        if not c:
            print(f"::warning::developer screen: could not find '{s['target']}' to point at")
            return
        t_arr = glide((c[0] / W, c[1] / H))
        yield from wait_until(t_arr)
        page.mouse.move(c[0], c[1])
        yield from wait_until(now[0] + 0.5 * sp)

    def do_type(s):
        el = _dev_find(page, s["target"], want_input=True) if s.get("target") else None
        if el is None:
            try:
                el = page.locator("input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=button]):not([type=submit]), textarea, [contenteditable=true]").first
                if not el.is_visible():
                    el = None
            except Exception:
                el = None
        if el is None:
            print(f"::warning::developer screen: no place to type '{s['text'][:20]}'")
            return
        c = _dev_center(el, W, H)
        if not c:
            return
        t_arr = glide((c[0] / W, c[1] / H))
        yield from wait_until(t_arr + 0.08)
        cursor.steps[-1]["click"] = now[0]
        page.mouse.click(c[0], c[1])
        try:
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
        except Exception:
            pass
        text, t0, per = s["text"], now[0] + 0.25 * sp, 0.07 * sp
        done = 0
        while done < len(text):
            n = min(len(text), int(max(now[0] - t0, 0) / per))
            if n > done:
                page.keyboard.type(text[done:n])
                done = n
            yield
        yield from wait_until(now[0] + 0.35 * sp)

    def do_scroll(s):
        try:
            y0 = page.evaluate("window.scrollY")
            if s.get("to"):
                el = _dev_find(page, s["to"])
                if not el:
                    print(f"::warning::developer screen: could not find '{s['to']}' to scroll to")
                    return
                bb = el.bounding_box()
                y1 = y0 + bb["y"] - H * 0.3
            elif s.get("full"):
                y1 = 0 if s.get("dir") == "up" else page.evaluate("document.documentElement.scrollHeight")
            else:
                amt = s.get("amount") or int(H * 0.6)
                y1 = y0 + (-amt if s.get("dir") == "up" else amt)
        except Exception:
            return
        t0, dur = now[0], 1.0 * sp
        last = y0
        while now[0] < t0 + dur:
            p = (now[0] - t0) / dur
            y = y0 + (y1 - y0) * (p * p * (3 - 2 * p))
            page.evaluate("d => window.scrollBy(0, d)", y - last)
            last = y
            yield
        page.evaluate("d => window.scrollBy(0, d)", y1 - last)
        yield from wait_until(now[0] + 0.3 * sp)

    def do_wait(s):
        yield from wait_until(now[0] + s["seconds"])

    def do_zoom(s):
        page.evaluate("p => { document.body.style.zoom = p + '%'; }", s["pct"])
        yield from wait_until(now[0] + 0.3)

    def do_key(s):
        page.keyboard.press(s["key"])
        yield from wait_until(now[0] + 0.4 * sp)

    def quiz_info():
        try:
            return page.evaluate("(() => { try { return {a: LESSON.questions[i].a, n: LESSON.questions[i].o.length, left: LESSON.questions.length - i, done: answered}; } catch (e) { return null; } })()")
        except Exception:
            return None

    def do_answer_one(wrong=False):
        info = quiz_info()
        if not info or info.get("done"):
            return
        idx = info["a"]
        if wrong:
            idx = next((k for k in range(info["n"]) if k != info["a"]), info["a"])
        yield from do_click({"target": "#opts .opt:nth-child(%d)" % (idx + 1)})
        yield from wait_until(now[0] + 0.9 * sp)          # time to read the green / red note

    def do_answer(s):
        if s.get("mode") == "all":
            for _ in range(60):
                info = quiz_info()
                if not info:
                    break
                yield from do_answer_one(False)
                yield from do_click({"target": "#next"})
                yield from wait_until(now[0] + 0.3 * sp)
                nxt = quiz_info()
                if not nxt or nxt.get("left", 0) <= 0 or (nxt.get("left") == info.get("left")):
                    break
        else:
            yield from do_answer_one(s.get("mode") == "wrong")

    def do_next(s):
        yield from do_click({"target": "#next"})

    table = {"answer": do_answer, "next": do_next, "click": do_click, "hover": do_hover, "type": do_type, "scroll": do_scroll, "wait": do_wait, "zoom": do_zoom, "key": do_key}
    N = max(int(round(duration * fps)), 2)
    idx, gen, next_start, last = 0, None, DEV_START_DELAY, None

    def shot():
        raw = page.screenshot(type="jpeg", quality=88)
        img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return None
        if img.shape[1] != W or img.shape[0] != H:
            img = cv2.resize(img, (W, H), interpolation=cv2.INTER_AREA)
        return img

    for k in range(N):
        now[0] = k / float(fps)
        try:
            if gen is None and idx < len(steps) and now[0] >= next_start:
                gen = table[steps[idx]["do"]](steps[idx])
                idx += 1
            if gen is not None:
                try:
                    next(gen)
                except StopIteration:
                    gen = None
                    next_start = now[0] + DEV_GAP * sp
        except Exception as e:
            print(f"::warning::developer screen step skipped ({str(e)[:120]})")
            gen = None
            next_start = now[0] + DEV_GAP
        try:
            if last is None or k % DEV_CAPTURE_EVERY == 0:
                s_ = shot()
                if s_ is not None:
                    last = s_
        except Exception as e:
            if last is None:
                raise RuntimeError(f"the page could not be captured ({e})")
        frame = last.copy()
        if cursor is not None and len(cursor.steps) > 1:
            cursor.draw(frame, now[0])
        yield frame


import atexit
atexit.register(_dev_close)


def _peek_first(gen):
    """Start the page generator now, so a page that cannot open fails here (and the scene falls back to its picture)."""
    first = next(gen)
    def _again():
        yield first
        yield from gen
    return _again()


def build_cinema_segment(scene_index, image_path, audio_path, duration, sub_text, tmp, segment_path,
                         plan, next_image=None, next_plan=None, is_first=False, is_last=False):
    """One scene with the 2.5D camera. The last frames melt into the first picture of the next scene (when there is one)."""
    st = cin_state(scene_index, image_path, tmp, static=bool(plan.get("static")))
    nst = cin_state(scene_index + 1, next_image, tmp, static=bool(next_plan.get("static"))) if next_image and next_plan and plan.get("kind") != "cut" else None
    N = max(int(round(duration * FPS)), 2)
    td = min(CINEMA_TRANSITION_SECONDS, duration * 0.4) if nst is not None else 0.0
    TN = int(round(td * FPS))
    tfun = _TRANSITIONS.get(plan["kind"], tr_dissolve)
    tools = cin_finish_tools(WIDTH, HEIGHT)
    B0 = None
    if nst is not None:
        B0 = cin_render(nst, cin_pose_plan(next_plan, 0.0))
    audio_in = audio_path
    if nst is not None and CINEMA_WHOOSH:
        audio_in = _whoosh_audio(audio_path, duration, td, plan["kind"], tmp, f"s{scene_index:03d}")
    lab, lab_t0 = None, 0.0
    sub = plan.get("subscribe")
    if sub:
        try:
            lab_T = min(CINEMA_SUBSCRIBE_SECONDS, duration * 0.85)
            if sub["pos"] == "start":
                lab_t0 = min(0.4, max(duration - lab_T, 0.0))
            elif sub["pos"] == "middle":
                lab_t0 = max((duration - lab_T) / 2.0, 0.0)
            else:
                lab_t0 = max(duration - td - 0.1 - lab_T, 0.0)
            lab = _SubLabel(sub["msg"], lab_T, WIDTH, HEIGHT)
            if CINEMA_WHOOSH:
                audio_in = _subscribe_audio(audio_in, lab_t0 + lab.click_t, duration, tmp, f"s{scene_index:03d}")
            print(f"scene {scene_index}: subscribe label ({sub['pos']}) from {lab_t0:.1f}s for {lab_T:.1f}s")
        except Exception as e:
            print(f"::warning::subscribe label skipped ({e})")
            lab = None

    endt, endt_t0 = None, 0.0
    if plan.get("end"):
        try:
            end_T = min(CINEMA_END_SECONDS, duration * 0.9)
            endt_t0 = max(duration - end_T, 0.0)
            endt = _EndTitle(plan["end"], end_T, WIDTH, HEIGHT)
            print(f'scene {scene_index}: end title "{plan["end"]}" from {endt_t0:.1f}s for {end_T:.1f}s')
        except Exception as e:
            print(f"::warning::end title skipped ({e})")
            endt = None

    devgen = None
    if plan.get("dev"):
        try:
            devgen = dev_scene_frames(plan["dev"], duration, td, WIDTH, HEIGHT, FPS, tmp)
            devgen = _peek_first(devgen)
            print(f"scene {scene_index}: live developer screen {plan['dev']['url']}")
        except Exception as e:
            print(f"::warning::scene {scene_index}: developer screen not used ({str(e)[:160]}) - the scene keeps its picture")
            devgen = None
    ov = None
    if plan.get("overlays"):
        try:
            ov = _Overlay(plan["overlays"], WIDTH, HEIGHT, duration, td)
            print(f"scene {scene_index}: {len(ov.items)} on-screen items (labels/animations)")
        except Exception as e:
            print(f"::warning::labels/animations skipped ({e})")
            ov = None

    fade_out_start = max(duration - FADE_SECONDS, 0)
    vf_parts = []
    vf_parts += subtitle_filters(sub_text, duration, tmp, f"s{scene_index:03d}")
    vf_parts += watermark_filter()
    if is_first:
        vf_parts.append(f"fade=t=in:st=0:d={FADE_SECONDS}")
    if is_last or (nst is None and plan.get("kind") != "cut"):
        vf_parts.append(f"fade=t=out:st={fade_out_start:.3f}:d={FADE_SECONDS}")
    vf_parts.append("format=yuv420p")
    af = (f"afade=t=in:st=0:d=0.2,afade=t=out:st={max(duration - 0.25, 0):.3f}:d=0.25,apad")
    log_path = os.path.join(tmp, f"cin_{scene_index:03d}.log")
    cmd = ["ffmpeg", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{WIDTH}x{HEIGHT}", "-framerate", str(FPS), "-i", "pipe:0",
           "-i", audio_in, "-vf", ",".join(vf_parts), "-af", af,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", str(VIDEO_CRF), "-maxrate", VIDEO_MAXRATE, "-bufsize", "12M",
           "-pix_fmt", "yuv420p", "-r", str(FPS), "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
           "-t", f"{duration:.3f}", segment_path]
    with open(log_path, "w") as lg:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=lg, stderr=lg)
        try:
            for k in range(N):
                t = k / float(N - 1)
                pose = cin_pose_plan(plan, t)
                if TN and k >= N - TN:
                    p = (k - (N - TN) + 1) / float(TN)
                    rl = CINEMA_LEADIN_RELAX + (CINEMA_TRANSITION_RELAX - CINEMA_LEADIN_RELAX) * _ease(p)
                    pose = dict(pose)
                    for _key in ("tx", "ty", "dz"):
                        pose[_key] *= (1.0 - rl)
                elif TN and CINEMA_LEADIN_SECONDS > 0 and k >= N - TN - int(round(CINEMA_LEADIN_SECONDS * FPS)):
                    _lead = max(int(round(CINEMA_LEADIN_SECONDS * FPS)), 1)
                    q = (k - (N - TN - _lead) + 1) / float(_lead)
                    rl = _ease(q) * CINEMA_LEADIN_RELAX
                    pose = dict(pose)
                    for _key in ("tx", "ty", "dz"):
                        pose[_key] *= (1.0 - rl)
                frame = None
                if devgen is not None:
                    try:
                        frame = next(devgen)
                    except Exception as e:
                        print(f"::warning::scene {scene_index}: developer screen stopped ({str(e)[:140]}) - the rest of the scene keeps the picture")
                        devgen = None
                if frame is None:
                    frame = cin_render(st, pose)
                if TN and k >= N - TN:
                    frame = tfun(frame, B0, p, plan["dir"])
                frame = cin_finish(frame, tools, k, clean=bool(plan.get("static")))
                if ov is not None:
                    frame = ov.draw(np.ascontiguousarray(frame), k / float(FPS))
                if lab is not None and lab_t0 <= k / float(FPS) < lab_t0 + lab.T:
                    frame = lab.draw(np.ascontiguousarray(frame), k / float(FPS) - lab_t0)
                if endt is not None and k / float(FPS) >= endt_t0:
                    frame = endt.draw(np.ascontiguousarray(frame), k / float(FPS) - endt_t0)
                proc.stdin.write(np.ascontiguousarray(frame).tobytes())
            proc.stdin.close()
        except Exception as e:
            try:
                proc.kill()
            except Exception:
                pass
            tail = ""
            try:
                tail = open(log_path).read()[-400:]
            except Exception:
                pass
            raise RuntimeError(f"cinema render failed: {e} {tail}")
        rc = proc.wait()
    if rc != 0:
        raise RuntimeError("cinema ffmpeg failed: " + open(log_path).read()[-400:])
    print(f"scene {scene_index}: camera {'NONE (still)' if plan.get('static') else '+'.join(plan['moves'])}" + (f", transition {plan['kind']} ({plan['dir']})" if nst is not None else (", hard cut (no transition)" if plan.get("kind") == "cut" else ", ends with a fade")))


# ---------------- YOUTUBE QUIZ CARDS (v84): the voice reads the question and the options, a 5-4-3-2-1-0 countdown runs, then the answer ticks ----------------
# A script with a QUIZ block but no /Developer gets these scenes automatically (n8n writes them: scene["quiz_card"]).
# phase "intro"  : a title card ("QUIZ TIME") while the voice explains
# phase "ask"    : the question and options A-D appear while the voice reads them, then 5..0 counts down, then the right option ticks (ding)
# phase "reveal" : the card stays with the right option ticked while the voice gives the answer and the short explanation
QUIZ_CARDS = True
QUIZ_COUNT_FROM = 5
QUIZ_LEAD = 0.5          # seconds of quiet after the voice stops, before the countdown starts
QUIZ_HOLD = 1.3          # seconds the ticked answer is shown after the tick (ask phase)
QUIZ_GREEN = (46, 204, 113)
QUIZ_ACCENT = (255, 196, 0)


def quiz_ask_duration(voice_len):
    return voice_len + QUIZ_LEAD + (QUIZ_COUNT_FROM + 1) + QUIZ_HOLD


def _quiz_audio(audio_path, duration, tick_t, beep_times, tmp, tag):
    """The voice, a soft beep as each digit appears, and a bell ding when the answer ticks."""
    out = os.path.join(tmp, f"{tag}_quizaudio.wav")
    inputs = ["-i", audio_path]
    chains, labels = [], ["[0:a]"]
    k = 1
    for bt in beep_times:
        inputs += ["-f", "lavfi", "-t", "0.09", "-i", "sine=f=880:d=0.09:r=44100"]
        chains.append(f"[{k}:a]afade=t=out:st=0.03:d=0.06,volume=0.5,adelay={int(max(bt, 0) * 1000)}:all=1[b{k}]")
        labels.append(f"[b{k}]")
        k += 1
    if tick_t is not None:
        ms = int(max(tick_t, 0) * 1000)
        for f_, vol, extra in ((1318, 0.5, 0), (2637, 0.22, 0)):
            inputs += ["-f", "lavfi", "-t", "1.3", "-i", f"sine=f={f_}:d=1.3:r=44100"]
            chains.append(f"[{k}:a]afade=t=out:st=0.05:d=1.1:curve=exp,volume={vol},adelay={ms}:all=1[b{k}]")
            labels.append(f"[b{k}]")
            k += 1
    fc = ";".join(chains + [f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:normalize=0,apad,atrim=0:{duration:.3f}[o]"])
    try:
        run(["ffmpeg", "-y"] + inputs + ["-filter_complex", fc, "-map", "[o]", "-ar", "44100", "-ac", "2", out])
        return out
    except Exception as e:
        print(f"::warning::quiz sounds skipped ({str(e)[:120]})")
        return audio_path


def _quiz_wrap(draw, text, font, maxw):
    words, lines, cur = str(text).split(), [], ""
    for w_ in words:
        t = (cur + " " + w_).strip()
        if draw.textlength(t, font=font) <= maxw or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w_
    if cur:
        lines.append(cur)
    return lines


def build_quiz_segment(scene_index, card, image_path, audio_path, duration, voice_len, tmp, segment_path):
    """Draw the quiz card frame by frame (Pillow) and pipe it to ffmpeg together with the scene's voice."""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter
    W, H = WIDTH, HEIGHT
    vert = H > W
    phase = str(card.get("phase") or "ask")
    opts = [str(x) for x in (card.get("o") or [])][:4]
    right = int(card.get("a") if card.get("a") is not None else -1)
    marks = [float(x) for x in (card.get("marks") or [])]
    S = W / 1920.0 if not vert else W / 1080.0
    fq = _ov_font(int((68 if not vert else 76) * S))
    fo = _ov_font(int((58 if not vert else 64) * S))
    fl = _ov_font(int((50 if not vert else 56) * S))
    fh = _ov_font(int((40 if not vert else 46) * S))
    fbig = _ov_font(int((260 if not vert else 300) * S))
    ftitle = _ov_font(int((120 if not vert else 120) * S))

    # background: the scene picture, blurred and darkened (or a plain deep blue)
    try:
        bg = Image.open(image_path).convert("RGB")
        r = max(W / bg.width, H / bg.height)
        bg = bg.resize((int(bg.width * r) + 1, int(bg.height * r) + 1))
        bg = bg.crop(((bg.width - W) // 2, (bg.height - H) // 2, (bg.width - W) // 2 + W, (bg.height - H) // 2 + H)).filter(ImageFilter.GaussianBlur(18))
        bg = Image.blend(bg, Image.new("RGB", (W, H), (8, 14, 34)), 0.72)
    except Exception:
        bg = Image.new("RGB", (W, H), (10, 18, 42))
    base = bg.copy()
    d = ImageDraw.Draw(base)
    pad = int(W * (0.06 if not vert else 0.07))
    # layout
    if vert:
        col_w = W - 2 * pad
        ring_c = (W // 2, int(H * 0.86))
        ring_r = int(W * 0.13)
        top_y = int(H * 0.07)
        bottom_lim = int(H * 0.74)
    else:
        col_w = int(W * 0.64) - pad
        ring_c = (int(W * 0.82), int(H * 0.52))
        ring_r = int(H * 0.2)
        top_y = int(H * 0.09)
        bottom_lim = int(H * 0.94)

    if phase == "intro":
        title = str(card.get("title") or "QUIZ TIME")
        tw = d.textlength(title, font=ftitle)
        d.text(((W - tw) / 2, H * 0.34), title, font=ftitle, fill=(255, 255, 255))
        sub = str(card.get("sub") or "")
        sw = d.textlength(sub, font=fo)
        d.text(((W - sw) / 2, H * 0.34 + ftitle.size * 1.35), sub, font=fo, fill=QUIZ_ACCENT)
        rows = []
    else:
        # header pill
        head = f"QUESTION {card.get('n', 1)} OF {card.get('total', 1)}"
        hw = d.textlength(head, font=fh)
        d.rounded_rectangle((pad, top_y, pad + hw + 44 * S, top_y + fh.size + 26 * S), radius=int(30 * S), fill=QUIZ_ACCENT)
        d.text((pad + 22 * S, top_y + 11 * S), head, font=fh, fill=(20, 20, 30))
        y = top_y + fh.size + 26 * S + 34 * S
        for ln in _quiz_wrap(d, card.get("q") or "", fq, col_w):
            d.text((pad + 3 * S, y + 3 * S), ln, font=fq, fill=(0, 0, 0))
            d.text((pad, y), ln, font=fq, fill=(255, 255, 255))
            y += fq.size * 1.28
        y += 26 * S
        # option rows (normal + ticked version)
        n_o = max(len(opts), 1)
        avail = bottom_lim - y
        row_h = int(min(fo.size * 2.2, avail / n_o - 18 * S))
        rows = []
        for i, t in enumerate(opts):
            L = "ABCD"[i]
            def make(ticked):
                img = Image.new("RGBA", (col_w, row_h), (0, 0, 0, 0))
                dd = ImageDraw.Draw(img)
                fill = (22, 120, 70, 235) if ticked else (255, 255, 255, 30)
                edge = QUIZ_GREEN + (255,) if ticked else (255, 255, 255, 110)
                dd.rounded_rectangle((0, 0, col_w - 1, row_h - 1), radius=int(row_h * 0.28), fill=fill, outline=edge, width=int(4 * S) if ticked else int(2 * S))
                bs = int(row_h * 0.7)
                bx, by = int(row_h * 0.15), int(row_h * 0.15)
                dd.ellipse((bx, by, bx + bs, by + bs), fill=(QUIZ_GREEN if ticked else QUIZ_ACCENT) + (255,))
                lw = dd.textlength(L, font=fl)
                dd.text((bx + (bs - lw) / 2, by + (bs - fl.size) / 2 - 3 * S), L, font=fl, fill=(15, 20, 30))
                tx = bx + bs + int(row_h * 0.25)
                txt = t
                while dd.textlength(txt, font=fo) > col_w - tx - 90 * S and len(txt) > 4:
                    txt = txt[:-2]
                if txt != t:
                    txt = txt.rstrip() + "..."
                dd.text((tx, (row_h - fo.size) / 2 - 4 * S), txt, font=fo, fill=(255, 255, 255, 255))
                if ticked:
                    cx, cy = col_w - int(row_h * 0.55), row_h // 2
                    s_ = int(row_h * 0.2)
                    dd.line((cx - s_, cy, cx - s_ // 3, cy + s_ * 0.8, cx + s_ * 1.2, cy - s_ * 0.9), fill=(255, 255, 255, 255), width=int(8 * S), joint="curve")
                return img
            rows.append({"y": int(y + i * (row_h + 18 * S)), "n": make(False), "t": make(True)})

    # timing
    t_v = voice_len if phase == "ask" else 0.0
    t_c0 = t_v + QUIZ_LEAD
    # the tick happens as the "0" appears: digits 5,4,3,2,1 take 5 s, then "0" appears and the answer ticks
    t_tick = t_c0 + QUIZ_COUNT_FROM if phase == "ask" else -1.0
    appear = []
    for i in range(len(opts)):
        fr = marks[i] if i < len(marks) else (i + 1) / (len(opts) + 1)
        appear.append(max(0.15, fr * max(t_v, 1.0) - 0.15) if phase == "ask" else -1.0)
    qa_in = 0.15
    if phase == "ask":
        beeps = [t_c0 + k for k in range(QUIZ_COUNT_FROM)]
        aud = _quiz_audio(audio_path, duration, t_tick, beeps, tmp, f"s{scene_index:03d}")
    else:
        aud = audio_path
        beeps = []
    N = max(int(round(duration * FPS)), 2)

    def ring_layer(frac, digit, color):
        sz = ring_r * 2 + 40
        SS = 2
        im = Image.new("RGBA", (sz * SS, sz * SS), (0, 0, 0, 0))
        dd = ImageDraw.Draw(im)
        c = sz * SS // 2
        R = ring_r * SS
        dd.ellipse((c - R, c - R, c + R, c + R), fill=(8, 14, 34, 200), outline=(255, 255, 255, 60), width=10 * SS)
        if frac > 0.002:
            dd.arc((c - R, c - R, c + R, c + R), start=-90, end=-90 + 360 * frac, fill=color + (255,), width=16 * SS)
        im = im.resize((sz, sz), Image.LANCZOS)
        dd2 = ImageDraw.Draw(im)
        tw = dd2.textlength(str(digit), font=fbig)
        dd2.text(((sz - tw) / 2, sz / 2 - fbig.size * 0.62), str(digit), font=fbig, fill=(255, 255, 255, 255))
        return im

    cmd = ["ffmpeg", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
           "-i", aud,
           "-vf", f"fade=t=in:st=0:d=0.25,fade=t=out:st={max(duration - 0.3, 0):.3f}:d=0.3,format=yuv420p",
           "-af", f"afade=t=in:st=0:d=0.05,afade=t=out:st={max(duration - 0.25, 0):.3f}:d=0.25,apad",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", str(VIDEO_CRF), "-maxrate", VIDEO_MAXRATE, "-bufsize", "12M",
           "-pix_fmt", "yuv420p", "-r", str(FPS), "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
           "-t", f"{duration:.3f}", segment_path]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
    last_key, last_bytes = None, b""
    ring_cache = {}
    try:
        for fi in range(N):
            t = fi / FPS
            ticked_a = 0.0
            if phase == "reveal":
                ticked_a = min(1.0, t / 0.35)
            elif phase == "ask" and t >= t_tick:
                ticked_a = min(1.0, (t - t_tick) / 0.25)
            key = None
            if phase == "ask" and t_c0 <= t < t_tick + 0.0:
                key = None            # countdown moves every frame
            else:
                key = (tuple(round(min(max((t - a) / 0.3, 0), 1), 2) for a in appear) if phase == "ask" else 1, round(ticked_a, 2), int(min(t, 0.3) * FPS) if t < 0.3 else 99, int(t >= t_c0) if phase == "ask" else 0, int(t >= t_tick) if phase == "ask" else 0)
            if key is not None and key == last_key:
                proc.stdin.write(last_bytes)
                continue
            fr_img = base.copy()
            if phase != "intro":
                for i, rw in enumerate(rows):
                    a_in = 1.0 if phase == "reveal" else min(max((t - appear[i]) / 0.3, 0), 1)
                    if a_in <= 0:
                        continue
                    img = rw["n"]
                    if i == right and ticked_a > 0:
                        img = Image.blend(rw["n"], rw["t"], ticked_a)
                        if phase == "ask" and 0 <= t - t_tick < 0.4:
                            sc_ = 1.0 + 0.04 * np.sin((t - t_tick) / 0.4 * np.pi)
                            img = img.resize((int(img.width * sc_), int(img.height * sc_)), Image.LANCZOS)
                            ox = pad - (img.width - rw["n"].width) // 2
                            oy = rw["y"] - (img.height - rw["n"].height) // 2
                            if a_in < 1:
                                img.putalpha(img.getchannel("A").point(lambda v: int(v * a_in)))
                            fr_img.paste(img, (ox, oy), img)
                            continue
                    if a_in < 1:
                        img = img.copy()
                        img.putalpha(img.getchannel("A").point(lambda v, a=a_in: int(v * a)))
                    fr_img.paste(img, (pad, rw["y"]), img)
                # countdown ring (ask phase, from the end of the voice)
                if phase == "ask" and t >= t_c0:
                    if t < t_tick:
                        el = t - t_c0
                        digit = QUIZ_COUNT_FROM - int(el)
                        frac = 1.0 - (el - int(el))
                        low = digit <= 2
                        rl = ring_layer(frac, digit, (255, 90, 80) if low else QUIZ_ACCENT)
                    else:
                        rl = ring_layer(1.0, 0, QUIZ_GREEN)
                    fr_img.paste(rl, (ring_c[0] - rl.width // 2, ring_c[1] - rl.height // 2), rl)
                    cap = "THINK..." if t < t_tick else "ANSWER!"
                    cw_ = ImageDraw.Draw(fr_img).textlength(cap, font=fh)
                    ImageDraw.Draw(fr_img).text((ring_c[0] - cw_ / 2, ring_c[1] + ring_r + 40 * S), cap, font=fh, fill=(255, 255, 255))
                elif phase == "reveal":
                    cap = "CORRECT ANSWER"
                    cw_ = ImageDraw.Draw(fr_img).textlength(cap, font=fh)
                    ImageDraw.Draw(fr_img).text((ring_c[0] - cw_ / 2, ring_c[1] - fbig.size * 0.45 - 70 * S), cap, font=fh, fill=QUIZ_GREEN)
                    L = "ABCD"[right] if 0 <= right < 4 else ""
                    lw = ImageDraw.Draw(fr_img).textlength(L, font=fbig)
                    ImageDraw.Draw(fr_img).text((ring_c[0] - lw / 2, ring_c[1] - fbig.size * 0.45), L, font=fbig, fill=QUIZ_GREEN)
            by = fr_img.tobytes()
            proc.stdin.write(by)
            last_key, last_bytes = key, by
        proc.stdin.close()
        rc = proc.wait()
        if rc != 0:
            raise RuntimeError(f"ffmpeg exited with {rc}")
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        raise
    print(f"scene {scene_index}: quiz card ({phase}) {duration:.1f}s")


def build_segment(scene_index, image_path, audio_path, duration, sub_text, tmp, segment_path):
    """One scene: composite image + eased zoom + subtitles + fades + audio."""
    composite = os.path.join(tmp, f"composite_{scene_index:03d}.png")
    build_composite(image_path, composite)

    total_frames = int(round(duration * FPS))
    z, x, y = zoom_expressions(scene_index, total_frames)
    fade_out_start = max(duration - FADE_SECONDS, 0)

    vf_parts = [
        f"scale={WIDTH * 2}:{HEIGHT * 2}:flags=lanczos",
        f"zoompan=z='{z}':x='{x}':y='{y}':d=1:s={WIDTH}x{HEIGHT}:fps={FPS}",
    ]
    vf_parts += subtitle_filters(sub_text, duration, tmp, f"s{scene_index:03d}")
    vf_parts += watermark_filter()
    vf_parts.append(
        f"fade=t=in:st=0:d={FADE_SECONDS},fade=t=out:st={fade_out_start:.3f}:d={FADE_SECONDS}"
    )
    vf_parts.append("format=yuv420p")
    vf = ",".join(vf_parts)

    af = (
        f"afade=t=in:st=0:d=0.2,"
        f"afade=t=out:st={max(duration - 0.25, 0):.3f}:d=0.25,"
        "apad"
    )

    run([
        "ffmpeg", "-y", "-loop", "1", "-framerate", str(FPS), "-i", composite,
        "-i", audio_path,
        "-vf", vf, "-af", af,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(VIDEO_CRF),
        "-maxrate", VIDEO_MAXRATE, "-bufsize", "12M", "-pix_fmt", "yuv420p", "-r", str(FPS),
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
        "-t", f"{duration:.3f}", segment_path,
    ])


def build_clip_segment(scene_index, clip_path, audio_path, duration, sub_text, tmp, segment_path, next_frame=None):
    """One scene from an animated clip: the clip is fitted to the 9:16 frame on a blurred
    copy of itself (same look as the stills), then subtitles + fades + the scene's voice.
    A clip shorter than the narration is looped (or its last frame is held); a longer one is cut."""
    clip_dur = probe_duration(clip_path)
    fade_out_start = max(duration - FADE_SECONDS, 0)
    short = clip_dur < duration

    ratio = 1.0
    if clip_dur > 0 and clip_dur < duration and CLIP_FILL_MODE == "hold":
        ratio = min(CLIP_MAX_STRETCH, duration / clip_dur)
    clip_dur = clip_dur * ratio
    short = clip_dur < duration - 0.02
    chain = (
        (f"[0:v]setpts=PTS*{ratio:.4f},split=2[bgsrc][fgsrc];" if ratio > 1.001 else "[0:v]split=2[bgsrc][fgsrc];")
        + f"[bgsrc]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={WIDTH}:{HEIGHT},boxblur=40:6,eq=brightness=-0.12[bg];"
        f"[fgsrc]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease:flags=lanczos[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,fps={FPS}"
    )
    if short and CLIP_FILL_MODE == "hold":
        chain += f",tpad=stop_mode=clone:stop_duration={duration - clip_dur + 0.5:.3f}"
    blend = min(CLIP_JOIN_BLEND, max(duration - 0.3, 0)) if next_frame else 0
    if blend > 0.05:
        # the last moments melt into the first picture of the next clip scene (video only: timing and sound do not change)
        chain += (
            "[vm];"
            "[2:v]split=2[nbg][nfg];"
            f"[nbg]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={WIDTH}:{HEIGHT},boxblur=40:6,eq=brightness=-0.12[nb];"
            f"[nfg]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease:flags=lanczos[nf];"
            f"[nb][nf]overlay=(W-w)/2:(H-h)/2,fps={FPS},format=yuva420p,"
            f"fade=t=in:st={max(duration - blend, 0):.3f}:d={blend:.3f}:alpha=1[nx];"
            "[vm][nx]overlay=format=auto"
        )
    else:
        next_frame = None

    steps = subtitle_filters(sub_text, duration, tmp, f"c{scene_index:03d}")
    steps += watermark_filter()
    if CLIP_SCENE_FADES:
        steps.append(f"fade=t=in:st=0:d={FADE_SECONDS}")
        steps.append(f"fade=t=out:st={fade_out_start:.3f}:d={FADE_SECONDS}")
    steps.append("format=yuv420p")
    fc = chain + "," + ",".join(steps) + "[vout]"

    if CLIP_SCENE_FADES:
        af = (
            f"afade=t=in:st=0:d=0.2,"
            f"afade=t=out:st={max(duration - 0.25, 0):.3f}:d=0.25,"
            "apad"
        )
    else:
        # no dip between scenes: only a tiny fade to avoid a click at the cut
        af = (
            f"afade=t=in:st=0:d=0.02,"
            f"afade=t=out:st={max(duration - 0.03, 0):.3f}:d=0.03,"
            "apad"
        )

    cmd = ["ffmpeg", "-y"]
    if short and CLIP_FILL_MODE != "hold":
        cmd += ["-stream_loop", "-1"]
    cmd += [
        "-i", clip_path,
        "-i", audio_path,
    ]
    if next_frame:
        cmd += ["-loop", "1", "-framerate", str(FPS), "-i", next_frame]
    cmd += [
        "-filter_complex", fc,
        "-map", "[vout]", "-map", "1:a",
        "-af", af,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(VIDEO_CRF),
        "-maxrate", VIDEO_MAXRATE, "-bufsize", "12M", "-pix_fmt", "yuv420p", "-r", str(FPS),
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
        "-t", f"{duration:.3f}", segment_path,
    ]
    run(cmd)


def master_audio_and_music(final_path, out_path):
    """Level the narration, duck background music under it, and add faststart."""
    total = probe_duration(final_path)
    if os.path.exists(MUSIC_PATH) and not SKIP_DEFAULT_MUSIC:
        fade_start = max(total - 3, 0)
        fc = (
            "[0:a]aresample=async=1:first_pts=0,loudnorm=I=-16:TP=-1.5:LRA=11,asplit=2[voice][side];"
            f"[1:a]volume={MUSIC_VOLUME},afade=t=in:st=0:d=2,"
            f"afade=t=out:st={fade_start:.3f}:d=3[music];"
            "[music][side]sidechaincompress=threshold=0.02:ratio=10:attack=20:release=500[ducked];"
            "[voice][ducked]amix=inputs=2:duration=first:normalize=0[aout]"
        )
        run([
            "ffmpeg", "-y", "-i", final_path, "-stream_loop", "-1", "-i", MUSIC_PATH,
            "-filter_complex", fc,
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
            "-t", f"{total:.3f}", "-movflags", "+faststart", out_path,
        ])
    else:
        print("no assets/music/default.mp3 found - skipping background music")
        run([
            "ffmpeg", "-y", "-i", final_path,
            "-af", "aresample=async=1:first_pts=0,loudnorm=I=-16:TP=-1.5:LRA=11",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
            "-movflags", "+faststart", out_path,
        ])


# ---------------------------------------------------------------- main

# ---------------------------------------------------------------- YouTube thumbnail

FINAL_MAX_MB = 47            # the finished film is squeezed to fit under this size before upload (Supabase Free allows 50 MB; 0 = never squeeze)


def shrink_to_limit(path, tmp, limit_mb=None):
    """If the film is bigger than FINAL_MAX_MB, re-encode the picture at a bitrate that fits (the sound is copied untouched)."""
    limit_mb = limit_mb or FINAL_MAX_MB
    size = os.path.getsize(path) / 1e6
    if limit_mb <= 0 or size <= limit_mb:
        return path
    total = max(probe_duration(path), 1.0)
    factor = 0.93
    cur = path
    for attempt in range(4):
        vkbps = max(int(limit_mb * factor * 8000.0 / total) - 170, 200)
        out = os.path.join(os.path.dirname(path), f"final_small{attempt}.mp4")
        print(f"the film is {size:.0f} MB - squeezing it to fit {limit_mb} MB (picture {vkbps} kbit/s)")
        run(["ffmpeg", "-y", "-i", path, "-c:v", "libx264", "-preset", "veryfast", "-b:v", f"{vkbps}k",
             "-maxrate", f"{int(vkbps * 1.25)}k", "-bufsize", f"{vkbps * 2}k", "-pix_fmt", "yuv420p",
             "-c:a", "copy", "-movflags", "+faststart", out])
        size = os.path.getsize(out) / 1e6
        cur = out
        if size <= limit_mb:
            break
        factor *= 0.82
    return cur



# ---- v69: the finished film goes to a GitHub Release (up to 2 GB, free, no card) so the quality never has to be squeezed to 50 MB ----
RELEASE_ENABLED = True       # needs a PUBLIC repo; False = always use Supabase Storage (squeezed under FINAL_MAX_MB)
RELEASE_MAX_MB = 1900        # GitHub allows 2 GB per file; a film bigger than this is squeezed to fit
RELEASE_KEEP = 3             # how many finished films stay on GitHub (older ones are deleted automatically)


def upload_to_release(path, episode_id, tmp):
    """Upload the finished film as a GitHub Release asset and return its public download link (None when it is not possible)."""
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not token or not repo:
        print("no GITHUB_TOKEN / GITHUB_REPOSITORY here - using Supabase Storage for the finished film")
        return None
    H = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    path = shrink_to_limit(path, tmp, RELEASE_MAX_MB)
    size_mb = os.path.getsize(path) / 1e6
    tag = f"film-{episode_id}"
    api = f"https://api.github.com/repos/{repo}"
    # a release left from an earlier try of the same episode is removed first
    old = requests.get(f"{api}/releases/tags/{tag}", headers=H, timeout=60)
    if old.status_code == 200:
        requests.delete(f"{api}/releases/{old.json()['id']}", headers=H, timeout=60)
        requests.delete(f"{api}/git/refs/tags/{tag}", headers=H, timeout=60)
    rel = requests.post(f"{api}/releases", headers=H, timeout=60, json={
        "tag_name": tag, "name": f"Film {episode_id}", "body": "Finished film. Deleted automatically after newer films are made.",
        "draft": False, "prerelease": True, "make_latest": "false"})
    rel.raise_for_status()
    rid = rel.json()["id"]
    url = None
    for attempt in range(3):
        print(f"uploading the finished film to GitHub: {size_mb:.0f} MB (try {attempt + 1})")
        try:
            with open(path, "rb") as f:
                up = requests.post(f"https://uploads.github.com/repos/{repo}/releases/{rid}/assets?name=film.mp4",
                                   headers={**H, "Content-Type": "video/mp4"}, data=f, timeout=(30, 3600))
            if up.status_code in (200, 201):
                url = up.json().get("browser_download_url")
                break
            print(f"::warning::GitHub upload answered {up.status_code}: {up.text[:160]}")
            if up.status_code == 422:      # the asset name already exists
                break
        except Exception as e:
            print(f"::warning::GitHub upload failed ({e})")
        time.sleep(5 * (attempt + 1))
    if not url:
        requests.delete(f"{api}/releases/{rid}", headers=H, timeout=60)
        requests.delete(f"{api}/git/refs/tags/{tag}", headers=H, timeout=60)
        return None
    # keep only the newest few finished films on GitHub
    try:
        lst = requests.get(f"{api}/releases?per_page=100", headers=H, timeout=60).json()
        films = sorted([r for r in lst if str(r.get("tag_name", "")).startswith("film-") and r.get("tag_name") != tag],
                       key=lambda r: r.get("created_at", ""), reverse=True)
        for r in films[max(RELEASE_KEEP - 1, 0):]:
            requests.delete(f"{api}/releases/{r['id']}", headers=H, timeout=60)
            requests.delete(f"{api}/git/refs/tags/{r['tag_name']}", headers=H, timeout=60)
    except Exception as e:
        print(f"::warning::old films were not cleaned up ({e})")
    return url



# ---- v70: a small playable copy goes to Supabase, so the Telegram link opens and plays in the browser like before ----
PREVIEW_ENABLED = True
PREVIEW_MAX_MB = 45          # stays under the Supabase Free 50 MB limit


def upload_preview_copy(path, episode_id, tmp):
    """A 720p copy under 45 MB at videos/<episodeId>-preview.mp4 (public). The full-quality film is not touched."""
    small = os.path.join(tmp, "preview_small.mp4")
    run(["ffmpeg", "-y", "-i", path, "-vf", "scale='min(720,iw)':-2", "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", small])
    small = shrink_to_limit(small, tmp, PREVIEW_MAX_MB)
    size_mb = os.path.getsize(small) / 1e6
    print(f"uploading the preview copy to Supabase: {size_mb:.0f} MB")
    with open(small, "rb") as f:
        r = requests.post(f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/videos/{episode_id}-preview.mp4",
                          headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "video/mp4", "x-upsert": "true"},
                          data=f, timeout=(30, 900))
    r.raise_for_status()
    return f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/videos/{episode_id}-preview.mp4"


def pick_thumbnail_scene(scenes, image_by_scene, manifest):
    """The most dramatic picture that does not give the ending away: an explicit manifest choice wins,
    otherwise the scene with the most characters + the strongest emotion; the last scene is skipped (spoilers)."""
    try:
        forced = int(manifest.get("thumbnail_scene") or 0)
        if forced and image_by_scene.get(forced):
            return forced
    except (TypeError, ValueError):
        pass
    cands = [s for s in scenes if image_by_scene.get(s.get("scene_number"))]
    if len(cands) > 3:
        cands = cands[:-1]
    if not cands:
        return None

    def score(s):
        chars = s.get("scene_characters") or s.get("speakers") or []
        try:
            inten = float(s.get("intensity_score") or 0)
        except (TypeError, ValueError):
            inten = 0.0
        return (len(set(chars)) * 2 + inten, -int(s.get("scene_number") or 0))
    return max(cands, key=score).get("scene_number")


# Heavy title font (Inter Display Black, SIL Open Font License) cut down to capitals, digits and punctuation, so the
# cover never depends on fonts installed on the build machine.
_TITLE_FONT_B64 = (
    "T1RUTwAMAIAAAwBAQ0ZGIFUm8ZcAAAJ4AAAUlEdERUYALwA7AAAXDAAAABxHUE9THc0RfAAAFygAAAe8R1NVQrj6uPQAAB7kAAAA"
    "Kk9TLzJyHXgqAAABMAAAAGBjbWFwAfwCcwAAAeQAAAByaGVhZC/aYGYAAADMAAAANmhoZWEQDwZxAAABBAAAACRobXR44jAKuwAA"
    "HxAAAAC4bWF4cAAuUAAAAAEoAAAABm5hbWUE/A1iAAABkAAAAFJwb3N0/6sBBAAAAlgAAAAgAAEAAAAEAACCpGBUXw889QADCAAA"
    "AAAA4YBTjQAAAADm88JBAAD+YAhMB0AAAAADAAIAAAAAAAAAAQAAB8D+EgAACEwAAP/kCEwAAQAAAAAAAAAAAAAAAAAAAC4AAFAA"
    "AC4AAAAEBU8DhAAFAAAFMwTNAAAAmgUzBM0AAALNAQQCegAAAgAKAwAAAAIABAAAAAEAAAAAAAAAAAAAAABSU01TAMAAIABaB8D+"
    "EgAAB8AB7gAAAAEAAAAABCAF0gAAACAACAAAAAIAHgADAAEECQABACYAAAADAAEECQACAA4AJgBJAG4AdABlAHIAIABEAGkAcwBw"
    "AGwAYQB5ACAAQgBsAGEAYwBrAFIAZQBnAHUAbABhAHIAAAAAAAIAAAADAAAAFAADAAEAAAAUAAQAXgAAABAAEAADAAAAIQAnAC4A"
    "OQA6AD8AWv//AAAAIAAmACwAMAA6AD8AQf//AAAAAAAA/+v/8v/o/8AAAQAQABIAFAAAAAAAAAAAAAAALQAmACUAKQAqACgAKwAA"
    "AAMAAAAAAAD/qAEEAAAAAAAAAAAAAAAAAAAAAAAAAAABAAQCAAEBARNJbnRlckRpc3BsYXktQmxhY2sAAQEBPfgbAPgcAfgdDAD4"
    "HgL4HwMzDAP3mAwEHkiCjI+Lix5IgoyPi4sMB4v8NBwITBwHQAX3cA+rHBR0EveSEQAFAQEELlZpdjQuMEludGVyIFVJIGFuZCBJ"
    "bnRlciBpcyBhIHRyYWRlbWFyayBvZiByc21zLkNvcHlyaWdodCAyMDE2IFRoZSBJbnRlciBQcm9qZWN0IEF1dGhvcnNJbnRlciBE"
    "aXNwbGF5IEJsYWNrSW50ZXIgRGlzcGxheQAAAQAiGQARCQAHAAACAAAgAAAOAABoAAANAAAPAAAbAAABAAAuAgABAcICFAKmAxUD"
    "bgOxA+kEdQS6BNsFLQV2BZ0GHQZ8Bu8HTAfrCFUI/wkyCYkJzApNCq4K8ws7C6kL2wxKDRANYg3vDpsOyw+QEDoRExFqEhgSNhJU"
    "EnESrBMTExa6/DT3NMv3FMvLy/cUy/cUy8v3FMvLy8vrS8vLy8vrK/d0S8vL68v3NBL33PdM+0z3vPu898z7zPhM+1TLS/hM+9z3"
    "3PvMy0v3zPtM90wT/7eJQPqM/DQVixwI4AX9RIsFixz3IAX3TPl0FffUiwWL+5QF+9SLBYv5FBXLiwWL+1QF91SLBYv3FAUTBgAI"
    "gEuLBYtLBUuLBYv3FAUT6gSAQPdUiwWL+5QF+9SLBYv9FBX3bPcUBftsiwWLywX31IsFi0oF+2v7EwX3a4sFi0sF+9SLBYscBoAV"
    "EwAEAID3VIsFEwAIAICL+zQFEwAqgED3FIsFi0sF+9SLBYv39BUTAAJAAPcEiwWL6wUTAAOAQPsEiwWLywX31IsFi0sFEwACAgD7"
    "JIsFiysFEwCGgED3JIsFi0sF+9SLBYv8lBUTAIAgAPcUiwWL6wUTAKCAQPsUiwWLywX31IsFi0sFEwGAgID7FIsFi/s0BftUiwWL"
    "SxUTA0CBQPeUiwWL9zQFy4sFi/t0BfvUiwUTEAAQAPeU/RQVi/cUBRMQAAQA+1SLBYv7FAUTAAgIAMv69BUTABAIAIvrBUuLBRMA"
    "CAgAiysFDvegoHb3iffU+jF3AYscBh0Di4sV+FuLBdT3iQX4hosF0vuJBfhoiwX8jRwF0gX8xIsF9xD+MRWPmAXB90i990K890kI"
    "u/tJvPtCv/tICI9+BQ6ti/fM97r3uveq98wS2/go9+74NPwM+D4T9NuLFflSiwX314v3U/dGi/eICIv3Zvsm9yT7VJIIi40FE/j3"
    "Qaz3B/cTi/dBCIv3bftI9y/76IsI/RqLBfgoHPtmFYv3ugX3cIsFE/Twi8xRizIIizJKUSaLCPtw+OAVi/eqBfdXiwUT+OeLxlSL"
    "NwiLN1BUL4sIDvdZc/f8+cb3/AGz+DgD+ZBzFfgRi/eh93y19/QI/DeLBXH7ICs3+yOLCPtfi/sB9yiL95gIi/eZ9wL3Kfdeiwj3"
    "I4vsNqT7IQj4N4sFYvf0+6L3fvwRiwj8K4v70fuki/yGCIv8gvfR+6b4K4sIDvczi/fs+bb37AHb+DT4iPg4A/kqixX4X4v3u/e4"
    "i/hYCIv4WfvA97n8YIsI/NSLBYsc+i4F+DT37BWL+bYF9xqLBfePi/cH+xWL+6UIi/uj+wP7FfuHiwgOiIv39Pdy9+j3dPf0Adv4"
    "NAPbixUcBIKLBYv39AX9dosFi/dyBfk9iwWL9+gF/T2LBYv3dAX5dosFi/f0BRz7fosFDlGgdvis9+b3nPf0Adv4NAPbixX4NIsF"
    "i/isBfkriwWL9+YF/SuLBYv3nAX5U4sFi/f0Bf7ziwUO929z9/z3W/e999b3/BKz+Dj32Pk4/Cr4KhP0+ZhzFfgxi/en95GL+BgI"
    "i/drBRP4/TiLBYv7vQUT9PeiiwWH+wwtPPs9iwj7a4v7BPcji/edCIv3nfcC9yX3Z4sI9yOL20ig+wMI+DyLBXD32Pu792r7/4sI"
    "/CuL+9H7pIv8hgiL/Gv3tPu9+FCLCA73U6B2+Nn39PjBdwHb+DT4hPg0A9uLFfg0iwWL+NkF+ISLBYv82QX4NIsFixwF0gX8NIsF"
    "i/zBBfyEiwWL+MEF/DSLBQ79ZaB2HAXSdwHb+DQD+IQcBdIV/DSLBYsc+i4F+DSLBQ4pc/fsHASSdwG4+BT3pvg0A/jRcxX384v3"
    "V/dCi/fMCIv6mAX8NIsFi/6QBYsrWlUziwgzi1rBi+sIi/cOBfwUiwWL+woFi/vU91H7RvfniwgO91igdhwF0ncB2/g0A9uLFfg0"
    "iwWL9+4F90n3cgX36PzMBfhwiwX8uPnmBfic+RQF/HWLBfu8/BMFSzhLOUs4CIv5CwX8NIsFDvsLi/f0HARydwHb+DQD24sV+raL"
    "BYv39AX9FosFixwEcgX8NIsFDvjpoHYcBdJ3Adv4LPoq+CwD24sV+CyLBYv41AWL64P3U4b3TAi7+0q9+1CqJgj3RPzUBffkiwX3"
    "QfjUBar0v/dZuvdPCIf7UYL7XIsnCIv81AX4LIsFixwF0gX9A4sF+xj8ZgVuJl/7QWX7LQhl9y1e90Fu8Aj7GvhmBf0CiwUO92ug"
    "dhwF0ncB2/g0+Jz4NAPbixX4NIsFi/i0BYvHi/cJhvcaCNH7Fc37B7VICPfn/LQF+DyLBYscBdIF/DSLBYv8tgWLRIz7DY/7DQhR"
    "9k73A2jDCPv/+N0F/DyLBQ73e3P3/PnG9/wBs/g4+PT4OAP5kHMV+CmL99P3o4v4hQiL+Ib70/ek/CmLCPwri/vR+6SL/IYIi/yC"
    "99H7pvgriwiL9/wV+1eL+wH3K4v3lQiL95b3Afcs91eLCPdYi/cA+yyL+5YIi/uV+wD7K/tYiwgOqaB2+Fb36vf49+oB2/g0+Br4"
    "KgPbixX4NIsFi/hWBfdtiwX3+Iv3c/ddi/fTCIv30/tz9137+IsI/Q2LBfg0/U4Vi/f4BfdOiwX3EYvaRov7AQiL+wE8RvsRiwgO"
    "93tz9/z7/PjU+O73/BKz+Dj49Pg4E7j5kHMV6Yvkmt2nCPb7FQX38osF+3f3ngX3IvcV4vdbi/eYCIv4hvvT96T8KYsI/CuL+9H7"
    "pIv8hgiL/IL30fum+CuLCBN4+zD41BUTuPc4+2wFiIuJi4iLCPtXi/sB9yuL95UIi/eW9wH3LPdXiwj3WIv3APssi/uWCBN4i/sN"
    "cypeRgg+6gUOzaB2+Gb31vgA9+YB2/g0+Br4KgPbixX4NIsFi/hmBfdOiwX3c/xmBfhFiwX7pfjABfcl4d33I4v3TQiL99P7c/dd"
    "+/iLCP0NiwX4NP1SFYv4AAX3TosF9xGL2kKL+wEIi/sBPEL7EYsIDvVz99z73Pic+J74hPvc99wS5/g2+Db4LxOc+VRzFfgoi/d7"
    "90aL98wIi/eC+yX3GPvqygj7DKEF+y+nSLGL0giL1t3A9wmLCBOs9xCL3kuVIwj4EosFE5yB98z7dfdM/AiLCPv+i/uL+1WL+68I"
    "i/tx9zD7Lfe0VAj3KG8F9zxr12GLQwiLPTNW+xeLCPsriyzShfcNCBNc/B2LBROci/vj93f7TfgxiwgOuqB2HARy9/QB+GT4NAOz"
    "HARyFfg8iwWLHPuOBfg0iwWLHARyBfg8iwWL9/QFHPsQiwUO9ydz9/IcBIx3Adv4KPhw+CgD+WZzFfgZi/eR926L9+QIi/pUBfwo"
    "iwWL/jIFi/sfKSj7IIsI+x6LJ++L9x4Ii/oyBfwoiwWL/lQFi/vl95D7bfgaiwgO94OgdhwF0ncBixwGAAP4iIsV+LyLBfh4HAXS"
    "BfxYiwX7OfzWBVn7Ql37QV77Qghc90Jb90FX90II+0H41gX8XosFDvnPoHYcBdJ3AYscCEwD+B+LFfhdiwX3PflEBZi/l8qb2Qib"
    "PZdMmFcI9z39RAX4XYsF+B8cBdIF/FiLBfsM/NYFcPsWc/sWdPsWCHH3FnH3Fm33Fgj7GPjWBfwoiwX7GPzWBW37F3D7F3L7Fwhz"
    "9xdz9xdw9xcI+wv41gX8WIsFDvdxoHYcBdJ3AYscBe4Di4sV+GqLBfc/95QFtMixyLHICLFOsU6yTgj3NvuUBfh3iwX8ovmZBfho"
    "+WEF/F2LBfsN+1YFYkpkSWVKCGXMZc1jzAj7C/dWBfxpiwX4cf1sBQ73W6B2HAXSdwH4v/gmA/i/ixX4JosFi/iQBfiv+moF/G2L"
    "Bftd/DYFdVx1XHdbCHW7dbpzugj7avg2BfxwiwX4v/5qBQ6yi/f0+ab39AG9HATUA72LFRwE1IsFi/f0Bf1biwXAxr/FvcYI+Lr5"
    "HgWL98wFHPtAiwWL+/QF+VOLBVRPVlBXTwj8wf0bBQ7uc/fU+hb31AGz+Bz4qPgcA/lOcxX4MYv3ife3i/hxCIv4cvuJ97j8MYsI"
    "/DGL+4n7uYv8cQiL/HD3ifu4+DGLCIv31BX7OYsm9z6L96oIi/er8Pc/9zmLCPc5i/D7P4v7qwiL+6om+z77OYsIDvxXoHYcBdJ3"
    "AfgC+CQD+ZIcBdIV/ASLBfwC+3QFi/vsBffg918FjYsFi/75BfgkiwUOhYv31vn899QBx/gR+Bj4FgPWixUcBHiLBYv31gX82IsF"
    "i40F94X3WAX3Wvc19x33G4v3cwiL97z7hPdH+96LCPv9i/t0+2uL++8I+BGLBYv3L93i9w2LCPcDi9VDiyEIi/sV+wM9OEgI/MT8"
    "WAUOlHP3yvvK+Kzz96z3HPh2+8D3wBK9+Br7/vgY+An4EfwC+DATrID5InMV+AGL94v3UYv3kwiL90b7DPcy+3SiCIuOBROtAPc9"
    "n/cV9wiL90oIi/eH+3z3P/veiwgTsoD8AIv7bPtli/ulCPgYiwUTqwCL9t3W9wKLCPSL10aLLAiLKjxS+xqLCPskiwWL+6wF9ySL"
    "BROsgPcfi+RKiycIiyo4R/sLiwj7GIs74Iv3IQgTbID8GosFE6yAi/uq90b7lvg+iwgO9wmgdve098b6FHcB+az4GgPL97QV+WyL"
    "BYv7tAX4GosFi/e0Bfc6iwWL98YF+zqLBYv6FAX8k4sF/PP+DAX5bIMV+/WLBYuNBff0+J4FjIsFDmtz98b7xviM94j3ufcp99gS"
    "rvgV+DP4GhO8+QdzFff+i/eA93SL98wIi/ev+473cvudiwj7Aosua05PCImLBZ73hQX5MosFi/fYBf6eiwVh/g4F+AiLBaLV1r3y"
    "iwj3DIvgLov7FwiL+xY4M/sOiwj7DIsx34v3BggTfPwViwUTvJ/7zvd4+1L37IsIDqRz98z4OPe4w/he+8T3xBKz+A78DvhI+DL4"
    "HBPa+UJzFffwi/eM92aL98wIi/e3+3D3Z/vEiwj7KYv7A1k9+wQIiYsFi/d72/ch90OLCBPq9wWL2E+cLQj4EosFE9py95P7cPdf"
    "+92LCPwqi/uG+8SL/HYIi/xu94P7qvgriwiH98wVE9b7C4s044v3DgiL9w7i4/cLiwj3DYviM4v7DggT2ov7DjQz+w2LCA77A6B2"
    "HASA9+YBs/rmA/ciixX4NIsF+OAcBI4Fi/fYBf7miwWL++YF+UKLBYuJBQ6cc/eq9/73t/fS97USs/g4/Ar4IPfy+CD8Cvg4E/L5"
    "JHMV+AmL94n3Pov3lgiL92D7KfcX+4WsCBPs92ur9xX3CIv3TgiL9437c/cz+/GLCPvpi/t5+ziL+4gIi/tO9xb7CfdqbAgT8vuF"
    "avsp+xeL+2AIi/uW94j7PvgIiwiL96oV+waLOdeL9QiL9N3W9waLCPcHi95AiyIIiyI3PvsGiwgT7Iv5IRUhi0fJi+wIi+zPyfWL"
    "CPaL0E2LKgiLKkZNIIsIDqRz98T7xPhew/e4+Dj3zBKz+Bz4MvhI/A74DhO6+Q5zFfgqi/eG98SL+HYIi/hu+4P3qvwriwj78Iv7"
    "jPtmi/vMCIv7t/dw+2f3xIsI9ymL9wO92fcECI2LBYv7ezv7IftDiwgTevsFiz7HeukI/BKLBRO6pPuT93D7X/fdiwgTvJH5uhX7"
    "DYs044v3DgiL9w7i4/cNiwj3C4viM4v7DgiL+w40M/sLiwgO6nP3zvuhdhwErPfQErn4FPuK99r3bve8f/fSE7T4xnMV9w6L9x+s"
    "9wnUCBNy2TkF+CaLBfuA95gF8vcetvdMi/dWCPvSiwWLLHI1YUII+1L3ZgXSvQUTbPcU5tH3D4v3HgiL92P7Lvc/+46LCPuai/tC"
    "+02L+3cIi/sVw/sFzDMIiIkFE3T7MikpIIv7OwgTtIv7e/dX+1H31YsI9zH36RVgeWCCYIsIKotMvYvfCIvGqbCyqQgTbNj4GBVh"
    "xHO9i7wIi8m1s8+LCNSLrmCLXAiLZndaR2EIDv19ePg0/B92HAXldxLP+DT8JfgWE2jx+GwV9/CLBbH6jgX8PIsFE3D3aBz6GxX3"
    "B4vo6Iv3BwgTsIv3By7o+weLCPsHiy4ui/sHCBNwi/sH6C73B4sIDlB4+DT8H3b6ofiA+8z3zBLL+AhY+DT8FffqnfgSE1L4NPhs"
    "FffqiwWLrwWL8OnH9wnWCBNR9xTdyPcLi/cvCIv3hPtx9z770YsIE2j74ov7cvtki/uwCPgIiwUTWYv3ANLT9osI64vNT4s1CItF"
    "YE0vXggTUvtBNlc7i/tICBNU90X8uRX3B4vo6Iv3BwgTlIv3By7o+weLCPsHiy4ui/sHCBNUi/sH6C73B4sIDvv5+Db3wgH3DflO"
    "A/nH+WQV/U6LBYv7wgX5TosFDv1b+gT49gH3CPf2A/cr+gQV97CLBa749gX79osFDv2F++b49gGz+EMDs/vmFfeniwX3MPj2BfwG"
    "iwUO/YV4+DT8H3YSy/g0E2D3pHgV9weL6OiL9wcIE6CL9wcu6PsHiwj7B4suLov7BwgTYIv7B+gu9weLCA79hXj4NPwfdvkH+DQS"
    "y/g0E3D3pHgV9weL6OiL9wcIE7CL9wcu6PsHiwj7B4suLov7BwgTcIv7B+gu9weLCIv5BxX3B4vo6Iv3BwiL9wcu6PsHiwj7B4su"
    "Lov7BwiL+wfoLvcHiwgO/i0Oc6P6tKP4LqMGHjElxf8MCZsMCosMC/foCvgkCxwFERUAAQAAAAwAAAAAAAAAAgACAAEAGwABAB0A"
    "HgABAAEAAAAKACQAMgACREZMVAAObGF0bgAOAAQAAAAA//8AAQAAAAFrZXJuAAgAAAABAAAAAQAEAAkACAACAAoAEgABAAIAAAAQ"
    "AAEAAgAAALYAAQAkAAQAAAANAEIASACaAFIAUgBYAF4AbAB2AIQAmgCgAKAAAQANAAYAEAAUABYAFwAZACAAIQAiACUAKQAqACsA"
    "AQAr/7sAAgAl/7sAK/+7AAEAJf+YAAEAJf+AAAMAHP/YACr/4gAr/+IAAgAq/8kAK//JAAMAIP/sACT/7AAl/6MABQAU/4wAFv+A"
    "ABf/rwAZ/2kAKf+7AAEAJf+7AAMAJP/cACb/0gAt/4wAAgXsAAQAAAYCBmAAHgAZAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/nAAA/7v/nAAAAAD/dP+SAAAAAP+w/6YAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAD/nAAe/5z/OP8k/7v/xgAA/xAAAAAA/xoAAAAA/7D/XgAA/4AAAP8QAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/nAAAAAAAAAAAAAAAAAAAAAAAAAAA/7sAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAD/nP8kAAAAAAAA/5gAAAAAAAAAAP+AAAD/Rv+7AAAAAAAAAAD/dQAAAAAAAAAAAAAAAP+7/zgAAAAAAAD/YAAAAAAAAAAA/7sA"
    "AP+7/0b/uwAAAAAAAP91AAD/XgAAAAAAAAAAAAD/uwAA/2D/mAAAAAD/o/+vAAAAAP+6AAD/owAAAAAAAAAAAAAAAP+7AAD/6QAA"
    "AAAAAAAAAAD/2AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/8YAAAAAAAAAAAAA/7oAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAD/4gAAAAAAAAAA//YAAAAA/87/uwAAAAAAAP/SAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAD/fgAAAAAAAAAA/5j/pgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/7oAAAAAAAAAAAAA/9IAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/3QAAAAAAAAAAP+j/7oAAAAAAAAAAAAAAAAAAP+cAAD/zgAA/68AAAAA/84AAAAA"
    "AAAAAP+wAAD/v/+7AAAAAP/S/9IAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP/GAAAAAP9P/vL/mAAAAAD/UgAAAAD/"
    "dQAAAAAAAP9GAAAAAAAA/6MAAAAAAAAAAAAA/7sAAAAAAAAAAP+7AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAP9GAAD/9gAAAAAAAAAAAAAAAAAAAAD/r/87AAAAAAAAAAAAAAAAAAAAAAAAAAAAAP+w/xoAAAAAAAD/uv/pAAAAAAAA/5gA"
    "KP87/4AAAAAAAAAAAP+7AAAAAAAAAAAAAAAA/5L/EAAAAAAAAP+AAAAAAAAeAAD/gAAA/zv/O/+SAAAAAAAA/7sAAAAAAAAAAAAA"
    "AAAAAAAAAAD/uwAAAAAAAAAA/4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP+mAAAAAP+7/0YAAAAAAAAAAAAAAAAA"
    "AAAAAAD/vv8Y/9j+wwAA/xD/pv/sAAAAAAAAAAAAAAAAAAAAAAAAAAD/zgAAAAAAAAAA/+wAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAP9GAAAAAAAAAAAAAAAAAAAAAAAAAAD/r/91AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/14AAAAAAAAAAAAAAAAAAAAAAAAA"
    "AP87/q8AAAAAAAAAAP+AAAAAAAAAAAAAAAAAAAD/gAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAP+wAAAAAP+7AAAAAP+c/5IAAAAAAAD/vgAAAAAAAAAAAAAAAAAA/9gAAAAAAAAAAAAA/+wAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AP+7AAAAAAAAAAAAAAAA/37/iAAAAAAAAAAAAAD/IwAAAAAAAAAAAAAAAAAAAAD/uwAA/wD/O//gAAD/3gAA/4kAAAAo/+MAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/4AAAAAAAAAAAAAIAAwABAB8AAAAiACQAHwAnACwAIgABAAEA"
    "LAACAAgADAABAAMAFwAOAAAAAAAEAAsADwAAAAAAAQARAAEACgAJAAYABAATABIADQAFABAAGgAAAB0AFgAbAAAAAAAcABYAGgAA"
    "AAAAGQAHABgAFQAVABQAAQABACwAAwABAAIAAQABAAEAAgABAAEADwABAAEAAQABAAIAAQACAAEACAAFAAQACgANAAkABgALABAA"
    "FQAYABIAFAAAABAAFgAXAAAAAAAAABMABwARAA4ADgAMAAEAAAAKACYAKAACREZMVAAObGF0bgAYAAQAAAAA//8AAAAAAAAAAAAA"
    "AAAFQAFIBh0AAAUzAFAF1gAoBbAAUAUOAFAE1wBQBewAKAXQAFACQABQBK8ALQXVAFAEmgBQB2YAUAXoAFAF+AAoBS8AUAX4ACgF"
    "UwBQBXsAQAVAACgFpABQBgAAAAhMAAAF7gAABdgAAAU4ADIFdAAoA04AIAULADwFGgAyBYYAQATxACMFKgAoBKIAKAUiACgFKgAo"
    "BXAALgIoAEAE1gBAA6wAeQJKAHQCIAAoAiAAQAIgAEABeAAA"
)
_TITLE_FONT_UPM = 2048
_TITLE_FONT_ADV = {" ":376,"!":552,"&":1392,"'":586,",":544,"-":940,".":544,"0":1396,"1":846,"2":1291,"3":1306,"4":1414,"5":1265,"6":1322,"7":1186,"8":1314,"9":1322,":":544,"?":1238,"A":1565,"B":1331,"C":1494,"D":1456,"E":1294,"F":1239,"G":1516,"H":1488,"I":576,"J":1199,"K":1493,"L":1178,"M":1894,"N":1512,"O":1528,"P":1327,"Q":1528,"R":1363,"S":1403,"T":1344,"U":1444,"V":1536,"W":2124,"X":1518,"Y":1496,"Z":1336}


def _title_font_file(tmp):
    """Write the embedded font to disk; fall back to a system font if that ever fails."""
    try:
        import base64
        p = os.path.join(tmp, "title_font.otf")
        with open(p, "wb") as f:
            f.write(base64.b64decode(_TITLE_FONT_B64))
        return p, True
    except Exception:
        return find_font(), False


def _text_width(text, size, embedded):
    if embedded:
        return sum(_TITLE_FONT_ADV.get(ch, 1100) for ch in text) * size / _TITLE_FONT_UPM
    return len(text) * size * 0.66


def wrap_title(title):
    """Short, punchy capital lines (max 3), like a real YouTube cover."""
    t = re.sub(r"\s+", " ", str(title or "").replace("’", "'")).strip().upper()
    t = re.sub(r"[^A-Z0-9'!?&.,:\- ]", "", t) or "NEW FILM"
    n = 1 if len(t) <= 16 else (2 if len(t) <= 34 else 3)
    lines = textwrap.wrap(t, width=max(8, -(-len(t) // n)), break_long_words=False) or [t]
    while len(lines) > 3:           # very long titles: merge the shortest neighbours
        i = min(range(len(lines) - 1), key=lambda k: len(lines[k]) + len(lines[k + 1]))
        lines[i:i + 2] = [lines[i] + " " + lines[i + 1]]
    return lines


def _hex_rgb(h, default):
    h = re.sub(r"[^0-9A-Fa-f]", "", str(h or ""))
    if len(h) != 6:
        h = default
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def make_thumbnail(image_path, title, out_path, tmp):
    """1280x720 JPEG in the style of a movie poster on YouTube: the scene picture full-frame and bright, a dark fade at the
    bottom, and the title in huge gold capitals with a thick black outline and shadow across the bottom."""
    W, H = 1280, 720
    font, embedded = _title_font_file(tmp)
    lines = wrap_title(title)
    sq = THUMB_SQUEEZE if embedded else 0.9
    max_w = W - 120                                   # the title may use the width minus side margins
    size = {1: 215, 2: 175, 3: 130}[len(lines)]
    for _ in range(60):                               # shrink until the widest line fits
        if max(_text_width(l, size, embedded) for l in lines) * sq <= max_w or size <= 60:
            break
        size -= 4
    pad = max(10, size // 7)                          # room for the outline around the letters
    line_h = int(size * 1.02)
    ch = line_h + 2 * pad
    ch -= ch % 2                                      # height of one line's canvas (even number)
    gap = int(line_h * 0.86)                          # distance between line tops (tight, like a poster)
    block_h = ch + gap * (len(lines) - 1)
    y_first = H - 34 - block_h
    top = _hex_rgb(THUMB_COLOR_TOP, "FFE45A")
    bot = _hex_rgb(THUMB_COLOR_BOTTOM, "F07A0A")
    bw = max(7, size // 11)                           # outline thickness

    graph = [
        f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}:(iw-{W})/2:(ih-{H})*0.18,"
        "eq=contrast=1.10:saturation=1.22:brightness=0.01,unsharp=5:5:0.6:5:5:0.0,vignette=angle=PI/6[bg]",
        # dark fade from the middle of the picture to the bottom, so the title reads on any scene
        f"color=c=black:s={W}x{H},format=yuva420p,geq=lum='0':cb='128':cr='128':"
        f"a='clip(235*pow(clip((Y/{H}-0.40)/0.60,0,1),1.5),0,255)'[fade]",
        "[bg][fade]overlay=format=auto[b0]",
    ]
    cur = "b0"
    fontopt = f"fontfile={font}:" if font else ""

    if THUMB_TAG:
        tagf = os.path.join(tmp, "thumb_tag.txt")
        with open(tagf, "w") as f:
            f.write(THUMB_TAG.upper())
        tsize = 34
        tag_w = int(_text_width(THUMB_TAG.upper(), tsize, embedded) + 44)
        graph.append(f"[{cur}]drawbox=x=36:y=32:w={tag_w}:h=62:color=black@0.78:t=fill[g1]")
        graph.append(f"[g1]drawbox=x=36:y=32:w={tag_w}:h=62:color=0x{THUMB_ACCENT}@0.95:t=3[g2]")
        graph.append(
            f"[g2]drawtext={fontopt}textfile={tagf}:fontsize={tsize}:fontcolor=0x{THUMB_ACCENT}:"
            f"x=36+(({tag_w}-text_w)/2):y=32+((62-text_h)/2)[g3]"
        )
        cur = "g3"

    for k, line in enumerate(lines):
        lf = os.path.join(tmp, f"thumb_l{k}.txt")
        with open(lf, "w") as f:
            f.write(line)
        tw = int(_text_width(line, size, embedded)) + 2 * pad + 8
        tw -= tw % 2
        sw = max(2, int(tw * sq))                     # width after the poster-style squeeze
        sw -= sw % 2
        y_pos = y_first + k * gap
        common = f"fontsize={size}:fontcolor=white:y=(h-text_h)/2:x=(w-text_w)/2"
        graph += [
            # white-on-black masks: letters only, and letters + outline
            f"color=c=black:s={tw}x{ch},drawtext={fontopt}textfile={lf}:{common},format=gray,scale={sw}:{ch},lutyuv=y='clip((val-16)*1.1644,0,255)'[mf{k}]",
            f"color=c=black:s={tw}x{ch},drawtext={fontopt}textfile={lf}:{common}:borderw={bw}:bordercolor=white,"
            f"format=gray,scale={sw}:{ch},lutyuv=y='clip((val-16)*1.1644,0,255)'[mo{k}]",
            # gold gradient (top -> bottom of the letters), cut out by the letter mask
            f"color=c=black:s={sw}x{ch},format=gbrp,geq="
            f"r='{top[0]}+({bot[0]}-{top[0]})*clip((Y-{pad})/{line_h},0,1)':"
            f"g='{top[1]}+({bot[1]}-{top[1]})*clip((Y-{pad})/{line_h},0,1)':"
            f"b='{top[2]}+({bot[2]}-{top[2]})*clip((Y-{pad})/{line_h},0,1)',format=rgba[gr{k}]",
            f"[gr{k}][mf{k}]alphamerge[fill{k}]",
            # black outline layer, plus a soft shadow copy pushed down and to the right
            f"color=c=black:s={sw}x{ch},format=rgba[bk{k}]",
            f"[bk{k}][mo{k}]alphamerge,split=2[ol{k}][sh0{k}]",
            f"[sh0{k}]boxblur=7:2,colorchannelmixer=aa=0.75[sh{k}]",
            f"[{cur}][sh{k}]overlay=x=(W-w)/2+9:y={y_pos}+11:format=auto[s{k}]",
            f"[s{k}][ol{k}]overlay=x=(W-w)/2:y={y_pos}:format=auto[o{k}]",
            f"[o{k}][fill{k}]overlay=x=(W-w)/2:y={y_pos}:format=auto[d{k}]",
        ]
        cur = f"d{k}"

    for q in (2, 5, 9):             # YouTube allows up to 2 MB: lower the quality until it fits
        run(["ffmpeg", "-y", "-i", image_path, "-filter_complex", ";".join(graph), "-map", f"[{cur}]",
             "-frames:v", "1", "-update", "1", "-q:v", str(q), out_path])
        if os.path.getsize(out_path) < 1_900_000:
            break
    return out_path


# ---- HOOK THUMBNAIL (added): the thumbnail has the shape of the film (vertical film = vertical cover) and shows the script's HOOK ----
THUMB_HOOK_ENABLED = True       # False = the old cover (film title, always 1280x720)
THUMB_HOOK_HI_WORDS = 2         # how many key words of the hook get the gold colour (the longest ones)
THUMB_HOOK_BAR = True           # a short gold bar under / above the hook
THUMB_HOOK_TOP = 0.115          # vertical covers: where the hook starts, as a share of the cover height (0.115 = near the top)


def thumb_canvas():
    """Cover size that follows the film: vertical 1080x1920, square 1080x1080, 4:3 1280x960, otherwise 1280x720."""
    if HEIGHT > WIDTH:
        return 1080, 1920, "vertical"
    if WIDTH == HEIGHT:
        return 1080, 1080, "square"
    if WIDTH * 3 == HEIGHT * 4:
        return 1280, 960, "four-three"
    return 1280, 720, "horizontal"


def make_hook_thumbnail(image_path, text, title, out_path, tmp):
    """Cover in the shape of the film with the HOOK as the main design: big poster capitals, the key words in gold,
    white for the rest, thick black outline + shadow, a dark fade for reading, a small gold bar and the film title as a small label.
    Vertical covers carry the hook in the upper part (the lower part is covered by buttons in the Shorts feed);
    horizontal covers carry it along the bottom like a movie poster."""
    W, H, shape = thumb_canvas()
    vertical = shape in ("vertical", "square")
    font, embedded = _title_font_file(tmp)
    if not embedded:
        raise RuntimeError("poster font not available")
    sq = THUMB_SQUEEZE

    clean = re.sub(r"\s+", " ", str(text or "").replace("\u2019", "'")).strip().upper()
    words = [w for w in (re.sub(r"[^A-Z0-9'!?&.,:\-]", "", t) for t in clean.split(" ")) if w]
    if not words:
        raise RuntimeError("no hook text")
    # key words: the longest ones (5+ letters) get the gold colour
    cand = sorted({re.sub(r"[^A-Z0-9']", "", w) for w in words if len(re.sub(r"[^A-Z0-9']", "", w)) >= 5}, key=lambda s: -len(s))[:THUMB_HOOK_HI_WORDS]
    is_hi = [re.sub(r"[^A-Z0-9']", "", w) in cand for w in words]

    margin = int(W * 0.06)
    max_w = W - 2 * margin
    max_lines = 5 if vertical else 3
    max_block = 0.46 * H if vertical else 0.60 * H
    size = {"vertical": 230, "square": 190, "four-three": 170, "horizontal": 170}[shape]

    def layout(sz):
        gap = _text_width(" ", sz, True) * sq * 1.25
        lines, cur, cur_w = [], [], 0.0
        for i, w in enumerate(words):
            ww = _text_width(w, sz, True) * sq
            add = ww if not cur else gap + ww
            if cur and cur_w + add > max_w:
                lines.append(cur); cur, cur_w = [i], ww
            else:
                cur.append(i); cur_w += add
        if cur:
            lines.append(cur)
        return lines, gap

    while True:
        lines, gap = layout(size)
        line_h_ = int(size * 1.02)
        step_ = int(line_h_ * 0.90)
        block_ = line_h_ + step_ * (len(lines) - 1)
        widest_ok = all(_text_width(w, size, True) * sq <= max_w for w in words)
        if (len(lines) <= max_lines and block_ <= max_block and widest_ok) or size <= 70:
            break
        size -= 6

    pad = max(10, size // 7)
    line_h = int(size * 1.02)
    ch = line_h + 2 * pad
    ch -= ch % 2
    step = int(line_h * 0.90)
    block_h = line_h + step * (len(lines) - 1)
    bw = max(7, size // 11)
    shx, shy = max(6, size // 20), max(8, size // 16)
    bar_h = max(8, int(H * 0.0075))
    bar_w = int(W * 0.30)

    if vertical:
        y_first = int(H * THUMB_HOOK_TOP)
        bar_y = y_first + block_h + int(size * 0.20)
    else:
        bar_y = H - 34 - bar_h
        y_first = bar_y - int(size * 0.16) - block_h

    gold_top = _hex_rgb(THUMB_COLOR_TOP, "FFE45A")
    gold_bot = _hex_rgb(THUMB_COLOR_BOTTOM, "F07A0A")
    white_top, white_bot = (255, 255, 255), (206, 206, 206)

    # background: the scene picture filling the cover, a little richer, with a dark fade behind the text
    if vertical:
        fade_a = f"clip(240*pow(clip((0.66-Y/{H})/0.66,0,1),1.35),0,255)"          # dark at the top, clear by two thirds
    else:
        fade_a = f"clip(235*pow(clip((Y/{H}-0.38)/0.62,0,1),1.5),0,255)"            # dark at the bottom
    graph = [
        f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}:(iw-{W})/2:(ih-{H})*0.18,"
        "eq=contrast=1.12:saturation=1.25:brightness=0.01,unsharp=5:5:0.6:5:5:0.0,vignette=angle=PI/6[bg]",
        f"color=c=black:s={W}x{H},format=yuva420p,geq=lum='0':cb='128':cr='128':a='{fade_a}'[fade]",
        "[bg][fade]overlay=format=auto[b0]",
    ]
    cur = "b0"
    fontopt = f"fontfile={font}:"

    # small label with the film title (only when the cover shows a real hook and the label is switched on)
    tag_text = re.sub(r"[^A-Z0-9'!?&.,:\- ]", "", str(title or "").upper()).strip()[:26]
    if THUMB_TAG and tag_text and tag_text != clean:
        tagf = os.path.join(tmp, "thumb_tag.txt")
        with open(tagf, "w") as f:
            f.write(tag_text)
        tsize = int(H * 0.024) if vertical else 34
        tag_w = int(_text_width(tag_text, tsize, True) + tsize * 1.4)
        tag_h = int(tsize * 1.85)
        tx, ty = (margin, int(H * 0.035)) if vertical else (36, 32)
        graph.append(f"[{cur}]drawbox=x={tx}:y={ty}:w={tag_w}:h={tag_h}:color=black@0.78:t=fill[g1]")
        graph.append(f"[g1]drawbox=x={tx}:y={ty}:w={tag_w}:h={tag_h}:color=0x{THUMB_ACCENT}@0.95:t=3[g2]")
        graph.append(f"[g2]drawtext={fontopt}textfile={tagf}:fontsize={tsize}:fontcolor=0x{THUMB_ACCENT}:"
                     f"x={tx}+(({tag_w}-text_w)/2):y={ty}+(({tag_h}-text_h)/2)[g3]")
        cur = "g3"

    # the hook, word by word (each word is its own little poster canvas, so key words can have another colour)
    for li, line in enumerate(lines):
        widths = [_text_width(words[i], size, True) * sq for i in line]
        line_w = sum(widths) + gap * (len(line) - 1)
        x = (W - line_w) / 2.0
        y_pos = y_first + li * step
        for i, ww in zip(line, widths):
            k = f"{li}_{i}"
            wf = os.path.join(tmp, f"thumb_w{k}.txt")
            with open(wf, "w") as f:
                f.write(words[i])
            tw = int(_text_width(words[i], size, True)) + 2 * pad + 8
            tw -= tw % 2
            sw = max(2, int(tw * sq))
            sw -= sw % 2
            lead = (pad + 4) * sq                          # empty margin of the canvas to the left of the first letter
            ox = int(round(x - lead))
            top, bot = (gold_top, gold_bot) if is_hi[i] else (white_top, white_bot)
            common = f"fontsize={size}:fontcolor=white:y=(h-text_h)/2:x=(w-text_w)/2"
            graph += [
                f"color=c=black:s={tw}x{ch},drawtext={fontopt}textfile={wf}:{common},format=gray,scale={sw}:{ch},lutyuv=y='clip((val-16)*1.1644,0,255)'[mf{k}]",
                f"color=c=black:s={tw}x{ch},drawtext={fontopt}textfile={wf}:{common}:borderw={bw}:bordercolor=white,"
                f"format=gray,scale={sw}:{ch},lutyuv=y='clip((val-16)*1.1644,0,255)'[mo{k}]",
                f"color=c=black:s={sw}x{ch},format=gbrp,geq="
                f"r='{top[0]}+({bot[0]}-{top[0]})*clip((Y-{pad})/{line_h},0,1)':"
                f"g='{top[1]}+({bot[1]}-{top[1]})*clip((Y-{pad})/{line_h},0,1)':"
                f"b='{top[2]}+({bot[2]}-{top[2]})*clip((Y-{pad})/{line_h},0,1)',format=rgba[gr{k}]",
                f"[gr{k}][mf{k}]alphamerge[fill{k}]",
                f"color=c=black:s={sw}x{ch},format=rgba[bk{k}]",
                f"[bk{k}][mo{k}]alphamerge,split=2[ol{k}][sh0{k}]",
                f"[sh0{k}]boxblur=7:2,colorchannelmixer=aa=0.75[sh{k}]",
                f"[{cur}][sh{k}]overlay=x={ox}+{shx}:y={y_pos}+{shy}:format=auto[s{k}]",
                f"[s{k}][ol{k}]overlay=x={ox}:y={y_pos}:format=auto[o{k}]",
                f"[o{k}][fill{k}]overlay=x={ox}:y={y_pos}:format=auto[d{k}]",
            ]
            cur = f"d{k}"
            x += ww + gap

    if THUMB_HOOK_BAR:
        graph.append(f"[{cur}]drawbox=x=({W}-{bar_w})/2:y={bar_y}:w={bar_w}:h={bar_h}:color=0x{THUMB_ACCENT}@0.95:t=fill[gbar]")
        cur = "gbar"

    for q in (2, 5, 9):             # YouTube allows up to 2 MB: lower the quality until it fits
        run(["ffmpeg", "-y", "-i", image_path, "-filter_complex", ";".join(graph), "-map", f"[{cur}]",
             "-frames:v", "1", "-update", "1", "-q:v", str(q), out_path])
        if os.path.getsize(out_path) < 1_900_000:
            break
    return out_path


def main():
    manifest = json.loads(os.environ["MANIFEST_JSON"])
    global WIDTH, HEIGHT, SUBTITLES, SUB_WRAP_CHARS, SUB_CHUNK_CHARS, SUB_BOTTOM_MARGIN, SUB_FONT_SIZE
    _asp = str(manifest.get("aspect") or "").strip() or "16:9"      # v61: no aspect written = horizontal 16:9
    if _asp in ("16:9", "9:16", "4:3", "1:1"):
        WIDTH, HEIGHT = {"16:9": (1920, 1080), "9:16": (1080, 1920), "4:3": (1440, 1080), "1:1": (1080, 1080)}[_asp]
        # subtitle layout for each shape: (characters per line, characters per screen, bottom margin, font size)
        SUB_WRAP_CHARS, SUB_CHUNK_CHARS, SUB_BOTTOM_MARGIN, SUB_FONT_SIZE = {
            "16:9": (46, 92, 90, 46), "9:16": (26, 52, 300, 52), "4:3": (38, 76, 100, 46), "1:1": (32, 64, 140, 46)}[_asp]
        SUBTITLES = manifest.get("subtitles", True) is not False or _asp == "9:16"   # vertical films always get the CapCut-style captions
    print(f"Output {WIDTH}x{HEIGHT}, subtitles {'on' if SUBTITLES else 'off'}")
    episode_id = manifest["episodeId"]
    script_meta = {}
    try:
        script_meta = _script_blocks(manifest) if _CIN_LIBS else {}
    except Exception:
        script_meta = {}
    cinema_on = CINEMA and _CIN_LIBS and manifest.get("cinema", True) is not False
    print(f"cinema camera: {'on' if cinema_on else 'off (needs numpy + opencv-python-headless + onnxruntime)'}")

    # animated clips offered by n8n: {scene_number: clip_url}
    clip_by_scene = {}
    if manifest.get("useAnimation"):
        for c in manifest.get("clips") or []:
            try:
                if c and c.get("clip_url"):
                    clip_by_scene[int(c["scene_number"])] = c["clip_url"]
            except (TypeError, ValueError, KeyError):
                continue
    print(f"animated clips offered for {len(clip_by_scene)} scene(s)")

    # sound-effect plan offered by n8n (from the script's SOUND lines and the story itself)
    sfx_cues = []
    if SFX_ENABLED:
        for c in manifest.get("sfx") or []:
            if isinstance(c, dict) and (c.get("urls") or c.get("kind") == "music"):
                sfx_cues.append(c)
    print(f"sound-effect cues offered: {len(sfx_cues)}")

    try:
        ep_rows = sb_get(f"episodes?id=eq.{episode_id}&select=script_lines,scenes,title,status")
        if not ep_rows:
            raise RuntimeError(f"No episode found with id {episode_id}")
        ep = ep_rows[0]

        # a duplicate request for an episode that is already built: do nothing
        if ep.get("status") in ("assemblycomplete", "telegram_preview", "awaiting_approval", "archived"):
            print(f"episode {episode_id} is already '{ep.get('status')}' - skipping duplicate build")
            return

        script_lines = ep["script_lines"]
        scenes = sorted(ep["scenes"], key=lambda s: s["scene_number"])

        voice_assets = sb_get(
            f"assets?episode_id=eq.{episode_id}&type=eq.voice_line&status=eq.completed&select=line_index,scene_number,file_url"
        )
        voice_by_scene = {}
        for a in voice_assets:
            voice_by_scene.setdefault(a["scene_number"], []).append(a)
        for scene_num in voice_by_scene:
            voice_by_scene[scene_num].sort(key=lambda a: a["line_index"])

        image_assets = sb_get(
            f"assets?episode_id=eq.{episode_id}&type=eq.scene_image&status=eq.completed&select=scene_number,file_url"
        )
        image_by_scene = {a["scene_number"]: a["file_url"] for a in image_assets}

        lines_by_scene = {}
        for l in script_lines:
            lines_by_scene.setdefault(l["scene_number"], []).append(l)

        with tempfile.TemporaryDirectory() as tmp:
            segment_paths = []
            scene_spans = {}     # scene number -> (start second in the film, length), for placing sound effects
            film_cursor = 0.0

            for i, scene in enumerate(scenes):
                scene_num = scene["scene_number"]
                print(f"--- scene {scene_num} ---")

                audio_parts = []
                for j, asset in enumerate(voice_by_scene.get(scene_num, [])):
                    part_path = os.path.join(tmp, f"s{scene_num}_line{j}.mp3")
                    download(asset["file_url"], part_path)
                    audio_parts.append(part_path)

                scene_audio_path = os.path.join(tmp, f"s{scene_num}_audio.wav")
                if audio_parts:
                    concat_list = os.path.join(tmp, f"s{scene_num}_audio_list.txt")
                    with open(concat_list, "w") as f:
                        for p in audio_parts:
                            f.write(f"file '{p}'\n")
                    # decode to one clean, identical format (44.1kHz stereo) so every scene joins perfectly
                    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list,
                         "-ar", "44100", "-ac", "2", scene_audio_path])
                    duration = probe_duration(scene_audio_path) + (CLIP_SCENE_TAIL_SECONDS if clip_by_scene.get(int(scene_num)) else SCENE_TAIL_SECONDS)
                else:
                    print(f"::warning::scene {scene_num} has no completed voice lines, using silence")
                    duration = max(scene.get("duration_seconds", 3), 3)
                    run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                         "-t", str(duration), scene_audio_path])

                sub_lines = lines_by_scene.get(scene_num, [])
                sub_text = " ".join(
                    l["text"] if l["speaker"] == "NARRATOR" else f'{l["speaker"].title()}: {l["text"]}'
                    for l in sub_lines
                )

                segment_path = os.path.join(tmp, f"segment_{i:03d}.mp4")

                # v67: real word times of this scene's voice, for the yellow box
                if SUBTITLES and SHORTS_CAPTIONS and HEIGHT > WIDTH and audio_parts:
                    pres = [([] if l["speaker"] == "NARRATOR" else f'{l["speaker"].title()}:'.split()) for l in sub_lines]
                    if len(audio_parts) == len(sub_lines):
                        blocks = [(p, pr, l["text"]) for p, pr, l in zip(audio_parts, pres, sub_lines)]
                    else:
                        blocks = [(scene_audio_path, [x for pr in pres for x in pr], " ".join(l["text"] for l in sub_lines))]
                    prepare_caption_timing(f"s{i:03d}", blocks)
                    if f"s{i:03d}" in CAPTION_TIMING:
                        CAPTION_TIMING[f"c{i:03d}"] = CAPTION_TIMING[f"s{i:03d}"]

                # 1) animated clip, if n8n gave us one for this scene
                built = False
                # 0) v84: YouTube quiz card (voice reads the question, countdown 5..0, the answer ticks)
                card = scene.get("quiz_card") or (script_meta.get(int(scene_num)) or {}).get("quiz_card")
                if QUIZ_CARDS and isinstance(card, dict):
                    try:
                        voice_len = probe_duration(scene_audio_path) if audio_parts else 0.0
                        ph = str(card.get("phase") or "ask")
                        qdur = quiz_ask_duration(voice_len) if ph == "ask" else voice_len + (1.0 if ph == "reveal" else 0.8)
                        qdur = max(qdur, 3.0)
                        qimg = os.path.join(tmp, f"s{scene_num}.png")
                        qurl = image_by_scene.get(scene_num)
                        if qurl and not os.path.exists(qimg):
                            download(qurl, qimg)
                        if not os.path.exists(qimg):
                            qimg = ""
                        build_quiz_segment(i, card, qimg, scene_audio_path, qdur, voice_len, tmp, segment_path)
                        duration = qdur
                        built = True
                    except Exception as e:
                        print(f"::warning::scene {scene_num}: quiz card not drawn ({str(e)[:200]}) - using the normal picture")
                clip_url = None if built else clip_by_scene.get(int(scene_num))
                if clip_url:
                    try:
                        clip_path = os.path.join(tmp, f"s{scene_num}_clip.mp4")
                        if not os.path.exists(clip_path):
                            download(clip_url, clip_path)
                        next_frame = None
                        if CLIP_JOIN_BLEND > 0 and i + 1 < len(scenes):
                            try:
                                nn = int(scenes[i + 1]["scene_number"])
                                nurl = clip_by_scene.get(nn)
                                if nurl:
                                    npath = os.path.join(tmp, f"s{nn}_clip.mp4")
                                    if not os.path.exists(npath):
                                        download(nurl, npath)
                                    nf = os.path.join(tmp, f"s{nn}_first.png")
                                    run(["ffmpeg", "-y", "-i", npath, "-frames:v", "1", nf])
                                    if os.path.exists(nf):
                                        next_frame = nf
                            except Exception as e:
                                print(f"::warning::scene {scene_num}: soft join skipped ({e})")
                                next_frame = None
                        build_clip_segment(i, clip_path, scene_audio_path, duration, sub_text, tmp, segment_path, next_frame)
                        built = True
                        print(f"scene {scene_num}: built from the animated clip")
                    except Exception as e:
                        print(f"::warning::scene {scene_num}: clip could not be used ({e}) - using the still image")

                # 2) still image with the slow zoom (also the fallback)
                if not built:
                    image_path = os.path.join(tmp, f"s{scene_num}.png")
                    image_url = image_by_scene.get(scene_num)
                    if image_url:
                        if not os.path.exists(image_path):
                            download(image_url, image_path)
                    else:
                        print(f"::warning::scene {scene_num} has no completed image, using a plain fallback")
                        run(["ffmpeg", "-y", "-f", "lavfi",
                             "-i", f"color=c=gray20:s={WIDTH}x{HEIGHT}", "-frames:v", "1", image_path])
                    if cinema_on and image_url:
                        try:
                            plan = cinema_plan(i, scene, script_meta.get(int(scene_num)))
                            nimg, nplan = None, None
                            if i + 1 < len(scenes):
                                nsc = scenes[i + 1]
                                nn = int(nsc["scene_number"])
                                nurl = image_by_scene.get(nn)
                                if nurl and not clip_by_scene.get(nn):
                                    nimg = os.path.join(tmp, f"s{nn}.png")
                                    if not os.path.exists(nimg):
                                        download(nurl, nimg)
                                    nplan = cinema_plan(i + 1, nsc, script_meta.get(nn))
                            build_cinema_segment(i, image_path, scene_audio_path, duration, sub_text, tmp, segment_path,
                                                 plan, nimg, nplan, is_first=(i == 0), is_last=(i == len(scenes) - 1))
                            built = True
                        except Exception as e:
                            print(f"::warning::scene {scene_num}: cinema camera not used ({e}) - using the slow zoom")
                    if not built:
                        build_segment(i, image_path, scene_audio_path, duration, sub_text, tmp, segment_path)

                segment_paths.append(segment_path)
                try:
                    seg_len = probe_duration(segment_path)
                except Exception:
                    seg_len = duration
                scene_spans[int(scene_num)] = (film_cursor, seg_len)
                film_cursor += seg_len

            final_list = os.path.join(tmp, "final_list.txt")
            with open(final_list, "w") as f:
                for p in segment_paths:
                    f.write(f"file '{p}'\n")
            joined_path = os.path.join(tmp, "joined.mp4")
            # v66: the scenes are joined with ONE clean re-encode (not a stream copy): every join gets continuous timestamps,
            # the same frame rate/size/pixel format/aspect, and no audio-priming gap, so no player can hiccup at a cut.
            run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", final_list,
                 "-vf", f"scale={WIDTH}:{HEIGHT}:flags=lanczos,setsar=1,fps={FPS},format=yuv420p",
                 "-af", "aresample=async=1:first_pts=0,aformat=sample_rates=44100:channel_layouts=stereo",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-r", str(FPS),
                 "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
                 "-fflags", "+genpts", "-avoid_negative_ts", "make_zero", joined_path])

            if sfx_cues:
                try:
                    joined_path = apply_sfx(joined_path, sfx_cues, scene_spans, tmp)
                except Exception as e:
                    print(f"::warning::sound effects could not be added ({e}) - the film keeps its narration only")

            final_path = os.path.join(tmp, "final.mp4")
            master_audio_and_music(joined_path, final_path)
            public_url = None
            if RELEASE_ENABLED:
                try:
                    public_url = upload_to_release(final_path, episode_id, tmp)
                except Exception as e:
                    print(f"::warning::GitHub upload not used ({e}) - using Supabase Storage")
                    public_url = None
            if public_url and PREVIEW_ENABLED:
                try:
                    print("preview link: " + upload_preview_copy(final_path, episode_id, tmp))
                except Exception as e:
                    print(f"::warning::preview copy not uploaded ({e})")
            if not public_url:
                try:
                    final_path = shrink_to_limit(final_path, tmp)
                except Exception as e:
                    print(f"::warning::could not squeeze the film ({e}) - uploading it as it is")

                storage_path = f"videos/{episode_id}.mp4"
                size_mb = os.path.getsize(final_path) / 1e6
                print(f"uploading the finished video: {size_mb:.0f} MB")
                with open(final_path, "rb") as f:
                    # streamed from disk (not loaded into memory) and given up to 30 minutes: a long film is a big file
                    r = requests.post(
                        f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{storage_path}",
                        headers={
                            "apikey": SUPABASE_KEY,
                            "Authorization": f"Bearer {SUPABASE_KEY}",
                            "Content-Type": "video/mp4",
                            "x-upsert": "true",
                        },
                        data=f,
                        timeout=(30, 1800),
                    )
                if r.status_code == 413 or "maximum allowed size" in r.text:
                    raise RuntimeError(
                        f"Supabase refused the finished video ({size_mb:.0f} MB): it is bigger than the storage file-size limit "
                        "(the Free plan allows 50 MB). Raise the limit in Supabase > Storage > Settings (needs the Pro plan), then send /resume."
                    )
                r.raise_for_status()

                public_url = f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{storage_path}"

            # YouTube cover picture (never able to fail the film: if anything goes wrong n8n falls back to scene 1)
            if THUMB_ENABLED:
                try:
                    tscene = pick_thumbnail_scene(scenes, image_by_scene, manifest)
                    if tscene:
                        timg = os.path.join(tmp, "thumb_src.png")
                        download(image_by_scene[tscene], timg)
                        tout = os.path.join(tmp, "thumbnail.jpg")
                        _title = manifest.get("title") or ep.get("title") or ""
                        _s1 = scenes[0] if scenes else {}
                        _hook = str(_s1.get("hook") or _s1.get("hook_text") or "").strip()    # the script's HOOK line (saved on scene 1)
                        try:
                            if not THUMB_HOOK_ENABLED:
                                raise RuntimeError("hook cover switched off")
                            make_hook_thumbnail(timg, _hook or _title, _title, tout, tmp)
                        except Exception as e:
                            print(f"::warning::hook cover not used ({e}) - making the classic cover instead")
                            make_thumbnail(timg, _title, tout, tmp)
                        with open(tout, "rb") as tf:
                            tr = requests.post(
                                f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/thumbnails/{episode_id}.jpg",
                                headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                                         "Content-Type": "image/jpeg", "x-upsert": "true"},
                                data=tf, timeout=(30, 120))
                        print(f"thumbnail from scene {tscene}: upload status {tr.status_code}")
                except Exception as e:
                    print(f"::warning::thumbnail could not be made ({e}) - YouTube will use scene 1")

            updated = sb_patch(
                f"episodes?id=eq.{episode_id}&status=in.(assembly_running,assemblycomplete,failed)",
                {"status": "assemblycomplete", "output_url": public_url},
            )
            if not updated:
                print("::warning::episode status was already moved on - not changing it")
            print(f"done: {public_url}")

    except Exception as e:
        print(f"::error::assembly failed: {e}")
        mark_failed(episode_id, str(e))
        raise


if __name__ == "__main__":
    main()
