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
import os
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
FADE_SECONDS = 0.30         # fade in/out at the start/end of each scene
SCENE_TAIL_SECONDS = 0.4    # pause after each scene's narration
SUB_FONT_SIZE = 46
SUB_WRAP_CHARS = 46         # characters per subtitle line (horizontal film)
SUB_CHUNK_CHARS = 92        # characters per subtitle screen (about 2 lines)
SUB_BOTTOM_MARGIN = 90      # pixels from the bottom
MUSIC_PATH = "assets/music/default.mp3"
MUSIC_VOLUME = 0.25         # before ducking; it dips automatically under the voice
VIDEO_CRF = 23              # lower = better quality, bigger file
VIDEO_MAXRATE = "3M"        # caps file size
CLIP_FILL_MODE = "loop"     # clip shorter than the narration: "loop" it, or "hold" its last frame

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


def subtitle_filters(text, duration, tmp, tag):
    """Return a list of drawtext filters, each shown only during its time slot."""
    if not SUBTITLES:
        return []
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


def build_clip_segment(scene_index, clip_path, audio_path, duration, sub_text, tmp, segment_path):
    """One scene from an animated clip: the clip is fitted to the 9:16 frame on a blurred
    copy of itself (same look as the stills), then subtitles + fades + the scene's voice.
    A clip shorter than the narration is looped (or its last frame is held); a longer one is cut."""
    clip_dur = probe_duration(clip_path)
    fade_out_start = max(duration - FADE_SECONDS, 0)
    short = clip_dur < duration

    chain = (
        "[0:v]split=2[bgsrc][fgsrc];"
        f"[bgsrc]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={WIDTH}:{HEIGHT},boxblur=40:6,eq=brightness=-0.12[bg];"
        f"[fgsrc]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease:flags=lanczos[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,fps={FPS}"
    )
    if short and CLIP_FILL_MODE == "hold":
        chain += f",tpad=stop_mode=clone:stop_duration={duration - clip_dur + 0.5:.3f}"

    steps = subtitle_filters(sub_text, duration, tmp, f"c{scene_index:03d}")
    steps.append(f"fade=t=in:st=0:d={FADE_SECONDS}")
    steps.append(f"fade=t=out:st={fade_out_start:.3f}:d={FADE_SECONDS}")
    steps.append("format=yuv420p")
    fc = chain + "," + ",".join(steps) + "[vout]"

    af = (
        f"afade=t=in:st=0:d=0.2,"
        f"afade=t=out:st={max(duration - 0.25, 0):.3f}:d=0.25,"
        "apad"
    )

    cmd = ["ffmpeg", "-y"]
    if short and CLIP_FILL_MODE != "hold":
        cmd += ["-stream_loop", "-1"]
    cmd += [
        "-i", clip_path,
        "-i", audio_path,
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
    if os.path.exists(MUSIC_PATH):
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
    "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"
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
    SUBTITLES = manifest.get("subtitles", True) is not False
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
                    duration = probe_duration(scene_audio_path) + SCENE_TAIL_SECONDS
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
                        download(clip_url, clip_path)
                        build_clip_segment(i, clip_path, scene_audio_path, duration, sub_text, tmp, segment_path)
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

            final_list = os.path.join(tmp, "final_list.txt")
            with open(final_list, "w") as f:
                for p in segment_paths:
                    f.write(f"file '{p}'\n")
            joined_path = os.path.join(tmp, "joined.mp4")
            run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", final_list,
                 "-c", "copy", joined_path])

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
