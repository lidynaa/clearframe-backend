from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import os
import uuid
import subprocess
import tempfile
import threading
import time

app = Flask(__name__)
CORS(app)

UPLOAD_FOLDER = "/tmp/videoclean_uploads"
OUTPUT_FOLDER = "/tmp/videoclean_outputs"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# Track job status
jobs = {}

ALLOWED_EXTENSIONS = {'mp4', 'mov', 'avi', 'mkv', 'webm', 'flv'}
MAX_FILE_SIZE = 500 * 1024 * 1024  # 500MB

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def process_video(job_id, input_path, output_path, region, method):
    """
    Remove text/captions from video using FFmpeg.
    Methods:
    - blur: blur the subtitle region
    - inpaint: use delogo filter (works well for text overlays)
    - crop_blur: combination approach
    """
    try:
        jobs[job_id]['status'] = 'processing'
        jobs[job_id]['progress'] = 10
        jobs[job_id]['message'] = 'Analyzing your video…'

        # Get video info
        probe_cmd = [
            'ffprobe', '-v', 'quiet', '-print_format', 'json',
            '-show_streams', input_path
        ]
        result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=30)
        
        jobs[job_id]['progress'] = 20
        jobs[job_id]['message'] = 'Building filter…'

        # Build FFmpeg filter based on region and method
        # region: 'bottom', 'top', 'full', 'custom'
        # We use the video stream to determine height/width dynamically

        if region == 'bottom':
            # Bottom 20% of video - most common subtitle location
            if method == 'blur':
                vf = "split[original][copy];[copy]crop=iw:ih*0.20:0:ih*0.80,boxblur=20:5[blurred];[original][blurred]overlay=0:H*0.80"
            else:  # delogo/inpaint style
                vf = "delogo=x=0:y=ih*0.80:w=iw:h=ih*0.20:show=0"
        elif region == 'top':
            if method == 'blur':
                vf = "split[original][copy];[copy]crop=iw:ih*0.15:0:0,boxblur=20:5[blurred];[original][blurred]overlay=0:0"
            else:
                vf = "delogo=x=0:y=0:w=iw:h=ih*0.15:show=0"
        elif region == 'both':
            if method == 'blur':
                vf = ("split=3[a][b][c];"
                      "[b]crop=iw:ih*0.15:0:0,boxblur=20:5[top];"
                      "[c]crop=iw:ih*0.20:0:ih*0.80,boxblur=20:5[bottom];"
                      "[a][top]overlay=0:0[mid];"
                      "[mid][bottom]overlay=0:H*0.80")
            else:
                vf = "delogo=x=0:y=0:w=iw:h=ih*0.15:show=0,delogo=x=0:y=ih*0.80:w=iw:h=ih*0.20:show=0"
        else:  # full - aggressive removal
            if method == 'blur':
                vf = "boxblur=10:3"
            else:
                vf = "delogo=x=0:y=0:w=iw:h=ih:show=0"

        jobs[job_id]['progress'] = 30
        jobs[job_id]['message'] = 'Removing text from every frame — hang tight…'

        # FFmpeg command - high quality output
        cmd = [
            'ffmpeg', '-y',
            '-i', input_path,
            '-vf', vf,
            '-c:v', 'libx264',
            '-crf', '18',          # High quality (0=lossless, 23=default, 18=visually lossless)
            '-preset', 'ultrafast',   # Much faster — minimal quality difference
            '-c:a', 'copy',        # Copy audio without re-encoding
            '-movflags', '+faststart',  # Web-optimized
            output_path
        ]

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )

        # Monitor progress
        jobs[job_id]['progress'] = 40
        jobs[job_id]['message'] = 'Processing frames — almost there…'
        stdout, stderr = process.communicate(timeout=300)
        
        if process.returncode != 0:
            raise Exception(f"FFmpeg error: {stderr.decode()}")

        jobs[job_id]['status'] = 'done'
        jobs[job_id]['progress'] = 100
        jobs[job_id]['message'] = 'All done! Your video is ready.'
        jobs[job_id]['output'] = output_path

    except subprocess.TimeoutExpired:
        process.kill()
        jobs[job_id]['status'] = 'error'
        jobs[job_id]['error'] = 'Processing timeout. Please try a shorter video.'
    except Exception as e:
        jobs[job_id]['status'] = 'error'
        jobs[job_id]['error'] = str(e)
    finally:
        # Clean up input file
        try:
            os.remove(input_path)
        except:
            pass


