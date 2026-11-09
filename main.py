
from pathlib import Path
from uuid import uuid4
from contextlib import asynccontextmanager
import os

from fastapi import (
    FastAPI, Depends, HTTPException,
    UploadFile, File, Form, Query
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from sqlalchemy import select, or_
from sqlalchemy.orm import Session, joinedload

from database import Base, engine, get_db
from models import User, Book
from schemas import (
    RegisterRequest, LoginRequest,
    UserOut, TokenOut, BookOut, BookUpdate
)
from auth import password_hash, create_token, current_user


# ==========================================
# IMAGE UPLOAD CONFIGURATION
# ==========================================

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_IMAGE_BYTES = 5 * 1024 * 1024

ALLOWED_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp"
}


# ==========================================
# DATABASE INITIALIZATION
# ==========================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


# ==========================================
# FASTAPI APPLICATION
# ==========================================

app = FastAPI(
    title="Book Sharing API",
    description="Book Sharing Application Backend",
    version="1.1.0",
    lifespan=lifespan
)


# ==========================================
# CORS CONFIGURATION
# ==========================================

# Add your deployed frontend domain to
# FRONTEND_ORIGINS in Render environment settings.
# Multiple origins can be separated by commas.

default_origins = [
    "http://localhost:8080",
    "http://127.0.0.1:8080",
    "http://localhost:5500",
    "http://127.0.0.1:5500"
]

extra_origins = [
    origin.strip().rstrip("/")
    for origin in os.getenv(
        "FRONTEND_ORIGINS", ""
    ).split(",")
    if origin.strip()
]

allowed_origins = list(
    dict.fromkeys(default_origins + extra_origins)
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"]
)


# ==========================================
# STATIC FILES
# ==========================================

app.mount(
    "/uploads",
    StaticFiles(directory=str(UPLOAD_DIR)),
    name="uploads"
)


# ==========================================
# HOME
# ==========================================

@app.get("/")
def home():
    return {
        "message": "Welcome to Book Sharing API",
        "status": "running",
        "version": "1.1.0",
        "docs": "/docs"
    }


# ==========================================
# REGISTER
# ==========================================

@app.post(
    "/auth/register",
    response_model=UserOut,
    status_code=201
)
def register(
    data: RegisterRequest,
    db: Session = Depends(get_db)
):

    email = str(data.email).strip().lower()

    existing_user = db.scalar(
        select(User).where(User.email == email)
    )

    if existing_user:
        raise HTTPException(
            status_code=409,
            detail="Email already registered"
        )

    user = User(
        name=data.name.strip(),
        email=email,
        password_hash=password_hash.hash(
            data.password
        )
    )

    db.add(user)

    try:
        db.commit()
        db.refresh(user)

    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Unable to register account"
        )

    return user


# ==========================================
# LOGIN
# ==========================================

@app.post(
    "/auth/login",
    response_model=TokenOut
)
def login(
    data: LoginRequest,
    db: Session = Depends(get_db)
):

    email = str(data.email).strip().lower()

    user = db.scalar(
        select(User).where(User.email == email)
    )

    if not user or not password_hash.verify(
        data.password,
        user.password_hash
    ):
        raise HTTPException(
            status_code=401,
            detail="Incorrect email or password"
        )

    return TokenOut(
        access_token=create_token(user.id)
    )


# ==========================================
# CURRENT USER PROFILE
# ==========================================

@app.get(
    "/users/me",
    response_model=UserOut
)
def me(
    user: User = Depends(current_user)
):
    return user


# ==========================================
# IMAGE UPLOAD FUNCTION
# ==========================================

def save_image(
    image: UploadFile | None
) -> str | None:

    if image is None or not image.filename:
        return None

    if image.content_type not in ALLOWED_TYPES:
        raise HTTPException(
            status_code=400,
            detail="Only JPEG, PNG and WebP images are allowed"
        )

    data = image.file.read(
        MAX_IMAGE_BYTES + 1
    )

    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=413,
            detail="Image must be 5 MB or smaller"
        )

    signatures = {
        "image/jpeg": data.startswith(
            b"\xff\xd8\xff"
        ),
        "image/png": data.startswith(
            b"\x89PNG\r\n\x1a\n"
        ),
        "image/webp": (
            data.startswith(b"RIFF")
            and data[8:12] == b"WEBP"
        )
    }

    if not signatures[image.content_type]:
        raise HTTPException(
            status_code=400,
            detail="Invalid image content"
        )

    filename = (
        f"{uuid4().hex}"
        f"{ALLOWED_TYPES[image.content_type]}"
    )

    file_path = UPLOAD_DIR / filename

    try:
        file_path.write_bytes(data)
    except OSError:
        raise HTTPException(
            status_code=500,
            detail="Unable to save image"
        )

    return f"/uploads/{filename}"


# ==========================================
# GET BOOK HELPER
# ==========================================

def get_book_or_404(
    book_id: int,
    db: Session
) -> Book:

    book = db.scalar(
        select(Book)
        .options(joinedload(Book.owner))
        .where(Book.id == book_id)
    )

    if not book:
        raise HTTPException(
            status_code=404,
            detail="Book not found"
        )

    return book


# ==========================================
# CREATE BOOK
# ==========================================

