import cv2
import re
import asyncio
from ultralytics import YOLO
import easyocr
import numpy as np

# Load models
# We use the nano model for speed
try:
    model = YOLO("yolov8n.pt")
    reader = easyocr.Reader(['en'], gpu=False) # fallback to CPU
except Exception as e:
    print(f"Error loading models: {e}")

# Classes in COCO: 2:car, 3:motorcycle, 5:bus, 7:truck
VEHICLE_CLASSES = [2, 3, 5, 7]

def clean_plate(text):
    """Remove special chars and spaces from OCR output"""
    return re.sub(r'[\W_]+', '', text).upper()

class AIProcessor:
    def __init__(self, db_session, websocket_manager):
        self.db = db_session
        self.ws_manager = websocket_manager
        
    async def process_frame(self, frame):
        # 1. Vehicle Detection
        results = model(frame, verbose=False)[0]
        
        detected_plates = []
        
        for box in results.boxes:
            cls_id = int(box.cls[0])
            if cls_id in VEHICLE_CLASSES:
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                
                # Extract Vehicle ROI
                roi = frame[y1:y2, x1:x2]
                if roi.shape[0] < 10 or roi.shape[1] < 10:
                    continue
                    
                # 2. Number Plate Detection & OCR
                # For realistic speed without a dedicated ALPR model, we just OCR the whole vehicle ROI or its lower half
                gray_roi = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
                ocr_results = reader.readtext(gray_roi)
                
                plate_text = ""
                for (bbox, text, prob) in ocr_results:
                    if prob > 0.3:
                        plate_text += str(text)
                
                cleaned_plate = clean_plate(plate_text)
                
                if cleaned_plate and len(cleaned_plate) > 4: # basic filter
                    detected_plates.append((x1, y1, x2, y2, cleaned_plate))
        
        # We return the frame and the detected plates
        return frame, detected_plates
