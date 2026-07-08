"""
Terminal UI utilities — colors, progress bar, spinner.

Pure stdlib + rich (optional). Falls back gracefully when rich is not installed.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from typing import Optional

try:
    import rich.console
    import rich.progress
    import rich.panel
    import rich.text
    _HAS_RICH = True
except ImportError:
    _HAS_RICH = False


# ---------------------------------------------------------------------------
# ANSI colors (works even without rich)
# ---------------------------------------------------------------------------

class _C:
    """ANSI color codes — reset on Windows if needed."""
    RST = "\033[0m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    DIM = "\033[2m"


def _supports_color() -> bool:
    """Check if terminal supports ANSI color."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
        return False
    # Windows 10+ supports ANSI via VT processing
    return True


_USE_COLOR = _supports_color()


def c(text: str, color: str) -> str:
    """Wrap text in ANSI color if terminal supports it."""
    if not _USE_COLOR:
        return text
    codes = {
        "red": _C.RED, "green": _C.GREEN, "yellow": _C.YELLOW,
        "blue": _C.BLUE, "cyan": _C.CYAN, "bold": _C.BOLD, "dim": _C.DIM,
    }
    code = codes.get(color, "")
    return f"{code}{text}{_C.RST}"


# ---------------------------------------------------------------------------
# Convenience print helpers
# ---------------------------------------------------------------------------

def ok(msg: str) -> None:
    print(f"  {c('✓', 'green')} {msg}")


def fail(msg: str) -> None:
    print(f"  {c('✗', 'red')} {msg}")


def warn(msg: str) -> None:
    print(f"  {c('⚠', 'yellow')} {msg}")


def info(msg: str) -> None:
    print(f"  {c('•', 'cyan')} {msg}")


def header(title: str) -> None:
    print(f"\n{c(f'=== {title} ===', 'bold')}")


def dim(msg: str) -> None:
    print(f"  {c(msg, 'dim')}")


def timestamp() -> str:
    return c(datetime.now().strftime("%H:%M:%S"), "dim")


# ---------------------------------------------------------------------------
# Progress bar
# ---------------------------------------------------------------------------

class ProgressBar:
    """Simple progress bar that works with or without rich."""

    def __init__(self, total: int, description: str = "", width: int = 40):
        self.total = total
        self.current = 0
        self.description = description
        self.width = width
        self._start = time.time()

    def update(self, n: int = 1) -> None:
        self.current = min(self.current + n, self.total)
        self._render()

    def _render(self) -> None:
        pct = self.current / max(self.total, 1)
        filled = int(self.width * pct)
        bar = "█" * filled + "░" * (self.width - filled)
        elapsed = time.time() - self._start
        line = f"\r {self.description} [{bar}] {self.current}/{self.total} {elapsed:.1f}s"
        sys.stdout.write(line)
        sys.stdout.flush()
        if self.current >= self.total:
            sys.stdout.write("\n")

    def finish(self) -> None:
        if self.current < self.total:
            self.current = self.total
            self._render()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.finish()


# ---------------------------------------------------------------------------
# Spinner
# ---------------------------------------------------------------------------

_SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


class Spinner:
    """Terminal spinner for long-running operations."""

    def __init__(self, message: str = "Working", interval: float = 0.08):
        self.message = message
        self.interval = interval
        self._frame = 0
        self._running = False
        self._thread = None

    def _animate(self) -> None:
        import threading
        while self._running:
            frame = _SPINNER_FRAMES[self._frame % len(_SPINNER_FRAMES)]
            line = f" {frame} {self.message} "
            sys.stdout.write(f"\r{line}")
            sys.stdout.flush()
            self._frame += 1
            time.sleep(self.interval)

    def start(self) -> None:
        self._running = True
        self._thread = __import__("threading").Thread(target=self._animate, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=1)
        sys.stdout.write("\r" + " " * 60 + "\r")
        sys.stdout.flush()

    def update(self, message: str) -> None:
        self.message = message

    def succeed(self, msg: str = "") -> None:
        self.stop()
        ok(msg or "Done")

    def fail(self, msg: str = "") -> None:
        self.stop()
        fail(msg or "Failed")

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
        return False


# ---------------------------------------------------------------------------
# Rich-based progress (used when available)
# ---------------------------------------------------------------------------

def create_rich_progress(*tasks, description: str = "Processing"):
    """Create a rich progress bar if available, else fallback to ProgressBar."""
    if not _HAS_RICH:
        return None
    return rich.progress.Progress(
        rich.progress.TextColumn(description),
        rich.progress.BarColumn(),
        rich.progress.TaskProgressColumn(),
        rich.progress.TimeRemainingColumn(),
    )


def print_panel(title: str, body: str) -> None:
    """Print a bordered panel."""
    if _HAS_RICH:
        print(rich.panel.Panel(body, title=c(title, "bold"), border_style="bold blue"))
    else:
        print(f"\n--- {title} ---")
        print(body)
        print("-" * len(f"--- {title} ---"))


# ---------------------------------------------------------------------------
# Styled text helper (used by CLI modules)
# ---------------------------------------------------------------------------

class _Styled:
    """Convenience styled text methods used by diff, history, init CLI modules."""

    @staticmethod
    def title(text: str) -> str:
        return c(f"=== {text} ===", "bold")

    @staticmethod
    def status(text: str) -> str:
        return c(f"--- {text} ---", "cyan")

    @staticmethod
    def error(text: str) -> str:
        return c(str(text), "red")

    @staticmethod
    def warning(text: str) -> str:
        return c(str(text), "yellow")

    @staticmethod
    def stable(text: str) -> str:
        return c(str(text), "green")

    @staticmethod
    def info(text: str) -> str:
        return c(str(text), "cyan")

    @staticmethod
    def ok(text: str) -> str:
        return c(f"[OK] {text}", "green")


styled = _Styled()


def progress_bar(total: int, description: str = "") -> ProgressBar:
    """Create a progress bar instance (backward-compatible shortcut)."""
    return ProgressBar(total=total, description=description)


def print_table(rows: list[list[str]], headers: list[str]) -> None:
    """Print a simple table."""
    if _HAS_RICH:
        table = rich.table.Table(show_header=True, header_style="bold cyan")
        for h in headers:
            table.add_column(h, style="white")
        for row in rows:
            table.add_row(*row)
        print(table)
    else:
        widths = [max(len(str(r)), len(h)) for r, h in zip(rows[0] if rows else [], headers)]
        header_line = " | ".join(h.ljust(w) for h, w in zip(headers, widths))
        sep = "-+-".join("-" * w for w in widths)
        print(f"\n{header_line}\n{sep}")
        for row in rows:
            print(" | ".join(str(c).ljust(w) for c, w in zip(row, widths)))
