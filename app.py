import os
import tempfile
import threading
import time

import requests as req_lib
from flask import (Flask, Response, jsonify, render_template, request, send_file,
                   send_from_directory)

from cloner import build_single_html
from jobs import jobs as job_manager

app = Flask(__name__)

# preview_id -> {dir, origin}
active_previews = {}

# ---- simple per-IP rate limit for clone jobs (20/minute) ------------------
RATE_WINDOW = 60
RATE_LIMIT = 20
_rate_lock = threading.Lock()
_rate_hits = {}


def rate_limited(client_ip):
    now = time.time()
    with _rate_lock:
        hits = [t for t in _rate_hits.get(client_ip, []) if now - t < RATE_WINDOW]
        if len(hits) >= RATE_LIMIT:
            _rate_hits[client_ip] = hits
            return True
        hits.append(now)
        _rate_hits[client_ip] = hits
        if len(_rate_hits) > 5000:  # keep the table small
            _rate_hits.clear()
        return False


def client_ip():
    forwarded = request.headers.get('X-Forwarded-For', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.remote_addr or 'unknown'


def public_job(job):
    """Strip internal fields (filesystem paths) before returning JSON."""
    job = dict(job)
    job.pop('dir', None)
    job.pop('zip', None)
    job.pop('params', None)
    return job


def ensure_preview(job):
    if not job.get('dir'):
        return None
    preview_id = os.path.basename(job['dir'])
    active_previews.setdefault(preview_id, {
        'dir': job['dir'],
        'origin': job['url'].rstrip('/'),
    })
    return preview_id


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/clone', methods=['POST'])
def clone_api():
    data = request.json or {}
    url = data.get('url')
    if not url:
        return jsonify({'error': 'URL is required'}), 400
    if not url.startswith('http://') and not url.startswith('https://'):
        url = 'https://' + url

    if rate_limited(client_ip()):
        return jsonify({'error': 'Rate limit reached — try again in a minute.'}), 429

    try:
        depth = max(1, min(10, int(data.get('depth', 1) or 1)))
        max_pages = max(1, min(500, int(data.get('max_pages', 60) or 60)))
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid depth or max_pages'}), 400

    params = {
        'max_depth': depth,
        'max_pages': max_pages,
        'respect_robots': bool(data.get('respect_robots', True)),
        'render': True if data.get('render') else 'auto',
    }

    job_id = job_manager.create(url, params)

    if data.get('wait'):  # synchronous mode for scripts/tests
        job = job_manager.wait(job_id, timeout=int(data.get('wait_timeout', 600)))
        if job and job['status'] == 'done':
            ensure_preview(job)
        if job and job['status'] == 'error':
            return jsonify({'error': job['error']}), 500
        if not job or job['status'] != 'done':
            return jsonify({'error': 'Clone did not finish in time'}), 504
        return jsonify(public_job(job))

    return jsonify({'job_id': job_id, 'status': 'queued'}), 202


@app.route('/api/jobs/<job_id>')
def job_status(job_id):
    job = job_manager.get(job_id)
    if not job:
        return jsonify({'error': 'Unknown job'}), 404
    if job['status'] == 'done':
        ensure_preview(job)
    return jsonify(public_job(job))


@app.route('/download')
def download():
    filename = request.args.get('file', '')
    filename = os.path.basename(filename)
    if not filename.startswith('cloned_') or not filename.endswith('.zip'):
        return 'Invalid file', 400

    filepath = os.path.join(tempfile.gettempdir(), filename)
    if not os.path.isfile(filepath):
        return 'File not found', 404

    return send_file(
        filepath,
        as_attachment=True,
        download_name='cloned_site.zip',
        mimetype='application/zip',
    )


@app.route('/download-single')
def download_single():
    preview_id = os.path.basename(request.args.get('id', ''))
    info = active_previews.get(preview_id)
    if not info:
        return 'Preview expired or not found', 404
    try:
        path = build_single_html(info['dir'])
    except Exception as exc:
        return f'Could not build single-file HTML: {exc}', 500
    return send_file(
        path,
        as_attachment=True,
        download_name='cloned_site_single.html',
        mimetype='text/html',
    )


@app.route('/preview/<preview_id>/<path:filename>')
def preview(preview_id, filename):
    info = active_previews.get(preview_id)
    if not info:
        return 'Preview expired or not found', 404
    if '..' in filename:
        return 'Invalid path', 400

    folder_path = info['dir']
    local_file = os.path.join(folder_path, filename)
    if os.path.isfile(local_file):
        return send_from_directory(folder_path, filename)

    # Strict offline preview by default: show exactly what the ZIP contains.
    # ?proxy=1 falls back to fetching the missing asset from the live site.
    if request.args.get('proxy') == '1':
        try:
            resp = req_lib.get(
                f"{info['origin']}/{filename}",
                headers={'User-Agent': 'Mozilla/5.0'},
                timeout=10,
            )
            content_type = resp.headers.get('Content-Type', 'application/octet-stream')
            return Response(resp.content, status=resp.status_code,
                            content_type=content_type)
        except Exception:
            return 'Asset not found', 404

    return 'Asset not in this clone', 404


if __name__ == '__main__':
    port = int(os.environ.get('CLONER_PORT') or os.environ.get('PORT') or 5000)
    debug = os.environ.get('CLONER_DEBUG', '1') == '1'
    use_reloader = os.environ.get('CLONER_RELOAD', '1') == '1'
    app.run(debug=debug, port=port, use_reloader=use_reloader, threaded=True)
