#!/usr/bin/env python3
"""
STUDIO CORE - product motion-ad renderer (code only, no video model).

    python ad_render.py spec.json out.mp4 [--res 0.5] [--qa qa.json] [--frames N]

spec.json (written by run_job.py after the planner):
{
 "aspect": "9:16" | "16:9", "duration": 30, "fps": 30, "style": "dark_studio" | "light_studio",
 "palette": {"bg": "#0b0a0d", "accent": "#e01a24", "text": "#f2efe9"},
 "product": {"kind": "pouch" | "box" | "flat", "name": "...", "brand": "...",
             "front": "front.png", "back": "back.png"|null, "side": "side.png"|null},     # PNG files WITH alpha (already cut out)
 "shots": [ {"type": "reveal|turntable|orbit|hero|macro|detail|end", "dur": 4, "texts": [
               {"role": "headline|sub|cta|brand", "text": "...", "anim": "mask_up|track_in|fade", "at": 0.8, "dur": 3, "zone": "top|center|bottom|behind|below", "case": "upper|asis"} ],
             "macro_box": [x0,y0,x1,y1] (0..1 of the front picture), "yaw": [a0,a1] } ],
 "voice": [ {"shot": 1, "file": "line1.mp3", "dur": 3.2, "start": 0.5} ]       # optional
}
Rules built in: silent unless "voice" is given, no sound effects, no pop / bounce, text never overlaps text, text stays inside the safe margin,
text colour flips for contrast, flat photos never rotate past 22 degrees, macro shots fall back to orbit when the picture is too small to zoom.
"""
import json, math, os, subprocess, sys
import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))


def clamp(x, a=0.0, b=1.0): return max(a, min(b, x))
def ease_io(x): x = clamp(x); return x * x * (3 - 2 * x)
def ease_out(x): x = clamp(x); return 1 - (1 - x) ** 3
def hex2bgr(h):
    h = h.lstrip('#'); return (int(h[4:6], 16), int(h[2:4], 16), int(h[0:2], 16))
def lum(bgr): return (0.114 * bgr[0] + 0.587 * bgr[1] + 0.299 * bgr[2]) / 255.0


# ----------------------------------------------------------------------------- fonts
_FONT_CACHE = {}
_FILES = {
    'display': ['Poppins-SemiBold.ttf', 'Poppins-Bold.ttf', 'Inter-SemiBold.otf', 'Inter-SemiBold.ttf', 'DejaVuSans-Bold.ttf', 'LiberationSans-Bold.ttf'],
    'bold': ['Poppins-Bold.ttf', 'Inter-Bold.otf', 'Inter-Bold.ttf', 'DejaVuSans-Bold.ttf', 'LiberationSans-Bold.ttf'],
    'light': ['Poppins-Light.ttf', 'Inter-Light.otf', 'Inter-Light.ttf', 'DejaVuSans.ttf', 'LiberationSans-Regular.ttf'],
}
_DIRS = [os.path.join(HERE, 'fonts'), '/usr/share/fonts/truetype/google-fonts', '/usr/share/fonts/opentype/inter', '/usr/share/fonts/truetype/inter',
         '/usr/share/fonts/truetype/dejavu', '/usr/share/fonts/truetype/liberation', '/usr/share/fonts/truetype']


def get_font(role, size):
    key = (role, int(size))
    if key in _FONT_CACHE: return _FONT_CACHE[key]
    for fn in _FILES.get(role, _FILES['display']):
        for d in _DIRS:
            p = os.path.join(d, fn)
            if os.path.exists(p):
                f = ImageFont.truetype(p, int(size)); _FONT_CACHE[key] = f; return f
    f = ImageFont.load_default(); _FONT_CACHE[key] = f; return f


# ----------------------------------------------------------------------------- stage (background, floor, light)
def make_stage(W, H, style, pal, bgspec=None):
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    bg = np.array(hex2bgr(pal['bg']), np.float32); ac = np.array(hex2bgr(pal['accent']), np.float32)
    floor_y = int(H * (0.76 if W < H else 0.80))
    if style == 'light_studio':
        base = np.zeros((H, W, 3), np.float32); base[:] = np.array([236, 233, 229], np.float32)
        d = np.sqrt(((xx - W * .5) / (W * .7)) ** 2 + ((yy - H * .45) / (H * .7)) ** 2)
        base -= (np.clip(d, 0, 1.2) ** 2 * 26)[..., None]
        base[floor_y:] -= ((yy[floor_y:] - floor_y) / (H - floor_y) * 14)[..., None]
        glow = np.clip(1 - d, 0, 1) ** 2 * 0.0
    else:
        base = np.zeros((H, W, 3), np.float32); base[:] = bg * 0.9 + 3
        d = np.sqrt(((xx - W * .5) / (W * .62)) ** 2 + ((yy - H * .42) / (H * .55)) ** 2)
        glow = np.clip(1 - d, 0, 1) ** 2.2
        base[floor_y:] += ((yy[floor_y:] - floor_y) / (H - floor_y) * 10)[..., None] * 0.8
        base += np.exp(-((yy - floor_y) / 3.0) ** 2)[..., None] * np.array([30, 26, 40], np.float32) * 0.30
    if bgspec and isinstance(bgspec, dict):
        t = bgspec.get('type'); c1 = bgspec.get('color'); c2 = bgspec.get('color2') or c1
        vig = (1 - 0.30 * np.clip(np.sqrt(((xx - W * .5) / (W * .75)) ** 2 + ((yy - H * .5) / (H * .75)) ** 2), 0, 1.2) ** 2)[..., None]
        if t in ('solid', 'gradient') and c1:
            a_, b_ = np.array(hex2bgr(c1), np.float32), np.array(hex2bgr(c2), np.float32)
            k = (yy / H)[..., None] if bgspec.get('dir', 'vertical') == 'vertical' else (xx / W)[..., None]
            if bgspec.get('dir') == 'radial': k = np.clip(np.sqrt(((xx - W * .5) / (W * .7)) ** 2 + ((yy - H * .45) / (H * .7)) ** 2), 0, 1)[..., None]
            base = (a_ * (1 - k) + b_ * k) * vig
        elif t == 'image' and bgspec.get('image') and os.path.exists(bgspec['image']):
            im = cv2.imread(bgspec['image'])
            if im is not None:
                sc = max(W / im.shape[1], H / im.shape[0]); im = cv2.resize(im, None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA if sc < 1 else cv2.INTER_CUBIC)
                y0 = (im.shape[0] - H) // 2; x0 = (im.shape[1] - W) // 2; im = im[y0:y0 + H, x0:x0 + W].astype(np.float32)
                if bgspec.get('blur', 1): im = cv2.GaussianBlur(im, (0, 0), 0.012 * H)      # a soft background keeps the product sharp
                base = im * float(bgspec.get('dim', 0.8)) * vig
        if t in ('solid', 'gradient', 'image'):
            glow = glow * 0.0; floor_y = floor_y
    tint = (ac * 0.30 + 6).astype(np.float32)
    return base, glow.astype(np.float32), tint, floor_y


