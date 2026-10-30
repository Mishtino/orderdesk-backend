import os
import uuid
import csv
import io
import secrets
import string
import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, List
from urllib.parse import parse_qsl

import jwt
import requests
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import FastAPI, Depends, HTTPException, Response, UploadFile, File, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy import create_engine, Column, String, Float, Integer, DateTime, Boolean, Numeric, UniqueConstraint, text, inspect
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from paynow import Paynow

# --- 0. CONFIGURATION & FAIL-SAFE ENVS ---
JWT_SECRET = os.getenv("JWT_SECRET", "orderdesk-dev-secret-change-in-production-please")
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "https://orderdesk-ui.vercel.app")
SESSION_COOKIE = "od_session"
SESSION_HOURS = 12
BUYER_COOKIE = "od_buyer_session"
BUYER_SESSION_HOURS = 24

_raw_secure = os.getenv("SESSION_COOKIE_SECURE", "auto").lower()
if _raw_secure == "auto":
    SESSION_COOKIE_SECURE = ("localhost" not in FRONTEND_ORIGIN) and FRONTEND_ORIGIN.startswith("https")
else:
    SESSION_COOKIE_SECURE = _raw_secure == "true"

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", "alerts@orderdesk.co.zw")

PAYNOW_INTEGRATION_ID = os.getenv("PAYNOW_INTEGRATION_ID", "")
PAYNOW_INTEGRATION_KEY = os.getenv("PAYNOW_INTEGRATION_KEY", "")
RESULT_URL = os.getenv("PAYNOW_RESULT_URL", "https://orderdesk-backend-9gu2.onrender.com/webhooks/paynow")
RETURN_URL = os.getenv("PAYNOW_RETURN_URL", f"{FRONTEND_ORIGIN}/billing?status=success")

# --- 1. DATABASE SETUP ---
raw_db_url = os.getenv("DATABASE_URL", "sqlite:///./orderdesk.db")

if raw_db_url.startswith("postgres://"):
    DATABASE_URL = raw_db_url.replace("postgres://", "postgresql+psycopg2://", 1)
elif raw_db_url.startswith("postgresql://") and "+psycopg2" not in raw_db_url:
    DATABASE_URL = raw_db_url.replace("postgresql://", "postgresql+psycopg2://", 1)
else:
    DATABASE_URL = raw_db_url

engine_args = {}
if "sqlite" in DATABASE_URL:
    engine_args["connect_args"] = {"check_same_thread": False}
else:
    engine_args["pool_size"] = 10
    engine_args["max_overflow"] = 20

engine = create_engine(DATABASE_URL, **engine_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- 2. SECURITY & CRYPTOGRAPHY HELPERS ---
ph = PasswordHasher()
_DUMMY_HASH = ph.hash("timing-equalizer")
_ALPHABET = string.ascii_uppercase.replace("O", "").replace("I", "") + "23456789"

def generate_unique_passkey() -> str:
    g = lambda: "".join(secrets.choice(_ALPHABET) for _ in range(4))
    return f"ORD-{g()}-{g()}-{g()}"

def verify_and_upgrade_password(user, supplied: str, db: Session) -> bool:
    stored = user.password or ""
    if stored.startswith("$argon2"):
        try:
            ph.verify(stored, supplied)
        except (VerifyMismatchError, InvalidHashError):
            return False
        if ph.check_needs_rehash(stored):
            user.password = ph.hash(supplied)
            db.commit()
        return True
    if hmac.compare_digest(stored.encode(), supplied.encode()):
        user.password = ph.hash(supplied)
        db.commit()
        return True
    return False

# --- 3. SESSION & TENANT SCOPING DEPENDENCY ---
@dataclass
class Ctx:
    user_id: str
    org_id: str
    role: str = "ADMIN"

def issue_session_cookie(response: Response, user) -> None:
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "sub": user.id,
            "org": user.organization_id,
            "iat": now,
            "exp": now + timedelta(hours=SESSION_HOURS),
        },
        JWT_SECRET,
        algorithm="HS256",
    )
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=SESSION_COOKIE_SECURE,
        samesite="none" if SESSION_COOKIE_SECURE else "lax",
        max_age=SESSION_HOURS * 3600,
        path="/",
    )

def current_ctx(request: Request, db: Session = Depends(get_db)) -> Ctx:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]
    
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        claims = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        user = db.query(SupplierUserModel).filter(SupplierUserModel.id == claims["sub"]).first()
        role = user.role if user else "ADMIN"
        return Ctx(user_id=claims["sub"], org_id=claims["org"], role=role)
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Session expired or invalid")

def require_role(ctx: Ctx, allowed: set):
    if ctx.role not in allowed:
        raise HTTPException(status_code=403, detail="Insufficient permissions")

def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else get_remote_address(request)
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else get_remote_address(request)

limiter = Limiter(key_func=client_ip)

# --- 4. PERSISTENT MODELS ---
class SupplierUserModel(Base):
    __tablename__ = "supplier_users"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(String, nullable=False, index=True)
    organization_name = Column(String, nullable=False)
    email = Column(String, unique=True, nullable=False, index=True)
    password = Column(String, nullable=False)
    security_passkey = Column(String, unique=True, nullable=False, index=True)
    role = Column(String, nullable=False, default="ADMIN")
    created_at = Column(DateTime, default=datetime.utcnow)

class BuyerUserModel(Base):
    __tablename__ = "buyer_users"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(String, nullable=False, index=True)
    customer_id = Column(String, nullable=True, index=True)
    company_name = Column(String, nullable=False)
    email = Column(String, unique=True, nullable=False, index=True)
    password = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class ProductModel(Base):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("organization_id", "sku", name="uq_org_sku"),)

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(String, nullable=False, index=True)
    sku = Column(String, nullable=False, index=True)
    title = Column(String, nullable=False)
    category = Column(String, default="General")
    wholesale_price = Column(Numeric(12, 2), nullable=False)
    stock_quantity = Column(Integer, default=0)
    reorder_level = Column(Integer, default=10)
    weight_kg = Column(Numeric(10, 2), default=Decimal("2.00"))
    volume_cbm = Column(Numeric(10, 4), default=Decimal("0.0100"))

