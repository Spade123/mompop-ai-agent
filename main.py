import uuid
from datetime import datetime, date, timedelta
from typing import List, Optional, Dict

try:
    from fastapi import FastAPI, Depends, HTTPException
    from pydantic import BaseModel
except ImportError:
    print("Error: FastAPI or Pydantic not found. Please run: pip install fastapi")
    # Define dummy classes so the rest of the script doesn't crash during static analysis
    class FastAPI: 
        def __init__(self, **kwargs): pass
        def get(self, *args, **kwargs): return lambda f: f
        def post(self, *args, **kwargs): return lambda f: f
    class BaseModel: pass
    Depends = lambda x: None

from sqlalchemy import (
    create_engine, Column, String, Integer, Boolean, 
    DateTime, ForeignKey, JSON, Text, Enum, UniqueConstraint, CHAR, select
)
from sqlalchemy.types import TypeDecorator
from sqlalchemy.orm import (
    Session, sessionmaker, DeclarativeBase, Mapped, 
    mapped_column, relationship
)

# ---------------------------------------------------------------------------
# 1. Platform-independent UUID column
# ---------------------------------------------------------------------------
class GUID(TypeDecorator):
    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import UUID
            return dialect.type_descriptor(UUID())
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value, dialect):
        if value is None: return None
        return str(value)

    def process_result_value(self, value, dialect):
        if value is None: return None
        return uuid.UUID(str(value))

# ---------------------------------------------------------------------------
# 2. Database Models
# ---------------------------------------------------------------------------
class Base(DeclarativeBase):
    pass

class Business(Base):
    __tablename__ = "businesses"
    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="America/New_York")
    working_hours: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    services: Mapped[list["Service"]] = relationship(back_populates="business", cascade="all, delete-orphan")
    employees: Mapped[list["Employee"]] = relationship(back_populates="business", cascade="all, delete-orphan")
    bookings: Mapped[list["Booking"]] = relationship(back_populates="business")

class Service(Base):
    __tablename__ = "services"
    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    business: Mapped["Business"] = relationship(back_populates="services")
    bookings: Mapped[list["Booking"]] = relationship(back_populates="service")
    __table_args__ = (UniqueConstraint("business_id", "name", name="uq_service_name_per_business"),)

class Employee(Base):
    __tablename__ = "employees"
    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    business: Mapped["Business"] = relationship(back_populates="employees")
    availability_blocks: Mapped[list["EmployeeAvailability"]] = relationship(back_populates="employee", cascade="all, delete-orphan")
    bookings: Mapped[list["Booking"]] = relationship(back_populates="employee")

class EmployeeAvailability(Base):
    __tablename__ = "employee_availability"
    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    employee_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    start_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    employee: Mapped["Employee"] = relationship(back_populates="availability_blocks")

class Booking(Base):
    __tablename__ = "bookings"
    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False)
    customer_phone: Mapped[str] = mapped_column(String(20), nullable=False)
    service_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("services.id"), nullable=False)
    employee_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("employees.id"), nullable=False)
    start_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="confirmed")
    business: Mapped["Business"] = relationship(back_populates="bookings")
    service: Mapped["Service"] = relationship(back_populates="bookings")
    employee: Mapped["Employee"] = relationship(back_populates="bookings")

class Customer(Base):
    __tablename__ = "customers"
    phone: Mapped[str] = mapped_column(String(20), primary_key=True)
    business_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("businesses.id", ondelete="CASCADE"), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_seen: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

# ---------------------------------------------------------------------------
# 3. Scheduler Utilities
# ---------------------------------------------------------------------------
def get_available_slots(db: Session, business_id: uuid.UUID, service_id: uuid.UUID, date_val: date, employee_id: uuid.UUID = None):
    business = db.get(Business, business_id)
    service = db.get(Service, service_id)
    if not business or not service: return []

    day_name = date_val.strftime("%A").lower()
    hours = business.working_hours.get(day_name)
    if not hours: return []

    biz_open = datetime.combine(date_val, datetime.strptime(hours['open'], "%H:%M").time())
    biz_close = datetime.combine(date_val, datetime.strptime(hours['close'], "%H:%M").time())

    query = select(Employee).where(Employee.business_id == business_id, Employee.is_active == True)
    if employee_id: query = query.where(Employee.id == employee_id)
    employees = db.execute(query).scalars().all()
    
    slots = []
    for emp in employees:
        bookings = db.execute(select(Booking).where(Booking.employee_id == emp.id, Booking.start_time >= biz_open, Booking.start_time < biz_close, Booking.status != "cancelled")).scalars().all()
        blocks = db.execute(select(EmployeeAvailability).where(EmployeeAvailability.employee_id == emp.id)).scalars().all()

        curr = biz_open
        while curr + timedelta(minutes=service.duration_minutes) <= biz_close:
            end = curr + timedelta(minutes=service.duration_minutes)
            blocked = any(curr < b.end_time and end > b.start_time for b in bookings) or \
                      any(curr < bl.end_time and end > bl.start_time for bl in blocks)
            if not blocked:
                slots.append({"start_time": curr, "end_time": end, "employee_id": emp.id, "employee_name": emp.name})
            curr += timedelta(minutes=15)
    return slots

