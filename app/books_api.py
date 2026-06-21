import json
import os
import logging
from typing import Optional
import httpx

logger = logging.getLogger(__name__)

GOOGLE_BOOKS_BASE = "https://www.googleapis.com/books/v1/volumes"
OPEN_LIBRARY_SEARCH = "https://openlibrary.org/search.json"
HARDCOVER_GQL = "https://api.hardcover.app/v1/graphql"
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


def _parse_ol_series(series_list: list):
    """Parse Open Library series list like ['Harry Potter #3', 'Harry Potter'] into (name, order)."""
    import re
    for entry in (series_list or []):
        m = re.search(r'#\s*(\d+(?:\.\d+)?)', entry)
        if m:
            name = entry[:m.start()].strip().rstrip('#').strip()
            return (name or None), m.group(1)
    # No order found — return first entry as series name only
    if series_list:
        return series_list[0].strip() or None, None
    return None, None


def _parse_series_from_subtitle(subtitle: str):
    """Extract series name and order from a subtitle like 'Harry Potter, Book 3'."""
    import re
    if not subtitle:
        return None, None
    # Patterns: "Book 3", "#3", "Volume 3", "Vol. 3", "Part 3"
    m = re.search(r'(?:book|#|volume|vol\.?|part)\s*(\d+(?:\.\d+)?)', subtitle, re.IGNORECASE)
    if m:
        order = m.group(1)
        # Series name is everything before the matched pattern (strip trailing comma/space)
        series = subtitle[:m.start()].strip().rstrip(',').strip()
        return (series or None), order
    return None, None


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
    # Google Books subtitle sometimes contains series info e.g. "Harry Potter, Book 1"
    subtitle = info.get("subtitle", "")
    series, series_order = _parse_series_from_subtitle(subtitle)
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
        "series": series,
        "series_order": series_order,
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
                ol_series = doc.get("series", [])
                series_name, series_order = _parse_ol_series(ol_series)
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
                    "series": series_name,
                    "series_order": series_order,
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
                ol_series = doc.get("series", [])
                series_name, series_order = _parse_ol_series(ol_series)
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
                    "series": series_name,
                    "series_order": series_order,
                }
    except Exception as e:
        logger.error("Open Library metadata fetch error: %s", e)

    return None


# ---------------------------------------------------------------------------
# Explicit single-source fetchers (used by the source selector)
# ---------------------------------------------------------------------------

def fetch_google_books_metadata(title: str, author: str = "") -> Optional[dict]:
    query = f"intitle:{title}"
    if author:
        query += f"+inauthor:{author}"
    params = {"q": query, "maxResults": 1, **_google_params()}
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            r = client.get(GOOGLE_BOOKS_BASE, params=params)
            r.raise_for_status()
            items = r.json().get("items", [])
            return _extract_google_volume(items[0]) if items else None
    except Exception as e:
        logger.error("Google Books explicit fetch error: %s", e)
        return None


def fetch_openlibrary_metadata(title: str, author: str = "") -> Optional[dict]:
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            r = client.get(OPEN_LIBRARY_SEARCH, params={"title": title, "author": author, "limit": 1})
            r.raise_for_status()
            docs = r.json().get("docs", [])
            if not docs:
                return None
            doc = docs[0]
            cover_id = doc.get("cover_i")
            cover_url = f"https://covers.openlibrary.org/b/id/{cover_id}-M.jpg" if cover_id else ""
            isbn_list = doc.get("isbn", [])
            isbn = next((i for i in isbn_list if len(i) == 13), isbn_list[0] if isbn_list else "")
            ol_authors = doc.get("author_name", [])
            ol_genres = doc.get("subject", [])[:5]
            ol_series = doc.get("series", [])
            series_name, series_order = _parse_ol_series(ol_series)
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
                "series": series_name,
                "series_order": series_order,
            }
    except Exception as e:
        logger.error("Open Library explicit fetch error: %s", e)
        return None


