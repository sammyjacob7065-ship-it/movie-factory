"""Studio core: person / object cutout. Model finds the person, then a guided filter snaps the edge to the real hair and clothes
at FULL resolution, and edge colours are cleaned so there is no halo from the old background."""
import cv2, numpy as np, onnxruntime as ort, os
_H=os.path.dirname(os.path.abspath(__file__))
_S={}
def _sess(name):
    if name not in _S:
        alias={'isnet.onnx':'isnet-general-use.onnx'}
        for p in (os.path.join(_H,name),os.path.join(_H,'models',name),os.path.join(_H,'models',alias.get(name,name))):
            if os.path.exists(p): break
        _S[name]=ort.InferenceSession(p,providers=['CPUExecutionProvider'])
    return _S[name]
def _run(bgr,model,size,mean,std):
    s=_sess(model); im=cv2.cvtColor(cv2.resize(bgr,(size,size),interpolation=cv2.INTER_AREA),cv2.COLOR_BGR2RGB).astype(np.float32)
    im=im/max(im.max(),1e-6); im=(im-np.array(mean,np.float32))/np.array(std,np.float32)
    p=s.run(None,{s.get_inputs()[0].name:im.transpose(2,0,1)[None].astype(np.float32)})[0][0,0]
    p=(p-p.min())/max(p.max()-p.min(),1e-6); return cv2.resize(p,(bgr.shape[1],bgr.shape[0]),interpolation=cv2.INTER_CUBIC).clip(0,1)
def coarse(bgr,quality='still'):
    if quality=='still': return _run(bgr,'isnet.onnx',1024,(0.485,0.456,0.406),(1,1,1))
    return _run(bgr,'u2net_human_seg.onnx',320,(0.485,0.456,0.406),(0.229,0.224,0.225))
def guided(I,p,r,eps):
    I=I.astype(np.float32)/255; p=p.astype(np.float32); k=(2*r+1,2*r+1)
    mI=cv2.boxFilter(I,-1,k); mp=cv2.boxFilter(p,-1,k); cIp=cv2.boxFilter(I*p,-1,k)-mI*mp; vI=cv2.boxFilter(I*I,-1,k)-mI*mI
    a=cIp/(vI+eps); b=mp-a*mI; return (cv2.boxFilter(a,-1,k)*I+cv2.boxFilter(b,-1,k)).clip(0,1)
def refine(bgr,a0):
    H,W=a0.shape; sc=max(H,W)/1000.0
    gray=cv2.cvtColor(bgr,cv2.COLOR_BGR2GRAY)
    # trimap: sure foreground / sure background / uncertain band around the edge
    fg=(a0>0.92).astype(np.uint8); bg=(a0<0.08).astype(np.uint8)
    kb=max(3,int(14*sc))|1
    sure_fg=cv2.erode(fg,np.ones((kb,kb),np.uint8)); sure_bg=cv2.erode(bg,np.ones((kb,kb),np.uint8))
    a=guided(gray,a0,max(2,int(10*sc)),2e-4)
    a=guided(gray,a,max(1,int(3*sc)),1e-5)
    a=np.where(sure_fg>0,1.0,np.where(sure_bg>0,0.0,a)).astype(np.float32)
    a=np.clip((a-0.12)/0.76,0,1); a=a*a*(3-2*a)                    # tighten the soft edge, keep hair detail
    return cv2.GaussianBlur(a,(0,0),0.6*sc)
def decontaminate(bgr,a):
    I=bgr.astype(np.float32); m=(a>0.97).astype(np.float32); k=max(5,int(25*max(a.shape)/1000))|1
    num=cv2.GaussianBlur(I*m[...,None],(k,k),0); den=cv2.GaussianBlur(m,(k,k),0)[...,None]
    Fb=num/np.maximum(den,1e-4); w=np.clip((1-a)*2.2,0,1)[...,None]*(den>1e-3)   # edge pixels take the colour of the person next to them
    return (I*(1-w)+Fb*w).clip(0,255).astype(np.uint8)
def cutout(bgr,quality='still'):
    a=refine(bgr,coarse(bgr,quality)); return decontaminate(bgr,a),a
if __name__=='__main__':
    import sys,time
    img=cv2.imread(sys.argv[1]); t=time.time(); F,a=cutout(img,sys.argv[2] if len(sys.argv)>2 else 'still'); print('sec',round(time.time()-t,1))
    cv2.imwrite('alpha.png',(a*255).astype(np.uint8))
    for nm,col in [('dark',(14,12,16)),('red',(40,30,200)),('light',(230,226,220))]:
        bg=np.zeros_like(img); bg[:]=col; out=(F*a[...,None]+bg*(1-a[...,None])).astype(np.uint8); cv2.imwrite(f'comp_{nm}.jpg',out)


