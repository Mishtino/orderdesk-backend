import os
import uuid
import csv
import io
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, Form, Response, UploadFile, File, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, Column, String, Float, Integer, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session

# --- DATABASE SETUP ---
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./orderdesk.db")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(
    DATABASE_URL, 
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- MODELS ---
class ProductModel(Base):
    __tablename__ = "products"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(String, nullable=False, index=True)
    sku = Column(String, nullable=False, index=True)
    title = Column(String, nullable=False)
    wholesale_price = Column(Float, nullable=False)
    stock_quantity = Column(Integer, default=0)
    weight_kg = Column(Float, default=2.0)
    volume_cbm = Column(Float, default=0.01)

class OrderModel(Base):
    __tablename__ = "orders"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(String, nullable=False, index=True)
    client_name = Column(String, nullable=False)
    fulfillment_type = Column(String, default="PICKUP")
    delivery_city = Column(String, nullable=True)
    item_subtotal = Column(Float, nullable=False)
    shipping_fee = Column(Float, default=0.0)
    total_amount = Column(Float, nullable=False)
    vehicle_type = Column(String, nullable=True)
    status = Column(String, default="PENDING_PAYMENT")
    payment_status = Column(String, default="UNPAID")
    created_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

# --- FASTAPI APP ---
app = FastAPI(title="OrderDesk B2B Engine", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DEMO_TENANT_ID = "123e4567-e89b-12d3-a456-426614174000"

@app.get("/products", tags=["Products"])
def get_products(db: Session = Depends(get_db)):
    try:
        products = db.query(ProductModel).filter(ProductModel.organization_id == DEMO_TENANT_ID).all()
        return {"data": products}
    except Exception as e:
        return {"data": []}

@app.post("/products/seed", tags=["Products"])
def seed_demo_products(db: Session = Depends(get_db)):
    try:
        default_items = [
            {"sku": "SKU-1001", "title": "Maize Meal 10kg", "wholesale_price": 12.50, "stock_quantity": 150, "weight_kg": 10.0},
            {"sku": "SKU-1002", "title": "Cooking Oil 2L", "wholesale_price": 3.80, "stock_quantity": 300, "weight_kg": 2.0},
            {"sku": "SKU-1003", "title": "Refined Sugar 2kg", "wholesale_price": 2.40, "stock_quantity": 200, "weight_kg": 2.0},
        ]
        for item in default_items:
            exists = db.query(ProductModel).filter(
                ProductModel.organization_id == DEMO_TENANT_ID, 
                ProductModel.sku == item["sku"]
            ).first()
            if not exists:
                db.add(ProductModel(organization_id=DEMO_TENANT_ID, **item))
        db.commit()
        return {"status": "seeded", "message": "Demo catalog initialized successfully."}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/products/bulk-upload", tags=["Products"])
def bulk_upload_products(file: UploadFile = File(...), db: Session = Depends(get_db)):
    try:
        content = file.file.read().decode("utf-8")
        csv_reader = csv.DictReader(io.StringIO(content))

        processed_count = 0
        for row in csv_reader:
            sku = row.get("sku", "").strip().upper()
            title = row.get("title", "").strip()
            if not sku or not title:
                continue

            price = float(row.get("wholesale_price", 0.0))
            stock = int(row.get("stock_quantity", 0))
            weight = float(row.get("weight_kg", 2.0))

            existing_prod = db.query(ProductModel).filter(
                ProductModel.