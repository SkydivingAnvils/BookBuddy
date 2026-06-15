from datetime import datetime
from sqlalchemy import Column, Integer, String, Date, DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import relationship, declarative_base

Base = declarative_base()


class Child(Base):
    __tablename__ = "children"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    birthday = Column(Date, nullable=False)
    avatar = Column(Text, nullable=True)  # base64 data URI, compressed client-side
    created_at = Column(DateTime, default=datetime.utcnow)

    ratings = relationship("Rating", back_populates="child", cascade="all, delete-orphan")


class Book(Base):
    __tablename__ = "books"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    author = Column(String, nullable=False)
    isbn = Column(String, nullable=True)
    cover_url = Column(String, nullable=True)
    description = Column(Text, nullable=True)
    published_date = Column(String, nullable=True)
    page_count = Column(Integer, nullable=True)
    genres = Column(Text, nullable=True)       # JSON array stored as string
    tags = Column(Text, nullable=True)         # Comma-separated free text
    series = Column(String, nullable=True)          # e.g. "Elephant & Piggie"
    series_order = Column(String, nullable=True)    # e.g. "1", "2.5"
    reading_level = Column(String, nullable=True)  # picture_book | early_reader | chapter_book | middle_grade
    status = Column(String, default="library", index=True)    # library | wishlist
    google_books_id = Column(String, nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    ratings = relationship("Rating", back_populates="book", cascade="all, delete-orphan")


class Rating(Base):
    __tablename__ = "ratings"
    __table_args__ = (UniqueConstraint("book_id", "child_id", name="uq_book_child"),)

    id = Column(Integer, primary_key=True, index=True)
    book_id = Column(Integer, ForeignKey("books.id"), nullable=False, index=True)
    child_id = Column(Integer, ForeignKey("children.id"), nullable=False, index=True)
    rating = Column(String, nullable=False)  # love | like | neutral | dislike | hate
    notes = Column(Text, nullable=True)      # reserved for future use
    date_read = Column(Date, nullable=True)  # user-set date the book was actually read
    created_at = Column(DateTime, default=datetime.utcnow)

    book = relationship("Book", back_populates="ratings")
    child = relationship("Child", back_populates="ratings")


class Setting(Base):
    __tablename__ = "settings"

    key = Column(String, primary_key=True)
    value = Column(Text, nullable=False)
