"""Generate PWA icon PNGs from icon-master.png using Pillow."""
import os
from PIL import Image

src = os.path.join(os.path.dirname(__file__), 'icon-master.png')
out_dir = os.path.join(os.path.dirname(__file__), 'app', 'static')
os.makedirs(out_dir, exist_ok=True)

master = Image.open(src).convert('RGBA')

for size in [180, 192, 512]:
    icon = master.resize((size, size), Image.LANCZOS)
    path = os.path.join(out_dir, f'icon-{size}.png')
    icon.save(path, 'PNG', optimize=True)
    print(f'  icon-{size}.png')
