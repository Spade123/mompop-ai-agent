import os
import uuid
import json
import requests
from datetime import datetime, date, timedelta
from typing import List, Optional, Dict

from fastapi import FastAPI, Depends, HTTPException, Form, Response
from pydantic import BaseModel
from sqlalchemy import (
    create_engine, Column, String, Integer, Boolean, 
    DateTime, ForeignKey, JSON, CHAR, select, delete
)
from sqlalchemy.types import TypeDecorator
from sqlalchemy.orm import (
    Session, sessionmaker, DeclarativeBase, Mapped, 
    mapped_column, relationship
)
from twilio.twiml.messaging_response import MessagingResponse

# ---------------------------------------------------------------------------
# 1. DATABASE CONFIGURATION & GUID SUPPORT
# ---------------------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./mompop_scheduler.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

class GUID(TypeDecorator):
    impl = CHAR
    cache_ok = True
    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(CHAR(36))
    def process_bind_param(self, value, dialect):
        return str(value) if value is not None else None
    def process_result_value(self, value, dialect):
        return uuid.UUID(str(value)) if value is not None else None

class Base(DeclarativeBase):
    pass

class Business(Base):
    __tablename__ = "businesses"
    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    working_hours: Mapped[dict] = mapped_column(JSON, nullable=False)
    services: Mapped[list["Service"]] = relationship(back_populates="business")
    employees: Mapped[list["Employee"]] = relationship(back_populates="business")

class Service(Base):
    __tablename__ = "services"
    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("businesses.id"))
    name: Mapped[str] = mapped_column(String(255))
    duration_minutes: Mapped[int] = mapped_column(Integer)
    business: Mapped["Business"] = relationship(back_populates="services")

class Employee(Base):
    __tablename__ = "employees"
    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("businesses.id"))
    name: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    business: Mapped["Business"] = relationship(back_populates="employees")

class Booking(Base):
    __tablename__ = "bookings"
    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("businesses.id"))
    customer_phone: Mapped[str] = mapped_column(String(20))
    service_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("services.id"))
    employee_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("employees.id"))
    start_time: Mapped[datetime] = mapped_column(DateTime)
    end_time: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(50), default="confirmed")
    is_urgent: Mapped[bool] = mapped_column(Boolean, default=False)

# ---------------------------------------------------------------------------
# 2. SCHEDULER LOGIC
# ---------------------------------------------------------------------------
def get_available_slots(db: Session, business_id: uuid.UUID, service_id: uuid.UUID, date_val: date):
    biz = db.get(Business, business_id)
    svc = db.get(Service, service_id)
    if not biz or not svc: return []

    day_name = date_val.strftime("%A").lower()
    hours = biz.working_hours.get(day_name)
    if not hours: return []

    biz_open = datetime.combine(date_val, datetime.strptime(hours['open'], "%H:%M").time())
    biz_close = datetime.combine(date_val, datetime.strptime(hours['close'], "%H:%M").time())

    employees = db.execute(select(Employee).where(Employee.business_id == business_id, Employee.is_active == True)).scalars().all()
    slots = []
    
    for emp in employees:
        existing_bookings = db.execute(select(Booking).where(Booking.employee_id == emp.id, Booking.status != "cancelled")).scalars().all()
        
        current_time = biz_open
        while current_time + timedelta(minutes=svc.duration_minutes) <= biz_close:
            slot_end = current_time + timedelta(minutes=svc.duration_minutes)
            is_blocked = any(current_time < b.end_time and slot_end > b.start_time for b in existing_bookings)
            
            if not is_blocked:
                slots.append({
                    "start_time": current_time.isoformat(),
                    "employee_id": str(emp.id),
                    "employee_name": emp.name
                })
            current_time += timedelta(minutes=15)
    return slots

# ---------------------------------------------------------------------------
# 3. AI AGENT CORE (Fixed Endpoint)
# ---------------------------------------------------------------------------
def call_gemini(user_msg, phone, biz_id):
    if not GEMINI_API_KEY:
        return {"action": "chat", "message": "Backend configuration error: API Key missing."}

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    
    prompt = f"""
    You are the AI Front Desk for 'Joe's Cuts'. Today is {datetime.now().strftime('%Y-%m-%d %H:%M')}.
    Customer Phone: {phone}
    Business ID: {biz_id}

    You MUST respond in JSON format ONLY:
    {{
      "action": "list_services" | "check_slots" | "confirm_booking" | "chat",
      "date": "YYYY-MM-DD",
      "start_time": "ISO_TIMESTAMP",
      "message": "Friendly response to customer"
    }}
    Rules:
    - If user sounds stressed or says "ASAP", imply urgency.
    - Be extremely brief; this is for SMS.
    """

    payload = {
        "contents": [{"parts": [{"text": user_msg}]}],
        "systemInstruction": {"parts": [{"text": prompt}]},
        "generationConfig": {"responseMimeType": "application/json"}
    }

    try:
        res = requests.post(url, json=payload, timeout=10)
        res_data = res.json()
        return json.loads(res_data['candidates'][0]['content']['parts'][0]['text'])
    except Exception as e:
        print(f"Gemini Error: {e}")
        return {"action": "chat", "message": "I'm having trouble thinking right now. Please try again in a moment!"}

# ---------------------------------------------------------------------------
# 4. FASTAPI APPLICATION
# ---------------------------------------------------------------------------
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base.metadata.create_all(bind=engine)
app = FastAPI()

def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

@app.get("/")
def health(): return {"status": "online", "service": "MomPop AI Agent"}

@app.post("/sms")
async def sms_webhook(From: str = Form(...), Body: str = Form(...), db: Session = Depends(get_db)):
    biz = db.execute(select(Business)).scalars().first()
    if not biz:
        twiml = MessagingResponse()
        twiml.message("Store is not yet configured.")
        return Response(content=str(twiml), media_type="application/xml")
    
    ai_response = call_gemini(Body, From, biz.id)
    action = ai_response.get("action")
    final_msg = ai_response.get("message", "How can I help you today?")

    if action == "list_services":
        svcs = db.execute(select(Service).where(Service.business_id == biz.id)).scalars().all()
        final_msg = f"We offer: {', '.join([s.name for s in svcs])}. Which one would you like?"
    
    elif action == "check_slots":
        svc = db.execute(select(Service).where(Service.business_id == biz.id)).scalars().first()
        date_str = ai_response.get("date", datetime.now().strftime('%Y-%m-%d'))
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
            slots = get_available_slots(db, biz.id, svc.id, d)
            if slots:
                times = ", ".join([s['start_time'][11:16] for s in slots[:3]])
                final_msg = f"Available on {date_str}: {times}. Any of those work?"
            else:
                final_msg = f"We are fully booked on {date_str}."
        except:
            final_msg = "Which date were you looking for?"

    twiml = MessagingResponse()
    twiml.message(final_msg)
    return Response(content=str(twiml), media_type="application/xml")
