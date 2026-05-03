"""
Main FastAPI application — Traffic Monitoring & Number Plate Detection System.
Run with:  uvicorn main:app --reload --port 8000
"""
import os
import json
import asyncio
import datetime
import logging

from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from fastapi import (FastAPI, Request, Response, Depends, HTTPException,
                     UploadFile, File, Form, WebSocket, WebSocketDisconnect,
                     status as http_status)
from fastapi.responses import (HTMLResponse, RedirectResponse,
                               StreamingResponse, JSONResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from dotenv import load_dotenv

load_dotenv()

from database import init_db, get_db, SessionLocal
from database import (User, PoliceStation, SuspiciousVehicle,
                       DetectionLog, AlertNotification, VideoSession)
from auth import (authenticate_user, create_access_token, get_current_user,
                  require_user, get_password_hash)
import detector
import pdf_extractor

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(levelname)s:  %(message)s")
logger = logging.getLogger(__name__)

# ── App Setup ─────────────────────────────────────────────────────────────────
app = FastAPI(title="Traffic Monitoring System", version="2.0.0")

BASE_DIR   = Path(__file__).parent
UPLOAD_DIR = BASE_DIR / "uploads"
CAPTURE_DIR = BASE_DIR / "static" / "captures"
UPLOAD_DIR.mkdir(exist_ok=True)
CAPTURE_DIR.mkdir(exist_ok=True)

app.mount("/static",  StaticFiles(directory=str(BASE_DIR / "static")),  name="static")
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)),           name="uploads")

def safe_fromjson(val):
    try:
        return json.loads(val) if val else {}
    except:
        return {}

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.filters["fromjson"] = safe_fromjson
templates.env.filters["tojson"]   = json.dumps


def tmpl(name: str, request: Request, ctx: dict = {}):
    """Helper: render template — compatible with both old & new Starlette."""
    ctx["request"] = request
    return templates.TemplateResponse(request=request, name=name, context=ctx)


# ── WebSocket Manager ─────────────────────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self.connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.connections.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.connections:
            self.connections.remove(ws)

    async def broadcast(self, data: dict):
        msg = json.dumps(data)
        dead = []
        for ws in self.connections:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


ws_manager = ConnectionManager()


# ── Startup ───────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    init_db()
    logger.info("Database initialised & seeded.")


# ═══════════════════════════════════════════════════════════════════════════════
#  AUTH ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def root(request: Request, current_user=Depends(get_current_user)):
    if current_user:
        return RedirectResponse(url="/dashboard", status_code=302)
    return RedirectResponse(url="/login", status_code=302)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return tmpl("login.html", request)


@app.post("/login")
async def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = authenticate_user(db, email, password)
    if not user:
        return tmpl("login.html", request, {"error": "Invalid email or password."})
    token = create_access_token({"sub": user.email})
    response = RedirectResponse(url="/dashboard", status_code=302)
    response.set_cookie(
        key="access_token", value=token,
        httponly=True, max_age=60 * 60 * 8, samesite="lax"
    )
    return response


@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie("access_token")
    return response


