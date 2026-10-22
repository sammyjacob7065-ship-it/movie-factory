import os, sys, json, subprocess, time, shutil, traceback
import urllib.request

JOB = json.loads(r"""__JOB__""")
SB_URL = "__SBURL__"; SB_KEY = "__SBKEY__"; BUCKET = "__BUCKET__"
STEPS = JOB.get("steps", 20); GUIDE = JOB.get("guide", 2.0)
FOLDER = JOB.get("folder", "lipsync")
W = "/kaggle/working"; PY = "/tmp/venv/bin/python"
log_tail = []
results = []

def put(path, data, ctype):
    req = urllib.request.Request(f"{SB_URL}/storage/v1/object/{BUCKET}/{path}", data=data, method="POST",
        headers={"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY, "Content-Type": ctype, "x-upsert": "true"})
    urllib.request.urlopen(req, timeout=300).read()

def status(state, **kw):
    body = {"job_id": JOB["job_id"], "state": state, "time": time.time(), "results": results, **kw}
    put(f"{FOLDER}/{JOB['job_id']}.json", json.dumps(body).encode(), "application/json")

def sh(cmd, cwd=None, check=True):
    e = dict(os.environ); e["MPLBACKEND"] = "Agg"
    print("$", cmd[:300], flush=True)
    p = subprocess.run(cmd, shell=True, cwd=cwd, env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out = p.stdout or ""
    print(out[-2500:], flush=True)
    log_tail.append(out[-1500:])
    if check and p.returncode != 0:
        raise RuntimeError(f"command failed ({p.returncode}): {cmd[:100]}\n" + out[-600:])
    return out

def dl(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=300) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    assert os.path.getsize(dest) > 1000, "download too small: " + url

def make_scene(s, LS):
    n = s.get("scene", 0); tag = f"s{n}"
    vurl = s["video_url"]
    is_img = vurl.lower().split("?")[0].endswith((".png", ".jpg", ".jpeg", ".webp"))
    v = f"{W}/{tag}_in"; dl(vurl, v)
    auds = []
    for i, u in enumerate(s["audio_urls"]):
        a = f"{W}/{tag}_a{i}"; dl(u, a); auds.append(a)
    wav = f"{W}/{tag}_audio.wav"
    lst = f"{W}/{tag}_list.txt"
    # every line is first turned into the same clean format, then joined in order (same as the final assembly does)
    fixed = []
    for i, a in enumerate(auds):
        f = f"{W}/{tag}_f{i}.wav"
        sh(f"ffmpeg -y -i {a} -ar 44100 -ac 2 {f}"); fixed.append(f)
    open(lst, "w").write("".join(f"file '{f}'\n" for f in fixed))
    sh(f"ffmpeg -y -f concat -safe 0 -i {lst} -ar 44100 -ac 2 {wav}")
    dur = float(sh(f"ffprobe -v error -show_entries format=duration -of csv=p=0 {wav}").strip())
    if dur > 40: raise RuntimeError(f"voice is {dur:.0f}s, longer than the 40s limit for one scene")
    T = dur + 0.12
    fit = f"{W}/{tag}_fit.mp4"
    if is_img:
        sh(f"ffmpeg -y -loop 1 -i {v} -t {T:.2f} -r 25 -vf 'scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2' -c:v libx264 -pix_fmt yuv420p {fit}")
    else:
        # action plays once; if the voice is longer the last frame is held (the mouth still moves), if shorter the clip is cut
        sh(f"ffmpeg -y -i {v} -an -vf 'fps=25,tpad=stop_mode=clone:stop_duration={T:.2f}' -t {T:.2f} -c:v libx264 -pix_fmt yuv420p {fit}")
    outp = f"{W}/{tag}_out.mp4"
    sh(f"{PY} -m scripts.inference --unet_config_path configs/unet/stage2.yaml "
       f"--inference_ckpt_path checkpoints/latentsync_unet.pt --inference_steps {STEPS} --guidance_scale {GUIDE} "
       f"--enable_deepcache --video_path {fit} --audio_path {wav} --video_out_path {outp}", cwd=LS)
    assert os.path.exists(outp) and os.path.getsize(outp) > 10000, "no output video (face not found?)"
    name = s.get("out") or f"lipsync_{n}"
    put(f"{FOLDER}/{name}.mp4", open(outp, "rb").read(), "video/mp4")
    for f in os.listdir(W):
        if f.startswith(tag + "_"): 
            try: os.remove(f"{W}/{f}")
            except Exception: pass
    return f"{SB_URL}/storage/v1/object/public/{BUCKET}/{FOLDER}/{name}.mp4"

try:
    status("running", note="started")
    out = sh("nvidia-smi --query-gpu=name,memory.total --format=csv,noheader", check=False)
    if "MiB" not in out:
        raise RuntimeError("No GPU attached to this Kaggle run. GPU output: " + out[:200])
    os.chdir(W)
    if not os.path.isdir(f"{W}/LatentSync"):
        sh("git clone --depth 1 https://github.com/bytedance/LatentSync.git", cwd=W)
    LS = f"{W}/LatentSync"
    sh("pip install -q uv")
    sh("uv venv --python 3.10 --clear /tmp/venv")
    sh(f"uv pip install --python {PY} -r {LS}/requirements.txt --extra-index-url https://download.pytorch.org/whl/cu121 --index-strategy unsafe-best-match")
    sh("apt-get install -y -qq libgl1 libglib2.0-0", check=False)
    status("running", note="installed, downloading models")
    sh("pip install -q huggingface_hub")
    from huggingface_hub import hf_hub_download
    os.makedirs(f"{LS}/checkpoints/whisper", exist_ok=True)
    hf_hub_download("ByteDance/LatentSync-1.5", "latentsync_unet.pt", local_dir=f"{LS}/checkpoints")
    hf_hub_download("ByteDance/LatentSync-1.5", "whisper/tiny.pt", local_dir=f"{LS}/checkpoints")
    status("running", note="models ready")

    scenes = JOB.get("scenes") or [{"scene": 0, "video_url": JOB["video_url"], "audio_urls": [JOB["audio_url"]], "out": JOB["job_id"]}]
    for k, s in enumerate(scenes):
        try:
            url = make_scene(s, LS)
            results.append({"scene": s.get("scene", 0), "ok": True, "url": url})
        except Exception as e:
            results.append({"scene": s.get("scene", 0), "ok": False, "error": str(e)[-400:]})
            traceback.print_exc()
        status("running", note=f"scene {k+1} of {len(scenes)} finished")
    if not any(r["ok"] for r in results):
        status("failed", error="no scene could be lip-synced: " + "; ".join(f"scene {r['scene']}: {r.get('error','')[-150:]}" for r in results)[:900])
    else:
        status("done", video_url=next(r["url"] for r in results if r["ok"]))
except Exception as e:
    err = (str(e) + "\n" + "\n".join(log_tail)[-600:])[-1500:]
    try: status("failed", error=err)
    except Exception: pass
    traceback.print_exc(); sys.exit(1)
