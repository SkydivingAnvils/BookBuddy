import os
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from .models import Base, Setting

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./bookbuddy.db")

def _set_wal(dbapi_conn, _connection_record):
    if DATABASE_URL.startswith("sqlite"):
        dbapi_conn.execute("PRAGMA journal_mode=WAL")
        dbapi_conn.execute("PRAGMA synchronous=NORMAL")

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
if DATABASE_URL.startswith("sqlite"):
    from sqlalchemy import event
    event.listen(engine, "connect", _set_wal)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    Base.metadata.create_all(bind=engine)

    # Additive migrations for existing databases
    with engine.connect() as conn:
        for stmt in [
            "ALTER TABLE children ADD COLUMN avatar TEXT",
            "ALTER TABLE books ADD COLUMN series TEXT",
            "ALTER TABLE books ADD COLUMN reading_level TEXT",
            "ALTER TABLE books ADD COLUMN status TEXT DEFAULT 'library'",
            "ALTER TABLE ratings ADD COLUMN date_read DATE",
            "ALTER TABLE ratings ADD COLUMN notes TEXT",
            "CREATE INDEX IF NOT EXISTS ix_ratings_book_id ON ratings (book_id)",
            "CREATE INDEX IF NOT EXISTS ix_ratings_child_id ON ratings (child_id)",
            "CREATE INDEX IF NOT EXISTS ix_books_google_books_id ON books (google_books_id)",
            "CREATE INDEX IF NOT EXISTS ix_books_status ON books (status)",
            "ALTER TABLE books ADD COLUMN series_order TEXT",
        ]:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                pass  # column already exists

    db = SessionLocal()
    try:
        defaults = {
            "confidence_threshold": "0.75",
            "library_catalog_url": "https://losalamos.ent.sirsi.net/client/en_US/default",
            "color_scheme": "forest",
        }
        for key, value in defaults.items():
            if not db.query(Setting).filter(Setting.key == key).first():
                db.add(Setting(key=key, value=value))
        db.commit()
    finally:
        db.close()
