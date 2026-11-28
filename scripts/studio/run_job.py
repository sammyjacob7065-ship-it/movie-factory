#!/usr/bin/env python3
"""
AD STUDIO job runner (runs inside GitHub Actions, or locally with --local DIR for testing).

    python run_job.py <storage-prefix>            e.g. ads/6430369772_1732800000
    python run_job.py --local ./jobdir            jobdir/job.json + the photos next to it; results go to jobdir/out

Reads   <prefix>/job.json  (written by n8n: the plan from the planner + where the photos are)
Makes   cut-outs -> voice (optional) -> spec -> render -> checks -> uploads
Writes  <prefix>/done.json  ALWAYS (status "done" or "error"): n8n polls this file and sends the result to Telegram.
"""
import json, os, subprocess, sys, time, traceback
import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
TTS_URL = os.environ.get('TTS_URL', 'https://openai-edge-tts-9mrp.onrender.com') + '/v1/audio/speech'
SB_URL = os.environ.get('SUPABASE_URL', '').rstrip('/')
SB_KEY = os.environ.get('SUPABASE_SERVICE_KEY', '')
BUCKET = os.environ.get('SUPABASE_BUCKET', 'betrayer-assets')
SUPPORTED = ('pouch', 'box', 'flat')


# ----------------------------------------------------------------------------- storage
def sb_get(path):
    import requests
    r = requests.get(f'{SB_URL}/storage/v1/object/{BUCKET}/{path}', headers={'Authorization': f'Bearer {SB_KEY}'}, timeout=120)
    r.raise_for_status(); return r.content


def sb_put(path, data, ctype):
    import requests
    r = requests.post(f'{SB_URL}/storage/v1/object/{BUCKET}/{path}', data=data, timeout=300,
                      headers={'Authorization': f'Bearer {SB_KEY}', 'Content-Type': ctype, 'x-upsert': 'true'})
    r.raise_for_status()
    return f'{SB_URL}/storage/v1/object/public/{BUCKET}/{path}'


# ----------------------------------------------------------------------------- cut-outs
def prepare_cutout(path, out_png, note):
    """Photo -> BGRA PNG, cropped to the object. A photo that already has a transparent background is used as it is."""
    img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    if img is None: raise RuntimeError(f'cannot read picture {os.path.basename(path)}')
    if img.ndim == 2: img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    if img.shape[2] == 4 and (img[..., 3] < 250).mean() > 0.05:
        bgr, a = img[..., :3], img[..., 3].astype(np.float32) / 255
    else:
        import cutout
        bgr = img[..., :3]
        big = max(bgr.shape[:2])
        if big > 2000: bgr = cv2.resize(bgr, None, fx=2000 / big, fy=2000 / big, interpolation=cv2.INTER_AREA)
        F, a = cutout.cutout(bgr, 'still'); bgr = F
        cov = float((a > 0.5).mean())
        if cov < 0.06 or cov > 0.985:
            note.append(f'{os.path.basename(path)}: the background could not be separated cleanly, the whole picture is used')
            a = np.ones(bgr.shape[:2], np.float32)
    ys, xs = np.where(a > 0.12)
    if len(xs) == 0: raise RuntimeError(f'nothing found in {os.path.basename(path)}')
    pad = int(0.015 * max(bgr.shape[:2]))
    x0, x1 = max(0, xs.min() - pad), min(bgr.shape[1], xs.max() + pad + 1); y0, y1 = max(0, ys.min() - pad), min(bgr.shape[0], ys.max() + pad + 1)
    out = np.dstack([bgr[y0:y1, x0:x1], (a[y0:y1, x0:x1] * 255).astype(np.uint8)])
    cv2.imwrite(out_png, out); return out


