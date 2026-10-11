import csv
import io
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from . import backup
from .barcode import normalize_isbn, read_isbn
from .series import (
    build_overview, lookup_series_books, next_in_series, save_catalog, series_key, suggest_series,
)
from .books_api import (
    fetch_book_metadata, fetch_by_google_id, fetch_by_isbn, search_books,
    fetch_google_books_metadata, fetch_openlibrary_metadata,
    fetch_hardcover_metadata, fetch_metadata_from_claude,
    search_google_books, search_open_library, search_hardcover,
)
from .claude import get_recommendations, identify_book
from .config import get_setting
from .database import get_db, init_db
from .models import Book, Child, Rating, SeriesCatalog, Setting

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    if not os.getenv("ANTHROPIC_API_KEY"):
        logger.error("ANTHROPIC_API_KEY is not set — book identification and recommendations will fail.")
    scheduler = backup.BackupScheduler()
    scheduler.start()
    yield
    scheduler.stop()


app = FastAPI(title="BookBuddy", docs_url=None, redoc_url=None, lifespan=lifespan)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

REC_COUNT = 20
REC_REQUEST_COUNT = 25     # ask for extra so filtering out owned books still leaves REC_COUNT
REC_OWNED_LIMIT = 300

VALID_RATINGS = {"love", "like", "neutral", "dislike", "hate"}
VALID_READING_LEVELS = {"picture_book", "early_reader", "chapter_book", "middle_grade"}


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class ChildCreate(BaseModel):
    name: str
    birthday: date
    avatar: Optional[str] = None  # base64 data URI, compressed client-side


class ChildUpdate(BaseModel):
    name: str
    birthday: date
    avatar: Optional[str] = None


class RatingItem(BaseModel):
    child_id: int
    rating: str
    date_read: Optional[date] = None
    notes: Optional[str] = None
    read_myself: Optional[bool] = None


class BookSubmit(BaseModel):
    title: str
    author: str
    isbn: Optional[str] = None
    cover_url: Optional[str] = None
    description: Optional[str] = None
    published_date: Optional[str] = None
    page_count: Optional[int] = None
    genres: Optional[List[str]] = None
    tags: Optional[str] = None
    series: Optional[str] = None
    series_order: Optional[str] = None
    reading_level: Optional[str] = None
    google_books_id: Optional[str] = None
    ratings: List[RatingItem] = []
    status: str = "library"
    force_duplicate: bool = False
    placeholder: bool = False   # save book now, ratings will follow
    book_id: Optional[int] = None  # add ratings to a specific existing book


class BookUpdate(BaseModel):
    title: Optional[str] = None
    author: Optional[str] = None
    cover_url: Optional[str] = None
    isbn: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    tags: Optional[str] = None
    series: Optional[str] = None
    series_order: Optional[str] = None
    reading_level: Optional[str] = None


class RatingUpdate(BaseModel):
    rating: Optional[str] = None
    date_read: Optional[date] = None
    notes: Optional[str] = None
    read_myself: Optional[bool] = None


class SettingUpdate(BaseModel):
    key: str
    value: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _age(birthday: date) -> int:
    today = date.today()
    return today.year - birthday.year - ((today.month, today.day) < (birthday.month, birthday.day))


def _child_dict(child: Child) -> dict:
    return {
        "id": child.id,
        "name": child.name,
        "birthday": child.birthday.isoformat(),
        "age": _age(child.birthday),
        "avatar": child.avatar,
        "created_at": child.created_at.isoformat(),
    }


def _book_dict(book: Book, db: Session) -> dict:
    ratings_by_child = {}
    for r in book.ratings:
        child = r.child
        if child:
            ratings_by_child[str(r.child_id)] = {
                "child_id": r.child_id,
                "child_name": child.name,
                "rating": r.rating,
                "rating_id": r.id,
                "date_read": r.date_read.isoformat() if r.date_read else None,
                "notes": r.notes,
                "read_myself": bool(r.read_myself),
            }

    genres: list = []
    if book.genres:
        try:
            genres = json.loads(book.genres)
        except (json.JSONDecodeError, TypeError):
            genres = []

    return {
        "id": book.id,
        "title": book.title,
        "author": book.author,
        "isbn": book.isbn,
        "cover_url": book.cover_url,
        "description": book.description,
        "published_date": book.published_date,
        "page_count": book.page_count,
        "genres": genres,
        "tags": book.tags,
        "series": book.series,
        "series_order": book.series_order,
        "reading_level": book.reading_level,
        "status": book.status or "library",
        "google_books_id": book.google_books_id,
        "created_at": book.created_at.isoformat(),
        "ratings": ratings_by_child,
    }


def _same_book(title: str, author: Optional[str] = None):
    """Case-insensitive exact match on title (and author, if given).

    Uses lower() equality rather than ilike so % and _ in titles aren't wildcards.
    """
    conds = [func.lower(Book.title) == title.strip().lower()]
    if author is not None:
        conds.append(func.lower(Book.author) == author.strip().lower())
    return conds