# ----------------------------------------------------------------------------- the 3D product
class Product:
    """A thick slab: photo on the front, back photo (or a plain panel with the name) on the back, sides from the side photo or from the front's edge colours."""

    def __init__(self, W, H, spec, pal):
        self.W, self.H = W, H
        p = spec['product']; self.kind = p.get('kind', 'pouch')
        fr = cv2.imread(p['front'], cv2.IMREAD_UNCHANGED)
        if fr.shape[2] == 3: fr = np.dstack([fr, np.full(fr.shape[:2], 255, np.uint8)])
        self.front_src = fr
        ar = fr.shape[1] / fr.shape[0]
        maxh = 0.56 * H if W < H else 0.62 * H
        maxw = 0.56 * W if W < H else 0.30 * W
        PH = maxh; PW = PH * ar
        if PW > maxw: PW = maxw; PH = PW / ar
        self.PW, self.PH = int(PW), int(PH)

        def prep(img):
            img = cv2.resize(img, (self.PW, self.PH), interpolation=cv2.INTER_AREA if img.shape[1] > self.PW else cv2.INTER_LANCZOS4)
            return img[..., :3].astype(np.float32), cv2.GaussianBlur(img[..., 3].astype(np.float32) / 255, (3, 3), 0)
        self.fr_rgb, self.fr_a = prep(fr)
        # back
        if p.get('back'):
            b = cv2.imread(p['back'], cv2.IMREAD_UNCHANGED)
            if b.shape[2] == 3: b = np.dstack([b, np.full(b.shape[:2], 255, np.uint8)])
            self.bk_rgb, self.bk_a = prep(b); self.back_is_real = True
        else:
            self.bk_rgb, self.bk_a = self._placeholder_back(p, pal); self.back_is_real = False
        # depth + sides
        if self.kind == 'flat':
            self.T = max(2.0, self.PW * 0.012)
        elif p.get('side'):
            s = cv2.imread(p['side'], cv2.IMREAD_UNCHANGED)
            depth = float(np.clip(self.PH * s.shape[1] / s.shape[0], self.PW * 0.10, self.PW * 0.95))
            self.T = depth / 2
        else:
            self.T = self.PW * (0.12 if self.kind == 'pouch' else 0.17)
        if p.get('side'):
            s = cv2.imread(p['side'], cv2.IMREAD_UNCHANGED)
            if s.shape[2] == 3: s = np.dstack([s, np.full(s.shape[:2], 255, np.uint8)])
            s = cv2.resize(s, (max(8, int(self.T * 2)), self.PH), interpolation=cv2.INTER_AREA)
            self.side_l = s[..., :3].astype(np.float32); self.side_r = self.side_l[:, ::-1].copy(); self.side_real = True
        else:
            self.side_l = self._edge_side('l'); self.side_r = self._edge_side('r'); self.side_real = False
        if self.kind == 'pouch':
            self.ys = [self.PH, 0.66 * self.PH, 0.30 * self.PH, 0.0]; self.zs = [0.30, 0.85, 1.0, 0.72]
            self.rows = [0, int(0.34 * self.PH), int(0.70 * self.PH), self.PH]
        else:
            self.ys = [self.PH, 0.0]; self.zs = [1.0, 1.0]; self.rows = [0, self.PH]
        self.maxyaw = 22.0 if self.kind == 'flat' else 360.0

    def _placeholder_back(self, p, pal):
        bgc = np.array(hex2bgr(pal['bg']), np.float32) * 0.9 + 14
        img = np.zeros((self.PH, self.PW, 3), np.float32); img[:] = bgc
        g = np.linspace(1.15, 0.85, self.PH)[:, None, None]; img *= g
        pil = Image.new('RGB', (self.PW, self.PH), (0, 0, 0)); d = ImageDraw.Draw(pil)
        txt = str(p.get('brand') or p.get('name') or '').upper()
        if txt:
            sz = int(self.PW * 0.16)
            while sz > 12 and d.textlength(txt, font=get_font('display', sz)) > self.PW * 0.78: sz -= 2
            f = get_font('display', sz); w = d.textlength(txt, font=f)
            d.text(((self.PW - w) / 2, self.PH * 0.46), txt, font=f, fill=(255, 255, 255))
        m = np.array(pil).astype(np.float32)[..., 0:1] / 255
        acc = np.array(hex2bgr(pal['accent']), np.float32) * 0.5 + 120
        img = img * (1 - m * 0.9) + acc * m * 0.9
        return img, self.fr_a.copy()

    def _edge_side(self, which):
        tw = 64; a = self.fr_a; rgb = self.fr_rgb
        cols = slice(0, max(2, int(self.PW * 0.05))) if which == 'l' else slice(self.PW - max(2, int(self.PW * 0.05)), self.PW)
        m = (a[:, cols] > 0.9).astype(np.float32)[..., None]
        num = (rgb[:, cols] * m).sum(1); den = m.sum(1)
        mean_all = (rgb * (a > 0.9)[..., None]).sum((0, 1)) / max(1.0, (a > 0.9).sum())
        row = np.where(den > 0, num / np.maximum(den, 1), mean_all)
        row = cv2.GaussianBlur(row.astype(np.float32), (1, 0), sigmaX=0.1, sigmaY=max(2, self.PH * 0.02)) if row.ndim == 2 else row
        u = np.linspace(-1, 1, tw)[None, :]; prof = np.clip(1 - u ** 2, 0, 1) ** 0.6
        g = row[:, None, :] * (0.40 + 0.85 * prof)[..., None]
        g += np.clip((prof - 0.85) * 6, 0, 1)[..., None] * 28
        return g.astype(np.float32)

    # -- one textured quad warped into the frame buffers
    def _face(self, tex, alpha, quad, shade, layer, alay):
        quad = np.float32(quad); W, H = self.W, self.H
        x0 = int(max(0, math.floor(quad[:, 0].min()) - 1)); y0 = int(max(0, math.floor(quad[:, 1].min()) - 1))
        x1 = int(min(W, math.ceil(quad[:, 0].max()) + 2)); y1 = int(min(H, math.ceil(quad[:, 1].max()) + 2))
        if x1 - x0 < 2 or y1 - y0 < 2: return
        h, w = tex.shape[:2]
        src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
        M = cv2.getPerspectiveTransform(src, quad - np.float32([x0, y0]))
        wr = cv2.warpPerspective(tex * shade, M, (x1 - x0, y1 - y0), flags=cv2.INTER_LINEAR)
        wa = cv2.warpPerspective(alpha, M, (x1 - x0, y1 - y0), flags=cv2.INTER_LINEAR)
        layer[y0:y1, x0:x1] += wr * wa[..., None]; alay[y0:y1, x0:x1] += wa

    def render(self, yaw_deg, cx, base, scale=1.0):
        """-> rgb(H,W,3) float, alpha(H,W) float, rect(x0,y0,x1,y1)"""
        W, H = self.W, self.H; PW, PH = self.PW * scale, self.PH * scale
        th = math.radians(yaw_deg); c = math.cos(th); s = math.sin(th)
        D = PH * 4.5; Wd = PW / 2; T = self.T * scale
        def Pj(x, y, z):
            xr = x * c + z * s; zr = -x * s + z * c; f = D / (D - zr)
            return [cx + xr * f, (base - PH / 2) - (y - PH / 2) * f]
        L = np.array([-0.45, 0.45, 0.77]); L /= np.linalg.norm(L)
        def sh(n): return 0.55 + 0.62 * max(0.0, float(np.dot(n, L)))
        layer = np.zeros((H, W, 3), np.float32); alay = np.zeros((H, W), np.float32)
        ys = [v * scale for v in self.ys]; n = len(ys) - 1
        for k in range(n):
            zt, zb = T * self.zs[k], T * self.zs[k + 1]; yt, yb = ys[k], ys[k + 1]; r0, r1 = self.rows[k], self.rows[k + 1]
            if k < n - 1:      # overlap the next slice by 2 picture rows so no hairline gap shows between slices
                e = 2; r1e = min(self.PH, r1 + e); fe = (r1e - r0) / max(1, (r1 - r0)); yb = yt + (yb - yt) * fe; zb = zt + (zb - zt) * fe; r1 = r1e
            if c > 0:
                self._face(self.fr_rgb[r0:r1], self.fr_a[r0:r1], [Pj(-Wd, yt, zt), Pj(Wd, yt, zt), Pj(Wd, yb, zb), Pj(-Wd, yb, zb)], sh([s, 0, c]), layer, alay)
            else:
                self._face(self.bk_rgb[r0:r1], self.bk_a[r0:r1], [Pj(Wd, yt, -zt), Pj(-Wd, yt, -zt), Pj(-Wd, yb, -zb), Pj(Wd, yb, -zb)], sh([-s, 0, -c]), layer, alay)
            if s < 0:
                t_ = self.side_r[r0:r1]; self._face(t_, np.ones(t_.shape[:2], np.float32), [Pj(Wd, yt, zt), Pj(Wd, yt, -zt), Pj(Wd, yb, -zb), Pj(Wd, yb, zb)], sh([c, 0, -s]), layer, alay)
            if s > 0:
                t_ = self.side_l[r0:r1]; self._face(t_, np.ones(t_.shape[:2], np.float32), [Pj(-Wd, yt, -zt), Pj(-Wd, yt, zt), Pj(-Wd, yb, zb), Pj(-Wd, yb, -zb)], sh([-c, 0, s]), layer, alay)
        a = np.clip(alay, 0, 1)
        rgb = layer / np.maximum(alay, 1e-3)[..., None]        # divide by the RAW coverage so overlapped slice rows keep their true colour
        cols = np.where(a.max(0) > 0.05)[0]; rows = np.where(a.max(1) > 0.05)[0]
        if len(cols) == 0: return rgb, a, (0, 0, 1, 1)
        return rgb, a, (int(cols.min()), int(rows.min()), int(cols.max()) + 1, int(rows.max()) + 1)


