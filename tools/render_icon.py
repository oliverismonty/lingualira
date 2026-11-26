"""Render tools/icon.svg into the app icons (icon-180/192/512.png). Needs playwright + Pillow.
After changing the icon, bump the ?v= number on the icon links in index.html, reader.html and manifest.webmanifest."""
import os, tempfile
from playwright.sync_api import sync_playwright
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
tmp = os.path.join(tempfile.gettempdir(), 'icon-1024.png')
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={'width': 1024, 'height': 1024})
    pg.goto('file://' + os.path.join(HERE, 'icon.svg'))
    pg.screenshot(path=tmp)
    b.close()
im = Image.open(tmp).convert('RGB')
for n in (180, 192, 512):
    im.resize((n, n), Image.LANCZOS).save(os.path.join(ROOT, f'icon-{n}.png'))
