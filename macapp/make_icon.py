#!/usr/bin/env python3
"""Overnight Upscaler app icon — a big film-stack glyph on a dark glass tile."""
from PIL import Image, ImageDraw, ImageFilter

S = 1024
PAD, R = 92, 200


def blank():
    return Image.new("RGBA", (S, S), (0, 0, 0, 0))


def vgrad(top, bot):
    col = Image.new("RGBA", (1, S))
    for y in range(S):
        t = y / (S - 1)
        col.putpixel((0, y), tuple(int(a + (b - a) * t) for a, b in zip(top, bot)) + (255,))
    return col.resize((S, S))


# dark glass tile
tile = Image.new("L", (S, S), 0)
ImageDraw.Draw(tile).rounded_rectangle([PAD, PAD, S - PAD, S - PAD], radius=R, fill=255)
icon = blank()
icon.paste(vgrad((42, 49, 66), (14, 17, 24)), (0, 0), tile)

# subtle top sheen + rim light (keeps a hint of glass)
sc = Image.new("RGBA", (1, S), (255, 255, 255, 0))
for y in range(S):
    sc.putpixel((0, y), (255, 255, 255, int(max(0, 48 * (1 - y / (S * 0.5))))))
icon = Image.alpha_composite(icon, Image.composite(sc.resize((S, S)), blank(), tile))
rim = blank()
ImageDraw.Draw(rim).rounded_rectangle([PAD + 3, PAD + 3, S - PAD - 3, S - PAD - 3],
                                      radius=R - 3, outline=(255, 255, 255, 60), width=3)
icon = Image.alpha_composite(icon, rim)

# big film.stack glyph mask (centered, ~60% of the tile)
gm = Image.new("L", (S, S), 0)
gd = ImageDraw.Draw(gm)
gd.rounded_rectangle([340, 318, 684, 352], radius=15, fill=255)    # stack bar (back)
gd.rounded_rectangle([302, 360, 722, 396], radius=15, fill=255)    # stack bar (mid)
gd.rounded_rectangle([262, 404, 762, 720], radius=58, fill=255)    # film frame
for x in range(322, 726, 60):                                      # perforations (punched)
    gd.rounded_rectangle([x, 428, x + 36, 466], radius=9, fill=0)  # top row
    gd.rounded_rectangle([x, 658, x + 36, 696], radius=9, fill=0)  # bottom row

# soft blue glow for depth on the dark tile
glow = blank(); glow.paste((74, 150, 255, 140), (0, 0), gm)
glow = glow.filter(ImageFilter.GaussianBlur(20))
icon = Image.alpha_composite(icon, glow)

# the glyph: vertical blue gradient through the mask
icon = Image.alpha_composite(icon, Image.composite(vgrad((126, 180, 255), (56, 126, 236)), blank(), gm))

# glassy sheen on the glyph (bright top → fade), clipped to the glyph
gs = Image.new("RGBA", (1, S), (255, 255, 255, 0))
for y in range(S):
    a = 120 if y < 318 else (0 if y > 720 else int(120 * (1 - (y - 318) / (720 - 318))))
    gs.putpixel((0, y), (255, 255, 255, a))
icon = Image.alpha_composite(icon, Image.composite(gs.resize((S, S)), blank(), gm))

icon.save("/tmp/appicon_1024.png")
print("wrote /tmp/appicon_1024.png")
