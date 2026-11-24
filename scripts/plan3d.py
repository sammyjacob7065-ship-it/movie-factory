#!/usr/bin/env python3
"""v112: renders the 3D scenes of an episode.
Reads the manifest (env MANIFEST_JSON), finds scenes whose ANIMATION text has  scale-3D model=<key> preset=<p> env=<e>,
finds the model in this repo, renders each scene as a vertical clip with scripts/render3d (three.js), and writes clips3d.json
({"scene number": "clips3d/s<N>.mp4"}) which assemble.py picks up. A scene that cannot be rendered simply keeps its normal picture.
Run from the repo root:   python scripts/plan3d.py            (render)
                          python scripts/plan3d.py --check    (one short clip per model, to look at them)"""
import json, os, re, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAT = json.load(open(os.path.join(ROOT, "scripts", "catalog.json"), encoding="utf-8"))
MODELS = CAT["models"]
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
    json.dump([{k: v for k, v in j.items() if not k.startswith("_")} for j in jobs], open(jf, "w"))
    r = subprocess.run(["node", "render.js", "--batch", jf], cwd=os.path.join(ROOT, "scripts", "render3d"))
    return r.returncode


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
    if not jobs:
        return
    rc = run_jobs(jobs)
    done = {}
    used = []
    for j in jobs:
        p = os.path.join(ROOT, j["out"])
        if os.path.exists(p) and os.path.getsize(p) > 20000:
            done[str(j["_scene"])] = j["out"]
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
