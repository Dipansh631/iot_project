"""
Number plate detection & OCR pipeline.
Uses YOLOv8 for vehicle detection and EasyOCR for plate text extraction.
Falls back gracefully if models are not available.
"""
import cv2
import re
import os
import numpy as np
import logging
import pdf_extractor
import google.generativeai as genai

logger = logging.getLogger(__name__)

# ── Load models lazily ────────────────────────────────────────────────────────
_yolo_model = None
_ocr_reader  = None

# ── Gemini Setup ──────────────────────────────────────────────────────────────
_GEMINI_KEY = os.getenv("GEMINI_API_KEY")
_gemini_model = None

if _GEMINI_KEY:
    genai.configure(api_key=_GEMINI_KEY)
    _gemini_model = genai.GenerativeModel('gemini-2.0-flash')


def _get_yolo():
    global _yolo_model
    if _yolo_model is None:
        try:
            from ultralytics import YOLO
            _yolo_model = YOLO("yolov8n.pt")
            logger.info("YOLOv8n model loaded successfully.")
        except Exception as e:
            logger.warning(f"Could not load YOLO model: {e}. Falling back to Haar cascade.")
            _yolo_model = "haar"
    return _yolo_model


def _get_ocr():
    global _ocr_reader
    if _ocr_reader is None:
        try:
            import easyocr
            _ocr_reader = easyocr.Reader(['en'], gpu=False, verbose=False)
            logger.info("EasyOCR reader loaded successfully.")
        except Exception as e:
            logger.warning(f"Could not load EasyOCR: {e}. OCR disabled.")
            _ocr_reader = "disabled"
    return _ocr_reader


# ── Haar cascade fallback ─────────────────────────────────────────────────────
_CASCADE_PATH = os.path.join(os.path.dirname(__file__), "haarcascade_car.xml")
_haar_cascade = None


def _get_haar():
    global _haar_cascade
    if _haar_cascade is None and os.path.exists(_CASCADE_PATH):
        _haar_cascade = cv2.CascadeClassifier(_CASCADE_PATH)
    return _haar_cascade


# ── Plate text cleaning ───────────────────────────────────────────────────────

def clean_plate(raw_text: str) -> str:
    """Strip non-alphanumeric chars, uppercase, and remove common prefixes like IND."""
    text = raw_text.upper()
    # Remove 'IND' or 'INDIA' if it's at the start (common on HSRP plates)
    text = re.sub(r'^(IND|INDIA)', '', text)
    cleaned = re.sub(r'[^A-Z0-9]', '', text)
    return cleaned


def is_valid_plate(text: str) -> bool:
    """
    Strict filter for Indian number plates.
    Patterns:
    - Standard: MH12AB1234, DL3CAB5678
    - Old: ABC1234, MHA1234
    - BH Series: 22BH1234AA
    """
    if not (4 <= len(text) <= 12):
        return False
    
    # Common non-plate words to exclude (brands, watermarks, country codes)
    EXCLUDE_WORDS = [
        "TOYOTA", "HONDA", "SUZUKI", "HYUNDAI", "TATA", "MAHINDRA", "BMW", "AUDI", "FORD", "KIA", "MARUTI",
        "STORYBLOCKS", "STORY", "BLOCKS", "ADOBE", "STOCK", "GETTY", "SHUTTERSTOCK", "PREMIUM"
    ]
    for word in EXCLUDE_WORDS:
        if word in text:
            return False

    # Regex patterns for Indian plates
    patterns = [
        r'^[A-Z]{2}[0-9]{1,2}[A-Z]{1,3}[0-9]{1,4}$',  # MH12AB1234, DL3CAB5678
        r'^[A-Z]{3}[0-9]{1,4}$',                       # ABC1234
        r'^[0-9]{2}BH[0-9]{4}[A-Z]{1,2}$',             # 22BH1234AA
        r'^[A-Z]{2}[0-9]{1,2}[0-9]{4}$',              # MH121234
    ]
    
    for p in patterns:
        if re.match(p, text):
            # Final sanity check: must not be just a long string of letters
            if len(re.findall(r'[0-9]', text)) < 2 and len(text) > 4:
                return False
            return True
            
    # Fallback for slightly noisy OCR but still looking like a plate
    # Must have State code (2 letters) + some digits + some letters
    if len(text) >= 7 and re.match(r'^[A-Z]{2}', text):
        has_letters = len(re.findall(r'[A-Z]', text)) >= 3
        has_digits = len(re.findall(r'[0-9]', text)) >= 2
        if has_letters and has_digits:
            return True

    return False


