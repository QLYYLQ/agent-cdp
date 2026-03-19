"""Shared ANSI output helpers for demo scripts."""

from typing import Any

BOLD = '\033[1m'
GREEN = '\033[92m'
RED = '\033[91m'
YELLOW = '\033[93m'
CYAN = '\033[96m'
MAGENTA = '\033[95m'
DIM = '\033[2m'
RESET = '\033[0m'

_print = print


def pr(*args: Any, **kwargs: Any) -> None:
    _print(*args, **kwargs, flush=True)


def banner(text: str, width: int = 65) -> None:
    pr(f'\n{BOLD}{CYAN}{"═" * width}')
    pr(f'  {text}')
    pr(f'{"═" * width}{RESET}\n')


def phase(num: int | str, text: str) -> None:
    if isinstance(num, int):
        pr(f'\n{BOLD}{YELLOW}Phase {num}: {text}{RESET}')
    else:
        pr(f'\n{BOLD}{YELLOW}── {text} {"─" * max(1, 55 - len(text))}{RESET}')


def ok(text: str) -> None:
    pr(f'  {GREEN}✓{RESET} {text}')


def fail(text: str) -> None:
    pr(f'  {RED}✗{RESET} {text}')


def info(text: str) -> None:
    pr(f'  {DIM}→ {text}{RESET}')


def warn(text: str) -> None:
    pr(f'  {YELLOW}!{RESET} {text}')


def trace(entries: list[str]) -> None:
    for entry in entries:
        pr(f'    {DIM}{entry}{RESET}')


def tab_label(name: str) -> str:
    return f'{MAGENTA}[{name}]{RESET}'


def fmt_us(us: float) -> str:
    """Format microseconds to human-readable."""
    if us < 1000:
        return f'{us:.1f}µs'
    elif us < 1_000_000:
        return f'{us / 1000:.2f}ms'
    else:
        return f'{us / 1_000_000:.2f}s'
