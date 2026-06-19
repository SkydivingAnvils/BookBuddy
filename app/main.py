import csv
import io
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload

from .books_api import (
    fetch_book_metadata, fetch_by_google_id, search_books,
    fetch_google_books_metadata, fetch_openlibrary_metadata,
    fetch_hardcover_metadata, fetch_metadata_from_claude,
    search_google_books, search_open_library, search_hardcover,
)
from .claude import get_recommendations, identify_book
from .config import get_setting
from .database import get_db, init_db
from .models import Book, Child, Rating, Setting

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="BookBuddy", docs_url=None, redoc_url=None)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

VALID_RATINGS = {"love", "like", "neutral", "dislike", "hate"}
VALID_READING_LEVELS = {"picture_book", "early_reader", "chapter_book", "middle_grade"}


@app.on_event("startup")
def startup():
    init_db()
    if not os.getenv("ANTHROPIC_API_KEY"):
        logger.error("ANTHROPIC_API_KEY is not set — book identification and recommendations will fail.")


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
    _require_api_key()

    media_type = image.content_type or "image/jpeg"
    if media_type not in {"image/jpeg", "image/png", "image/gif", "image/webp"}:
        media_type = "image/jpeg"

    image_data = await image.read()
    if len(image_data) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Image too large. Maximum size is 10 MB.")
    try:
        result = identify_book(image_data, media_type)
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.error("Identification error: %s", e)
        raise HTTPException(status_code=500, detail="Book identification failed. Please try again.")

    threshold = float(get_setting("confidence_threshold", "0.75"))
    result["threshold"] = threshold
    result["above_threshold"] = float(result.get("confidence", 0)) >= threshold
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
def get_metadata(title: str = "", author: str = "", google_books_id: str = "", source: str = "auto"):
    result = None

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
            "isbn": "",
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
def check_duplicate(title: str = "", author: str = "", google_books_id: str = "", db: Session = Depends(get_db)):
    book = None
    if google_books_id:
        book = db.query(Book).filter(Book.google_books_id == google_books_id).first()
    if not book and title and author:
        book = (
            db.query(Book)
            .filter(Book.title.ilike(title), Book.author.ilike(author))
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
        for r in data.ratings:
            existing = db.query(Rating).filter(
                Rating.book_id == book.id, Rating.child_id == r.child_id
            ).first()
            if existing:
                existing.rating = r.rating
                if r.date_read is not None:
                    existing.date_read = r.date_read
                if r.notes is not None:
                    existing.notes = r.notes
                if r.read_myself is not None:
                    existing.read_myself = r.read_myself
            else:
                db.add(Rating(book_id=book.id, child_id=r.child_id, rating=r.rating,
                              date_read=r.date_read, notes=r.notes,
                              read_myself=r.read_myself or False))
        db.commit()
        return {"ok": True, "book_id": book.id, "duplicate": False}

    # Duplicate detection
    book = None
    if data.google_books_id:
        book = db.query(Book).filter(Book.google_books_id == data.google_books_id).first()
    if not book:
        book = (
            db.query(Book)
            .filter(Book.title.ilike(data.title), Book.author.ilike(data.author))
            .first()
        )

    if book and not data.force_duplicate:
        return {"duplicate": True, "book_id": book.id, "title": book.title}

    if not book:
        book = Book(
            title=data.title,
            author=data.author,
            isbn=data.isbn,
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

    # Upsert ratings
    for r in data.ratings:
        existing = (
            db.query(Rating)
            .filter(Rating.book_id == book.id, Rating.child_id == r.child_id)
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
            db.add(Rating(book_id=book.id, child_id=r.child_id, rating=r.rating,
                          date_read=r.date_read, notes=r.notes,
                          read_myself=r.read_myself or False))

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

    age = _age(child.birthday)

    try:
        recs = get_recommendations(child.name, age, history)
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.error("Recommendations error: %s", e)
        raise HTTPException(status_code=500, detail="Failed to generate recommendations. Please try again.")

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
    rows = [row for row in reader if row.get("Title", "").strip()]

    if not rows:
        raise HTTPException(status_code=400, detail="No valid rows found. Make sure the CSV has a 'Title' column header.")
    if len(rows) > 200:
        raise HTTPException(status_code=400, detail="Maximum 200 books per upload.")

    # Fetch metadata concurrently — I/O bound, safe to parallelise
    def fetch_for_row(args):
        idx, row = args
        title = row.get("Title", "").strip()
        author = row.get("Author", "").strip()
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
        title  = row.get("Title",  "").strip()
        author = row.get("Author", "").strip()
        series = row.get("Series", "").strip() or None
        tags   = row.get("Tags",   "").strip() or None
        meta   = meta_by_idx.get(idx)

        try:
            book = None
            if author:
                book = db.query(Book).filter(Book.title.ilike(title), Book.author.ilike(author)).first()
            if not book:
                book = db.query(Book).filter(Book.title.ilike(title)).first()

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
# Static files — must be last
# ---------------------------------------------------------------------------

app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
