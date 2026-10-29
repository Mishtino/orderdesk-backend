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

_raw_secure = os.getenv("SESSION_COOKIE_SECURE", "auto").lower()
if _raw_secure == "auto":
    SESSION_COOKIE_SECURE = ("localhost" not in FRONTEND_ORIGIN) and FRONTEND_ORIGIN.startswith("https")
else:
    SESSION_COOKIE_SECURE = _raw_secure == "true"

PAYNOW_INTEGRATION_ID = os.getenv("PAYNOW_INTEGRATION_ID", "12345")
PAYNOW_INTEGRATION_KEY = os.getenv("PAYNOW_INTEGRATION_KEY", "sandbox-key")
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

def current_ctx(request: Request) -> Ctx:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]
    
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        claims = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return Ctx(user_id=claims["sub"], org_id=claims["org"])
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Session expired or invalid")

def client_ip(request: Request) -> str:
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
    vehicle_type = Column(String, nullable=True)
    status = Column(String, default="PENDING")
    payment_status = Column(String, default="UNPAID")
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

class SubscribeRequest(BaseModel):
    plan_tier: str = Field(..., max_length=50)

class RateUpdatePayload(BaseModel):
    zig_exchange_rate: float = Field(..., gt=0.0)

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

    order.status = payload.status.upper()
    db.commit()
    return {"status": "success", "order_id": order.id, "new_status": order.status}

# --- 11. HARDENED PAYNOW BILLING & WEBHOOK ---
@app.post("/billing/subscribe", tags=["Billing"])
def initiate_paynow_subscription(req: SubscribeRequest, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    amount = Decimal("29.99") if "MONTH" in req.plan_tier.upper() else Decimal("299.00")
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
    return {"status": "success", "zig_exchange_rate": float(setting.zig_exchange_rate)}

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