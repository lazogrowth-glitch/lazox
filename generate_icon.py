"""
Genere icon.ico pour VocalType (utilise par build.bat)
"""
from PIL import Image, ImageDraw
import os

def make_icon(size=64):
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    d   = ImageDraw.Draw(img)
    cx  = size // 2
    fg  = (165, 180, 252, 255)
    mw  = 9

    d.ellipse([0, 0, size, size], fill=(26, 26, 46, 255))
    d.ellipse([cx-mw, 10,      cx+mw, 10+mw*2], fill=fg)
    d.rectangle([cx-mw, 10+mw, cx+mw, 34],       fill=fg)
    d.ellipse([cx-mw, 34-mw,   cx+mw, 34+mw],    fill=fg)
    d.arc([cx-14, 28, cx+14, 48], 0, 180, fill=fg, width=3)
    d.line([cx, 48, cx, 56],     fill=fg, width=3)
    d.line([cx-9, 56, cx+9, 56], fill=fg, width=3)

    return img

if __name__ == '__main__':
    ico = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'icon.ico')
    img = make_icon(64)
    img.save(ico, format='ICO', sizes=[(64, 64), (32, 32), (16, 16)])
    print(f"Icone generee : {ico}")