@app.route('/api/upload', methods=['POST'])
def upload_video():
    if 'video' not in request.files:
        return jsonify({'error': 'No video file provided'}), 400
    
    file = request.files['video']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    
    if not allowed_file(file.filename):
        return jsonify({'error': 'Invalid file type. Supported: MP4, MOV, AVI, MKV, WEBM, FLV'}), 400

    # Get options
    region = request.form.get('region', 'bottom')
    method = request.form.get('method', 'delogo')

    # Generate unique job ID
    job_id = str(uuid.uuid4())
    
    # Save uploaded file
    ext = file.filename.rsplit('.', 1)[1].lower()
    input_path = os.path.join(UPLOAD_FOLDER, f"{job_id}_input.{ext}")
    output_path = os.path.join(OUTPUT_FOLDER, f"{job_id}_output.mp4")
    
    file.save(input_path)
    
    # Check file size
    if os.path.getsize(input_path) > MAX_FILE_SIZE:
        os.remove(input_path)
        return jsonify({'error': 'File too large. Maximum size is 500MB'}), 400

    # Initialize job
    jobs[job_id] = {
        'status': 'queued',
        'progress': 0,
        'output': None,
        'error': None,
        'created': time.time()
    }

    # Process in background thread
    thread = threading.Thread(
        target=process_video,
        args=(job_id, input_path, output_path, region, method)
    )
    thread.daemon = True
    thread.start()

    return jsonify({'job_id': job_id}), 202


@app.route('/api/status/<job_id>', methods=['GET'])
def get_status(job_id):
    if job_id not in jobs:
        return jsonify({'error': 'Job not found'}), 404
    
    job = jobs[job_id]
    return jsonify({
        'status': job['status'],
        'progress': job['progress'],
        'message': job.get('message', ''),
        'error': job.get('error')
    })


@app.route('/api/download/<job_id>', methods=['GET'])
def download_video(job_id):
    if job_id not in jobs:
        return jsonify({'error': 'Job not found'}), 404
    
    job = jobs[job_id]
    if job['status'] != 'done':
        return jsonify({'error': 'Job not complete'}), 400
    
    output_path = job['output']
    if not os.path.exists(output_path):
        return jsonify({'error': 'Output file not found'}), 404

    return send_file(
        output_path,
        as_attachment=True,
        download_name='cleaned_video.mp4',
        mimetype='video/mp4'
    )


@app.route('/api/health', methods=['GET'])
def health():
    # Check FFmpeg is available
    try:
        subprocess.run(['ffmpeg', '-version'], capture_output=True, timeout=5)
        ffmpeg_ok = True
    except:
        ffmpeg_ok = False
    
    return jsonify({'status': 'ok', 'ffmpeg': ffmpeg_ok})


# Cleanup old jobs every hour
def cleanup_old_jobs():
    while True:
        time.sleep(3600)
        now = time.time()
        to_delete = [jid for jid, j in list(jobs.items()) 
                     if now - j.get('created', 0) > 7200]
        for jid in to_delete:
            job = jobs.pop(jid, {})
            if job.get('output') and os.path.exists(job['output']):
                try:
                    os.remove(job['output'])
                except:
                    pass

cleanup_thread = threading.Thread(target=cleanup_old_jobs, daemon=True)
cleanup_thread.start()


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
