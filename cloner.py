"""Core website cloning engine.

 * parallel page crawling with page/asset budgets
 * deep asset capture: stylesheets, scripts, images, every srcset candidate,
   fonts and every url()/@import reference found inside CSS
 * offline score: how much of the clone works without the internet
 * optional JS rendering (Playwright or Selenium) for SPA / React / Next sites
 * polite crawling: robots.txt, retries with backoff
"""
import base64
import mimetypes
import os
import posixpath
import re
import tempfile
import threading
import time
import zipfile
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import minify_html
import rcssmin
import rjsmin
import requests
from bs4 import BeautifulSoup
from PIL import Image

try:
    from renderer import render_page, looks_like_spa_shell, available as renderer_available
except Exception:  # pragma: no cover
    def render_page(url, timeout=60):
        return None

    def looks_like_spa_shell(html):
        return False

    def renderer_available():
        return False

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) '
                  'Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
}

PAGE_WORKERS = 4
ASSET_WORKERS = 12
MAX_ASSETS = 4000
MAX_FILE_BYTES = 60 * 1024 * 1024
MAX_INLINE_IMAGE_BYTES = 12 * 1024 * 1024
DEFAULT_MAX_PAGES = 60
FETCH_RETRIES = 2
RETRY_STATUSES = (429, 500, 502, 503, 504)
CSS_DEPTH_LIMIT = 3
MAX_MISSING_REPORTED = 200

CSS_URL_RE = re.compile(r"""url\(\s*(?:'([^']+)'|"([^"]+)"|([^)'"\s]+))\s*\)""")
CSS_IMPORT_RE = re.compile(r"""@import\s+['"]([^'"]+)['"]""")
STYLE_ATTR_RE = re.compile(r"""url\(\s*(?:'([^']+)'|"([^"]+)"|([^)'"\s]+))\s*\)""")
SKIP_PREFIXES = ('data:', 'javascript:', 'mailto:', 'tel:', '#', 'blob:', 'about:')

SHEET_RELS = ('stylesheet',)
CACHED_RELS = ('icon', 'shortcut icon', 'apple-touch-icon', 'mask-icon', 'manifest',
               'apple-touch-icon-precomposed')


def normalize(url):
    p = urlparse(url)
    path = p.path.rstrip('/') or '/'
    return f"{p.scheme}://{p.netloc}{path}"


def clean_filename(url, is_page=False):
    parsed = urlparse(url)
    if is_page:
        path = parsed.path
        if not path or path == '/':
            return "index.html"
        basename = path.strip('/').replace('/', '_')
        if not basename.endswith('.html'):
            basename += '.html'
        return basename
    basename = os.path.basename(parsed.path)
    if not basename:
        return f"asset_{abs(hash(url)) % 100000}.dat"
    return re.sub(r'[^\w\-_\.]', '_', basename)


def guess_kind(url):
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    if ext == '.css':
        return 'css'
    if ext in ('.js', '.mjs', '.cjs'):
        return 'js'
    if ext in ('.woff', '.woff2', '.ttf', '.otf', '.eot'):
        return 'font'
    if ext in ('.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.ico', '.avif', '.bmp'):
        return 'img'
    if ext in ('.mp4', '.webm', '.mp3', '.wav', '.ogg'):
        return 'media'
    return 'other'


def parse_srcset(value):
    """Parse a srcset attribute into [(url, descriptor), ...]."""
    if not value or value.strip().startswith('data:'):
        return []
    out = []
    for part in value.split(','):
        part = part.strip()
        if not part:
            continue
        bits = part.split()
        out.append((bits[0], ' '.join(bits[1:])))
    return out


def optimize_image(filepath):
    try:
        with Image.open(filepath) as img:
            img.save(filepath, optimize=True, quality=82)
    except Exception:
        pass


def _data_uri(filepath):
    mime, _ = mimetypes.guess_type(filepath)
    mime = mime or 'application/octet-stream'
    with open(filepath, 'rb') as f:
        data = f.read()
    return f"data:{mime};base64," + base64.b64encode(data).decode('ascii')