def _find_by_title_author(db: Session, title: str, author: str) -> Optional[Book]:
    """Match on title+author; fall back to title alone only when no author is known."""
    if author:
        return db.query(Book).filter(*_same_book(title, author)).first()
    return db.query(Book).filter(*_same_book(title)).first()


def _confidence_threshold() -> float:
    try:
        return float(get_setting("confidence_threshold", "0.75"))
    except (TypeError, ValueError):
        return 0.75


def _upsert_ratings(db: Session, book_id: int, ratings: List[RatingItem]):
    for r in ratings:
        existing = (
            db.query(Rating)
            .filter(Rating.book_id == book_id, Rating.child_id == r.child_id)
            .first()
        )
        if existing:
            existing.rating = r.rating
            if r.date_read is not None:
                existing.date_read = r.date_read
            if r.notes is not None:
                existing.notes = r.notes
            if r.read_myself is not None:
                existing.read_myself = r.read_myself
        else:
            db.add(Rating(book_id=book_id, child_id=r.child_id, rating=r.rating,
                          date_read=r.date_read, notes=r.notes,
                          read_myself=r.read_myself or False))


def _title_key(title: str) -> str:
    """Normalise a title for loose matching: case, punctuation and a leading article."""
    t = re.sub(r"[^a-z0-9 ]", "", (title or "").lower())
    t = re.sub(r"^(the|a|an) ", "", " ".join(t.split()))
    return t


def _require_api_key():
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise HTTPException(
            status_code=500,
            detail="ANTHROPIC_API_KEY environment variable is not set. Please configure it and restart.",
        )


# ---------------------------------------------------------------------------
# Children
# ---------------------------------------------------------------------------

@app.get("/api/children")
def list_children(db: Session = Depends(get_db)):
    return [_child_dict(c) for c in db.query(Child).order_by(Child.name).all()]


@app.post("/api/children", status_code=201)
def create_child(data: ChildCreate, db: Session = Depends(get_db)):
    child = Child(name=data.name.strip(), birthday=data.birthday, avatar=data.avatar)
    db.add(child)
    db.commit()
    db.refresh(child)
    return _child_dict(child)


@app.put("/api/children/{child_id}")
def update_child(child_id: int, data: ChildUpdate, db: Session = Depends(get_db)):
    child = db.query(Child).filter(Child.id == child_id).first()
    if not child:
        raise HTTPException(status_code=404, detail="Child not found")
    child.name = data.name.strip()
    child.birthday = data.birthday
    if data.avatar is not None:
        child.avatar = data.avatar
    db.commit()
    db.refresh(child)
    return _child_dict(child)


@app.delete("/api/children/{child_id}")
def delete_child(child_id: int, db: Session = Depends(get_db)):
    child = db.query(Child).filter(Child.id == child_id).first()
    if not child:
        raise HTTPException(status_code=404, detail="Child not found")
    db.delete(child)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Book identification
# ---------------------------------------------------------------------------

@app.post("/api/identify")
async def identify_book_endpoint(image: UploadFile = File(...)):
    media_type = image.content_type or "image/jpeg"
    if media_type not in {"image/jpeg", "image/png", "image/gif", "image/webp"}:
        media_type = "image/jpeg"

    image_data = await image.read()
    if len(image_data) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Image too large. Maximum size is 10 MB.")

    threshold = _confidence_threshold()

    # A barcode gives an exact edition and costs no Claude call, so try it first
    isbn = await run_in_threadpool(read_isbn, image_data)
    if isbn:
        meta = await run_in_threadpool(fetch_by_isbn, isbn)
        if meta:
            return {
                "title": meta.get("title", ""), "author": meta.get("author", ""),
                "confidence": 1.0, "isbn": isbn, "source": "barcode",
                "threshold": threshold, "above_threshold": True,
            }
        if not os.getenv("ANTHROPIC_API_KEY"):
            # Barcode read but no source knows it: let the user search or type it in
            return {
                "title": "", "author": "", "confidence": 0.0, "isbn": isbn, "source": "barcode",
                "threshold": threshold, "above_threshold": False,
            }

    _require_api_key()
    try:
        result = await run_in_threadpool(identify_book, image_data, media_type)
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.error("Identification error: %s", e)
        raise HTTPException(status_code=500, detail="Book identification failed. Please try again.")

    result["threshold"] = threshold
    result["above_threshold"] = float(result.get("confidence", 0)) >= threshold
    result["source"] = "cover"
    if isbn:
        result["isbn"] = isbn
    return result


# ---------------------------------------------------------------------------
# Book metadata & search
# ---------------------------------------------------------------------------

VALID_SEARCH_SOURCES = {"auto", "google", "openlibrary", "hardcover"}

