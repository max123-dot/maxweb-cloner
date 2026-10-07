"""JS rendering backends for SPA / React / Next.js sites.

Two interchangeable backends, tried in order:
  1. Playwright  (pip install playwright && playwright install chromium)
  2. Selenium    (already available here) driving the system Google Chrome

A single background thread owns the browser (both APIs are not thread-safe)
and serves render requests through a queue, so parallel page workers can ask
for renders safely.
"""
import queue
import threading

try:
    from playwright.sync_api import sync_playwright
    _HAS_PLAYWRIGHT = True
except Exception:
    _HAS_PLAYWRIGHT = False

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    _HAS_SELENIUM = True
except Exception:
    _HAS_SELENIUM = False

RENDER_TIMEOUT_S = 45
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def available():
    return _HAS_PLAYWRIGHT or _HAS_SELENIUM


class Renderer:
    """Singleton render worker: one browser, one owning thread."""

    def __init__(self):
        self._queue = queue.Queue(maxsize=8)
        self._thread = None
        self._lock = threading.Lock()
        self._failed = False

    # ------------------------------------------------------------- worker
    def _ensure_worker(self):
        with self._lock:
            if self._failed:
                return False
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._worker, daemon=True,
                                                name='cloner-renderer')
                self._thread.start()
            return True

    def _drain(self, error):
        while True:
            try:
                job = self._queue.get_nowait()
            except queue.Empty:
                return
            job['result'] = None
            job['error'] = error
            job['event'].set()

    def _worker(self):
        if self._run_playwright():
            return
        if self._run_selenium():
            return
        self._failed = True
        self._drain('No JS renderer available (pip install playwright)')

    def _run_playwright(self):
        if not _HAS_PLAYWRIGHT:
            return False
        try:
            with sync_playwright() as p:
                browser = None
                for kind in ('chrome', 'chromium', 'default'):
                    try:
                        if kind == 'chrome':
                            browser = p.chromium.launch(headless=True, channel='chrome')
                        elif kind == 'chromium':
                            browser = p.chromium.launch(headless=True)
                        else:
                            browser = p.chromium.launch(headless=True)
                        break
                    except Exception:
                        browser = None
                if browser is None:
                    return False

                while True:
                    job = self._queue.get()
                    if job is None:
                        return
                    context = None
                    try:
                        context = browser.new_context(user_agent=UA,
                                                      viewport={'width': 1440, 'height': 900})
                        page = context.new_page()
                        page.goto(job['url'], wait_until='load',
                                  timeout=RENDER_TIMEOUT_S * 1000)
                        try:
                            page.wait_for_load_state('networkidle', timeout=8000)
                        except Exception:
                            pass
                        page.wait_for_timeout(400)
                        job['result'] = page.content()
                        job['error'] = None
                    except Exception as exc:
                        job['result'] = None
                        job['error'] = str(exc)
                    finally:
                        try:
                            if context is not None:
                                context.close()
                        except Exception:
                            pass
                        job['event'].set()
        except Exception:
            return False
        return True

    def _run_selenium(self):
        if not _HAS_SELENIUM:
            return False
        driver = None
        try:
            opts = Options()
            opts.add_argument('--headless=new')
            opts.add_argument('--no-sandbox')
            opts.add_argument('--disable-gpu')
            opts.add_argument('--disable-dev-shm-usage')
            opts.add_argument('--window-size=1440,900')
            opts.add_argument(f'--user-agent={UA}')
            opts.page_load_strategy = 'eager'
            driver = webdriver.Chrome(options=opts)
            driver.set_page_load_timeout(RENDER_TIMEOUT_S)
        except Exception:
            return False

        try:
            while True:
                job = self._queue.get()
                if job is None:
                    return
                try:
                    driver.get(job['url'])
                    try:
                        driver.execute_script(
                            "return document.readyState === 'complete'", )
                    except Exception:
                        pass
                    driver.implicitly_wait(1.5)
                    job['result'] = driver.page_source
                    job['error'] = None
                except Exception as exc:
                    job['result'] = None
                    job['error'] = str(exc)
                finally:
                    job['event'].set()
        finally:
            try:
                driver.quit()
            except Exception:
                pass

    # ------------------------------------------------------------ public
    def render(self, url, timeout=RENDER_TIMEOUT_S + 15):
        """Return the fully rendered HTML for *url*, or None on failure."""
        if self._failed or not available():
            return None
        if not self._ensure_worker():
            return None
        job = {'url': url, 'event': threading.Event(), 'result': None, 'error': None}
        try:
            self._queue.put(job, timeout=5)
        except queue.Empty:
            return None
        if not job['event'].wait(timeout):
            return None
        return job['result']


_renderer = Renderer()


def render_page(url, timeout=RENDER_TIMEOUT_S + 15):
    return _renderer.render(url, timeout=timeout)


def looks_like_spa_shell(html):
    """True when a page looks like an un-rendered JS app shell."""
    if not html or len(html) > 60000:
        return False
    markers = ('id="root"', "id='root'", 'id="__next"', 'id="__nuxt"',
               'id="app"', 'id="mount"', 'id="___gatsby"')
    if not any(m in html for m in markers):
        return False
    try:
        from bs4 import BeautifulSoup
        text = BeautifulSoup(html, 'html.parser').get_text(' ', strip=True)
    except Exception:
        return False
    return len(text) < 400