class CustomerModel(Base):
    __tablename__ = "customers"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(String, nullable=False, index=True)
    company_name = Column(String, nullable=False)
    contact_email = Column(String, nullable=False)
    phone_number = Column(String, nullable=True)
    pricing_tier = Column(String, default="STANDARD")
    discount_rate = Column(Numeric(5, 2), default=Decimal("0.00"))
    created_at = Column(DateTime, default=datetime.utcnow)

class OrderModel(Base):
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("organization_id", "invoice_number", name="uq_org_invoice"),)

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    invoice_number = Column(String, nullable=False, default=lambda: f"INV-{uuid.uuid4().hex[:8].upper()}")
    organization_id = Column(String, nullable=False, index=True)
    client_name = Column(String, nullable=False)
    client_email = Column(String, default="client@orderdesk.co.zw")
    fulfillment_type = Column(String, default="PICKUP")
    delivery_city = Column(String, nullable=True)
    item_subtotal = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    shipping_fee = Column(Numeric(12, 2), default=Decimal("0.00"))
    total_amount = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    currency = Column(String, default="USD")
    tax_rate = Column(Numeric(5, 2), default=Decimal("0.00"))
    tax_amount = Column(Numeric(12, 2), default=Decimal("0.00"))
    vehicle_type = Column(String, nullable=True)
    status = Column(String, default="PENDING")
    payment_status = Column(String, default="UNPAID")
    created_at = Column(DateTime, default=datetime.utcnow)
    courier = Column(String, nullable=True)
    tracking_number = Column(String, nullable=True)
    shipped_at = Column(DateTime, nullable=True)
    delivered_at = Column(DateTime, nullable=True)

class OrderItemModel(Base):
    __tablename__ = "order_items"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    order_id = Column(String, nullable=False, index=True)
    organization_id = Column(String, nullable=False, index=True)
    sku = Column(String, nullable=False)
    title = Column(String, nullable=False)
    quantity = Column(Integer, nullable=False, default=1)
    unit_price = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    line_total = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))

class OrderStatusHistoryModel(Base):
    __tablename__ = "order_status_history"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    order_id = Column(String, nullable=False, index=True)
    organization_id = Column(String, nullable=False, index=True)
    from_status = Column(String, nullable=True)
    to_status = Column(String, nullable=False)
    changed_by = Column(String, nullable=True)
    note = Column(String, nullable=True)
    changed_at = Column(DateTime, default=datetime.utcnow)

class OrderNoteModel(Base):
    __tablename__ = "order_notes"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    order_id = Column(String, nullable=False, index=True)
    organization_id = Column(String, nullable=False, index=True)
    author = Column(String, nullable=True)
    note = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class PaymentModel(Base):
    __tablename__ = "payments"
    __table_args__ = (UniqueConstraint("reference", name="uq_payment_reference"),)

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(String, nullable=False, index=True)
    reference = Column(String, nullable=False, index=True)
    paynow_reference = Column(String, nullable=True)
    expected_amount = Column(Numeric(12, 2), nullable=False)
    status = Column(String, default="CREATED")
    created_at = Column(DateTime, default=datetime.utcnow)
    paid_at = Column(DateTime, nullable=True)

class SettingsModel(Base):
    __tablename__ = "settings"

    organization_id = Column(String, primary_key=True)
    zig_exchange_rate = Column(Numeric(10, 4), default=Decimal("26.5000"))
    tax_rate = Column(Numeric(5, 2), default=Decimal("0.00"))

Base.metadata.create_all(bind=engine)

def auto_migrate_schema():
    try:
        inspector = inspect(engine)
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                col_type = col.type.compile(dialect=engine.dialect)
                default_clause = ""
                if col.server_default is not None:
                    default_clause = " DEFAULT " + str(col.server_default.arg)
                elif col.default is not None and getattr(col.default, "is_scalar", False):
                    val = col.default.arg
                    if isinstance(val, str):
                        default_clause = " DEFAULT '" + val.replace("'", "''") + "'"
                    elif isinstance(val, bool):
                        default_clause = " DEFAULT " + ("TRUE" if val else "FALSE")
                    elif isinstance(val, (int, float, Decimal)):
                        default_clause = " DEFAULT " + str(val)
                # Don't enforce NOT NULL on a newly added column unless we have a default
                nullable_clause = ""
                if not col.nullable and default_clause:
                    nullable_clause = " NOT NULL"
                stmt = f"ALTER TABLE {table.name} ADD COLUMN {col.name} {col_type}{default_clause}{nullable_clause}"
                if engine.dialect.name == "postgresql":
                    stmt = f"ALTER TABLE {table.name} ADD COLUMN IF NOT EXISTS {col.name} {col_type}{default_clause}{nullable_clause}"
                try:
                    with engine.connect() as conn:
                        conn.execute(text(stmt))
                        conn.commit()
                    print(f"Auto-migration: added {table.name}.{col.name}")
                except Exception as e:
                    print(f"Auto-migration note ({table.name}.{col.name}): {str(e)}")
    except Exception as e:
        print(f"Auto-migration error: {str(e)}")

auto_migrate_schema()

# --- 5. PAYNOW & FASTAPI APP SETUP ---
paynow = Paynow(
    PAYNOW_INTEGRATION_ID,
    PAYNOW_INTEGRATION_KEY,
    RESULT_URL,
    RETURN_URL
)

app = FastAPI(title="OrderDesk B2B Engine", version="6.0.0")
app.state.limiter = limiter

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_ORIGIN, "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return Response(content="Too many requests. Please slow down.", status_code=429)

# --- 6. SCHEMAS ---
class RegisterPayload(BaseModel):
    org_name: str = Field(..., max_length=150)
    email: EmailStr
    password: str = Field(..., min_length=8)

class LoginPayload(BaseModel):
    email: EmailStr
    password: str

class ResetPasswordPayload(BaseModel):
    email: EmailStr
    security_passkey: str
    new_password: str = Field(..., min_length=8)

class CustomerCreatePayload(BaseModel):
    company_name: str = Field(..., max_length=150)
    contact_email: str = Field(..., max_length=120)
    phone_number: Optional[str] = Field("", max_length=50)
    pricing_tier: str = Field("STANDARD", max_length=30)
    discount_rate: float = Field(0.0, ge=0.0, le=100.0)

class CustomerUpdatePayload(BaseModel):
    company_name: Optional[str] = Field(None, max_length=150)
    contact_email: Optional[str] = Field(None, max_length=120)
    phone_number: Optional[str] = Field(None, max_length=50)
    pricing_tier: Optional[str] = Field(None, max_length=30)
    discount_rate: Optional[float] = Field(None, ge=0.0, le=100.0)

