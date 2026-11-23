// usage: node render.js <model file> <out.mp4> [preset=spin] [seconds=5] [color=#e8b600] [yaw=0] [env=space] [w=1080] [h=1920]
const fs = require('fs'), path = require('path'), http = require('http'), { spawn } = require('child_process');
const { chromium } = require('playwright');
const [model, out, preset = 'spin', secs = '5', color = '#e8b600', yaw = '0', env = 'space', w = '1080', h = '1920'] = process.argv.slice(2);
if (!model || !out) { console.error('usage: node render.js <model> <out.mp4> [preset] [seconds] [color] [yaw] [w] [h]'); process.exit(2); }
const ROOT = path.resolve(__dirname, '../..');            // repo root, so model paths with spaces just work
const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.mjs': 'text/javascript', '.glb': 'model/gltf-binary', '.gltf': 'model/gltf+json', '.obj': 'text/plain', '.bin': 'application/octet-stream' };
const server = http.createServer((req, res) => {
  let p = decodeURIComponent(req.url.split('?')[0]);
  let f = p === '/' ? path.join(__dirname, 'scene.html')
        : p.startsWith('/_three/') ? path.join(__dirname, 'node_modules/three', p.slice(8))
        : path.join(ROOT, p);
  if (!f.startsWith(ROOT) && !f.startsWith(__dirname)) { res.writeHead(403); return res.end(); }
  if (p === '/favicon.ico') { res.writeHead(204); return res.end(); }
  fs.readFile(f, (e, d) => { if (e) { console.log('[server 404]', p); res.writeHead(404); return res.end('nf'); } res.writeHead(200, { 'Content-Type': MIME[path.extname(f).toLowerCase()] || 'application/octet-stream' }); res.end(d); });
});
(async () => {
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  const port = server.address().port, FPS = 30, N = Math.round(+secs * FPS);
  const browser = await chromium.launch({ args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist', '--enable-webgl'] });
  const page = await browser.newPage({ viewport: { width: +w, height: +h } });
  page.on('console', m => console.log('[page]', m.type(), m.text())); page.on('pageerror', e => console.error('[pageerror]', e.message, e.stack));
  page.on('requestfailed', r => console.log('[requestfailed]', r.url(), r.failure() && r.failure().errorText));
  page.on('response', r => { if (r.status() >= 400) console.log('[http ' + r.status() + ']', r.url()); });
  page.on('request', r => console.log('[request]', r.url()));
  await page.goto(`http://127.0.0.1:${port}/?w=${w}&h=${h}&env=${env}`);
  try { await page.waitForFunction('window.ready === true', null, { timeout: 60000 }); }
  catch (e) { console.log('[diag] page never became ready. readyState =', await page.evaluate(() => document.readyState)); throw e; }
  const models = model.split('|'), colors = color.split(','), yaws = yaw.split(',');
  for (let i = 0; i < models.length; i++) {
    const rel = '/' + path.relative(ROOT, path.resolve(models[i])).split(path.sep).map(encodeURIComponent).join('/');
    await page.evaluate(o => window.loadModel(o), { url: rel, preset, color: colors[i] || colors[0], yaw: yaws[i] || yaws[0] });
  }
  const ff = spawn('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'image2pipe', '-framerate', String(FPS), '-i', '-', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '16', out], { stdio: ['pipe', 'inherit', 'inherit'] });
  const canvas = await page.$('canvas');
  for (let i = 0; i < N; i++) {
    await page.evaluate(([t, d]) => window.renderAt(t, d), [i / FPS, +secs]);
    const png = await canvas.screenshot({ type: 'png' });
    if (!ff.stdin.write(png)) await new Promise(r => ff.stdin.once('drain', r));
    if (i % 30 === 0) console.log('frame', i, '/', N);
  }
  ff.stdin.end(); await new Promise(r => ff.on('close', r));
  await browser.close(); server.close(); console.log('done', out);
})().catch(e => { console.error(e); process.exit(1); });
