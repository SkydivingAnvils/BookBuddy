"""Series tracking: grouping, full series lists, and series suggestions."""
import json
import logging
import os
import re
from collections import Counter
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session, joinedload

from .models import Book, Rating, SeriesCatalog

logger = logging.getLogger(__name__)

MODEL = "claude-opus-5-5"
LIKED = {"love", "like"}


# ---------------------------------------------------------------------------
# Names and ordering
# ---------------------------------------------------------------------------

def series_key(name: str) -> str:
    """Normalise a series name so spelling variants group together.

    "Elephant & Piggie", "elephant and piggie" and "The Elephant and Piggie Series" all match.
    """
    k = (name or "").lower().replace("&", " and ")
    k = re.sub(r"[^a-z0-9 ]", " ", k)
    k = " ".join(k.split())
    k = re.sub(r"^the ", "", k)
    k = re.sub(r" (series|books)$", "", k)
    return k


def title_key(title: str) -> str:
    t = re.sub(r"[^a-z0-9 ]", "", (title or "").lower())
    return re.sub(r"^(the|a|an) ", "", " ".join(t.split()))


def order_value(order) -> Optional[float]:
    try:
        return float(order) if order not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _number(value: float):
    return int(value) if value == int(value) else value


def _sort_key(entry: dict):
    o = order_value(entry["series_order"])
    return (o is None, o or 0, entry["title"].lower())


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------

def get_catalog(db: Session, key: str) -> Optional[dict]:
    row = db.get(SeriesCatalog, key)
    if not row:
        return None
    try:
        books = json.loads(row.books)
    except (TypeError, json.JSONDecodeError):
        books = []
    return {"name": row.name, "books": books, "complete": bool(row.complete),
            "source": row.source, "fetched_at": row.fetched_at.isoformat() if row.fetched_at else None}


def build_overview(db: Session, child_id: Optional[int] = None) -> list:
    """Every series with its books, read state, gaps and what to read or get next.

    Owned and wishlisted books come from the library. When a full series list has been
    looked up, books we don't have are added with status "missing".
    """
    books = (
        db.query(Book)
        .filter(Book.series.isnot(None), Book.series != "")
        .options(joinedload(Book.ratings).joinedload(Rating.child))
        .all()
    )

    groups: dict = {}
    names: dict = {}
    for book in books:
        key = series_key(book.series)
        if not key:
            continue
        names.setdefault(key, Counter())[book.series.strip()] += 1
        child_rating = next((r.rating for r in book.ratings if r.child_id == child_id), None)
        groups.setdefault(key, []).append({
            "id": book.id,
            "title": book.title,
            "author": book.author,
            "cover_url": book.cover_url,
            "series_order": book.series_order,
            "status": book.status or "library",
            "readers": sorted(r.child.name for r in book.ratings if r.child),
            "read": child_rating is not None,
            "child_rating": child_rating,
        })

    result = []
    for key, entries in groups.items():
        catalog = get_catalog(db, key)
        if catalog:
            _add_missing(entries, catalog["books"])
        entries.sort(key=_sort_key)
        result.append(_summarise(key, names[key].most_common(1)[0][0], entries, catalog, child_id))

    # Series with something to do next come first, then alphabetical
    result.sort(key=lambda g: (not (g["next_up"] or g["next_to_get"]), g["series"].lower()))
    return result


def _add_missing(entries: list, catalog_books: list):
    have_orders = {order_value(e["series_order"]) for e in entries} - {None}
    have_titles = {title_key(e["title"]) for e in entries}
    for cb in catalog_books:
        o = order_value(cb.get("order"))
        if (o is not None and o in have_orders) or title_key(cb.get("title", "")) in have_titles:
            continue
        entries.append({
            "id": None, "title": cb.get("title", ""), "author": cb.get("author", ""),
            "cover_url": None, "series_order": cb.get("order") or None, "status": "missing",
            "readers": [], "read": False, "child_rating": None,
        })


def _summarise(key: str, name: str, entries: list, catalog: Optional[dict], child_id: Optional[int]) -> dict:
    owned = [e for e in entries if e["status"] == "library"]
    owned_orders = {order_value(e["series_order"]) for e in owned} - {None}

    if catalog:
        missing = [_number(order_value(e["series_order"])) for e in entries
                   if e["status"] != "library" and order_value(e["series_order"]) is not None]
    else:
        # No full list: gaps between the numbers on the shelf (wishlisted books still count)
        whole = {int(o) for o in owned_orders if o == int(o)}
        missing = [n for n in range(1, max(whole) + 1) if n not in whole] if whole else []

    group = {
        "key": key,
        "series": name,
        "books": entries,
        "owned_count": len(owned),
        "read_count": sum(e["read"] for e in entries),
        "total": len(catalog["books"]) if catalog else None,
        "catalog": {k: catalog[k] for k in ("complete", "source", "fetched_at")} if catalog else None,
        "missing": missing,
        "liked": any(e["child_rating"] in LIKED for e in entries),
        "next_up": None,
        "next_to_get": None,
        "next_to_get_title": None,
        "next_to_get_status": None,
        "finished": False,
    }
    if child_id is None:
        return group

    group["next_up"] = next((e for e in owned if not e["read"]), None)
    read_orders = [order_value(e["series_order"]) for e in entries if e["read"]]
    read_orders = [o for o in read_orders if o is not None]
    if group["next_up"] or not read_orders:
        return group

    furthest = max(read_orders)
    if catalog:
        nxt = next((e for e in entries if e["status"] != "library"
                    and (order_value(e["series_order"]) or 0) > furthest), None)
        if nxt:
            group["next_to_get"] = _number(order_value(nxt["series_order"]))
            group["next_to_get_title"] = nxt["title"]
            group["next_to_get_status"] = nxt["status"]
        elif catalog["complete"]:
            group["finished"] = True
    else:
        candidate = int(furthest) + 1
        while candidate in owned_orders:
            candidate += 1
        group["next_to_get"] = candidate
        wished = next((e for e in entries if order_value(e["series_order"]) == candidate), None)
        if wished:
            group["next_to_get_title"] = wished["title"]
            group["next_to_get_status"] = wished["status"]
    return group


