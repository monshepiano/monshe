#!/usr/bin/env python3
# BM30.3: галерея v3 — 10 КАРДИНАЛЬНО разных пар (орб + строка) в воде LIVE.
# Растеризация numpy+Pillow: мягкие радиальные свечения, блики, дымка.
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1440, 900
OUT = "/home/user/monshe/design/orb-variants3"
F = "/usr/share/fonts/truetype/dejavu/"

def font(sz, bold=False):
    return ImageFont.truetype(F + ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"), sz)

def radial_rgba(w, h, stops, cx=.5, cy=.5):
    """stops: [(t,(r,g,b,a)),...] по нормированной дистанции 0..1+."""
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    dx = (xx - cx * (w - 1)) / (w / 2)
    dy = (yy - cy * (h - 1)) / (h / 2)
    d = np.clip(np.sqrt(dx * dx + dy * dy), 0, 1)
    ts = np.array([s[0] for s in stops], np.float32)
    ch = []
    for c in range(4):
        v = np.array([s[1][c] for s in stops], np.float32)
        ch.append(np.interp(d, ts, v))
    arr = np.stack(ch, -1).astype(np.uint8)
    return Image.fromarray(arr, "RGBA")

def vgrad_rgba(w, h, stops):
    """Вертикальный градиент: stops по y 0..1."""
    yy = (np.mgrid[0:h, 0:w][0].astype(np.float32) / max(h - 1, 1))[:, :, None]
    yy = np.repeat(yy, w, axis=1)[:, :, 0]
    ts = np.array([s[0] for s in stops], np.float32)
    ch = []
    for c in range(4):
        v = np.array([s[1][c] for s in stops], np.float32)
        ch.append(np.interp(yy, ts, v))
    arr = np.stack(ch, -1).astype(np.uint8)
    return Image.fromarray(arr, "RGBA")

def soft(img, layer, xy):
    layer = layer.filter(ImageFilter.GaussianBlur(6))
    img.alpha_composite(layer, xy)

def add_scene_base():
    """Вода LIVE: глубокий градиент + 3 синих дыхания + виньетка."""
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    t = yy / (H - 1)
    base = np.zeros((H, W, 4), np.float32)
    top = np.array([4, 10, 20]); bot = np.array([3, 6, 13])
    for c in range(3):
        base[..., c] = top[c] * (1 - t) + bot[c] * t
    base[..., 3] = 255
    img = Image.fromarray(base.astype(np.uint8), "RGBA")
    for (cx, cy, rw, rh, col, a) in [
        (-.18, -.2, 1.15, .78, (0, 110, 190), 42),
        (1.18, 1.15, 1.0, .72, (46, 80, 200), 36),
        (.32, .38, .7, .55, (0, 140, 210), 22),
    ]:
        gl = radial_rgba(900, 620, [(0, (*col, a)), (.5, (*col, a // 3)), (1, (*col, 0))])
        gl = gl.resize((int(rw * W), int(rh * H)))
        img.alpha_composite(gl, (int(cx * W - gl.width / 2), int(cy * H - gl.height / 2)))
    # виньетка по краям
    dx = (xx - W / 2) / (W / 2); dy = (yy - H / 2) / (H / 2)
    v = np.clip(np.sqrt(dx * dx + dy * dy) - .55, 0, 1) / .85
    dark = np.zeros((H, W, 4), np.uint8); dark[..., 3] = (v * 120).astype(np.uint8)
    img.alpha_composite(Image.fromarray(dark, "RGBA"), (0, 0))
    return img

def draw_bar(img):
    """Панель звонка: 4 кнопки 64px, разлёт 92px, мягкие посадочные площадки."""
    n, size, gap = 4, 64, 92
    total = n * size + (n - 1) * gap
    x0 = (W - total) // 2
    y0 = H - 96
    for i in range(n):
        cx = x0 + i * (size + gap) + size // 2
        cy = y0 + size // 2
        pad = radial_rgba(96, 96, [(0, (120, 200, 255, 16)), (.6, (120, 200, 255, 7)), (1, (120, 200, 255, 0))])
        img.alpha_composite(pad, (cx - 48, cy - 48))
        d = ImageDraw.Draw(img)
        col = (150, 175, 196, 200) if i != 1 else (120, 205, 245, 235)
        lw = 3
        if i == 0:      # камера
            d.rounded_rectangle([cx - 20, cy - 13, cx + 10, cy + 13], 5, outline=col, width=lw)
            d.polygon([(cx + 14, cy - 6), (cx + 24, cy - 12), (cx + 24, cy + 12), (cx + 14, cy + 6)], outline=col, width=lw)
        elif i == 1:    # микрофон
            d.rounded_rectangle([cx - 8, cy - 20, cx + 8, cy - 2], 8, outline=col, width=lw)
            d.arc([cx - 16, cy - 6, cx + 16, cy + 22], 15, 165, fill=col, width=lw)
            d.line([cx, cy + 22, cx, cy + 27], fill=col, width=lw)
            d.line([cx - 7, cy + 27, cx + 7, cy + 27], fill=col, width=lw)
        elif i == 2:    # компьютер
            d.rounded_rectangle([cx - 20, cy - 14, cx + 20, cy + 10], 4, outline=col, width=lw)
            d.line([cx, cy + 10, cx, cy + 20], fill=col, width=lw)
            d.line([cx - 8, cy + 20, cx + 8, cy + 20], fill=col, width=lw)
        else:           # выход
            d.line([cx + 12, cy - 18, cx - 4, cy - 18], fill=col, width=lw)
            d.line([cx - 4, cy - 18, cx - 16, cy - 18], fill=col, width=lw)
            d.line([cx - 16, cy - 18, cx - 16, cy + 18], fill=col, width=lw)
            d.line([cx - 16, cy + 18, cx + 12, cy + 18], fill=col, width=lw)
            d.polygon([(cx - 2, cy - 8), (cx + 14, cy), (cx - 2, cy + 8)], outline=col, width=lw)

def draw_badge(img, num, name, desc):
    d = ImageDraw.Draw(img)
    kick = " ".join(list("ВАРИАНТ %s" % num))
    d.text((36, 30), kick, font=font(15, True), fill=(94, 200, 255, 235))
    d.text((36, 56), name, font=font(31, True), fill=(234, 246, 255, 245))
    # перенос описания
    words, line, y = desc.split(), "", 106
    for wd in words:
        if d.textlength(line + wd, font=font(14)) > 560:
            d.text((36, y), line, font=font(14), fill=(127, 155, 181, 230)); y += 21; line = wd + " "
        else:
            line += wd + " "
    d.text((36, y), line, font=font(14), fill=(127, 155, 181, 230))

def line_base(img, y=742, w=640, h=64, fill=(10, 20, 34, 92), radius=32):
    x0 = (W - w) // 2
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([x0, y, x0 + w, y + h], radius, fill=fill)
    d.text((x0 + 30, y + h // 2 - 12), "Спроси Джарвиса…", font=font(18), fill=(111, 138, 163, 220))
    # кнопка отправки
    cx, cy = x0 + w - 38, y + h // 2
    btn = radial_rgba(92, 92, [(0, (0, 212, 255, 30)), (.55, (0, 212, 255, 16)), (1, (0, 212, 255, 0))])
    img.alpha_composite(btn, (cx - 46, cy - 46))
    d = ImageDraw.Draw(img)
    d.polygon([(cx - 12, cy - 10), (cx + 14, cy), (cx - 12, cy + 10), (cx - 6, cy)],
              fill=(85, 200, 240, 230))
    return x0, y, w, h

# ===================== ВАРИАНТЫ =====================

def v01(img):
    # ТОР: кольцо света с пустым сердцем и искрой
    cx, cy, R = W // 2, 330, 130
    ring = radial_rgba(420, 420, [(.66, (120, 210, 255, 0)), (.76, (120, 210, 255, 110)),
                                  (.84, (80, 180, 255, 40)), (.92, (80, 180, 255, 0))])
    img.alpha_composite(ring, (cx - 210, cy - 210))
    ring2 = radial_rgba(520, 520, [(.74, (90, 190, 255, 0)), (.82, (90, 190, 255, 40)), (.9, (90, 190, 255, 0))])
    ring2 = ring2.filter(ImageFilter.GaussianBlur(7))
    img.alpha_composite(ring2, (cx - 260, cy - 260))
    spark = radial_rgba(120, 120, [(0, (235, 250, 255, 235)), (.25, (140, 215, 255, 110)), (1, (140, 215, 255, 0))])
    img.alpha_composite(spark, (cx - 60, cy - 60))
    x0, y, w, h = line_base(img)
    bar = Image.new("RGBA", (W, 8), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bar)
    bd.rectangle([W // 2 - 220, 2, W // 2 + 220, 5], fill=(120, 205, 255, 120))
    bar = bar.filter(ImageFilter.GaussianBlur(3))
    img.alpha_composite(bar, (0, y - 14))

def v02(img):
    # ЗАНАВЕС: вертикальные полотна авроры
    cx = W // 2
    curts = [(-150, 250, (0, 225, 205), 34), (-60, 300, (90, 160, 255), 40),
             (40, 230, (140, 120, 255), 30), (130, 270, (0, 205, 255), 26)]
    for dx, hh, col, a in curts:
        cur = vgrad_rgba(120, hh, [(0, (*col, 0)), (.45, (*col, a)), (.8, (*col, a // 4)), (1, (*col, 0))])
        cur = cur.filter(ImageFilter.GaussianBlur(16))
        top = 330 - hh // 2 - 30
        img.alpha_composite(cur, (cx + dx - 60, top))
    pool = radial_rgba(560, 160, [(0, (120, 220, 255, 46)), (.6, (120, 220, 255, 14)), (1, (120, 220, 255, 0))])
    img.alpha_composite(pool, (cx - 280, 330 + 130))
    x0, y, w, h = line_base(img, fill=(8, 18, 32, 74), radius=26)
    halo = Image.new("RGBA", (w + 40, h + 40), (0, 0, 0, 0))
    hd = ImageDraw.Draw(halo)
    hd.rounded_rectangle([20, 20, w + 20, h + 20], 28, fill=(90, 160, 255, 22))
    halo = halo.filter(ImageFilter.GaussianBlur(12))
    img.alpha_composite(halo, (x0 - 20, y - 20))

def v03(img):
    # ГЛАЗ: миндаль, радужка, зрачок, лучи
    cx, cy = W // 2, 330
    rays = Image.new("RGBA", (460, 460), (0, 0, 0, 0))
    rd = ImageDraw.Draw(rays)
    for ang in range(0, 360, 30):
        rd.pieslice([30, 30, 430, 430], ang, ang + 12, fill=(180, 230, 255, 26))
    rays = rays.filter(ImageFilter.GaussianBlur(9))
    img.alpha_composite(rays, (cx - 230, cy - 230))
    iris = radial_rgba(300, 300, [(.0, (160, 235, 255, 130)), (.46, (40, 140, 230, 90)),
                                  (.72, (10, 60, 120, 40)), (1, (10, 60, 120, 0))])
    iris.putalpha(iris.split()[3].point(lambda a: min(a, 130)))
    img.alpha_composite(iris, (cx - 150, cy - 150))
    almond = Image.new("RGBA", (520, 300), (0, 0, 0, 0))
    ad = ImageDraw.Draw(almond)
    ad.ellipse([10, 60, 510, 240], outline=(110, 200, 255, 46), width=10)
    almond = almond.filter(ImageFilter.GaussianBlur(8))
    img.alpha_composite(almond, (cx - 260, cy - 150))
    pupil = radial_rgba(150, 150, [(0, (235, 250, 255, 235)), (.4, (120, 200, 250, 90)), (1, (120, 200, 250, 0))])
    img.alpha_composite(pupil, (cx - 75, cy - 75))
    x0, y, w, h = line_base(img, radius=110)
    d = ImageDraw.Draw(img)
    lg = Image.new("RGBA", (w, 6), (0, 0, 0, 0))
    gd = ImageDraw.Draw(lg)
    gd.rectangle([w // 2 - 200, 1, w // 2 + 200, 3], fill=(140, 215, 255, 110))
    lg = lg.filter(ImageFilter.GaussianBlur(2))
    img.alpha_composite(lg, (x0, y + h // 2 - 2))

def v04(img):
    # КРИСТАЛЛ: призма с гранями
    cx, cy, s = W // 2, 330, 120
    gem = Image.new("RGBA", (300, 320), (0, 0, 0, 0))
    gd = ImageDraw.Draw(gem)
    pts = [(150, 6), (288, 104), (232, 306), (68, 306), (12, 104)]
    gd.polygon(pts, fill=(70, 160, 240, 60))
    gd.polygon([(150, 6), (288, 104), (150, 150)], fill=(200, 240, 255, 60))
    gd.polygon([(150, 6), (12, 104), (150, 150)], fill=(120, 210, 255, 34))
    gd.polygon([(12, 104), (150, 150), (68, 306), (12, 104)], fill=(40, 130, 220, 50))
    gd.polygon([(288, 104), (150, 150), (232, 306)], fill=(90, 190, 250, 44))
    gem = gem.filter(ImageFilter.GaussianBlur(2))
    gl = radial_rgba(420, 420, [(0, (110, 200, 255, 60)), (.5, (110, 200, 255, 18)), (1, (110, 200, 255, 0))])
    gl = gl.filter(ImageFilter.GaussianBlur(10))
    img.alpha_composite(gl, (cx - 210, cy - 210))
    img.alpha_composite(gem, (cx - 150, cy - 160))
    base = radial_rgba(300, 90, [(0, (120, 205, 255, 60)), (.6, (120, 205, 255, 16)), (1, (120, 205, 255, 0))])
    img.alpha_composite(base, (cx - 150, cy + 130))
    x0, y, w, h = line_base(img, radius=18)
    d = ImageDraw.Draw(img)
    for rx in (x0 + w // 3, x0 + 2 * w // 3):
        ln = Image.new("RGBA", (4, h), (0, 0, 0, 0))
        ld = ImageDraw.Draw(ln)
        ld.rectangle([1, 10, 2, h - 10], fill=(150, 220, 255, 90))
        ln = ln.filter(ImageFilter.GaussianBlur(2))
        img.alpha_composite(ln, (rx - 2, y))

def v05(img):
    # ВИХРЬ: спиральные рукава
    cx, cy = W // 2, 330
    arm = Image.new("RGBA", (520, 520), (0, 0, 0, 0))
    ad = ImageDraw.Draw(arm)
    for i, a0 in enumerate((0, 140, 262)):
        col = [(120, 210, 255, 90), (90, 190, 250, 70), (150, 220, 255, 56)][i]
        for t in range(0, 90, 3):
            import math
            ang = math.radians(a0 + t * 1.6)
            rr = 30 + t * 1.9
            x, y = 260 + rr * math.cos(ang), 260 + rr * math.sin(ang)
            ad.ellipse([x - 14, y - 14, x + 14, y + 14], fill=(*col[:3], max(6, col[3] - t)))
    arm = arm.filter(ImageFilter.GaussianBlur(13))
    img.alpha_composite(arm, (cx - 260, cy - 260))
    core = radial_rgba(220, 220, [(0, (240, 250, 255, 220)), (.3, (130, 205, 255, 100)), (1, (130, 205, 255, 0))])
    core = core.filter(ImageFilter.GaussianBlur(1))
    img.alpha_composite(core, (cx - 110, cy - 110))
    x0, y, w, h = line_base(img, fill=(8, 18, 32, 74))
    d = ImageDraw.Draw(img)
    for i in range(7):
        px = x0 + 70 + i * ((w - 190) / 6)
        sz = 3 + (2.5 if 1 < i < 6 else 0)
        a = 100 if 1 < i < 6 else 40
        d.ellipse([px - sz, y + h // 2 - sz, px + sz, y + h // 2 + sz], fill=(159, 216, 255, a))

def v06(img):
    # МАЯК: столб света вверх
    cx = W // 2
    beam = Image.new("RGBA", (240, 420), (0, 0, 0, 0))
    bd = ImageDraw.Draw(beam)
    bd.polygon([(70, 20), (170, 20), (216, 410), (24, 410)], fill=(120, 210, 255, 66))
    bd.polygon([(96, 20), (144, 20), (164, 410), (76, 410)], fill=(180, 235, 255, 56))
    beam = beam.filter(ImageFilter.GaussianBlur(14))
    img.alpha_composite(beam, (cx - 120, 140))
    crown = radial_rgba(300, 110, [(0, (230, 248, 255, 130)), (.55, (160, 225, 255, 40)), (1, (160, 225, 255, 0))])
    img.alpha_composite(crown, (cx - 150, 130))
    halo = radial_rgba(360, 360, [(.6, (100, 195, 255, 0)), (.72, (100, 195, 255, 30)), (.84, (100, 195, 255, 0))])
    img.alpha_composite(halo, (cx - 180, 250))
    foot = radial_rgba(360, 110, [(0, (120, 205, 255, 66)), (.6, (120, 205, 255, 18)), (1, (120, 205, 255, 0))])
    img.alpha_composite(foot, (cx - 180, 420))
    x0, y, w, h = line_base(img, radius=16)
    cone = radial_rgba(300, 120, [(0, (140, 215, 255, 40)), (.6, (140, 215, 255, 12)), (1, (140, 215, 255, 0))])
    cone = cone.filter(ImageFilter.GaussianBlur(6))
    img.alpha_composite(cone, (x0 + 60, y - 100))

def v07(img):
    # ЖЕМЧУЖИНА: сфера на луже
    cx, cy = W // 2, 320
    pool = Image.new("RGBA", (520, 190), (0, 0, 0, 0))
    pd = ImageDraw.Draw(pool)
    pd.ellipse([10, 30, 510, 160], fill=(80, 180, 255, 40))
    pd.ellipse([70, 50, 450, 150], fill=(130, 210, 255, 46))
    pool = pool.filter(ImageFilter.GaussianBlur(10))
    img.alpha_composite(pool, (cx - 260, cy + 90))
    pearl = radial_rgba(260, 260, [(.0, (245, 252, 255, 190)), (.4, (160, 225, 255, 90)),
                                   (.7, (50, 130, 210, 46)), (1, (50, 130, 210, 0))], cx=.42, cy=.34)
    img.alpha_composite(pearl, (cx - 130, cy - 130))
    sheen = Image.new("RGBA", (120, 70), (0, 0, 0, 0))
    sd = ImageDraw.Draw(sheen)
    sd.ellipse([10, 10, 110, 60], fill=(255, 255, 255, 120))
    sheen = sheen.filter(ImageFilter.GaussianBlur(10)).rotate(-18, expand=True)
    img.alpha_composite(sheen, (cx - 60, cy - 70))
    caus = radial_rgba(180, 60, [(0, (200, 240, 255, 90)), (.6, (200, 240, 255, 26)), (1, (200, 240, 255, 0))])
    img.alpha_composite(caus, (cx - 90, cy + 78))
    x0, y, w, h = line_base(img, fill=(8, 18, 32, 60), radius=90)
    pool2 = Image.new("RGBA", (w + 60, h + 70), (0, 0, 0, 0))
    pd2 = ImageDraw.Draw(pool2)
    pd2.ellipse([30, 34, w + 30, h + 52], fill=(0, 160, 255, 26))
    pool2 = pool2.filter(ImageFilter.GaussianBlur(12))
    img.alpha_composite(pool2, (x0 - 30, y - 30))

def v08(img):
    # ПОРТАЛ: шестиугольники
    import math
    cx, cy, r = W // 2, 330, 128
    def hexpts(rr, rot=0):
        return [(cx + rr * math.cos(math.radians(a + rot)), cy + rr * math.sin(math.radians(a + rot)))
                for a in range(0, 360, 60)]
    hexg = Image.new("RGBA", (400, 400), (0, 0, 0, 0))
    hd = ImageDraw.Draw(hexg)
    hd.polygon(hexpts(150, 30), fill=(0, 225, 205, 26))
    hd.polygon(hexpts(118, 30), fill=(0, 200, 195, 34))
    hexg = hexg.filter(ImageFilter.GaussianBlur(3))
    img.alpha_composite(hexg, (cx - 200, cy - 200))
    eye = radial_rgba(180, 180, [(0, (240, 255, 252, 210)), (.35, (0, 225, 205, 90)), (1, (0, 225, 205, 0))])
    img.alpha_composite(eye, (cx - 90, cy - 90))
    d = ImageDraw.Draw(img)
    d.polygon(hexpts(150, 30), outline=(0, 225, 205, 60), width=2)
    x0, y, w, h = line_base(img, fill=(6, 20, 26, 84), radius=14)
    d = ImageDraw.Draw(img)
    d.arc([x0 - 26, y + 10, x0 + 6, y + h - 10], 270, 90, fill=(0, 225, 205, 110), width=3)
    d.arc([x0 + w - 6, y + 10, x0 + w + 26, y + h - 10], 90, 270, fill=(0, 225, 205, 110), width=3)

def v09(img):
    # ДУЭТ: две сферы и мост
    cx, cy = W // 2, 330
    big = radial_rgba(300, 300, [(.0, (215, 245, 255, 170)), (.46, (90, 200, 250, 80)), (1, (90, 200, 250, 0))], cx=.44, cy=.38)
    img.alpha_composite(big, (cx - 190, cy - 110))
    small = radial_rgba(170, 170, [(.0, (255, 244, 222, 180)), (.5, (255, 210, 130, 70)), (1, (255, 210, 130, 0))], cx=.46, cy=.4)
    img.alpha_composite(small, (cx + 52, cy + 18))
    bridge = Image.new("RGBA", (120, 16), (0, 0, 0, 0))
    bdd = ImageDraw.Draw(bridge)
    bdd.rectangle([0, 6, 120, 10], fill=(255, 200, 150, 90))
    bridge = bridge.filter(ImageFilter.GaussianBlur(2))
    img.alpha_composite(bridge, (cx - 34, cy + 60))
    orbit = radial_rgba(360, 360, [(.7, (110, 200, 255, 0)), (.8, (110, 200, 255, 20)), (.9, (110, 200, 255, 0))])
    img.alpha_composite(orbit, (cx - 180, cy - 180))
    x0, y, w, h = line_base(img, fill=(8, 18, 32, 78))
    for px, col in ((x0 - 14, (140, 215, 255)), (x0 + w + 14, (255, 214, 140))):
        lamp = radial_rgba(90, 90, [(0, (*col, 220)), (.3, (*col, 90)), (1, (*col, 0))])
        img.alpha_composite(lamp, (px - 45, y + h // 2 - 45))

def v10(img):
    # ПЛАМЯ: живой огонь
    cx = W // 2
    fire = Image.new("RGBA", (260, 420), (0, 0, 0, 0))
    fd = ImageDraw.Draw(fire)
    fd.polygon([(130, 10), (212, 200), (196, 330), (64, 330), (48, 200)], fill=(255, 208, 120, 66))
    fd.polygon([(130, 90), (180, 240), (168, 330), (92, 330), (80, 240)], fill=(255, 240, 200, 60))
    fire = fire.filter(ImageFilter.GaussianBlur(12))
    img.alpha_composite(fire, (cx - 130, 130))
    inner = Image.new("RGBA", (110, 190), (0, 0, 0, 0))
    idd = ImageDraw.Draw(inner)
    idd.ellipse([4, 20, 106, 180], fill=(255, 253, 245, 150))
    idd.ellipse([26, 60, 84, 184], fill=(255, 253, 245, 90))
    inner = inner.filter(ImageFilter.GaussianBlur(8))
    img.alpha_composite(inner, (cx - 55, 240))
    for ex, ey, ea in ((cx - 66, 200, 190), (cx + 58, 176, 160), (cx - 6, 152, 120)):
        emb = radial_rgba(60, 60, [(0, (255, 232, 190, ea)), (.4, (255, 210, 150, ea // 3)), (1, (255, 210, 150, 0))])
        img.alpha_composite(emb, (ex - 30, ey - 30))
    hearth = radial_rgba(420, 120, [(0, (255, 190, 110, 70)), (.6, (255, 190, 110, 20)), (1, (255, 190, 110, 0))])
    img.alpha_composite(hearth, (cx - 210, 420))
    x0, y, w, h = line_base(img, fill=(16, 24, 36, 108), radius=32)
    bar = Image.new("RGBA", (w, 6), (0, 0, 0, 0))
    bd2 = ImageDraw.Draw(bar)
    bd2.rectangle([w // 2 - 200, 2, w // 2 + 200, 4], fill=(255, 224, 170, 130))
    bar = bar.filter(ImageFilter.GaussianBlur(2))
    img.alpha_composite(bar, (x0, y + h - 10))
    d = ImageDraw.Draw(img)
    d.ellipse([x0 + 100, y + h - 14, x0 + 108, y + h - 6], fill=(255, 235, 190, 200))
    d.ellipse([x0 + w - 120, y + h - 12, x0 + w - 113, y + h - 5], fill=(255, 235, 190, 150))

VARIANTS = [
    ("01", "Тор", "Кольцо света с пустой сердцевиной и искрой в центре. Строка — перекладина света над вводом.", v01),
    ("02", "Занавес", "Вертикальные полотна авроры: бирюза, синева, фиолет. Строка — ореол-градиент вокруг ввода.", v02),
    ("03", "Глаз", "Радужка, зрачок и лучи — Джарвис смотрит на тебя. Строка — линза-прищур с линией взгляда.", v03),
    ("04", "Кристалл", "Призма с гранями и преломлением света. Строка — гранёная полоса с двумя рёбрами света.", v04),
    ("05", "Вихрь", "Спиральные рукава света вокруг яркого ядра. Строка — пунктир световых точек до кнопки.", v05),
    ("06", "Маяк", "Столб света, рвущийся вверх, с короной наверху. Строка — ввод с конусом света сверху.", v06),
    ("07", "Жемчужина", "Сфера с глянцевым бликом лежит на луже света. Строка — сама лужа света с ядром-вводом.", v07),
    ("08", "Портал", "Изумрудный шестиугольник с глазом в центре. Строка — две световые скобки вокруг ввода.", v08),
    ("09", "Дуэт", "Холодная и тёплая сферы соединены мостом света. Строка — ввод с двумя огнями по краям.", v09),
    ("10", "Пламя", "Тёплый живой огонь с искрами и углями. Строка — тёплая линия с искрами-точками.", v10),
]

import os
os.makedirs(OUT, exist_ok=True)
for num, name, desc, fn in VARIANTS:
    img = add_scene_base()
    fn(img)
    draw_bar(img)
    draw_badge(img, num, name, desc)
    img.convert("RGB").save(f"{OUT}/v{num}.png", "PNG")
    print("v" + num, name)

# контакт-лист 2×5
CW, CH, GAP, COLS = 420, 262, 14, 2
rows = 5
sheet = Image.new("RGB", (COLS * CW + (COLS + 1) * GAP, rows * CH + (rows + 1) * GAP), (2, 5, 11))
for i, (num, *_rest) in enumerate(VARIANTS):
    tile = Image.open(f"{OUT}/v{num}.png").resize((CW, CH), Image.LANCZOS)
    x = GAP + (i % COLS) * (CW + GAP)
    y = GAP + (i // COLS) * (CH + GAP)
    sheet.paste(tile, (x, y))
sheet.save(f"{OUT}/sheet-all.png", "PNG")
print("sheet-all.png")
