import io
import os
import sys

import pytest
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

zxingcpp = pytest.importorskip("zxingcpp")

from app import barcode  # noqa: E402


def _photo_with_barcode(code: str, rotate: int = 0) -> bytes:
    """A phone-photo-sized JPEG with an EAN-13 barcode somewhere on a grey page."""
    symbol = zxingcpp.create_barcode(code, zxingcpp.BarcodeFormat.EAN13)
    bar = Image.fromarray(zxingcpp.write_barcode_to_image(symbol, scale=3)).convert("RGB")
    page = Image.new("RGB", (1400, 1800), (215, 210, 200))
    page.paste(bar, (700, 1300))
    if rotate:
        page = page.rotate(rotate, expand=True, fillcolor=(215, 210, 200))
    out = io.BytesIO()
    page.save(out, "JPEG", quality=82)  # matches the app's client-side compression
    return out.getvalue()


def _plain_photo() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (800, 1000), (90, 140, 200)).save(out, "JPEG")
    return out.getvalue()


# --- ISBN helpers ----------------------------------------------------------

def test_isbn13_validation():
    assert barcode.is_valid_isbn13("9780064440219")
    assert not barcode.is_valid_isbn13("9780064440218")   # bad check digit
    assert not barcode.is_valid_isbn13("4006381333931")   # valid EAN-13, not a book
    assert not barcode.is_valid_isbn13("97800644402")


def test_normalize_isbn():
    assert barcode.normalize_isbn("0-06-444021-4") == "9780064440219"   # ISBN-10 → 13
    assert barcode.normalize_isbn("978-0-06-444021-9") == "9780064440219"
    assert barcode.normalize_isbn("043942089X") == "9780439420891"     # X check digit
    assert barcode.normalize_isbn("not an isbn") is None
    assert barcode.normalize_isbn("") is None


# --- Reading barcodes from photos -------------------------------------------

def test_reads_isbn_from_photo():
    assert barcode.read_isbn(_photo_with_barcode("9780064440219")) == "9780064440219"


def test_reads_rotated_barcode():
    assert barcode.read_isbn(_photo_with_barcode("9780439420891", rotate=90)) == "9780439420891"


def test_ignores_non_book_barcodes():
    # e.g. a library sticker or grocery barcode
    assert barcode.read_isbn(_photo_with_barcode("4006381333931")) is None


def test_no_barcode_or_bad_image():
    assert barcode.read_isbn(_plain_photo()) is None
    assert barcode.read_isbn(b"definitely not an image") is None


# --- /api/identify --------------------------------------------------------

@pytest.fixture
def app_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("BACKUP_INTERVAL_HOURS", "0")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    for mod in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
        del sys.modules[mod]
    from app import main

    def no_claude(*a, **k):
        raise AssertionError("Claude should not be called when the barcode is found")

    monkeypatch.setattr(main, "identify_book", no_claude)
    return main


@pytest.fixture
def client(app_mod):
    from fastapi.testclient import TestClient
    with TestClient(app_mod.app) as c:
        yield c


def _identify(client, image: bytes):
    return client.post("/api/identify", files={"image": ("cover.jpg", image, "image/jpeg")})


def test_identify_uses_barcode_without_claude(client, app_mod, monkeypatch):
    monkeypatch.setattr(app_mod, "fetch_by_isbn",
                        lambda isbn: {"title": "Frog and Toad Together", "author": "Arnold Lobel", "isbn": isbn})
    r = _identify(client, _photo_with_barcode("9780064440219"))
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "barcode"
    assert body["isbn"] == "9780064440219"
    assert body["title"] == "Frog and Toad Together"
    assert body["above_threshold"] is True


def test_identify_barcode_not_in_any_source(client, app_mod, monkeypatch):
    monkeypatch.setattr(app_mod, "fetch_by_isbn", lambda isbn: None)
    body = _identify(client, _photo_with_barcode("9780064440219")).json()
    assert body["isbn"] == "9780064440219"
    assert body["title"] == ""
    assert body["above_threshold"] is False


def test_identify_falls_back_to_claude_with_isbn(client, app_mod, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(app_mod, "fetch_by_isbn", lambda isbn: None)
    monkeypatch.setattr(app_mod, "identify_book",
                        lambda data, media: {"title": "Cover Title", "author": "A", "confidence": 0.9})
    body = _identify(client, _photo_with_barcode("9780064440219")).json()
    assert body["source"] == "cover"
    assert body["title"] == "Cover Title"
    assert body["isbn"] == "9780064440219"


def test_identify_cover_without_key_still_errors(client):
    assert _identify(client, _plain_photo()).status_code == 500


def test_metadata_prefers_isbn_lookup(client, app_mod, monkeypatch):
    monkeypatch.setattr(app_mod, "fetch_by_isbn", lambda isbn: {"title": "By ISBN", "isbn": isbn})
    monkeypatch.setattr(app_mod, "fetch_book_metadata", lambda *a, **k: {"title": "By title"})
    r = client.get("/api/books/metadata", params={"title": "x", "isbn": "0-06-444021-4"})
    assert r.json() == {"title": "By ISBN", "isbn": "9780064440219"}


def test_duplicate_detection_by_isbn(client):
    child = client.post("/api/children", json={"name": "A", "birthday": "2018-01-01"}).json()["id"]
    first = client.post("/api/books/submit", json={
        "title": "Frog and Toad Together", "author": "Arnold Lobel", "isbn": "9780064440219",
        "ratings": [{"child_id": child, "rating": "love"}],
    }).json()["book_id"]

    # Same ISBN, title written differently by another source
    r = client.get("/api/books/check-duplicate", params={"isbn": "0064440214", "title": "Frog & Toad"})
    assert r.json()["book_id"] == first

    r = client.post("/api/books/submit", json={
        "title": "Frog & Toad Together", "author": "Lobel", "isbn": "9780064440219",
        "ratings": [{"child_id": child, "rating": "like"}],
    })
    assert r.json() == {"duplicate": True, "book_id": first, "title": "Frog and Toad Together"}