# ── Core processing functions ─────────────────────────────────────────────────

def detect_vehicles_yolo(frame: np.ndarray) -> list[tuple]:
    """
    Detect vehicles with YOLOv8.
    Returns list of (x1, y1, x2, y2, confidence).
    """
    model = _get_yolo()
    if model == "haar":
        return _detect_vehicles_haar(frame)

    VEHICLE_CLASSES = [2, 3, 5, 7]   # car, motorcycle, bus, truck in COCO
    results = model(frame, verbose=False)[0]
    boxes = []
    for box in results.boxes:
        cls_id = int(box.cls[0])
        if cls_id in VEHICLE_CLASSES:
            x1, y1, x2, y2 = map(int, box.xyxy[0])
            conf = float(box.conf[0])
            boxes.append((x1, y1, x2, y2, conf))
    return boxes


def _detect_vehicles_haar(frame: np.ndarray) -> list[tuple]:
    cascade = _get_haar()
    if cascade is None:
        return []
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    cars = cascade.detectMultiScale(gray, 1.2, 3, minSize=(60, 60))
    return [(x, y, x+w, y+h, 0.75) for (x, y, w, h) in cars]


def extract_plate_text(roi: np.ndarray) -> tuple[str, float]:
    """
    Run OCR on the vehicle ROI (or lower 40% where the plate typically is).
    Returns (plate_text, confidence).
    """
    reader = _get_ocr()
    if reader == "disabled":
        return "", 0.0

    # Focus on lower half of vehicle where plate usually is
    h = roi.shape[0]
    plate_region = roi[int(h * 0.55):, :]

    if plate_region.shape[0] < 10 or plate_region.shape[1] < 10:
        plate_region = roi

    # Enhance contrast and sharpen for OCR
    gray = cv2.cvtColor(plate_region, cv2.COLOR_BGR2GRAY)
    
    # Apply CLAHE (Contrast Limited Adaptive Histogram Equalization)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8,8))
    gemini_input = clahe.apply(gray)
    
    # Thresholding to isolate black text on light background (or vice versa)
    # Using Otsu's thresholding to handle varying lighting
    _, thresh = cv2.threshold(gemini_input, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    
    # Use the thresholded image for fallback OCR to eliminate light-grey watermarks
    fallback_input = thresh

    # --- Gemini Strategy (Best Accuracy) ---
    if _gemini_model:
        try:
            logger.info("Attempting high-accuracy OCR via Google Gemini...")
            # Encode image to JPEG (Gemini works best with clear grayscale/color)
            ret, buffer = cv2.imencode('.jpg', gemini_input)
            if ret:
                img_data = buffer.tobytes()
                # Extremely strict prompt for Gemini to ignore watermarks
                prompt = (
                    "You are a traffic AI. Extract the REAL vehicle license plate number from this image. "
                    "IMPORTANT: Ignore any watermarks like 'Storyblocks', 'Adobe Stock', or similar grey text in the middle of the frame. "
                    "Only extract the text from the actual metallic/plastic number plate mounted on the vehicle. "
                    "Return ONLY the plate number with no spaces. If you cannot see a clear number plate, return 'EMPTY'."
                )
                
                response = _gemini_model.generate_content([
                    prompt,
                    {"mime_type": "image/jpeg", "data": img_data}
                ])
                
                gemini_text = response.text.strip().upper()
                # Remove common prefixes and clean
                gemini_plate = clean_plate(gemini_text)
                
                if is_valid_plate(gemini_plate):
                    logger.info(f"Gemini extracted valid plate: {gemini_plate}")
                    return gemini_plate, 1.0
                else:
                    logger.debug(f"Gemini output '{gemini_text}' did not pass validation.")
        except Exception as e:
            logger.warning(f"Gemini OCR failed: {e}. Falling back...")

    # --- PDF.co Strategy (Legacy High Accuracy) ---
    try:
        # Encode ROI to JPEG bytes for API upload
        ret, buffer = cv2.imencode('.jpg', fallback_input)
        if ret:
            logger.info("Attempting high-accuracy OCR via PDF.co...")
            result = pdf_extractor.extract_text_from_bytes(
                file_bytes=buffer.tobytes(),
                filename="plate_crop.jpg",
                ocr_enabled=True
            )
            # PDF.co sometimes returns text with spaces/newlines even with inline:true
            raw_pdf_text = result.get("text", "").strip()
            
            # Split by any whitespace/newline to get individual words/lines
            potential_plates = []
            for word in re.split(r'[\s\n\r]+', raw_pdf_text):
                cleaned = clean_plate(word)
                if is_valid_plate(cleaned):
                    potential_plates.append(cleaned)
            
            # If no single word matches, try cleaning the whole line (for spaced plates)
            if not potential_plates:
                for line in raw_pdf_text.splitlines():
                    cleaned = clean_plate(line)
                    if is_valid_plate(cleaned):
                        potential_plates.append(cleaned)

            if potential_plates:
                # Pick the longest one that passes (usually the full plate)
                best_plate = max(potential_plates, key=len)
                logger.info(f"PDF.co extracted valid plate: {best_plate}")
                return best_plate, 1.0
            else:
                logger.debug(f"PDF.co found text but none matched plate pattern: {raw_pdf_text}")
    except Exception as e:
        logger.warning(f"PDF.co OCR failed: {e}. Falling back to EasyOCR.")

    # --- EasyOCR Fallback (Local/Faster) ---
    try:
        # Use thresholded image for local OCR too
        ocr_results = reader.readtext(fallback_input)
    except Exception as e:
        logger.debug(f"OCR error: {e}")
        return "", 0.0

    if not ocr_results:
        return "", 0.0

    # Filter results by pattern
    valid_results = []
    for res in ocr_results:
        raw_text = res[1]
        confidence = res[2]
        plate = clean_plate(raw_text)
        if is_valid_plate(plate):
            valid_results.append((plate, confidence))
        else:
            if plate:
                logger.debug(f"Filter REJECTED potential plate: {plate}")

    if not valid_results:
        return "", 0.0

    # Pick the result with highest confidence among valid patterns
    best = max(valid_results, key=lambda x: x[1])
    return (best[0], round(best[1], 2))


def process_frame(frame: np.ndarray) -> tuple[np.ndarray, list[dict]]:
    """
    Full pipeline: detect vehicles → extract plate text.
    Returns (annotated_frame, list_of_detections).
    Each detection: {plate, confidence, bbox, is_suspicious, vehicle_data}
    """
    vehicles = detect_vehicles_yolo(frame)
    detections = []

    for (x1, y1, x2, y2, vconf) in vehicles:
        roi = frame[y1:y2, x1:x2]
        if roi.shape[0] < 20 or roi.shape[1] < 20:
            continue

        plate, oconf = extract_plate_text(roi)

        if plate:
            detections.append({
                "plate": plate,
                "confidence": oconf,
                "bbox": [x1, y1, x2, y2],
                "is_suspicious": False,
                "vehicle_data": None,
            })

        # Draw vehicle box (will be coloured by caller after DB check)
        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
        if plate:
            cv2.putText(frame, plate, (x1, y1 - 8),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)

    return frame, detections