@app.get("/api/books/search")
def search_books_endpoint(q: str, source: str = "auto"):
    if source == "google":
        return search_google_books(q)
    if source == "openlibrary":
        return search_open_library(q)
    if source == "hardcover":
        if not os.getenv("HARDCOVER_API_KEY"):
            raise HTTPException(status_code=400, detail="HARDCOVER_API_KEY is not configured.")
        return search_hardcover(q)
    return search_books(q)  # auto: Google + Open Library


@app.get("/api/books/metadata")
def get_metadata(title: str = "", author: str = "", google_books_id: str = "", source: str = "auto",
                 isbn: str = ""):
    result = None
    isbn = normalize_isbn(isbn) or ""

    if isbn and source == "auto":
        result = fetch_by_isbn(isbn)
        if result:
            return result

    if source == "google":
        if google_books_id:
            result = fetch_by_google_id(google_books_id)
        if not result:
            result = fetch_google_books_metadata(title, author)
    elif source == "openlibrary":
        result = fetch_openlibrary_metadata(title, author)
    elif source == "hardcover":
        if not os.getenv("HARDCOVER_API_KEY"):
            raise HTTPException(status_code=400, detail="HARDCOVER_API_KEY is not configured.")
        result = fetch_hardcover_metadata(title, author)
    elif source == "claude":
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise HTTPException(status_code=400, detail="ANTHROPIC_API_KEY is not configured.")
        result = fetch_metadata_from_claude(title, author)
    else:  # auto
        if google_books_id:
            result = fetch_by_google_id(google_books_id)
        if not result:
            result = fetch_book_metadata(title, author)

    if not result:
        return {
            "google_books_id": "",
            "title": title,
            "author": author,
            "isbn": isbn,
            "cover_url": "",
            "description": "",
            "published_date": "",
            "page_count": None,
            "genres": [],
        }
    return result


@app.get("/api/metadata-sources")
def metadata_sources():
    return {
        "google": True,
        "openlibrary": True,
        "hardcover": bool(os.getenv("HARDCOVER_API_KEY")),
        "claude": bool(os.getenv("ANTHROPIC_API_KEY")),
    }


@app.get("/api/books/check-duplicate")
def check_duplicate(title: str = "", author: str = "", google_books_id: str = "", isbn: str = "",
                    db: Session = Depends(get_db)):
    book = None
    isbn = normalize_isbn(isbn)
    if isbn:
        book = db.query(Book).filter(Book.isbn == isbn).first()
    if not book and google_books_id:
        book = db.query(Book).filter(Book.google_books_id == google_books_id).first()
    if not book and title and author:
        book = (
            db.query(Book)
            .filter(*_same_book(title, author))
            .first()
        )
    if book:
        return {"exists": True, "book_id": book.id, "title": book.title, "author": book.author}
    return {"exists": False, "book_id": None}


# ---------------------------------------------------------------------------
# Book submission
# ---------------------------------------------------------------------------

@app.post("/api/books/submit", status_code=201)
def submit_book(data: BookSubmit, db: Session = Depends(get_db)):
    if data.status not in {"library", "wishlist"}:
        raise HTTPException(status_code=400, detail="Invalid status.")
    # Allow empty ratings for placeholders and book_id-targeted updates
    if data.status == "library" and not data.ratings and not data.placeholder and data.book_id is None:
        raise HTTPException(status_code=400, detail="At least one rating is required.")
    if data.reading_level and data.reading_level not in VALID_READING_LEVELS:
        raise HTTPException(status_code=400, detail="Invalid reading level.")

    for r in data.ratings:
        if r.rating not in VALID_RATINGS:
            raise HTTPException(status_code=400, detail=f"Invalid rating value: {r.rating}")

    # Validate children exist
    for r in data.ratings:
        if not db.query(Child).filter(Child.id == r.child_id).first():
            raise HTTPException(status_code=400, detail=f"Child {r.child_id} not found.")

    # Fast path: attach ratings to a book we already created as a placeholder
    if data.book_id is not None:
        book = db.query(Book).filter(Book.id == data.book_id).first()
        if not book:
            raise HTTPException(status_code=404, detail="Book not found.")
        if data.tags is not None:
            book.tags = data.tags or None
        if data.series:
            book.series = data.series
        if data.series_order:
            book.series_order = data.series_order
        if data.reading_level:
            book.reading_level = data.reading_level
        if data.ratings and book.status == "wishlist":
            book.status = "library"
        _upsert_ratings(db, book.id, data.ratings)
        db.commit()
        return {"ok": True, "book_id": book.id, "duplicate": False}

    # Duplicate detection
    book = None
    isbn = normalize_isbn(data.isbn or "")
    if isbn:
        book = db.query(Book).filter(Book.isbn == isbn).first()
    if not book and data.google_books_id:
        book = db.query(Book).filter(Book.google_books_id == data.google_books_id).first()
    if not book:
        book = (
            db.query(Book)
            .filter(*_same_book(data.title, data.author))
            .first()
        )

    if book and not data.force_duplicate:
        return {"duplicate": True, "book_id": book.id, "title": book.title}

    if not book:
        book = Book(
            title=data.title,
            author=data.author,
            isbn=isbn or data.isbn,
            cover_url=data.cover_url,
            description=data.description,
            published_date=data.published_date,
            page_count=data.page_count,
            genres=json.dumps(data.genres or []),
            tags=data.tags,
            series=data.series,
            series_order=data.series_order,
            reading_level=data.reading_level,
            status=data.status,
            google_books_id=data.google_books_id,
        )
        db.add(book)
        db.flush()
    else:
        if data.tags:
            book.tags = data.tags
        if data.series:
            book.series = data.series
        if data.series_order:
            book.series_order = data.series_order
        if data.reading_level:
            book.reading_level = data.reading_level
        # Promote from wishlist → library when ratings are added
        if data.ratings and book.status == "wishlist":
            book.status = "library"
        book.updated_at = datetime.utcnow()

    _upsert_ratings(db, book.id, data.ratings)

    db.commit()
    return {"ok": True, "book_id": book.id, "duplicate": False}


# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------

@app.get("/api/books")
def list_books(
    child_id: Optional[int] = None,
    rating: Optional[str] = None,
    sort: str = "date_desc",
    search: Optional[str] = None,
    status: str = "library",
    reading_level: Optional[str] = None,
    db: Session = Depends(get_db),
):
    query = db.query(Book).filter(Book.status == status)

    if reading_level:
        query = query.filter(Book.reading_level == reading_level)

    if search:
        pattern = f"%{search}%"
        query = query.filter((Book.title.ilike(pattern)) | (Book.author.ilike(pattern)))

    if child_id or rating:
        query = query.join(Rating)
        if child_id:
            query = query.filter(Rating.child_id == child_id)
        if rating:
            query = query.filter(Rating.rating == rating)
        query = query.distinct()

    sort_map = {
        "date_desc": Book.created_at.desc(),
        "date_asc": Book.created_at.asc(),
        "title_asc": Book.title.asc(),
        "author_asc": Book.author.asc(),
    }
    query = query.order_by(sort_map.get(sort, Book.created_at.desc()))
    query = query.options(joinedload(Book.ratings).joinedload(Rating.child))

    return [_book_dict(b, db) for b in query.all()]


@app.get("/api/books/{book_id}")
def get_book(book_id: int, db: Session = Depends(get_db)):
    book = db.query(Book).filter(Book.id == book_id).first()
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")
    return _book_dict(book, db)


@app.put("/api/books/{book_id}")
def update_book(book_id: int, data: BookUpdate, db: Session = Depends(get_db)):
    book = db.query(Book).filter(Book.id == book_id).first()
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")
    if data.title is not None:
        book.title = data.title
    if data.author is not None:
        book.author = data.author
    if data.cover_url is not None:
        book.cover_url = data.cover_url or None
    if data.isbn is not None:
        book.isbn = data.isbn or None
    if data.description is not None:
        book.description = data.description or None
    if data.status is not None:
        if data.status not in {"library", "wishlist"}:
            raise HTTPException(status_code=400, detail="Invalid status")
        book.status = data.status
    if data.tags is not None:
        book.tags = data.tags
    if data.series is not None:
        book.series = data.series or None
    if data.series_order is not None:
        book.series_order = data.series_order or None
    if data.reading_level is not None:
        if data.reading_level and data.reading_level not in VALID_READING_LEVELS:
            raise HTTPException(status_code=400, detail="Invalid reading level")
        book.reading_level = data.reading_level or None
    book.updated_at = datetime.utcnow()
    db.commit()
    return _book_dict(book, db)


@app.delete("/api/books/{book_id}")
def delete_book(book_id: int, db: Session = Depends(get_db)):
    book = db.query(Book).filter(Book.id == book_id).first()
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")
    db.delete(book)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Ratings
# ---------------------------------------------------------------------------

