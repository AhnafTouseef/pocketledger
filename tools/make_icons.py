"""Generate PWA icons into pocketledger/static/icons. Run: python tools/make_icons.py (needs Pillow)."""
from pathlib import Path
from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "pocketledger" / "static" / "icons"
BG, GREEN, GREEN_D, INK = (18, 20, 19), (16, 185, 129), (5, 120, 85), (232, 236, 234)


def draw(size, maskable=False, scale=4):
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if maskable:
        d.rectangle([0, 0, s, s], fill=BG)
        inner = 0.60  # keep the mark inside the maskable safe zone
    else:
        d.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.22), fill=BG)
        inner = 0.70
    m = s * (1 - inner) / 2
    w = s * inner
    # card
    d.rounded_rectangle([m, m + w * 0.12, m + w, m + w * 0.88], radius=int(w * 0.14), fill=GREEN)
    # ledger lines
    lx, lw = m + w * 0.16, w * 0.68
    for i, frac in enumerate((1.0, 0.72, 0.46)):
        y = m + w * (0.30 + i * 0.17)
        d.rounded_rectangle([lx, y, lx + lw * frac, y + w * 0.07], radius=int(w * 0.035), fill=BG if i != 2 else INK)
    # coin
    r = w * 0.15
    cx, cy = m + w * 0.80, m + w * 0.80
    d.ellipse([cx - r - w * 0.03, cy - r - w * 0.03, cx + r + w * 0.03, cy + r + w * 0.03], fill=BG)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=INK)
    d.ellipse([cx - r * 0.45, cy - r * 0.45, cx + r * 0.45, cy + r * 0.45], fill=GREEN_D)
    return img.resize((size, size), Image.LANCZOS)


OUT.mkdir(parents=True, exist_ok=True)
draw(192).save(OUT / "icon-192.png")
draw(512).save(OUT / "icon-512.png")
draw(512, maskable=True).save(OUT / "icon-maskable-512.png")
draw(180, maskable=True).save(OUT / "apple-touch-icon.png")
draw(32).save(OUT / "favicon-32.png")
print("icons written to", OUT)
