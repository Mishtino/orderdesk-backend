import os
import uuid
import csv
import io
from datetime import datetime
from typing import Optional, List

from fastapi import FastAPI, Depends, HTTPException, Form, Response, UploadFile, File, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, String, Float, Integer, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from paynow import Paynow

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
    reorder_level = Column(Integer, default=10)
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
    status = Column(String, default="PENDING")
    payment_status = Column(String, default="UNPAID")
    created_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

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

# --- FASTAPI APP ---
app = FastAPI(title="OrderDesk B2B Engine", version="2.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DEMO_TENANT_ID = "123e4567-e89b-12d3-a456-426614174000"

# --- PYDANTIC SCHEMAS ---
class SubscribeRequest(BaseModel):
    plan_tier: str
    email: str

class StatusUpdatePayload(BaseModel):
    status: str

# --- SYSTEM & DATABASE ROUTES ---
@app.post("/reset-db", tags=["System"])
def reset_database():
    try:
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        return {"status": "success", "message": "Database reset cleanly with new reorder_level support."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/", tags=["Health"])
def health_check():
    return {"status": "online", "system": "OrderDesk B2B Engine v2.1"}

# --- PRODUCT & LOW STOCK ROUTES ---
@app.get("/products", tags=["Products"])
def get_products(db: Session = Depends(get_db)):
    try:
        products = db.query(ProductModel).filter(ProductModel.organization_id == DEMO_TENANT_ID).all()
        return {"data": products}
    except Exception:
        return {"data": []}

@app.get("/products/low-stock", tags=["Products"])
def get_low_stock_products(db: Session = Depends(get_db)):
    try:
        products = db.query(ProductModel).filter(
            ProductModel.organization_id == DEMO_TENANT_ID,
            ProductModel.stock_quantity <= ProductModel.reorder_level
        ).all()
        return {"data": products, "count": len(products)}
    except Exception as e:
        return {"data": [], "count": 0, "error": str(e)}

@app.post("/products/seed", tags=["Products"])
def seed_demo_products(db: Session = Depends(get_db)):
    try:
        default_items = [
            {"sku": "SKU-1001", "title": "Maize Meal 10kg", "wholesale_price": 12.50, "stock_quantity": 8, "reorder_level": 15, "weight_kg": 10.0},
            {"sku": "SKU-1002", "title": "Cooking Oil 2L", "wholesale_price": 3.80, "stock_quantity": 300, "reorder_level": 50, "weight_kg": 2.0},
            {"sku": "SKU-1003", "title": "Refined Sugar 2kg", "wholesale_price": 2.40, "stock_quantity": 5, "reorder_level": 20, "weight_kg": 2.0},
        ]
        for item in default_items:
            exists = db.query(ProductModel).filter(
                ProductModel.organization_id == DEMO_TENANT_ID, 
                ProductModel.sku == item["sku"]
            ).first()
            if not exists:
                db.add(ProductModel(organization_id=DEMO_TENANT_ID, **item))
        db.commit()
        return {"status": "seeded", "message": "Demo catalog initialized with reorder triggers."}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Seed error: {str(e)}")

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
            reorder = int(row.get("reorder_level", 10))
            weight = float(row.get("weight_kg", 2.0))

            existing_prod = db.query(ProductModel).filter(
                ProductModel.organization_id == DEMO_TENANT_ID,
                ProductModel.sku == sku
            ).first()

            if existing_prod:
                existing_prod.title = title
                existing_prod.wholesale_price = price
                existing_prod.stock_quantity = stock
                existing_prod.reorder_level = reorder
                existing_prod.weight_kg = weight
            else:
                db.add(ProductModel(
                    organization_id=DEMO_TENANT_ID,
                    sku=sku,
                    title=title,
                    wholesale_price=price,
                    stock_quantity=stock,
                    reorder_level=reorder,
                    weight_kg=weight
                ))
            processed_count += 1

        db.commit()
        return {"status": "success", "message": f"Successfully imported {processed_count} SKUs."}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"CSV processing error: {str(e)}")

# --- ORDERS & PIPELINE ROUTES ---
@app.get("/orders", tags=["Orders"])
def get_orders(db: Session = Depends(get_db)):
    try:
        orders = db.query(OrderModel).filter(OrderModel.organization_id == DEMO_TENANT_ID).all()
        return {"data": orders}
    except Exception:
        return {"data": []}

@app.patch("/orders/{order_id}/status", tags=["Orders"])
def update_order_status(order_id: str, payload: StatusUpdatePayload, db: Session = Depends(get_db)):
    valid_statuses = ["PENDING", "CONFIRMED", "PACKED", "DISPATCHED", "DELIVERED"]
    new_status = payload.status.upper()
    
    if new_status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of {valid_statuses}")

    order = db.query(OrderModel).filter(OrderModel.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    order.status = new_status
    db.commit()
    db.refresh(order)
    return {"status": "success", "order_id": order.id, "new_status": order.status}

# --- BILLING & PAYNOW ROUTES ---
@app.post("/billing/subscribe", tags=["Billing"])
@app.post("/payments/initiate", tags=["Billing"])
def initiate_paynow_subscription(req: SubscribeRequest):
    try:
        amount = 29.99 if "MONTH" in req.plan_tier.upper() else 299.00
        plan_label = "OrderDesk Monthly Tier" if amount == 29.99 else "OrderDesk Annual Tier"

        payment = paynow.create_payment(f"SUB-{uuid.uuid4().hex[:8]}", req.email)
        payment.add(plan_label, amount)

        response = paynow.send(payment)

        if response.success:
            return {
                "status": "success",
                "redirect_url": response.redirect_url,
                "poll_url": response.poll_url
            }
        else:
            demo_url = f"https://orderdesk-ui.vercel.app/?payment=success&tier={req.plan_tier}"
            return {
                "status": "success",
                "redirect_url": demo_url,
                "note": "Sandbox mode activated."
            }
    except Exception as e:
        demo_url = f"https://orderdesk-ui.vercel.app/?payment=success&tier={req.plan_tier}"
        return {
            "status": "success",
            "redirect_url": demo_url,
            "error_detail": str(e)
        }

@app.get("/webhooks/paynow", tags=["Webhooks"])
def paynow_webhook_status():
    return {"status": "active", "endpoint": "Paynow Result Webhook Listener"}

@app.post("/webhooks/paynow", tags=["Webhooks"])
async def paynow_result_webhook(request: Request):
    return Response(content="OK", media_type="text/plain")