# ----------------------------------------------------------------------------- text engine
class TextBlock:
    LIGHT = False
    ZONES = {'top': 0.12, 'below': 0.0, 'left': 0.5, 'right': 0.5, 'behind': 0.42}
    ALIAS = {'bottom': 'below', 'center': 'below'}

    def __init__(self, W, H, d, shot_dur, floor_y):
        self.W, self.H, self.floor_y = W, H, floor_y
        self.role = d.get('role', 'headline'); self.anim = d.get('anim', 'mask_up')
        if self.anim not in ('mask_up', 'track_in', 'fade'): self.anim = 'mask_up'
        z = self.ALIAS.get(d.get('zone', 'below'), d.get('zone', 'below')); self.zone = z if z in self.ZONES else 'below'
        txt = str(d.get('text', '')).strip()
        self.text = txt.upper() if (d.get('case', 'upper' if self.role != 'headline' else 'asis') == 'upper') else txt
        self.at = float(d.get('at', 0.8)); self.dur = float(d.get('dur', max(1.5, shot_dur - self.at - 0.4)))
        self.color = d.get('color'); self.scale_fit = 1.0
        self.forced = '|' in txt; self.blend = d.get('blend', 'normal')
        if self.forced: self.text = txt.upper() if (d.get('case', 'asis') == 'upper') else txt
        self.scales = d.get('scales') or []; self.colors = d.get('colors') or []
        self.rect = None; self.lines = []; self.sz = 0; self.font_role = 'display'; self.track = 0.0
        m = min(W, H); portrait = H > W
        self.base = {'headline': (0.062 * H if not portrait else 0.050 * H), 'sub': 0.031 * H if not portrait else 0.023 * H, 'cta': 0.033 * H if not portrait else 0.025 * H, 'brand': 0.070 * H if not portrait else 0.050 * H}[self.role] if self.role in ('headline', 'sub', 'cta', 'brand') else 0.05 * H
        if self.zone == 'behind': self.base = (0.20 * H if not portrait else 0.115 * H)
        self.base *= float(d.get('size', 1.0))
        self.font_role = {'headline': 'display', 'sub': 'light', 'cta': 'display', 'brand': 'bold'}.get(self.role, 'display')
        self.track = {'headline': 0.01, 'sub': 0.18, 'cta': 0.14, 'brand': 0.12}.get(self.role, 0.02)
        if self.zone == 'behind': self.font_role = 'bold'; self.track = 0.0

    def layout(self, shrink=1.0):
        W, H = self.W, self.H; maxw = W * {'behind': 0.92, 'left': 0.29, 'right': 0.29}.get(self.zone, 0.80)
        sz = max(10, int(self.base * shrink)); words = self.text.split()
        tmp = ImageDraw.Draw(Image.new('L', (4, 4)))
        if self.forced:
            parts = [p.strip() for p in self.text.split('|') if p.strip()][:5]
            sc = [float(self.scales[i]) if i < len(self.scales) else 1.0 for i in range(len(parts))]
            while True:
                fs = [get_font('bold', max(10, int(sz * c))) for c in sc]
                lw = [sum(tmp.textlength(ch, font=f) for ch in p) + max(0, len(p) - 1) * self.track * f.size for p, f in zip(parts, fs)]
                if max(lw) <= maxw or sz <= 14: break
                sz = int(sz * 0.93)
            self.sz = sz; self.f = fs[0]; self.fs = fs; self.lines = parts; self.lw = lw
            self.lhs = [int(f.size * 1.02) for f in fs]; self.lh = max(self.lhs)
            self.offs = [sum(self.lhs[:i]) for i in range(len(parts))]
            self.w = int(max(lw)); self.h = sum(self.lhs); return
        def width(s, f):
            return sum(tmp.textlength(ch, font=f) for ch in s) + max(0, len(s) - 1) * self.track * f.size
        while True:
            f = get_font(self.font_role, sz); lines = []; cur = ''
            for w_ in words:
                t_ = (cur + ' ' + w_).strip()
                if width(t_, f) <= maxw or not cur: cur = t_
                else: lines.append(cur); cur = w_
            if cur: lines.append(cur)
            fits = all(width(l, f) <= maxw for l in lines) and len(lines) <= (2 if self.zone == 'behind' else 4 if self.zone in ('left', 'right') else 3)
            if fits or sz <= 14: break
            sz = int(sz * 0.93)
        self.sz = sz; self.f = f; self.lines = lines; self.lh = int(sz * 1.18)
        self.lw = [width(l, f) for l in lines]
        self.fs = [f] * len(lines); self.lhs = [self.lh] * len(lines); self.offs = [self.lh * i for i in range(len(lines))]
        self.w = int(max(self.lw)) if lines else 0; self.h = self.lh * len(lines)

    def place(self, y_center=None, top_anchor=None):
        W, H = self.W, self.H
        if self.zone == 'behind' and top_anchor is not None and self.H > self.W and y_center is None: y_center = top_anchor - self.h * 0.28
        if y_center is None:
            y_center = (self.floor_y + 0.045 * H + self.h / 2 + 0.01 * H) if self.zone == 'below' else self.ZONES[self.zone] * H + (self.h / 2 if self.zone == 'top' else 0)
        y = y_center - self.h / 2
        cxz = {'left': 0.20, 'right': 0.80}.get(self.zone, 0.5) * W
        self.x = cxz - self.w / 2; self.y = y
        self.rect = (self.x, self.y, self.x + self.w, self.y + self.h)

    def draw(self, frame, t, bg_lum_fn):
        """t = seconds since the SHOT started. draws into frame (float32 BGR)."""
        lt = t - self.at
        if lt < 0 or lt > self.dur + 0.5 or not self.lines: return
        out = clamp(1 - (lt - self.dur) / 0.5) if lt > self.dur else 1.0
        pal_col = self.color
        if self.col_final is None:
            # contrast: sample what is under the text the first time it shows
            L = bg_lum_fn(self.rect); self.col_final = (245, 243, 240) if L < (0.30 if TextBlock.LIGHT else 0.78) else (22, 22, 24)   # a dark stage keeps light text even over the product's reflection
            if pal_col and (self.zone == 'behind' or self.forced): self.col_final = hex2bgr(pal_col)
        col = self.col_final
        for i, line in enumerate(self.lines):
            li = lt - i * 0.13
            p = ease_out(li / (0.85 if self.anim != 'track_in' else 1.2))
            if p <= 0: continue
            lx = self.x + (self.w - self.lw[i]) / 2; ly = self.y + self.offs[i]; fl = self.fs[i]; lhi = self.lhs[i]
            colL = col
            if i < len(self.colors) and isinstance(self.colors[i], str) and self.colors[i].startswith('#'): colL = hex2bgr(self.colors[i])
            pad = int(self.sz * 0.35); extra = int((len(line) - 1) * self.track * self.sz * 0.9) + 2 if self.anim == 'track_in' else 0; bw = int(self.lw[i]) + extra + 2 * pad; bh = lhi + pad
            canvas = Image.new('L', (bw, bh), 0); d = ImageDraw.Draw(canvas)
            sp = self.track * self.sz * ((1 + (1 - p) * 0.9) if self.anim == 'track_in' else 1)
            x = pad
            if self.anim == 'track_in':
                tot = sum(d.textlength(ch, font=fl) for ch in line) + (len(line) - 1) * sp
                x = pad + (self.lw[i] + extra - tot) / 2
            for ch in line:
                d.text((x, pad * 0.3), ch, font=fl, fill=255); x += d.textlength(ch, font=fl) + sp
            m = np.array(canvas).astype(np.float32) / 255
            if self.anim == 'mask_up':
                shift = int((1 - p) * lhi * 0.95)
                if shift >= bh: continue
                m2 = np.zeros_like(m); m2[shift:] = m[:bh - shift]; m = m2
                # hard clip: the text rises out of an invisible line
                alpha_mul = out
            elif self.anim == 'fade':
                alpha_mul = p * out; m = np.roll(m, int((1 - p) * -8), axis=0)
            else:
                alpha_mul = p * out
            x0 = int(lx - pad - extra / 2); y0 = int(ly - pad * 0.3)
            xa, ya = max(0, x0), max(0, y0); xb, yb = min(self.W, x0 + bw), min(self.H, y0 + bh)
            if xb <= xa or yb <= ya: continue
            mm = m[ya - y0:yb - y0, xa - x0:xb - x0] * alpha_mul
            roi = frame[ya:yb, xa:xb]
            if self.blend == 'negative':      # the text INVERTS whatever is under it (the "negative text" look)
                roi[:] = roi * (1 - mm[..., None]) + (255.0 - roi) * mm[..., None]
                continue
            if lum(colL) > 0.5:     # soft shadow under light text
                sh_ = cv2.GaussianBlur(mm, (0, 0), max(2, self.sz * 0.08)) * 0.45
                roi *= (1 - sh_)[..., None]
            roi[:] = roi * (1 - mm[..., None]) + np.array(colL, np.float32) * mm[..., None]


