#!/usr/bin/env python3
"""
assemble.py - Betrayer episode assembler.
"""

import json
import os
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

                scene_audio_path = os.path.join(tmp, f"s{scene_num}_audio.mp3")
                if audio_parts:
                    concat_list = os.path.join(tmp, f"s{scene_num}_audio_list.txt")
                    with open(concat_list, "w") as f:
                        for p in audio_parts:
                            f.write(f"file '{p}'\n")
                    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list,
                         "-c", "copy", scene_audio_path])
                    duration = probe_duration(scene_audio_path) + 0.4
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
                sub_text = "\n".join(
                    l["text"] if l["speaker"] == "NARRATOR" else f'{l["speaker"].title()}: {l["text"]}'
                    for l in sub_lines
                ) or ""
                sub_text = "\n".join(textwrap.wrap(sub_text, width=38)) if sub_text else ""
                subtitle_file = os.path.join(tmp, f"s{scene_num}_sub.txt")
                with open(subtitle_file, "w") as f:
                    f.write(sub_text)

                total_frames = int(duration * FPS)
                segment_path = os.path.join(tmp, f"segment_{i:03d}.mp4")
                drawtext = (
                    f"drawtext=textfile='{subtitle_file}':fontcolor=white:fontsize=42:"
                    f"box=1:boxcolor=black@0.55:boxborderw=20:x=(w-text_w)/2:y=h-th-140:"
                    f"line_spacing=10" if sub_text else None
                )
                vf_parts = [
                    f"scale={WIDTH*2}:{HEIGHT*2}",
                    f"zoompan=z='min(zoom+0.0007,1.2)':d={total_frames}:s={WIDTH}x{HEIGHT}:fps={FPS}",
                ]
                if drawtext:
                    vf_parts.append(drawtext)
                vf_parts.append(f"fade=t=in:st=0:d=0.3,fade=t=out:st={max(duration-0.3,0)}:d=0.3")
                vf = ",".join(vf_parts)

                run([
                    "ffmpeg", "-y", "-loop", "1", "-i", image_path, "-i", scene_audio_path,
                    "-vf", vf, "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-af", f"afade=t=in:st=0:d=0.2,afade=t=out:st={max(duration-0.2,0)}:d=0.2",
                    "-c:a", "aac", "-shortest", "-t", str(duration), segment_path,
                ])
                segment_paths.append(segment_path)

            final_list = os.path.join(tmp, "final_list.txt")
            with open(final_list, "w") as f:
                for p in segment_paths:
                    f.write(f"file '{p}'\n")
            final_path = os.path.join(tmp, "final.mp4")
            run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", final_list,
                 "-c", "copy", final_path])

            music_path = "assets/music/default.mp3"
            if os.path.exists(music_path):
                mixed_path = os.path.join(tmp, "final_with_music.mp4")
                run([
                    "ffmpeg", "-y", "-i", final_path, "-stream_loop", "-1", "-i", music_path,
                    "-filter_complex", "[1:a]volume=0.08[music];[0:a][music]amix=inputs=2:duration=first[aout]",
                    "-map", "0:v", "-map", "[aout]", "-c:v", "copy", "-c:a", "aac",
                    "-shortest", mixed_path,
                ])
                final_path = mixed_path
            else:
                print("no assets/music/default.mp3 found - skipping background music")

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

            sb_patch(f"episodes?id=eq.{episode_id}", {
                "status": "assembly_complete",
                "output_url": public_url,
            })
            print(f"done: {public_url}")

    except Exception as e:
        print(f"::error::assembly failed: {e}")
        mark_failed(episode_id, str(e))
        raise


if __name__ == "__main__":
    main()
