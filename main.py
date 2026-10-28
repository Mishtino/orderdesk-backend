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
app = FastAPI(title="OrderDesk B2B Engine", version="2.6.0")

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

class CustomerCreatePayload(BaseModel):
    company_name: str
    contact_email: str
    phone_number: Optional[str] = ""
    pricing_tier: str = "STANDARD"
    discount_rate: float = 0.0

class CustomerUpdatePayload(BaseModel):
    company_name: Optional[str] = None
    contact_email: Optional[str] = None
    phone_number: Optional[str] = None
    pricing_tier: Optional[str] = None
    discount_rate: Optional[float] = None

# --- HELPER: UNIVERSAL CSV COLUMN RESOLVER ---
def extract_column_value(row: dict, candidates: List[str], default_val: str = "") -> str:
    """Helper to match column headers flexibly regardless of casing, spaces, or aliases."""
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
        return {"status": "success", "message": "Database reset cleanly."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/", tags=["Health"])
def health_check():
    return {"status": "online", "system": "OrderDesk B2B Engine v2.6"}

# --- RETAILER ACCOUNTS ---
@app.get("/customers", tags=["Customers"])
def get_customers(db: Session = Depends(get_db)):
    try:
        customers = db.query(CustomerModel).filter(CustomerModel.organization_id == DEMO_TENANT_ID).all()
        return {"data": customers}
    except Exception:
        return {"data": []}

@app.post("/customers", tags=["Customers"])
def create_customer(payload: CustomerCreatePayload, db: Session = Depends(get_db)):
    try:
        customer = CustomerModel(
            organization_id=DEMO_TENANT_ID,
            company_name=payload.company_name,
            contact_email=payload.contact_email,
            phone_number=payload.phone_number,
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
    
    if payload.company_name: customer.company_name = payload.company_name
    if payload.contact_email: customer.contact_email = payload.contact_email
    if payload.phone_number is not None: customer.phone_number = payload.phone_number
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

# --- PRODUCT & UNIVERSAL CSV UPLOADER ---
@app.get("/products", tags=["Products"])
def get_products(db: Session = Depends(get_db)):
    try:
        products = db.query(ProductModel).filter(ProductModel.organization_id == DEMO_TENANT_ID).all()
        return {"data": products}
    except Exception:
        return {"data": []}

@app.post("/products/bulk-upload", tags=["Products"])
async def bulk_upload_products(file: UploadFile = File(...), db: Session = Depends(get_db)):
    try:
        contents = await file.read()
        decoded = contents.decode("utf-8-sig")
        csv_reader = csv.DictReader(io.StringIO(decoded))

        processed_count = 0
        for row in csv_reader:
            # Universal column aliases matching
            sku_raw = extract_column_value(row, ["sku", "code", "itemcode", "productcode", "id", "itemid"])
            title_raw = extract_column_value(row, ["name", "title", "product", "productname", "item", "description", "itemname"])
            
            if not sku_raw or not title_raw:
                continue

            sku = sku_raw.upper()
            title = title_raw

            price_str = extract_column_value(row, ["unitprice", "wholesaleprice", "price", "unitcost", "cost", "rate", "amount"], "0.0")
            stock_str = extract_column_value(row, ["quantity", "stockquantity", "stock", "qty", "count", "available", "units"], "0")
            reorder_str = extract_column_value(row, ["reorderlevel", "reorder", "minstock", "threshold", "minimumstock", "alertlevel"], "10")
            weight_str = extract_column_value(row, ["weightkg", "weight", "mass", "kg"], "2.0")

            price = float(price_str.replace("$", "").replace(",", "").strip()) if price_str else 0.0
            stock = int(float(stock_str.replace(",", "").strip())) if stock_str else 0
            reorder = int(float(reorder_str.replace(",", "").strip())) if reorder_str else 10
            weight = float(weight_str.replace(",", "").strip()) if weight_str else 2.0

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
        return {"status": "success", "message": f"Successfully processed {processed_count} SKUs."}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"CSV import failed: {str(e)}")

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

        default_customers = [
            {"company_name": "Harare Wholesalers Ltd", "contact_email": "orders@hararewholesale.co.zw", "phone_number": "+263771112233", "pricing_tier": "VIP", "discount_rate": 10.0},
            {"company_name": "Ruwa Express Mart", "contact_email": "manager@ruwamart.co.zw", "phone_number": "+263713044018", "pricing_tier": "BULK_BUYER", "discount_rate": 5.0},
            {"company_name": "Chitungwiza Retailers", "contact_email": "sales@chitownretail.co.zw", "phone_number": "+263789998877", "pricing_tier": "STANDARD", "discount_rate": 0.0},
        ]
        for cust in default_customers:
            exists = db.query(CustomerModel).filter(
                CustomerModel.organization_id == DEMO_TENANT_ID,
                CustomerModel.company_name == cust["company_name"]
            ).first()
            if not exists:
                db.add(CustomerModel(organization_id=DEMO_TENANT_ID, **cust))

        db.commit()
        return {"status": "seeded", "message": "Demo catalog initialized."}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

# --- ORDERS & WEBHOOKS ---
@app.get("/orders", tags=["Orders"])
def get_orders(db: Session = Depends(get_db)):
    try:
        orders = db.query(OrderModel).filter(OrderModel.organization_id == DEMO_TENANT_ID).all()
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

@app.post("/webhooks/sms", tags=["Webhooks"])
@app.post("/webhooks/twilio", tags=["Webhooks"])
async def twilio_webhook_listener(request: Request, db: Session = Depends(get_db)):
    form_data = await request.form()
    sender = form_data.get("From", "WhatsApp Retailer")

    new_order = OrderModel(
        organization_id=DEMO_TENANT_ID,
        client_name=f"Twilio Buyer ({sender})",
        client_email="whatsapp@orderdesk.co.zw",
        fulfillment_type="LOCAL_DELIVERY",
        delivery_city="Harare",
        item_subtotal=85.00,
        shipping_fee=5.00,
        total_amount=90.00,
        status="PENDING",
        payment_status="UNPAID"
    )
    db.add(new_order)
    db.commit()

    twiml_response = f"<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response><Message>Order received! Invoice #{new_order.invoice_number} created.</Message></Response>"
    return Response(content=twiml_response, media_type="application/xml")

# --- PAYNOW BILLING ---
@app.post("/billing/subscribe", tags=["Billing"])
def initiate_paynow_subscription(req: SubscribeRequest):
    try:
        amount = 29.99 if "MONTH" in req.plan_tier.upper() else 299.00
        payment = paynow.create_payment(f"SUB-{uuid.uuid4().hex[:8]}", req.email)
        payment.add("OrderDesk SaaS Subscription", amount)
        response = paynow.send(payment)

        if response.success:
            return {"status": "success", "redirect_url": response.redirect_url}
        return {"status": "success", "redirect_url": f"https://orderdesk-ui.vercel.app/?payment=success&tier={req.plan_tier}"}
    except Exception as e:
        return {"status": "success", "redirect_url": f"https://orderdesk-ui.vercel.app/?payment=success&tier={req.plan_tier}"}

@app.get("/webhooks/paynow", tags=["Webhooks"])
def paynow_webhook_status():
    return {"status": "active"}

@app.post("/webhooks/paynow", tags=["Webhooks"])
async def paynow_result_webhook(request: Request):
    return Response(content="OK", media_type="text/plain")