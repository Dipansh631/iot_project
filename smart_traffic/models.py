from sqlalchemy import Column, Integer, String, Boolean, DateTime
from database import Base
import datetime

class CriminalVehicle(Base):
    __tablename__ = "criminal_vehicles"
    id = Column(Integer, primary_key=True, index=True)
    vehicle_number = Column(String, index=True, unique=True)
    owner_name = Column(String)
    crime_type = Column(String)
    police_station = Column(String)
    status = Column(String)
    vehicle_image = Column(String, nullable=True)

class DetectedVehicle(Base):
    __tablename__ = "detected_vehicles"
    id = Column(Integer, primary_key=True, index=True)
    vehicle_number = Column(String, index=True)
    time = Column(DateTime, default=datetime.datetime.utcnow)
    location = Column(String)
    image = Column(String, nullable=True)
    is_criminal = Column(Boolean, default=False)

class Alert(Base):
    __tablename__ = "alerts"
    id = Column(Integer, primary_key=True, index=True)
    vehicle_number = Column(String)
    alert_time = Column(DateTime, default=datetime.datetime.utcnow)
    crime_type = Column(String)
    owner_name = Column(String)
    location = Column(String)
    image = Column(String, nullable=True)
