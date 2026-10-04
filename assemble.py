#!/usr/bin/env python3
"""
assemble.py - Betrayer episode assembler (polished version).

Polish added on top of the original pipeline:
  - images are fitted to the 9:16 frame on a blurred background (no stretching)
  - smooth eased zoom/pan, changing style scene by scene
  - timed subtitles in short chunks (not one giant block)
  - soft fades between scenes
  - consistent audio format per scene (no glitches on join)
  - narration loudness levelled, background music ducked under the voice
  - faststart + capped bitrate (smaller, quicker-to-open file)
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
WIDTH, HEIGHT = 1080, 1920
FPS = 30

# ---------------- POLISH SETTINGS (edit here) ----------------
ZOOM_AMOUNT = 0.12          # how far the slow zoom goes (0.12 = 12%)
FADE_SECONDS = 0.35         # fade in/out at the start/end of each scene
SCENE_TAIL_SECONDS = 0.4    # pause after each scene's narration
SUB_FONT_SIZE = 52
SUB_WRAP_CHARS = 26         # characters per subtitle line
SUB_CHUNK_CHARS = 52        # characters per subtitle screen (about 2 lines)
SUB_BOTTOM_MARGIN = 300     # pixels from the bottom (keeps clear of app buttons)
MUSIC_PATH = "assets/music/default.mp3"
MUSIC_VOLUME = 0.25         # before ducking; it dips automatically under the voice
VIDEO_CRF = 23              # lower = better quality, bigger file
VIDEO_MAXRATE = "3M"        # caps file size
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
    """Different slow camera move for each scene: in, out, drift. Eased start/end."""
    n = max(total_frames, 1)
    p = f"(on/{n})"
    ease = f"({p}*{p}*(3-2*{p}))"
    mode = scene_index % 3
    a = ZOOM_AMOUNT
    if mode == 0:      # slow push in, centred
        z = f"1+{a}*{ease}"
        x = "iw/2-(iw/zoom/2)"
        y = "ih/2-(ih/zoom/2)"
    elif mode == 1:    # slow pull out, centred
        z = f"1+{a}-{a}*{ease}"
        x = "iw/2-(iw/zoom/2)"
        y = "ih/2-(ih/zoom/2)"
    else:              # push in while drifting upward
        z = f"1+{a}*{ease}"
        x = "iw/2-(iw/zoom/2)"
        y = f"(ih-ih/zoom)*(0.65-0.45*{ease})"
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

def main():
    manifest = json.loads(os.environ["MANIFEST_JSON"])
    episode_id = manifest["episodeId"]

    try:
        ep_rows = sb_get(f"episodes?id=eq.{episode_id}&select=script_lines,scenes,title")
        if not ep_rows:
            raise RuntimeError(f"No episode found with id {episode_id}")
        ep = ep_rows[0]
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

                image_path = os.path.join(tmp, f"s{scene_num}.png")
                image_url = image_by_scene.get(scene_num)
                if image_url:
                    download(image_url, image_path)
                else:
                    print(f"::warning::scene {scene_num} has no completed image, using a plain fallback")
                    run(["ffmpeg", "-y", "-f", "lavfi",
                         "-i", f"color=c=gray20:s={WIDTH}x{HEIGHT}", "-frames:v", "1", image_path])

                sub_lines = lines_by_scene.get(scene_num, [])
                sub_text = " ".join(
                    l["text"] if l["speaker"] == "NARRATOR" else f'{l["speaker"].title()}: {l["text"]}'
                    for l in sub_lines
                )

                segment_path = os.path.join(tmp, f"segment_{i:03d}.mp4")
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
            with open(final_path, "rb") as f:
                r = requests.post(
                    f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{storage_path}",
                    headers={
                        "apikey": SUPABASE_KEY,
                        "Authorization": f"Bearer {SUPABASE_KEY}",
                        "Content-Type": "video/mp4",
                        "x-upsert": "true",
                    },
                    data=f.read(),
                    timeout=300,
                )
                r.raise_for_status()

            public_url = f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{storage_path}"

                        sb_patch(f"episodes?id=eq.{episode_id}&status=in.(assembly_running,assemblycomplete)", {
                "status": "assemblycomplete",
                "output_url": public_url,
            })
            print(f"done: {public_url}")

    except Exception as e:
        print(f"::error::assembly failed: {e}")
        mark_failed(episode_id, str(e))
        raise


if __name__ == "__main__":
    main()
