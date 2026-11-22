"""Input/data version labels for traceability (FR-08).

A label names the source and pins its exact content, for example
``fraudTest.csv@sha256:1a2b3c4d5e6f``. The simulator sends it in the
``X-Data-Version`` header; it is stored on every transaction row.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

MAX_LENGTH = 200
_UNSAFE = re.compile(r"[^A-Za-z0-9._:@/+#=-]")
_cache: dict[tuple[str, int, int], str] = {}


def normalize_data_version(value: str | None) -> str | None:
    """Keep a short, log-safe label; empty or missing becomes ``None``."""
    if value is None:
        return None
    cleaned = _UNSAFE.sub("_", str(value).strip())[:MAX_LENGTH]
    return cleaned or None


def file_data_version(path: str | Path, digest_chars: int = 12) -> str:
    """``<file name>@sha256:<prefix>``; cached until the file changes."""
    file = Path(path)
    stat = file.stat()
    key = (str(file.resolve()), stat.st_mtime_ns, stat.st_size)
    if key not in _cache:
        digest = hashlib.sha256()
        with file.open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        _cache[key] = f"{file.name}@sha256:{digest.hexdigest()[:digest_chars]}"
    return _cache[key]