class StatusUpdatePayload(BaseModel):
    status: str = Field(..., max_length=30)

class ShipmentPayload(BaseModel):
    courier: Optional[str] = Field(None, max_length=80)
    tracking_number: Optional[str] = Field(None, max_length=120)

class UserCreatePayload(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8)
    organization_name: Optional[str] = None
    role: str = Field("SALES", max_length=20)

class UserRoleUpdatePayload(BaseModel):
    role: str = Field(..., max_length=20)

class SubscribeRequest(BaseModel):
    plan_tier: str = Field(..., max_length=50)

class RateUpdatePayload(BaseModel):
    zig_exchange_rate: float = Field(..., gt=0.0)
    tax_rate: Optional[float] = Field(None, ge=0.0, le=100.0)

class OrderItemCreate(BaseModel):
    sku: str
    title: str
    quantity: int = Field(..., gt=0)
    unit_price: float = Field(..., ge=0)

class OrderCreatePayload(BaseModel):
    client_name: str = Field(..., max_length=150)
    client_email: Optional[str] = None
    fulfillment_type: str = Field("PICKUP", max_length=30)
    delivery_city: Optional[str] = None
    shipping_fee: float = Field(0.0, ge=0)
    currency: str = Field("USD", max_length=10)
    tax_rate: Optional[float] = None
    items: List[OrderItemCreate] = Field(..., min_length=1)

# --- 7. AUTHENTICATION ENDPOINTS ---
@app.post("/auth/register", tags=["Auth"])
@limiter.limit("5/minute")
def register_supplier(request: Request, payload: RegisterPayload, db: Session = Depends(get_db)):
    clean_email = payload.email.strip().lower()
    
    if db.query(SupplierUserModel).filter(SupplierUserModel.email == clean_email).first():
        raise HTTPException(status_code=400, detail="An account with this email already exists.")

    org_id = str(uuid.uuid4())
    passkey = generate_unique_passkey()
    hashed_pwd = ph.hash(payload.password)

    user = SupplierUserModel(
        organization_id=org_id,
        organization_name=payload.org_name.strip(),
        email=clean_email,
        password=hashed_pwd,
        security_passkey=passkey
    )
    db.add(user)
    db.commit()

    return {
        "status": "success",
        "message": "Account created successfully.",
        "security_passkey": passkey,
        "organization_name": user.organization_name
    }

@app.post("/auth/login", tags=["Auth"])
@limiter.limit("10/minute")
def login_supplier(request: Request, payload: LoginPayload, response: Response, db: Session = Depends(get_db)):
    clean_email = payload.email.strip().lower()
    user = db.query(SupplierUserModel).filter(SupplierUserModel.email == clean_email).first()

    if not user:
        try:
            ph.verify(_DUMMY_HASH, payload.password)
        except Exception:
            pass
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    if not verify_and_upgrade_password(user, payload.password, db):
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    if not user.security_passkey:
        user.security_passkey = generate_unique_passkey()
        db.commit()

    issue_session_cookie(response, user)

    return {
        "status": "success",
        "organization_name": user.organization_name,
        "email": user.email
    }