# ---------------------------------------------------------------------------
# Hardcover (GraphQL)
# ---------------------------------------------------------------------------

_HARDCOVER_SEARCH_GQL = """
query SearchBooks($query: String!, $limit: Int!) {
  search(query: $query, query_type: "Book", per_page: $limit) {
    results
  }
}
"""


def _parse_hardcover_doc(doc: dict, title: str = "", author: str = "") -> dict:
    authors = doc.get("author_names") or []
    isbns = doc.get("isbns") or []
    image = doc.get("image") or {}
    release_year = doc.get("release_year")
    featured = doc.get("featured_series") or {}
    series_info = featured.get("series") or {}
    series = series_info.get("name") or None
    position = featured.get("position")
    series_order = str(int(position)) if position and position == int(position) else str(position) if position else None
    return {
        "google_books_id": "",
        "title": doc.get("title") or title,
        "author": ", ".join(authors[:2]) if authors else author,
        "isbn": isbns[0] if isbns else "",
        "cover_url": image.get("url") or "",
        "description": doc.get("description") or "",
        "published_date": str(release_year) if release_year and release_year > 1000 else "",
        "page_count": doc.get("pages"),
        "genres": doc.get("genres") or [],
        "series": series,
        "series_order": series_order,
        "reading_level": None,
    }


def search_hardcover(query: str, limit: int = 10) -> list:
    api_key = os.getenv("HARDCOVER_API_KEY")
    if not api_key:
        return []
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            r = client.post(
                HARDCOVER_GQL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={"query": _HARDCOVER_SEARCH_GQL, "variables": {"query": query, "limit": limit}},
            )
            r.raise_for_status()
            data = r.json()
            if "errors" in data:
                logger.error("Hardcover GraphQL errors: %s", data["errors"])
                return []
            hits = (data.get("data", {}).get("search", {}).get("results", {}).get("hits") or [])
            return [_parse_hardcover_doc(h["document"]) for h in hits if h.get("document")]
    except Exception as e:
        logger.error("Hardcover search error: %s", e)
        return []


def fetch_hardcover_metadata(title: str, author: str = "") -> Optional[dict]:
    results = search_hardcover(title, limit=5)
    if not results:
        return None
    if author:
        author_lower = author.lower()
        for r in results:
            if author_lower in r.get("author", "").lower():
                return r
    return results[0]


# ---------------------------------------------------------------------------
# Claude Haiku fallback (uses training knowledge — no ISBN, no cover)
# ---------------------------------------------------------------------------

def fetch_metadata_from_claude(title: str, author: str) -> Optional[dict]:
    try:
        import anthropic
    except ImportError:
        return None
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    prompt = (
        f'Return metadata for the children\'s book "{title}" by {author} as JSON.\n'
        "Fields: title (string), author (string), description (string, 2-3 sentences), "
        "published_date (YYYY or null), page_count (integer or null), "
        "genres (array of short tag strings like ['adventure', 'friendship']), "
        "series (string or null — the series name if part of one), "
        "series_order (string or null — e.g. '1', '2', '3.5'). "
        "IMPORTANT: Do NOT include an isbn field — omit it entirely. "
        "Only include values you are confident about. "
        "Return only the JSON object, no other text."
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            parts = text.split("```")
            text = parts[1].lstrip("json").strip() if len(parts) > 1 else text
        data = json.loads(text)
        return {
            "google_books_id": "",
            "title": data.get("title") or title,
            "author": data.get("author") or author,
            "isbn": "",
            "cover_url": "",
            "description": data.get("description") or "",
            "published_date": str(data.get("published_date") or ""),
            "page_count": data.get("page_count"),
            "genres": data.get("genres") or [],
            "series": data.get("series") or None,
            "series_order": str(data["series_order"]) if data.get("series_order") else None,
            "reading_level": None,
        }
    except Exception as e:
        logger.error("Claude Haiku metadata fallback error: %s", e)
        return None