# ---------------------------------------------------------------------------
# 4. FastAPI Setup
# ---------------------------------------------------------------------------
SQLALCHEMY_DATABASE_URL = "sqlite:///./mompop_scheduler.db"
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base.metadata.create_all(bind=engine)

app = FastAPI(title="MompopScheduler API")

def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

# Pydantic Schemas
class SlotResponse(BaseModel):
    start_time: datetime
    end_time: datetime
    employee_id: uuid.UUID
    employee_name: str

class BookingCreate(BaseModel):
    business_id: uuid.UUID
    service_id: uuid.UUID
    employee_id: uuid.UUID
    customer_phone: str
    customer_name: Optional[str] = None
    start_time: datetime

class ServiceResponse(BaseModel):
    id: uuid.UUID
    name: str
    duration_minutes: int
    class Config: from_attributes = True

# API Endpoints
@app.get("/services/{business_id}", response_model=List[ServiceResponse])
def get_business_services(business_id: uuid.UUID, db: Session = Depends(get_db)):
    return db.execute(select(Service).where(Service.business_id == business_id)).scalars().all()

@app.get("/availability", response_model=List[SlotResponse])
def check_availability(business_id: uuid.UUID, service_id: uuid.UUID, booking_date: date, db: Session = Depends(get_db)):
    return get_available_slots(db, business_id, service_id, booking_date)

@app.post("/bookings")
def create_booking(data: BookingCreate, db: Session = Depends(get_db)):
    service = db.get(Service, data.service_id)
    if not service: raise HTTPException(status_code=404, detail="Service not found")
    
    end_time = data.start_time + timedelta(minutes=service.duration_minutes)
    
    # Check for overlaps
    overlap = db.execute(select(Booking).where(
        Booking.employee_id == data.employee_id,
        Booking.status != "cancelled",
        Booking.start_time < end_time,
        Booking.end_time > data.start_time
    )).first()
    
    if overlap:
        raise HTTPException(status_code=400, detail="Employee is already booked for this time")

    new_booking = Booking(
        business_id=data.business_id,
        service_id=data.service_id,
        employee_id=data.employee_id,
        customer_phone=data.customer_phone,
        start_time=data.start_time,
        end_time=end_time
    )
    
    # Update/Create Customer
    customer = db.get(Customer, (data.customer_phone, data.business_id))
    if customer:
        customer.last_seen = datetime.utcnow()
        if data.customer_name: customer.name = data.customer_name
    else:
        db.add(Customer(phone=data.customer_phone, business_id=data.business_id, name=data.customer_name))
        
    db.add(new_booking)
    db.commit()
    return {"message": "Booking confirmed", "booking_id": new_booking.id}

if __name__ == "__main__":
    try:
        import uvicorn
        with SessionLocal() as db:
            if not db.execute(select(Business)).first():
                biz = Business(name="Joe's Cuts", phone="555-0101", working_hours={"monday": {"open": "09:00", "close": "17:00"}})
                db.add(biz)
                db.commit()
                emp = Employee(name="Joe", business_id=biz.id)
                svc = Service(name="Haircut", duration_minutes=30, business_id=biz.id)
                db.add_all([emp, svc])
                db.commit()
                print(f"\n--- TEST DATA SEEDED ---")
                print(f"Business ID: {biz.id}")
                print(f"Service ID:  {svc.id}")
                print(f"Employee ID: {emp.id}")
                print(f"------------------------\n")

        print("Starting server...")
        uvicorn.run(app, host="127.0.0.1", port=8000)
    except ImportError:
        print("Error: uvicorn not found. Please run: pip install uvicorn")