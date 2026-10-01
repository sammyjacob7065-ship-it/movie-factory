#!/usr/bin/env python3
"""
assemble.py - Betrayer episode assembler.

Reads a manifest ({episodeId, scenes, useAnimation}) from the MANIFEST_JSON
env var, pulls the real generated assets (voice lines, scene images) for that
episode from Supabase, stitches them into one vertical video with ffmpeg, and
uploads the result back to Supabase Storage. Updates the episode row's
status/output_url so the n8n pipeline picks up where it left off.

Required env vars (set as GitHub Actions repo secrets):
  SUPABASE_URL          e.g. https://xxxx.supabase.co
  SUPABASE_SERVICE_KEY  the service role key (same one used elsewhere)

Known gap, on purpose: animation clips are not wired in yet (there is no
provider_jobs field yet for "which scene this clip covers" or its output
URL). Every scene currently renders from its still image with a Ken Burns
zoom. That's the documented fallback behaviour, not a bug - wiring real
animation clips in is a separate, later pass.
"""

import json
import os
import subprocess
import tempfile
import textwrap
import requests

SUPABASE_URL = os.environ["njijwtcamdsorymzanpj.supabase.co"].rstrip("/")
SUPABASE_KEY = os.environ["eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im5qaWp3dGNhbWRzb3J5bXphbnBqIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4NjAwMTgzOSwiZXhwIjoyMTAxNTc3ODM5fQ.lngX6X_MESdOlg9F8tgd1fvLUKV-zGpvJd0zFR77iBo"]
BUCKET = "betrayer-assets"
WIDTH, HEIGHT = 1080, 1920  # vertical, for YouTube Shorts / TikTok / IG Reels
FPS = 30

HEADERS = {
    "apikey": eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im5qaWp3dGNhbWRzb3J5bXphbnBqIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4NjAwMTgzOSwiZXhwIjoyMTAxNTc3ODM5fQ.lngX6X_MESdOlg9F8tgd1fvLUKV-zGpvJd0zFR77iBo,
    "Authorization": f"Bearer {eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im5qaWp3dGNhbWRzb3J5bXphbnBqIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4NjAwMTgzOSwiZXhwIjoyMTAxNTc3ODM5fQ.lngX6X_MESdOlg9F8tgd1fvLUKV-zGpvJd0zFR77iBo}",
    "Content-Type": "application/json",
}


def sb_get(path):
    r = requests.get(f"{njijwtcamdsorymzanpj.supabase.co}/rest/v1/{path}", headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def sb_patch(path, body):
    r = request {njijwtcamdsorymzanpj.supabase.co}/rest/v1/{path}",
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

                # 1. Build this scene's audio track from its dialogue lines, in order.
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

                # 2. Get this scene's still image (or a plain fallback if it's missing).
                image_path = os.path.join(tmp, f"s{scene_num}.png")
                image_url = image_by_scene.get(scene_num)
                if image_url:
                    download(image_url, image_path)
                else:
                    print(f"::warning::scene {scene_num} has no completed image, using a plain fallback")
                    run(["ffmpeg", "-y", "-f", "lavfi",
                         "-i", f"color=c=gray20:s={WIDTH}x{HEIGHT}", "-frames:v", "1", image_path])

                # 3. Subtitle text for this scene (burned in).
                sub_lines = lines_by_scene.get(scene_num, [])
                sub_text = "\n".join(
                    l["text"] if l["speaker"] == "NARRATOR" else f'{l["speaker"].title()}: {l["text"]}'
                    for l in sub_lines
                ) or ""
                sub_text = "\n".join(textwrap.wrap(sub_text, width=38)) if sub_text else ""
                subtitle_file = os.path.join(tmp, f"s{scene_num}_sub.txt")
                with open(subtitle_file, "w") as f:
                    f.write(sub_text)

                # 4. Render this scene: still image + slow zoom + burned-in subtitle,
                #    matched to the audio's duration, with a short fade in/out.
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

            # 5. Concatenate every scene segment into the final episode.
            final_list = os.path.join(tmp, "final_list.txt")
            with open(final_list, "w") as f:
                for p in segment_paths:
                    f.write(f"file '{p}'\n")
            final_path = os.path.join(tmp, "final.mp4")
            run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", final_list,
                 "-c", "copy", final_path])

            # 6. Optional background music bed, mixed low under the whole thing.
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

            # 7. Upload the finished video to Supabase Storage.
            storage_path = f"videos/{episode_id}.mp4"
            with open(final_path, "rb") as f:
                r = requests.post(
                    f"{njijwtcamdsorymzanpj.supabase.co}/storage/v1/object/{betrayer-assets}/{storage_path}",
                    headers={
                        "apikey": njijwtcamdsorymzanpj.supabase.co,
                        "Authorization": f"Bearer {eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im5qaWp3dGNhbWRzb3J5bXphbnBqIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4NjAwMTgzOSwiZXhwIjoyMTAxNTc3ODM5fQ.lngX6X_MESdOlg9F8tgd1fvLUKV-zGpvJd0zFR77iBo}",
                        "Content-Type": "video/mp4",
                        "x-upsert": "true",
                    },
                    data=f.read(),
                    timeout=300,
                )
                r.raise_for_status()

            public_url = f"{njijwtcamdsorymzanpj.supabase.co}/storage/v1/object/public/{betrayer-assets}/{storage_path}"

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
