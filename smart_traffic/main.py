import asyncio
import cv2
import json
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from database import SessionLocal, engine, Base
import models
from ai import AIProcessor, clean_plate

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Smart Traffic Management")
# app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Dependency
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# WebSocket Manager for Live Alerts
class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except Exception:
                pass

manager = ConnectionManager()

# Preseed fake database entries
db = SessionLocal()
if not db.query(models.CriminalVehicle).first():
    fake_criminal = models.CriminalVehicle(
        vehicle_number="DL8SBT1234", owner_name="John Doe", crime_type="Stolen Vehicle", 
        police_station="Central Station", status="Wanted", vehicle_image=""
    )
    db.add(fake_criminal)
    db.commit()
db.close()

async def generate_video(camera_id: int):
    # Capture from local webcam for demo
    cap = cv2.VideoCapture(0)
    db = SessionLocal()
    processor = AIProcessor(db, manager)
    
    while cap.isOpened():
        success, frame = cap.read()
        if not success:
            break
            
        # Process the frame for AI (YOLO + OCR)
        frame, detected_plates = await processor.process_frame(frame)
        
        # Match with Crime DB over processed plates
        for (x1, y1, x2, y2, plate) in detected_plates:
            # Check criminal database
            criminal = db.query(models.CriminalVehicle).filter(models.CriminalVehicle.vehicle_number == plate).first()
            
            if criminal:
                # 4. Matching Logic : CRITICAL HIGHLIGHT
                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 0, 255), 4) # RED
                cv2.putText(frame, f"CRIMINAL: {plate}", (x1, y1 - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 255), 2)
                
                # Send WebSocket alert
                alert_data = json.dumps({
                    "vehicle": plate, "crime": criminal.crime_type, "owner": criminal.owner_name
                })
                await manager.broadcast(alert_data)
                
                # Log Alert
                new_alert = models.Alert(vehicle_number=plate, crime_type=criminal.crime_type, owner_name=criminal.owner_name, location="Camera 1")
                db.add(new_alert)
                
                # Log Detected
                new_detected = models.DetectedVehicle(vehicle_number=plate, location="Camera 1", is_criminal=True)
                db.add(new_detected)
                db.commit()
            else:
                # Normal vehicle
                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2) # GREEN
                cv2.putText(frame, plate, (x1, y1 - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 0), 2)
                
        # Encode for streaming
        ret, buffer = cv2.imencode('.jpg', frame)
        frame_bytes = buffer.tobytes()
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')

    cap.release()
    db.close()

@app.get("/")
async def dashboard(request: Request, db: Session = Depends(get_db)):
    criminals = db.query(models.CriminalVehicle).all()
    alerts = db.query(models.Alert).order_by(models.Alert.id.desc()).limit(10).all()
    return templates.TemplateResponse("dashboard.html", {"request": request, "criminals": criminals, "alerts": alerts})

@app.get("/video_feed")
async def video_feed():
    return StreamingResponse(generate_video(0), media_type="multipart/x-mixed-replace; boundary=frame")

@app.websocket("/ws/alerts")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
