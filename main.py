import os
import uuid
import csv
import io
import random
import string
from datetime import datetime
from typing import Optional, List

from fastapi import FastAPI, Depends, HTTPException, Response, UploadFile, File, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import create_engine, Column, String, Float, Integer, DateTime, Boolean, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from paynow import Paynow

# --- DATABASE SETUP (Explicit psycopg2 dialect for PostgreSQL) ---
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

engine = create_engine(DATABASE_URL, **engine_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- HELPER: PASSKEY GENERATOR ---
def generate_unique_passkey() -> str:
    chars = string.ascii_uppercase + string.digits
    part1 = ''.join(random.choices(chars, k=4))
    part2 = ''.join(random.choices(chars, k=4))
    return f"ORD-{part1}-{part2}"

# --- MODELS ---
class SupplierUserModel(Base):
    __tablename__ = "supplier_users"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(String, nullable=False, index=True)
    organization_name = Column(String, nullable=False)
    email = Column(String, unique=True, nullable=False, index=True)
    password = Column(String, nullable=False, default="password")
    security_passkey = Column(String, unique=True, nullable=False, index=True, default=lambda: generate_unique_passkey())
    is_verified = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class ProductModel(Base):
    __tablename__ = "products"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(String, nullable=False, index=True)
    sku = Column(String, nullable=False, index=True)
    title = Column(String, nullable=False)
    category = Column(String, default="General")
    wholesale_price = Column(Float, nullable=False)
    stock_quantity = Column(Integer, default=0)
    reorder_level = Column(Integer, default=10)
    weight_kg = Column(Float, default=2.0)
    volume_cbm = Column(Float, default=0.01)

class CustomerModel(Base):
    __tablename__ = "customers"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(String, nullable=False, index=True)
    company_name = Column(String, nullable=False)
    contact_email = Column(String, nullable=False)
    phone_number = Column(String, nullable=True)
    pricing_tier = Column(String, default="STANDARD")
    discount_rate = Column(Float, default=0.0)
    created_at = Column(DateTime, default=datetime.utcnow)

class OrderModel(Base):
    __tablename__ = "orders"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    invoice_number = Column(String, nullable=False, default=lambda: f"INV-{uuid.uuid4().hex[:6].upper()}")
    organization_id = Column(String, nullable=False, index=True)
    client_name = Column(String, nullable=False)
    client_email = Column(String, default="client@orderdesk.co.zw")
    fulfillment_type = Column(String, default="PICKUP")
    delivery_city = Column(String, nullable=True)
    item_subtotal = Column(Float, nullable=False)
    shipping_fee = Column(Float, default=0.0)
    total_amount = Column(Float, nullable=False)
    currency = Column(String, default="USD")
    vehicle_type = Column(String, nullable=True)
    status = Column(String, default="PENDING")
    payment_status = Column(String, default="UNPAID")
    created_at = Column(DateTime, default=datetime.utcnow)

class SettingsModel(Base):
    __tablename__ = "settings"

    organization_id = Column(String, primary_key=True)
    zig_exchange_rate = Column(Float, default=26.50)

# Synchronize Database Schema
Base.metadata.create_all(bind=engine)

def auto_migrate_schema():
    """Safely adds missing columns to existing PostgreSQL tables."""
    try:
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE supplier_users ADD COLUMN IF NOT EXISTS password VARCHAR DEFAULT 'password';"))
            conn.execute(text("ALTER TABLE supplier_users ADD COLUMN IF NOT EXISTS security_passkey VARCHAR;"))
            conn.commit()
    except Exception as e:
        print(f"Auto-migration note: {str(e)}")

auto_migrate_schema()

# --- PAYNOW CONFIGURATION ---
PAYNOW_INTEGRATION_ID = os.getenv("PAYNOW_INTEGRATION_ID", "12345")
PAYNOW_INTEGRATION_KEY = os.getenv("PAYNOW_INTEGRATION_KEY", "sandbox-key")
RESULT_URL = os.getenv("PAYNOW_RESULT_URL", "https://orderdesk-backend-9gu2.onrender.com/webhooks/paynow")
RETURN_URL = os.getenv("PAYNOW_RETURN_URL", "https://orderdesk-ui.vercel.app/billing?status=success")

paynow = Paynow(
    PAYNOW_INTEGRATION_ID,
    PAYNOW_INTEGRATION_KEY,
    RESULT_URL,
    RETURN_URL
)

# --- FASTAPI APP & CORS ---
app = FastAPI(title="OrderDesk B2B Engine", version="5.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def cors_and_security_middleware(request: Request, call_next):
    if request.method == "OPTIONS":
        response = Response(status_code=200)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Requested-With"
        return response

    response = await call_next(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    return response

# --- SCHEMAS ---
class RegisterPayload(BaseModel):
    org_name: str = Field(..., max_length=150)
    email: EmailStr
    password: str = Field(..., min_length=6)

class LoginPayload(BaseModel):
    email: EmailStr
    password: str

class ResetPasswordPayload(BaseModel):
    email: EmailStr
    security_passkey: str
    new_password: str = Field(..., min_length=6)

class SubscribeRequest(BaseModel):
    plan_tier: str = Field(..., max_length=50)
    email: str = Field(..., max_length=120)

class StatusUpdatePayload(BaseModel):
    status: str = Field(..., max_length=30)

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

class RateUpdatePayload(BaseModel):
    zig_exchange_rate: float = Field(..., gt=0.0)

# --- AUTH & PASSKEY MANAGEMENT ---
@app.post("/auth/register", tags=["Auth"])
def register_supplier(payload: RegisterPayload, db: Session = Depends(get_db)):
    clean_email = payload.email.strip().lower()
    
    existing_user = db.query(SupplierUserModel).filter(SupplierUserModel.email == clean_email).first()
    if existing_user:
        raise HTTPException(status_code=400, detail="An account with this email already exists.")

    org_id = str(uuid.uuid4())
    passkey = generate_unique_passkey()

    user = SupplierUserModel(
        organization_id=org_id,
        organization_name=payload.org_name.strip(),
        email=clean_email,
        password=payload.password,
        security_passkey=passkey
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    return {
        "status": "success",
        "message": "Account created successfully.",
        "organization_id": user.organization_id,
        "organization_name": user.organization_name,
        "email": user.email,
        "security_passkey": user.security_passkey
    }

@app.post("/auth/login", tags=["Auth"])
def login_supplier(payload: LoginPayload, db: Session = Depends(get_db)):
    clean_email = payload.email.strip().lower()
    
    user = db.query(SupplierUserModel).filter(
        SupplierUserModel.email == clean_email,
        SupplierUserModel.password == payload.password
    ).first()

    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    return {
        "status": "success",
        "organization_id": user.organization_id,
        "organization_name": user.organization_name,
        "email": user.email,
        "security_passkey": user.security_passkey or generate_unique_passkey()
    }

@app.post("/auth/reset-password", tags=["Auth"])
def reset_password(payload: ResetPasswordPayload, db: Session = Depends(get_db)):
    clean_email = payload.email.strip().lower()
    clean_passkey = payload.security_passkey.strip().upper()

    user = db.query(SupplierUserModel).filter(
        SupplierUserModel.email == clean_email,
        SupplierUserModel.security_passkey == clean_passkey
    ).first()

    if not user:
        raise HTTPException(status_code=400, detail="Invalid email or security passkey mismatch.")

    user.password = payload.new_password
    db.commit()

    return {
        "status": "success",
        "message": "Password updated successfully. You can now log in."
    }

# --- HELPER: UNIVERSAL CSV RESOLVER ---
def extract_column_value(row: dict, candidates: List[str], default_val: str = "") -> str:
    normalized_row = {str(k).strip().lower().replace("_", "").replace("-", "").replace(" ", ""): str(v).strip() for k, v in row.items() if k and v is not None}
    for candidate in candidates:
        clean_cand = candidate.lower().replace("_", "").replace("-", "").replace(" ", "")
        if clean_cand in normalized_row:
            return normalized_row[clean_cand]
    return default_val

# --- SYSTEM & HEALTH ---
@app.post("/reset-db", tags=["System"])
def reset_database():
    try:
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        auto_migrate_schema()
        return {"status": "success", "message": "Database reset cleanly."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/", tags=["Health"])
def health_check():
    return {"status": "online", "system": "OrderDesk B2B Passkey Engine v5.2"}

@app.get("/settings/rate", tags=["Settings"])
def get_exchange_rate(org_id: str = "default", db: Session = Depends(get_db)):
    setting = db.query(SettingsModel).filter(SettingsModel.organization_id == org_id).first()
    if not setting:
        setting = SettingsModel(organization_id=org_id, zig_exchange_rate=26.50)
        db.add(setting)
        db.commit()
        db.refresh(setting)
    return {"status": "success", "zig_exchange_rate": setting.zig_exchange_rate}

@app.post("/settings/rate", tags=["Settings"])
def update_exchange_rate(payload: RateUpdatePayload, org_id: str = "default", db: Session = Depends(get_db)):
    setting = db.query(SettingsModel).filter(SettingsModel.organization_id == org_id).first()
    if not setting:
        setting = SettingsModel(organization_id=org_id, zig_exchange_rate=payload.zig_exchange_rate)
        db.add(setting)
    else:
        setting.zig_exchange_rate = payload.zig_exchange_rate
    db.commit()
    return {"status": "success", "zig_exchange_rate": setting.zig_exchange_rate}

# --- RETAILER ACCOUNTS ---
@app.get("/customers", tags=["Customers"])
def get_customers(org_id: str = "default", db: Session = Depends(get_db)):
    try:
        customers = db.query(CustomerModel).filter(CustomerModel.organization_id == org_id).all()
        return {"data": customers}
    except Exception:
        return {"data": []}

@app.post("/customers", tags=["Customers"])
def create_customer(payload: CustomerCreatePayload, org_id: str = "default", db: Session = Depends(get_db)):
    try:
        customer = CustomerModel(
            organization_id=org_id,
            company_name=payload.company_name.strip(),
            contact_email=payload.contact_email.strip().lower(),
            phone_number=payload.phone_number.strip(),
            pricing_tier=payload.pricing_tier.upper(),
            discount_rate=payload.discount_rate
        )
        db.add(customer)
        db.commit()
        db.refresh(customer)
        return {"status": "success", "data": customer}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@app.patch("/customers/{customer_id}", tags=["Customers"])
def update_customer(customer_id: str, payload: CustomerUpdatePayload, db: Session = Depends(get_db)):
    customer = db.query(CustomerModel).filter(CustomerModel.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    
    if payload.company_name: customer.company_name = payload.company_name.strip()
    if payload.contact_email: customer.contact_email = payload.contact_email.strip().lower()
    if payload.phone_number is not None: customer.phone_number = payload.phone_number.strip()
    if payload.pricing_tier: customer.pricing_tier = payload.pricing_tier.upper()
    if payload.discount_rate is not None: customer.discount_rate = payload.discount_rate

    db.commit()
    db.refresh(customer)
    return {"status": "success", "data": customer}

@app.delete("/customers/{customer_id}", tags=["Customers"])
def delete_customer(customer_id: str, db: Session = Depends(get_db)):
    customer = db.query(CustomerModel).filter(CustomerModel.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    
    db.delete(customer)
    db.commit()
    return {"status": "success", "message": "Customer deleted successfully"}

# --- PRODUCT & CSV UPLOADER ---
@app.get("/products", tags=["Products"])
def get_products(org_id: str = "default", db: Session = Depends(get_db)):
    try:
        products = db.query(ProductModel).filter(ProductModel.organization_id == org_id).all()
        return {"data": products}
    except Exception:
        return {"data": []}

@app.post("/products/bulk-upload", tags=["Products"])
async def bulk_upload_products(file: UploadFile = File(...), org_id: str = "default", db: Session = Depends(get_db)):
    try:
        contents = await file.read()
        decoded = contents.decode("utf-8-sig")
        csv_reader = csv.DictReader(io.StringIO(decoded))

        processed_count = 0
        for row in csv_reader:
            sku_raw = extract_column_value(row, ["sku", "code", "itemcode", "productcode", "id", "itemid"])
            title_raw = extract_column_value(row, ["name", "title", "product", "productname", "item", "description", "itemname"])
            
            if not sku_raw or not title_raw:
                continue

            sku = sku_raw.upper().strip()
            title = title_raw.strip()
            category = extract_column_value(row, ["category", "cat", "group", "type"], "General").strip()

            price_str = extract_column_value(row, ["unitprice", "wholesaleprice", "price", "unitcost", "cost", "rate", "amount"], "0.0")
            stock_str = extract_column_value(row, ["quantity", "stockquantity", "stock", "qty", "count", "available", "units"], "0")
            reorder_str = extract_column_value(row, ["reorderlevel", "reorder", "minstock", "threshold", "minimumstock", "alertlevel"], "10")
            weight_str = extract_column_value(row, ["weightkg", "weight", "mass", "kg"], "2.0")

            price = float(price_str.replace("$", "").replace(",", "").strip()) if price_str else 0.0
            stock = int(float(stock_str.replace(",", "").strip())) if stock_str else 0
            reorder = int(float(reorder_str.replace(",", "").strip())) if reorder_str else 10
            weight = float(weight_str.replace(",", "").strip()) if weight_str else 2.0

            existing_prod = db.query(ProductModel).filter(
                ProductModel.organization_id == org_id,
                ProductModel.sku == sku
            ).first()

            if existing_prod:
                existing_prod.title = title
                existing_prod.category = category
                existing_prod.wholesale_price = price
                existing_prod.stock_quantity = stock
                existing_prod.reorder_level = reorder
                existing_prod.weight_kg = weight
            else:
                db.add(ProductModel(
                    organization_id=org_id,
                    sku=sku,
                    title=title,
                    category=category,
                    wholesale_price=price,
                    stock_quantity=stock,
                    reorder_level=reorder,
                    weight_kg=weight
                ))
            processed_count += 1

        db.commit()
        return {"status": "success", "message": f"Successfully processed {processed_count} SKUs."}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"CSV import failed: {str(e)}")

# --- ORDERS & PIPELINE ---
@app.get("/orders", tags=["Orders"])
def get_orders(org_id: str = "default", db: Session = Depends(get_db)):
    try:
        orders = db.query(OrderModel).filter(OrderModel.organization_id == org_id).all()
        return {"data": orders}
    except Exception:
        return {"data": []}

@app.patch("/orders/{order_id}/status", tags=["Orders"])
def update_order_status(order_id: str, payload: StatusUpdatePayload, db: Session = Depends(get_db)):
    order = db.query(OrderModel).filter(OrderModel.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    order.status = payload.status.upper()
    db.commit()
    db.refresh(order)
    return {"status": "success", "order_id": order.id, "new_status": order.status}

# --- PAYNOW LIVE CHECKOUT ---
@app.post("/billing/subscribe", tags=["Billing"])
def initiate_paynow_subscription(req: SubscribeRequest):
    try:
        amount = 29.99 if "MONTH" in req.plan_tier.upper() else 299.00
        payment = paynow.create_payment(f"SUB-{uuid.uuid4().hex[:8]}", req.email)
        payment.add("OrderDesk SaaS Subscription", amount)
        response = paynow.send(payment)

        if response.success:
            return {"status": "success", "redirect_url": response.redirect_url}
        raise HTTPException(status_code=400, detail="Failed to initiate Paynow checkout.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/webhooks/paynow", tags=["Webhooks"])
def paynow_webhook_status():
    return {"status": "active"}

@app.post("/webhooks/paynow", tags=["Webhooks"])
async def paynow_result_webhook(request: Request):
    return Response(content="OK", media_type="text/plain")