import uuid
from sqlalchemy import Column, String, Integer, Float, DateTime
from sqlalchemy.sql import func

from database import Base


class ProductModel(Base):
    __tablename__ = "products"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(String, index=True, nullable=False)
    sku = Column(String, index=True, nullable=False)
    title = Column(String, nullable=False)
    stock_quantity = Column(Integer, default=0)
    wholesale_price = Column(Float, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class OrderModel(Base):
    __tablename__ = "orders"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(String, index=True, nullable=False)
    client_name = Column(String, nullable=False)
    status = Column(String, default="PENDING_DISPATCH")
    total_amount = Column(Float, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
