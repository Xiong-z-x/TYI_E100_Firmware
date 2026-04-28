#!/usr/bin/env python3
import os
import pathlib
import threading
import time
from typing import Optional, Tuple

DEFAULT_TIMEBASE_FILE = "/tmp/tyi_timebase.env"
_SUPPORTED_MODES = {"monotonic_raw_internal", "monotonic_raw_epoch"}
_LOCK = threading.Lock()
_CACHE_PATH: Optional[pathlib.Path] = None
_CACHE_MTIME_NS: Optional[int] = None
_CACHE_ANCHOR: Optional[Tuple[int, int]] = None


def monotonic_raw_now_ns() -> int:
    clock_id = getattr(time, "CLOCK_MONOTONIC_RAW", None)
    if clock_id is not None:
        return time.clock_gettime_ns(clock_id)
    return time.monotonic_ns()


def _parse_anchor(path: pathlib.Path) -> Optional[Tuple[int, int]]:
    values = {}
    for raw_line in path.read_text(encoding="ascii").splitlines():
        line = raw_line.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()

    mode = values.get("TYI_TIMEBASE_MODE", "monotonic_raw_internal")
    if mode not in _SUPPORTED_MODES:
        return None

    anchor_value = values.get("INTERNAL_ANCHOR_NS") or values.get("UNIX_ANCHOR_NS")
    mono_value = values.get("MONOTONIC_RAW_ANCHOR_NS")
    if not anchor_value or not mono_value:
        return None
    return int(anchor_value), int(mono_value)


def _load_anchor() -> Optional[Tuple[int, int]]:
    global _CACHE_ANCHOR, _CACHE_MTIME_NS, _CACHE_PATH
    path = pathlib.Path(os.environ.get("TYI_TIMEBASE_FILE", DEFAULT_TIMEBASE_FILE))
    try:
        stat = path.stat()
    except OSError:
        return _CACHE_ANCHOR

    with _LOCK:
        if _CACHE_PATH == path and _CACHE_MTIME_NS == stat.st_mtime_ns:
            return _CACHE_ANCHOR
        try:
            anchor = _parse_anchor(path)
        except Exception:
            anchor = None
        _CACHE_PATH = path
        _CACHE_MTIME_NS = stat.st_mtime_ns
        _CACHE_ANCHOR = anchor
        return anchor


def now_ns() -> int:
    anchor = _load_anchor()
    current_mono_ns = monotonic_raw_now_ns()
    if anchor is None:
        return current_mono_ns
    internal_anchor_ns, mono_anchor_ns = anchor
    return max(0, internal_anchor_ns + (current_mono_ns - mono_anchor_ns))


def now_sec() -> float:
    return float(now_ns()) / 1_000_000_000.0


def now_ms() -> int:
    return int(round(float(now_ns()) / 1_000_000.0))
