"""
Number plate detection & OCR pipeline.
Uses YOLOv8 for vehicle detection and EasyOCR for plate text extraction.
Falls back gracefully if models are not available.
"""
import cv2
import json
import re
import os
import numpy as np
import logging
import google.generativeai as genai

logger = logging.getLogger(__name__)

# ── Load models lazily ────────────────────────────────────────────────────────
_yolo_model = None
_ocr_reader  = None

# ── Gemini Setup ──────────────────────────────────────────────────────────────
_GEMINI_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
_gemini_model = None

if _GEMINI_KEY:
    genai.configure(api_key=_GEMINI_KEY)
    _gemini_model = genai.GenerativeModel(os.getenv("GEMINI_MODEL_PLATE", "gemini-2.0-flash"))


# ── Plate validation ─────────────────────────────────────────────────────────

_EXCLUDE_WORDS = [
    "TOYOTA", "HONDA", "SUZUKI", "HYUNDAI", "TATA", "MAHINDRA", "BMW", "AUDI", "FORD", "KIA", "MARUTI",
    "STORYBLOCKS", "STORY", "BLOCKS", "ADOBE", "STOCK", "GETTY", "SHUTTERSTOCK", "PREMIUM",
]


def is_plausible_plate(text: str) -> bool:
    """Generic plate plausibility check.

    This is intentionally stricter than "any alnum" but looser than Indian-only rules,
    so short watchlist plates like "GYP274" can pass without letting watermark noise through.
    """
    if not text:
        return False
    if not re.fullmatch(r"[A-Z0-9]+", text):
        return False
    if not (5 <= len(text) <= 12):
        return False
    for word in _EXCLUDE_WORDS:
        if word in text:
            return False

    letters = len(re.findall(r"[A-Z]", text))
    digits = len(re.findall(r"[0-9]", text))

    # Must contain both letters and digits.
    if letters == 0 or digits == 0:
        return False
    # Avoid very letter-heavy strings that are rarely plates.
    if letters >= 9 and digits <= 1:
        return False
    # For short strings, require at least 2 digits to cut down false positives.
    if len(text) <= 6 and digits < 2:
        return False

    return True


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


def plate_match_keys(raw_text: str) -> set[str]:
    """Return a set of normalized plate keys for matching.

    Used to match OCR outputs against watchlist entries even when there are:
    - spaces/hyphens
    - common OCR confusions (O/0, I/1, Z/2, S/5, B/8)
    """
    cleaned = clean_plate(raw_text or "")
    if not cleaned:
        return set()

    keys: set[str] = {cleaned}
    swaps = [
        ("O", "0"), ("0", "O"),
        ("I", "1"), ("1", "I"),
        ("Z", "2"), ("2", "Z"),
        ("S", "5"), ("5", "S"),
        ("B", "8"), ("8", "B"),
    ]
    for a, b in swaps:
        keys.add(cleaned.replace(a, b))

    # Best-effort: normalize first two chars as letters for Indian plates.
    if len(cleaned) >= 2:
        state_map = str.maketrans({"0": "O", "1": "I", "2": "Z", "5": "S", "8": "B"})
        keys.add(cleaned[:2].translate(state_map) + cleaned[2:])

    return {k for k in keys if k}


def _normalize_plate_candidate(text: str) -> str:
    """Try common OCR confusion fixes and return a validated plate, else '' ."""
    cleaned = clean_plate(text)
    if not cleaned:
        return ""
    if is_valid_plate(cleaned):
        return cleaned

    variants: list[str] = []
    variants.append(cleaned.replace("O", "0"))
    variants.append(cleaned.replace("I", "1"))
    variants.append(cleaned.replace("Z", "2"))
    variants.append(cleaned.replace("S", "5"))
    variants.append(cleaned.replace("B", "8"))

    # Also fix state code characters that got OCR'd as digits.
    if len(cleaned) >= 2:
        state_map = str.maketrans({"0": "O", "1": "I", "2": "Z", "5": "S", "8": "B"})
        variants.append(cleaned[:2].translate(state_map) + cleaned[2:])

    for v in variants:
        if v and is_valid_plate(v):
            return v

    # Allow a stricter generic format for non-Indian watchlist plates.
    allow_generic = os.getenv("PLATE_ALLOW_GENERIC", "1").strip() not in ("0", "false", "False", "no", "NO")
    if allow_generic:
        if is_plausible_plate(cleaned):
            return cleaned
        for v in variants:
            if v and is_plausible_plate(v):
                return v

    return ""