@app.get("/auth/me", tags=["Auth"])
def auth_me(ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    user = db.query(SupplierUserModel).filter(SupplierUserModel.id == ctx.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return {
        "status": "success",
        "organization_name": user.organization_name,
        "email": user.email,
        "organization_id": user.organization_id,
        "role": user.role,
    }

@app.post("/auth/logout", tags=["Auth"])
def logout_supplier(response: Response):
    response.delete_cookie(SESSION_COOKIE, path="/", secure=SESSION_COOKIE_SECURE, samesite="none" if SESSION_COOKIE_SECURE else "lax")
    return {"status": "success", "message": "Logged out successfully."}

@app.post("/auth/reset-password", tags=["Auth"])
@limiter.limit("5/minute")
def reset_password(request: Request, payload: ResetPasswordPayload, db: Session = Depends(get_db)):
    clean_email = payload.email.strip().lower()
    clean_passkey = payload.security_passkey.strip().upper()

    user = db.query(SupplierUserModel).filter(SupplierUserModel.email == clean_email).first()
    if not user or not hmac.compare_digest(user.security_passkey.encode(), clean_passkey.encode()):
        raise HTTPException(status_code=400, detail="Invalid email or security passkey mismatch.")

    user.password = ph.hash(payload.new_password)
    db.commit()

    return {"status": "success", "message": "Password updated successfully. You can now sign in."}

# --- 7B. BUYER PORTAL AUTH & ENDPOINTS ---
class BuyerRegisterPayload(BaseModel):
    organization_id: str
    company_name: str = Field(..., max_length=150)
    email: EmailStr
    password: str = Field(..., min_length=8)

class BuyerLoginPayload(BaseModel):
    email: EmailStr
    password: str

def _issue_buyer_cookie(response, buyer):
    now = datetime.now(timezone.utc)
    token = jwt.encode({"sub": buyer.id, "org": buyer.organization_id, "type": "buyer", "iat": now, "exp": now + timedelta(hours=BUYER_SESSION_HOURS)}, JWT_SECRET, algorithm="HS256")
    response.set_cookie(BUYER_COOKIE, token, httponly=True, secure=SESSION_COOKIE_SECURE, samesite="none" if SESSION_COOKIE_SECURE else "lax", max_age=BUYER_SESSION_HOURS*3600, path="/")

def current_buyer(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get(BUYER_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        claims = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        if claims.get("type") != "buyer":
            raise HTTPException(status_code=401, detail="Not a buyer session")
        buyer = db.query(BuyerUserModel).filter(BuyerUserModel.id == claims["sub"]).first()
        if not buyer:
            raise HTTPException(status_code=401, detail="Buyer not found")
        return buyer
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Session expired or invalid")

@app.post("/buyer/auth/register", tags=["Buyer"])
def buyer_register(payload: BuyerRegisterPayload, db: Session = Depends(get_db)):
    if db.query(BuyerUserModel).filter(BuyerUserModel.email == payload.email.strip().lower()).first():
        raise HTTPException(status_code=400, detail="An account with this email already exists.")
    buyer = BuyerUserModel(
        organization_id=payload.organization_id,
        company_name=payload.company_name.strip(),
        email=payload.email.strip().lower(),
        password=ph.hash(payload.password),
    )
    db.add(buyer)
    db.commit()
    db.refresh(buyer)
    return {"status": "success", "buyer_id": buyer.id}

@app.post("/buyer/auth/login", tags=["Buyer"])
def buyer_login(payload: BuyerLoginPayload, response: Response, db: Session = Depends(get_db)):
    buyer = db.query(BuyerUserModel).filter(BuyerUserModel.email == payload.email.strip().lower()).first()
    if not buyer or not verify_and_upgrade_password(buyer, payload.password, db):
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    _issue_buyer_cookie(response, buyer)
    return {"status": "success", "company_name": buyer.company_name, "email": buyer.email, "organization_id": buyer.organization_id}

@app.post("/buyer/auth/logout", tags=["Buyer"])
def buyer_logout(response: Response):
    response.delete_cookie(BUYER_COOKIE, path="/", secure=SESSION_COOKIE_SECURE, samesite="none" if SESSION_COOKIE_SECURE else "lax")
    return {"status": "success"}

@app.get("/buyer/auth/me", tags=["Buyer"])
def buyer_me(buyer: BuyerUserModel = Depends(current_buyer)):
    return {"status": "success", "company_name": buyer.company_name, "email": buyer.email, "organization_id": buyer.organization_id, "customer_id": buyer.customer_id}

@app.get("/buyer/products", tags=["Buyer"])
def buyer_products(buyer: BuyerUserModel = Depends(current_buyer), db: Session = Depends(get_db)):
    products = db.query(ProductModel).filter(ProductModel.organization_id == buyer.organization_id).all()
    return {"data": products}

@app.get("/buyer/orders", tags=["Buyer"])
def buyer_orders(buyer: BuyerUserModel = Depends(current_buyer), db: Session = Depends(get_db)):
    orders = db.query(OrderModel).filter(OrderModel.organization_id == buyer.organization_id, OrderModel.client_email == buyer.email).all()
    return {"data": orders}

@app.post("/buyer/orders", tags=["Buyer"])
def buyer_create_order(payload: OrderCreatePayload, buyer: BuyerUserModel = Depends(current_buyer), db: Session = Depends(get_db)):
    payload.client_name = buyer.company_name
    payload.client_email = buyer.email
    # reuse create_order logic by calling it directly
    from fastapi import BackgroundTasks
    # delegate
    ctx = None
    # Build order manually
    subtotal = Decimal("0.00")
    for it in payload.items:
        subtotal += Decimal(str(it.unit_price)) * it.quantity
    shipping = Decimal(str(payload.shipping_fee))
    total = subtotal + shipping
    order = OrderModel(
        organization_id=buyer.organization_id,
        client_name=buyer.company_name,
        client_email=buyer.email,
        fulfillment_type=payload.fulfillment_type.upper(),
        delivery_city=payload.delivery_city,
        item_subtotal=subtotal,
        shipping_fee=shipping,
        total_amount=total,
        currency=payload.currency.upper(),
        status="PENDING",
        payment_status="UNPAID",
    )
    db.add(order)
    db.flush()
    for it in payload.items:
        db.add(OrderItemModel(order_id=order.id, organization_id=buyer.organization_id, sku=it.sku.strip().upper(), title=it.title.strip(), quantity=it.quantity, unit_price=Decimal(str(it.unit_price)), line_total=Decimal(str(it.unit_price))*it.quantity))
    db.commit()
    db.refresh(order)
    org = db.query(SupplierUserModel).filter(SupplierUserModel.organization_id == buyer.organization_id).first()
    _send_order_confirmation_email(order, org.organization_name if org else "OrderDesk")
    return {"status": "success", "data": order}

# --- 8. TENANT-SCOPED PRODUCTS ---
@app.get("/products", tags=["Products"])
def list_products(ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    products = db.query(ProductModel).filter(ProductModel.organization_id == ctx.org_id).all()
    return {"data": products}

@app.post("/products/bulk-upload", tags=["Products"])
async def bulk_upload_products(file: UploadFile = File(...), ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    try:
        contents = await file.read()
        decoded = contents.decode("utf-8-sig")
        csv_reader = csv.DictReader(io.StringIO(decoded))

        processed_count = 0
        for row in csv_reader:
            normalized_row = {str(k).strip().lower().replace("_", "").replace("-", "").replace(" ", ""): str(v).strip() for k, v in row.items() if k and v is not None}
            
            sku = normalized_row.get("sku") or normalized_row.get("code") or normalized_row.get("id")
            title = normalized_row.get("title") or normalized_row.get("name") or normalized_row.get("product")

            if not sku or not title:
                continue

            clean_sku = sku.upper().strip()
            category = normalized_row.get("category", "General").strip()
            price_val = Decimal(normalized_row.get("wholesaleprice", normalized_row.get("price", "0.00")).replace("$", "").replace(",", "").strip() or "0.00")
            stock_val = int(float(normalized_row.get("stockquantity", normalized_row.get("quantity", "0")).replace(",", "").strip() or "0"))

            existing = db.query(ProductModel).filter(
                ProductModel.organization_id == ctx.org_id,
                ProductModel.sku == clean_sku
            ).first()

            if existing:
                existing.title = title.strip()
                existing.category = category
                existing.wholesale_price = price_val
                existing.stock_quantity = stock_val
            else:
                db.add(ProductModel(
                    organization_id=ctx.org_id,
                    sku=clean_sku,
                    title=title.strip(),
                    category=category,
                    wholesale_price=price_val,
                    stock_quantity=stock_val
                ))
            processed_count += 1

        db.commit()
        return {"status": "success", "message": f"Successfully synced {processed_count} SKUs."}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"CSV import failed: {str(e)}")

# --- 8B. INVENTORY ALERTS & REORDER SUGGESTIONS ---
@app.get("/inventory/alerts", tags=["Inventory"])
def inventory_alerts(ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    products = db.query(ProductModel).filter(ProductModel.organization_id == ctx.org_id).all()
    alerts = []
    for p in products:
        threshold = p.reorder_level or 10
        if (p.stock_quantity or 0) <= threshold:
            suggested = max(threshold * 2 - (p.stock_quantity or 0), threshold)
            alerts.append({
                "id": p.id,
                "sku": p.sku,
                "title": p.title,
                "category": p.category,
                "stock_quantity": p.stock_quantity,
                "reorder_level": threshold,
                "suggested_reorder_qty": suggested,
                "severity": "critical" if (p.stock_quantity or 0) == 0 else "low",
            })
    return {"data": alerts, "count": len(alerts)}

@app.post("/inventory/notify", tags=["Inventory"])
def notify_low_stock(ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    products = db.query(ProductModel).filter(ProductModel.organization_id == ctx.org_id).all()
    alerts = [p for p in products if (p.stock_quantity or 0) <= (p.reorder_level or 10)]
    if not alerts:
        return {"status": "ok", "message": "No low-stock items.", "count": 0}

    if not SMTP_HOST:
        return {"status": "skipped", "message": "SMTP not configured; alerts returned only.", "count": len(alerts)}

    user = db.query(SupplierUserModel).filter(SupplierUserModel.organization_id == ctx.org_id).first()
    recipient = user.email if user else None
    if not recipient:
        return {"status": "skipped", "message": "No recipient email.", "count": len(alerts)}

    import smtplib
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["Subject"] = f"OrderDesk Low-Stock Alert ({len(alerts)} items)"
    msg["From"] = SMTP_FROM
    msg["To"] = recipient
    lines = ["The following SKUs are at or below their reorder level:", ""]
    for p in alerts:
        lines.append(f"- {p.sku} {p.title}: stock={p.stock_quantity}, reorder_level={p.reorder_level or 10}")
    msg.set_content("\n".join(lines))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls()
            if SMTP_USER and SMTP_PASS:
                server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        return {"status": "sent", "count": len(alerts)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to send alert email: {str(e)}")

# --- 9. TENANT-SCOPED CUSTOMERS ---
@app.get("/customers", tags=["Customers"])
def get_customers(ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    customers = db.query(CustomerModel).filter(CustomerModel.organization_id == ctx.org_id).all()
    return {"data": customers}

@app.post("/customers", tags=["Customers"])
def create_customer(payload: CustomerCreatePayload, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    customer = CustomerModel(
        organization_id=ctx.org_id,
        company_name=payload.company_name.strip(),
        contact_email=payload.contact_email.strip().lower(),
        phone_number=payload.phone_number.strip(),
        pricing_tier=payload.pricing_tier.upper(),
        discount_rate=Decimal(str(payload.discount_rate))
    )
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return {"status": "success", "data": customer}

@app.patch("/customers/{customer_id}", tags=["Customers"])
def update_customer(customer_id: str, payload: CustomerUpdatePayload, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    customer = db.query(CustomerModel).filter(
        CustomerModel.id == customer_id,
        CustomerModel.organization_id == ctx.org_id
    ).first()
    
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    if payload.company_name: customer.company_name = payload.company_name.strip()
    if payload.contact_email: customer.contact_email = payload.contact_email.strip().lower()
    if payload.phone_number is not None: customer.phone_number = payload.phone_number.strip()
    if payload.pricing_tier: customer.pricing_tier = payload.pricing_tier.upper()
    if payload.discount_rate is not None: customer.discount_rate = Decimal(str(payload.discount_rate))

    db.commit()
    return {"status": "success", "data": customer}

# --- 10. TENANT-SCOPED ORDERS ---
@app.get("/orders", tags=["Orders"])
def get_orders(ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    orders = db.query(OrderModel).filter(OrderModel.organization_id == ctx.org_id).all()
    return {"data": orders}

@app.get("/orders/{order_id}", tags=["Orders"])
def get_order_by_id(order_id: str, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    order = db.query(OrderModel).filter(
        OrderModel.id == order_id,
        OrderModel.organization_id == ctx.org_id
    ).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order

@app.patch("/orders/{order_id}/status", tags=["Orders"])
def update_order_status(order_id: str, payload: StatusUpdatePayload, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    order = db.query(OrderModel).filter(
        OrderModel.id == order_id,
        OrderModel.organization_id == ctx.org_id
    ).first()
    
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    _old = order.status
    order.status = payload.status.upper()
    db.add(OrderStatusHistoryModel(order_id=order.id, organization_id=ctx.org_id, from_status=_old, to_status=payload.status.upper(), changed_by=ctx.user_id, note="Status updated"))
    db.commit()
    return {"status": "success", "order_id": order.id, "new_status": order.status}

@app.post("/orders/{order_id}/send-confirmation", tags=["Orders"])
def send_order_confirmation(order_id: str, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    order = db.query(OrderModel).filter(OrderModel.id == order_id, OrderModel.organization_id == ctx.org_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    org = db.query(SupplierUserModel).filter(SupplierUserModel.organization_id == ctx.org_id).first()
    sent = _send_order_confirmation_email(order, org.organization_name if org else "OrderDesk")
    return {"status": "sent" if sent else "skipped", "sent": sent}

@app.post("/orders/{order_id}/ship", tags=["Orders"])
def ship_order(order_id: str, payload: ShipmentPayload, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    order = db.query(OrderModel).filter(OrderModel.id == order_id, OrderModel.organization_id == ctx.org_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if payload.courier: order.courier = payload.courier.strip()
    if payload.tracking_number: order.tracking_number = payload.tracking_number.strip()
    _old = order.status
    order.status = "SHIPPED"
    order.shipped_at = datetime.utcnow()
    db.add(OrderStatusHistoryModel(order_id=order.id, organization_id=ctx.org_id, from_status=_old, to_status="SHIPPED", changed_by=ctx.user_id, note="Shipped"))
    db.commit()
    db.refresh(order)
    org = db.query(SupplierUserModel).filter(SupplierUserModel.organization_id == ctx.org_id).first()
    _send_status_email(order, org.organization_name if org else "OrderDesk", "SHIPPED")
    return {"status": "success", "data": order}

@app.post("/orders/{order_id}/deliver", tags=["Orders"])
def deliver_order(order_id: str, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    order = db.query(OrderModel).filter(OrderModel.id == order_id, OrderModel.organization_id == ctx.org_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    _old = order.status
    order.status = "DELIVERED"
    order.delivered_at = datetime.utcnow()
    db.add(OrderStatusHistoryModel(order_id=order.id, organization_id=ctx.org_id, from_status=_old, to_status="DELIVERED", changed_by=ctx.user_id, note="Delivered"))
    db.commit()
    db.refresh(order)
    org = db.query(SupplierUserModel).filter(SupplierUserModel.organization_id == ctx.org_id).first()
    _send_status_email(order, org.organization_name if org else "OrderDesk", "DELIVERED")
    return {"status": "success", "data": order}

@app.get("/orders/{order_id}/notes", tags=["Orders"])
def list_order_notes(order_id: str, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    order = db.query(OrderModel).filter(OrderModel.id == order_id, OrderModel.organization_id == ctx.org_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    notes = db.query(OrderNoteModel).filter(OrderNoteModel.order_id == order_id).order_by(OrderNoteModel.created_at).all()
    return {"data": [{"id": n.id, "author": n.author, "note": n.note, "created_at": n.created_at} for n in notes]}

@app.post("/orders/{order_id}/notes", tags=["Orders"])
def add_order_note(order_id: str, payload: dict, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    order = db.query(OrderModel).filter(OrderModel.id == order_id, OrderModel.organization_id == ctx.org_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    note = OrderNoteModel(order_id=order_id, organization_id=ctx.org_id, author=payload.get("author"), note=payload.get("note", ""))
    if not note.note:
        raise HTTPException(status_code=400, detail="Note required")
    db.add(note)
    db.commit()
    db.refresh(note)
    return {"status": "success", "data": {"id": note.id, "note": note.note, "created_at": note.created_at}}

@app.get("/orders/{order_id}/history", tags=["Orders"])
def order_history(order_id: str, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    order = db.query(OrderModel).filter(OrderModel.id == order_id, OrderModel.organization_id == ctx.org_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    rows = db.query(OrderStatusHistoryModel).filter(OrderStatusHistoryModel.order_id == order_id).order_by(OrderStatusHistoryModel.changed_at).all()
    return {"data": [{"id": r.id, "from_status": r.from_status, "to_status": r.to_status, "changed_by": r.changed_by, "note": r.note, "changed_at": r.changed_at} for r in rows]}

@app.post("/orders/{order_id}/payment-reminder", tags=["Orders"])
def payment_reminder(order_id: str, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    order = db.query(OrderModel).filter(OrderModel.id == order_id, OrderModel.organization_id == ctx.org_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.payment_status and order.payment_status.upper() == "PAID":
        return {"status": "skipped", "message": "Order already paid."}
    org = db.query(SupplierUserModel).filter(SupplierUserModel.organization_id == ctx.org_id).first()
    sent = _send_payment_reminder_email(order, org.organization_name if org else "OrderDesk")
    return {"status": "sent" if sent else "skipped", "sent": sent}

def _send_order_confirmation_email(order, org_name: str):
    if not SMTP_HOST:
        return False
    recipient = order.client_email
    if not recipient:
        return False
    import smtplib
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["Subject"] = f"Order Confirmation {order.invoice_number} - {org_name}"
    msg["From"] = SMTP_FROM
    msg["To"] = recipient
    body = [
        f"Dear {order.client_name},",
        "",
        f"Thank you for your order with {org_name}.",
        "",
        f"Invoice: {order.invoice_number}",
        f"Status: {order.status}",
        f"Fulfillment: {order.fulfillment_type}",
        f"Total: {float(order.total_amount):.2f} {order.currency}",
        "",
        "You can log in to the portal to view and download your invoice.",
        "",
        "Regards,",
        org_name,
    ]
    msg.set_content("\n".join(body))
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls()
            if SMTP_USER and SMTP_PASS:
                server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        return True
    except Exception:
        return False

def _send_status_email(order, org_name: str, status: str):
    if not SMTP_HOST or not order.client_email:
        return False
    import smtplib
    from email.message import EmailMessage
    msg = EmailMessage()
    subject_status = "shipped" if status == "SHIPPED" else "delivered"
    msg["Subject"] = f"Your order {order.invoice_number} has been {subject_status} - {org_name}"
    msg["From"] = SMTP_FROM
    msg["To"] = order.client_email
    lines = [
        f"Dear {order.client_name},",
        "",
        f"Your order {order.invoice_number} has been {subject_status}.",
    ]
    if status == "SHIPPED":
        if order.courier:
            lines.append(f"Courier: {order.courier}")
        if order.tracking_number:
            lines.append(f"Tracking number: {order.tracking_number}")
    lines += ["", f"Total: {float(order.total_amount):.2f} {order.currency}", "", "Regards,", org_name]
    msg.set_content("\n".join(lines))
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls()
            if SMTP_USER and SMTP_PASS:
                server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        return True
    except Exception:
        return False

def _send_payment_reminder_email(order, org_name: str):
    if not SMTP_HOST or not order.client_email:
        return False
    import smtplib
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["Subject"] = f"Payment reminder: Invoice {order.invoice_number} - {org_name}"
    msg["From"] = SMTP_FROM
    msg["To"] = order.client_email
    body = [
        f"Dear {order.client_name},",
        "",
        f"This is a reminder that invoice {order.invoice_number} for {float(order.total_amount):.2f} {order.currency} is still unpaid.",
        "",
        f"Status: {order.payment_status}",
        "",
        "Please arrange payment at your earliest convenience.",
        "",
        "Regards,",
        org_name,
    ]
    msg.set_content("\n".join(body))
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls()
            if SMTP_USER and SMTP_PASS:
                server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        return True
    except Exception:
        return False

@app.post("/orders", tags=["Orders"])
def create_order(payload: OrderCreatePayload, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    subtotal = Decimal("0.00")
    for it in payload.items:
        subtotal += Decimal(str(it.unit_price)) * it.quantity
    shipping = Decimal(str(payload.shipping_fee))
    if payload.tax_rate is None:
        setting = db.query(SettingsModel).filter(SettingsModel.organization_id == ctx.org_id).first()
        tr = setting.tax_rate if setting else Decimal("0.00")
    else:
        tr = Decimal(str(payload.tax_rate))
    tax_amount = (subtotal * tr / Decimal("100")).quantize(Decimal("0.01"))
    total = subtotal + shipping + tax_amount

    order = OrderModel(
        organization_id=ctx.org_id,
        client_name=payload.client_name.strip(),
        client_email=(payload.client_email or "client@orderdesk.co.zw").strip().lower(),
        fulfillment_type=payload.fulfillment_type.upper(),
        delivery_city=payload.delivery_city,
        item_subtotal=subtotal,
        shipping_fee=shipping,
        tax_rate=tr,
        tax_amount=tax_amount,
        total_amount=total,
        currency=payload.currency.upper(),
        status="PENDING",
        payment_status="UNPAID",
    )
    db.add(order)
    db.flush()

    for it in payload.items:
        line_total = Decimal(str(it.unit_price)) * it.quantity
        db.add(OrderItemModel(
            order_id=order.id,
            organization_id=ctx.org_id,
            sku=it.sku.strip().upper(),
            title=it.title.strip(),
            quantity=it.quantity,
            unit_price=Decimal(str(it.unit_price)),
            line_total=line_total,
        ))
    db.commit()
    db.refresh(order)
    return {"status": "success", "data": order}

@app.get("/orders/{order_id}/invoice", tags=["Orders"])
def get_invoice(order_id: str, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    order = db.query(OrderModel).filter(
        OrderModel.id == order_id,
        OrderModel.organization_id == ctx.org_id,
    ).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    items = db.query(OrderItemModel).filter(OrderItemModel.order_id == order.id).all()
    org = db.query(SupplierUserModel).filter(SupplierUserModel.organization_id == ctx.org_id).first()

    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet
    from io import BytesIO

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, topMargin=40, bottomMargin=40)
    styles = getSampleStyleSheet()
    story = []

    story.append(Paragraph(f"<b>{org.organization_name if org else 'OrderDesk Supplier'}</b>", styles["Title"]))
    story.append(Paragraph("INVOICE", styles["Heading2"]))
    story.append(Spacer(1, 12))

    meta = [
        ["Invoice #", order.invoice_number],
        ["Date", order.created_at.strftime("%Y-%m-%d") if order.created_at else ""],
        ["Client", order.client_name],
        ["Email", order.client_email],
        ["Fulfillment", order.fulfillment_type],
        ["Status", order.status],
        ["Payment", order.payment_status],
    ]
    t = Table(meta, colWidths=[120, 360])
    t.setStyle(TableStyle([("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 10)]))
    story.append(t)
    story.append(Spacer(1, 16))

    rows = [["SKU", "Description", "Qty", "Unit Price", "Line Total"]]
    for it in items:
        rows.append([it.sku, it.title, str(it.quantity), f"{float(it.unit_price):.2f}", f"{float(it.line_total):.2f}"])
    rows.append(["", "", "", "Subtotal", f"{float(order.item_subtotal):.2f}"])
    rows.append(["", "", "", "Shipping", f"{float(order.shipping_fee):.2f}"])
    if float(order.tax_amount or 0) > 0:
        rows.append(["", "", "", f"Tax ({float(order.tax_rate)}%)", f"{float(order.tax_amount):.2f}"])
    rows.append(["", "", "", "TOTAL", f"{float(order.total_amount):.2f} {order.currency}"])

    table = Table(rows, colWidths=[80, 200, 50, 90, 90])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4f46e5")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -2), 0.5, colors.grey),
        ("ALIGN", (2, 1), (-1, -1), "RIGHT"),
        ("FONTNAME", (3, -1), (-1, -1), "Helvetica-Bold"),
    ]))
    story.append(table)
    story.append(Spacer(1, 24))
    story.append(Paragraph("Thank you for your business.", styles["Italic"]))

    doc.build(story)
    pdf_bytes = buffer.getvalue()
    buffer.close()

    from fastapi.responses import Response as FastAPIResponse
    return FastAPIResponse(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="invoice-{order.invoice_number}.pdf"'},
    )

# --- 11. HARDENED PAYNOW BILLING & WEBHOOK ---
@app.post("/billing/subscribe", tags=["Billing"])
def initiate_paynow_subscription(req: SubscribeRequest, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    if not PAYNOW_INTEGRATION_ID or not PAYNOW_INTEGRATION_KEY:
        raise HTTPException(status_code=503, detail="Paynow is not configured for production. Set PAYNOW_INTEGRATION_ID and PAYNOW_INTEGRATION_KEY.")

    amount = Decimal("49.00") if "MONTH" in req.plan_tier.upper() else Decimal("499.00")
    internal_ref = f"SUB-{uuid.uuid4().hex[:12].upper()}"

    payment = PaymentModel(
        organization_id=ctx.org_id,
        reference=internal_ref,
        expected_amount=amount,
        status="CREATED"
    )
    db.add(payment)
    db.commit()

    user = db.query(SupplierUserModel).filter(SupplierUserModel.id == ctx.user_id).first()
    paynow_payment = paynow.create_payment(internal_ref, user.email if user else "billing@orderdesk.co.zw")
    paynow_payment.add("OrderDesk SaaS Subscription", float(amount))
    response = paynow.send(paynow_payment)

    if response.success:
        return {"status": "success", "redirect_url": response.redirect_url}
    detail = "Failed to initiate Paynow checkout."
    try:
        detail = f"Failed to initiate Paynow checkout: {response.data}"
    except Exception:
        pass
    raise HTTPException(status_code=400, detail=detail)

def _paynow_hash(items: list) -> str:
    concat = "".join(v for k, v in items if k.lower() != "hash")
    return hashlib.sha512((concat + PAYNOW_INTEGRATION_KEY).encode()).hexdigest().upper()

def _hash_ok(items: list) -> bool:
    got = next((v for k, v in items if k.lower() == "hash"), "")
    return hmac.compare_digest(_paynow_hash(items), got.upper())

def _confirm_via_poll(pollurl: str, reference: str) -> dict:
    if not pollurl.startswith("https://www.paynow.co.zw/"):
        raise HTTPException(status_code=400, detail="Invalid poll URL origin")
    r = requests.post(pollurl, timeout=10)
    items = parse_qsl(r.text, keep_blank_values=True)
    if not _hash_ok(items):
        raise HTTPException(status_code=400, detail="Poll hash mismatch")
    data = dict((k.lower(), v) for k, v in items)
    if data.get("reference") != reference:
        raise HTTPException(status_code=400, detail="Poll reference mismatch")
    return data

@app.post("/webhooks/paynow", tags=["Webhooks"])
async def paynow_webhook(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    items = [(k, str(v)) for k, v in form.multi_items()]
    
    if not _hash_ok(items):
        raise HTTPException(status_code=400, detail="Invalid payload signature")

    data = {k.lower(): v for k, v in items}
    ref = data.get("reference", "")

    payment = db.query(PaymentModel).filter(
        PaymentModel.reference == ref
    ).with_for_update().first()

    if not payment or payment.status == "PAID":
        return Response(content="OK", media_type="text/plain")

    confirmed = _confirm_via_poll(data.get("pollurl", ""), ref)
    status = confirmed.get("status", "").lower()

    if status in {"paid", "awaiting delivery", "delivered"}:
        paid = Decimal(confirmed.get("amount", "0")).quantize(Decimal("0.01"), ROUND_HALF_UP)
        if paid < payment.expected_amount:
            payment.status = "FAILED"
            db.commit()
            return Response(content="OK", media_type="text/plain")
        
        payment.status = "PAID"
        payment.paid_at = datetime.utcnow()
        payment.paynow_reference = confirmed.get("paynowreference")
        db.commit()
    elif status in {"cancelled", "refunded", "disputed"}:
        payment.status = "CANCELLED"
        db.commit()

    return Response(content="OK", media_type="text/plain")

# --- 11B. USER MANAGEMENT & RBAC ---
@app.get("/users", tags=["Users"])
def list_users(ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    require_role(ctx, {"ADMIN"})
    users = db.query(SupplierUserModel).filter(SupplierUserModel.organization_id == ctx.org_id).all()
    return {"data": [{"id": u.id, "email": u.email, "organization_name": u.organization_name, "role": u.role, "created_at": u.created_at} for u in users]}

@app.post("/users", tags=["Users"])
def create_user(payload: UserCreatePayload, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    require_role(ctx, {"ADMIN"})
    if db.query(SupplierUserModel).filter(SupplierUserModel.email == payload.email.strip().lower()).first():
        raise HTTPException(status_code=400, detail="An account with this email already exists.")
    if payload.role not in {"ADMIN", "SALES", "WAREHOUSE", "ACCOUNTANT"}:
        raise HTTPException(status_code=400, detail="Invalid role.")
    org_name = payload.organization_name.strip() if payload.organization_name else None
    if not org_name:
        org = db.query(SupplierUserModel).filter(SupplierUserModel.organization_id == ctx.org_id).first()
        org_name = org.organization_name if org else "Organization"
    user = SupplierUserModel(
        organization_id=ctx.org_id,
        organization_name=org_name,
        email=payload.email.strip().lower(),
        password=ph.hash(payload.password),
        security_passkey=generate_unique_passkey(),
        role=payload.role.upper(),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"status": "success", "data": {"id": user.id, "email": user.email, "role": user.role}}

@app.patch("/users/{user_id}/role", tags=["Users"])
def update_user_role(user_id: str, payload: UserRoleUpdatePayload, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    require_role(ctx, {"ADMIN"})
    if payload.role not in {"ADMIN", "SALES", "WAREHOUSE", "ACCOUNTANT"}:
        raise HTTPException(status_code=400, detail="Invalid role.")
    if user_id == ctx.user_id:
        raise HTTPException(status_code=400, detail="Cannot change your own role.")
    user = db.query(SupplierUserModel).filter(SupplierUserModel.id == user_id, SupplierUserModel.organization_id == ctx.org_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.role = payload.role.upper()
    db.commit()
    return {"status": "success", "user_id": user.id, "role": user.role}

@app.delete("/users/{user_id}", tags=["Users"])
def delete_user(user_id: str, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    require_role(ctx, {"ADMIN"})
    if user_id == ctx.user_id:
        raise HTTPException(status_code=400, detail="Cannot delete yourself.")
    user = db.query(SupplierUserModel).filter(SupplierUserModel.id == user_id, SupplierUserModel.organization_id == ctx.org_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    db.delete(user)
    db.commit()
    return {"status": "success", "message": "User deleted."}

# --- 11C. CSV EXPORT ---
@app.get("/export/orders", tags=["Export"])
def export_orders(ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    orders = db.query(OrderModel).filter(OrderModel.organization_id == ctx.org_id).all()
    import csv as _csv, io as _io
    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow(["invoice_number", "client_name", "client_email", "status", "payment_status", "fulfillment_type", "item_subtotal", "shipping_fee", "tax_rate", "tax_amount", "total_amount", "currency", "courier", "tracking_number", "created_at"])
    for o in orders:
        w.writerow([o.invoice_number, o.client_name, o.client_email, o.status, o.payment_status, o.fulfillment_type, float(o.item_subtotal), float(o.shipping_fee), float(o.tax_rate), float(o.tax_amount), float(o.total_amount), o.currency, o.courier, o.tracking_number, o.created_at])
    from fastapi.responses import Response as R
    return R(content=buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="orders.csv"'})

@app.get("/export/products", tags=["Export"])
def export_products(ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    products = db.query(ProductModel).filter(ProductModel.organization_id == ctx.org_id).all()
    import csv as _csv, io as _io
    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow(["sku", "title", "category", "wholesale_price", "stock_quantity", "reorder_level", "weight_kg"])
    for p in products:
        w.writerow([p.sku, p.title, p.category, float(p.wholesale_price), p.stock_quantity, p.reorder_level, float(p.weight_kg)])
    from fastapi.responses import Response as R
    return R(content=buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="products.csv"'})

# --- 12. SYSTEM & HEALTH ---
@app.get("/", tags=["Health"])
def health_check():
    return {"status": "online", "system": "OrderDesk Production B2B Engine v6.0"}

@app.get("/settings/rate", tags=["Settings"])
def get_exchange_rate(ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    setting = db.query(SettingsModel).filter(SettingsModel.organization_id == ctx.org_id).first()
    if not setting:
        setting = SettingsModel(organization_id=ctx.org_id, zig_exchange_rate=Decimal("26.5000"))
        db.add(setting)
        db.commit()
        db.refresh(setting)
    return {"status": "success", "zig_exchange_rate": float(setting.zig_exchange_rate), "tax_rate": float(setting.tax_rate)}

@app.post("/settings/rate", tags=["Settings"])
def update_exchange_rate(payload: RateUpdatePayload, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    setting = db.query(SettingsModel).filter(SettingsModel.organization_id == ctx.org_id).first()
    rate_val = Decimal(str(payload.zig_exchange_rate))
    if not setting:
        setting = SettingsModel(organization_id=ctx.org_id, zig_exchange_rate=rate_val)
        db.add(setting)
    else:
        setting.zig_exchange_rate = rate_val
    db.commit()
    return {"status": "success", "zig_exchange_rate": float(setting.zig_exchange_rate)}