import cv2
import argparse
import os

def process_video(input_path, output_path, cascade_path):
    # 1. OPTIMIZATION: Load the classifier ONCE outside the loop
    if not os.path.exists(cascade_path):
        print(f"Error: Cannot find cascade file at {cascade_path}")
        return
        
    car_cascade = cv2.CascadeClassifier(cascade_path)

    # Initialize video capture
    vid = cv2.VideoCapture(input_path)
    if not vid.isOpened():
        print(f"Error: Cannot open video {input_path}")
        return

    # Get video properties
    fps = vid.get(cv2.CAP_PROP_FPS)
    if fps == 0 or fps is None:
        fps = 30.0 # fallback

    # We enforce a resize to (700, 500) based on your original code
    output_width, output_height = 700, 500
    
    # 2. OUTPUT FEATURE: Initialize video writer to save the result
    fourcc = cv2.VideoWriter_fourcc(*'mp4v') # Codec for mp4
    out = cv2.VideoWriter(output_path, fourcc, fps, (output_width, output_height))

    print(f"Processing `{input_path}`...")
    print(f"Output will be saved to `{output_path}`")

    while True:
        r, frame = vid.read()
        if not r:
            break
            
        gry = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        
        # Detect vehicles
        cars = car_cascade.detectMultiScale(gry, 1.2, 3)
        
        for (x, y, w, h) in cars:
            # 3. BUGFIX: Fixed x+h -> x+w for correct bounding box width
            cv2.rectangle(frame, (x, y), (x+w, y+h), (0, 0, 255), 3)
            
        frame = cv2.resize(frame, (output_width, output_height))
        
        # Write frame to the output video
        out.write(frame)
        
        cv2.imshow("Vehicle Detection", frame)
        if cv2.waitKey(1) & 0xff == ord("p"): # Press 'p' to quit
            break

    vid.release()
    out.release()
    cv2.destroyAllWindows()
    print("Done!")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Vehicle Detection in Video")
    parser.add_argument("-i", "--input", default=r"D:\dowl\highway.mp4", help="Path to input video")
    parser.add_argument("-o", "--output", default=r"D:\dowl\output_highway.mp4", help="Path to save output video")
    parser.add_argument("-c", "--cascade", default=r"d:\dowl\iot_project\haarcascade_car.xml", help="Path to cascade XML")
    
    args = parser.parse_args()
    process_video(args.input, args.output, args.cascade)
