// node render_games.js jobs.json
// jobs = [{out, scenes:[{sp, v}], fps}]   one job = ONE whole game video (one continuous screen) with its own game sounds.
// It writes <out> (video + sound) and <out>.plan.json ({lens, starts, D}) so the planner can cut it into the scenes.
const { chromium } = require('playwright'), fs = require('fs'), path = require('path'), { spawn } = require('child_process');
const SR = 44100;
function synth(cues, D) {   // the game sounds, made from simple tones and noise (no audio files needed)
  const n = Math.ceil((D + 1) * SR), L = new Float32Array(n);
  const add = (t0, dur, fn, vol) => { const i0 = Math.floor(t0 * SR), m = Math.floor(dur * SR); for (let i = 0; i < m && i0 + i < n; i++) { const x = i / SR; L[i0 + i] += fn(x, i / m) * vol; } };
  const env = (u, a = 0.02) => Math.min(1, u / a) * Math.pow(1 - u, 2);
  let seed = 12345; const rnd = () => { seed = (seed * 1664525 + 1013904223) >>> 0; return seed / 4294967296 * 2 - 1; };
  const tone = (t0, f, dur, vol, type) => add(t0, dur, (x, u) => { const p = 2 * Math.PI * f * x; const w = type === 'tri' ? (2 / Math.PI) * Math.asin(Math.sin(p)) : Math.sin(p); return w * env(u, 0.01); }, vol);
  cues.forEach(c => {
    const t = c.t, v = c.v || 1;
    switch (c.k) {
      case 'tick': tone(t, 1000, 0.06, 0.32 * v, 'sin'); break;
      case 'pop': add(t, 0.09, (x, u) => Math.sin(2 * Math.PI * (500 + 700 * (1 - u)) * x) * Math.pow(1 - u, 2), 0.3 * v); break;
      case 'ding': tone(t, 1318, 0.9, 0.3 * v, 'sin'); tone(t, 2637, 0.6, 0.12 * v, 'sin'); break;
      case 'chime': [784, 988, 1175, 1568].forEach((f, i) => { tone(t + i * 0.09, f, 0.7, 0.24 * v, 'sin'); }); break;
      case 'go': add(t, 0.45, (x, u) => Math.sin(2 * Math.PI * (440 + 880 * u) * x) * Math.pow(1 - u, 1.2), 0.34 * v); break;
      case 'whoosh': { let lp = 0; add(t - 0.1, 0.5, (x, u) => { const a = 0.05 + 0.5 * u; lp += (rnd() - lp) * a; return lp * Math.sin(Math.PI * u) * 3; }, 0.16 * v); break; }
      case 'boost': add(t, 0.35, (x, u) => Math.sin(2 * Math.PI * (300 + 900 * u) * x) * Math.sin(Math.PI * u), 0.16 * v); break;
      case 'clunk': add(t, 0.22, (x, u) => (Math.sin(2 * Math.PI * 110 * x) * 0.9 + rnd() * 0.25 * (1 - u)) * Math.pow(1 - u, 2.5), 0.5 * v); break;
      case 'fanfare': [523, 659, 784, 1047, 1319].forEach((f, i) => { tone(t + i * 0.11, f, 0.55, 0.24 * v, 'tri'); }); tone(t + 0.55, 1047, 1.2, 0.2 * v, 'tri'); break;
      case 'rumble': { let lp = 0, lp2 = 0; const dur = c.v; add(t, dur, (x, u) => { lp += (rnd() - lp) * 0.025; lp2 += (lp - lp2) * 0.2; return lp2 * (0.7 + 0.3 * Math.sin(x * 9)) * Math.min(1, x / 0.3) * Math.min(1, (dur - x) / 0.4); }, 1.1); break; }
    }
  });
  let mx = 0; for (let i = 0; i < n; i++) mx = Math.max(mx, Math.abs(L[i])); const g = mx > 0.9 ? 0.9 / mx : 1;
  const buf = Buffer.alloc(44 + n * 2); buf.write('RIFF', 0); buf.writeUInt32LE(36 + n * 2, 4); buf.write('WAVEfmt ', 8); buf.writeUInt32LE(16, 16); buf.writeUInt16LE(1, 20); buf.writeUInt16LE(1, 22); buf.writeUInt32LE(SR, 24); buf.writeUInt32LE(SR * 2, 28); buf.writeUInt16LE(2, 32); buf.writeUInt16LE(16, 34); buf.write('data', 36); buf.writeUInt32LE(n * 2, 40);
  for (let i = 0; i < n; i++) buf.writeInt16LE(Math.round(Math.max(-1, Math.min(1, L[i] * g)) * 32000), 44 + i * 2);
  return buf;
}
(async () => {
  const jobs = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  const browser = await chromium.launch({ args: ['--use-gl=swiftshader', '--no-sandbox'] });
  const page = await browser.newPage({ viewport: { width: 1080, height: 1920 }, deviceScaleFactor: 1 });
  page.on('pageerror', e => console.error('PAGE ERROR', String(e).slice(0, 300)));
  await page.goto('file://' + path.join(__dirname, 'game.html'));
  let failed = 0;
  for (const j of jobs) {
    try {
      const fps = j.fps || 30;
      const plan = await page.evaluate((sc) => window.setup(sc), j.scenes);
      const n = Math.round(plan.D * fps);
      fs.mkdirSync(path.dirname(path.resolve(j.out)), { recursive: true });
      fs.writeFileSync(j.out + '.plan.json', JSON.stringify({ lens: plan.lens, starts: plan.starts, D: plan.D }));
      const wav = j.out + '.sfx.wav'; fs.writeFileSync(wav, synth(plan.cues, plan.D));
      const ff = spawn('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'image2pipe', '-framerate', String(fps), '-i', '-', '-i', wav, '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '18', '-preset', 'veryfast', '-c:a', 'aac', '-b:a', '160k', '-t', String(plan.D), j.out], { stdio: ['pipe', 'inherit', 'inherit'] });
      const done = new Promise((res) => ff.on('close', res));
      console.log('game', j.out, plan.D.toFixed(1) + ' s', n + ' frames', plan.cues.length + ' sounds');
      for (let i = 0; i < n; i++) {
        await page.evaluate((t) => window.frame(t), i / fps);
        const buf = await page.screenshot({ type: 'jpeg', quality: 90 });
        if (!ff.stdin.write(buf)) await new Promise(r => ff.stdin.once('drain', r));
      }
      ff.stdin.end(); await done; console.log('done', j.out);
    } catch (e) { failed++; console.error('JOB FAILED', j.out, String(e && e.message || e).slice(0, 300)); try { fs.unlinkSync(j.out); } catch (x) {} }
  }
  await browser.close(); process.exit(failed && failed === jobs.length ? 1 : 0);
})();
