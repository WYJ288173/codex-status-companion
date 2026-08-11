"""生成 LocalAgent 线条风格图标：程序坞应用图标 + 菜单栏模板图标。
设计：圆角方框 + 心电脉冲线（监控/守护含义），纯线条、可识别。"""
from PIL import Image, ImageDraw
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "workspace", "assets")


def draw_pulse(d, x0, x1, cy, amp, w, color):
    """画一条心电脉冲线：平-小波-尖峰-平。"""
    mid = (x0 + x1) / 2
    pts = [
        (x0, cy),
        (mid - w * 2.6, cy),
        (mid - w * 1.9, cy - amp * 0.35),
        (mid - w * 1.2, cy + amp * 0.25),
        (mid - w * 0.5, cy - amp),
        (mid + w * 0.3, cy + amp * 0.8),
        (mid + w * 1.0, cy - amp * 0.3),
        (mid + w * 1.6, cy),
        (x1, cy),
    ]
    d.line(pts, fill=color, width=int(w * 0.62), joint="curve")
    # 末端圆点（信号点）
    r = w * 0.55
    d.ellipse([x1 - r, cy - r, x1 + r, cy + r], fill=color)


def app_icon(path, size=512):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 深色圆角底
    m = size * 0.04
    d.rounded_rectangle([m, m, size - m, size - m], radius=size * 0.22,
                        fill=(22, 29, 33, 255), outline=(0, 193, 106, 255),
                        width=int(size * 0.02))
    # 内部细圆角框（线条感）
    m2 = size * 0.13
    d.rounded_rectangle([m2, m2, size - m2, size - m2], radius=size * 0.16,
                        outline=(230, 237, 243, 200), width=int(size * 0.016))
    # 脉冲线
    draw_pulse(d, size * 0.2, size * 0.74, size * 0.5, size * 0.17, size * 0.05,
               (0, 193, 106, 255))
    img.save(path)
    print("saved", path, os.path.getsize(path), "bytes")


def menubar_icon(path, size=44):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    black = (0, 0, 0, 255)
    m = size * 0.08
    d.rounded_rectangle([m, m, size - m, size - m], radius=size * 0.24,
                        outline=black, width=max(2, int(size * 0.07)))
    draw_pulse(d, size * 0.2, size * 0.72, size * 0.5, size * 0.18, size * 0.16, black)
    img.save(path)
    print("saved", path, os.path.getsize(path), "bytes")


if __name__ == "__main__":
    app_icon(os.path.join(OUT, "app_icon.png"))
    menubar_icon(os.path.join(OUT, "menubar_icon.png"))
