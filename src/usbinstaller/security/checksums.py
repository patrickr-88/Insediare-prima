"""SHA-256 checksum storage and verification.

The checksum store lives at ``checksums/checksums.json`` in the repository and
is keyed by the repository-relative installer path (POSIX separators), which is
unambiguous even when two applications ship a file called ``setup.exe``.
A bare file-name key is accepted as a fallback so hand-written stores keep
working.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from ..errors import ChecksumError, ConfigParseError

CHUNK_SIZE = 1024 * 1024


def sha256_file(path: Path, chunk_size: int = CHUNK_SIZE) -> str:
    """Return the lowercase hex SHA-256 digest of *path*."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_tree(path: Path) -> str:
    """Return a stable digest for a directory (used for ``.app`` bundles).

    Files are hashed in sorted relative-path order, and each path is mixed into
    the digest so that renaming a file changes the result.
    """
    digest = hashlib.sha256()
    for file in sorted(p for p in path.rglob("*") if p.is_file()):
        rel = file.relative_to(path).as_posix()
        digest.update(rel.encode("utf-8") + b"\0")
        digest.update(sha256_file(file).encode("ascii") + b"\0")
    return digest.hexdigest()


def digest_of(path: Path) -> str:
    """Digest a file or a directory bundle."""
    return sha256_tree(path) if path.is_dir() else sha256_file(path)


@dataclass(frozen=True)
class ChecksumResult:
    """Outcome of verifying one installer."""

    path: str
    status: str  # "ok" | "mismatch" | "missing_entry" | "missing_file"
    expected: str | None = None
    actual: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def message(self) -> str:
        return {
            "ok": f"{self.path}: checksum verified",
            "mismatch": (
                f"{self.path}: checksum MISMATCH "
                f"(expected {self.expected}, got {self.actual})"
            ),
            "missing_entry": f"{self.path}: no checksum recorded",
            "missing_file": f"{self.path}: file not found",
        }[self.status]


class ChecksumStore:
    """Reads, verifies and writes ``checksums.json``."""

    def __init__(self, entries: dict[str, dict[str, str]] | None = None) -> None:
        self._entries: dict[str, dict[str, str]] = dict(entries or {})

    # -- construction ----------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> ChecksumStore:
        """Load a store, returning an empty store when the file is absent."""
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigParseError(f"{path} is not valid JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigParseError(f"{path} must contain a JSON object")
        entries: dict[str, dict[str, str]] = {}
        for key, value in raw.items():
            if isinstance(value, str):
                entries[key] = {"sha256": value.lower()}
            elif isinstance(value, dict) and isinstance(value.get("sha256"), str):
                entry = dict(value)
                entry["sha256"] = str(entry["sha256"]).lower()
                entries[key] = entry
            else:
                raise ConfigParseError(
                    f"{path}: entry {key!r} must be a hex string or an object "
                    "with a 'sha256' field"
                )
        return cls(entries)

    # -- queries ---------------------------------------------------------

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: str) -> bool:
        return self.expected_for(key) is not None

    @property
    def entries(self) -> dict[str, dict[str, str]]:
        return dict(self._entries)

    def expected_for(self, relative_path: str) -> str | None:
        """Look up a digest by relative path, falling back to the bare name."""
        key = PurePosixPath(str(relative_path).replace("\\", "/")).as_posix()
        entry = self._entries.get(key)
        if entry is None:
            entry = self._entries.get(PurePosixPath(key).name)
        if entry is None:
            return None
        value = entry.get("sha256")
        return value.lower() if isinstance(value, str) else None

    def verify(self, relative_path: str, absolute_path: Path) -> ChecksumResult:
        """Verify one installer against the store."""
        expected = self.expected_for(relative_path)
        if not absolute_path.exists():
            return ChecksumResult(relative_path, "missing_file", expected, None)
        if expected is None:
            return ChecksumResult(relative_path, "missing_entry", None, None)
        actual = digest_of(absolute_path)
        status = "ok" if actual == expected else "mismatch"
        return ChecksumResult(relative_path, status, expected, actual)

    def verify_strict(self, relative_path: str, absolute_path: Path) -> ChecksumResult:
        """Verify and raise :class:`ChecksumError` on anything but success."""
        result = self.verify(relative_path, absolute_path)
        if not result.ok:
            raise ChecksumError(result.message)
        return result

    # -- mutation --------------------------------------------------------

    def update(self, relative_path: str, absolute_path: Path) -> str:
        """Record (or refresh) the digest for one installer and return it."""
        key = PurePosixPath(str(relative_path).replace("\\", "/")).as_posix()
        digest = digest_of(absolute_path)
        entry: dict[str, Any] = dict(self._entries.get(key, {}))
        entry["sha256"] = digest
        if absolute_path.is_file():
            entry["size"] = absolute_path.stat().st_size
        self._entries[key] = entry
        return digest

    def prune(self, keep: Iterable[str]) -> list[str]:
        """Drop entries not in *keep*; returns the removed keys."""
        keep_set = {PurePosixPath(str(k).replace("\\", "/")).as_posix() for k in keep}
        removed = [k for k in self._entries if k not in keep_set]
        for key in removed:
            del self._entries[key]
        return removed

    def save(self, path: Path) -> None:
        """Write the store atomically, sorted for reviewable diffs."""
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            dict(sorted(self._entries.items())), indent=2, sort_keys=True
        )
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(payload + "\n", encoding="utf-8")
        tmp.replace(path)

    def __iter__(self) -> Iterator[str]:
        return iter(self._entries)
