#!/usr/bin/env python
"""Terminal mode: clone a website straight from the command line.

Examples:
    cloner clone https://example.com
    cloner clone https://www.python.org --depth 2 --max-pages 30 --single
    cloner clone https://news.ycombinator.com --out ./hn --render
"""
import argparse
import os
import shutil
import sys
import time
from urllib.parse import urlparse

from cloner import build_single_html, clone_website


def human(n):
    n = float(n or 0)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return f"{n:.0f} {unit}" if unit == 'B' else f"{n:.1f} {unit}"
        n /= 1024


def main():
    parser = argparse.ArgumentParser(
        prog='cloner clone',
        description='Clone a website (HTML, CSS, JS, images, fonts) from the terminal.')
    parser.add_argument('url', help='website to clone, e.g. https://example.com')
    parser.add_argument('-d', '--depth', type=int, default=1,
                        help='crawl depth of internal links (default: 1)')
    parser.add_argument('-m', '--max-pages', type=int, default=60,
                        help='maximum number of pages to crawl (default: 60)')
    parser.add_argument('-o', '--out', default=None,
                        help='output directory (default: ./<hostname>)')
    parser.add_argument('--single', action='store_true',
                        help='also build single.html (fully self-contained page)')
    parser.add_argument('--zip', dest='keep_zip', action='store_true',
                        help='also keep a cloned_site.zip in the output directory')
    parser.add_argument('--no-robots', action='store_true',
                        help='ignore robots.txt (crawl everything)')
    parser.add_argument('--render', action='store_true',
                        help='render JavaScript with a real browser (React/Next sites)')
    parser.add_argument('-q', '--quiet', action='store_true', help='less output')
    args = parser.parse_args()

    def progress(snap):
        if args.quiet:
            return
        line = (f"\r  [{snap['pages_done']} pages | {snap['assets_done']} assets | "
                f"{human(snap['bytes'])}] {(snap['current'] or '')[:60]:<60}")
        sys.stderr.write(line)
        sys.stderr.flush()

    started = time.time()
    try:
        result = clone_website(
            args.url,
            max_depth=args.depth,
            max_pages=args.max_pages,
            progress_cb=progress,
            respect_robots=not args.no_robots,
            render=True if args.render else 'auto',
        )
    except Exception as exc:
        print(f"\n✗ {exc}", file=sys.stderr)
        return 1
    if not args.quiet:
        sys.stderr.write('\n')

    host = urlparse(args.url if args.url.startswith('http') else 'https://' + args.url).netloc
    out_dir = args.out or os.path.join('.', host.replace(':', '_') or 'cloned_site')
    os.makedirs(out_dir, exist_ok=True)
    shutil.copytree(result['dir'], out_dir, dirs_exist_ok=True)

    if args.keep_zip:
        shutil.copy(result['zip'], os.path.join(out_dir, 'cloned_site.zip'))

    single_path = None
    if args.single:
        try:
            single_path = build_single_html(result['dir'])
            shutil.copy(single_path, os.path.join(out_dir, 'single.html'))
        except Exception as exc:
            print(f"  (single-file build failed: {exc})", file=sys.stderr)

    elapsed = time.time() - started
    print(f"✅ Cloned {args.url}")
    print(f"   pages   : {result['pages']}"
          + (f" ({result['pages_failed']} failed)" if result.get('pages_failed') else ''))
    print(f"   assets  : {result['assets']}"
          + (f" ({result['assets_failed']} failed)" if result.get('assets_failed') else ''))
    print(f"   size    : {human(result['bytes'])}")
    print(f"   offline : {result['score']}%")
    if result['missing']:
        print(f"   missing : {len(result['missing'])} url(s), e.g. {result['missing'][0]}")
    for warning in result.get('warnings', []):
        print(f"   warning : {warning}")
    print(f"   output  : {os.path.abspath(out_dir)}")
    if single_path:
        print(f"   single  : {os.path.abspath(os.path.join(out_dir, 'single.html'))}")
    print(f"   time    : {elapsed:.1f}s")
    return 0


if __name__ == '__main__':
    sys.exit(main())
