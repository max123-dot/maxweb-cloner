"""End-to-end test suite for the Website Cloner.

Run with:  cloner test          (starts the server first)
      or:  venv/bin/python test_cloner.py
"""
import io
import os
import subprocess
import sys
import tempfile
import time
import zipfile

import requests

BASE = "http://localhost:5000"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TARGET = "https://www.python.org"
JOB_TIMEOUT = 300

_results = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    _results.append((name, bool(condition)))
    line = f"  [{status}] {name}"
    if detail:
        line += f" ({detail})" if condition else f" -> {detail}"
    print(line, flush=True)
    return bool(condition)


def skip(name, reason):
    print(f"  [SKIP] {name} ({reason})", flush=True)


def server_up():
    try:
        return requests.get(BASE, timeout=3).status_code == 200
    except requests.RequestException:
        return False


# ---------------------------------------------------------------- job API
def start_job(url, **params):
    body = {"url": url}
    body.update(params)
    return requests.post(f"{BASE}/api/clone", json=body, timeout=30)


def wait_job(job_id, timeout=JOB_TIMEOUT):
    deadline = time.time() + timeout
    snap = None
    while time.time() < deadline:
        r = requests.get(f"{BASE}/api/jobs/{job_id}", timeout=30)
        if r.status_code != 200:
            return r.status_code, (r.json() if r.headers.get(
                "content-type", "").startswith("application/json") else {})
        snap = r.json()
        if snap.get("status") in ("done", "error"):
            return 200, snap
        time.sleep(0.3)
    return 200, snap or {}


def clone(url, **params):
    """Start a clone job and wait for it. Returns (first_snap, final_job)."""
    r = start_job(url, **params)
    if r.status_code != 202:
        return {}, {"status": "error",
                    "error": f"start failed with HTTP {r.status_code}"}
    first = requests.get(f"{BASE}/api/jobs/{r.json()['job_id']}", timeout=30).json()
    _code, job = wait_job(r.json()["job_id"])
    return first, job


def zip_entries(download_url):
    resp = requests.get(BASE + download_url, timeout=90)
    if resp.status_code != 200 or not resp.content:
        return None, resp
    try:
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        return zf.namelist(), resp
    except zipfile.BadZipFile:
        return None, resp


# ----------------------------------------------------------------- tests
def test_ui():
    resp = requests.get(BASE, timeout=10)
    ok = resp.status_code == 200 and "Cloner" in resp.text
    check("UI serves the homepage", ok, f"status={resp.status_code}")
    check("UI has progress + score elements",
          'id="progress-section"' in resp.text and 'id="score-badge"' in resp.text)


def test_async_clone_simple():
    first, job = clone("https://example.com", depth=1, max_pages=5,
                       respect_robots=False)
    check("Clone starts as an async job (202 + job_id)",
          first.get("status") in ("queued", "running") and "phase" in first,
          f"status={first.get('status')} phase={first.get('phase')}")
    check("Job reports live progress fields",
          all(k in first for k in ("pages_done", "assets_done", "bytes", "pages_queued")),
          str({k: first.get(k) for k in ("pages_done", "assets_done", "pages_queued")}))
    check("Simple site clone completes (example.com)",
          job.get("status") == "done", f"status={job.get('status')} err={job.get('error')}")
    if job.get("status") != "done":
        return None

    entries, zresp = zip_entries(job["download_url"])
    check("ZIP archive downloads and is valid", entries is not None,
          f"status={zresp.status_code} size={len(zresp.content)} bytes")
    if entries:
        check("ZIP contains index.html", "index.html" in entries, str(entries[:8]))

    preview = requests.get(BASE + job["preview_url"], timeout=30)
    check("Live preview renders the clone",
          preview.status_code == 200 and "<html" in preview.text.lower(),
          f"status={preview.status_code}")

    # strict offline preview: missing asset must 404, not be proxied
    pid = job["preview_url"].split("/")[2]
    strict = requests.get(f"{BASE}/preview/{pid}/assets/definitely-not-here.css", timeout=15)
    check("Strict offline preview refuses missing assets (404)",
          strict.status_code == 404, f"status={strict.status_code}")

    # single-file HTML export
    single = requests.get(BASE + job["single_url"], timeout=60)
    body_ok = single.status_code == 200 and "<style" in single.text
    check("Single-file HTML export builds", body_ok,
          f"status={single.status_code} len={len(single.content)}")
    if single.status_code == 200:
        check("Single-file HTML has styles inlined (no local .css refs)",
              'href="assets/' not in single.text)

    # offline score present
    score = job.get("score")
    check("Offline score reported", isinstance(score, int) and 0 <= score <= 100,
          f"score={score}")
    return job


def test_missing_url():
    r = requests.post(f"{BASE}/api/clone", json={}, timeout=10)
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    check("Rejects a missing URL with a friendly error",
          r.status_code == 400 and "error" in data, f"status={r.status_code} body={data}")


def test_download_traversal_blocked():
    r = requests.get(f"{BASE}/download?file=../../../etc/passwd", timeout=10)
    check("Path traversal on /download is blocked", r.status_code == 400,
          f"status={r.status_code}")