def _plate_candidates_from_easyocr(reader, image: np.ndarray) -> list[tuple[str, float]]:
    """Return list of (plate, confidence) candidates."""
    try:
        results = reader.readtext(image)
    except Exception as exc:
        logger.debug(f"EasyOCR error: {exc}")
        return []

    out: list[tuple[str, float]] = []
    for (bbox, raw_text, conf) in results:
        plate = _normalize_plate_candidate(raw_text)
        if plate:
            out.append((plate, float(conf)))
        else:
            cleaned = clean_plate(raw_text)
            if cleaned:
                logger.debug(f"Filter rejected OCR text: {cleaned}")
    return out


def _find_plate_crops(bgr_region: np.ndarray) -> list[np.ndarray]:
    """Find likely plate crops within a region using contour heuristics."""
    if bgr_region is None or bgr_region.size == 0:
        return []

    gray = cv2.cvtColor(bgr_region, cv2.COLOR_BGR2GRAY)
    gray = cv2.bilateralFilter(gray, 9, 75, 75)
    edges = cv2.Canny(gray, 80, 200)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)

    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return []

    h, w = gray.shape[:2]
    area_min = max(500, int(0.01 * w * h))

    rects: list[tuple[int, int, int, int, float]] = []
    for cnt in contours:
        x, y, rw, rh = cv2.boundingRect(cnt)
        area = rw * rh
        if area < area_min:
            continue
        if rh == 0:
            continue
        aspect = rw / float(rh)
        # Typical plate aspect ratio ~ 2-6
        if not (2.0 <= aspect <= 6.5):
            continue
        if rw < 60 or rh < 15:
            continue
        rects.append((x, y, rw, rh, area))

    rects.sort(key=lambda r: r[4], reverse=True)
    crops: list[np.ndarray] = []
    for (x, y, rw, rh, area) in rects[:3]:
        pad_x = int(rw * 0.05)
        pad_y = int(rh * 0.15)
        x1 = max(0, x - pad_x)
        y1 = max(0, y - pad_y)
        x2 = min(w, x + rw + pad_x)
        y2 = min(h, y + rh + pad_y)
        crop = bgr_region[y1:y2, x1:x2]
        if crop.size:
            crops.append(crop)
    return crops


