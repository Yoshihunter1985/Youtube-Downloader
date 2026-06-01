import os
import sys
import json
import threading
import subprocess
import webbrowser
from flask import Flask, request, jsonify, send_file, render_template
import yt_dlp

# When frozen by PyInstaller, templates live in sys._MEIPASS/templates
if getattr(sys, 'frozen', False):
    template_folder = os.path.join(sys._MEIPASS, 'templates')
else:
    template_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates')

app = Flask(__name__, template_folder=template_folder)

# Determine download folder — next to the exe when packaged, otherwise ./downloads
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Track progress per request
progress_store = {}


def get_ffmpeg_path():
    """Return bundled ffmpeg path when frozen, else rely on system ffmpeg."""
    if getattr(sys, 'frozen', False):
        ffmpeg = os.path.join(sys._MEIPASS, "ffmpeg")
        if sys.platform == "win32":
            ffmpeg += ".exe"
        return ffmpeg
    return "ffmpeg"


def make_ydl_opts(download_type, output_format, output_path, progress_hook):
    ffmpeg_loc = os.path.dirname(get_ffmpeg_path()) if getattr(sys, 'frozen', False) else None
    base_opts = {
        "outtmpl": os.path.join(output_path, "%(title)s.%(ext)s"),
        "progress_hooks": [progress_hook],
        "quiet": True,
        "no_warnings": True,
    }
    if ffmpeg_loc:
        base_opts["ffmpeg_location"] = ffmpeg_loc

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
        # Video only, no audio
        if output_format == "avi":
            return {
                **base_opts,
                "format": "bestvideo[ext=mp4]/bestvideo",
                "postprocessors": [{
                    "key": "FFmpegVideoConvertor",
                    "preferedformat": "avi",
                }],
            }
        else:
            return {
                **base_opts,
                "format": "bestvideo[ext=mp4]/bestvideo",
            }
    else:  # "both" — audio + video merged
        if output_format == "avi":
            return {
                **base_opts,
                "format": "bestvideo+bestaudio/best",
                "postprocessors": [{
                    "key": "FFmpegVideoConvertor",
                    "preferedformat": "avi",
                }],
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
    data = request.json
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
    data = request.json
    url = data.get("url", "").strip()
    download_type = data.get("type", "both")   # audio | video | both
    output_format = data.get("format", "mp4")  # mp4 | avi

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    job_id = str(threading.get_ident())
    progress_store[job_id] = {"status": "starting", "percent": 0, "filename": None, "error": None}

    def progress_hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 1)
            downloaded = d.get("downloaded_bytes", 0)
            pct = int((downloaded / total) * 100) if total else 0
            progress_store[job_id]["percent"] = pct
            progress_store[job_id]["status"] = "downloading"
        elif d["status"] == "finished":
            progress_store[job_id]["status"] = "processing"
            progress_store[job_id]["percent"] = 99
            progress_store[job_id]["filename"] = d.get("filename", "")

    def run_download():
        try:
            opts = make_ydl_opts(download_type, output_format, DOWNLOAD_DIR, progress_hook)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                # Find the actual output file
                title = info.get("title", "video")
                # Determine expected extension
                if download_type == "audio":
                    ext = "mp3"
                elif download_type == "video":
                    ext = output_format
                else:
                    ext = output_format

                # Try to resolve actual filename
                safe_title = ydl.prepare_filename(info)
                base = os.path.splitext(safe_title)[0]
                candidate = f"{base}.{ext}"
                if not os.path.exists(candidate):
                    # Fallback: scan downloads dir for newest file
                    files = sorted(
                        [os.path.join(DOWNLOAD_DIR, f) for f in os.listdir(DOWNLOAD_DIR)],
                        key=os.path.getmtime, reverse=True
                    )
                    candidate = files[0] if files else candidate

                progress_store[job_id]["filename"] = candidate
                progress_store[job_id]["status"] = "done"
                progress_store[job_id]["percent"] = 100
        except Exception as e:
            progress_store[job_id]["status"] = "error"
            progress_store[job_id]["error"] = str(e)

    t = threading.Thread(target=run_download, daemon=True)
    t.start()
    return jsonify({"job_id": job_id})


@app.route("/progress/<job_id>")
def progress(job_id):
    return jsonify(progress_store.get(job_id, {"status": "unknown"}))


@app.route("/fetch/<job_id>")
def fetch_file(job_id):
    job = progress_store.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "Not ready"}), 400
    filepath = job.get("filename")
    if not filepath or not os.path.exists(filepath):
        return jsonify({"error": "File not found"}), 404
    return send_file(filepath, as_attachment=True)


def open_browser():
    import time
    time.sleep(1.2)
    webbrowser.open("http://127.0.0.1:5757")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5757))
    host = "0.0.0.0" if os.environ.get("RENDER") else "127.0.0.1"
    # Only open browser when running locally
    if not os.environ.get("RENDER"):
        threading.Thread(target=open_browser, daemon=True).start()
    app.run(host=host, port=port, debug=False)