@app.put("/api/ratings/{rating_id}")
def update_rating(rating_id: int, data: RatingUpdate, db: Session = Depends(get_db)):
    r = db.query(Rating).filter(Rating.id == rating_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Rating not found")
    if data.rating is not None:
        if data.rating not in VALID_RATINGS:
            raise HTTPException(status_code=400, detail="Invalid rating value")
        r.rating = data.rating
    if "date_read" in data.model_fields_set:
        r.date_read = data.date_read
    if "notes" in data.model_fields_set:
        r.notes = data.notes if data.notes else None
    if data.read_myself is not None:
        r.read_myself = data.read_myself
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------

@app.get("/api/recommendations/{child_id}")
def recommendations(child_id: int, db: Session = Depends(get_db)):
    _require_api_key()

    child = db.query(Child).filter(Child.id == child_id).first()
    if not child:
        raise HTTPException(status_code=404, detail="Child not found")

    ratings = (
        db.query(Rating)
        .options(joinedload(Rating.book))
        .filter(Rating.child_id == child_id)
        .all()
    )
    if not ratings:
        raise HTTPException(
            status_code=400,
            detail=f"{child.name} has no reading history yet. Add some books first!",
        )

    history = []
    for r in ratings:
        if r.book:
            history.append({
                "title": r.book.title,
                "author": r.book.author,
                "rating": r.rating,
                "series": r.book.series,
            })

    # Books already in the house (library or wishlist) that this child hasn't rated,
    # so Claude doesn't suggest them. Most recent first, capped to bound prompt size.
    rated_ids = {r.book_id for r in ratings}
    owned = [
        {"title": b.title, "author": b.author}
        for b in db.query(Book).order_by(Book.created_at.desc()).limit(REC_OWNED_LIMIT + len(rated_ids))
        if b.id not in rated_ids
    ][:REC_OWNED_LIMIT]

    age = _age(child.birthday)

    try:
        recs = get_recommendations(child.name, age, history, owned=owned, count=REC_REQUEST_COUNT)
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.error("Recommendations error: %s", e)
        raise HTTPException(status_code=500, detail="Failed to generate recommendations. Please try again.")

    # Claude doesn't always honour the exclusion list, so filter against every book we have
    known = {_title_key(t) for (t,) in db.query(Book.title)}
    seen: set = set()
    filtered = []
    for rec in recs:
        key = _title_key(rec.get("title", ""))
        if key and key not in known and key not in seen:
            seen.add(key)
            filtered.append(rec)
    recs = filtered[:REC_COUNT]

    # Fetch covers concurrently
    def enrich(args):
        idx, rec = args
        meta = fetch_book_metadata(rec.get("title", ""), rec.get("author", ""))
        rec["cover_url"] = meta.get("cover_url", "") if meta else ""
        rec["google_books_id"] = meta.get("google_books_id", "") if meta else ""
        return idx, rec

    enriched = [None] * len(recs)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(enrich, (i, r)) for i, r in enumerate(recs)]
        for future in as_completed(futures):
            idx, rec = future.result()
            enriched[idx] = rec

    return [r for r in enriched if r is not None]


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@app.get("/api/settings")
def get_all_settings(db: Session = Depends(get_db)):
    return {s.key: s.value for s in db.query(Setting).all()}


@app.put("/api/settings")
def update_setting(data: SettingUpdate, db: Session = Depends(get_db)):
    if data.key == "confidence_threshold":
        try:
            value = float(data.value)
        except ValueError:
            raise HTTPException(status_code=400, detail="Confidence threshold must be a number.")
        if not 0.0 <= value <= 1.0:
            raise HTTPException(status_code=400, detail="Confidence threshold must be between 0 and 1.")
    setting = db.query(Setting).filter(Setting.key == data.key).first()
    if setting:
        setting.value = data.value
    else:
        db.add(Setting(key=data.key, value=data.value))
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Bulk import
# ---------------------------------------------------------------------------

@app.post("/api/books/bulk", status_code=201)
async def bulk_import(file: UploadFile = File(...), db: Session = Depends(get_db)):
    content = await file.read()
    try:
        text = content.decode("utf-8-sig")   # handle Excel BOM
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    children = db.query(Child).all()
    child_by_name = {c.name.lower(): c for c in children}

    reader = csv.DictReader(io.StringIO(text))
    rows = [row for row in reader if (row.get("Title") or "").strip()]

    if not rows:
        raise HTTPException(status_code=400, detail="No valid rows found. Make sure the CSV has a 'Title' column header.")
    if len(rows) > 200:
        raise HTTPException(status_code=400, detail="Maximum 200 books per upload.")

    # Fetch metadata concurrently — I/O bound, safe to parallelise
    def fetch_for_row(args):
        idx, row = args
        title = (row.get("Title") or "").strip()
        author = (row.get("Author") or "").strip()
        return idx, (fetch_book_metadata(title, author) if title else None)

    meta_by_idx: dict = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        for future in as_completed([pool.submit(fetch_for_row, (i, r)) for i, r in enumerate(rows)]):
            idx, meta = future.result()
            meta_by_idx[idx] = meta

    imported = 0
    duplicates = 0
    no_meta = 0
    errors: list = []

    for idx, row in enumerate(rows):
        title  = (row.get("Title")  or "").strip()
        author = (row.get("Author") or "").strip()
        series = (row.get("Series") or "").strip() or None
        tags   = (row.get("Tags")   or "").strip() or None
        meta   = meta_by_idx.get(idx)

        try:
            book = _find_by_title_author(db, title, author)

            if book:
                duplicates += 1
                if series and not book.series:
                    book.series = series
                if tags and not book.tags:
                    book.tags = tags
            else:
                if meta:
                    book = Book(
                        title=meta.get("title") or title,
                        author=meta.get("author") or author,
                        isbn=meta.get("isbn"),
                        cover_url=meta.get("cover_url"),
                        description=meta.get("description"),
                        published_date=meta.get("published_date"),
                        page_count=meta.get("page_count"),
                        genres=json.dumps(meta.get("genres") or []),
                        google_books_id=meta.get("google_books_id"),
                        reading_level=meta.get("reading_level"),
                        series=series,
                        tags=tags,
                    )
                else:
                    book = Book(title=title, author=author, series=series, tags=tags)
                    no_meta += 1
                db.add(book)
                db.flush()
                imported += 1

            # Ratings — match column headers to child names (case-insensitive)
            for col, val in row.items():
                # Short rows yield None values; extra fields land under a None key
                if not isinstance(col, str) or not isinstance(val, str):
                    continue
                child = child_by_name.get(col.strip().lower())
                if child and val.strip().lower() in VALID_RATINGS:
                    existing = (
                        db.query(Rating)
                        .filter(Rating.book_id == book.id, Rating.child_id == child.id)
                        .first()
                    )
                    if existing:
                        existing.rating = val.strip().lower()
                    else:
                        db.add(Rating(book_id=book.id, child_id=child.id, rating=val.strip().lower()))

        except Exception as e:
            errors.append(f'"{title}": {e}')
            logger.error("Bulk import error for %s: %s", title, e)

    db.commit()
    return {
        "imported": imported,
        "duplicates": duplicates,
        "no_metadata": no_meta,
        "errors": errors,
        "total": len(rows),
    }


