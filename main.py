import os
import uuid
import csv
import io
from datetime import datetime
from typing import Optional, List

from fastapi import FastAPI, Depends, HTTPException, Form, Response, UploadFile, File, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, String, Float, Integer, DateTime, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from paynow import Paynow

# --- DATABASE SETUP ---
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./orderdesk.db")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- SQLALCHEMY MODELS ---
class OrganizationModel(Base):
    __tablename__ = "organizations"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, nullable=False)
    email = Column(String, unique=True, nullable=False)
    subscription_plan = Column(String, default="FREE_TRIAL")
    subscription_status = Column(String, default="ACTIVE")
    created_at = Column(DateTime, default=datetime.utcnow)

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

class SubscriptionPaymentModel(Base):
    __tablename__ = "subscription_payments"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(String, ForeignKey("organizations.id"), nullable=False)
    amount = Column(Float, nullable=False)
    plan_type = Column(String, nullable=False)
    paynow_poll_url = Column(String, nullable=True)
    paynow_reference = Column(String, nullable=True)
    status = Column(String, default="PENDING")
    created_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

# --- FASTAPI APP DECLARATION (REQUIRED BY UVICORN) ---
app = FastAPI(title="OrderDesk B2B Engine", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- LOGISTICS ENGINE LOGIC ---
def calculate_fulfillment_cost(fulfillment_type: str, destination_city: str, total_weight_kg: float):
    if fulfillment_type.upper() == "PICKUP":
        return 0.00, "CUSTOMER_PICKUP", "Warehouse Pick Up (Free)"
    
    city = destination_city.upper().strip()

    if total_weight_kg <= 35.0:
        base_bike_fee = 3.50 if city == "HARARE" else 5.00
        shipping_fee = round(base_bike_fee + (total_weight_kg * 0.10), 2)
        vehicle_type = "BIKER_EXPRESS"
        details = f"Biker Express Delivery ({total_weight_kg:.1f} kg)"
        return shipping_fee, vehicle_type, details
    else:
        if total_weight_kg <= 300.0:
            vehicle_type = "AISLE_1_TONNE_VAN"
            base_fee, per_kg = 20.0, 0.30
        elif total_weight_kg <= 1500.0:
            vehicle_type = "AISLE_3_TONNE_TRUCK"
            base_fee, per_kg = 45.0, 0.25
        else:
            vehicle_type = "AISLE_HEAVY_FREIGHT_TRUCK"
            base_fee, per_kg = 80.0, 0.20

        city_multiplier = 1.0 if city == "HARARE" else 1.4
        shipping_fee = round((base_fee + (total_weight_kg * per_kg)) * city_multiplier, 2)
        details = f"Aisle Logistics Freight ({vehicle_type} - {total_weight_kg:.1f} kg)"
        return shipping_fee, vehicle_type, details

# --- SCHEMAS ---
class ProductCreate(BaseModel):
    sku: str
    title: str
    wholesale_price: float
    stock_quantity: int
    weight_kg: Optional[float] = 2.0

class SubscriptionRequest(BaseModel):
    plan_type: str
    organization_id: str
    user_email: str

# --- PRODUCT & ORDER ENDPOINTS ---
@app.get("/products", tags=["Products"])
def get_products(db: Session = Depends(get_db)):
    demo_tenant_id = "123e4567-e89b-12d3-a456-426614174000"
    products = db.query(ProductModel).filter(ProductModel.organization_id == demo_tenant_id).all()
    return {"data": products}

@app.post("/products", tags=["Products"])
def create_product(product: ProductCreate, db: Session = Depends(get_db)):
    demo_tenant_id = "123e4567-e89b-12d3-a456-426614174000"
    db_prod = ProductModel(organization_id=demo_tenant_id, **product.dict())
    db.add(db_prod)
    db.commit()
    db.refresh(db_prod)
    return {"message": "Product registered successfully", "data": db_prod}

@app.post("/products/bulk-upload", tags=["Products"])
def bulk_upload_products(file: UploadFile = File(...), db: Session = Depends(get_db)):
    if not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="File must be a valid .csv spreadsheet.")

    demo_tenant_id = "123e4567-e89b-12d3-a456-426614174000"

    try:
        content = file.file.read().decode("utf-8")
        csv_reader = csv.DictReader(io.StringIO(content))

        processed_count = 0
        for row in csv_reader:
            sku = row.get("sku", "").strip().upper()
            title = row.get("title", "").strip()
            price = float(row.get("wholesale_price", 0.0))
            stock = int(row.get("stock_quantity", 0))
            weight = float(row.get("weight_kg", 2.0))

            if not sku or not title:
                continue

            existing_prod = db.query(ProductModel).filter(
                ProductModel.organization_id == demo_tenant_id,
                ProductModel.sku == sku
            ).first()

            if existing_prod:
                existing_prod.title = title
                existing_prod.wholesale_price = price
                existing_prod.stock_quantity = stock
                existing_prod.weight_kg = weight
            else:
                new_prod = ProductModel(
                    organization_id=demo_tenant_id,
                    sku=sku,
                    title=title,
                    wholesale_price=price,
                    stock_quantity=stock,
                    weight_kg=weight
                )
                db.add(new_prod)

            processed_count += 1

        db.commit()
        return {"status": "success", "message": f"Successfully processed {processed_count} SKUs."}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to process CSV: {str(e)}")

@app.get("/orders", tags=["Orders"])
def get_orders(db: Session = Depends(get_db)):
    demo_tenant_id = "123e4567-e89b-12d3-a456-426614174000"
    orders = db.query(OrderModel).filter(OrderModel.organization_id == demo_tenant_id).all()
    return {"data": orders}

