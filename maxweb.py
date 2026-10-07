#!/usr/bin/env python
"""MaxWeb Cloner — an OpenCode-style terminal UI for the Website Cloner.

Run it with:   maxweb          (or: cloner tui)
Line mode:     maxweb --line   (automatic when there is no TTY)
One-shot:      maxweb https://example.com
"""
from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from urllib.parse import urlparse

APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from cloner import build_single_html, clone_website  # noqa: E402

NAME = "MaxWeb Cloner"
VERSION = "1.0"
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# OpenCode-inspired palette
C_BG = "#0f172a"
C_TEXT = "#e2e8f0"
C_DIM = "#64748b"
C_ACCENT = "#60a5fa"
C_ACCENT2 = "#c084fc"
C_BORDER = "#334155"
C_BORDER_FOCUS = "#3b82f6"
C_OK = "#22c55e"
C_ERR = "#ef4444"
C_WARN = "#f59e0b"
C_PATH = "#a78bfa"
C_KEY = "#93c5fd"

# symbolic styles (used when writing log lines)
SYMBOLIC = ("text", "dim", "ok", "err", "warn", "info", "path", "key", "brand", "brand2")

# symbolic -> prompt_toolkit style
def F(color, bold=False):
    return f"fg:{color} bold" if bold else f"fg:{color}"

SYM2PT = {
    "": "",
    "text": F(C_TEXT),
    "dim": F(C_DIM),
    "ok": F(C_OK),
    "err": F(C_ERR),
    "warn": F(C_WARN),
    "info": F(C_ACCENT),
    "path": F(C_PATH),
    "key": F(C_KEY),
    "brand": F(C_ACCENT, True),
    "brand2": F(C_ACCENT2, True),
}

# symbolic -> ANSI (line mode)
ANSI = {
    "": "",
    "text": "\x1b[37m",
    "dim": "\x1b[90m",
    "ok": "\x1b[32m",
    "err": "\x1b[31m",
    "warn": "\x1b[33m",
    "info": "\x1b[94m",
    "path": "\x1b[95m",
    "key": "\x1b[94m",
    "brand": "\x1b[1;94m",
    "brand2": "\x1b[1;95m",
}
ANSI_RESET = "\x1b[0m"


def human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def clip(text, limit):
    text = text or ""
    if len(text) <= limit:
        return text
    if limit <= 5:
        return text[:limit]
    keep = limit - 1
    head = keep // 2
    return text[:head] + "…" + text[len(text) - (keep - head):]


def vis_len(frags):
    return sum(len(t) for _s, t in frags)


def clip_frags(frags, limit=170):
    if vis_len(frags) <= limit:
        return frags
    out, used = [], 0
    for style, text in frags:
        if used >= limit - 1:
            break
        room = limit - 1 - used
        if len(text) > room:
            out.append((style, text[:room]))
            used += room
            break
        out.append((style, text))
        used += len(text)
    out.append((out[-1][0] if out else "", "…"))
    return out


# --------------------------------------------------------------------- help
HELP_LINES = [
    ("key", "  commands"),
    ("text", "    <url>            "),
    ("dim", "clone that website (example.com or https://site.com)"),
    ("text", "    clone <url>       "),
    ("dim", "same as above"),
    ("text", "    depth <1-5>       "),
    ("dim", "crawl depth for internal links"),
    ("text", "    render on|off     "),
    ("dim", "render JavaScript with a real browser (React/Next sites)"),
    ("text", "    robots on|off     "),
    ("dim", "respect robots.txt while crawling"),
    ("text", "    max <n>           "),
    ("dim", "max pages per clone (default 25)"),
    ("text", "    status            "),
    ("dim", "show current options"),
    ("text", "    clear             "),
    ("dim", "clear the screen (ctrl+l)"),
    ("text", "    web               "),
    ("dim", "show the web UI address"),
    ("text", "    help              "),
    ("dim", "show this help"),
    ("text", "    exit              "),
    ("dim", "quit (ctrl+d)"),
    ("key", "  keys"),
    ("dim", "    enter clone · tab cycle depth · ctrl+r render · ctrl+l clear · ctrl+d exit"),
]