def test_unreachable_domain():
    # .invalid TLD is guaranteed by RFC 2606 to never resolve
    _first, job = clone("https://this-site-does-not-exist-xyz123.invalid")
    ok = job.get("status") == "error" and job.get("error")
    check("Unreachable domain fails loudly (no fake success)",
          ok, f"status={job.get('status')} error={job.get('error')}")


def test_asset_rich_site(url):
    first, job = clone(url, depth=1, max_pages=15)
    check(f"Clones an asset-rich site ({url})",
          job.get("status") == "done",
          f"status={job.get('status')} err={job.get('error')}")
    if job.get("status") != "done":
        return

    entries, zresp = zip_entries(job["download_url"])
    if not entries:
        check("ZIP archive downloads and is valid", False,
              f"status={zresp.status_code}")
        return
    zfile = zipfile.ZipFile(io.BytesIO(zresp.content))
    html = [e for e in entries if e.endswith((".html", ".htm"))]
    css = [e for e in entries if e.endswith(".css")]
    js = [e for e in entries if e.endswith(".js")]
    imgs = [e for e in entries if e.lower().endswith(
        (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico"))]
    fonts = [e for e in entries if e.lower().endswith(
        (".woff", ".woff2", ".ttf", ".otf", ".eot"))]

    check("ZIP contains HTML page(s)", len(html) >= 1, f"{len(html)} page(s)")
    check("ZIP contains stylesheets", len(css) >= 1, f"{len(css)} css")
    check("ZIP contains scripts", len(js) >= 1, f"{len(js)} js")
    check("ZIP contains images", len(imgs) >= 1, f"{len(imgs)} images")
    check("ZIP contains fonts pulled out of CSS", len(fonts) >= 1,
          f"{len(fonts)} fonts" if fonts else
          "no fonts found (site may use system fonts only)")

    home = None
    for entry in html:
        home = zfile.read(entry).decode("utf-8", errors="ignore")
        break
    if home:
        check("HTML references local assets (assets/...)", "assets/" in home)
        check("No absolute origin URLs left in <script src>",
              'src="http' not in home.lower())

    check("Offline score reported for real site",
          isinstance(job.get("score"), int) and 0 <= job["score"] <= 100,
          f"score={job.get('score')} missing={job.get('missing')}")
    check("Progress was tracked during the crawl",
          job.get("pages_done", 0) >= 1 and job.get("assets_done", 0) >= 1,
          f"pages={job.get('pages_done')} assets={job.get('assets_done')} "
          f"bytes={job.get('bytes')}")

    preview = requests.get(BASE + job["preview_url"], timeout=30)
    check("Live preview renders the clone",
          preview.status_code == 200 and "<html" in preview.text.lower(),
          f"status={preview.status_code}")

    if css:
        pid = job["preview_url"].split("/")[2]
        asset = requests.get(f"{BASE}/preview/{pid}/{css[0]}", timeout=30)
        check("Preview serves downloaded CSS asset",
              asset.status_code == 200,
              f"status={asset.status_code} type={asset.headers.get('Content-Type')}")


def test_js_rendering():
    _first, job = clone("https://example.com", depth=1, max_pages=3, render=True)
    if job.get("status") == "error" and "renderer" in (job.get("error") or ""):
        skip("JS rendering (no browser available on this machine)", job.get("error"))
        return
    check("JS rendering mode completes", job.get("status") == "done",
          f"status={job.get('status')} err={job.get('error')}")


def test_cli_mode():
    out_dir = os.path.join(tempfile.mkdtemp(prefix="cloner_cli_"), "site")
    proc = subprocess.run(
        [sys.executable, os.path.join(APP_DIR, "cli.py"),
         "https://example.com", "--out", out_dir, "--single", "-q",
         "--max-pages", "3", "--no-robots"],
        capture_output=True, text=True, timeout=180,
    )
    ok = proc.returncode == 0
    check("CLI mode runs (cloner clone ...)", ok,
          f"rc={proc.returncode} err={proc.stderr.strip()[:200]}")
    if not ok:
        return
    check("CLI writes the site to --out",
          os.path.isfile(os.path.join(out_dir, "index.html")))
    check("CLI --single builds single.html",
          os.path.isfile(os.path.join(out_dir, "single.html")))
    check("CLI prints the offline score", "offline" in proc.stdout,
          proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "no output")


def main():
    if not server_up():
        print(f"❌ Server is not responding at {BASE}. Start it first with: cloner start")
        sys.exit(2)

    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TARGET
    print(f"\n🧪 Running Website Cloner test suite against {BASE}")
    print(f"   Asset-rich test target: {target}\n")

    print("Basic behaviour (async jobs):")
    test_ui()
    test_async_clone_simple()
    test_missing_url()
    test_download_traversal_blocked()
    test_unreachable_domain()

    print("\nReal-world clone:")
    test_asset_rich_site(target)

    print("\nJS rendering + CLI:")
    test_js_rendering()
    test_cli_mode()

    passed = sum(1 for _, ok in _results if ok)
    total = len(_results)
    print(f"\n{'=' * 50}")
    print(f"Result: {passed}/{total} tests passed")
    if passed != total:
        print("Failed tests:")
        for name, ok in _results:
            if not ok:
                print(f"  - {name}")
    print("=" * 50)
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
