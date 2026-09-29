import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/test.db")
    # database.py reads DATABASE_URL at import time, so import fresh per test
    for mod in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
        del sys.modules[mod]
    from fastapi.testclient import TestClient
    from app import main

    # Keep CSV import offline
    monkeypatch.setattr(main, "fetch_book_metadata", lambda title, author="": None)
    with TestClient(main.app) as c:
        yield c


def _child(c, name):
    return c.post("/api/children", json={"name": name, "birthday": "2018-01-01"}).json()["id"]


def _book(c, title, child_id, rating="like"):
    r = c.post("/api/books/submit", json={
        "title": title, "author": "Author",
        "ratings": [{"child_id": child_id, "rating": rating}],
    })
    return r.json()["book_id"]


def test_merge_keeps_non_conflicting_ratings(client):
    a, b = _child(client, "A"), _child(client, "B")
    keep = _book(client, "Keep", a, "love")
    gone = _book(client, "Gone", b, "like")

    client.post("/api/admin/merge", json={"keep_id": keep, "delete_id": gone})

    ratings = client.get(f"/api/books/{keep}").json()["ratings"]
    assert {r["child_name"]: r["rating"] for r in ratings.values()} == {"A": "love", "B": "like"}
    assert client.get(f"/api/books/{gone}").status_code == 404


def test_merge_conflict_keeps_existing_rating(client):
    a = _child(client, "A")
    keep = _book(client, "Keep", a, "love")
    gone = _book(client, "Gone", a, "hate")

    client.post("/api/admin/merge", json={"keep_id": keep, "delete_id": gone})

    ratings = client.get(f"/api/books/{keep}").json()["ratings"]
    assert [r["rating"] for r in ratings.values()] == ["love"]


CSV_SHORT_AND_LONG = b"Title,Author,A\nShort Row\nLong Row,Someone,love,extra\n"


def test_csv_preview_handles_short_and_long_rows(client):
    _child(client, "A")
    r = client.post("/api/admin/csv/preview", files={"file": ("x.csv", CSV_SHORT_AND_LONG)})
    assert r.status_code == 200
    rows = {row["title"]: row for row in r.json()["rows"]}
    assert rows["Short Row"]["author"] == ""
    assert rows["Long Row"]["ratings"] == {"A": "love"}


def test_bulk_import_handles_short_and_long_rows(client):
    _child(client, "A")
    r = client.post("/api/books/bulk", files={"file": ("x.csv", CSV_SHORT_AND_LONG)})
    assert r.status_code == 201
    body = r.json()
    assert body["imported"] == 2
    assert body["errors"] == []


def test_duplicate_check_treats_wildcards_literally(client):
    a = _child(client, "A")
    _book(client, "100% Wolf", a)
    r = client.get("/api/books/check-duplicate", params={"title": "1_0% Wolf", "author": "Author"})
    assert r.json()["exists"] is False
    r = client.get("/api/books/check-duplicate", params={"title": "100% wolf", "author": "author"})
    assert r.json()["exists"] is True


def test_csv_import_same_title_different_author_is_new_book(client):
    a = _child(client, "A")
    _book(client, "Frog", a)
    r = client.post("/api/books/bulk", files={"file": ("x.csv", b"Title,Author\nFrog,Someone Else\n")})
    assert r.json()["imported"] == 1
    assert r.json()["duplicates"] == 0


def test_confidence_threshold_setting_is_validated(client):
    assert client.put("/api/settings", json={"key": "confidence_threshold", "value": "abc"}).status_code == 400
    assert client.put("/api/settings", json={"key": "confidence_threshold", "value": "1.5"}).status_code == 400
    assert client.put("/api/settings", json={"key": "confidence_threshold", "value": "0.6"}).status_code == 200