def next_in_series(db: Session, child_id: int) -> list:
    """Series the child liked, with the next book to read or get."""
    items = []
    for g in build_overview(db, child_id):
        if not g["liked"]:
            continue
        if g["next_up"]:
            b = g["next_up"]
            items.append({"series": g["series"], "kind": "owned", "book_id": b["id"],
                          "title": b["title"], "author": b["author"], "cover_url": b["cover_url"],
                          "series_order": b["series_order"]})
        elif g["next_to_get"] is not None:
            items.append({"series": g["series"], "kind": g["next_to_get_status"] or "missing",
                          "book_id": None, "title": g["next_to_get_title"], "author": None,
                          "cover_url": None, "series_order": str(g["next_to_get"])})
    return items


# ---------------------------------------------------------------------------
# Claude lookups
# ---------------------------------------------------------------------------

def _claude_json(prompt: str, schema: dict, max_tokens: int = 4096) -> Optional[dict]:
    """One structured-output request. Returns parsed JSON, or None on refusal or error."""
    if not os.getenv("ANTHROPIC_API_KEY"):
        return None
    import anthropic

    try:
        client = anthropic.Anthropic()
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIStatusError as e:
        logger.error("Claude series request failed (%s): %s", e.status_code, e.message)
        return None
    except anthropic.APIConnectionError as e:
        logger.error("Claude series request connection error: %s", e)
        return None

    if response.stop_reason in ("refusal", "max_tokens"):
        logger.warning("Claude series request stopped: %s", response.stop_reason)
        return None
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        logger.error("Claude series response was not JSON")
        return None


_CATALOG_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "complete": {"type": "boolean"},
        "books": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "order": {"type": "string"},
                    "title": {"type": "string"},
                    "author": {"type": "string"},
                },
                "required": ["order", "title", "author"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["found", "complete", "books"],
    "additionalProperties": False,
}


def lookup_series_books(name: str, examples: list) -> Optional[dict]:
    """Ask Claude for every book in a series, in order. None if unavailable."""
    example_lines = "\n".join(f'- "{e["title"]}" by {e["author"]}' for e in examples[:5])
    prompt = (
        f'List every book in the children\'s book series "{name}", in reading order.\n'
        f"Books we know are in this series:\n{example_lines}\n\n"
        "Use the series these books belong to. Include only the main numbered series, not "
        "spin-offs, box sets, or companion books. 'order' is the book's number in the series "
        "as a string (e.g. \"1\", \"2\", \"10\"). Set found to false and books to [] if you don't "
        "know this series. Set complete to false if you're not sure the list is complete or the "
        "series is still being published."
    )
    data = _claude_json(prompt, _CATALOG_SCHEMA, max_tokens=8000)
    if not data or not data.get("found"):
        return None
    books = [b for b in data.get("books", []) if b.get("title")]
    return {"books": books, "complete": bool(data.get("complete"))} if books else None


def save_catalog(db: Session, key: str, name: str, found: dict, source: str = "claude"):
    row = db.get(SeriesCatalog, key) or SeriesCatalog(key=key)
    row.name = name
    row.books = json.dumps(found["books"])
    row.complete = found["complete"]
    row.source = source
    row.fetched_at = datetime.utcnow()
    db.merge(row)
    db.commit()


_SUGGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "books": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "series": {"type": "string"},
                    "series_order": {"type": "string"},
                },
                "required": ["id", "series", "series_order"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["books"],
    "additionalProperties": False,
}


def suggest_series(books: list, known_series: list) -> Optional[list]:
    """Ask Claude which of these books belong to a series. None if unavailable."""
    lines = "\n".join(f'{b["id"]}: "{b["title"]}" by {b["author"]}' for b in books)
    known = ", ".join(f'"{s}"' for s in known_series) or "none yet"
    prompt = (
        "These children's books have no series recorded. For each one that belongs to a "
        "published book series, give the series name and its number in that series.\n\n"
        f"{lines}\n\n"
        f"Series names already used in this library: {known}. If a book belongs to one of "
        "these, use that exact name.\n"
        "Leave out books that aren't part of a series and any you're unsure about. "
        "Use series_order \"\" when the series has no numbering."
    )
    data = _claude_json(prompt, _SUGGEST_SCHEMA)
    if data is None:
        return None
    by_id = {b["id"]: b for b in books}
    return [s for s in data.get("books", []) if s.get("id") in by_id and s.get("series", "").strip()]
