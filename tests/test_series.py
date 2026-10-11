import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def app_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("BACKUP_INTERVAL_HOURS", "0")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    for mod in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
        del sys.modules[mod]
    from app import main
    monkeypatch.setattr(main, "fetch_book_metadata", lambda title, author="": None)
    return main


@pytest.fixture
def client(app_mod):
    from fastapi.testclient import TestClient
    with TestClient(app_mod.app) as c:
        yield c


def _child(c, name="A"):
    return c.post("/api/children", json={"name": name, "birthday": "2018-01-01"}).json()["id"]


def _book(c, title, child_id=None, series=None, order=None, status="library", rating="love", author="Mo Willems"):
    ratings = [{"child_id": child_id, "rating": rating}] if child_id else []
    return c.post("/api/books/submit", json={
        "title": title, "author": author, "series": series, "series_order": order,
        "status": status, "ratings": ratings, "placeholder": not ratings,
    }).json()["book_id"]


PIGGIE = [
    {"order": "1", "title": "Today I Will Fly!", "author": "Mo Willems"},
    {"order": "2", "title": "My Friend Is Sad", "author": "Mo Willems"},
    {"order": "3", "title": "I Am Invited to a Party!", "author": "Mo Willems"},
    {"order": "4", "title": "There Is a Bird on Your Head!", "author": "Mo Willems"},
]


def _with_catalog(client, app_mod, monkeypatch, books, name="Elephant & Piggie", complete=True):
    monkeypatch.setattr(app_mod, "lookup_series_books", lambda n, ex: {"books": books, "complete": complete})
    r = client.post("/api/series/catalog", json={"name": name})
    assert r.status_code == 200, r.text


# --- Name normalisation -----------------------------------------------------

def test_series_key_merges_spelling_variants():
    from app.series import series_key
    keys = {series_key(n) for n in ["Elephant & Piggie", "elephant and piggie",
                                     "The Elephant and Piggie Series", "Elephant & Piggie books"]}
    assert keys == {"elephant and piggie"}
    assert series_key("Frog and Toad") != series_key("Elephant & Piggie")


def test_overview_groups_variants_under_most_common_name(client):
    a = _child(client)
    _book(client, "One", a, series="Elephant & Piggie", order="1")
    _book(client, "Two", series="Elephant & Piggie", order="2")
    _book(client, "Three", series="elephant and piggie", order="3")
    series = client.get("/api/series", params={"child_id": a}).json()
    assert len(series) == 1
    assert series[0]["series"] == "Elephant & Piggie"
    assert [b["title"] for b in series[0]["books"]] == ["One", "Two", "Three"]


# --- Full series lists ------------------------------------------------------

def test_catalog_adds_missing_books_with_titles(client, app_mod, monkeypatch):
    a = _child(client)
    _book(client, "Today I Will Fly!", a, series="Elephant & Piggie", order="1")
    _book(client, "I Am Invited to a Party!", series="Elephant & Piggie", order="3")
    _with_catalog(client, app_mod, monkeypatch, PIGGIE)

    s = client.get("/api/series", params={"child_id": a}).json()[0]
    assert s["total"] == 4
    assert [(b["series_order"], b["status"]) for b in s["books"]] == [
        ("1", "library"), ("2", "missing"), ("3", "library"), ("4", "missing")]
    assert s["missing"] == [2, 4]
    # Owned #3 is unread, so it's next up rather than buying #2
    assert s["next_up"]["title"] == "I Am Invited to a Party!"


def test_catalog_matches_owned_books_by_title_when_unnumbered(client, app_mod, monkeypatch):
    _book(client, "My Friend is Sad", series="Elephant & Piggie")   # no number, different case
    _with_catalog(client, app_mod, monkeypatch, PIGGIE)
    s = client.get("/api/series").json()[0]
    assert sum(b["title"].lower() == "my friend is sad" for b in s["books"]) == 1


def test_next_to_get_uses_catalog_title_and_finished(client, app_mod, monkeypatch):
    a = _child(client)
    _book(client, "Today I Will Fly!", a, series="Elephant & Piggie", order="1")
    _book(client, "My Friend Is Sad", a, series="Elephant & Piggie", order="2")
    _with_catalog(client, app_mod, monkeypatch, PIGGIE[:3])

    s = client.get("/api/series", params={"child_id": a}).json()[0]
    assert (s["next_to_get"], s["next_to_get_title"], s["next_to_get_status"]) == (
        3, "I Am Invited to a Party!", "missing")

    _book(client, "I Am Invited to a Party!", a, series="Elephant & Piggie", order="3")
    s = client.get("/api/series", params={"child_id": a}).json()[0]
    assert s["next_to_get"] is None and s["finished"] is True


def test_incomplete_catalog_never_says_finished(client, app_mod, monkeypatch):
    a = _child(client)
    _book(client, "Today I Will Fly!", a, series="Elephant & Piggie", order="1")
    _with_catalog(client, app_mod, monkeypatch, PIGGIE[:1], complete=False)
    s = client.get("/api/series", params={"child_id": a}).json()[0]
    assert s["finished"] is False


