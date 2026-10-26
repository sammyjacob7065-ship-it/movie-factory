#!/usr/bin/env python3
"""
assemble.py - Betrayer episode assembler (polished version).

Polish added on top of the original pipeline:
  - images are fitted to the 9:16 frame on a blurred background (no stretching)
  - smooth eased zoom/pan, six different camera moves in rotation scene by scene
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
import subprocess
import tempfile
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
# exactly while that word is being spoken. The timing of each word is shared out over the spoken part of the scene by its number of letters.
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
WATERMARK_TEXT = "Sammys_galories"   # "" = no watermark
WATERMARK_OPACITY = 0.40            # 0.0 invisible ... 1.0 solid; 0.35-0.5 looks professional
WATERMARK_SIZE = 0.028              # letter height as a share of the short side of the frame (0.028 = small)
WATERMARK_POSITION = "top-right"    # top-right, top-left, bottom-right or bottom-left
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
MUSIC_VOLUME = 15          # before ducking; it dips automatically under the voice (was 0.25: too quiet)
VIDEO_CRF = 23              # lower = better quality, bigger file
VIDEO_MAXRATE = "3M"        # caps file size
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
    speech = max(duration - SCENE_TAIL_SECONDS, 0.5)
    weights = [len(w) + 2 + (3 if stop else 0) for w, stop in words]      # a word that ends a sentence or a comma gets a short pause
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
    pos = {
        "top-right": (f"w-text_w-{m}", f"{m}"),
        "top-left": (f"{m}", f"{m}"),
        "bottom-right": (f"w-text_w-{m}", f"h-text_h-{m}"),
        "bottom-left": (f"{m}", f"h-text_h-{m}"),
    }.get(WATERMARK_POSITION, (f"w-text_w-{m}", f"{m}"))
    safe = WATERMARK_TEXT.replace("\\", "").replace("'", "").replace(":", "").replace("%", "")
    parts = []
    font = find_font()
    if font:
        parts.append(f"fontfile={font}")
    parts += [
        f"text='{safe}'",
        "expansion=none",
        f"fontsize={size}",
        f"fontcolor=white@{WATERMARK_OPACITY}",
        f"shadowcolor=black@{WATERMARK_OPACITY * 0.6:.2f}",
        "shadowx=2", "shadowy=2",
        f"x={pos[0]}", f"y={pos[1]}",
    ]
    return ["drawtext=" + ":".join(parts)]


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
        "-maxrate", VIDEO_MAXRATE, "-bufsize", "6M", "-pix_fmt", "yuv420p", "-r", str(FPS),
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
        "-maxrate", VIDEO_MAXRATE, "-bufsize", "6M", "-pix_fmt", "yuv420p", "-r", str(FPS),
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

                # 1) animated clip, if n8n gave us one for this scene
                built = False
                clip_url = clip_by_scene.get(int(scene_num))
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
                        download(image_url, image_path)
                    else:
                        print(f"::warning::scene {scene_num} has no completed image, using a plain fallback")
                        run(["ffmpeg", "-y", "-f", "lavfi",
                             "-i", f"color=c=gray20:s={WIDTH}x{HEIGHT}", "-frames:v", "1", image_path])
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
                        make_thumbnail(timg, manifest.get("title") or ep.get("title") or "", tout, tmp)
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
