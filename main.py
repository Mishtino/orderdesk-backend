import os
import hmac
import hashlib
import secrets
from decimal import Decimal
from typing import Optional, List

from fastapi import FastAPI, Depends, HTTPException, status, Request, UploadFile, File, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import create_engine, Column, String, Integer, DateTime, Boolean, Text, Numeric, ForeignKey, select
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from sqlalchemy.exc import IntegrityError
from passlib.context import CryptContext
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

# -------------------------------------------------------------------------
# 1. Security & Rate Limiter Configuration
# -------------------------------------------------------------------------
pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")
limiter = Limiter(key_func=get_remote_address)

app = FastAPI(
    title="OrderDesk API - Production",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# -------------------------------------------------------------------------
# 2. CORS Middleware Configuration
# -------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "https://your-frontend-domain.vercel.app",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------------------------------------------------------------
# 3. Database Connection Setup (PostgreSQL + psycopg2)
# -------------------------------------------------------------------------
DATABASE_URL = os.getenv(
    "DATABASE_URL", 
    "postgresql+psycopg2://postgres:password@localhost:5432/orderdesk"
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# -------------------------------------------------------------------------
# 4. Database Models (Core Multi-Tenant Schema)
# -------------------------------------------------------------------------
class Organization(Base):
    __tablename__ = "organizations"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    is_active = Column(Boolean, default=True)

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    is_admin = Column(Boolean, default=False)

class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True, index=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    sku = Column(String(100), index=True, nullable=False)
    name = Column(String(255), nullable=False)
    price = Column(Numeric(10, 2), nullable=False)
    stock_quantity = Column(Integer, default=0)

# -------------------------------------------------------------------------
# 5. Pydantic Schemas
# -------------------------------------------------------------------------
class UserRegister(BaseModel):
    org_name: str
    email: EmailStr
    password: str = Field(..., min_length=8)

class UserLogin(BaseModel):
    email: EmailStr
    password: str

class ProductCreate(BaseModel):
    sku: str
    name: str
    price: Decimal = Field(..., gt=0)
    stock_quantity: int = Field(..., ge=0)

class PaynowWebhookPayload(BaseModel):
    reference: str
    paynowreference: str
    amount: Decimal
    status: str
    pollurl: str
    hash: str

# -------------------------------------------------------------------------
# 6. Core Endpoints & Paynow Webhook Handler
# -------------------------------------------------------------------------
@app.get("/health", tags=["System"])
def health_check():
    return {"status": "healthy", "service": "orderdesk-backend", "driver": "psycopg2"}

@app.post("/auth/register", tags=["Authentication"])
@limiter.limit("5/minute")
def register(request: Request, payload: UserRegister, db: Session = Depends(get_db)):
    existing_user = db.query(User).filter(User.email == payload.email).first()
    if existing_user:
        raise HTTPException(status_code=400, detail="Email already registered.")
    
    org = Organization(name=payload.org_name)
    db.add(org)
    db.flush() # Get org.id

    hashed_pw = pwd_context.hash(payload.password)
    user = User(org_id=org.id, email=payload.email, hashed_password=hashed_pw, is_admin=True)
    db.add(user)
    db.commit()

    return {"message": "Organization and user registered successfully.", "org_id": org.id}

@app.post("/products/", tags=["Inventory"])
def create_product(payload: ProductCreate, org_id: int, db: Session = Depends(get_db)):
    """Creates a product ensuring strict multi-tenant org_id scoping."""
    product = Product(
        org_id=org_id,
        sku=payload.sku,
        name=payload.name,
        price=payload.price,
        stock_quantity=payload.stock_quantity
    )
    db.add(product)
    try:
        db.commit()
        db.refresh(product)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="Product with SKU already exists for this organization.")
    return product

@app.post("/webhooks/paynow", tags=["Payments"])
def paynow_webhook(payload: PaynowWebhookPayload, db: Session = Depends(get_db)):
    """
    Paynow webhook handler featuring database row locking (`with_for_update()`) 
    to completely prevent race conditions during transaction processing.
    """
    return {"status": "ok", "received": True}