@app.post("/api/auth/login")
async def api_login(
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    """JSON endpoint for SPA / programmatic access."""
    user = authenticate_user(db, email, password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_access_token({"sub": user.email})
    return {"access_token": token, "token_type": "bearer",
            "user": {"email": user.email, "full_name": user.full_name, "role": user.role}}


# ═══════════════════════════════════════════════════════════════════════════════
#  DASHBOARD
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    recent_detections = (db.query(DetectionLog)
                         .order_by(DetectionLog.detection_timestamp.desc())
                         .limit(20).all())
    recent_alerts     = (db.query(AlertNotification)
                         .order_by(AlertNotification.created_at.desc())
                         .limit(10).all())
    total_detections  = db.query(DetectionLog).count()
    total_suspicious  = db.query(DetectionLog).filter(DetectionLog.is_suspicious == True).count()
    total_vehicles    = db.query(SuspiciousVehicle).filter(SuspiciousVehicle.is_active == True).count()
    total_alerts      = db.query(AlertNotification).count()
    stations          = db.query(PoliceStation).all()

    return tmpl("dashboard.html", request, {
        "user":              current_user,
        "recent_detections": recent_detections,
        "recent_alerts":     recent_alerts,
        "total_detections":  total_detections,
        "total_suspicious":  total_suspicious,
        "total_vehicles":    total_vehicles,
        "total_alerts":      total_alerts,
        "stations":          stations,
    })


# ═══════════════════════════════════════════════════════════════════════════════
#  VIDEO PROCESSING
# ═══════════════════════════════════════════════════════════════════════════════

# Track active live session
_live_active = False
_live_session_id: Optional[int] = None


def _check_suspicious(plate: str, db: Session):
    """Look up a plate in suspicious_vehicles. Returns (bool, vehicle_row|None)."""
    vehicle = (db.query(SuspiciousVehicle)
               .filter(SuspiciousVehicle.license_plate == plate,
                       SuspiciousVehicle.is_active == True)
               .first())
    return (True, vehicle) if vehicle else (False, None)


def _log_detection(plate: str, confidence: float, is_suspicious: bool,
                   vehicle, session_id: Optional[int], frame: np.ndarray,
                   db: Session, user_id: Optional[int] = None) -> DetectionLog:
    """Persist a detection and fire an alert if suspicious."""
    img_filename = None
    if frame is not None:
        ts = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
        img_filename = f"{plate}_{ts}.jpg"
        cv2.imwrite(str(CAPTURE_DIR / img_filename), frame)

    log = DetectionLog(
        session_id=session_id,
        detected_plate=plate,
        confidence_score=round(confidence * 100, 1),
        is_suspicious=is_suspicious,
        suspicious_vehicle_id=vehicle.id if vehicle else None,
        image_filename=img_filename,
        created_by=user_id,
    )
    db.add(log)
    db.flush()

    if is_suspicious and vehicle:
        _send_alert(log.id, plate, vehicle, db)
        log.alert_sent = True

    db.commit()
    db.refresh(log)
    return log


def _send_alert(detection_log_id: int, plate: str,
                vehicle: SuspiciousVehicle, db: Session):
    """Create alert records for nearby (all) police stations."""
    stations = db.query(PoliceStation).limit(3).all()
    for station in stations:
        alert = AlertNotification(
            detection_log_id=detection_log_id,
            police_station_id=station.id,
            station_name=station.station_name,
            station_email=station.email,
            license_plate=plate,
            vehicle_info=json.dumps({
                "type": vehicle.vehicle_type,
                "color": vehicle.vehicle_color,
                "reason": vehicle.reason_for_flagging,
                "severity": vehicle.severity_level,
                "owner": vehicle.owner_name,
            }),
            severity_level=vehicle.severity_level,
            alert_type="system",
            alert_status="sent",
        )
        db.add(alert)
    logger.warning(f"ALERT: Suspicious plate {plate} detected! Notified {len(stations)} stations.")


async def _generate_video_frames(video_path: str, session_id: int):
    """Generator that yields MJPEG frames and pushes WS events for suspicious plates."""
    cap = cv2.VideoCapture(video_path)
    frame_idx = 0
    db = SessionLocal()

    try:
        while cap.isOpened():
            ret, frame = cap.read()
            if not ret:
                break

            frame = cv2.resize(frame, (800, 500))

            if frame_idx % 15 == 0:
                annotated, detections = detector.process_frame(frame.copy())

                for det in detections:
                    plate = det["plate"]
                    is_susp, vehicle = _check_suspicious(plate, db)
                    det["is_suspicious"] = is_susp

                    _log_detection(
                        plate=plate,
                        confidence=det["confidence"],
                        is_suspicious=is_susp,
                        vehicle=vehicle,
                        session_id=session_id,
                        frame=frame,
                        db=db,
                    )

                    x1, y1, x2, y2 = det["bbox"]
                    color = (0, 0, 255) if is_susp else (0, 255, 100)
                    cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 3)
                    cv2.putText(annotated, f"{'! ' if is_susp else ''}{plate}",
                                (x1, y1 - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.75, color, 2)

                    if is_susp:
                        asyncio.create_task(ws_manager.broadcast({
                            "type": "alert",
                            "plate": plate,
                            "severity": vehicle.severity_level if vehicle else "unknown",
                            "reason": vehicle.reason_for_flagging if vehicle else "",
                            "timestamp": datetime.datetime.utcnow().isoformat(),
                        }))

                frame = annotated

            _draw_hud(frame, frame_idx)
            ret, buffer = cv2.imencode('.jpg', frame)
            if ret:
                yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n'
                       + buffer.tobytes() + b'\r\n')

            frame_idx += 1
            await asyncio.sleep(0.03)
    finally:
        cap.release()
        db.close()


async def _generate_live_frames(session_id: int):
    """Generator for webcam live feed."""
    global _live_active
    cap = cv2.VideoCapture(0)
    db  = SessionLocal()
    _live_active = True

    try:
        while _live_active and cap.isOpened():
            ret, frame = cap.read()
            if not ret:
                break

            frame = cv2.resize(frame, (800, 500))
            annotated, detections = detector.process_frame(frame.copy())

            for det in detections:
                plate = det["plate"]
                is_susp, vehicle = _check_suspicious(plate, db)

                _log_detection(
                    plate=plate, confidence=det["confidence"],
                    is_suspicious=is_susp, vehicle=vehicle,
                    session_id=session_id, frame=frame, db=db,
                )

                x1, y1, x2, y2 = det["bbox"]
                color = (0, 0, 255) if is_susp else (0, 255, 100)
                cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 3)
                cv2.putText(annotated, f"{'! ' if is_susp else ''}{plate}",
                            (x1, y1 - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.75, color, 2)

                if is_susp:
                    await ws_manager.broadcast({
                        "type": "alert",
                        "plate": plate,
                        "severity": vehicle.severity_level if vehicle else "unknown",
                        "reason": vehicle.reason_for_flagging if vehicle else "",
                        "timestamp": datetime.datetime.utcnow().isoformat(),
                    })

            _draw_hud(annotated, 0)
            ret, buffer = cv2.imencode('.jpg', annotated)
            if ret:
                yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n'
                       + buffer.tobytes() + b'\r\n')

            await asyncio.sleep(0.05)
    finally:
        cap.release()
        db.close()
        _live_active = False