def layout_shot_texts(W, H, shot, qa, floor_y, keepout):
    """Builds every text block of one shot: text never touches text, never covers the product, and stays inside the safe margin."""
    blocks = [TextBlock(W, H, d, float(shot['dur']), floor_y) for d in shot.get('texts', []) if str(d.get('text', '')).strip()]
    if shot.get('type') == 'macro' and blocks:      # a macro shot is the product's own detail: our text would land on the pack's printed words
        blocks = []; qa.append({'check': 'macro_text', 'status': 'NOTE', 'detail': 'text on a macro shot was removed so it does not cover the pack\'s own words'})
    for b in blocks: b.col_final = None
    alts = ['left', 'right', 'top', 'below'] if W > H else ['below', 'top']
    gap = 0.018 * H; shrink = 1.0; clean = False
    for attempt in range(9):
        for b in blocks: b.layout(shrink)
        placed = []
        for b in sorted(blocks, key=lambda x: x.at):
            b.place(top_anchor=(keepout[1] if keepout is not None else None))
            if keepout is not None and b.zone != 'behind' and rect_hit(b.rect, keepout):
                z0 = b.zone
                for alt in alts:
                    b.zone = alt; b.layout(shrink); b.place()
                    if not rect_hit(b.rect, keepout): break
                else:
                    b.zone = z0; b.layout(shrink); b.place()
            for q in placed:
                if q.zone == b.zone and not (b.at >= q.at + q.dur or q.at >= b.at + b.dur) and rect_hit(b.rect, q.rect):
                    b.place(q.rect[3] + gap + b.h / 2)
            placed.append(b)
        ok = all(b.rect[0] >= W * 0.05 and b.rect[2] <= W * 0.95 and b.rect[1] >= H * 0.05 and b.rect[3] <= H * 0.94 for b in blocks if b.zone != 'behind')
        free = all(not (keepout is not None and b.zone != 'behind' and rect_hit(b.rect, keepout)) for b in blocks)
        hits = any(rect_hit(a.rect, b.rect) and not (a.at >= b.at + b.dur or b.at >= a.at + a.dur) for i, a in enumerate(blocks) for b in blocks[i + 1:])
        if ok and free and not hits: clean = True; break
        shrink *= 0.9
    if not clean: qa.append({'check': 'text_layout', 'status': 'WARN', 'detail': 'text did not fit cleanly after shrinking'})
    return blocks


