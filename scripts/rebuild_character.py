"""素材处理：rembg 抠图 + 去文字碎片（不误伤角色）+ 角色高度归一化 + 底部卡通状态标签。"""
from PIL import Image, ImageDraw, ImageFont
from rembg import remove, new_session
from scipy import ndimage
import numpy as np
import os

SLOW = 1.7
CANVAS = 240
TARGET_H = 158
BASELINE = 178
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONT_PATH = os.path.join(ROOT, "workspace/assets/fonts/ZCOOLKuaiLe.ttf")
LABELS = {"idle": ("待命", (0, 193, 106)), "working": ("working", (56, 189, 248)),
          "attention": ("需要关注", (245, 158, 11)), "error": ("异常", (248, 113, 113))}
sessions = {"u2net": new_session("u2net"), "isnet": new_session("isnet-general-use")}


def clean_text_blobs(f):
    """去掉小的孤立碎片（字幕文字），保留角色主体及其附近的大块内容。"""
    a = np.array(f.getchannel("A"))
    mask = a > 40
    lab, cnt = ndimage.label(mask)
    if cnt <= 1:
        return f
    sizes = ndimage.sum(mask, lab, range(1, cnt + 1))
    largest = int(np.argmax(sizes)) + 1
    slices = ndimage.find_objects(lab, max_label=cnt)
    lb = slices[largest - 1]
    lx0, ly0, lx1, ly1 = lb[1].start, lb[0].start, lb[1].stop, lb[0].stop
    lw, lh = lx1 - lx0, ly1 - ly0
    keep_mask = lab == largest
    for i in range(1, cnt + 1):
        if i == largest:
            continue
        if sizes[i - 1] >= 0.08 * sizes[largest - 1]:
            keep_mask |= (lab == i)      # 大块内容保留（可能是道具/肢体）
            continue
        b = slices[i - 1]
        bh = b[0].stop - b[0].start
        bw = b[1].stop - b[1].start
        # 文字形状（扁长条小块）无论位置一律剔除
        if bh < 16 and bw / max(bh, 1) > 2.2:
            continue
        cx, cy = (b[1].start + b[1].stop) / 2, (b[0].start + b[0].stop) / 2
        # 落在角色包围盒外扩 25% 范围内的保留，远处的碎片（字幕）才剔除
        if (lx0 - lw * 0.25 <= cx <= lx1 + lw * 0.25 and ly0 - lh * 0.25 <= cy <= ly1 + lh * 0.25):
            keep_mask |= (lab == i)
    a[~keep_mask] = 0
    f.putalpha(Image.fromarray(a))
    return f


def make_label(text, color):
    """卡通字标签：白字+彩色描边，返回 RGBA 图。"""
    font = ImageFont.truetype(FONT_PATH, 34)
    tmp = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    tw, th = tmp.textbbox((0, 0), text, font=font, stroke_width=4)[2:]
    img = Image.new("RGBA", (tw + 12, th + 12), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)
    dr.text((6, 4), text, font=font, fill=(255, 255, 255, 255),
            stroke_width=4, stroke_fill=color + (255,))
    return img


def main():
    labels = {st: make_label(t, c) for st, (t, c) in LABELS.items()}
    # 每个素材只抠图一次，可产出多个状态文件（各自贴自己的标签）
    plan = [  # (素材, 模型, [输出状态...])
        ("tmp/src/idle_src.gif", "u2net", ["idle", "working"]),
        ("tmp/src/attention_src.gif", "isnet", ["attention"]),
        ("tmp/src/error_src.gif", "u2net", ["error"]),
    ]
    for src_rel, model, states in plan:
        im = Image.open(os.path.join(ROOT, src_rel))
        n = im.n_frames
        raw = []
        for i in range(n):
            im.seek(i)
            d = max(int(im.info.get("duration", 80) * SLOW), 60)
            f = im.convert("RGBA")
            w, h = f.size
            s = min(w, h)
            f = f.crop(((w - s) // 2, (h - s) // 2, (w + s) // 2, (h + s) // 2))
            kw = dict(session=sessions[model])
            if model == "isnet":
                kw.update(alpha_matting=True, alpha_matting_foreground_threshold=235,
                          alpha_matting_background_threshold=25, alpha_matting_erode_size=8)
            f = clean_text_blobs(remove(f, **kw))
            raw.append((f, d))
        chars, last_c = [], None
        for f, d in raw:
            bb = f.getchannel("A").getbbox()
            c = f.crop(bb) if bb else last_c
            if c is None:
                continue
            last_c = c
            scale = TARGET_H / c.height
            c = c.resize((max(1, int(c.width * scale)), TARGET_H), Image.LANCZOS)
            if c.width > CANVAS - 8:
                c = c.resize((CANVAS - 8, int(c.height * (CANVAS - 8) / c.width)), Image.LANCZOS)
            chars.append((c, d))
        for state in states:
            frames = []
            for c, d in chars:
                canvas = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
                canvas.paste(c, ((CANVAS - c.width) // 2, BASELINE - c.height), c)
                lb = labels[state]
                canvas.paste(lb, ((CANVAS - lb.width) // 2, BASELINE + 6), lb)
                frames.append((canvas, d))
            out = os.path.join(ROOT, "workspace", "assets", "character", state + ".gif")
            frames[0][0].save(out, save_all=True, append_images=[f for f, _ in frames[1:]],
                              duration=[d for _, d in frames], loop=0, disposal=2)
            print(f"{state}: {len(frames)} frames -> {os.path.getsize(out) // 1024}KB", flush=True)
    print("ALL DONE")


if __name__ == "__main__":
    main()
