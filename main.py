import os
import uuid
import csv
import io
import random
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from typing import Optional, List, Dict

from fastapi import FastAPI, Depends, HTTPException, Form, Response, UploadFile, File, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import create_engine, Column, String, Float, Integer, DateTime, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from paynow import Paynow

# --- DATABASE SETUP ---
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

# --- MODELS ---
class SupplierUserModel(Base):
    __tablename__ = "supplier_users"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(String, nullable=False, index=True)
    organization_name = Column(String, nullable=False)
    email = Column(String, unique=True, nullable=False, index=True)
    is_verified = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class VerificationCodeModel(Base):
    __tablename__ = "verification_codes"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    email = Column(String, nullable=False, index=True)
    code = Column(String, nullable=False)
    expires_at = Column(DateTime, nullable=False)
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

Base.metadata.create_all(bind=engine)

# --- SECURE SSL SMTP DISPATCH ---
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SENDER_EMAIL = os.getenv("SENDER_EMAIL", SMTP_USERNAME or "noreply@orderdesk.co.zw")

def dispatch_email_sync(target_email: str, code: str):
    if not SMTP_USERNAME or not SMTP_PASSWORD:
        print(f"SMTP NOT CONFIGURED. CODE FOR {target_email}: {code}")
        return

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Your OrderDesk Verification Code: {code}"
        msg["From"] = f"OrderDesk Security <{SENDER_EMAIL}>"
        msg["To"] = target_email

        html_body = f"""
        <html>
            <body style="font-family: Arial, sans-serif; background-color: #f8fafc; padding: 20px;">
                <div style="max-width: 500px; margin: 0 auto; background: #ffffff; padding: 30px; border-radius: 16px; border: 1px solid #e2e8f0;">
                    <h2 style="color: #0f172a; margin-top: 0;">OrderDesk B2B Verification</h2>
                    <p style="color: #475569; font-size: 14px;">Use the 6-digit code below to log in:</p>
                    <div style="background-color: #e0e7ff; color: #4338ca; font-size: 28px; font-weight: bold; text-align: center; padding: 15px; border-radius: 12px; letter-spacing: 6px; margin: 20px 0;">
                        {code}
                    </div>
                </div>
            </body>
        </html>
        """
        msg.attach(MIMEText(html_body, "html"))

        # Try SSL Port 465 first for cloud servers, fallback to TLS 587
        try:
            server = smtplib.SMTP_SSL(SMTP_SERVER, 465, timeout=5)
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(SENDER_EMAIL, target_email, msg.as_string())
            server.quit()
        except Exception:
            server = smtplib.SMTP(SMTP_SERVER, 587, timeout=5)
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(SENDER_EMAIL, target_email, msg.as_string())
            server.quit()
            
        print(f"SUCCESS: Email sent to {target_email}")
    except Exception as e:
        print(f"SMTP Email Delivery Error: {str(e)}")

# --- FASTAPI APP ---
app = FastAPI(title="OrderDesk B2B Engine", version="4.7.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    if request.method == "OPTIONS":
        response = Response(status_code=200)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "*"
        response.headers["Access-Control-Allow-Headers"] = "*"
        return response

    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    return response

# --- SCHEMAS ---
class SendCodePayload(BaseModel):
    email: EmailStr
    org_name: Optional[str] = "Supplier Org"

class VerifyCodePayload(BaseModel):
    email: EmailStr
    code: str = Field(..., min_length=6, max_length=6)
    org_name: Optional[str] = "Supplier Org"

# --- AUTH ROUTES ---
@app.post("/auth/send-code", tags=["Auth"])
def send_verification_code(payload: SendCodePayload, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    clean_email = payload.email.strip().lower()
    generated_code = f"{random.randint(100000, 999999)}"
    expires_at = datetime.utcnow() + timedelta(minutes=15)

    db.query(VerificationCodeModel).filter(VerificationCodeModel.email == clean_email).delete()

    record = VerificationCodeModel(
        email=clean_email,
        code=generated_code,
        expires_at=expires_at
    )
    db.add(record)
    db.commit()

    background_tasks.add_task(dispatch_email_sync, clean_email, generated_code)

    return {
        "status": "success",
        "message": f"Verification code sent to {clean_email}.",
        "demo_code": generated_code
    }

@app.post("/auth/verify-code", tags=["Auth"])
def verify_code(payload: VerifyCodePayload, db: Session = Depends(get_db)):
    clean_email = payload.email.strip().lower()
    submitted_code = payload.code.strip()

    record = db.query(VerificationCodeModel).filter(
        VerificationCodeModel.email == clean_email,
        VerificationCodeModel.code == submitted_code
    ).first()

    if not record:
        raise HTTPException(status_code=400, detail="Invalid verification code.")

    if datetime.utcnow() > record.expires_at:
        db.delete(record)
        db.commit()
        raise HTTPException(status_code=400, detail="Verification code has expired.")

    user = db.query(SupplierUserModel).filter(SupplierUserModel.email == clean_email).first()
    if not user:
        org_id = str(uuid.uuid4())
        user = SupplierUserModel(
            organization_id=org_id,
            organization_name=payload.org_name or "Supplier Org",
            email=clean_email,
            is_verified=True
        )
        db.add(user)
    else:
        user.is_verified = True

    db.delete(record)
    db.commit()

    return {
        "status": "success",
        "message": "Email verified successfully.",
        "organization_id": user.organization_id,
        "organization_name": user.organization_name,
        "email": user.email
    }