# ---------------------------------------------------------------------------
# Series
# ---------------------------------------------------------------------------

class SeriesName(BaseModel):
    name: str


class SeriesRename(BaseModel):
    from_name: str
    to_name: str


class SeriesWant(BaseModel):
    series: str
    title: str
    author: Optional[str] = ""
    series_order: Optional[str] = None


def _series_books(db: Session, name: str) -> list:
    key = series_key(name)
    return [b for b in db.query(Book).filter(Book.series.isnot(None)).all() if series_key(b.series) == key]


@app.get("/api/series")
def list_series(child_id: Optional[int] = None, db: Session = Depends(get_db)):
    """Every series with read state, gaps, and (per child) what to read or get next."""
    return build_overview(db, child_id)


@app.get("/api/series/next/{child_id}")
def series_next(child_id: int, db: Session = Depends(get_db)):
    """Next book in each series this child liked: one on the shelf, or one to get."""
    if not db.get(Child, child_id):
        raise HTTPException(status_code=404, detail="Child not found")
    return next_in_series(db, child_id)


@app.post("/api/series/catalog")
def lookup_series_catalog(data: SeriesName, db: Session = Depends(get_db)):
    """Look up (or refresh) the full list of books in a series and cache it."""
    _require_api_key()
    books = _series_books(db, data.name)
    if not books:
        raise HTTPException(status_code=404, detail="No books in that series yet")
    examples = [{"title": b.title, "author": b.author} for b in books]
    found = lookup_series_books(data.name.strip(), examples)
    if not found:
        raise HTTPException(status_code=404, detail=f"Couldn't find a book list for \"{data.name}\".")
    save_catalog(db, series_key(data.name), data.name.strip(), found)
    return {"ok": True, "count": len(found["books"]), "complete": found["complete"]}


@app.post("/api/series/rename")
def rename_series(data: SeriesRename, db: Session = Depends(get_db)):
    """Rename a series on every book in it (also merges it into an existing series)."""
    to_name = data.to_name.strip()
    if not to_name:
        raise HTTPException(status_code=400, detail="New series name is required")
    books = _series_books(db, data.from_name)
    for b in books:
        b.series = to_name
        b.updated_at = datetime.utcnow()

    # Keep a looked-up book list with the renamed series, unless the target has its own
    old_key, new_key = series_key(data.from_name), series_key(to_name)
    if old_key != new_key:
        old = db.get(SeriesCatalog, old_key)
        if old:
            if db.get(SeriesCatalog, new_key):
                db.delete(old)
            else:
                db.add(SeriesCatalog(key=new_key, name=to_name, books=old.books,
                                     complete=old.complete, source=old.source, fetched_at=old.fetched_at))
                db.delete(old)
    db.commit()
    return {"updated": len(books)}


@app.post("/api/series/want", status_code=201)
def want_series_book(data: SeriesWant, db: Session = Depends(get_db)):
    """Add a series book to the wishlist, with metadata, in one step."""
    title = data.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")
    existing = _find_by_title_author(db, title, (data.author or "").strip())
    if existing:
        return {"duplicate": True, "book_id": existing.id, "title": existing.title}

    meta = fetch_book_metadata(title, data.author or "") or {}
    book = Book(
        title=meta.get("title") or title,
        author=meta.get("author") or data.author or "",
        isbn=normalize_isbn(meta.get("isbn") or "") or meta.get("isbn") or None,
        cover_url=meta.get("cover_url") or None,
        description=meta.get("description") or None,
        published_date=meta.get("published_date") or None,
        page_count=meta.get("page_count"),
        genres=json.dumps(meta.get("genres") or []),
        google_books_id=meta.get("google_books_id") or None,
        reading_level=meta.get("reading_level"),
        series=data.series.strip(),
        series_order=data.series_order,
        status="wishlist",
    )
    db.add(book)
    db.commit()
    return {"ok": True, "book_id": book.id, "title": book.title, "duplicate": False}


