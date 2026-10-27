import os
import csv
import io
import uuid
from datetime import datetime
from typing import Optional, List

from fastapi import FastAPI, Depends, HTTPException, Form, Response, UploadFile, File, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, String, Float, Integer, DateTime, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from paynow import Paynow

# --- DATABASE SETUP (Supabase / PostgreSQL) ---
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./orderdesk.db")

# Fix for Render / Heroku postgres:// schema requirement
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
    subscription_plan = Column(String, default="FREE_TRIAL")  # "MONTHLY", "YEARLY", "EXPIRED"
    subscription_status = Column(String, default="ACTIVE")    # "ACTIVE", "PAST_DUE", "CANCELLED"
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
    fulfillment_type = Column(String, default="PICKUP")  # "PICKUP" or "DELIVERY"
    delivery_city = Column(String, nullable=True)
    item_subtotal = Column(Float, nullable=False)
    shipping_fee = Column(Float, default=0.0)
    total_amount = Column(Float, nullable=False)
    vehicle_type = Column(String, nullable=True)
    status = Column(String, default="PENDING_PAYMENT")  # "PENDING_PAYMENT", "PENDING_DISPATCH", "DISPATCHED", "DELIVERED"
    payment_status = Column(String, default="UNPAID")   # "UNPAID", "PAID"
    created_at = Column(DateTime, default=datetime.utcnow)

class SubscriptionPaymentModel(Base):
    __tablename__ = "subscription_payments"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(String, ForeignKey("organizations.id"), nullable=False)
    amount = Column(Float, nullable=False)
    plan_type = Column(String, nullable=False)  # "MONTHLY" or "YEARLY