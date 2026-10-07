document.addEventListener('DOMContentLoaded', () => {
    const form = document.getElementById('clone-form');
    const urlInput = document.getElementById('url-input');
    const depthInput = document.getElementById('depth-input');
    const depthVal = document.getElementById('depth-val');
    const maxPagesInput = document.getElementById('max-pages-input');
    const renderInput = document.getElementById('render-input');
    const robotsInput = document.getElementById('robots-input');
    const submitBtn = document.getElementById('submit-btn');
    const btnText = document.querySelector('.btn-text');
    const spinner = document.querySelector('.spinner');

    const statusMessage = document.getElementById('status-message');
    const statusText = document.querySelector('.status-text');
    const downloadSection = document.getElementById('download-section');
    const downloadBtn = document.getElementById('download-btn');
    const singleBtn = document.getElementById('single-btn');
    const previewSection = document.getElementById('preview-section');
    const previewFrame = document.getElementById('preview-frame');

    const progressSection = document.getElementById('progress-section');
    const progressPhase = document.getElementById('progress-phase');
    const progressFill = document.getElementById('progress-fill');
    const progressStats = document.getElementById('progress-stats');
    const progressCurrent = document.getElementById('progress-current');

    const scoreSection = document.getElementById('score-section');
    const scoreBadge = document.getElementById('score-badge');
    const missingNote = document.getElementById('missing-note');

    let pollTimer = null;

    depthInput.addEventListener('input', (e) => {
        depthVal.textContent = e.target.value;
    });

    const fmtBytes = (n) => {
        if (!n) return '0 B';
        const units = ['B', 'KB', 'MB', 'GB'];
        let i = 0;
        let v = n;
        while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
        return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
    };

    const PHASE_LABELS = {
        queued: 'Queued…',
        starting: 'Starting…',
        fetching: 'Fetching page…',
        crawling: 'Crawling…',
        packaging: 'Packaging ZIP…',
        done: 'Done!',
    };

    function setLoading(on) {
        submitBtn.disabled = on;
        btnText.classList.toggle('hidden', on);
        spinner.classList.toggle('hidden', !on);
    }

    function showError(message) {
        statusMessage.classList.remove('hidden');
        statusText.className = 'status-text error';
        statusText.textContent = message;
    }

    function updateProgress(job) {
        const phase = job.phase || 'crawling';
        progressPhase.textContent = PHASE_LABELS[phase] || phase;

        const known = (job.pages_done || 0) + (job.pages_queued || 0);
        let pct = 5;
        if (phase === 'done') pct = 100;
        else if (phase === 'packaging') pct = 95;
        else if (known > 0) pct = Math.min(93, Math.max(5, Math.round((job.pages_done / known) * 90)));
        progressFill.style.width = pct + '%';

        const parts = [
            `Pages: ${job.pages_done || 0}` + (job.pages_queued ? ` (+${job.pages_queued} queued)` : ''),
            `Assets: ${job.assets_done || 0}` + (job.assets_failed ? ` (${job.assets_failed} failed)` : ''),
            fmtBytes(job.bytes || 0),
        ];
        if (job.missing) parts.push(`${job.missing} missing`);
        progressStats.textContent = parts.join('  ·  ');
        progressCurrent.textContent = job.current || '';
    }

    function showResults(job) {
        statusMessage.classList.remove('hidden');
        statusText.className = 'status-text success';
        statusText.textContent = 'Website successfully cloned!';

        if (typeof job.score === 'number') {
            scoreSection.classList.remove('hidden');
            scoreBadge.textContent = `${job.score}% offline-ready`;
            scoreBadge.className = 'score-badge ' +
                (job.score >= 90 ? 'score-good' : job.score >= 70 ? 'score-warn' : 'score-bad');

            const notes = [];
            if (job.missing) notes.push(`${job.missing} asset(s) could not be downloaded`);
            if (job.assets) notes.push(`${job.assets} assets saved`);
            if (job.pages) notes.push(`${job.pages} page(s)`);
            if (job.warnings && job.warnings.length) notes.push(job.warnings.join(' · '));
            missingNote.textContent = notes.join(' — ');
        }

        if (job.download_url) downloadBtn.href = job.download_url;
        if (job.single_url) {
            singleBtn.href = job.single_url;
            singleBtn.classList.remove('hidden');
        }
        downloadSection.classList.remove('hidden');

        if (job.preview_url) {
            previewFrame.src = job.preview_url;
            previewSection.classList.remove('hidden');
        }
    }

    async function pollJob(jobId) {
        try {
            const res = await fetch(`/api/jobs/${jobId}`);
            const job = await res.json();
            if (!res.ok) throw new Error(job.error || 'Clone job disappeared');

            if (job.status === 'error') {
                progressFill.style.width = '100%';
                progressFill.classList.add('bar-error');
                progressSection.classList.add('hidden');
                showError(job.error || 'Clone failed.');
                return;
            }

            updateProgress(job);

            if (job.status === 'done') {
                progressSection.classList.add('hidden');
                showResults(job);
                return;
            }
            pollTimer = setTimeout(() => pollJob(jobId), 600);
        } catch (err) {
            progressSection.classList.add('hidden');
            showError(err.message);
        } finally {
            if (!pollTimer || jobStatusDone()) setLoading(false);
        }
    }

    function jobStatusDone() { return pollTimer === null; }

    form.addEventListener('submit', async (e) => {
        e.preventDefault();

        const url = urlInput.value.trim();
        if (!url) return;

        // Reset UI
        if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
        statusMessage.classList.add('hidden');
        downloadSection.classList.add('hidden');
        previewSection.classList.add('hidden');
        scoreSection.classList.add('hidden');
        previewFrame.src = '';
        statusText.className = 'status-text';

        progressSection.classList.remove('hidden');
        progressFill.style.width = '5%';
        progressPhase.textContent = 'Starting…';
        progressStats.textContent = '';
        progressCurrent.textContent = '';

        setLoading(true);

        try {
            const response = await fetch('/api/clone', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    url,
                    depth: parseInt(depthInput.value, 10),
                    max_pages: parseInt(maxPagesInput.value, 10) || 25,
                    render: renderInput.checked,
                    respect_robots: robotsInput.checked,
                }),
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Failed to start the clone.');
            if (!data.job_id) throw new Error('No job id returned.');
            pollTimer = 1;
            pollJob(data.job_id);
        } catch (error) {
            progressSection.classList.add('hidden');
            setLoading(false);
            showError(error.message);
        }
    });
});