SUGGEST_LIMIT = 40


@app.post("/api/series/suggest")
def suggest_series_endpoint(db: Session = Depends(get_db)):
    """Suggest series name and number for books that have none. Nothing is saved."""
    _require_api_key()
    books = (
        db.query(Book)
        .filter((Book.series.is_(None)) | (Book.series == ""))
        .order_by(Book.created_at.desc())
        .limit(SUGGEST_LIMIT)
        .all()
    )
    if not books:
        return {"suggestions": [], "checked": 0}
    known = sorted({b.series.strip() for b in db.query(Book).filter(Book.series.isnot(None), Book.series != "")})
    found = suggest_series([{"id": b.id, "title": b.title, "author": b.author} for b in books], known)
    if found is None:
        raise HTTPException(status_code=502, detail="Couldn't get suggestions right now. Please try again.")
    by_id = {b.id: b for b in books}
    return {
        "checked": len(books),
        "suggestions": [
            {"book_id": s["id"], "title": by_id[s["id"]].title, "author": by_id[s["id"]].author,
             "series": s["series"].strip(), "series_order": s["series_order"].strip() or None}
            for s in found if s.get("id") in by_id
        ],
    }


# ---------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------

@app.get("/api/backups")
def get_backups():
    return {
        "enabled": backup.interval_hours() > 0,
        "interval_hours": backup.interval_hours(),
        "keep": backup.keep_count(),
        "directory": backup.backup_dir(),
        "backups": backup.list_backups(),
    }


@app.post("/api/backups", status_code=201)
def create_backup():
    try:
        return backup.run_backup()
    except Exception as e:
        logger.error("Manual backup failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Backup failed: {e}")


@app.get("/api/backups/{name}")
def download_backup(name: str):
    path = backup.backup_path(name)
    if not path:
        raise HTTPException(status_code=404, detail="Backup not found")
    return FileResponse(path, media_type="application/octet-stream", filename=name)


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

@app.get("/api/tags")
def get_all_tags(db: Session = Depends(get_db)):
    books = db.query(Book).filter(Book.tags.isnot(None), Book.tags != "").all()
    tag_set: set[str] = set()
    for book in books:
        for tag in book.tags.split(","):
            t = tag.strip().lower()
            if t:
                tag_set.add(t)
    return sorted(tag_set)


# ---------------------------------------------------------------------------
# CSV Export
# ---------------------------------------------------------------------------

@app.get("/api/export/csv")
def export_csv(db: Session = Depends(get_db)):
    children = db.query(Child).order_by(Child.name).all()
    books = (
        db.query(Book)
        .options(joinedload(Book.ratings))
        .order_by(Book.created_at.desc())
        .all()
    )

    # Build rating lookup: (book_id, child_id) → Rating
    rating_index: dict = {}
    for book in books:
        for r in book.ratings:
            rating_index[(book.id, r.child_id)] = r

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        ["Title", "Author", "ISBN", "Genres", "Tags", "Published Date", "Page Count", "Date Logged"]
        + [c.name for c in children]
        + [f"{c.name} Date Read" for c in children]
    )

    for book in books:
        genres_str = ""
        if book.genres:
            try:
                genres_str = ", ".join(json.loads(book.genres))
            except (json.JSONDecodeError, TypeError):
                pass

        row = [
            book.title,
            book.author,
            book.isbn or "",
            genres_str,
            book.tags or "",
            book.published_date or "",
            book.page_count or "",
            book.created_at.strftime("%Y-%m-%d"),
        ]
        for child in children:
            r = rating_index.get((book.id, child.id))
            row.append(r.rating if r else "")
        for child in children:
            r = rating_index.get((book.id, child.id))
            row.append(r.date_read.strftime("%Y-%m-%d") if (r and r.date_read) else "")
        writer.writerow(row)

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=bookbuddy_export.csv"},
    )


# ---------------------------------------------------------------------------
# Admin API
# ---------------------------------------------------------------------------

class AdminBulkUpdate(BaseModel):
    ids: list[int]
    reading_level: Optional[str] = None   # set to empty string to clear
    status: Optional[str] = None
    tags_set: Optional[str] = None        # replace tags entirely
    tags_append: Optional[str] = None     # add to existing tags (comma-sep)


class AdminMerge(BaseModel):
    keep_id: int
    delete_id: int


@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    with open(os.path.join(STATIC_DIR, "admin.html")) as f:
        return f.read()


