# MaxWeb Cloner 🖥️

**Clone any website — HTML, CSS, JS, images, fonts and all — from a web UI, a
sleek OpenCode-style terminal UI, or plain CLI.**

![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=flat-square&logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-3.0-000000?style=flat-square&logo=flask&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-green?style=flat-square)
![Tests](https://img.shields.io/badge/tests-48%20passing-brightgreen?style=flat-square)

## ✨ Features

- **🌐 Web UI** — paste a URL, watch live progress, get an offline-readiness score, preview the clone in strict offline mode, download a ZIP or a single-file HTML.
- **🖥️ MaxWeb Cloner TUI** — a full-screen OpenCode-style terminal interface with a boxed input field (`maxweb`).
- **⚙️ CLI** — scriptable: `cloner clone <url> --depth 3 --single`
- **Parallel crawling** — 4 page workers + 12 asset workers with page/asset budgets.
- **Complete asset capture** — CSS `url()`/`@import` (fonts, backgrounds), every `srcset` candidate, `<picture>`, video posters, favicons/manifests.
- **JS rendering** — Playwright *or* Selenium + system Chrome for React/Next/Vue SPAs (auto-detected).
- **Offline score** — every clone reports how much works without the internet.
- **Single-file export** — one self-contained `.html` with everything inlined as data URIs.
- **Polite by default** — `robots.txt` support, retries with backoff, max-pages caps.
- **Async jobs API** — `POST /api/clone` → `GET /api/jobs/<id>` with live progress.

## 🚀 Quick start

```bash
git clone https://github.com/Codewithmax/maxweb-cloner.git
cd maxweb-cloner
python3 -m venv venv
venv/bin/pip install -r requirements.txt

venv/bin/python app.py          # web UI  → http://localhost:5000
venv/bin/python maxweb.py       # TUI     → MaxWeb Cloner
venv/bin/python cli.py https://example.com --single   # CLI
```

## 📦 Terminal commands

| Command | What it does |
|---|---|
| `cloner` | Start the web UI and open it in your browser |
| `maxweb` | Open the MaxWeb Cloner full-screen TUI |
| `cloner clone <url> -d 3 --single` | Clone from the terminal, no server needed |
| `cloner test` | Run the full automated test suites |
| `cloner stop / restart / status / logs` | Manage the background server |

## 🧪 Tests

```bash
venv/bin/python test_cloner.py   # 32 web/API tests
venv/bin/python test_tui.py      # 16 TUI/CLI tests (drives the TUI through a pty)
```

## 🔌 API

```bash
# start a clone job (async)
curl -X POST http://localhost:5000/api/clone \
     -H 'Content-Type: application/json' \
     -d '{"url":"https://example.com","depth":1,"max_pages":25}'

# poll progress
curl http://localhost:5000/api/jobs/<job_id>
```

Job response includes `status`, `phase`, `pages_done`, `assets_done`, `bytes`,
`score` (offline %), `download_url`, `preview_url` and `single_url`.

## 📂 Project layout

| File | Purpose |
|---|---|
| `cloner.py` | Crawling engine (parallel pages, deep asset capture, scoring) |
| `app.py` | Flask web app + JSON API |
| `jobs.py` | Background job manager with live progress |
| `renderer.py` | JS rendering backends (Playwright / Selenium) |
| `maxweb.py` | MaxWeb Cloner terminal UI |
| `cli.py` | Command-line cloning |
| `test_cloner.py`, `test_tui.py` | Automated tests |

## ⚠️ Use responsibly

Only clone sites you have the right to copy. The crawler respects `robots.txt`
by default — keep it on. Clones are for personal study, archiving and
development; don't republish other people's content.

## License

MIT