def rect_hit(a, b):
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


# ----------------------------------------------------------------------------- shots
def shot_pose(shot, t, prod, W, H, pal, macro_ok):
    """-> dict(yaw, scale, dx, dy (fractions of W,H), glow, cam, fade, sweep, macro)"""
    typ = shot['type']; dur = float(shot['dur']); u = t / dur
    flat = prod.kind == 'flat'
    def lim(y): return float(np.clip(y, -22, 22)) if flat else y
    P = dict(yaw=0.0, scale=1.0, dx=0.0, dy=0.0, glow=1.0, cam=1.0, fade=1.0, sweep=None, macro=None)
    if typ == 'macro' and not macro_ok: typ = 'orbit'
    if typ == 'reveal':
        P.update(yaw=lim(-20 + 12 * ease_io(u)), glow=0.10 + 0.9 * ease_io(t / min(dur, 2.4)), cam=1.0 + 0.045 * ease_io(u), fade=ease_io(t / 1.4), sweep=(dur * 0.50, dur * 0.50 + 1.8))
    elif typ == 'turntable':
        a = shot.get('yaw', [0, 360]); P.update(yaw=lim(a[0] + (a[1] - a[0]) * ease_io((t - 0.4) / max(1.0, dur - 1.8))) if not flat else lim(-18 + 36 * ease_io(u)), cam=1.0 + 0.05 * ease_io(u))
    elif typ == 'orbit':
        a = shot.get('yaw', [28, -28]); P.update(yaw=lim(a[0] + (a[1] - a[0]) * ease_io(u)), scale=1.08, cam=1.03 + 0.07 * ease_io(u), sweep=(dur * 0.35, dur * 0.35 + 1.6))
    elif typ == 'hero':
        P.update(yaw=lim(-7 + 14 * ease_io(u)), scale=1.0, cam=1.0 + 0.04 * ease_io(u), sweep=(dur * 0.55, dur * 0.55 + 1.6))
    elif typ == 'detail':
        side = -1 if shot.get('side', 'left') == 'left' else 1
        if W > H: P.update(dx=0.20 * side, scale=1.0)
        else: P.update(dy=-0.16, scale=0.82)
        P.update(yaw=lim(-14 + 28 * ease_io(u) if side < 0 else 14 - 28 * ease_io(u)), cam=1.0 + 0.03 * ease_io(u))
    elif typ == 'end':
        P.update(yaw=lim(-10 * (1 - ease_io(t / 1.8))), scale=0.88 if W > H else 0.78, dy=-0.10 if W > H else -0.12, cam=1.0 + 0.02 * ease_io(u), sweep=(0.6, 2.2))
    elif typ == 'macro':
        P.update(macro=dict(u=ease_io(u)), sweep=(dur * 0.30, dur * 0.30 + 1.9))
    P['fade_out'] = 1.0
    return P


