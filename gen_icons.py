"""Generate solid-color PNG icons for the PWA using only stdlib — no Pillow needed."""
import struct
import zlib
import os

def make_png(size, r, g, b):
    def chunk(tag, data):
        buf = struct.pack('>I', len(data)) + tag + data
        return buf + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff)
    sig  = b'\x89PNG\r\n\x1a\n'
    ihdr = chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 2, 0, 0, 0))
    row  = b'\x00' + bytes([r, g, b]) * size
    idat = chunk(b'IDAT', zlib.compress(row * size, 9))
    iend = chunk(b'IEND', b'')
    return sig + ihdr + idat + iend

out_dir = os.path.join(os.path.dirname(__file__), 'app', 'static')
os.makedirs(out_dir, exist_ok=True)

for size in [180, 192, 512]:
    path = os.path.join(out_dir, f'icon-{size}.png')
    with open(path, 'wb') as f:
        f.write(make_png(size, 27, 94, 59))   # #1B5E3B
    print(f'  icon-{size}.png')
