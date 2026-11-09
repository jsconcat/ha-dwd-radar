"""Synthetic keyframes for the WebGL test page: R = rain (byte 0 dry, else 1+24*log2(rate/0.05)), G = cloud cover."""
import json, math, sys
from pathlib import Path
import numpy as np
from PIL import Image
out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
cfg = json.load(open(sys.argv[2]))
(la0, lo0), (la1, lo1) = cfg["bounds"]
mY = lambda la: math.log(math.tan(math.pi / 4 + la * math.pi / 360))
W = 851; H = round(W * (mY(la1) - mY(la0)) / ((lo1 - lo0) * math.pi / 180))
N = 12
rs = np.random.RandomState(7)
def noise(g):
    v = rs.rand(g + 1, g + 1).astype(np.float32); v[-1] = v[0]; v[:, -1] = v[:, 0]
    def f(x, y):
        x = np.mod(x, g); y = np.mod(y, g); xi = np.minimum(x.astype(int), g - 1); yi = np.minimum(y.astype(int), g - 1)
        fx = x - xi; fy = y - yi; sx = fx * fx * (3 - 2 * fx); sy = fy * fy * (3 - 2 * fy)
        a = v[yi, xi] + (v[yi, xi + 1] - v[yi, xi]) * sx; b = v[yi + 1, xi] + (v[yi + 1, xi + 1] - v[yi + 1, xi]) * sx
        return a + (b - a) * sy
    return f
n1, n2, n3 = noise(64), noise(64), noise(64)
fbm = lambda n, x, y: .5 * n(x, y) + .27 * n(x * 2.03, y * 2.03) + .15 * n(x * 4.1, y * 4.1) + .08 * n(x * 8.3, y * 8.3)
Y, X = np.mgrid[0:H, 0:W].astype(np.float32)
fx = 6 * W / 640 + 3.5 * (H / 2 - Y) / H; fy = np.full_like(Y, -1.6)
r = np.random.RandomState(101)
cells = [dict(x=W * (.12 + .7 * r.rand()), y=H * (.15 + .7 * r.rand()), s=6 + 9 * r.rand(), p=4 + 22 * r.rand(), ph=r.rand() * N) for _ in range(9)]
def enc(rate):
    e = np.where(rate < 0.05, 0, np.clip(np.round(1 + 24 * np.log2(np.maximum(rate, 0.05) / 0.05)), 1, 255))
    return e.astype(np.uint8)
for k in range(N):
    x0 = X - fx * k; y0 = Y - fy * k; u = x0 / 90; v = y0 / 90
    band = np.exp(-(((x0 - W * .38) * .6 + (y0 - H * .5) * .8) / (H * .2)) ** 2)
    rate = 7 * np.maximum(0, fbm(n1, u, v) - .45) ** 1.3 * band * (.8 + .4 * math.sin(k / N * math.pi))
    for c in cells:
        life = max(0.0, math.sin(math.pi * (k + c["ph"]) / N))
        rate += c["p"] * life * np.exp(-((x0 - c["x"]) ** 2 + (y0 - c["y"]) ** 2) / (2 * c["s"] ** 2)) * (.6 + .8 * fbm(n3, u * 3, v * 3))
    cloud = np.clip((fbm(n2, u * .6, v * .6) - .4) * 3.2, 0, 1) + band * .6
    rgba = np.zeros((H, W, 4), np.uint8); rgba[..., 0] = enc(rate); rgba[..., 1] = np.round(np.clip(cloud, 0, 1) * 255); rgba[..., 3] = 255
    Image.fromarray(rgba).save(out / f"k{k:02d}.png", optimize=True)
FW, FH = math.ceil(W / 8), math.ceil(H / 8)
fy2, fx2 = np.mgrid[0:FH, 0:FW].astype(np.float32) * 8 + 4
fl = np.zeros((FH, FW, 4), np.uint8)
fl[..., 0] = np.round(128 + (6 * W / 640 + 3.5 * (H / 2 - fy2) / H) * 4); fl[..., 1] = np.round(128 - 1.6 * 4); fl[..., 3] = 255
Image.fromarray(fl).save(out / "flow.png")
json.dump({"w": W, "h": H, "n": N, "fw": FW, "fh": FH}, open(out / "meta.json", "w"))
print(W, H, sum(p.stat().st_size for p in out.glob("*.png")) // 1024, "KB")
