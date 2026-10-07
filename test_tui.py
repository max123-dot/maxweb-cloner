"""Tests for the MaxWeb Cloner terminal UI (TUI + line mode).

Drives the real full-screen TUI through a pseudo-terminal (pty), exactly the
way a human would type into it.
"""
import fcntl
import os
import pty
import select
import struct
import subprocess
import sys
import tempfile
import termios
import time

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "maxweb.py")
PY = sys.executable

_results = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    _results.append((name, bool(condition)))
    line = f"  [{status}] {name}"
    if detail:
        line += f" ({detail})" if condition else f" -> {detail}"
    print(line, flush=True)
    return bool(condition)


def _set_winsize(fd, rows=40, cols=120):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _read_until(fd, needles, timeout=15):
    if isinstance(needles, (bytes, str)):
        needles = [needles]
    needles = [n.encode() if isinstance(n, str) else n for n in needles]
    buf = b""
    end = time.time() + timeout
    while time.time() < end:
        ready, _, _ = select.select([fd], [], [], 0.3)
        if not ready:
            continue
        try:
            chunk = os.read(fd, 8192)
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
        if any(n in buf for n in needles):
            return buf
    return buf


def test_tui_boot_and_help():
    pid, fd = pty.fork()
    if pid == 0:
        os.environ["TERM"] = "xterm-256color"
        os.chdir(HERE)
        os.execv(PY, [PY, APP])
        os._exit(127)

    rc = None
    try:
        _set_winsize(fd)
        boot = _read_until(fd, b"MaxWeb", timeout=15)
        check("TUI boots and draws the MaxWeb header", b"MaxWeb" in boot and b"Cloner" in boot)
        check("TUI shows the boxed input field",
              b"input" in boot and (b"type a url" in boot or b"help" in boot),
              boot[-400:].decode("utf-8", "ignore").replace("\x1b", "<esc>")[:200])
        check("TUI shows toolbar key hints",
              b"enter" in boot and b"ctrl+d" in boot)

        os.write(fd, b"help\r")
        help_out = _read_until(fd, [b"commands", b"crawl depth"], timeout=10)
        check("help command renders inside the TUI", b"commands" in help_out and b"depth" in help_out)

        os.write(fd, b"depth 3\r")
        depth_out = _read_until(fd, b"depth \xe2\x86\x92", timeout=10)
        check("depth command updates options (depth → 3)", "depth → 3".encode() in depth_out)

        os.write(fd, b"\x04")  # ctrl+d
        end = time.time() + 10
        while time.time() < end:
            wpid, status = os.waitpid(pid, os.WNOHANG)
            if wpid == pid:
                rc = os.waitstatus_to_exitcode(status)
                break
            time.sleep(0.2)
        if rc is None:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
        check("ctrl+d exits the TUI cleanly (rc=0)", rc == 0, f"rc={rc}")
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass


def test_line_mode_repl():
    cwd = tempfile.mkdtemp(prefix="maxweb_repl_")
    proc = subprocess.run(
        [PY, APP, "--line"],
        input="help\nstatus\nexit\n",
        capture_output=True, text=True, timeout=40, cwd=cwd,
    )
    check("line-mode REPL runs and exits 0", proc.returncode == 0,
          f"rc={proc.returncode} err={proc.stderr.strip()[:200]}")
    check("line-mode banner shows MaxWeb Cloner", "MaxWeb Cloner" in proc.stdout)
    check("line-mode help works", "commands" in proc.stdout)
    check("line-mode status works", "options" in proc.stdout)


def test_one_shot_clone():
    cwd = tempfile.mkdtemp(prefix="maxweb_clone_")
    proc = subprocess.run(
        [PY, APP, "https://example.com"],
        capture_output=True, text=True, timeout=180, cwd=cwd,
    )
    check("one-shot clone (maxweb <url>) succeeds", proc.returncode == 0,
          f"rc={proc.returncode} out={proc.stdout.strip()[-300:]} "
          f"err={proc.stderr.strip()[-200:]}")
    check("one-shot clone reports the offline score", "offline" in proc.stdout)
    check("one-shot clone writes <host>/index.html",
          os.path.isfile(os.path.join(cwd, "example.com", "index.html")))
    check("one-shot clone builds single.html",
          os.path.isfile(os.path.join(cwd, "example.com", "single.html")))


def test_commands_on_path():
    proc = subprocess.run(["maxweb", "--help"], capture_output=True, text=True, timeout=30)
    check("`maxweb` command is installed on PATH",
          proc.returncode == 0 and "MaxWeb" in proc.stdout,
          f"rc={proc.returncode} out={proc.stdout[:120]}")
    proc = subprocess.run(["cloner", "tui", "--help"], capture_output=True, text=True, timeout=30)
    check("`cloner tui` launches MaxWeb Cloner",
          proc.returncode == 0 and "MaxWeb" in proc.stdout,
          f"rc={proc.returncode} out={proc.stdout[:120]}")


def main():
    print("\n🖥  Running MaxWeb Cloner TUI tests\n")
    print("Full-screen TUI (driven through a pty):")
    test_tui_boot_and_help()

    print("\nLine mode + commands:")
    test_line_mode_repl()
    test_commands_on_path()

    print("\nReal clone from the CLI:")
    test_one_shot_clone()

    passed = sum(1 for _, ok in _results if ok)
    total = len(_results)
    print(f"\n{'=' * 50}")
    print(f"Result: {passed}/{total} TUI tests passed")
    if passed != total:
        print("Failed tests:")
        for name, ok in _results:
            if not ok:
                print(f"  - {name}")
    print("=" * 50)
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
