from flask import Flask, request, jsonify, render_template, send_file, Response
import requests
import cv2
import os
from datetime import datetime, timedelta
import threading
import ffmpeg  # Import ffmpeg-python

app = Flask(__name__, static_folder='static')

# Global variables to track progress and cancellation
progress = {
    "total_images": 0,
    "downloaded_images": 0,
    "status": "idle"  # Can be "idle", "downloading", "creating_timelapse", "converting", "done"
}
cancel_event = threading.Event()  # Thread-safe event for cancellation

@app.route('/static/timelapse.mp4')
def serve_video():
    path = 'static/timelapse.mp4'
    range_header = request.headers.get('Range', None)
    if range_header:
        # Handle range requests
        return Response(response=open(path, 'rb'), content_type='video/mp4', status=206, direct_passthrough=True)
    else:
        # Serve the full file
        return send_file(path, mimetype='video/mp4')

def create_timelapse_process(start_date, end_date):
    global progress

    # Push the Flask application context
    with app.app_context():
        try:
            # Fetch images for the date range
            images = []
            current_date = datetime.strptime(start_date, "%Y-%m-%d")
            end_date_obj = datetime.strptime(end_date, "%Y-%m-%d")

            # Update progress
            progress["status"] = "downloading"

            # Fetch all images for the date range
            while current_date <= end_date_obj and not cancel_event.is_set():
                date_str = current_date.strftime("%Y-%m-%d")
                api_url = f"https://epic.gsfc.nasa.gov/api/natural/date/{date_str}"
                response = requests.get(api_url)
                if response.status_code == 200:
                    images.extend(response.json())
                current_date += timedelta(days=1)

            # Check if cancellation was requested
            if cancel_event.is_set():
                progress["status"] = "idle"
                cancel_event.clear()  # Reset the event
                cleanup_images()  # Clean up downloaded images
                return

            # Check if images are available
            if not images:
                progress["status"] = "idle"
                return

            # Update total_images to reflect the actual number of images
            progress["total_images"] = len(images)
            progress["downloaded_images"] = 0

            # Create a directory to store downloaded images
            if not os.path.exists("images"):
                os.makedirs("images")

            # Download images from NASA EPIC API
            for idx, image in enumerate(images):
                if cancel_event.is_set():
                    break

                image_url = f"https://epic.gsfc.nasa.gov/archive/natural/{image['date'].split(' ')[0].replace('-', '/')}/png/{image['image']}.png"
                image_data = requests.get(image_url).content
                with open(f"images/image_{idx}.png", "wb") as f:
                    f.write(image_data)

                # Update progress
                progress["downloaded_images"] = idx + 1

            # Check if cancellation was requested
            if cancel_event.is_set():
                progress["status"] = "idle"
                cancel_event.clear()  # Reset the event
                cleanup_images()  # Clean up downloaded images
                return

            # Create a timelapse video using OpenCV
            progress["status"] = "creating_timelapse"
            timelapse_path = "static/timelapse_raw.mp4"  # Temporary raw video
            create_timelapse("images", timelapse_path)

            # Convert the video to a web-friendly format using FFmpeg
            progress["status"] = "converting"
            web_friendly_path = "static/timelapse.mp4"
            convert_to_web_friendly(timelapse_path, web_friendly_path)

            # Clean up downloaded images and raw video
            cleanup_images()
            if os.path.exists(timelapse_path):
                os.remove(timelapse_path)

            # Update progress
            progress["status"] = "done"
            print("Timelapse creation and conversion completed. Progress status set to 'done'.")
        except Exception as e:
            progress["status"] = "idle"
            print(f"Error in timelapse process: {e}")

def cleanup_images():
    """Delete all downloaded images."""
    temp_images_directory_path = 'images/'
    if os.path.exists(temp_images_directory_path):
        for filename in os.listdir(temp_images_directory_path):
            file_path = os.path.join(temp_images_directory_path, filename)
            try:
                if os.path.isfile(file_path):
                    os.remove(file_path)
                    print(f"Deleted: {file_path}")
            except Exception as e:
                print(f"Error deleting {file_path}: {e}")

def create_timelapse(image_folder, output_path, fps=5):
    # Get list of images in the folder
    images = [img for img in os.listdir(image_folder) if img.endswith(".png")]
    images.sort()  # Ensure images are in the correct order

    # Read the first image to get dimensions
    first_image_path = os.path.join(image_folder, images[0])
    frame = cv2.imread(first_image_path)
    height, width, layers = frame.shape

    # Initialize video writer
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')  # Codec for MP4
    video = cv2.VideoWriter(output_path, fourcc, fps, (width, height))

    # Add images to the video
    for image in images:
        image_path = os.path.join(image_folder, image)
        frame = cv2.imread(image_path)
        video.write(frame)

    # Release the video writer
    video.release()

def convert_to_web_friendly(input_path, output_path):
    """Convert the video to a web-friendly format using FFmpeg."""
    try:
        (
            ffmpeg
            .input(input_path)
            .output(output_path, vcodec='libx264', pix_fmt='yuv420p', crf=23, preset='medium')
            .run(overwrite_output=True)
        )
        print(f"Video converted to web-friendly format: {output_path}")
    except ffmpeg.Error as e:
        print(f"FFmpeg error: {e.stderr.decode('utf-8')}")
        raise

@app.route("/")
def index():
    return render_template("index.html")

@app.route('/get_timelapse', methods=['POST'])
def get_timelapse():
    global progress

    # Reset cancellation event
    cancel_event.clear()

    # Get start and end dates from the frontend
    data = request.get_json()
    start_date = data.get('start_date')
    end_date = data.get('end_date')

    # Validate dates
    if not start_date or not end_date:
        return jsonify({"error": "Both start and end dates are required."}), 400

    # Start the timelapse process in a separate thread
    progress["status"] = "downloading"
    thread = threading.Thread(target=create_timelapse_process, args=(start_date, end_date))
    thread.start()

    return jsonify({"message": "Timelapse creation started."})

@app.route('/progress', methods=['GET'])
def get_progress():
    global progress
    return jsonify(progress)

@app.route('/cancel', methods=['POST'])
def cancel():
    cancel_event.set()  # Set the cancellation event
    return jsonify({"message": "Cancellation requested."})

if __name__ == "__main__":
    app.run(debug=True)