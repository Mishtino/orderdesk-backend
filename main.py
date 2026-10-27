import os
import uuid
import csv
import io
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, Form, Response, UploadFile, File, Request
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

Base.metadata.create_all(bind=engine)

# --- FASTAPI APP DECLARATION ---
app = FastAPI(title="OrderDesk B2B Engine", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DEMO_TENANT_ID = "123e4567-e89b-12d3-a456-426614174000"

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

# --- PRODUCT & ORDER ENDPOINTS ---
@app.get("/products", tags=["Products"])
def get_products(db: Session = Depends(get_db)):
    products = db.query(ProductModel).filter(ProductModel.organization_id == DEMO_TENANT_ID).all()
    return {"data": products}

@app.post("/products/seed", tags=["Products"])
def seed_demo_products(db: Session = Depends(get_db)):
    default_items = [
        {"sku": "SKU-1001", "title": "Maize Meal 10kg", "wholesale_price": 12.50, "stock_quantity": 150, "weight_kg": 10.0},
        {"sku": "SKU-1002", "title": "Cooking Oil 2L", "wholesale_price": 3.80, "stock_quantity": 300, "weight_kg": 2.0},
        {"sku": "SKU-1003", "title": "Refined Sugar 2kg", "wholesale_price": 2.40, "stock_quantity": 200, "weight_kg": 2.0},
    ]
    for item in default_items:
        exists = db.query(ProductModel).filter(ProductModel.organization_id == DEMO_TENANT_ID, ProductModel.sku == item["sku"]).first()
        if not exists:
            db.add(ProductModel(organization_id=DEMO_TENANT_ID, **item))
    db.commit()
    return {"status": "seeded"}

@app.post("/products/bulk-upload", tags=["Products"])
def bulk_upload_products(file: UploadFile = File(...), db: Session = Depends(get_db)):
    if not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="File must be a valid .csv spreadsheet.")

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
                ProductModel.organization_id == DEMO_TENANT_ID,
                ProductModel.sku == sku
            ).first()

            if existing_prod:
                existing_prod.title = title
                existing_prod.wholesale_price = price
                existing_prod.stock_quantity = stock
                existing_prod.weight_kg = weight
            else:
                db.add(ProductModel(
                    organization_id=DEMO_TENANT_ID,
                    sku=sku,
                    title=title,
                    wholesale_price=price,
                    stock_quantity=stock,
                    weight_kg=weight
                ))

            processed_count += 1

        db.commit()
        return {"status": "success", "message": f"Successfully processed {processed_count} SKUs."}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to process CSV: {str(e)}")

@app.get("/orders", tags=["Orders"])
def get_orders(db: Session = Depends(get_db)):
    orders = db.query(OrderModel).filter(OrderModel.organization_id == DEMO_TENANT_ID).all()
    return {"data": orders}

# --- TWILIO / MESSAGING WEBHOOK ---
@app.api_route("/webhooks/sms", methods=["GET", "POST"], tags=["Webhooks"])
@app.api_route("/webhooks/whatsapp", methods=["GET", "POST"], tags=["Webhooks"])
async def messaging_order_webhook(request: Request, db: Session = Depends(get_db)):
    form_data = await request.form()
    sender = form_data.get("From", "Customer")
    raw_body = form_data.get("Body", "").strip()
    message_text = raw_body.upper()
    parts = message_text.split()
    cmd = parts[0] if parts else ""

    if cmd in ["CATALOG", "ITEMS", "MENU", "PRODUCTS"]:
        products = db.query(ProductModel).filter(
            ProductModel.organization_id == DEMO_TENANT_ID,
            ProductModel.stock_quantity > 0
        ).all()

        if not products:
            msg = "Catalog is empty. Reply SEED to generate sample products."
        else:
            catalog_lines = ["--- ORDERDESK CATALOG ---"]
            for p in products:
                catalog_lines.append(f"• {p.sku}: {p.title}\n  Price: ${p.wholesale_price:.2f} | Stock: {p.stock_quantity}")
            catalog_lines.append("\nTo Order:\nORDER <SKU> <QTY> PICKUP\nOR\nORDER <SKU> <QTY> DELIVER TO <CITY>")
            msg = "\n".join(catalog_lines)

    elif cmd == "SEED":
        default_items = [
            {"sku": "SKU-1001", "title": "Maize Meal 10kg", "wholesale_price": 12.50, "stock_quantity": 150, "weight_kg": 10.0},
            {"sku": "SKU-1002", "title": "Cooking Oil 2L", "wholesale_price": 3.80, "stock_quantity": 300, "weight_kg": 2.0},
            {"sku": "SKU-1003", "title": "Refined Sugar 2kg", "wholesale_price": 2.40, "stock_quantity": 200, "weight_kg": 2.0},
        ]
        for item in default_items:
            exists = db.query(ProductModel).filter(ProductModel.organization_id == DEMO_TENANT_ID, ProductModel.sku == item["sku"]).first()
            if not exists:
                db.add(ProductModel(organization_id=DEMO_TENANT_ID, **item))
        db.commit()
        msg = "Demo products added! Reply CATALOG to view inventory."

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
                ProductModel.organization_id == DEMO_TENANT_ID,
                ProductModel.sku == sku
            ).first()

            if not product or product.stock_quantity < quantity:
                msg = f"Error: SKU '{sku}' unavailable or insufficient stock."
            else:
                item_subtotal = product.wholesale_price * quantity
                total_weight = (product.weight_kg or 2.0) * quantity
                
                shipping_fee, vehicle_type, shipping_details = calculate_fulfillment_cost(
                    fulfillment_type, destination_city, total_weight
                )
                grand_total = item_subtotal + shipping_fee

                db_order = OrderModel(
                    organization_id=DEMO_TENANT_ID,
                    client_name=sender,
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

                msg = f"Order #{db_order.id[:8]} placed! Total: ${grand_total:.2f}. {shipping_details}"
        except Exception:
            msg = "Format error: Reply CATALOG or use 'ORDER <SKU> <QTY> PICKUP'"
    else:
        msg = "Welcome to OrderDesk! Reply CATALOG to view items or SEED to add sample inventory."

    twiml_xml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{msg}</Message></Response>'
    return Response(content=twiml_xml, media_type="application/xml")

@app.get("/", tags=["Health"])
def health_check():
    return {"status": "online", "system": "OrderDesk B2B Engine v2.0"}