def macro_plate(prod, box, u, W, H, bgc, dx_dir=1):
    fr = prod.front_src; fh, fw = fr.shape[:2]
    x0, y0, x1, y1 = [float(v) for v in box]
    cxr, cyr = (x0 + x1) / 2 * fw, (y0 + y1) / 2 * fh; bw, bh = (x1 - x0) * fw, (y1 - y0) * fh
    asp = W / H; cw = max(bw, bh * asp) * 1.15; ch = cw / asp
    z = 1.0 + 0.12 * u; cw2, ch2 = cw / z, ch / z
    cxr += (u - 0.5) * cw * 0.05 * dx_dir
    M = np.float32([[W / cw2, 0, -(cxr - cw2 / 2) * W / cw2], [0, H / ch2, -(cyr - ch2 / 2) * H / ch2]])
    rgb = cv2.warpAffine(fr[..., :3], M, (W, H), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0)).astype(np.float32)
    a = cv2.warpAffine(fr[..., 3], M, (W, H), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT, borderValue=0).astype(np.float32) / 255
    plate = rgb * a[..., None] + bgc * (1 - a[..., None])
    return plate, W / cw


def macro_possible(prod, shot, W, H):
    box = shot.get('macro_box') or [0.15, 0.30, 0.85, 0.75]
    fh, fw = prod.front_src.shape[:2]; asp = W / H
    cw = max((box[2] - box[0]) * fw, (box[3] - box[1]) * fh * asp) * 1.15
    if (W / cw) > 2.3: return False
    # a zoom into a plain area (a blank screen, a flat colour) is a white or empty frame: only zoom where there is detail
    x0, y0, x1, y1 = int(box[0] * fw), int(box[1] * fh), int(box[2] * fw), int(box[3] * fh)
    crop = prod.front_src[y0:y1, x0:x1]
    if crop.size:
        g = cv2.cvtColor(crop[..., :3].astype(np.uint8), cv2.COLOR_BGR2GRAY).astype(np.float32)
        m = (crop[..., 3] > 200) if crop.shape[2] == 4 else np.ones(g.shape, bool)
        if m.sum() > 100 and float(g[m].std()) < 22: return False
    return True


# ----------------------------------------------------------------------------- the renderer
def sweep_band(xx, yy, x0, x1, ycen, hh, pos):
    r = (xx - x0) / max(1.0, (x1 - x0))
    return np.exp(-((r - pos + (yy - ycen) / max(1.0, hh) * 0.35) / 0.08) ** 2)


