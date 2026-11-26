// node render_games.js jobs.json   (jobs = [{out, spec, seconds, fps}])  - draws brain-game scenes (game.html) frame by frame and encodes them with ffmpeg
const { chromium } = require('playwright'), fs = require('fs'), path = require('path'), { spawn } = require('child_process');
(async () => {
  const jobs = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  const browser = await chromium.launch({ args: ['--use-gl=swiftshader', '--no-sandbox'] });
  const page = await browser.newPage({ viewport: { width: 1080, height: 1920 }, deviceScaleFactor: 1 });
  page.on('pageerror', e => console.error('PAGE ERROR', String(e).slice(0, 300)));
  await page.goto('file://' + path.join(__dirname, 'game.html'));
  let failed = 0;
  for (const j of jobs) {
    try {
      const fps = j.fps || 30, n = Math.round((j.seconds || 7.5) * fps);
      fs.mkdirSync(path.dirname(path.resolve(j.out)), { recursive: true });
      const ff = spawn('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'image2pipe', '-framerate', String(fps), '-i', '-', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '18', '-preset', 'veryfast', j.out], { stdio: ['pipe', 'inherit', 'inherit'] });
      const done = new Promise((res) => ff.on('close', res));
      console.log('game', j.out, (j.spec && j.spec.k) || '?', n + ' frames');
      for (let i = 0; i < n; i++) {
        await page.evaluate(([s, t]) => window.draw(s, t), [j.spec, i / fps]);
        const buf = await page.screenshot({ type: 'jpeg', quality: 90 });
        if (!ff.stdin.write(buf)) await new Promise(r => ff.stdin.once('drain', r));
      }
      ff.stdin.end(); await done; console.log('done', j.out);
    } catch (e) { failed++; console.error('JOB FAILED', j.out, String(e && e.message || e).slice(0, 300)); try { fs.unlinkSync(j.out); } catch (x) {} }
  }
  await browser.close(); process.exit(failed && failed === jobs.length ? 1 : 0);
})();
