import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def app_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("BACKUP_INTERVAL_HOURS", "0")
    monkeypatch.setenv("BACKUP_KEEP", "2")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.delenv("BACKUP_DIR", raising=False)
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


def _book(c, title, child_id=None, series=None, order=None, status="library"):
    ratings = [{"child_id": child_id, "rating": "love"}] if child_id else []
    return c.post("/api/books/submit", json={
        "title": title, "author": "Author", "series": series, "series_order": order,
        "status": status, "ratings": ratings, "placeholder": not ratings,
    }).json()["book_id"]


# --- Backups ---------------------------------------------------------------

def test_backup_creates_restorable_copy_and_prunes(client, tmp_path):
    import sqlite3
    _child(client, "Ada")
    names = []
    for _ in range(3):
        r = client.post("/api/backups")
        assert r.status_code == 201
        names.append(r.json()["name"])
        # Timestamps have 1s resolution; make names distinct
        import time; time.sleep(1.05)

    listed = [b["name"] for b in client.get("/api/backups").json()["backups"]]
    assert listed == names[:0:-1]  # newest two kept, newest first

    path = tmp_path / "backups" / listed[0]
    rows = sqlite3.connect(path).execute("SELECT name FROM children").fetchall()
    assert rows == [("Ada",)]

    dl = client.get(f"/api/backups/{listed[0]}")
    assert dl.status_code == 200 and dl.content == path.read_bytes()


def test_backup_download_rejects_other_paths(client):
    assert client.get("/api/backups/..%2Ftest.db").status_code == 404
    assert client.get("/api/backups/test.db").status_code == 404


def test_scheduler_backs_up_when_none_exist(app_mod, tmp_path, monkeypatch):
    from app import backup
    monkeypatch.setenv("BACKUP_INTERVAL_HOURS", "24")
    from app.database import init_db
    init_db()
    scheduler = backup.BackupScheduler()
    scheduler.start()
    try:
        for _ in range(50):
            if backup.list_backups():
                break
            import time; time.sleep(0.05)
    finally:
        scheduler.stop()
    assert len(backup.list_backups()) == 1


# --- Recommendations -------------------------------------------------------

def test_recommendations_exclude_owned_books(client, app_mod, monkeypatch):
    a = _child(client)
    _book(client, "Read Book", a)
    _book(client, "The Owned Book")
    _book(client, "Wished Book", status="wishlist")

    captured = {}

    def fake_recs(name, age, history, owned=None, count=20):
        captured["owned"] = {b["title"] for b in owned}
        captured["count"] = count
        return [
            {"title": "Owned Book", "author": "x", "reason": "r"},      # article stripped
            {"title": "wished book!", "author": "x", "reason": "r"},    # case/punctuation
            {"title": "Read Book", "author": "x", "reason": "r"},
            {"title": "Brand New", "author": "x", "reason": "r"},
            {"title": "Brand New", "author": "x", "reason": "dup"},
        ]

    monkeypatch.setattr(app_mod, "get_recommendations", fake_recs)
    recs = client.get(f"/api/recommendations/{a}").json()

    assert captured["owned"] == {"The Owned Book", "Wished Book"}
    assert captured["count"] > app_mod.REC_COUNT
    assert [r["title"] for r in recs] == ["Brand New"]


def test_recommendations_capped_at_rec_count(client, app_mod, monkeypatch):
    a = _child(client)
    _book(client, "Read Book", a)
    monkeypatch.setattr(app_mod, "get_recommendations", lambda *a, **k: [
        {"title": f"Book {i}", "author": "x", "reason": "r"} for i in range(30)])
    assert len(client.get(f"/api/recommendations/{a}").json()) == app_mod.REC_COUNT


def test_recommendation_prompt_lists_owned_books(monkeypatch):
    from app import claude

    class FakeMessages:
        def create(self, **kwargs):
            FakeMessages.prompt = kwargs["messages"][0]["content"]
            class R:
                stop_reason = "end_turn"
                content = [type("C", (), {"text": "[]"})()]
            return R()

    monkeypatch.setattr(claude, "_client", lambda: type("Cl", (), {"messages": FakeMessages()})())
    claude.get_recommendations("A", 6, [{"title": "T", "author": "U", "rating": "love"}],
                               owned=[{"title": "Owned", "author": "O"}], count=25)
    assert '"Owned" by O' in FakeMessages.prompt
    assert "suggest 25 books" in FakeMessages.prompt


# --- Series ----------------------------------------------------------------

def test_series_gaps_next_up_and_next_to_get(client):
    a, b = _child(client, "A"), _child(client, "B")
    _book(client, "Pig 1", a, series="Piggie", order="1")
    _book(client, "Pig 2", series="piggie ", order="2")        # owned, unread; name varies
    _book(client, "Pig 4", a, series="Piggie", order="4")
    _book(client, "Pig 5", series="Piggie", order="5", status="wishlist")
    _book(client, "Frog 1", b, series="Frog", order="1")
    _book(client, "No series")

    series = {s["series"].lower(): s for s in client.get("/api/series", params={"child_id": a}).json()}
    assert set(series) == {"piggie", "frog"}

    pig = series["piggie"]
    assert [x["title"] for x in pig["books"]] == ["Pig 1", "Pig 2", "Pig 4", "Pig 5"]
    assert pig["missing"] == [3]
    assert pig["owned_count"] == 3 and pig["read_count"] == 2
    assert pig["next_up"]["title"] == "Pig 2"
    assert pig["books"][0]["readers"] == ["A"]

    # Child A hasn't read any Frog, and the only one is owned → next up
    assert series["frog"]["next_up"]["title"] == "Frog 1"

    # Child B: finished the only owned Frog → next to get is #2
    series_b = {s["series"].lower(): s for s in client.get("/api/series", params={"child_id": b}).json()}
    assert series_b["frog"]["next_up"] is None
    assert series_b["frog"]["next_to_get"] == 2


def test_series_next_to_get_counts_wishlisted_as_not_owned(client):
    a = _child(client)
    _book(client, "S1", a, series="S", order="1")
    _book(client, "S2", series="S", order="2", status="wishlist")
    s = client.get("/api/series", params={"child_id": a}).json()[0]
    assert s["next_up"] is None
    assert s["next_to_get"] == 2