class Renderer:
    def __init__(self, spec, res=1.0):
        asp = spec.get('aspect', '9:16')
        W, H = (1080, 1920) if asp == '9:16' else (1920, 1080)
        if asp == '1:1': W, H = 1080, 1080
        self.W, self.H = int(W * res) // 2 * 2, int(H * res) // 2 * 2
        self.spec = spec; self.fps = int(spec.get('fps', 30)); W, H = self.W, self.H
        self.pal = {'bg': '#0b0a0d', 'accent': '#e01a24', 'text': '#f2efe9', **(spec.get('palette') or {})}
        self.style = spec.get('style', 'dark_studio'); TextBlock.LIGHT = (self.style == 'light_studio')
        self.base, self.glow, self.tint, self.floor_y = make_stage(W, H, self.style, self.pal, spec.get('background'))
        self.prod = Product(W, H, spec, self.pal)
        self.yy, self.xx = np.mgrid[0:H, 0:W].astype(np.float32)
        self.rng = np.random.RandomState(11)
        self.dust = [(self.rng.rand() * W, self.rng.rand() * H, self.rng.rand(), self.rng.rand() * 6.28) for _ in range(55)]
        self.qa = []
        self.shots = self._fit_timeline()
        self.macro_ok = [macro_possible(self.prod, s, W, H) for s in self.shots]
        self.texts = [layout_shot_texts(W, H, s, self.qa, self.floor_y, self._keepout(i, s)) for i, s in enumerate(self.shots)]
        for i, s in enumerate(self.shots):
            if s['type'] == 'macro' and not self.macro_ok[i]:
                self.qa.append({'check': 'macro', 'status': 'FALLBACK', 'detail': f'shot {i + 1}: the picture is too small to zoom in sharply, used a close orbit instead'})
        e = self.pal['text']; self.vg = 1 - 0.40 * np.clip(np.sqrt(((self.xx - W / 2) / (W * .62)) ** 2 + ((self.yy - H / 2) / (H * .70)) ** 2), 0, 1) ** 2
        if self.style == 'light_studio': self.vg = 1 - 0.12 * (1 - self.vg) / 0.40
        self.edge = np.clip(((self.xx - W / 2) / (W / 2)) ** 2 * 0.9 + ((self.yy - H / 2) / (H / 2)) ** 2 * 0.35, 0, 1)
        self.bgc = self.base[int(H * 0.5), int(W * 0.5)].copy()

    def _keepout(self, i, shot):
        if shot['type'] == 'macro' and self.macro_ok[i]: return None
        P = shot_pose(shot, float(shot['dur']) * 0.5, self.prod, self.W, self.H, self.pal, self.macro_ok[i])
        pw = self.prod.PW * P['scale'] * 1.18 + 2 * self.prod.T * P['scale']; ph = self.prod.PH * P['scale'] * 1.04
        cx = self.W * (0.5 + P['dx']); base = self.floor_y + 10 + self.H * P['dy']
        return (cx - pw / 2, base - ph, cx + pw / 2, base + 0.03 * self.H)

    # shots get exact timing: voice lines fix a minimum, the rest is scaled to the requested length
    def _fit_timeline(self):
        shots = [dict(s) for s in self.spec['shots']]; target = float(self.spec.get('duration', 30)); xf = 0.6
        for s in shots: s['dur'] = float(s.get('dur', 4))
        mins = [2.4] * len(shots)
        for v in self.spec.get('voice') or []:
            k = int(v['shot']) - 1
            if 0 <= k < len(shots): mins[k] = max(mins[k], float(v.get('start', 0.6)) + float(v['dur']) + 0.7)
        n = len(shots); need = target + xf * (n - 1)       # sum of shot lengths so that the film lasts `target`
        for _ in range(12):
            tot = sum(s['dur'] for s in shots)
            if abs(tot - need) < 0.02: break
            free = [i for i in range(n) if shots[i]['dur'] > mins[i] + 1e-6 or tot < need]
            if tot > need:
                room = sum(shots[i]['dur'] - mins[i] for i in range(n));
                if room <= 1e-6: break
                f = min(1.0, (tot - need) / room)
                for i in range(n): shots[i]['dur'] -= (shots[i]['dur'] - mins[i]) * f
            else:
                f = need / tot
                for i in range(n): shots[i]['dur'] = max(mins[i], shots[i]['dur'] * f)
        self.xf = xf
        self.starts = []; t = 0.0
        for s in shots: self.starts.append(t); t += s['dur'] - xf
        self.total = t + xf
        if abs(self.total - target) > 1.5: self.qa.append({'check': 'duration', 'status': 'WARN', 'detail': f'asked for {target:.0f}s, voice needs made it {self.total:.1f}s'})
        return shots

    def _bg_lum(self, frame):
        def fn(rect):
            x0, y0, x1, y1 = [int(v) for v in rect]; x0 = max(0, x0); y0 = max(0, y0); x1 = min(self.W, x1); y1 = min(self.H, y1)
            if x1 <= x0 or y1 <= y0: return 0.2
            return lum(frame[y0:y1:4, x0:x1:4].reshape(-1, 3).mean(0))
        return fn

    def shot_frame(self, k, t):
        W, H = self.W, self.H; shot = self.shots[k]; P = shot_pose(shot, t, self.prod, W, H, self.pal, self.macro_ok[k])
        img = self.base.copy()
        light = self.style == 'light_studio'
        if not light: img += self.glow[..., None] * self.tint * (P['glow'] * 1.0)
        blocks = self.texts[k]
        macro = P['macro'] is not None
        if macro:
            box = shot.get('macro_box') or [0.15, 0.30, 0.85, 0.75]
            plate, up = macro_plate(self.prod, box, P['macro']['u'], W, H, self.bgc)
            img = plate * 1.0
            if up > 1.15: img = img + 0.5 * (img - cv2.GaussianBlur(img, (0, 0), 1.2))      # a little sharpening for the zoom
            self.up_max = max(getattr(self, 'up_max', 0), up)
        # text behind the product
        beh = [b for b in blocks if b.zone == 'behind']
        for b in beh: b.draw(img, t, self._bg_lum(img))
        cx = W * (0.5 + P['dx']); base = self.floor_y + 10 + H * P['dy']
        ys = None; rect = None; a_prod = None
        if not macro:
            rgb, a, rect = self.prod.render(P['yaw'], cx, base, P['scale'])
            rgb = np.where(rgb > 205, 205 + (rgb - 205) * 0.45, rgb)      # highlight shoulder: a white screen stays bright but never burns out
            x0, y0, x1, y1 = rect; a = a * P['fade']
            # shadow + reflection
            sh = np.zeros((H, W), np.float32)
            cv2.ellipse(sh, (int(cx), int(base + 4)), (int(self.prod.PW * P['scale'] * (0.46 * abs(math.cos(math.radians(P['yaw']))) + 0.12 * abs(math.sin(math.radians(P['yaw'])))) + 40), max(8, int(H * 0.013))), 0, 0, 360, 1, -1)
            sh = cv2.GaussianBlur(sh, (0, 0), max(6, H * 0.02))
            img *= (1 - (0.55 if light else 0.75) * sh * P['fade'])[..., None]
            by = int(min(H - 1, base)); ph = y1 - y0
            if not light or True:
                n = min(int(ph * 0.55), H - by)
                if n > 4:
                    seg = rgb[max(0, by - n):by][::-1]; sa = a[max(0, by - n):by][::-1]
                    fade = (np.linspace(1, 0, seg.shape[0]) ** 2.0 * (0.20 if light else 0.28))[:, None]
                    seg = cv2.GaussianBlur(seg, (0, 0), 2.5); sa = cv2.GaussianBlur(sa, (0, 0), 2.5)
                    wgt = (sa * fade)[..., None]; hh = seg.shape[0]
                    img[by:by + hh] = img[by:by + hh] * (1 - wgt) + seg * wgt
            img = img * (1 - a[..., None]) + rgb * a[..., None]
            a_prod = a
        # light sweep across the product (or across the macro plate)
        if P['sweep'] and P['sweep'][0] <= t <= P['sweep'][1]:
            uu = (t - P['sweep'][0]) / (P['sweep'][1] - P['sweep'][0])
            pos = -0.3 + 1.6 * ease_io(uu)
            if macro: band = sweep_band(self.xx, self.yy, 0, W, H / 2, H, pos); img += (band * 0.30)[..., None] * 255 * 0.55
            elif a_prod is not None:
                band = sweep_band(self.xx, self.yy, rect[0], rect[2], (rect[1] + rect[3]) / 2, rect[3] - rect[1], pos)
                img += (band * a_prod * 0.50)[..., None] * 255 * 0.55
        # dust, bloom
        if not light:
            layer = np.zeros((H, W), np.float32)
            for (px, py, z, ph) in self.dust:
                x = (px + t * (6 + 14 * z) * W / 1280 + 20 * math.sin(t * 0.4 + ph)) % W; y = (py - t * (4 + 10 * z) * H / 720) % H
                r = (1.0 + 3.2 * z) * H / 720; al = 0.10 + 0.28 * z * (0.6 + 0.4 * math.sin(t * 1.3 + ph))
                cv2.circle(layer, (int(x), int(y)), int(math.ceil(r)), al, -1)
            layer = cv2.GaussianBlur(layer, (0, 0), 1.6 * H / 720)
            img += layer[..., None] * np.array([235, 240, 255], np.float32) * 0.8
        small = cv2.resize(np.clip(img - (175 if not light else 215), 0, 255), (W // 4, H // 4), interpolation=cv2.INTER_AREA)
        img += cv2.resize(cv2.GaussianBlur(small, (0, 0), 6), (W, H), interpolation=cv2.INTER_CUBIC) * (0.55 if not light else 0.25)
        # camera push
        if not macro:
            kk = P['cam']; Mx = cv2.getRotationMatrix2D((W * .5, H * .52), 0, kk)
            img = cv2.warpAffine(img, Mx, (W, H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
        # depth of field feel: soft edges
        bl = cv2.resize(cv2.GaussianBlur(cv2.resize(img, (W // 3, H // 3), interpolation=cv2.INTER_AREA), (0, 0), 3), (W, H), interpolation=cv2.INTER_LINEAR)
        w_ = (self.edge * 0.60)[..., None]; img = img * (1 - w_) + bl * w_
        # text in front
        for b in blocks:
            if b.zone != 'behind': b.draw(img, t, self._bg_lum(img))
        return img

    def grade(self, img, f):
        img = np.clip(img, 0, 255); img = 255 * np.power(img / 255, 0.96 if self.style != 'light_studio' else 1.0)
        img *= self.vg[..., None]
        img += self.rng.randn(self.H, self.W, 1).astype(np.float32) * 2.8
        return np.clip(img * f, 0, 255).astype(np.uint8)

    def frame_at(self, T):
        xf = self.xf; k = 0
        for i, s0 in enumerate(self.starts):
            if T >= s0 - 1e-9: k = i
        t = max(0.0, T - self.starts[k]); img = self.shot_frame(k, min(t, self.shots[k]['dur']))
        if k > 0 and T < self.starts[k - 1] + self.shots[k - 1]['dur']:      # the tail of the shot before: dissolve into this one
            tp = T - self.starts[k - 1]; w = ease_io((T - self.starts[k]) / xf)
            img = self.shot_frame(k - 1, min(tp, self.shots[k - 1]['dur'])) * (1 - w) + img * w
        f = ease_io(T / 0.9) * ease_io((self.total - T) / 0.9)
        return self.grade(img, f)

    def render(self, out, frames=None, qa_path=None, sheet_path=None):
        W, H, fps = self.W, self.H, self.fps; n = int(round(self.total * fps)) if frames is None else frames
        voice = self.spec.get('voice') or []
        vf = None
        if voice:
            vf = out + '.vo.wav'; self._mix_voice(vf)
        cmd = ['ffmpeg', '-y', '-loglevel', 'error', '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-s', f'{W}x{H}', '-r', str(fps), '-i', '-']
        if vf: cmd += ['-i', vf]
        cmd += ['-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '16', '-preset', 'medium', '-movflags', '+faststart']
        if vf: cmd += ['-c:a', 'aac', '-b:a', '192k', '-shortest']
        else: cmd += ['-an']
        cmd += [out]
        ff = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        sample_at = set(int(n * q) for q in (0.04, 0.16, 0.30, 0.45, 0.60, 0.75, 0.88, 0.96)); samples = []; stats = []
        for i in range(n):
            fr = self.frame_at(i / fps); ff.stdin.write(fr.tobytes())
            if i in sample_at:
                samples.append(cv2.resize(fr, (W // 3, H // 3)))
            if i % 6 == 0:
                g = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY); stats.append((i / fps, float(g.mean()), float((g > 250).mean())))
        ff.stdin.close(); ff.wait()
        self._qa_frames(stats, n / fps)
        if sheet_path and samples:
            rows = []; cols = 4 if W > H else 4
            for r in range(0, len(samples), cols):
                row = samples[r:r + cols]
                while len(row) < cols: row.append(np.zeros_like(samples[0]))
                rows.append(np.hstack(row))
            cv2.imwrite(sheet_path, np.vstack(rows))
        if qa_path: json.dump(self.qa, open(qa_path, 'w'), indent=1)
        return self.qa

    def _mix_voice(self, wav):
        voice = self.spec['voice']; inputs = []; flt = []
        for i, v in enumerate(voice):
            inputs += ['-i', v['file']]; k = int(v['shot']) - 1
            st = self.starts[k] + float(v.get('start', 0.6)); flt.append(f'[{i}:a]adelay={int(st * 1000)}|{int(st * 1000)},volume=1.0[a{i}]')
        mix = ''.join(f'[a{i}]' for i in range(len(voice))) + f'amix=inputs={len(voice)}:normalize=0,loudnorm=I=-16:TP=-1.5[out]'
        cmd = ['ffmpeg', '-y', '-loglevel', 'error'] + inputs + ['-filter_complex', ';'.join(flt) + ';' + mix, '-map', '[out]', '-t', f'{self.total:.2f}', '-ar', '44100', wav]
        subprocess.run(cmd, check=True)

    # --- QA pass 2: look at what was really rendered
    def _qa_frames(self, stats, secs):
        mid = [s for s in stats if 1.0 < s[0] < secs - 1.0]
        blown = [s for s in mid if s[2] > 0.04]
        dark = [s for s in mid if s[1] < 6]
        self.qa.append({'check': 'blown_highlights', 'status': 'PASS' if not blown else 'WARN', 'detail': f'{len(blown)} sampled frames with more than 4% pure white'})
        self.qa.append({'check': 'black_frames', 'status': 'PASS' if not dark else 'WARN', 'detail': f'{len(dark)} sampled frames that are almost black'})
        self.qa.append({'check': 'length', 'status': 'PASS' if abs(secs - float(self.spec.get('duration', 30))) <= 1.5 else 'WARN', 'detail': f'{secs:.1f} s rendered, {float(self.spec.get("duration", 30)):.0f} s asked'})
        self.qa.append({'check': 'text_safe_area_and_overlap', 'status': 'PASS' if not any(q.get('check') == 'text_layout' for q in self.qa) else 'WARN', 'detail': 'checked before rendering, every block inside the margin and not touching another'})
        if not self.prod.back_is_real: self.qa.append({'check': 'back_photo', 'status': 'NOTE', 'detail': 'no back photo was sent: the back is a plain panel with the name'})
        if not self.prod.side_real and self.prod.kind != 'flat': self.qa.append({'check': 'side_photo', 'status': 'NOTE', 'detail': 'no side photo: the sides use the colours of the front edges'})
        if self.prod.kind == 'flat': self.qa.append({'check': 'rotation', 'status': 'NOTE', 'detail': 'flat product: it only tilts, a full 360 turn needs back and side photos of a pouch or box'})


def main():
    a = sys.argv[1:]; res = 1.0; frames = None; qa = None; sheet = None
    spec = json.load(open(a[0])); out = a[1]
    if '--res' in a: res = float(a[a.index('--res') + 1])
    if '--frames' in a: frames = int(a[a.index('--frames') + 1])
    if '--qa' in a: qa = a[a.index('--qa') + 1]
    if '--sheet' in a: sheet = a[a.index('--sheet') + 1]
    r = Renderer(spec, res)
    if '--probe' in a:
        for q in a[a.index('--probe') + 1].split(','):
            cv2.imwrite(out.replace('.mp4', f'_{q}.png'), r.frame_at(float(q)))
        print('probed', r.total, [round(x, 2) for x in r.starts], [round(s['dur'], 2) for s in r.shots]); [print(q['status'], q['check'], q['detail']) for q in r.qa]; return
    rep = r.render(out, frames, qa, sheet)
    for q in rep: print(q['status'], q['check'], '-', q['detail'])
    print('done', out, f'{r.total:.1f}s', f'{r.W}x{r.H}')


if __name__ == '__main__':
    main()
