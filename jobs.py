"""Background clone jobs with live progress reporting."""
import os
import threading
import time
import uuid

from cloner import clone_website

TRACKED_KEYS = ('phase', 'current', 'pages_done', 'pages_failed', 'pages_started',
                'pages_queued', 'assets_done', 'assets_failed', 'bytes', 'missing')


class JobManager:
    def __init__(self):
        self._jobs = {}
        self._lock = threading.Lock()

    def create(self, url, params):
        job_id = uuid.uuid4().hex[:12]
        job = {
            'id': job_id,
            'url': url,
            'params': params,
            'status': 'queued',
            'phase': 'queued',
            'created': time.time(),
            'current': url,
            'pages_done': 0,
            'pages_failed': 0,
            'pages_started': 1,
            'pages_queued': 1,
            'assets_done': 0,
            'assets_failed': 0,
            'bytes': 0,
            'missing': 0,
            'missing_list': [],
            'score': None,
            'warnings': [],
            'error': None,
            'download_url': None,
            'preview_url': None,
            'single_url': None,
            'dir': None,
            'zip': None,
        }
        with self._lock:
            self._jobs[job_id] = job
        thread = threading.Thread(target=self._run, args=(job,), daemon=True,
                                  name=f'cloner-job-{job_id}')
        thread.start()
        return job_id

    def _run(self, job):
        def progress(snap):
            with self._lock:
                for key in TRACKED_KEYS:
                    if key in snap:
                        job[key] = snap[key]

        with self._lock:
            job['status'] = 'running'
            job['phase'] = 'starting'
        try:
            result = clone_website(job['url'], progress_cb=progress, **job['params'])
            preview_id = os.path.basename(result['dir'])
            with self._lock:
                job.update(
                    status='done',
                    phase='done',
                    dir=result['dir'],
                    zip=result['zip'],
                    score=result['score'],
                    pages=result['pages'],
                    pages_failed=result['pages_failed'],
                    assets=result['assets'],
                    assets_failed=result['assets_failed'],
                    bytes=result['bytes'],
                    missing=len(result['missing']),
                    missing_list=result['missing'][:25],
                    warnings=result['warnings'],
                    download_url=f"/download?file={os.path.basename(result['zip'])}",
                    preview_url=f"/preview/{preview_id}/index.html",
                    single_url=f"/download-single?id={preview_id}",
                )
        except Exception as exc:
            with self._lock:
                job['status'] = 'error'
                job['phase'] = 'error'
                job['error'] = str(exc)

    def get(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def wait(self, job_id, timeout=600):
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.get(job_id)
            if job and job['status'] in ('done', 'error'):
                return job
            time.sleep(0.25)
        return self.get(job_id)


jobs = JobManager()