class Crawler:
    def __init__(self, start_url, max_depth=1, max_pages=DEFAULT_MAX_PAGES,
                 progress_cb=None, respect_robots=True, render='auto', page_delay=0.0,
                 verbose=True):
        if not start_url.startswith('http'):
            start_url = 'https://' + start_url
        self.start_url = start_url
        self.max_depth = max(1, int(max_depth))
        self.max_pages = max(1, int(max_pages))
        self.progress_cb = progress_cb
        self.respect_robots = respect_robots
        self.render = render
        self.page_delay = float(page_delay or 0)
        self.verbose = verbose

        self.base_domain = urlparse(start_url).netloc
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

        self.temp_dir = tempfile.mkdtemp(prefix='clone_')
        self.assets_dir = os.path.join(self.temp_dir, 'assets')
        os.makedirs(self.assets_dir, exist_ok=True)

        self.lock = threading.RLock()
        self.visited = set()
        self.page_map = {}
        self.asset_cache = {}     # remote url -> 'assets/x' | None
        self.asset_pending = {}   # remote url -> Event (download in flight)
        self.robots = {}          # origin -> True (allow all) | RobotFileParser
        self.css_done = set()     # local css paths already deep-processed
        self.missing = []         # urls we could not download
        self.warnings = []
        self.stats = dict(pages_done=0, pages_failed=0, pages_started=1,
                          assets_ok=0, assets_failed=0, refs_total=0,
                          refs_localized=0, bytes=0)
        self.current = start_url
        self.last_page = ''
        self.last_file = ''
        self.queue = deque([(start_url, 1)])

        self.asset_pool = ThreadPoolExecutor(max_workers=ASSET_WORKERS)

    # ------------------------------------------------------------------ util
    def _emit(self, phase):
        if not self.progress_cb:
            return
        with self.lock:
            snap = dict(phase=phase, current=self.current,
                        pages_done=self.stats['pages_done'],
                        pages_failed=self.stats['pages_failed'],
                        pages_started=self.stats['pages_started'],
                        pages_queued=len(self.queue),
                        assets_done=self.stats['assets_ok'],
                        assets_failed=self.stats['assets_failed'],
                        bytes=self.stats['bytes'],
                        missing=len(self.missing),
                        last_page=self.last_page,
                        last_file=self.last_file)
        try:
            self.progress_cb(snap)
        except Exception:
            pass

    def _fetch(self, url, timeout=15):
        for attempt in range(FETCH_RETRIES + 1):
            try:
                resp = self.session.get(url, timeout=timeout)
                if resp.status_code in RETRY_STATUSES and attempt < FETCH_RETRIES:
                    time.sleep(0.6 * (attempt + 1))
                    continue
                return resp
            except requests.RequestException:
                if attempt < FETCH_RETRIES:
                    time.sleep(0.5 * (attempt + 1))
        return None

    def _mark_missing(self, url):
        with self.lock:
            if len(self.missing) < MAX_MISSING_REPORTED and url not in self.missing:
                self.missing.append(url)

    def _robots_allowed(self, url):
        if not self.respect_robots:
            return True
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        with self.lock:
            rp = self.robots.get(origin)
        if rp is None:
            rp = True  # allow all until proven otherwise
            try:
                resp = self.session.get(origin + '/robots.txt', timeout=6)
                if resp.status_code == 200 and len(resp.text) < 300000:
                    parser = robotparser.RobotFileParser()
                    parser.parse(resp.text.splitlines())
                    rp = parser
            except Exception:
                rp = True
            with self.lock:
                self.robots[origin] = rp
        if rp is True:
            return True
        if normalize(url) == normalize(self.start_url):
            return True  # always allow the exact page the user asked for
        try:
            return rp.can_fetch('*', url)
        except Exception:
            return True

    # --------------------------------------------------------------- assets
    def _unique_name(self, url):
        filename = clean_filename(url)
        base, ext = os.path.splitext(filename)
        filepath = os.path.join(self.assets_dir, filename)
        counter = 1
        while os.path.exists(filepath):
            filename = f"{base}_{counter}{ext}"
            filepath = os.path.join(self.assets_dir, filename)
            counter += 1
        return filename, filepath

    def _download_asset(self, asset_url, kind='', css_depth=0):
        """Download one asset (thread safe, deduplicated). Returns local rel path."""
        if not asset_url or asset_url.startswith(SKIP_PREFIXES):
            return None
        with self.lock:
            if asset_url in self.asset_cache:
                return self.asset_cache[asset_url]
            pending = self.asset_pending.get(asset_url)
            if pending is None:
                if len(self.asset_cache) >= MAX_ASSETS:
                    return None
                pending = threading.Event()
                self.asset_pending[asset_url] = pending
                owner = True
            else:
                owner = False
        if not owner:  # another worker is fetching this exact asset
            pending.wait(30)
            with self.lock:
                return self.asset_cache.get(asset_url)

        try:
            local = self._fetch_asset(asset_url, kind, css_depth)
        except Exception:
            local = None
        with self.lock:
            self.asset_cache[asset_url] = local
            if local:
                self.stats['assets_ok'] += 1
            else:
                self.stats['assets_failed'] += 1
                self._mark_missing(asset_url)
            self.asset_pending.pop(asset_url, None)
        pending.set()
        return local

    def _fetch_asset(self, asset_url, kind, css_depth):
        resp = self._fetch(asset_url, timeout=20)
        if resp is None or resp.status_code != 200:
            return None
        if not kind or kind == 'other':
            kind = guess_kind(asset_url)
        content_type = resp.headers.get('Content-Type', '')
        if 'text/css' in content_type:
            kind = 'css'
        elif 'javascript' in content_type:
            kind = kind if kind == 'js' else 'js'

        content = resp.content
        if len(content) > MAX_FILE_BYTES:
            return None

        if kind == 'css':
            try:
                content = rcssmin.cssmin(content.decode('utf-8', 'ignore')).encode('utf-8')
            except Exception:
                pass
        elif kind == 'js':
            try:
                content = rjsmin.jsmin(content.decode('utf-8', 'ignore')).encode('utf-8')
            except Exception:
                pass

        with self.lock:
            filename, filepath = self._unique_name(asset_url)
            with open(filepath, 'wb') as f:
                f.write(content)

        if kind == 'img':
            optimize_image(filepath)

        with self.lock:
            self.stats['bytes'] += os.path.getsize(filepath)

        if kind == 'css':
            self._process_css_file(asset_url, filename, css_depth)

        return f"assets/{filename}"

    def _rewrite_css_text(self, text, base_url, css_file=None, depth=0):
        """Download every url()/@import inside CSS and rewrite to local paths.

        css_file: filename when rewriting a CSS file living in assets/
                  (so references stay relative to it), else None for inline CSS.
        Returns (new_text, changed_count).
        """
        if not text:
            return text, 0
        changed = 0

        def localize(ref):
            nonlocal changed
            if not ref or ref.startswith(SKIP_PREFIXES):
                return None
            resolved = urljoin(base_url, ref)
            if not resolved.startswith(('http://', 'https://')):
                return None
            kind = guess_kind(resolved)
            if kind == 'media':
                return None  # never pull heavy video/audio out of CSS
            with self.lock:
                self.stats['refs_total'] += 1
            local = self._download_asset(resolved, kind, depth + 1)
            if not local:
                return None
            with self.lock:
                self.stats['refs_localized'] += 1
            changed += 1
            if css_file:
                return posixpath.basename(local)
            return local

        def import_repl(m):
            new = localize(m.group(1))
            if new is None:
                return m.group(0)
            return f'@import "{new}"'

        def url_repl(m):
            ref = next((g for g in m.groups() if g), None)
            new = localize(ref)
            if new is None:
                return m.group(0)
            return f'url("{new}")'

        # @import "x.css" (without url()) first, then every url()
        text = CSS_IMPORT_RE.sub(import_repl, text)
        text = CSS_URL_RE.sub(url_repl, text)
        return text, changed

    def _process_css_file(self, css_url, filename, depth=0):
        if depth > CSS_DEPTH_LIMIT:
            return
        local_rel = f"assets/{filename}"
        with self.lock:
            if local_rel in self.css_done:
                return
            self.css_done.add(local_rel)
        filepath = os.path.join(self.assets_dir, filename)
        try:
            with open(filepath, encoding='utf-8', errors='ignore') as f:
                css = f.read()
        except Exception:
            return
        new_css, changed = self._rewrite_css_text(css, css_url, css_file=filename,
                                                  depth=depth)
        if changed:
            try:
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(new_css)
            except Exception:
                pass

    # ---------------------------------------------------------------- pages
    def _collect_asset_jobs(self, soup, page_url):
        jobs = {}

        def add(u, kind):
            if not u or u.startswith(SKIP_PREFIXES):
                return
            full = urljoin(page_url, u)
            if not full.startswith(('http://', 'https://')):
                return
            if guess_kind(full) == 'media':
                return
            if full not in jobs:
                jobs[full] = kind

        for link in soup.find_all('link', href=True):
            rels = [r.lower() for r in link.get('rel', [])]
            if any(r in rels for r in SHEET_RELS):
                add(link['href'], 'css')
            elif any(r in rels for r in CACHED_RELS):
                add(link['href'], guess_kind(link['href']) or 'img')

        for script in soup.find_all('script', src=True):
            add(script['src'], 'js')

        for img in soup.find_all('img'):
            src = img.get('src')
            if src:
                add(src, 'img')
            for cand, _desc in parse_srcset(img.get('srcset') or ''):
                add(cand, 'img')

        for tag in soup.find_all(['source', 'video']):
            if tag.get('srcset'):
                for cand, _desc in parse_srcset(tag.get('srcset') or ''):
                    add(cand, 'img')
            poster = tag.get('poster')
            if poster:
                add(poster, 'img')

        return jobs

    def _rewrite_html_assets(self, soup, page_url, results):
        def local_of(u):
            full = urljoin(page_url, u)
            return results.get(full)

        def count(u):
            with self.lock:
                self.stats['refs_total'] += 1

        def count_ok(u):
            with self.lock:
                self.stats['refs_localized'] += 1

        for link in soup.find_all('link', href=True):
            rels = [r.lower() for r in link.get('rel', [])]
            if not (any(r in rels for r in SHEET_RELS) or any(r in rels for r in CACHED_RELS)):
                continue
            if link['href'].startswith(SKIP_PREFIXES):
                continue
            count(link['href'])
            local = local_of(link['href'])
            if local:
                link['href'] = local
                count_ok(link['href'])

        for script in soup.find_all('script', src=True):
            if script['src'].startswith(SKIP_PREFIXES):
                continue
            count(script['src'])
            local = local_of(script['src'])
            if local:
                script['src'] = local
                count_ok(script['src'])

        for img in soup.find_all('img'):
            src = img.get('src')
            if src and not src.startswith(SKIP_PREFIXES):
                count(src)
                local = local_of(src)
                if local:
                    img['src'] = local
                    count_ok(src)
            srcset = img.get('srcset')
            if srcset:
                candidates = parse_srcset(srcset)
                if candidates:
                    rebuilt = []
                    for cand, desc in candidates:
                        if cand.startswith(SKIP_PREFIXES):
                            rebuilt.append(f"{cand} {desc}".strip())
                            continue
                        count(cand)
                        local = local_of(cand)
                        if local:
                            rebuilt.append(f"{local} {desc}".strip())
                            count_ok(cand)
                        else:
                            rebuilt.append(f"{cand} {desc}".strip())
                    if rebuilt:
                        img['srcset'] = ', '.join(rebuilt)

        for tag in soup.find_all(['source', 'video']):
            srcset = tag.get('srcset')
            if srcset:
                candidates = parse_srcset(srcset)
                rebuilt = []
                for cand, desc in candidates:
                    if cand.startswith(SKIP_PREFIXES):
                        rebuilt.append(f"{cand} {desc}".strip())
                        continue
                    count(cand)
                    local = local_of(cand)
                    if local:
                        rebuilt.append(f"{local} {desc}".strip())
                        count_ok(cand)
                    else:
                        rebuilt.append(f"{cand} {desc}".strip())
                if rebuilt:
                    tag['srcset'] = ', '.join(rebuilt)
            poster = tag.get('poster')
            if poster and not poster.startswith(SKIP_PREFIXES):
                count(poster)
                local = local_of(poster)
                if local:
                    tag['poster'] = local
                    count_ok(poster)

    def _rewrite_inline_css(self, soup, page_url):
        """Rewrite url() inside <style> blocks and style="" attributes."""
        for style in soup.find_all('style'):
            if style.string:
                new_css, _ = self._rewrite_css_text(style.string, page_url, depth=0)
                style.string.replace_with(new_css) if False else None
                style.clear()
                style.append(new_css)
        for tag in soup.find_all(style=True):
            value = tag.get('style') or ''
            if 'url(' not in value:
                continue
            new_css, _ = self._rewrite_css_text(value, page_url, depth=0)
            tag['style'] = new_css

    def _rewrite_links(self, soup, page_url, depth):
        for a_tag in soup.find_all('a', href=True):
            href = a_tag['href']
            if not href or href.startswith(('#', 'mailto:', 'tel:', 'javascript:')):
                continue
            full = urljoin(page_url, href)
            hp = urlparse(full)
            if hp.scheme not in ('http', 'https'):
                continue
            if hp.netloc == self.base_domain:
                nh = normalize(full)
                with self.lock:
                    known = nh in self.page_map
                    if not known and self.stats['pages_started'] < self.max_pages \
                            and depth < self.max_depth and nh not in self.visited:
                        # reserve a crawl slot now so every rewritten link has a page
                        self.stats['pages_started'] += 1
                        self.page_map[nh] = clean_filename(nh, is_page=True)
                        self.queue.append((full, depth + 1))
                        known = True
                if known:
                    a_tag['href'] = self.page_map.get(nh, a_tag['href'])
            elif hp.netloc:
                a_tag['target'] = '_blank'
                a_tag['rel'] = 'noopener noreferrer'

    def _process_page(self, url, depth):
        norm = normalize(url)
        with self.lock:
            if norm in self.visited:
                return
            self.visited.add(norm)
        if not self._robots_allowed(url):
            if self.verbose:
                print(f" robots.txt disallows: {url}", flush=True)
            return

        with self.lock:
            self.current = url
        self._emit('fetching')

        is_start = norm == normalize(self.start_url)
        render_needed = self.render is True or (
            self.render == 'auto' and renderer_available())

        resp = self._fetch(url, timeout=20)
        if resp is None or resp.status_code >= 400:
            with self.lock:
                self.stats['pages_failed'] += 1
            if self.verbose:
                print(f"✗ Failed to fetch {url}", flush=True)
            self._emit('crawling')
            return
        html = resp.text
        norm = normalize(resp.url)

        if render_needed and (self.render is True or looks_like_spa_shell(html)):
            rendered = render_page(resp.url)
            if rendered:
                html = rendered
            else:
                with self.lock:
                    if 'js-render' not in self.warnings:
                        self.warnings.append(
                            'js-render: no renderer available, used raw HTML')
                if self.render is True and not renderer_available():
                    with self.lock:
                        if 'renderer-missing' not in self.warnings:
                            self.warnings.append(
                                'JS rendering requested but no renderer installed '
                                '(pip install playwright)')

        soup = BeautifulSoup(html, 'html.parser')

        # 1. download every static asset this page references (in parallel)
        jobs = self._collect_asset_jobs(soup, url)
        results = {}
        if jobs:
            futures = {self.asset_pool.submit(self._download_asset, u, k): u
                       for u, k in jobs.items()}
            for fut in as_completed(futures):
                u = futures[fut]
                try:
                    results[u] = fut.result()
                except Exception:
                    results[u] = None

        # 2. rewrite references to the local copies
        self._rewrite_html_assets(soup, url, results)
        self._rewrite_inline_css(soup, url)
        self._rewrite_links(soup, url, depth)

        # 3. local file name
        with self.lock:
            if is_start:
                filename = 'index.html'
            elif norm in self.page_map:
                filename = self.page_map[norm]
            else:
                filename = clean_filename(norm, is_page=True)
            self.page_map[norm] = filename

        raw_html = str(soup)
        try:
            final_html = minify_html.minify(raw_html, minify_js=True,
                                            remove_processing_instructions=True)
        except Exception:
            final_html = raw_html

        with open(os.path.join(self.temp_dir, filename), 'w', encoding='utf-8') as f:
            f.write(final_html)

        with self.lock:
            self.stats['pages_done'] += 1
            self.stats['bytes'] += len(final_html.encode('utf-8'))
            self.last_page = url
            self.last_file = filename
        if self.page_delay:
            time.sleep(self.page_delay)
        if self.verbose:
            print(f"✓ Cloned: {url} → {filename} ({len(final_html):,} bytes)", flush=True)
        self._emit('crawling')

    # ----------------------------------------------------------------- run
    def run(self):
        try:
            while True:
                wave = []
                with self.lock:
                    while self.queue and len(wave) < PAGE_WORKERS:
                        wave.append(self.queue.popleft())
                if not wave:
                    break
                with ThreadPoolExecutor(max_workers=PAGE_WORKERS) as pool:
                    futures = [pool.submit(self._process_page, u, d) for u, d in wave]
                    for fut in as_completed(futures):
                        try:
                            fut.result()
                        except Exception as exc:
                            if self.verbose:
                                print(f"page worker error: {exc}", flush=True)

            with self.lock:
                pages_done = self.stats['pages_done']
                pages_failed = self.stats['pages_failed']
            if not pages_done:
                raise RuntimeError(
                    f"Could not clone {self.start_url}: the site could not be reached "
                    "(bad URL, DNS failure, timeout, blocked, or disallowed by robots.txt)."
                )
            if not os.path.exists(os.path.join(self.temp_dir, 'index.html')):
                raise RuntimeError(
                    f"Could not clone {self.start_url}: the start page failed to download."
                )

            self._emit('packaging')
            zip_path = self._make_zip()

            with self.lock:
                total = self.stats['refs_total']
                localized = self.stats['refs_localized']
                score = round(100 * localized / total) if total else 100
                missing = list(self.missing)
                warnings = list(self.warnings)
                result = dict(
                    zip=zip_path,
                    dir=self.temp_dir,
                    score=max(0, min(100, score)),
                    pages=pages_done,
                    pages_failed=pages_failed,
                    assets=self.stats['assets_ok'],
                    assets_failed=self.stats['assets_failed'],
                    bytes=self.stats['bytes'],
                    missing=missing,
                    warnings=warnings,
                )
            self._emit('done')
            return result
        finally:
            self.asset_pool.shutdown(wait=False)

    def _make_zip(self):
        zip_filename = f"cloned_{abs(hash(self.start_url))}.zip"
        zip_filepath = os.path.join(tempfile.gettempdir(), zip_filename)
        with zipfile.ZipFile(zip_filepath, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, _dirs, files in os.walk(self.temp_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, self.temp_dir)
                    zipf.write(file_path, arcname)
        return zip_filepath


def clone_website(start_url, max_depth=1, max_pages=DEFAULT_MAX_PAGES,
                  progress_cb=None, respect_robots=True, render='auto', page_delay=0.0,
                  verbose=True):
    """Clone a website. Returns a result dict (zip path, dir, score, stats...)."""
    crawler = Crawler(start_url, max_depth=max_depth, max_pages=max_pages,
                      progress_cb=progress_cb, respect_robots=respect_robots,
                      render=render, page_delay=page_delay, verbose=verbose)
    return crawler.run()


# ----------------------------------------------------------- single file
def _inline_css_for_single(css_path, temp_dir):
    try:
        with open(css_path, encoding='utf-8', errors='ignore') as f:
            css = f.read()
    except Exception:
        return ''

    def repl(m):
        ref = next((g for g in m.groups() if g), None)
        if not ref or ref.startswith(SKIP_PREFIXES):
            return m.group(0)
        if ref.startswith(('http://', 'https://', '//')):
            return m.group(0)
        target = os.path.normpath(os.path.join(os.path.dirname(css_path),
                                                ref.split('?')[0].split('#')[0]))
        if os.path.abspath(target).startswith(os.path.abspath(temp_dir)) and os.path.isfile(target):
            try:
                return f'url("{_data_uri(target)}")'
            except Exception:
                return m.group(0)
        return m.group(0)

    return CSS_URL_RE.sub(repl, css)


def build_single_html(temp_dir, page='index.html', out_path=None):
    """Bundle one cloned page + its assets into a single self-contained HTML."""
    src = os.path.join(temp_dir, page)
    if not os.path.isfile(src):
        raise RuntimeError(f"{page} not found in clone")
    with open(src, encoding='utf-8') as f:
        soup = BeautifulSoup(f.read(), 'html.parser')

    def local_path(ref):
        if not ref or ref.startswith(SKIP_PREFIXES):
            return None
        if ref.startswith(('http://', 'https://', '//')):
            return None
        clean = ref.split('#')[0].split('?')[0]
        if not clean:
            return None
        target = os.path.normpath(os.path.join(temp_dir, clean))
        if not os.path.abspath(target).startswith(os.path.abspath(temp_dir)):
            return None
        return target if os.path.isfile(target) else None

    for link in soup.find_all('link', href=True):
        rels = [r.lower() for r in link.get('rel', [])]
        lp = local_path(link['href'])
        if lp and any(r in rels for r in SHEET_RELS):
            tag = soup.new_tag('style')
            tag.string = _inline_css_for_single(lp, temp_dir)
            link.replace_with(tag)
        elif lp and any(r in rels for r in CACHED_RELS):
            link.decompose()

    for script in soup.find_all('script', src=True):
        lp = local_path(script['src'])
        if not lp:
            continue
        try:
            with open(lp, encoding='utf-8', errors='ignore') as f:
                text = f.read()
        except Exception:
            script.decompose()
            continue
        tag = soup.new_tag('script')
        if script.get('type') == 'module':
            tag['type'] = 'module'
        tag.string = text
        script.replace_with(tag)

    for tag in soup.find_all(['img', 'video']):
        for attr in ('src', 'poster'):
            value = tag.get(attr)
            if not value or value.startswith(SKIP_PREFIXES):
                continue
            lp = local_path(value)
            if lp and os.path.getsize(lp) <= MAX_INLINE_IMAGE_BYTES:
                try:
                    tag[attr] = _data_uri(lp)
                    if tag.get('srcset'):
                        del tag['srcset']
                except Exception:
                    pass

    for tag in soup.find_all(['source']):
        if tag.get('srcset'):
            candidates = parse_srcset(tag['srcset'])
            for cand, _d in candidates:
                lp = local_path(cand)
                if lp and os.path.getsize(lp) <= MAX_INLINE_IMAGE_BYTES:
                    try:
                        tag['srcset'] = _data_uri(lp)
                        break
                    except Exception:
                        pass

    out_path = out_path or os.path.join(temp_dir, 'single.html')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(str(soup))
    return out_path
