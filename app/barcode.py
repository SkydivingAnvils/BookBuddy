"""Read a book's ISBN from the EAN-13 barcode in a photo."""
import io
import logging
from typing import Optional

from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

try:
    import zxingcpp
except ImportError:  # optional: identification falls back to Claude without it
    zxingcpp = None


def is_valid_isbn13(code: str) -> bool:
    """A book barcode: 13 digits, 978/979 prefix, correct EAN-13 check digit.

    The prefix check rejects library stickers and other product barcodes.
    """
    if len(code) != 13 or not code.isdigit() or code[:3] not in ("978", "979"):
        return False
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(code[:12]))
    return (10 - total % 10) % 10 == int(code[12])


def isbn10_to_13(isbn10: str) -> Optional[str]:
    isbn10 = isbn10.replace("-", "").replace(" ", "").upper()
    if len(isbn10) != 10 or not isbn10[:9].isdigit() or not (isbn10[9].isdigit() or isbn10[9] == "X"):
        return None
    body = "978" + isbn10[:9]
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(body))
    return body + str((10 - total % 10) % 10)


def normalize_isbn(value: str) -> Optional[str]:
    """Return a valid ISBN-13 for an ISBN-10 or ISBN-13 string, else None."""
    digits = (value or "").replace("-", "").replace(" ", "").upper()
    if len(digits) == 10:
        digits = isbn10_to_13(digits) or ""
    return digits if is_valid_isbn13(digits) else None


def read_isbn(image_data: bytes) -> Optional[str]:
    """Find an ISBN barcode in the image. Returns the ISBN-13 or None."""
    if zxingcpp is None:
        return None
    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(image_data))).convert("L")
    except Exception as e:
        logger.warning("Could not open image for barcode scan: %s", e)
        return None

    # Try full size, then smaller: big phone photos sometimes decode better downscaled
    candidates = [img]
    if max(img.size) > 1600:
        scale = 1600 / max(img.size)
        candidates.append(img.resize((int(img.width * scale), int(img.height * scale))))

    formats = zxingcpp.BarcodeFormat.EAN13
    for candidate in candidates:
        try:
            results = zxingcpp.read_barcodes(candidate, formats=formats)
        except Exception as e:
            logger.warning("Barcode scan failed: %s", e)
            return None
        for r in results:
            # The price add-on (EAN-5) is a separate symbol and isn't requested above
            code = (r.text or "").strip()
            if is_valid_isbn13(code):
                return code
    return None
