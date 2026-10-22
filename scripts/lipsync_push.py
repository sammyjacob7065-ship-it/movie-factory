import os, sys, json, subprocess, time, pathlib
job = {"job_id": os.environ["JOB_ID"], "video_url": os.environ["VIDEO_URL"], "audio_url": os.environ["AUDIO_URL"],
       "steps": int(os.environ.get("STEPS", "20")), "guide": float(os.environ.get("GUIDE", "2.0"))}
user = os.environ["KAGGLE_USERNAME"]
tpl = pathlib.Path("kaggle/lipsync_kernel.py").read_text()
code = (tpl.replace("__JOB__", json.dumps(job))
           .replace("__SBURL__", os.environ["SB_URL"]).replace("__SBKEY__", os.environ["SB_KEY"])
           .replace("__BUCKET__", os.environ["SB_BUCKET"]))
d = pathlib.Path("kaggle_out"); d.mkdir(exist_ok=True)
(d / "script.py").write_text(code)
(d / "kernel-metadata.json").write_text(json.dumps({
    "id": f"{user}/lipsync-engine", "title": "lipsync-engine", "code_file": "script.py",
    "language": "python", "kernel_type": "script", "is_private": "true",
    "enable_gpu": "true", "enable_internet": "true",
    "dataset_sources": [], "competition_sources": [], "kernel_sources": []}, indent=1))
r = subprocess.run(["kaggle", "kernels", "push", "-p", str(d), "--accelerator", "NvidiaTeslaT4"], capture_output=True, text=True)
print(r.stdout, r.stderr)
if r.returncode != 0 and "accelerator" in (r.stderr + r.stdout).lower():
    print("retrying without --accelerator flag")
    r = subprocess.run(["kaggle", "kernels", "push", "-p", str(d)], capture_output=True, text=True)
    print(r.stdout, r.stderr)
if r.returncode != 0:
    sys.exit("Kaggle push failed: " + (r.stderr or r.stdout)[-500:])
print("pushed OK")
