from pathlib import Path
from uuid import uuid4
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, Query
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, or_
from sqlalchemy.orm import Session, joinedload
from database import Base, engine, get_db
from models import User, Book
from schemas import RegisterRequest, LoginRequest, UserOut, TokenOut, BookOut, BookUpdate
from auth import password_hash, create_token, current_user

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)
MAX_IMAGE_BYTES = 5 * 1024 * 1024
ALLOWED_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}

@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield

app = FastAPI(title="Book Sharing API", version="1.0.0", lifespan=lifespan)
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")

@app.get("/")
def home():
    return {"message": "Book Sharing API", "docs": "/docs"}

@app.post("/auth/register", response_model=UserOut, status_code=201)
def register(data: RegisterRequest, db: Session = Depends(get_db)):
    email = str(data.email).lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "Email already registered")
    user = User(name=data.name.strip(), email=email, password_hash=password_hash.hash(data.password))
    db.add(user)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(409, "Unable to register account")
    db.refresh(user)
    return user

@app.post("/auth/login", response_model=TokenOut)
def login(data: LoginRequest, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == str(data.email).lower()))
    if not user or not password_hash.verify(data.password, user.password_hash):
        raise HTTPException(401, "Incorrect email or password")
    return TokenOut(access_token=create_token(user.id))

@app.get("/users/me", response_model=UserOut)
def me(user: User = Depends(current_user)):
    return user

def save_image(image: UploadFile | None) -> str | None:
    if image is None or not image.filename:
        return None
    if image.content_type not in ALLOWED_TYPES:
        raise HTTPException(400, "Only JPEG, PNG, and WebP images are allowed")
    data = image.file.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(413, "Image must be 5 MB or smaller")
    signatures = {"image/jpeg": data.startswith(b"\xff\xd8\xff"), "image/png": data.startswith(b"\x89PNG\r\n\x1a\n"), "image/webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP"}
    if not signatures[image.content_type]:
        raise HTTPException(400, "Invalid image content")
    filename = f"{uuid4().hex}{ALLOWED_TYPES[image.content_type]}"
    (UPLOAD_DIR / filename).write_bytes(data)
    return f"/uploads/{filename}"

def get_book_or_404(book_id: int, db: Session) -> Book:
    book = db.scalar(select(Book).options(joinedload(Book.owner)).where(Book.id == book_id))
    if not book:
        raise HTTPException(404, "Book not found")
    return book

@app.post("/books", response_model=BookOut, status_code=201)
def create_book(title: str = Form(min_length=1, max_length=200), author: str = Form(min_length=1, max_length=150), description: str = Form(max_length=10000), image: UploadFile | None = File(default=None), user: User = Depends(current_user), db: Session = Depends(get_db)):
    image_path = save_image(image)
    book = Book(title=title.strip(), author=author.strip(), description=description, image=image_path, owner_id=user.id)
    db.add(book)
    db.commit()
    return get_book_or_404(book.id, db)

@app.get("/books", response_model=list[BookOut])
def list_books(q: str | None = Query(default=None, max_length=200), skip: int = Query(default=0, ge=0), limit: int = Query(default=20, ge=1, le=100), db: Session = Depends(get_db)):
    stmt = select(Book).options(joinedload(Book.owner))
    if q:
        stmt = stmt.where(or_(Book.title.ilike(f"%{q}%"), Book.author.ilike(f"%{q}%")))
    return db.scalars(stmt.order_by(Book.created_at.desc(), Book.id.desc()).offset(skip).limit(limit)).all()

@app.get("/books/mine", response_model=list[BookOut])
def my_books(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return db.scalars(select(Book).options(joinedload(Book.owner)).where(Book.owner_id == user.id).order_by(Book.id.desc())).all()

@app.get("/books/{book_id}", response_model=BookOut)
def book_detail(book_id: int, db: Session = Depends(get_db)):
    return get_book_or_404(book_id, db)

@app.patch("/books/{book_id}", response_model=BookOut)
def update_book(book_id: int, data: BookUpdate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    book = get_book_or_404(book_id, db)
    if book.owner_id != user.id:
        raise HTTPException(403, "You can only edit your own books")
    for field, value in data.model_dump(exclude_unset=True).items():
        if value is None:
            raise HTTPException(422, f"{field} cannot be null")
        setattr(book, field, value.strip() if field in {"title", "author"} else value)
    db.commit()
    return get_book_or_404(book_id, db)

@app.put("/books/{book_id}/image", response_model=BookOut)
def replace_book_image(book_id: int, image: UploadFile = File(...), user: User = Depends(current_user), db: Session = Depends(get_db)):
    book = get_book_or_404(book_id, db)
    if book.owner_id != user.id:
        raise HTTPException(403, "You can only edit your own books")
    old_image = book.image
    book.image = save_image(image)
    db.commit()
    if old_image:
        (UPLOAD_DIR / Path(old_image).name).unlink(missing_ok=True)
    return get_book_or_404(book_id, db)

@app.delete("/books/{book_id}")
def delete_book(book_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    book = get_book_or_404(book_id, db)
    if book.owner_id != user.id:
        raise HTTPException(403, "You can only delete your own books")
    old_image = book.image
    db.delete(book)
    db.commit()
    if old_image:
        (UPLOAD_DIR / Path(old_image).name).unlink(missing_ok=True)
    return {"message": "Book deleted"}
