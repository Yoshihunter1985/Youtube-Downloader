import os
import sys
import uuid
import threading
import webbrowser
from flask import Flask, request, jsonify, send_file, render_template
import yt_dlp

# Template folder — works both locally and when frozen
if getattr(sys, 'frozen', False):
    template_folder = os.path.join(sys._MEIPASS, 'templates')
else:
    template_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates')

app = Flask(__name__, template_folder=template_folder)

# Download folder — use /tmp on Render (writable), otherwise ./downloads
if os.environ.get("RENDER"):
    DOWNLOAD_DIR = "/tmp/ytgrab_downloads"
else:
    DOWNLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Job tracking
progress_store = {}


def make_ydl_opts(download_type, output_format, output_path, progress_hook):
    base_opts = {
        "outtmpl": os.path.join(output_path, "%(title)s.%(ext)s"),
        "progress_hooks": [progress_hook],
        "quiet": True,
        "no_warnings": True,
        "noprogress": False,
    }

    if download_type == "audio":
        return {
            **base_opts,
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
        }
    elif download_type == "video":
        if output_format == "avi":
            return {
                **base_opts,
                "format": "bestvideo[ext=mp4]/bestvideo",
                "postprocessors": [{"key": "FFmpegVideoConvertor", "preferedformat": "avi"}],
            }
        else:
            return {
                **base_opts,
                "format": "bestvideo[ext=mp4]/bestvideo",
            }
    else:  # both
        if output_format == "avi":
            return {
                **base_opts,
                "format": "bestvideo+bestaudio/best",
                "postprocessors": [{"key": "FFmpegVideoConvertor", "preferedformat": "avi"}],
                "merge_output_format": "avi",
            }
        else:
            return {
                **base_opts,
                "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
                "merge_output_format": "mp4",
            }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/info", methods=["POST"])
def get_info():
    data = request.json or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            info = ydl.extract_info(url, download=False)
            return jsonify({
                "title": info.get("title", "Unknown"),
                "thumbnail": info.get("thumbnail", ""),
                "duration": info.get("duration", 0),
                "uploader": info.get("uploader", "Unknown"),
            })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/download", methods=["POST"])
def download():
    data = request.json or {}
    url = data.get("url", "").strip()
    download_type = data.get("type", "both")
    output_format = data.get("format", "mp4")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    job_id = str(uuid.uuid4())  # unique per request, no collision
    progress_store[job_id] = {"status": "starting", "percent": 0, "filename": None, "error": None}

    def progress_hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 1
            downloaded = d.get("downloaded_bytes", 0)
            pct = int((downloaded / total) * 100)
            progress_store[job_id]["percent"] = min(pct, 98)
            progress_store[job_id]["status"] = "downloading"
        elif d["status"] == "finished":
            progress_store[job_id]["status"] = "processing"
            progress_store[job_id]["percent"] = 99

    def run_download():
        try:
            opts = make_ydl_opts(download_type, output_format, DOWNLOAD_DIR, progress_hook)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)

                # Resolve the output filename
                if download_type == "audio":
                    ext = "mp3"
                else:
                    ext = output_format

                prepared = ydl.prepare_filename(info)
                base = os.path.splitext(prepared)[0]
                candidate = f"{base}.{ext}"

                # Fallback: newest file in downloads dir
                if not os.path.exists(candidate):
                    files = [os.path.join(DOWNLOAD_DIR, f) for f in os.listdir(DOWNLOAD_DIR)]
                    files = [f for f in files if os.path.isfile(f)]
                    if files:
                        candidate = max(files, key=os.path.getmtime)

                if not os.path.exists(candidate):
                    raise FileNotFoundError(f"Output file not found: {candidate}")

                progress_store[job_id]["filename"] = candidate
                progress_store[job_id]["status"] = "done"
                progress_store[job_id]["percent"] = 100

        except Exception as e:
            progress_store[job_id]["status"] = "error"
            progress_store[job_id]["error"] = str(e)

    threading.Thread(target=run_download, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/progress/<job_id>")
def get_progress(job_id):
    return jsonify(progress_store.get(job_id, {"status": "unknown", "error": "Job not found"}))


@app.route("/fetch/<job_id>")
def fetch_file(job_id):
    job = progress_store.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "Not ready"}), 400
    filepath = job.get("filename")
    if not filepath or not os.path.exists(filepath):
        return jsonify({"error": "File not found on server"}), 404
    return send_file(filepath, as_attachment=True)


def open_browser():
    import time
    time.sleep(1.5)
    webbrowser.open("http://127.0.0.1:5757")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5757))
    is_render = bool(os.environ.get("RENDER"))
    host = "0.0.0.0" if is_render else "127.0.0.1"
    if not is_render:
        threading.Thread(target=open_browser, daemon=True).start()
    app.run(host=host, port=port, debug=False)
