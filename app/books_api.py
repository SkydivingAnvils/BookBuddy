import os
import logging
from typing import Optional
import httpx

logger = logging.getLogger(__name__)

GOOGLE_BOOKS_BASE = "https://www.googleapis.com/books/v1/volumes"
OPEN_LIBRARY_SEARCH = "https://openlibrary.org/search.json"
TIMEOUT = 10


def _google_params() -> dict:
    key = os.getenv("GOOGLE_BOOKS_API_KEY")
    return {"key": key} if key else {}


def _infer_reading_level(categories: list) -> Optional[str]:
    text = " ".join(c.lower() for c in (categories or []))
    if any(t in text for t in ["picture book", "board book", "concept book", "baby book", "toddler"]):
        return "picture_book"
    if any(t in text for t in ["easy reader", "beginning reader", "early reader", "i can read", "level 1", "level 2"]):
        return "early_reader"
    if any(t in text for t in ["chapter book"]):
        return "chapter_book"
    if any(t in text for t in ["middle grade", "juvenile fiction", "juvenile literature", "children's fiction"]):
        return "middle_grade"
    return None


def _extract_google_volume(item: dict) -> dict:
    info = item.get("volumeInfo", {})

    image_links = info.get("imageLinks", {})
    cover_url = image_links.get("thumbnail") or image_links.get("smallThumbnail") or ""
    if cover_url:
        cover_url = cover_url.replace("http://", "https://")
        # Request a slightly larger thumbnail
        cover_url = cover_url.replace("zoom=1", "zoom=2")

    isbn = ""
    for id_entry in info.get("industryIdentifiers", []):
        if id_entry.get("type") == "ISBN_13":
            isbn = id_entry.get("identifier", "")
            break
    if not isbn:
        for id_entry in info.get("industryIdentifiers", []):
            if id_entry.get("type") == "ISBN_10":
                isbn = id_entry.get("identifier", "")
                break

    authors = info.get("authors", [])
    genres = info.get("categories", [])
    return {
        "google_books_id": item.get("id", ""),
        "title": info.get("title", ""),
        "author": ", ".join(authors) if authors else "",
        "isbn": isbn,
        "cover_url": cover_url,
        "description": info.get("description", ""),
        "published_date": info.get("publishedDate", ""),
        "page_count": info.get("pageCount"),
        "genres": genres,
        "reading_level": _infer_reading_level(genres),
    }


def search_open_library(query: str, limit: int = 10) -> list:
    """Search Open Library by free-text query."""
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            r = client.get(OPEN_LIBRARY_SEARCH, params={"q": query, "limit": limit})
            r.raise_for_status()
            docs = r.json().get("docs", [])
            results = []
            for doc in docs:
                cover_id = doc.get("cover_i")
                cover_url = f"https://covers.openlibrary.org/b/id/{cover_id}-M.jpg" if cover_id else ""
                isbn_list = doc.get("isbn", [])
                isbn = next((i for i in isbn_list if len(i) == 13), isbn_list[0] if isbn_list else "")
                ol_authors = doc.get("author_name", [])
                title = doc.get("title", "")
                if not title:
                    continue
                ol_genres = doc.get("subject", [])[:3]
                results.append({
                    "google_books_id": "",
                    "title": title,
                    "author": ", ".join(ol_authors[:2]) if ol_authors else "",
                    "isbn": isbn,
                    "cover_url": cover_url,
                    "description": "",
                    "published_date": str(doc.get("first_publish_year", "")),
                    "page_count": doc.get("number_of_pages_median"),
                    "genres": ol_genres,
                    "reading_level": _infer_reading_level(ol_genres),
                })
            return results
    except Exception as e:
        logger.error("Open Library search error: %s", e)
        return []


def search_books(query: str, limit: int = 12) -> list:
    """Search Google Books first; supplement with Open Library if results are sparse."""
    results = search_google_books(query, limit=limit)
    if len(results) < 3:
        ol_results = search_open_library(query, limit=limit)
        seen = {(r["title"].lower(), r["author"].lower()) for r in results}
        for r in ol_results:
            key = (r["title"].lower(), r["author"].lower())
            if key not in seen:
                results.append(r)
                seen.add(key)
    return results[:limit]


def search_google_books(query: str, limit: int = 10) -> list:
    params = {"q": query, "maxResults": min(limit, 40), **_google_params()}
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            r = client.get(GOOGLE_BOOKS_BASE, params=params)
            r.raise_for_status()
            return [_extract_google_volume(item) for item in r.json().get("items", [])]
    except Exception as e:
        logger.error("Google Books search error: %s", e)
        return []


def fetch_by_google_id(google_books_id: str) -> Optional[dict]:
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            r = client.get(f"{GOOGLE_BOOKS_BASE}/{google_books_id}", params=_google_params())
            r.raise_for_status()
            return _extract_google_volume(r.json())
    except Exception as e:
        logger.error("Google Books ID fetch error: %s", e)
        return None


def fetch_book_metadata(title: str, author: str = "") -> Optional[dict]:
    # Try Google Books first
    query = f"intitle:{title}"
    if author:
        query += f"+inauthor:{author}"
    params = {"q": query, "maxResults": 1, **_google_params()}
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            r = client.get(GOOGLE_BOOKS_BASE, params=params)
            r.raise_for_status()
            items = r.json().get("items", [])
            if items:
                return _extract_google_volume(items[0])
    except Exception as e:
        logger.error("Google Books metadata fetch error: %s", e)

    # Fall back to Open Library
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            r = client.get(OPEN_LIBRARY_SEARCH, params={"title": title, "author": author, "limit": 1})
            r.raise_for_status()
            docs = r.json().get("docs", [])
            if docs:
                doc = docs[0]
                cover_id = doc.get("cover_i")
                cover_url = f"https://covers.openlibrary.org/b/id/{cover_id}-M.jpg" if cover_id else ""

                isbn_list = doc.get("isbn", [])
                isbn = next((i for i in isbn_list if len(i) == 13), isbn_list[0] if isbn_list else "")

                ol_authors = doc.get("author_name", [])
                ol_genres = doc.get("subject", [])[:5]
                return {
                    "google_books_id": "",
                    "title": doc.get("title", title),
                    "author": ", ".join(ol_authors[:2]) if ol_authors else author,
                    "isbn": isbn,
                    "cover_url": cover_url,
                    "description": "",
                    "published_date": str(doc.get("first_publish_year", "")),
                    "page_count": doc.get("number_of_pages_median"),
                    "genres": ol_genres,
                    "reading_level": _infer_reading_level(ol_genres),
                }
    except Exception as e:
        logger.error("Open Library metadata fetch error: %s", e)

    return None
