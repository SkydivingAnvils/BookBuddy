import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import books_api  # noqa: E402


def test_extract_ol_doc():
    doc = {
        "title": "Frog and Toad Together",
        "author_name": ["Arnold Lobel"],
        "isbn": ["0064440214", "9780064440219"],
        "cover_i": 42,
        "first_publish_year": 1972,
        "subject": ["Picture books", "Friendship", "Frogs", "Toads"],
        "series": ["Frog and Toad #2"],
    }
    meta = books_api._extract_ol_doc(doc, genre_limit=3)
    assert meta["author"] == "Arnold Lobel"
    assert meta["isbn"] == "9780064440219"
    assert meta["cover_url"] == "https://covers.openlibrary.org/b/id/42-M.jpg"
    assert meta["genres"] == ["Picture books", "Friendship", "Frogs"]
    assert meta["reading_level"] == "picture_book"
    assert (meta["series"], meta["series_order"]) == ("Frog and Toad", "2")


def test_extract_ol_doc_falls_back_to_given_title_author():
    meta = books_api._extract_ol_doc({}, "Title", "Author")
    assert (meta["title"], meta["author"], meta["isbn"], meta["cover_url"]) == ("Title", "Author", "", "")


def test_fetch_book_metadata_falls_back_to_open_library(monkeypatch):
    monkeypatch.setattr(books_api, "fetch_google_books_metadata", lambda t, a="": None)
    monkeypatch.setattr(books_api, "fetch_openlibrary_metadata", lambda t, a="": {"title": t, "src": "ol"})
    assert books_api.fetch_book_metadata("X", "Y") == {"title": "X", "src": "ol"}


def test_fetch_book_metadata_prefers_google(monkeypatch):
    monkeypatch.setattr(books_api, "fetch_google_books_metadata", lambda t, a="": {"src": "google"})
    monkeypatch.setattr(books_api, "fetch_openlibrary_metadata", lambda t, a="": {"src": "ol"})
    assert books_api.fetch_book_metadata("X")["src"] == "google"
