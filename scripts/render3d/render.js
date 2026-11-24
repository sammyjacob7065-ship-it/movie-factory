// Single clip : node render.js <model[|model2]> <out.mp4> [preset=spin] [seconds=5] [color[,color2]=#e8b600] [yaw[,yaw2]=0] [env=space] [w=1080] [h=1920]
// Many clips  : node render.js --batch jobs.json      (jobs.json = [{out, models:[path,...], preset, seconds, colors:[..], yaws:[..], paints:[..], env, w, h}, ...])
const fs = require('fs'), path = require('path'), http = require('http'), { spawn } = require('child_process');
const { chromium } = require('playwright');
const ROOT = path.resolve(__dirname, '../..');            // repo root, so model paths with spaces just work
const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.mjs': 'text/javascript', '.glb': 'model/gltf-binary', '.gltf': 'model/gltf+json', '.obj': 'text/plain', '.mtl': 'text/plain', '.bin': 'application/octet-stream', '.fbx': 'application/octet-stream', '.png': 'image/png', '.tga': 'application/octet-stream', '.bmp': 'image/bmp', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg' };
const server = http.createServer((req, res) => {
  let p = decodeURIComponent(req.url.split('?')[0]);
  if (p === '/favicon.ico') { res.writeHead(204); return res.end(); }
  let f = p === '/' ? path.join(__dirname, 'scene.html')
        : p.startsWith('/_three/') ? path.join(__dirname, 'node_modules/three', p.slice(8))
        : path.join(ROOT, p);
  if (!f.startsWith(ROOT) && !f.startsWith(__dirname)) { res.writeHead(403); return res.end(); }
  fs.readFile(f, (e, d) => { if (e) { if (!/\.mtl$/i.test(f)) console.log('[server 404]', p); res.writeHead(404); return res.end('nf'); } res.writeHead(200, { 'Content-Type': MIME[path.extname(f).toLowerCase()] || 'application/octet-stream' }); res.end(d); });
});
const relUrl = (m) => '/' + path.relative(ROOT, path.resolve(m)).split(path.sep).map(encodeURIComponent).join('/');

async function runJob(browser, port, job) {
  const FPS = 30, w = job.w || 1080, h = job.h || 1920, secs = +job.seconds || 5, N = Math.round(secs * FPS);
  const page = await browser.newPage({ viewport: { width: w, height: h } });
  page.on('console', m => { const t = m.text(); if (m.type() === 'error' || /mtl skipped|warn/i.test(t)) console.log('[page]', m.type(), t.slice(0, 300)); });
  page.on('pageerror', e => console.error('[pageerror]', e.message));
  try {
    await page.goto(`http://127.0.0.1:${port}/?w=${w}&h=${h}&env=${encodeURIComponent(job.env || 'space')}`);
    try { await page.waitForFunction('window.ready === true', null, { timeout: 60000 }); }
    catch (e) { console.log('[diag] page never became ready'); throw e; }
    for (let i = 0; i < job.models.length; i++) {
      await page.evaluate(o => window.loadModel(o), { url: relUrl(job.models[i]), preset: job.preset || 'spin',
        color: (job.colors || [])[i] || (job.colors || [])[0] || '#e8b600', yaw: (job.yaws || [])[i] || (job.yaws || [])[0] || '0', paint: (job.paints || [])[i] === true, axisfix: (job.axisfix || [])[i] === true });
    }
    fs.mkdirSync(path.dirname(path.resolve(job.out)), { recursive: true });
    const ff = spawn('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'image2pipe', '-framerate', String(FPS), '-i', '-', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '17', job.out], { stdio: ['pipe', 'inherit', 'inherit'] });
    const canvas = await page.$('canvas');
    for (let i = 0; i < N; i++) {
      await page.evaluate(([t, d]) => window.renderAt(t, d), [i / FPS, secs]);
      const png = await canvas.screenshot({ type: 'png' });
      if (!ff.stdin.write(png)) await new Promise(r => ff.stdin.once('drain', r));
      if (i % 60 === 0) console.log('  frame', i, '/', N);
    }
    ff.stdin.end(); await new Promise(r => ff.on('close', r));
    console.log('done', job.out);
  } finally { await page.close(); }
}

(async () => {
  const a = process.argv.slice(2);
  let jobs;
  if (a[0] === '--batch') jobs = JSON.parse(fs.readFileSync(a[1], 'utf8'));
  else {
    const [model, out, preset = 'spin', secs = '5', color = '#e8b600', yaw = '0', env = 'space', w = '1080', h = '1920'] = a;
    if (!model || !out) { console.error('usage: node render.js <model[|model2]> <out.mp4> [preset] [seconds] [color] [yaw] [env] [w] [h]  |  node render.js --batch jobs.json'); process.exit(2); }
    jobs = [{ out, models: model.split('|'), preset, seconds: secs, colors: color.split(','), yaws: yaw.split(','), paints: model.split('|').map(() => /\.obj$/i.test(model)), env, w: +w, h: +h }];
  }
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  const port = server.address().port;
  const browser = await chromium.launch({ args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist', '--enable-webgl'] });
  let failed = 0;
  for (const j of jobs) {
    try { console.log('job', j.out, j.preset, (j.models || []).join(' + ')); await runJob(browser, port, j); }
    catch (e) { failed++; console.error('JOB FAILED', j.out, String(e && e.message || e).slice(0, 300)); try { fs.unlinkSync(j.out); } catch (x) {} }
  }
  await browser.close(); server.close();
  if (failed && failed === jobs.length) process.exit(1);   // some failures are fine: those scenes keep their still picture
})().catch(e => { console.error(e); process.exit(1); });
