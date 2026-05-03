import os
import cv2
from flask import Flask, render_template, Response, request, redirect, url_for

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

CASCADE_PATH = r"d:\dowl\iot_project\haarcascade_car.xml"

def generate_frames(video_path):
    if not os.path.exists(CASCADE_PATH):
        raise FileNotFoundError(f"Cascade XML not found at {CASCADE_PATH}")

    car_cascade = cv2.CascadeClassifier(CASCADE_PATH)
    vid = cv2.VideoCapture(video_path)
    
    while vid.isOpened():
        success, frame = vid.read()
        if not success:
            break
            
        frame = cv2.resize(frame, (800, 500))
        gry = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        
        cars = car_cascade.detectMultiScale(gry, 1.2, 3)
        for (x, y, w, h) in cars:
            # Draw glowing bounding boxes
            cv2.rectangle(frame, (x, y), (x+w, y+h), (255, 0, 128), 3) # Neon purple
            
        # Encode as JPEG
        ret, buffer = cv2.imencode('.jpg', frame)
        if not ret:
            continue
            
        frame_bytes = buffer.tobytes()
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
               
    vid.release()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'video' not in request.files:
        return redirect(url_for('index'))
        
    file = request.files['video']
    if file.filename == '':
        return redirect(url_for('index'))
        
    if file:
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], 'current_video.mp4')
        file.save(filepath)
        return render_template('player.html')

@app.route('/video_feed')
def video_feed():
    video_path = os.path.join(app.config['UPLOAD_FOLDER'], 'current_video.mp4')
    return Response(generate_frames(video_path), mimetype='multipart/x-mixed-replace; boundary=frame')

if __name__ == '__main__':
    app.run(debug=True, port=5000)
