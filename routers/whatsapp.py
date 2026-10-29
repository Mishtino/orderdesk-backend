from fastapi import APIRouter, Depends, Form, Response
from sqlalchemy.orm import Session
from database import get_db

router = APIRouter(prefix="/webhooks", tags=["WhatsApp"])

try:
    from twilio.twiml.messaging_response import MessagingResponse
except Exception:  # pragma: no cover - twilio optional
    class MessagingResponse:  # type: ignore
        def __init__(self):
            self._msgs = []
        def message(self, text):
            self._msgs.append(text)
        def __str__(self):
            body = "".join(f"<Message>{m}</Message>" for m in self._msgs)
            return f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>'

try:
    import models as _models
    Product = _models.ProductModel if hasattr(_models, "ProductModel") else _models.Product
    Order = _models.OrderModel if hasattr(_models, "OrderModel") else _models.Order
except Exception:
    from main import ProductModel as Product, OrderModel as Order


@router.post("/whatsapp")
async def whatsapp_webhook(Body: str = Form(...), From: str = Form(...), db: Session = Depends(get_db)):
    resp = MessagingResponse()

    parts = Body.strip().split()

    if len(parts) == 3 and parts[0].upper() == "ORDER":
        sku = parts[1].upper()
        try:
            quantity = int(parts[2])
            product = db.query(Product).filter(Product.sku == sku).first()

            if not product:
                resp.message(f"Error: Product with SKU '{sku}' not found.")
            elif product.stock_quantity < quantity:
                resp.message(f"Insufficient stock for {product.title}. Available stock: {product.stock_quantity}.")
            else:
                product.stock_quantity -= quantity
                new_order = Order(
                    organization_id=product.organization_id,
                    client_name=From,
                    item_subtotal=product.wholesale_price * quantity,
                    total_amount=product.wholesale_price * quantity,
                    status="COMPLETED",
                    payment_status="PAID",
                )
                db.add(new_order)
                db.commit()

                resp.message(f"Order confirmed! {quantity}x {product.title} reserved. Remaining stock: {product.stock_quantity}.")
        except ValueError:
            resp.message("Invalid quantity format. Use: ORDER <SKU> <QUANTITY>")
    else:
        resp.message("Welcome to OrderDesk! To place an order, text: ORDER <SKU> <QUANTITY> (e.g., ORDER SKU-1001 2)")

    return Response(content=str(resp), media_type="application/xml")