def auto_palette(front, style, given):
    bgr = front[..., :3]; a = front[..., 3] > 230
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    m = a & (hsv[..., 1] > 110) & (hsv[..., 2] > 90)
    if m.sum() > 200:
        c = np.median(bgr[m], axis=0)
    else:
        c = np.array([90, 140, 200], np.float32)
    acc = '#%02x%02x%02x' % (int(c[2]), int(c[1]), int(c[0]))
    dark = style != 'light_studio'
    bg = '#%02x%02x%02x' % tuple(int(8 + c[i] * 0.06) for i in (2, 1, 0)) if dark else '#ecebe6'
    pal = {'bg': bg, 'accent': acc, 'text': '#f2efe9' if dark else '#16161a'}
    pal.update({k: v for k, v in (given or {}).items() if isinstance(v, str) and v.startswith('#') and len(v) == 7})
    return pal


# ----------------------------------------------------------------------------- voice
def make_voice(lines, voice_id, work, note):
    import requests
    out = []
    for i, ln in enumerate(lines):
        text = str(ln.get('text', '')).strip()
        if not text: continue
        f = os.path.join(work, f'vo_{i + 1}.mp3'); ok = False
        for attempt in range(4):       # the voice service sleeps on a free host: the first call can take a minute
            try:
                r = requests.post(TTS_URL, json={'input': text, 'voice': voice_id, 'response_format': 'mp3', 'speed': 1.0}, timeout=150)
                if r.status_code == 200 and len(r.content) > 1500:
                    open(f, 'wb').write(r.content); ok = True; break
            except Exception as e:
                last = str(e)
            time.sleep(8)
        if not ok: note.append(f'voice line {i + 1} could not be made (voice service did not answer)'); continue
        d = float(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=nw=1:nk=1', f]).decode().strip())
        out.append({'shot': int(ln['shot']), 'file': f, 'dur': d, 'start': float(ln.get('start', 0.7))})
    return out


# ----------------------------------------------------------------------------- final checks (pass 3: look at the finished file)
def probe_checks(path, spec, voice_on, qa):
    info = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'format=duration,size:stream=codec_type,width,height', '-of', 'json', path]).decode())
    st = info.get('streams', []); has_v = any(s['codec_type'] == 'video' for s in st); has_a = any(s['codec_type'] == 'audio' for s in st)
    dur = float(info['format']['duration']); size = int(info['format']['size']) / 1e6
    want = float(spec.get('duration', 30))
    qa.append({'check': 'file_video_stream', 'status': 'PASS' if has_v else 'FAIL', 'detail': 'video stream present' if has_v else 'no video stream'})
    qa.append({'check': 'file_audio', 'status': 'PASS' if (has_a == voice_on) else 'WARN', 'detail': 'voice-over present' if has_a else 'silent, as asked'})
    qa.append({'check': 'file_length', 'status': 'PASS' if abs(dur - want) <= 1.6 else 'WARN', 'detail': f'{dur:.1f} s (asked {want:.0f} s)'})
    qa.append({'check': 'file_size', 'status': 'PASS' if size < 400 else 'WARN', 'detail': f'{size:.1f} MB'})
    return has_v


def small_copy(src, dst, portrait):
    w = 720 if portrait else 1280
    subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', src, '-vf', f'scale={w}:-2', '-c:v', 'libx264', '-crf', '27', '-preset', 'medium', '-maxrate', '2500k', '-bufsize', '5000k',
                    '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart', dst], check=True)


