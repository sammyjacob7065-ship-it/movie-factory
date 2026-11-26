#!/usr/bin/env python3
"""v112: renders the 3D scenes of an episode.
Reads the manifest (env MANIFEST_JSON), finds scenes whose ANIMATION text has  scale-3D model=<key> preset=<p> env=<e>,
finds the model in this repo, renders each scene as a vertical clip with scripts/render3d (three.js), and writes clips3d.json
({"scene number": "clips3d/s<N>.mp4"}) which assemble.py picks up. A scene that cannot be rendered simply keeps its normal picture.
Run from the repo root:   python scripts/plan3d.py            (render)
                          python scripts/plan3d.py --check    (one short clip per model, to look at them)"""
import base64, json, os, re, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
try:
    CAT = json.load(open(os.path.join(ROOT, "scripts", "catalog.json"), encoding="utf-8"))
except Exception as _e:                      # the game videos do not need the 3D catalog
    print(f"3D catalog not found ({_e}) - 3D models are skipped, games still work")
    CAT = {"models": {}, "presets": [], "envs": [], "directional": []}
MODELS = CAT.get("models", {})
EXT = (".glb", ".gltf", ".obj", ".fbx")
CLIP_SECONDS = 6.5
W, H = 720, 1280


def norm(s):
    return re.sub(r"[^a-z0-9]+", "_", str(s or "").lower()).strip("_")


def resolve_key(raw):
    k = norm(raw)
    if k in MODELS:
        return k
    for name, m in MODELS.items():
        if k in [norm(a) for a in m.get("alias", [])]:
            return name
    for name, m in MODELS.items():                      # last chance: the key is part of a name or the other way round
        if k and (k in name or name in k):
            return name
    return None


def find_file(folder_part):
    part = folder_part.lower()
    best = None
    for d in sorted(os.listdir(ROOT)):
        full = os.path.join(ROOT, d)
        if not os.path.isdir(full) or d.startswith(".") or d in ("scripts", "node_modules", "textures", "assets", "music"):
            continue
        if part not in d.lower():
            continue
        files = []
        for dp, _, fs in os.walk(full):
            for f in fs:
                if f.lower().endswith(EXT):
                    files.append(os.path.join(dp, f))
        if not files:
            continue
        files.sort(key=lambda p: ({".glb": 0, ".gltf": 1, ".obj": 2, ".fbx": 3}.get(os.path.splitext(p)[1].lower(), 9), len(p)))
        best = files[0]
        break
    return best


def parse_directives(manifest):
    out = {}
    for sc in manifest.get("scenes") or []:
        txt = str(sc.get("overlay_anims") or "")
        m = re.search(r"(?i)scale-3d\s+([^;]*)", txt)
        if not m:
            continue
        kv = dict((a.lower(), b) for a, b in re.findall(r"(\w+)=([^\s;]+)", m.group(1)))
        try:
            out[int(sc["scene_number"])] = kv
        except (KeyError, ValueError, TypeError):
            pass
    return out


def parse_games(manifest):
    """Brain-game scenes: 'scale-GAME d=<base64 json>' -> {scene number: spec}. scripts/render3d/game.html draws them."""
    out = {}
    for sc in manifest.get("scenes") or []:
        txt = str(sc.get("overlay_anims") or "")
        m = re.search(r"(?i)scale-game\s+([^;]*)", txt)
        if not m:
            continue
        kv = dict((a.lower(), b) for a, b in re.findall(r"(\w+)=([^\s;]+)", m.group(1)))
        try:
            raw = kv["d"].replace("-", "+").replace("_", "/")
            raw += "=" * (-len(raw) % 4)
            out[int(sc["scene_number"])] = json.loads(base64.b64decode(raw).decode("utf-8"))
        except Exception as e:
            print(f"scene {sc.get('scene_number')}: game data could not be read ({e}) - keeps its picture")
    return out


def run_game_jobs(jobs):
    for j in jobs:
        os.makedirs(os.path.dirname(os.path.join(ROOT, j["out"])), exist_ok=True)
    jf = os.path.join(ROOT, "jobsgame.json")
    json.dump([{**j, "out": os.path.join(ROOT, j["out"])} for j in jobs], open(jf, "w"))
    r = subprocess.run(["node", "render_games.js", jf], cwd=os.path.join(ROOT, "scripts", "render3d"))
    return r.returncode


def build_job(num, kv, out_path, seconds=CLIP_SECONDS):
    key = resolve_key(kv.get("model"))
    if not key:
        print(f"scene {num}: no model called '{kv.get('model')}' - keeps its picture")
        return None
    m = MODELS[key]
    f = find_file(m["folder"])
    if not f:
        print(f"scene {num}: model folder '{m['folder']}' not found in the repo - keeps its picture")
        return None
    preset = kv.get("preset") or m["preset"]
    if preset not in CAT["presets"]:
        preset = m["preset"]
    env = kv.get("env") or m["env"]
    if env not in CAT["envs"]:
        env = m["env"]
    if preset in CAT["directional"] and not m.get("verified"):
        print(f"scene {num}: {key} is not direction-checked yet, so '{preset}' becomes 'orbit'")
        preset = "orbit"
    if preset == "fly" and env != "sky":
        env = "sky"
    if preset == "drive" and env not in ("road", "runway"):
        env = "road"
    return {"out": out_path, "models": [f], "preset": preset, "seconds": seconds, "colors": [m.get("color", "#e8b600")],
            "yaws": [str(m.get("flip", 0))], "paints": [bool(m.get("paint"))], "axisfix": [bool(m.get("vehicle"))],
            "env": env, "w": W, "h": H, "_key": key, "_scene": num}


