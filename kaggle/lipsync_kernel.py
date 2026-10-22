import os, sys, json, subprocess, time, shutil, traceback
import urllib.request

JOB = json.loads(r"""__JOB__""")
SB_URL = "__SBURL__"; SB_KEY = "__SBKEY__"; BUCKET = "__BUCKET__"
STEPS = JOB.get("steps", 20); GUIDE = JOB.get("guide", 2.0)
W = "/kaggle/working"; PY = "/tmp/venv/bin/python"
log_tail = []

def put(path, data, ctype):
    req = urllib.request.Request(f"{SB_URL}/storage/v1/object/{BUCKET}/{path}", data=data, method="POST",
        headers={"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY, "Content-Type": ctype, "x-upsert": "true"})
    urllib.request.urlopen(req, timeout=300).read()

def status(state, **kw):
    body = {"job_id": JOB["job_id"], "state": state, "time": time.time(), **kw}
    put(f"lipsync/{JOB['job_id']}.json", json.dumps(body).encode(), "application/json")

def sh(cmd, env=None, cwd=None, check=True):
    e = dict(os.environ); e["MPLBACKEND"] = "Agg"
    if env: e.update(env)
    print("$", cmd, flush=True)
    p = subprocess.run(cmd, shell=True, cwd=cwd, env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out = p.stdout or ""
    print(out[-3000:], flush=True)
    log_tail.append(out[-1500:])
    if check and p.returncode != 0:
        raise RuntimeError(f"command failed ({p.returncode}): {cmd[:120]}\n" + out[-1200:])
    return out

def dl(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=300) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    assert os.path.getsize(dest) > 1000, "download too small: " + url

try:
    status("running", note="started")
    out = sh("nvidia-smi --query-gpu=name,memory.total --format=csv,noheader", check=False)
    if "T4" not in out and "P100" not in out and "L4" not in out and "A" not in out:
        raise RuntimeError("No GPU attached to this Kaggle run. GPU output: " + out)
    status("running", note="gpu ok: " + out.strip()[:60])

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

    status("running", note="models ready, making lip sync")
    dl(JOB["video_url"], f"{W}/in_video.mp4"); dl(JOB["audio_url"], f"{W}/in_audio.mp3")
    # make the video at least as long as the audio (loop it), 25 fps
    dur = float(sh(f"ffprobe -v error -show_entries format=duration -of csv=p=0 {W}/in_audio.mp3").strip())
    sh(f"ffmpeg -y -stream_loop -1 -i {W}/in_video.mp4 -t {dur+0.2:.2f} -r 25 -an -c:v libx264 -pix_fmt yuv420p {W}/in_video_fit.mp4")
    outp = f"{W}/out_{JOB['job_id']}.mp4"
    sh(f"{PY} -m scripts.inference --unet_config_path configs/unet/stage2.yaml "
       f"--inference_ckpt_path checkpoints/latentsync_unet.pt --inference_steps {STEPS} --guidance_scale {GUIDE} "
       f"--enable_deepcache --video_path {W}/in_video_fit.mp4 --audio_path {W}/in_audio.mp3 --video_out_path {outp}", cwd=LS)
    assert os.path.exists(outp) and os.path.getsize(outp) > 10000, "no output video made (face not found?)"
    put(f"lipsync/{JOB['job_id']}.mp4", open(outp, "rb").read(), "video/mp4")
    status("done", video_url=f"{SB_URL}/storage/v1/object/public/{BUCKET}/lipsync/{JOB['job_id']}.mp4")
except Exception as e:
    err = (str(e) + "\n" + "\n".join(log_tail)[-800:])[-1500:]
    try: status("failed", error=err)
    except Exception: pass
    traceback.print_exc(); sys.exit(1)