# ----------------------------------------------------------------------------- main
def run(job, get, put, work):
    os.makedirs(work, exist_ok=True); note = []; qa = []
    plan = job['plan']; photos = job['photos']
    # 1) pictures -> cut-outs
    pics = {}
    for role in ('front', 'back', 'side'):
        if photos.get(role):
            raw = os.path.join(work, f'raw_{role}.img'); open(raw, 'wb').write(get(photos[role]))
            pics[role] = prepare_cutout(raw, os.path.join(work, f'{role}.png'), note)
    if 'front' not in pics: raise RuntimeError('no front picture was sent (send it with the caption "ad front")')
    # 2) product + palette
    prod = dict(plan.get('product') or {})
    kind = str(prod.get('kind', 'pouch')).lower()
    if kind not in SUPPORTED:
        note.append(f'a "{kind}" cannot be turned 360 from photos: it is shown flat with a gentle tilt (give me a 3D model or a pouch / box product for a full turn)'); kind = 'flat'
    prod.update({'kind': kind, 'front': os.path.join(work, 'front.png'), 'back': os.path.join(work, 'back.png') if 'back' in pics else None, 'side': os.path.join(work, 'side.png') if 'side' in pics else None})
    style = plan.get('style', 'dark_studio')
    pal = auto_palette(pics['front'], style, plan.get('palette'))
    # 3) voice
    voice_cfg = plan.get('voice') or {}; voice = []
    if voice_cfg.get('on') and voice_cfg.get('lines'):
        voice = make_voice(voice_cfg['lines'], voice_cfg.get('voice_id', 'en-NG-AbeoNeural'), work, note)
    bg = dict(plan.get('background') or {})
    if photos.get('bg'):
        rawb = os.path.join(work, 'bg_raw.img'); open(rawb, 'wb').write(get(photos['bg'])); bg = {'type': 'image', 'image': rawb, 'blur': 1, 'dim': 0.8}
    if bg.get('type') == 'image' and not (bg.get('image') and os.path.exists(bg['image'])): bg = {}
    spec = {'background': bg or None, 'aspect': plan.get('aspect', '9:16'), 'duration': float(plan.get('duration', 30)), 'fps': 30, 'style': style, 'palette': pal, 'product': prod, 'shots': plan['shots'], 'voice': voice}
    json.dump(spec, open(os.path.join(work, 'spec.json'), 'w'), indent=1)
    # 4) render
    import ad_render
    out = os.path.join(work, 'ad_master.mp4'); sheet = os.path.join(work, 'sheet.jpg')
    r = ad_render.Renderer(spec, float(os.environ.get('AD_RES', '1.0'))); qa += r.render(out, None, None, sheet)
    ok = probe_checks(out, spec, bool(voice), qa)
    if not ok: raise RuntimeError('the render finished without a video stream')
    tg = os.path.join(work, 'ad_telegram.mp4'); small_copy(out, tg, spec['aspect'] == '9:16')
    return {'master': out, 'telegram': tg, 'sheet': sheet, 'qa': qa, 'notes': note, 'seconds': r.total}


def main():
    a = sys.argv[1:]
    if not a: sys.exit(__doc__)
    if a[0] == '--local':
        d = os.path.abspath(a[1]); job = json.load(open(os.path.join(d, 'job.json')))
        get = lambda p: open(os.path.join(d, p), 'rb').read()
        res = run(job, get, None, os.path.join(d, 'out')); print(json.dumps({k: v for k, v in res.items() if k != 'qa'}, indent=1))
        for q in res['qa']: print(q['status'], q['check'], '-', q['detail'])
        return
    prefix = a[0]; done = {'status': 'error', 'error': 'unknown', 'finished': time.time()}
    try:
        job = json.loads(sb_get(f'{prefix}/job.json').decode())
        res = run(job, sb_get, sb_put, os.path.join('/tmp', 'adjob'))
        base = f'{prefix}/out'
        urls = {'telegram': sb_put(f'{base}/ad_telegram.mp4', open(res['telegram'], 'rb').read(), 'video/mp4'),
                'sheet': sb_put(f'{base}/sheet.jpg', open(res['sheet'], 'rb').read(), 'image/jpeg')}
        if os.path.getsize(res['master']) < 45e6:      # the storage has a file-size limit, a bigger master stays out
            urls['master'] = sb_put(f'{base}/ad_master.mp4', open(res['master'], 'rb').read(), 'video/mp4')
        else:
            res['notes'].append('the full-quality master is too big for storage; the Telegram copy is the one delivered')
        done = {'status': 'done', 'urls': urls, 'qa': res['qa'], 'notes': res['notes'], 'seconds': res['seconds'], 'finished': time.time()}
    except Exception as e:
        traceback.print_exc(); done = {'status': 'error', 'error': f'{type(e).__name__}: {str(e)[:400]}', 'finished': time.time()}
    finally:
        try: sb_put(f'{prefix}/done.json', json.dumps(done).encode(), 'application/json')
        except Exception: traceback.print_exc()
    sys.exit(0 if done['status'] == 'done' else 1)


if __name__ == '__main__':
    main()
