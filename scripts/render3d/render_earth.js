// node render_earth.js <out.mp4> [seconds=12] [lon=5] [lat=8] [w=1080] [h=1920]
const fs = require('fs'), path = require('path'), http = require('http'), { spawn } = require('child_process');
const { chromium } = require('playwright');
const ROOT = path.resolve(__dirname, '../..');
const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.mjs': 'text/javascript', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg' };
const server = http.createServer((req, res) => {
  const p = decodeURIComponent(req.url.split('?')[0]);
  if (p === '/favicon.ico') { res.writeHead(204); return res.end(); }
  const f = p === '/' ? path.join(__dirname, 'scene_earth.html') : p.startsWith('/_three/') ? path.join(__dirname, 'node_modules/three', p.slice(8)) : path.join(ROOT, p);
  fs.readFile(f, (e, d) => { if (e) { console.log('[server 404]', p); res.writeHead(404); return res.end('nf'); } res.writeHead(200, { 'Content-Type': MIME[path.extname(f).toLowerCase()] || 'application/octet-stream' }); res.end(d); });
});
(async () => {
  const [out, secs = '12', lon = '5', lat = '8', w = '1080', h = '1920'] = process.argv.slice(2);
  if (!out) { console.error('usage: node render_earth.js <out.mp4> [seconds] [lon] [lat] [w] [h]'); process.exit(2); }
  const FPS = 30, S = +secs, N = Math.round(S * FPS);
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  const port = server.address().port;
  const browser = await chromium.launch({ args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist', '--enable-webgl'] });
  const page = await browser.newPage({ viewport: { width: +w, height: +h } });
  page.on('console', m => { if (m.type() === 'error') console.log('[page error]', m.text().slice(0, 300)); });
  page.on('pageerror', e => console.error('[pageerror]', e.message));
  await page.goto(`http://127.0.0.1:${port}/?w=${w}&h=${h}&lon=${lon}&lat=${lat}`);
  await page.waitForFunction('window.ready === true', null, { timeout: 90000 });
  // light film look: fine grain + soft vignette
  const ff = spawn('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'image2pipe', '-framerate', String(FPS), '-i', '-', '-vf', 'noise=alls=4:allf=t,vignette=PI/6', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '16', out], { stdio: ['pipe', 'inherit', 'inherit'] });
  const canvas = await page.$('canvas');
  for (let i = 0; i < N; i++) {
    await page.evaluate(([t, d]) => window.renderAt(t, d), [i / FPS, S]);
    const png = await canvas.screenshot({ type: 'png' });
    if (!ff.stdin.write(png)) await new Promise(r => ff.stdin.once('drain', r));
    if (i % 30 === 0) console.log('frame', i, '/', N);
  }
  ff.stdin.end(); await new Promise(r => ff.on('close', r));
  await browser.close(); server.close(); console.log('done', out);
})().catch(e => { console.error(e); process.exit(1); });
