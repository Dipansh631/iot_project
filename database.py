from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, Float, Text, Enum
from sqlalchemy.orm import declarative_base, sessionmaker
import datetime
import enum
import os

SQLALCHEMY_DATABASE_URL = "sqlite:///./traffic_monitor.db"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# ─── Enums ───────────────────────────────────────────────────────────────────

class SeverityLevel(str, enum.Enum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"

class UserRole(str, enum.Enum):
    admin = "admin"
    police = "police"
    operator = "operator"

class AlertStatus(str, enum.Enum):
    pending = "pending"
    sent = "sent"
    failed = "failed"

class SessionType(str, enum.Enum):
    upload = "upload"
    live_camera = "live_camera"


# ─── Models ──────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"
    id            = Column(Integer, primary_key=True, index=True)
    email         = Column(String, unique=True, index=True, nullable=False)
    full_name     = Column(String, nullable=True)
    hashed_password = Column(String, nullable=False)
    role          = Column(String, default="operator")
    police_station_id = Column(Integer, nullable=True)
    is_active     = Column(Boolean, default=True)
    created_at    = Column(DateTime, default=datetime.datetime.utcnow)


class PoliceStation(Base):
    __tablename__ = "police_stations"
    id               = Column(Integer, primary_key=True, index=True)
    station_name     = Column(String, nullable=False)
    station_code     = Column(String, unique=True, nullable=False)
    address          = Column(String)
    city             = Column(String)
    state            = Column(String)
    phone_number     = Column(String)
    email            = Column(String)
    latitude         = Column(Float)
    longitude        = Column(Float)
    jurisdiction_area = Column(Text)
    created_at       = Column(DateTime, default=datetime.datetime.utcnow)


class SuspiciousVehicle(Base):
    __tablename__ = "suspicious_vehicles"
    id                 = Column(Integer, primary_key=True, index=True)
    license_plate      = Column(String, unique=True, index=True, nullable=False)
    vehicle_type       = Column(String)
    vehicle_color      = Column(String)
    vehicle_make       = Column(String)
    vehicle_model      = Column(String)
    owner_name         = Column(String)
    reason_for_flagging = Column(Text, nullable=False)
    severity_level     = Column(String, default="medium")   # low/medium/high/critical
    is_active          = Column(Boolean, default=True)
    reported_by        = Column(String)
    reported_date      = Column(String)
    additional_notes   = Column(Text)
    created_at         = Column(DateTime, default=datetime.datetime.utcnow)


class VideoSession(Base):
    __tablename__ = "video_sessions"
    id                    = Column(Integer, primary_key=True, index=True)
    session_type          = Column(String, nullable=False)   # upload / live_camera
    video_filename        = Column(String)
    camera_id             = Column(String)
    location              = Column(String)
    start_time            = Column(DateTime, default=datetime.datetime.utcnow)
    end_time              = Column(DateTime, nullable=True)
    total_detections      = Column(Integer, default=0)
    suspicious_detections = Column(Integer, default=0)
    created_by            = Column(Integer, nullable=True)


class DetectionLog(Base):
    __tablename__ = "detection_logs"
    id                    = Column(Integer, primary_key=True, index=True)
    session_id            = Column(Integer, nullable=True)
    detected_plate        = Column(String, nullable=False, index=True)
    confidence_score      = Column(Float)
    is_suspicious         = Column(Boolean, default=False)
    suspicious_vehicle_id = Column(Integer, nullable=True)
    detection_timestamp   = Column(DateTime, default=datetime.datetime.utcnow)
    camera_location       = Column(String)
    image_filename        = Column(String)
    video_frame_number    = Column(Integer)
    alert_sent            = Column(Boolean, default=False)
    alerted_station_id    = Column(Integer, nullable=True)
    created_by            = Column(Integer, nullable=True)


class AlertNotification(Base):
    __tablename__ = "alert_notifications"
    id                  = Column(Integer, primary_key=True, index=True)
    detection_log_id    = Column(Integer, nullable=True)
    police_station_id   = Column(Integer, nullable=True)
    station_name        = Column(String)
    station_email       = Column(String)
    license_plate       = Column(String, nullable=False)
    vehicle_info        = Column(Text)
    severity_level      = Column(String)
    alert_type          = Column(String, default="system")
    alert_status        = Column(String, default="sent")
    sent_at             = Column(DateTime, default=datetime.datetime.utcnow)
    error_message       = Column(Text)
    created_at          = Column(DateTime, default=datetime.datetime.utcnow)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Create tables and seed default data."""
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    # Seed police stations
    if not db.query(PoliceStation).first():
        stations = [
            PoliceStation(station_name="Central Police Station",  station_code="CPS001",
                          address="123 Main Street", city="Mumbai", state="Maharashtra",
                          phone_number="+91-22-12345678", email="central@police.gov.in",
                          latitude=19.0760, longitude=72.8777),
            PoliceStation(station_name="North Zone Station",      station_code="NZS002",
                          address="456 North Avenue", city="Delhi", state="Delhi",
                          phone_number="+91-11-23456789", email="north@police.gov.in",
                          latitude=28.7041, longitude=77.1025),
            PoliceStation(station_name="South District HQ",       station_code="SDH003",
                          address="789 South Road", city="Bangalore", state="Karnataka",
                          phone_number="+91-80-34567890", email="south@police.gov.in",
                          latitude=12.9716, longitude=77.5946),
            PoliceStation(station_name="East Zone Police Station", station_code="EZP004",
                          address="22 East Park Lane", city="Kolkata", state="West Bengal",
                          phone_number="+91-33-45678901", email="east@police.gov.in",
                          latitude=22.5726, longitude=88.3639),
        ]
        db.add_all(stations)
        db.commit()

    # Seed suspicious vehicles
    if not db.query(SuspiciousVehicle).first():
        vehicles = [
            SuspiciousVehicle(license_plate="MH12AB1234", vehicle_type="Car",   vehicle_color="Black",
                              reason_for_flagging="Stolen vehicle reported 2024-01-15",
                              severity_level="critical", reported_by="Mumbai Police", reported_date="2024-01-15"),
            SuspiciousVehicle(license_plate="DL3CAB5678", vehicle_type="SUV",   vehicle_color="White",
                              reason_for_flagging="Hit-and-run case FIR #DL2024-102",
                              severity_level="high",     reported_by="Delhi Traffic Police", reported_date="2024-02-10"),
            SuspiciousVehicle(license_plate="KA01XY9876", vehicle_type="Bike",  vehicle_color="Red",
                              reason_for_flagging="Suspected in armed robbery",
                              severity_level="high",     reported_by="Bangalore Crime Branch", reported_date="2024-03-05"),
            SuspiciousVehicle(license_plate="TN22CD4567", vehicle_type="Truck", vehicle_color="Blue",
                              reason_for_flagging="Transporting illegal contraband",
                              severity_level="medium",   reported_by="Highway Patrol", reported_date="2024-03-20"),
            SuspiciousVehicle(license_plate="GJ09EF3456", vehicle_type="Van",   vehicle_color="Grey",
                              reason_for_flagging="Repeat offender - no valid insurance",
                              severity_level="low",      reported_by="Ahmedabad RTO", reported_date="2024-04-01"),
            SuspiciousVehicle(license_plate="DL8SBT1234", vehicle_type="Car",   vehicle_color="Silver",
                              reason_for_flagging="Involved in drug trafficking case",
                              severity_level="critical", reported_by="Narcotics Control Bureau", reported_date="2024-04-12"),
        ]
        db.add_all(vehicles)
        db.commit()

    # Seed default admin user
    from auth import get_password_hash
    if not db.query(User).first():
        admin = User(
            email="admin@trafficmonitor.gov.in",
            full_name="System Administrator",
            hashed_password=get_password_hash("Admin@1234"),
            role="admin",
        )
        operator = User(
            email="operator@trafficmonitor.gov.in",
            full_name="Traffic Operator",
            hashed_password=get_password_hash("Operator@1234"),
            role="operator",
        )
        db.add_all([admin, operator])
        db.commit()

    db.close()