class MaxWeb:
    def __init__(self):
        self.opts = {"depth": 1, "max_pages": 25, "render": False, "robots": True}
        self.log: list[list[tuple[str, str]]] = []
        self.view_offset = 0
        self.busy = False
        self.status: dict = {}
        self._seen_pages = 0
        self._thread = None
        self._last_redraw = 0.0
        self.exit_code = 0
        self.line_mode = False
        self._line_dirty = False
        self.app = None
        self.buffer = None
        self.input_window = None

    # ----------------------------------------------------------------- log
    def add(self, *frags, style=None):
        if len(frags) == 1 and isinstance(frags[0], str):
            frags = ((style or "text", frags[0]),)
        line = clip_frags(list(frags))
        if not self.line_mode:
            line = [(SYM2PT.get(s, s), t) for s, t in line]
        self.log.append(line)
        if len(self.log) > 1500:
            dropped = len(self.log) - 1200
            del self.log[:dropped]
            self.view_offset = max(0, self.view_offset - dropped)
        if self.line_mode:
            self._print_line(line)
        else:
            self._request_redraw()

    def _print_line(self, frags):
        if self._line_dirty:
            sys.stdout.write("\r\x1b[2K")
            self._line_dirty = False
        out = "".join(ANSI.get(style, "") + text for style, text in frags)
        sys.stdout.write(out + ANSI_RESET + "\n")
        sys.stdout.flush()

    def clear(self):
        self.log.clear()
        self.view_offset = 0
        self._request_redraw()

    # -------------------------------------------------------------- redraw
    def _request_redraw(self):
        app = self.app
        if app is None:
            return
        now = time.time()
        if now - self._last_redraw < 0.05:
            return
        self._last_redraw = now
        try:
            loop = getattr(app, "loop", None)
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(self._invalidate)
            else:
                app.invalidate()
        except Exception:
            pass

    def _invalidate(self):
        try:
            if self.app is not None:
                self.app.invalidate()
        except Exception:
            pass

    # --------------------------------------------------------------- clone
    def start_clone(self, url):
        if self.busy:
            self.add(("warn", "  ⧗ a clone is already running — wait for it to finish"))
            return
        raw = url.strip()
        if not raw.startswith(("http://", "https://")):
            raw = "https://" + raw
        self.busy = True
        self._seen_pages = 0
        self.status = {"phase": "starting", "current": raw, "pages_done": 0,
                       "assets_done": 0, "bytes": 0, "missing": 0}
        o = self.opts
        self.add(
            ("ok", "  ❯ "),
            ("text", "cloning "),
            ("info", raw),
            ("dim", f"   depth {o['depth']} · max {o['max_pages']} pages · "
                    f"render {'on' if o['render'] else 'off'} · "
                    f"robots {'on' if o['robots'] else 'off'}"),
        )
        self._thread = threading.Thread(target=self._worker, args=(raw,), daemon=True)
        self._thread.start()

    def _worker(self, url):
        try:
            result = clone_website(
                url,
                max_depth=self.opts["depth"],
                max_pages=self.opts["max_pages"],
                respect_robots=self.opts["robots"],
                render=True if self.opts["render"] else "auto",
                progress_cb=self._on_progress,
                verbose=False,
            )
            host = urlparse(url).netloc.replace(":", "_") or "cloned_site"
            out_dir = os.path.join(os.getcwd(), host)
            os.makedirs(out_dir, exist_ok=True)
            shutil.copytree(result["dir"], out_dir, dirs_exist_ok=True)
            single_ok = True
            try:
                single = build_single_html(result["dir"])
                shutil.copy(single, os.path.join(out_dir, "single.html"))
            except Exception:
                single_ok = False
            self._finish_ok(url, result, out_dir, single_ok)
        except Exception as exc:
            self._finish_err(url, exc)
        finally:
            self.busy = False
            self.status = {}
            self._request_redraw()

    def _on_progress(self, snap):
        self.status = snap
        done = snap.get("pages_done", 0)
        if done > self._seen_pages and snap.get("last_file"):
            self._seen_pages = done
            self.add(("ok", "    ✓ "),
                     ("text", clip(snap.get("last_page") or "", 70)),
                     ("dim", f"  →  {snap['last_file']}"))
        if self.line_mode:
            self._line_progress(snap)
        else:
            self._request_redraw()

    def _line_progress(self, snap):
        if not sys.stdout.isatty():
            return
        line = (f"  {SPINNER[int(time.time() * 10) % len(SPINNER)]} "
                f"{snap.get('phase', '')} · {snap.get('pages_done', 0)} pages · "
                f"{snap.get('assets_done', 0)} assets · {human(snap.get('bytes', 0))} · "
                f"{clip(snap.get('current') or '', 48)}")
        sys.stdout.write("\r\x1b[2K" + line)
        sys.stdout.flush()
        self._line_dirty = True

    def _finish_ok(self, url, result, out_dir, single_ok):
        if self.line_mode and self._line_dirty:
            sys.stdout.write("\r\x1b[2K")
            self._line_dirty = False
        score = result["score"]
        score_style = "ok" if score >= 90 else "warn" if score >= 70 else "err"
        self.add(("ok", "  ✓ "), ("text", f"Cloned {url}"))
        self.add(
            ("dim", "    pages "),
            ("info", str(result["pages"])),
            ("dim", " · assets "),
            ("info", str(result["assets"])),
            ("dim", " · size "),
            ("info", human(result["bytes"])),
            ("dim", " · offline "),
            (score_style, f"{score}%"),
        )
        if result.get("missing"):
            self.add(("dim", f"    missing {len(result['missing'])} asset(s) — online URLs kept"))
        for warning in result.get("warnings", []):
            self.add(("warn", f"    ⚠ {warning}"))
        self.add(("dim", "    output "), ("path", os.path.abspath(out_dir)))
        if single_ok:
            self.add(("dim", "    single "),
                     ("path", os.path.join(os.path.abspath(out_dir), "single.html")))
        self.add(("dim", "    zip    "), ("path", result["zip"]))
        self.exit_code = 0

    def _finish_err(self, url, exc):
        if self.line_mode and self._line_dirty:
            sys.stdout.write("\r\x1b[2K")
            self._line_dirty = False
        self.add(("err", "  ✗ "), ("err", f"clone failed: {exc}"))
        self.exit_code = 1

    # ------------------------------------------------------------ commands
    def handle(self, text):
        raw = (text or "").strip()
        if not raw:
            return None
        low = raw.lower()

        if low in ("exit", "quit", "q"):
            if self.busy:
                self.add(("warn", "  ⧗ still cloning — wait for it to finish"))
                return None
            return "exit"
        if low in ("help", "?"):
            for entry in HELP_LINES:
                self.add(entry)
            return None
        if low in ("clear", "cls"):
            self.clear()
            return None
        if low == "web":
            self.add(("dim", "  web ui  "), ("info", "http://localhost:5000"),
                     ("dim", "   (start it with: cloner)"))
            return None
        if low == "status":
            o = self.opts
            state = "cloning…" if self.busy else "ready"
            self.add(("dim", "  options  "),
                     ("text", f"depth {o['depth']} · max {o['max_pages']} pages · "
                              f"render {'on' if o['render'] else 'off'} · "
                              f"robots {'on' if o['robots'] else 'off'}"),
                     ("dim", f"   state  {state}"))
            return None

        head, _, rest = raw.partition(" ")
        head_l = head.lower()

        if head_l == "depth":
            try:
                value = max(1, min(5, int(rest.strip())))
            except ValueError:
                self.add(("err", "  usage: depth 1-5"))
                return None
            self.opts["depth"] = value
            self.add(("dim", f"  depth → {value}"))
            return None
        if head_l == "max":
            try:
                value = max(1, min(500, int(rest.strip())))
            except ValueError:
                self.add(("err", "  usage: max <pages 1-500>"))
                return None
            self.opts["max_pages"] = value
            self.add(("dim", f"  max pages → {value}"))
            return None
        if head_l in ("render", "robots"):
            val = rest.strip().lower()
            if val in ("on", "off"):
                new = val == "on"
            elif val in ("toggle", ""):
                new = not self.opts[head_l]
            else:
                self.add(("err", f"  usage: {head_l} on|off"))
                return None
            self.opts[head_l] = new
            self.add(("dim", f"  {head_l} → {'on' if new else 'off'}"))
            return None

        if head_l == "clone" and rest.strip():
            self.start_clone(rest.strip())
            return None

        if " " in raw:
            self.add(("err", f"  unknown command: {clip(head, 30)}"),
                     ("dim", "   type help for commands"))
            return None
        if raw.startswith(("http://", "https://")) or "." in raw:
            self.start_clone(raw)
            return None
        self.add(("err", f"  that doesn't look like a URL: {clip(raw, 40)}"),
                 ("dim", "   try: example.com   (or type help)"))
        return None

    # ------------------------------------------------------------ TUI mode
    def run_tui(self):
        try:
            from prompt_toolkit import Application
            from prompt_toolkit.buffer import Buffer
            from prompt_toolkit.filters import Condition
            from prompt_toolkit.key_binding import KeyBindings
            from prompt_toolkit.layout import HSplit, Layout, Window
            from prompt_toolkit.layout.containers import ConditionalContainer
            from prompt_toolkit.layout.controls import (BufferControl,
                                                        FormattedTextControl,
                                                        UIContent)
            from prompt_toolkit.styles import Style
        except ImportError:
            print("prompt_toolkit is not installed → falling back to line mode")
            return self.run_line()

        shell = self

        class WidthText(FormattedTextControl):
            def __init__(self, render_fn):
                self._fn = render_fn
                super().__init__(text=self._noop, focusable=False, show_cursor=False)

            @staticmethod
            def _noop():
                return []

            def create_content(self, width, height):
                try:
                    frags = self._fn(width) or []
                except Exception:
                    frags = []
                return UIContent(get_line=lambda i: frags if i == 0 else [],
                                 line_count=1)

        class TailLog(FormattedTextControl):
            def __init__(self, sh):
                self.sh = sh
                super().__init__(text=self._noop, focusable=False, show_cursor=False)

            @staticmethod
            def _noop():
                return []

            def create_content(self, width, height):
                lines = self.sh.log
                # height is None during preferred-height queries
                h = max(1, height) if height else 1
                offset = min(self.sh.view_offset, max(0, len(lines) - 1))
                end = len(lines) - offset
                start = max(0, end - h)
                visible = lines[start:end]
                return UIContent(
                    get_line=lambda i: visible[i] if 0 <= i < len(visible) else [],
                    line_count=len(visible))

        def row(left, right, width):
            gap = width - vis_len(left) - vis_len(right)
            if gap < 1:
                return left
            return left + [(F(C_BORDER), " " * gap)] + right

        def header_render(width):
            left = [(F(C_ACCENT, True), " MaxWeb"), (F(C_ACCENT2, True), "Cloner"),
                    (F(C_DIM), f"  v{VERSION}")]
            if shell.busy:
                spin = SPINNER[int(time.time() * 10) % len(SPINNER)]
                right = [(F(C_ACCENT), f"{spin} cloning")]
            else:
                right = [(F(C_OK), "● ready")]
            return row(left, right, width)

        def input_top_render(width):
            title = "input · cloning… " if shell.busy else 'input · type a url or "help" '
            w = max(12, width)
            fill = max(0, w - 5 - len(title))
            return [(F(C_BORDER_FOCUS), "╭─ "), (F(C_ACCENT, True), title),
                    (F(C_BORDER_FOCUS), " " + "─" * fill + "╮")]

        def input_bottom_render(width):
            w = max(12, width)
            return [(F(C_BORDER_FOCUS), "╰" + "─" * max(0, w - 2) + "╯")]

        def status_render(width):
            s = shell.status or {}
            spin = SPINNER[int(time.time() * 10) % len(SPINNER)]
            left = [
                (F(C_ACCENT), f" {spin} "),
                (F(C_TEXT), str(s.get("phase", "working"))),
                (F(C_DIM), "  ·  "),
                (F(C_TEXT), f"{s.get('pages_done', 0)} pages"),
                (F(C_DIM), " · "),
                (F(C_TEXT), f"{s.get('assets_done', 0)} assets"),
                (F(C_DIM), " · "),
                (F(C_TEXT), human(s.get("bytes", 0))),
            ]
            if s.get("missing"):
                left += [(F(C_DIM), " · "), (F(C_WARN), f"{s['missing']} missing")]
            right = [(F(C_DIM), clip(s.get("current") or "", max(10, width // 2)))]
            return row(left, right, width)

        def toolbar_render(width):
            left = [(F(C_DIM), " enter"), (F(C_KEY), " clone"),
                    (F(C_DIM), "  tab"), (F(C_KEY), " depth"),
                    (F(C_DIM), "  ctrl+r"), (F(C_KEY), " render"),
                    (F(C_DIM), "  ctrl+l"), (F(C_KEY), " clear"),
                    (F(C_DIM), "  ctrl+d"), (F(C_KEY), " exit")]
            o = shell.opts
            right = [
                (F(C_DIM), "depth "), (F(C_ACCENT), str(o["depth"])),
                (F(C_DIM), " · render "),
                (F(C_OK if o["render"] else C_DIM), "on" if o["render"] else "off"),
                (F(C_DIM), " · robots "),
                (F(C_OK if o["robots"] else C_DIM), "on" if o["robots"] else "off"),
            ]
            return row(left, right, width)

        try:
            self.buffer = Buffer(multiline=False, prompt="❯ ")
        except TypeError:
            self.buffer = Buffer(multiline=False)

        kb = KeyBindings()

        @kb.add("enter")
        def _enter(event):
            text = shell.buffer.text
            if not text.strip():
                return
            shell.buffer.reset()
            shell.view_offset = 0
            try:
                shell.app.layout.focus(shell.input_window)
            except Exception:
                pass
            if shell.handle(text) == "exit":
                event.app.exit()

        @kb.add("c-d")
        @kb.add("c-c")
        def _exit(event):
            if shell.busy:
                shell.add(("warn", "  ⧗ cloning in progress — ctrl+d ignored"))
                return
            event.app.exit()

        @kb.add("tab")
        def _tab(event):
            depths = [1, 2, 3]
            cur = shell.opts["depth"]
            shell.opts["depth"] = depths[(depths.index(cur) + 1) % len(depths)] if cur in depths else 1
            shell._request_redraw()

        @kb.add("c-r")
        def _render_toggle(event):
            shell.opts["render"] = not shell.opts["render"]
            shell._request_redraw()

        @kb.add("c-l")
        def _clear(event):
            shell.clear()

        @kb.add("pageup")
        def _pgup(event):
            shell.view_offset = min(max(0, len(shell.log) - 1), shell.view_offset + 12)

        @kb.add("pagedown")
        def _pgdn(event):
            shell.view_offset = max(0, shell.view_offset - 12)

        input_win = Window(content=BufferControl(buffer=self.buffer), height=1)
        self.input_window = input_win

        root = HSplit([
            Window(content=WidthText(header_render), height=1),
            Window(content=TailLog(self), wrap_lines=False),
            Window(height=1, char="─", style=F(C_BORDER)),
            ConditionalContainer(
                Window(content=WidthText(status_render), height=1),
                filter=Condition(lambda: shell.busy)),
            Window(content=WidthText(input_top_render), height=1),
            input_win,
            ConditionalContainer(
                Window(content=WidthText(lambda w: [
                    (F(C_DIM), "  paste a website URL and press enter  —  e.g. example.com")]),
                    height=1),
                filter=Condition(lambda: not shell.buffer.text)),
            Window(content=WidthText(input_bottom_render), height=1),
            Window(content=WidthText(toolbar_render), height=1),
        ])

        layout = Layout(root)
        layout.focus(input_win)

        style = Style.from_dict({"": f"bg:{C_BG} fg:{C_TEXT}",
                                 "prompt": F(C_ACCENT, True)})
        app_kwargs = dict(layout=layout, key_bindings=kb, style=style,
                          full_screen=True, mouse_support=True)
        try:
            self.app = Application(refresh_interval=0.2, **app_kwargs)
        except TypeError:
            self.app = Application(**app_kwargs)

        self._welcome()
        try:
            self.app.run()
        except KeyboardInterrupt:
            pass
        return 0

    def _welcome(self):
        self.add(("ok", "  ● "), ("text", "ready — website cloning for your terminal"),
                 ("dim", f"   {NAME} v{VERSION}"))
        self.add(("dim", "  paste a URL in the input field below and press enter · type "),
                 ("key", "help"), ("dim", " for all commands"))
        self.add(("dim", ""))

    # ----------------------------------------------------------- line mode
    def run_line(self, url=None):
        self.line_mode = True
        cols = shutil.get_terminal_size((72, 24)).columns
        width = min(72, max(40, cols))
        tty = sys.stdout.isatty()

        def p(style, text):
            return (ANSI.get(style, "") + text + ANSI_RESET) if tty else text

        bar = "─" * max(10, width - 2)
        left = f" {NAME} "
        right = f"v{VERSION} "
        pad = max(1, width - 2 - len(left) - len(right))
        print(p("brand", "╭" + bar + "╮"))
        print("│" + p("brand2", left) + " " * pad + p("dim", right) + "│")
        print(p("brand", "╰" + bar + "╯"))
        print(p("dim", "  type a URL to clone · help for commands · exit to quit"))
        print()

        if url:
            self.handle(url)
            if self._thread is not None:
                self._thread.join()
            return self.exit_code

        prompt = (ANSI.get("brand", "") + "❯ " + ANSI_RESET) if tty else "> "
        while True:
            try:
                line = input(prompt)
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if self.handle(line) == "exit":
                break
        return self.exit_code


def main(argv):
    if "--help" in argv or "-h" in argv:
        print(f"{NAME} v{VERSION} — an OpenCode-style TUI for the Website Cloner\n")
        print("  maxweb                     open the interactive TUI")
        print("  maxweb --line              plain line-mode REPL")
        print("  maxweb <url>               clone one site and exit")
        return 0

    url_arg = next((a for a in argv if not a.startswith("-")), None)
    force_tui = "--tui" in argv
    shell = MaxWeb()

    if url_arg and not force_tui:
        return shell.run_line(url_arg)
    if "--line" in argv or not sys.stdin.isatty() or not sys.stdout.isatty():
        return shell.run_line()
    return shell.run_tui()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
