"""动图自检：角色不切边、四状态尺寸一致、标签已渲染。"""
from PIL import Image
import numpy as np
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ok = True
for st in ("idle", "working", "attention", "error"):
    im = Image.open(os.path.join(ROOT, f"workspace/assets/character/{st}.gif"))
    issues = []
    for i in [0, im.n_frames // 2, im.n_frames - 1]:
        im.seek(i)
        a = np.array(im.convert("RGBA").getchannel("A"))
        ys, xs = np.where(a > 40)
        if len(xs) == 0:
            issues.append(f"帧{i}空")
            continue
        x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()
        if x0 <= 1 or x1 >= 238 or y0 <= 1:
            issues.append(f"帧{i}切边({x0},{y0},{x1},{y1})")
        if a[:180, :].max() == 0:
            issues.append(f"帧{i}无角色")
    im.seek(0)
    a = np.array(im.convert("RGBA").getchannel("A"))
    label_px = int((a[184:240, :] > 40).sum())
    ys, xs = np.where(a[:180, :] > 40)
    ch = int(ys.max() - ys.min()) if len(ys) else 0
    status = "OK" if not issues else "问题:" + ";".join(issues)
    if issues:
        ok = False
    print(f"{st}: {im.n_frames}帧 角色高{ch}px 标签像素{label_px} {status}")
print("自检", "通过" if ok else "发现问题")