@app.post(
    "/books",
    response_model=BookOut,
    status_code=201
)
def create_book(
    title: str = Form(
        min_length=1,
        max_length=200
    ),
    author: str = Form(
        min_length=1,
        max_length=150
    ),
    description: str = Form(
        max_length=10000
    ),
    image: UploadFile | None = File(
        default=None
    ),
    user: User = Depends(current_user),
    db: Session = Depends(get_db)
):

    title = title.strip()
    author = author.strip()

    if not title or not author:
        raise HTTPException(
            status_code=422,
            detail="Title and author cannot be empty"
        )

    image_path = save_image(image)

    book = Book(
        title=title,
        author=author,
        description=description,
        image=image_path,
        owner_id=user.id
    )

    db.add(book)

    try:
        db.commit()
        db.refresh(book)
    except Exception:
        db.rollback()
        if image_path:
            (UPLOAD_DIR / Path(image_path).name).unlink(
                missing_ok=True
            )
        raise HTTPException(
            status_code=500,
            detail="Unable to create book"
        )

    return get_book_or_404(book.id, db)


# ==========================================
# GET ALL BOOKS + SEARCH
# ==========================================

@app.get(
    "/books",
    response_model=list[BookOut]
)
def list_books(
    q: str | None = Query(
        default=None,
        max_length=200
    ),
    skip: int = Query(
        default=0,
        ge=0
    ),
    limit: int = Query(
        default=20,
        ge=1,
        le=100
    ),
    db: Session = Depends(get_db)
):

    stmt = select(Book).options(
        joinedload(Book.owner)
    )

    if q:
        search = f"%{q.strip()}%"

        stmt = stmt.where(
            or_(
                Book.title.ilike(search),
                Book.author.ilike(search)
            )
        )

    stmt = (
        stmt
        .order_by(
            Book.created_at.desc(),
            Book.id.desc()
        )
        .offset(skip)
        .limit(limit)
    )

    return db.scalars(stmt).all()


# ==========================================
# MY BOOKS
# ==========================================

@app.get(
    "/books/mine",
    response_model=list[BookOut]
)
def my_books(
    user: User = Depends(current_user),
    db: Session = Depends(get_db)
):

    stmt = (
        select(Book)
        .options(joinedload(Book.owner))
        .where(Book.owner_id == user.id)
        .order_by(Book.id.desc())
    )

    return db.scalars(stmt).all()


# ==========================================
# BOOK DETAILS
# ==========================================

@app.get(
    "/books/{book_id}",
    response_model=BookOut
)
def book_detail(
    book_id: int,
    db: Session = Depends(get_db)
):
    return get_book_or_404(book_id, db)


# ==========================================
# UPDATE BOOK
# ==========================================

@app.patch(
    "/books/{book_id}",
    response_model=BookOut
)
def update_book(
    book_id: int,
    data: BookUpdate,
    user: User = Depends(current_user),
    db: Session = Depends(get_db)
):

    book = get_book_or_404(book_id, db)

    if book.owner_id != user.id:
        raise HTTPException(
            status_code=403,
            detail="You can only edit your own books"
        )

    updates = data.model_dump(
        exclude_unset=True
    )

    for field, value in updates.items():

        if value is None:
            raise HTTPException(
                status_code=422,
                detail=f"{field} cannot be null"
            )

        if field in {"title", "author"}:
            value = value.strip()

            if not value:
                raise HTTPException(
                    status_code=422,
                    detail=f"{field} cannot be empty"
                )

        setattr(book, field, value)

    try:
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail="Unable to update book"
        )

    return get_book_or_404(book_id, db)


# ==========================================
# REPLACE BOOK IMAGE
# ==========================================

@app.put(
    "/books/{book_id}/image",
    response_model=BookOut
)
def replace_book_image(
    book_id: int,
    image: UploadFile = File(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db)
):

    book = get_book_or_404(book_id, db)

    if book.owner_id != user.id:
        raise HTTPException(
            status_code=403,
            detail="You can only edit your own books"
        )

    if not image.filename:
        raise HTTPException(
            status_code=422,
            detail="Please select an image"
        )

    old_image = book.image
    new_image = save_image(image)

    book.image = new_image

    try:
        db.commit()
    except Exception:
        db.rollback()
        if new_image:
            (UPLOAD_DIR / Path(new_image).name).unlink(
                missing_ok=True
            )
        raise HTTPException(
            status_code=500,
            detail="Unable to update book image"
        )

    if old_image:
        try:
            (UPLOAD_DIR / Path(old_image).name).unlink(
                missing_ok=True
            )
        except OSError:
            pass

    return get_book_or_404(book_id, db)


# ==========================================
# DELETE BOOK
# ==========================================

@app.delete("/books/{book_id}")
def delete_book(
    book_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db)
):

    book = get_book_or_404(book_id, db)

    if book.owner_id != user.id:
        raise HTTPException(
            status_code=403,
            detail="You can only delete your own books"
        )

    old_image = book.image

    db.delete(book)

    try:
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail="Unable to delete book"
        )

    if old_image:
        try:
            (UPLOAD_DIR / Path(old_image).name).unlink(
                missing_ok=True
            )
        except OSError:
            pass

    return {
        "message": "Book deleted successfully"
    }