def run_jobs(jobs):
    for j in jobs:
        os.makedirs(os.path.dirname(os.path.join(ROOT, j["out"])), exist_ok=True)
    jf = os.path.join(ROOT, "jobs3d.json")
    json.dump([{**{k: v for k, v in j.items() if not k.startswith("_")}, "out": os.path.join(ROOT, j["out"])} for j in jobs], open(jf, "w"))   # absolute path: render.js runs inside scripts/render3d, so a relative path landed in the wrong folder
    r = subprocess.run(["node", "render.js", "--batch", jf], cwd=os.path.join(ROOT, "scripts", "render3d"))
    return r.returncode


def probe_seconds(url):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(url)], capture_output=True, text=True, timeout=90)
        return float((r.stdout or "0").strip().split()[0])
    except Exception:
        return 0.0


def get_voice_lengths(manifest):
    """{scene number: seconds of voice}, measured from the real voice files, so the game is timed to the real narration."""
    url, key, ep = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_KEY"), manifest.get("episodeId")
    if not (url and key and ep):
        print("voice lengths: no Supabase access here - using estimates")
        return {}
    try:
        import requests
        rows = requests.get(f"{url}/rest/v1/assets?episode_id=eq.{ep}&type=eq.voice_line&status=eq.completed&select=line_index,scene_number,file_url",
                            headers={"apikey": key, "Authorization": f"Bearer {key}"}, timeout=60).json()
        out = {}
        for a in rows:
            d = probe_seconds(a.get("file_url"))
            if d > 0:
                out[int(a["scene_number"])] = out.get(int(a["scene_number"]), 0.0) + d
        print("real voice lengths:", {k: round(v, 1) for k, v in sorted(out.items())})
        return out
    except Exception as e:
        print(f"voice lengths could not be read ({e}) - using estimates")
        return {}


def cut_game(job, done):
    """The whole game video was drawn as ONE film. Cut it into one clip per scene (the clips join up seamlessly) and tell assemble.py how long each must last."""
    full = os.path.join(ROOT, job["out"])
    try:
        plan = json.load(open(full + ".plan.json"))
    except Exception:
        return None
    if not (os.path.exists(full) and os.path.getsize(full) > 20000):
        return None
    lens = {}
    for idx, n in enumerate(job["_nums"]):
        outp = f"clips3d/s{n}.mp4"
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{plan['starts'][idx]:.3f}", "-i", full, "-t", f"{plan['lens'][idx]:.3f}",
                            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
                            os.path.join(ROOT, outp)])
        if r.returncode == 0 and os.path.exists(os.path.join(ROOT, outp)):
            done[str(n)] = outp
            lens[str(n)] = round(plan["lens"][idx], 3)
    json.dump({"len": lens}, open(os.path.join(ROOT, "games.json"), "w"))
    print(f"game film {plan['D']:.1f}s cut into scenes: {lens}")
    return lens


def main():
    os.chdir(ROOT)
    if "--check" in sys.argv:
        jobs = []
        for key, m in MODELS.items():
            kv = {"model": key, "preset": m["preset"] if m["preset"] in CAT["directional"] else "spin", "env": m["env"]}
            kv["preset"] = m["preset"]
            j = build_job(0, kv, f"check3d/{key}.mp4", seconds=3.0)
            if j:
                # for the check we WANT to see the direction, so use the real preset even if it is not verified yet
                p = m["preset"]
                j["preset"] = p
                jobs.append(j)
        run_jobs(jobs)
        return
    manifest = json.loads(os.environ.get("MANIFEST_JSON") or "{}")
    dirs = parse_directives(manifest)
    print(f"3D scenes asked for: {sorted(dirs)}")
    jobs = []
    for num, kv in sorted(dirs.items())[:5]:             # never more than 5 per film (render time)
        j = build_job(num, kv, f"clips3d/s{num}.mp4")
        if j:
            jobs.append(j)
    games = parse_games(manifest)
    print(f"game scenes asked for: {sorted(games)}")
    gjobs, game_plan = [], None
    all_nums = sorted(int(sc["scene_number"]) for sc in (manifest.get("scenes") or []) if str(sc.get("scene_number", "")).strip() != "")
    if games and sorted(games) == all_nums:          # a game video: EVERY scene belongs to ONE continuous screen
        nums = sorted(games)
        voice = get_voice_lengths(manifest)
        scenes = []
        for n in nums:
            v = voice.get(n)
            if not v:
                v = float(games[n].get("ed") or 3.5)
                print(f"scene {n}: voice length not found, using the estimate {v:.1f}s")
            scenes.append({"sp": games[n], "v": v})
        gjobs.append({"out": "clips3d/game_full.mp4", "scenes": scenes, "fps": 30, "_nums": nums})
    elif games:
        print("::warning::some scenes have no game data - the game screen is not drawn")
    if not jobs and not gjobs:
        return
    rc = run_jobs(jobs) if jobs else 0
    done = {}
    used = []
    if gjobs:
        rc = run_game_jobs(gjobs) or rc
        game_plan = cut_game(gjobs[0], done)
    for j in jobs:
        p = os.path.join(ROOT, j["out"])
        if os.path.exists(p) and os.path.getsize(p) > 20000:
            done[str(j["_scene"])] = j["out"]
            if "_key" in j:
                used.append(j["_key"])
    json.dump(done, open(os.path.join(ROOT, "clips3d.json"), "w"))
    credits = [CAT.get("credits", {}).get(k) for k in dict.fromkeys(used)]
    credits = [c for c in credits if c]
    if credits:
        open(os.path.join(ROOT, "credits3d.txt"), "w", encoding="utf-8").write("\n".join(credits))
    print(f"3D clips ready: {sorted(done)} (render code {rc})")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"::warning::3D render step failed ({e}) - every scene keeps its normal picture")
