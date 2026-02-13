#!/usr/bin/env python3
"""
Flask v2 backend for RedNote Video Downloader with ngrok support
- Run this to get a public URL accessible from any device
"""
from flask import Flask, request, jsonify, send_file, abort
from flask_cors import CORS
import yt_dlp
from pathlib import Path
import threading
import uuid
import os
import time
import shutil

app = Flask(__name__, static_folder='.')
CORS(app)

DOWNLOAD_FOLDER = Path("downloads_v2")
DOWNLOAD_FOLDER.mkdir(exist_ok=True)

# task_id -> status dict
_tasks = {}


def _update_progress(task_id, d):
    t = _tasks.get(task_id)
    if not t:
        return
    if d.get('status') == 'downloading':
        downloaded = d.get('downloaded_bytes') or 0
        total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
        if total:
            pct = int(min(100, max(0, (downloaded / total) * 100)))
            t['progress'] = pct
        else:
            t['progress'] = int(t.get('progress', 0))


def _download_background(task_id, url, ydl_opts):
    t = _tasks[task_id]
    try:
        t['status'] = 'downloading'
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)

        t.update({
            'status': 'completed',
            'progress': 100,
            'filename': os.path.basename(filename),
            'filepath': filename,
            'title': info.get('title') if isinstance(info, dict) else None,
        })
    except Exception as exc:
        app.logger.exception("Background download failed for %s", url)
        t.update({
            'status': 'failed',
            'error': 'Download failed on server — try again or use the local yt-dlp command'
        })


@app.route('/api/v2/ping', methods=['GET'])
def ping():
    return jsonify({'ok': True})


@app.route('/api/v2/download', methods=['POST'])
def api_start_download():
    data = request.get_json() or {}
    url = data.get('url')
    mode = data.get('mode')
    if not url:
        return jsonify({'error': 'url is required'}), 400

    if 'xiaohongshu.com' not in url and 'xhslink.com' not in url:
        return jsonify({'error': 'invalid rednote url'}), 400

    def _extract_info_with_timeout(url, opts, timeout=8):
        container = {}
        def _worker():
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    container['info'] = ydl.extract_info(url, download=False)
            except Exception as e:
                container['error'] = e
        th = threading.Thread(target=_worker, daemon=True)
        th.start()
        th.join(timeout)
        if th.is_alive():
            return None, 'timeout'
        if 'error' in container:
            return None, container['error']
        return container.get('info'), None

    def _choose_direct_url(info):
        if not isinstance(info, dict):
            return None
        if info.get('is_live'):
            return None
        formats = info.get('requested_formats') or info.get('formats') or []
        if formats:
            f = max(formats, key=lambda x: (x.get('height') or 0, x.get('tbr') or 0, x.get('filesize') or 0))
            return f.get('url')
        return info.get('url')

    if mode == 'direct':
        info, err = _extract_info_with_timeout(url, {'quiet': True})
        if err == 'timeout':
            return jsonify({'error': 'preparation timed out'}), 504
        if err:
            app.logger.exception('Direct-mode extraction failed for %s', url)
            return jsonify({'error': 'could not prepare browser download — try again or use the local command'}), 502
        direct = _choose_direct_url(info)
        if direct:
            return jsonify({'direct_url': direct})
        return jsonify({'error': 'no direct downloadable stream found'}), 422

    task_id = str(uuid.uuid4())
    _tasks[task_id] = {'status': 'queued', 'progress': 0, 'url': url, 'created': time.time()}

    out_template = str((DOWNLOAD_FOLDER / f"{task_id}_%(title)s.%(ext)s"))

    ydl_opts = {
        'format': 'best',
        'outtmpl': out_template,
        'quiet': True,
        'no_warnings': True,
        'progress_hooks': [lambda d: _update_progress(task_id, d)],
    }

    thread = threading.Thread(target=_download_background, args=(task_id, url, ydl_opts), daemon=True)
    thread.start()

    return jsonify({'task_id': task_id})


@app.route('/api/v2/status/<task_id>', methods=['GET'])
def api_status(task_id):
    t = _tasks.get(task_id)
    if not t:
        return jsonify({'error': 'not found'}), 404
    resp = {
        'status': t.get('status'),
        'progress': t.get('progress', 0),
        'filename': t.get('filename'),
        'title': t.get('title'),
    }
    if t.get('status') == 'failed':
        resp['error'] = 'Download failed on server — check server logs or try the local command'
    return jsonify(resp)


@app.route('/api/v2/download-file/<task_id>', methods=['GET'])
def api_download_file(task_id):
    t = _tasks.get(task_id)
    if not t:
        return jsonify({'error': 'not found'}), 404
    if t.get('status') != 'completed':
        return jsonify({'error': 'not completed'}), 400
    filepath = t.get('filepath')
    if not filepath or not os.path.exists(filepath):
        return jsonify({'error': 'file missing'}), 404

    return send_file(filepath, as_attachment=True, download_name=t.get('filename'))


@app.route('/api/v2/cleanup', methods=['POST'])
def api_cleanup():
    now = time.time()
    removed = 0
    for f in DOWNLOAD_FOLDER.glob('*'):
        try:
            if f.is_file() and (now - f.stat().st_mtime) > 2 * 3600:
                f.unlink()
                removed += 1
        except Exception:
            pass
    return jsonify({'removed': removed})


@app.route('/')
def index():
    index_path = Path('./rednote_frontend_v2.html')
    if index_path.exists():
        return index_path.read_text(encoding='utf-8')
    return "RedNote Downloader v2 - frontend not found", 200


# filepath: /home/itsmoon/Documents/nnn/web_downloader_v2_ngrok.py
# ...existing code...
if __name__ == '__main__':
    # Start ngrok tunnel
    try:
        from pyngrok import ngrok
        from pyngrok.exception import PyngrokNgrokInstallError

        # Prefer a locally-installed ngrok binary if available
        ngrok_bin = shutil.which("ngrok")
        if ngrok_bin:
            try:
                ngrok.set_ngrok_path(ngrok_bin)
            except Exception:
                pass

        # Use auth token from environment if provided (avoid hardcoding tokens)
        token = os.environ.get("NGROK_AUTHTOKEN")
        if token:
            try:
                ngrok.set_auth_token(token)
            except Exception as e:
                app.logger.warning("Could not set ngrok auth token: %s", e)

        try:
            public_url = ngrok.connect(5001)
            print('\n' + '='*60)
            print('🌐 PUBLIC URL (share this with any device):')
            print(f'   {public_url}')
            print('='*60 + '\n')
            app.run(host='0.0.0.0', port=5001, debug=False)
        except PyngrokNgrokInstallError as e:
            print("⚠️  ngrok install failed (network). Install ngrok manually or ensure network access.")
            print("Starting without ngrok on local network only...")
            app.run(host='0.0.0.0', port=5001, debug=True)
        except Exception as e:
            app.logger.warning("Failed to start ngrok tunnel: %s", e)
            print("Starting without ngrok on local network only...")
            app.run(host='0.0.0.0', port=5001, debug=True)

    except ImportError:
        print("⚠️  pyngrok not installed. Install it with: pip install pyngrok")
        print("Starting without ngrok on local network only...")
        app.run(host='0.0.0.0', port=5001, debug=True)