# --- MESSAGING WEBHOOK ---
@app.post("/webhooks/sms", tags=["Webhooks"])
@app.post("/webhooks/whatsapp", tags=["Webhooks"])
def messaging_order_webhook(From: str = Form(...), Body: str = Form(...), db: Session = Depends(get_db)):
    raw_text = Body.strip()
    message_text = raw_text.upper()
    parts = message_text.split()
    cmd = parts[0] if parts else ""
    demo_tenant_id = "123e4567-e89b-12d3-a456-426614174000"

    if cmd in ["CATALOG", "ITEMS", "MENU", "PRODUCTS"]:
        products = db.query(ProductModel).filter(
            ProductModel.organization_id == demo_tenant_id,
            ProductModel.stock_quantity > 0
        ).all()

        if not products:
            msg = "Catalog is currently empty."
        else:
            catalog_lines = ["--- ORDERDESK CATALOG ---"]
            for p in products:
                catalog_lines.append(f"• {p.sku}: {p.title}\n  Price: ${p.wholesale_price:.2f} | Stock: {p.stock_quantity}")
            catalog_lines.append("\nOrder command:\nORDER <SKU> <QTY> PICKUP\nOR\nORDER <SKU> <QTY> DELIVER TO <CITY>")
            msg = "\n".join(catalog_lines)

    elif cmd == "ORDER":
        try:
            raw_args = message_text.replace("ORDER", "").strip()
            fulfillment_type = "PICKUP"
            destination_city = "HARARE"

            if " DELIVER TO " in raw_args:
                parts_str, destination_city = raw_args.split(" DELIVER TO ", 1)
                fulfillment_type = "DELIVERY"
                args_parts = parts_str.split()
            elif " PICKUP" in raw_args:
                parts_str = raw_args.replace(" PICKUP", "").strip()
                fulfillment_type = "PICKUP"
                args_parts = parts_str.split()
            else:
                args_parts = raw_args.split()

            sku = args_parts[0]
            quantity = int(args_parts[1])

            product = db.query(ProductModel).filter(
                ProductModel.organization_id == demo_tenant_id,
                ProductModel.sku == sku
            ).first()

            if not product or product.stock_quantity < quantity:
                msg = f"Error: Item out of stock or SKU '{sku}' not found."
            else:
                item_subtotal = product.wholesale_price * quantity
                total_weight = (product.weight_kg or 2.0) * quantity
                
                shipping_fee, vehicle_type, shipping_details = calculate_fulfillment_cost(
                    fulfillment_type, destination_city, total_weight
                )
                grand_total = item_subtotal + shipping_fee

                db_order = OrderModel(
                    organization_id=demo_tenant_id,
                    client_name=From,
                    fulfillment_type=fulfillment_type,
                    delivery_city=destination_city.title() if fulfillment_type == "DELIVERY" else "N/A (Pick Up)",
                    item_subtotal=item_subtotal,
                    shipping_fee=shipping_fee,
                    total_amount=grand_total,
                    vehicle_type=vehicle_type,
                    status="PENDING_PAYMENT",
                    payment_status="UNPAID"
                )
                db.add(db_order)
                product.stock_quantity -= quantity
                db.commit()

                msg = f"Order #{db_order.id[:8]} confirmed. Total: ${grand_total:.2f}. Fulfillment: {shipping_details}"
        except Exception:
            msg = "Format error: ORDER <SKU> <QTY> PICKUP or DELIVER TO <CITY>"
    else:
        msg = "Welcome to OrderDesk! Reply CATALOG to view available inventory."

    response_xml = f"<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response><Message>{msg}</Message></Response>"
    return Response(content=response_xml, media_type="application/xml")

# --- PAYNOW SUBSCRIPTION ENDPOINTS ---
PLAN_PRICES = {"MONTHLY": 29.99, "YEARLY": 299.00}

@app.post("/saas/subscribe", tags=["SaaS Subscriptions"])
def initiate_saas_subscription(req: SubscriptionRequest, db: Session = Depends(get_db)):
    if req.plan_type not in PLAN_PRICES:
        raise HTTPException(status_code=400, detail="Invalid plan type")

    paynow_id = os.getenv("PAYNOW_INTEGRATION_ID", "24744")
    paynow_key = os.getenv("PAYNOW_INTEGRATION_KEY", "a643f3fb-5957-4c6d-a36e-10f431451b3e")
    amount = PLAN_PRICES[req.plan_type]

    paynow = Paynow(
        paynow_id,
        paynow_key,
        "https://orderdesk-ui-ag2t.vercel.app/billing/return",
        "https://orderdesk-backend-9gu2.onrender.com/webhooks/paynow/saas"
    )

    payment = paynow.create_payment(f"OrderDesk {req.plan_type} Subscription", req.user_email)
    payment.add(f"OrderDesk Access ({req.plan_type})", amount)
    response = paynow.send(payment)

    if response.success:
        sub_record = SubscriptionPaymentModel(
            organization_id=req.organization_id,
            amount=amount,
            plan_type=req.plan_type,
            paynow_poll_url=response.poll_url,
            status="PENDING"
        )
        db.add(sub_record)
        db.commit()
        return {"status": "success", "redirect_url": response.redirect_url, "poll_url": response.poll_url}
    else:
        raise HTTPException(status_code=500, detail="Failed to initialize Paynow transaction")

@app.get("/", tags=["Health"])
def health_check():
    return {"status": "online", "system": "OrderDesk B2B Engine v2.0"}