@app.get("/api/admin/books")
def admin_list_books(
    search: Optional[str] = None,
    status: Optional[str] = None,
    missing: Optional[str] = None,
    db: Session = Depends(get_db),
):
    query = db.query(Book)

    if status:
        query = query.filter(Book.status == status)

    if search:
        pattern = f"%{search}%"
        query = query.filter(
            (Book.title.ilike(pattern))
            | (Book.author.ilike(pattern))
            | (Book.series.ilike(pattern))
            | (Book.tags.ilike(pattern))
        )

    if missing == "cover":
        query = query.filter((Book.cover_url.is_(None)) | (Book.cover_url == ""))
    elif missing == "description":
        query = query.filter((Book.description.is_(None)) | (Book.description == ""))
    elif missing == "isbn":
        query = query.filter((Book.isbn.is_(None)) | (Book.isbn == ""))

    query = query.options(joinedload(Book.ratings).joinedload(Rating.child))
    query = query.order_by(Book.title.asc())
    return [_book_dict(b, db) for b in query.all()]


@app.post("/api/admin/bulk-update")
def admin_bulk_update(data: AdminBulkUpdate, db: Session = Depends(get_db)):
    if not data.ids:
        return {"updated": 0}

    books = db.query(Book).filter(Book.id.in_(data.ids)).all()
    if not books:
        raise HTTPException(status_code=404, detail="No books found")

    for book in books:
        if data.reading_level is not None:
            level = data.reading_level.strip()
            if level and level not in VALID_READING_LEVELS:
                raise HTTPException(status_code=400, detail=f"Invalid reading level: {level}")
            book.reading_level = level or None
        if data.status is not None:
            if data.status not in {"library", "wishlist"}:
                raise HTTPException(status_code=400, detail="Invalid status")
            book.status = data.status
        if data.tags_set is not None:
            book.tags = data.tags_set or None
        if data.tags_append is not None and data.tags_append.strip():
            new_tags = [t.strip() for t in data.tags_append.split(",") if t.strip()]
            if new_tags:
                existing = [t.strip() for t in (book.tags or "").split(",") if t.strip()]
                combined = existing + [t for t in new_tags if t not in existing]
                book.tags = ", ".join(combined)
        book.updated_at = datetime.utcnow()

    db.commit()
    return {"updated": len(books)}


@app.post("/api/admin/merge")
def admin_merge_books(data: AdminMerge, db: Session = Depends(get_db)):
    keep = db.query(Book).filter(Book.id == data.keep_id).first()
    delete = db.query(Book).filter(Book.id == data.delete_id).first()
    if not keep:
        raise HTTPException(status_code=404, detail="Keep book not found")
    if not delete:
        raise HTTPException(status_code=404, detail="Delete book not found")
    if data.keep_id == data.delete_id:
        raise HTTPException(status_code=400, detail="Cannot merge a book with itself")

    # Get child_ids already rated on the keep book
    keep_child_ids = {r.child_id for r in keep.ratings}

    # Reassign ratings that don't conflict
    for r in list(delete.ratings):
        if r.child_id not in keep_child_ids:
            # Move via the relationship so the delete-orphan cascade doesn't take it
            delete.ratings.remove(r)
            keep.ratings.append(r)
            keep_child_ids.add(r.child_id)
        # Conflicting ratings stay on the deleted book and go with it via the cascade

    db.flush()
    db.delete(delete)
    db.commit()
    db.refresh(keep)
    return _book_dict(keep, db)


@app.post("/api/admin/csv/preview")
async def admin_csv_preview(file: UploadFile = File(...), db: Session = Depends(get_db)):
    content = await file.read()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    children = db.query(Child).all()
    child_by_name = {c.name.lower(): c for c in children}
    child_names = [c.name for c in children]

    reader = csv.DictReader(io.StringIO(text))
    raw_rows = [row for row in reader if (row.get("Title") or "").strip()]

    if not raw_rows:
        raise HTTPException(status_code=400, detail="No valid rows found. Make sure the CSV has a 'Title' column header.")
    if len(raw_rows) > 200:
        raise HTTPException(status_code=400, detail="Maximum 200 books per preview.")

    preview_rows = []
    new_count = 0
    duplicate_count = 0

    for row in raw_rows:
        title = (row.get("Title") or "").strip()
        author = (row.get("Author") or "").strip()
        series = (row.get("Series") or "").strip() or None
        tags = (row.get("Tags") or "").strip() or None

        # Duplicate detection
        existing_book = _find_by_title_author(db, title, author)

        is_duplicate = existing_book is not None

        # Collect ratings from child-named columns
        ratings: dict = {}
        for col, val in row.items():
            if not isinstance(col, str) or not isinstance(val, str):
                continue
            child = child_by_name.get(col.strip().lower())
            if child and val.strip().lower() in VALID_RATINGS:
                ratings[child.name] = val.strip().lower()

        preview_rows.append({
            "title": title,
            "author": author,
            "series": series,
            "tags": tags,
            "status": "duplicate" if is_duplicate else "new",
            "existing_id": existing_book.id if existing_book else None,
            "ratings": ratings,
        })

        if is_duplicate:
            duplicate_count += 1
        else:
            new_count += 1

    return {
        "rows": preview_rows,
        "new_count": new_count,
        "duplicate_count": duplicate_count,
        "child_names": child_names,
    }


# ---------------------------------------------------------------------------
# Static files — must be last
# ---------------------------------------------------------------------------

app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