# ---------------------------------------------------------------------------------------------------------------
# Studio-photo cut-out: a product shot on a smooth backdrop. The backdrop is found by growing in from the picture border and
# stopping at every edge, so a white screen inside a black frame stays part of the product. No model needed.
def backdrop_cutout(bgr):
    H, W = bgr.shape[:2]; sc = max(H, W) / 1000.0
    sm = cv2.GaussianBlur(bgr, (0, 0), max(1.0, 1.4 * sc))
    gray = cv2.cvtColor(sm, cv2.COLOR_BGR2GRAY)
    ed = cv2.Canny(gray, 14, 42); ed = cv2.dilate(ed, np.ones((3, 3), np.uint8), iterations=max(1, int(round(sc))))
    mask = np.zeros((H + 2, W + 2), np.uint8); mask[1:-1, 1:-1] = (ed > 0).astype(np.uint8)
    filled = np.zeros((H, W), np.uint8)
    step = max(8, int(24 * sc)); tol = (3, 3, 3)
    pts = [(x, 0) for x in range(0, W, step)] + [(x, H - 1) for x in range(0, W, step)] + [(0, y) for y in range(0, H, step)] + [(W - 1, y) for y in range(0, H, step)]
    work = sm.copy()
    for (x, y) in pts:
        if mask[y + 1, x + 1] or filled[y, x]: continue
        m2 = mask.copy()
        cv2.floodFill(work, m2, (x, y), (0, 0, 0), tol, tol, cv2.FLOODFILL_MASK_ONLY | (255 << 8))
        newly = (m2[1:-1, 1:-1] == 255)
        filled[newly] = 1
        mask[1:-1, 1:-1] = np.maximum(mask[1:-1, 1:-1], np.where(newly, 1, 0).astype(np.uint8))
    bg = filled > 0
    fg = (~bg).astype(np.uint8)
    # the dilated edge line around the product belongs to the product: grow the foreground back by the barrier width
    fg = cv2.dilate(fg, np.ones((3, 3), np.uint8), iterations=max(1, int(round(sc))))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(fg, 8)
    if n > 1:
        k = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA])); fg = (lab == k).astype(np.uint8)
    # fill holes
    inv = (1 - fg).astype(np.uint8); ff = inv.copy(); m3 = np.zeros((H + 2, W + 2), np.uint8); cv2.floodFill(ff, m3, (0, 0), 2)
    fg = np.where(ff == 2, fg, 1).astype(np.uint8)
    a0 = cv2.GaussianBlur(fg.astype(np.float32), (0, 0), 1.0 * sc)
    g = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    a = guided(g, a0, max(2, int(4 * sc)), 1e-4)
    a = np.clip((a - 0.25) / 0.5, 0, 1)
    return a.astype(np.float32)


def _smooth_border(bgr):
    """True when the picture edge is a plain studio backdrop (even colour, soft gradient only)."""
    H, W = bgr.shape[:2]; b = max(4, int(0.02 * max(H, W)))
    g = cv2.cvtColor(cv2.GaussianBlur(bgr, (0, 0), 2), cv2.COLOR_BGR2GRAY)
    band = np.concatenate([g[:b].ravel(), g[-b:].ravel(), g[:, :b].ravel(), g[:, -b:].ravel()]).astype(np.float32)
    lo, hi = np.percentile(band, [5, 95])
    return (hi - lo) < 45


def product_cutout(bgr):
    """Returns (F, alpha, method). A plain studio backdrop uses the edge-growing method (exact, keeps white screens and labels);
    anything busier uses the model. A useless 'whole picture' mask is never returned."""
    why = []
    if _smooth_border(bgr):
        a2 = backdrop_cutout(bgr); cov2 = float((a2 > 0.5).mean())
        if 0.03 <= cov2 <= 0.92 and _plausible(a2): return decontaminate(bgr, a2), a2, 'backdrop'
        why.append('backdrop %.2f' % cov2)
    F, a = cutout(bgr, 'still'); cov = float((a > 0.5).mean())
    if 0.03 <= cov <= 0.90 and _plausible(a): return F, a, 'model'
    why.append('model %.2f' % cov)
    raise RuntimeError('I could not cut the product out of that picture (%s). Send it on a plain, even background, or as a PNG with a transparent background.' % ', '.join(why))


def _plausible(a):
    """A good product mask touches at most 2 of the 4 picture borders heavily."""
    H, W = a.shape; b = int(0.02 * max(H, W)) + 1
    sides = [a[:b].mean(), a[-b:].mean(), a[:, :b].mean(), a[:, -b:].mean()]
    return sum(1 for v in sides if v > 0.6) <= 2