def _draw_hud(frame: np.ndarray, frame_idx: int):
    ts = datetime.datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
    cv2.putText(frame, ts, (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 1)
    cv2.putText(frame, "TrafficMonitor AI", (10, frame.shape[0] - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (100, 100, 100), 1)


# ── Video routes ──────────────────────────────────────────────────────────────

@app.post("/upload")
async def upload_video(
    request: Request,
    video: UploadFile = File(...),
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    filepath = UPLOAD_DIR / "current_video.mp4"
    with open(str(filepath), "wb") as f:
        f.write(await video.read())

    session = VideoSession(
        session_type="upload",
        video_filename=video.filename,
        created_by=current_user.id,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return RedirectResponse(url=f"/player?session_id={session.id}", status_code=302)


@app.get("/player", response_class=HTMLResponse)
async def player_page(
    request: Request,
    session_id: int = 0,
    current_user: User = Depends(require_user),
):
    return tmpl("player.html", request, {
        "user": current_user,
        "session_id": session_id,
        "mode": "upload",
    })


@app.get("/video_feed")
async def video_feed(session_id: int = 0):
    video_path = str(UPLOAD_DIR / "current_video.mp4")
    if not os.path.exists(video_path):
        raise HTTPException(status_code=404, detail="No video uploaded")
    return StreamingResponse(
        _generate_video_frames(video_path, session_id),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.get("/live", response_class=HTMLResponse)
async def live_page(
    request: Request,
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    session = VideoSession(session_type="live_camera", created_by=current_user.id)
    db.add(session)
    db.commit()
    db.refresh(session)
    return tmpl("player.html", request, {
        "user": current_user,
        "session_id": session.id,
        "mode": "live",
    })


@app.get("/live_feed")
async def live_feed(session_id: int = 0):
    return StreamingResponse(
        _generate_live_frames(session_id),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.post("/stop_live")
async def stop_live(current_user: User = Depends(require_user)):
    global _live_active
    _live_active = False
    return {"status": "stopped"}


# ═══════════════════════════════════════════════════════════════════════════════
#  API ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/detections")
async def api_detections(
    limit: int = 20,
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    rows = (db.query(DetectionLog)
            .order_by(DetectionLog.detection_timestamp.desc())
            .limit(limit).all())
    return [
        {
            "id": r.id,
            "plate": r.detected_plate,
            "confidence": r.confidence_score,
            "is_suspicious": r.is_suspicious,
            "timestamp": r.detection_timestamp.isoformat() if r.detection_timestamp else None,
            "image": f"/static/captures/{r.image_filename}" if r.image_filename else None,
            "alert_sent": r.alert_sent,
        }
        for r in rows
    ]


@app.get("/api/alerts")
async def api_alerts(
    limit: int = 20,
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    rows = (db.query(AlertNotification)
            .order_by(AlertNotification.created_at.desc())
            .limit(limit).all())
    return [
        {
            "id": r.id,
            "plate": r.license_plate,
            "station": r.station_name,
            "severity": r.severity_level,
            "status": r.alert_status,
            "timestamp": r.created_at.isoformat() if r.created_at else None,
            "vehicle_info": json.loads(r.vehicle_info) if r.vehicle_info else {},
        }
        for r in rows
    ]


@app.get("/api/suspicious_vehicles")
async def api_suspicious(
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    rows = db.query(SuspiciousVehicle).filter(SuspiciousVehicle.is_active == True).all()
    return [
        {
            "id": r.id,
            "plate": r.license_plate,
            "type": r.vehicle_type,
            "color": r.vehicle_color,
            "owner": r.owner_name,
            "reason": r.reason_for_flagging,
            "severity": r.severity_level,
            "reported_by": r.reported_by,
            "reported_date": r.reported_date,
        }
        for r in rows
    ]


@app.post("/api/suspicious_vehicles")
async def add_suspicious_vehicle(
    request: Request,
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    data = await request.json()
    existing = db.query(SuspiciousVehicle).filter(
        SuspiciousVehicle.license_plate == data.get("license_plate")
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Plate already exists")

    v = SuspiciousVehicle(
        license_plate=data.get("license_plate", "").upper(),
        vehicle_type=data.get("vehicle_type"),
        vehicle_color=data.get("vehicle_color"),
        owner_name=data.get("owner_name"),
        reason_for_flagging=data.get("reason_for_flagging", "Manual entry"),
        severity_level=data.get("severity_level", "medium"),
        reported_by=current_user.full_name or current_user.email,
    )
    db.add(v)
    db.commit()
    return {"status": "created", "id": v.id}


@app.get("/api/stats")
async def api_stats(
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    return {
        "total_detections": db.query(DetectionLog).count(),
        "suspicious_detections": db.query(DetectionLog).filter(DetectionLog.is_suspicious == True).count(),
        "total_alerts": db.query(AlertNotification).count(),
        "active_watchlist": db.query(SuspiciousVehicle).filter(SuspiciousVehicle.is_active == True).count(),
        "police_stations": db.query(PoliceStation).count(),
    }


@app.get("/api/police_stations")
async def api_stations(
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    rows = db.query(PoliceStation).all()
    return [
        {
            "id": r.id, "name": r.station_name, "code": r.station_code,
            "city": r.city, "state": r.state,
            "phone": r.phone_number, "email": r.email,
            "lat": r.latitude, "lng": r.longitude,
        }
        for r in rows
    ]


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws/alerts")
async def websocket_alerts(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)


# ── Favicon (suppress 404) ────────────────────────────────────────────────────

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)


# ═══════════════════════════════════════════════════════════════════════════════
#  PDF TEXT EXTRACTION  (PDF.co API)
# ═══════════════════════════════════════════════════════════════════════════════

PDF_UPLOAD_DIR = BASE_DIR / "uploads" / "pdfs"
PDF_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


@app.post("/api/extract-pdf")
async def extract_pdf(
    file: UploadFile = File(...),
    pages: str = Form(default=""),
    ocr: bool = Form(default=True),
    current_user: User = Depends(require_user),
):
    """
    Upload a PDF and extract its text using PDF.co.
    • pages: comma/range string e.g. "1-3,5" or leave blank for all.
    • ocr:   set true for scanned / image-based PDFs.
    Returns JSON with extracted text and metadata.
    """
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted")

    file_bytes = await file.read()
    if len(file_bytes) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    # Save a local copy for audit / re-processing
    save_path = PDF_UPLOAD_DIR / file.filename
    save_path.write_bytes(file_bytes)

    try:
        result = pdf_extractor.extract_text_from_bytes(
            file_bytes=file_bytes,
            filename=file.filename,
            pages=pages,
            ocr_enabled=ocr,
        )
    except RuntimeError as exc:
        logger.error(f"PDF.co extraction failed: {exc}")
        raise HTTPException(status_code=502, detail=str(exc))
    except TimeoutError as exc:
        logger.error(f"PDF.co job timed out: {exc}")
        raise HTTPException(status_code=504, detail=str(exc))

    return JSONResponse({
        "status":     "success",
        "filename":   file.filename,
        "page_count": result["page_count"],
        "char_count": len(result["text"]),
        "text":       result["text"],
        "body_url":   result["body_url"],
    })


@app.post("/api/extract-pdf-url")
async def extract_pdf_from_url(
    request: Request,
    current_user: User = Depends(require_user),
):
    """
    Extract text from a publicly-accessible PDF URL via PDF.co.
    Body JSON: { "url": "https://...", "pages": "1-3", "ocr": true }
    """
    data  = await request.json()
    url   = data.get("url", "").strip()
    pages = data.get("pages", "")
    ocr   = data.get("ocr", True)

    if not url:
        raise HTTPException(status_code=400, detail="'url' field is required")

    try:
        result = pdf_extractor.extract_text_from_url(
            pdf_url=url, pages=pages, ocr_enabled=ocr
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc))

    return JSONResponse({
        "status":     "success",
        "source_url": url,
        "page_count": result["page_count"],
        "char_count": len(result["text"]),
        "text":       result["text"],
        "body_url":   result["body_url"],
    })


@app.get("/api/pdf-credits")
async def pdf_credits(current_user: User = Depends(require_user)):
    """Return remaining PDF.co API credits."""
    try:
        return pdf_extractor.get_api_credits()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/watchlist", response_class=HTMLResponse)
async def watchlist_page(
    request: Request,
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    vehicles = db.query(SuspiciousVehicle).filter(SuspiciousVehicle.is_active == True).all()
    return tmpl("watchlist.html", request, {
        "user": current_user,
        "vehicles": vehicles,
    })


@app.get("/alerts", response_class=HTMLResponse)
async def alerts_page(
    request: Request,
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    alerts = (db.query(AlertNotification)
              .order_by(AlertNotification.created_at.desc())
              .limit(50).all())
    return tmpl("alerts.html", request, {
        "user": current_user,
        "alerts": alerts,
    })



if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
