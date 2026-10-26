from fastapi import APIRouter, Depends, Form, Response
from sqlalchemy.orm import Session
from database import get_db
import models
from twilio.twiml.messaging_response import MessagingResponse

router = APIRouter(prefix="/webhooks", tags=["WhatsApp"])

@router.post("/whatsapp")
async def whatsapp_webhook(Body: str = Form(...), From: str = Form(...), db: Session = Depends(get_db)):
    resp = MessagingResponse()
    
    # Standardize incoming message string: "ORDER <SKU> <QUANTITY>"
    parts = Body.strip().split()
    
    if len(parts) == 3 and parts[0].upper() == "ORDER":
        sku = parts[1].upper()
        try:
            quantity = int(parts[2])
            product = db.query(models.Product).filter(models.Product.sku == sku).first()
            
            if not product:
                resp.message(f"Error: Product with SKU '{sku}' not found.")
            elif product.stock < quantity:
                resp.message(f"Insufficient stock for {product.title}. Available stock: {product.stock}.")
            else:
                # Deduct inventory stock and record the completed wholesale order
                product.stock -= quantity
                new_order = models.Order(
                    customer_phone=From,
                    sku=sku,
                    quantity=quantity,
                    total_price=product.price * quantity,
                    status="COMPLETED"
                )
                db.add(new_order)
                db.commit()
                
                resp.message(f"Order confirmed! {quantity}x {product.title} reserved. Remaining stock: {product.stock}.")
        except ValueError:
            resp.message("Invalid quantity format. Use: ORDER <SKU> <QUANTITY>")
    else:
        resp.message("Welcome to OrderDesk! To place an order, text: ORDER <SKU> <QUANTITY> (e.g., ORDER SKU-1001 2)")
        
    return Response(content=str(resp), media_type="application/xml")