def _preprocess_for_ocr(bgr_or_gray: np.ndarray) -> np.ndarray:
    if len(bgr_or_gray.shape) == 3:
        gray = cv2.cvtColor(bgr_or_gray, cv2.COLOR_BGR2GRAY)
    else:
        gray = bgr_or_gray

    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)
    _, thresh = cv2.threshold(enhanced, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return thresh


def _gemini_extract_plate_from_image(image: np.ndarray) -> str:
    if not _gemini_model:
        return ""
    try:
        # Gemini generally performs better with color crops.
        if len(image.shape) == 2:
            bgr = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
        else:
            bgr = image
        ok, buffer = cv2.imencode(".jpg", bgr)
        if not ok:
            return ""
        img_data = buffer.tobytes()

        prompt = (
            "You are reading a vehicle number plate from a cropped image. "
            "Extract the exact license plate characters visible on the physical plate. "
            "Ignore watermarks/stock text (e.g., Storyblocks/Adobe Stock/Gettty/Shutterstock) and any text not printed on the plate. "
            "Return ONLY valid JSON, no markdown: {\"plate\":\"...\"}. "
            "Rules: plate must be uppercase A-Z0-9 only, length 5 to 12, no spaces. "
            "Prefer a full plate; if the plate is not clearly readable, return {\"plate\":\"EMPTY\"}. "
            "Examples of valid plates: AP39UX7027, 22BH1234AA, GYP274."
        )

        generation_config = {
            "temperature": 0.0,
            "top_p": 0.1,
            "top_k": 1,
            "max_output_tokens": 64,
        }

        response = _gemini_model.generate_content(
            [
                prompt,
                {"mime_type": "image/jpeg", "data": img_data},
            ],
            generation_config=generation_config,
        )

        raw = (getattr(response, "text", "") or "").strip()
        m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not m:
            candidate = raw
        else:
            try:
                candidate = json.loads(m.group(0)).get("plate", "")
            except Exception:
                candidate = raw

        candidate = str(candidate or "").strip().upper()
        if candidate in ("EMPTY", "NONE", "N/A"):
            return ""
        return _normalize_plate_candidate(candidate)
    except Exception as exc:
        logger.warning(f"Gemini plate OCR failed: {exc}")
        return ""


def is_valid_plate(text: str) -> bool:
    """
    Strict filter for Indian number plates.
    Accepts common formats:
    - Standard: MH12AB1234, DL3CAB5678, AP39UX7027
    - BH Series: 22BH1234AA
    """
    # Reject short strings like "STC7" that are common false positives.
    # (Non-Indian short watchlist plates are handled separately by is_plausible_plate.)
    if not (8 <= len(text) <= 12):
        return False

    for word in _EXCLUDE_WORDS:
        if word in text:
            return False

    # Regex patterns for Indian plates (strict)
    patterns = [
        r'^[A-Z]{2}[0-9]{1,2}[A-Z]{1,3}[0-9]{3,4}$',  # MH12AB1234, DL3CAB5678, AP39UX7027
        r'^[0-9]{2}BH[0-9]{4}[A-Z]{1,2}$',            # 22BH1234AA
    ]
    
    for p in patterns:
        if re.match(p, text):
            # Final sanity check: must not be just a long string of letters
            if len(re.findall(r'[0-9]', text)) < 2 and len(text) > 4:
                return False
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
    if reader == "disabled" and not _gemini_model:
        return "", 0.0

    # Build candidate regions (plates can appear lower/middle depending on vehicle/camera).
    h = roi.shape[0]
    regions: list[np.ndarray] = []
    lower = roi[int(h * 0.55):, :]
    mid = roi[int(h * 0.35):int(h * 0.90), :]
    regions.extend([lower, mid, roi])

    # Collect crops across regions.
    crops: list[np.ndarray] = []
    for region in regions:
        if region is None or region.size == 0:
            continue
        if region.shape[0] < 10 or region.shape[1] < 10:
            continue
        region_crops = _find_plate_crops(region)
        if region_crops:
            crops.extend(region_crops)
        else:
            crops.append(region)

    # Keep a bounded number of crops (largest-first happens inside _find_plate_crops).
    if len(crops) > 8:
        crops = crops[:8]

    # Gemini-first: returns a plate if it passes normalization/validation.
    if _gemini_model:
        max_tries = int(os.getenv("GEMINI_PLATE_CROP_TRIES", "2"))
        for crop in crops[:max(1, max_tries)]:
            gem = _gemini_extract_plate_from_image(crop)
            if gem:
                return gem, 1.0

    # Local OCR fallback (also strict via is_valid_plate).
    if reader == "disabled":
        return "", 0.0
    best_plate = ""
    best_conf = 0.0
    for crop in crops:
        pre = _preprocess_for_ocr(crop)
        candidates = _plate_candidates_from_easyocr(reader, pre)
        if not candidates:
            continue
        plate, conf = max(candidates, key=lambda x: x[1])
        if conf > best_conf:
            best_plate, best_conf = plate, conf

    if best_plate:
        return best_plate, round(best_conf, 2)

    return "", 0.0


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
