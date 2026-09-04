from __future__ import annotations

import sys
from datetime import datetime
from typing import Iterable


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(message: str, level: str = "INFO") -> None:
    """Print a timestamped CLI log line immediately."""
    print(f"[{_stamp()}] [{level}] {message}", flush=True)


def info(message: str) -> None:
    log(message, "INFO")


def ok(message: str) -> None:
    log(message, " OK ")


def warn(message: str) -> None:
    log(message, "WARN")


def error(message: str) -> None:
    log(message, "ERROR")


def section(title: str) -> None:
    print("\n" + "=" * 72, flush=True)
    print(title, flush=True)
    print("=" * 72, flush=True)


def kv(label: str, value: object, indent: int = 2) -> None:
    print(f"{' ' * indent}{label}: {value}", flush=True)


def command_result(command: Iterable[str], returncode: int, stderr: str = "") -> None:
    """Report an external command result without hiding its error output."""
    if returncode == 0:
        ok("外部コマンド完了")
        return
    error(f"外部コマンド失敗 (exit={returncode})")
    if stderr.strip():
        print(stderr.rstrip(), file=sys.stderr, flush=True)