def test_catalog_lookup_errors(client, app_mod, monkeypatch):
    assert client.post("/api/series/catalog", json={"name": "Nope"}).status_code == 404
    _book(client, "One", series="Obscure")
    monkeypatch.setattr(app_mod, "lookup_series_books", lambda n, ex: None)
    assert client.post("/api/series/catalog", json={"name": "Obscure"}).status_code == 404


def test_lookup_series_books_parses_structured_output(monkeypatch):
    from app import series

    monkeypatch.setattr(series, "_claude_json", lambda prompt, schema, max_tokens=4096: {
        "found": True, "complete": True,
        "books": [{"order": "1", "title": "A", "author": "X"}, {"order": "2", "title": "", "author": "X"}],
    })
    assert series.lookup_series_books("S", [{"title": "A", "author": "X"}]) == {
        "books": [{"order": "1", "title": "A", "author": "X"}], "complete": True}

    monkeypatch.setattr(series, "_claude_json", lambda *a, **k: {"found": False, "complete": False, "books": []})
    assert series.lookup_series_books("S", []) is None


# --- One-tap "Want next" ------------------------------------------------------

def test_want_adds_wishlist_book_with_series(client):
    _book(client, "Today I Will Fly!", series="Elephant & Piggie", order="1")
    r = client.post("/api/series/want", json={
        "series": "Elephant & Piggie", "title": "My Friend Is Sad", "author": "Mo Willems", "series_order": "2"})
    assert r.status_code == 201
    book = client.get(f"/api/books/{r.json()['book_id']}").json()
    assert (book["status"], book["series"], book["series_order"]) == ("wishlist", "Elephant & Piggie", "2")

    again = client.post("/api/series/want", json={
        "series": "Elephant & Piggie", "title": "My Friend Is Sad", "author": "Mo Willems"})
    assert again.json()["duplicate"] is True


# --- Series in recommendations -------------------------------------------------

def test_next_in_series_only_for_liked_series(client, app_mod, monkeypatch):
    a = _child(client)
    _book(client, "Today I Will Fly!", a, series="Elephant & Piggie", order="1", rating="love")
    _book(client, "Days with Frog and Toad", a, series="Frog and Toad", order="1", rating="dislike")
    _book(client, "Frog and Toad 2", series="Frog and Toad", order="2")
    _with_catalog(client, app_mod, monkeypatch, PIGGIE)

    items = client.get(f"/api/series/next/{a}").json()
    assert [(i["series"], i["kind"], i["title"]) for i in items] == [
        ("Elephant & Piggie", "missing", "My Friend Is Sad")]
    assert client.get("/api/series/next/999").status_code == 404


def test_next_in_series_prefers_owned_book(client):
    a = _child(client)
    _book(client, "One", a, series="S", order="1", rating="like")
    owned = _book(client, "Two", series="S", order="2")
    items = client.get(f"/api/series/next/{a}").json()
    assert items == [{"series": "S", "kind": "owned", "book_id": owned, "title": "Two",
                      "author": "Mo Willems", "cover_url": None, "series_order": "2"}]


# --- Fixing series data ----------------------------------------------------------

def test_rename_merges_series_and_moves_catalog(client, app_mod, monkeypatch):
    _book(client, "One", series="Piggie", order="1")
    _book(client, "Two", series="Elephant & Piggie", order="2")
    _with_catalog(client, app_mod, monkeypatch, PIGGIE, name="Piggie")

    r = client.post("/api/series/rename", json={"from_name": "Piggie", "to_name": "Elephant & Piggie"})
    assert r.json() == {"updated": 1}
    series = client.get("/api/series").json()
    assert len(series) == 1 and series[0]["series"] == "Elephant & Piggie"
    assert series[0]["total"] == 4   # the looked-up list followed the rename

    assert client.post("/api/series/rename", json={"from_name": "x", "to_name": " "}).status_code == 400


def test_suggest_returns_suggestions_without_saving(client, app_mod, monkeypatch):
    _book(client, "Known", series="Elephant & Piggie", order="1")
    target = _book(client, "We Are in a Book!")
    _book(client, "Not a series book")

    seen = {}

    def fake(books, known):
        seen["titles"] = sorted(b["title"] for b in books)
        seen["known"] = known
        return [{"id": target, "series": "Elephant & Piggie", "series_order": "13"},
                {"id": 99999, "series": "Bogus", "series_order": ""}]

    monkeypatch.setattr(app_mod, "suggest_series", fake)
    body = client.post("/api/series/suggest").json()
    assert seen == {"titles": ["Not a series book", "We Are in a Book!"], "known": ["Elephant & Piggie"]}
    assert body["suggestions"] == [{"book_id": target, "title": "We Are in a Book!", "author": "Mo Willems",
                                    "series": "Elephant & Piggie", "series_order": "13"}]
    assert client.get(f"/api/books/{target}").json()["series"] is None   # not saved until accepted


def test_suggest_without_api_key(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert client.post("/api/series/suggest").status_code == 500
