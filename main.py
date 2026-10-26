from fastapi import FastAPI, Depends, status, HTTPException, Form, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List
from uuid import UUID
from sqlalchemy.orm import Session

from config import get_current_tenant, create_access_token
from database import engine, Base, get_db
from models import ProductModel, OrderModel

Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="OrderDesk B2B Engine",
    description="Multi-tenant wholesale catalog with persistent storage and inventory validation.",
    version="1.1.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Schemas ---
class TokenRequest(BaseModel):
    user_id: str
    organization_id: UUID


class ProductCreate(BaseModel):
    sku: str = Field(..., example="SKU-1001")
    title: str = Field(..., example="5L Cooking Oil Wholesale Pack")
    stock_quantity: int = Field(..., ge=0, example=250)
    wholesale_price: float = Field(..., gt=0, example=18.50)


class OrderItem(BaseModel):
    sku: str
    quantity: int = Field(..., gt=0)


class OrderCreate(BaseModel):
    client_name: str = Field(..., example="Zengeza Retail Stores")
    items: List[OrderItem]


# --- Auth ---
@app.post("/auth/token", tags=["Authentication"])
def generate_tenant_token(auth_data: TokenRequest):
    token = create_access_token({
        "sub": auth_data.user_id,
        "organization_id": str(auth_data.organization_id)
    })
    return {"access_token": token, "token_type": "bearer"}


# --- Inventory ---
@app.post("/products", status_code=status.HTTP_201_CREATED, tags=["Inventory"])
def create_product(
    product: ProductCreate,
    tenant_id: str = Depends(get_current_tenant),
    db: Session = Depends(get_db)
):
    existing = db.query(ProductModel).filter(
        ProductModel.organization_id == tenant_id,
        ProductModel.sku == product.sku
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="SKU already exists for this tenant.")

    db_product = ProductModel(
        organization_id=tenant_id,
        **product.dict()
    )
    db.add(db_product)
    db.commit()
    db.refresh(db_product)
    return {"message": "Product registered successfully", "data": db_product}


@app.get("/products", tags=["Inventory"])
def list_products(
    tenant_id: str = Depends(get_current_tenant),
    db: Session = Depends(get_db)
):
    products = db.query(ProductModel).filter(ProductModel.organization_id == tenant_id).all()
    return {"tenant_id": tenant_id, "count": len(products), "data": products}


# --- Orders with Automatic Stock Deduction ---
@app.post("/orders", status_code=status.HTTP_201_CREATED, tags=["Orders"])
def place_order(
    order: OrderCreate,
    tenant_id: str = Depends(get_current_tenant),
    db: Session = Depends(get_db)
):
    calculated_total = 0.0
    items_to_update = []

    for item in order.items:
        db_product = db.query(ProductModel).filter(
            ProductModel.organization_id == tenant_id,
            ProductModel.sku == item.sku
        ).first()

        if not db_product:
            raise HTTPException(status_code=404, detail=f"Product with SKU '{item.sku}' not found.")

        if db_product.stock_quantity < item.quantity:
            raise HTTPException(
                status_code=400,
                detail=f"Insufficient stock for SKU '{item.sku}'. Available: {db_product.stock_quantity}"
            )

        item_total = db_product.wholesale_price * item.quantity
        calculated_total += item_total
        items_to_update.append((db_product, item.quantity))

    db_order = OrderModel(
        organization_id=tenant_id,
        client_name=order.client_name,
        total_amount=calculated_total,
        status="PENDING_DISPATCH"
    )
    db.add(db_order)
    db.flush()

    for db_product, qty in items_to_update:
        db_product.stock_quantity -= qty

    db.commit()
    db.refresh(db_order)
    return {"message": "Order created successfully", "data": db_order}


@app.get("/orders", tags=["Orders"])
def list_orders(
    tenant_id: str = Depends(get_current_tenant),
    db: Session = Depends(get_db)
):
    orders = db.query(OrderModel).filter(OrderModel.organization_id == tenant_id).all()
    return {"tenant_id": tenant_id, "count": len(orders), "data": orders}


@app.get("/", tags=["Health"])
def health_check():
    return {"status": "online", "message": "OrderDesk Engine Operational"}


# --- WhatsApp B2B Webhook (Low-Data Ordering) ---
@app.post("/webhooks/whatsapp", tags=["Webhooks"])
def whatsapp_order_webhook(
    From: str = Form(...),
    Body: str = Form(...),
    db: Session = Depends(get_db)
):
    """
    Handles incoming WhatsApp messages from Twilio.
    Example message: "ORDER SKU-1001 5"
    """
    message_text = Body.strip()
    demo_tenant_id = "123e4567-e89b-12d3-a456-426614174000"

    if not message_text.upper().startswith("ORDER"):
        response_xml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Message>Welcome to OrderDesk! To place a quick order, send: ORDER SKU-1001 10</Message>
</Response>"""
        return Response(content=response_xml, media_type="application/xml")

    try:
        parts = message_text.split()
        if len(parts) < 3:
            raise ValueError("Missing SKU or quantity")

        sku = parts[1].upper()
        quantity = int(parts[2])

        product = db.query(ProductModel).filter(
            ProductModel.organization_id == demo_tenant_id,
            ProductModel.sku == sku
        ).first()

        if not product:
            msg = f"Error: SKU '{sku}' not found in inventory catalog."
        elif product.stock_quantity < quantity:
            msg = f"Error: Insufficient stock for SKU '{sku}'. Available: {product.stock_quantity}."
        else:
            total_amount = product.wholesale_price * quantity
            db_order = OrderModel(
                organization_id=demo_tenant_id,
                client_name=From,
                total_amount=total_amount,
                status="PENDING_DISPATCH"
            )
            db.add(db_order)
            db.flush()
            product.stock_quantity -= quantity
            db.commit()
            db.refresh(db_order)
            msg = (
                f"Order confirmed for {sku} x {quantity}. "
                f"Total: ${total_amount:.2f}. Remaining stock: {product.stock_quantity}."
            )
    except ValueError:
        msg = "Format error: use ORDER SKU-1001 10"

    response_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Message>{msg}</Message>
</Response>"""
    return Response(content=response_xml, media_type="application/xml")