"""Configurable version comparison.

Not every vendor follows semantic versioning, so the scheme used to compare an
installed version against the USB version is per-application configuration
(``version_scheme``). Available schemes:

``auto``
    Try ``numeric``; if either side has no digits, fall back to ``string``.
``semver``
    Strict ``MAJOR.MINOR.PATCH[-prerelease]``; pre-releases sort below the
    matching release. Malformed input is *unknown*, never "equal".
``numeric``
    Dotted/underscored numeric runs compared element-wise, ignoring any
    non-numeric decoration (``3.0.21-win64`` → ``(3, 0, 21)``).
``date``
    ``YYYY-MM-DD`` / ``YYYYMMDD`` / ``YYYY.MM.DD`` calendar versions.
``string``
    Case-insensitive equality only; ordering is never inferred.

An application may also supply ``version_pattern``, a regular expression whose
first capturing group extracts the comparable part of a messy version string
(e.g. ``"Adobe Acrobat (\\d+\\.\\d+)"``).
"""

from __future__ import annotations

import enum
import re
from collections.abc import Sequence

SCHEMES = ("auto", "semver", "numeric", "date", "string")

_NUM_RUN = re.compile(r"\d+")
_SEMVER = re.compile(
    r"^\s*v?(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?:-(?P<pre>[0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?\s*$"
)
_DATE = re.compile(r"^\s*(\d{4})[-._]?(\d{2})[-._]?(\d{2})\s*$")


class Comparison(str, enum.Enum):
    """Result of comparing an installed version with the USB version."""

    OLDER = "older"  # installed < usb  → upgrade available
    SAME = "same"
    NEWER = "newer"  # installed > usb  → never downgrade
    UNKNOWN = "unknown"  # not comparable


class VersionError(ValueError):
    """A version string could not be parsed under the requested scheme."""


def extract(value: str | None, pattern: str | None = None) -> str | None:
    """Apply an optional extraction *pattern* to *value*."""
    if value is None:
        return None
    text = str(value).strip()
    if not pattern:
        return text
    match = re.search(pattern, text)
    if not match:
        return None
    return (match.group(1) if match.groups() else match.group(0)).strip()


def _numeric_tuple(value: str) -> tuple[int, ...]:
    parts = _NUM_RUN.findall(value)
    if not parts:
        raise VersionError(f"no numeric component in {value!r}")
    return tuple(int(p) for p in parts)


def _pad(a: Sequence[int], b: Sequence[int]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    width = max(len(a), len(b))
    return (
        tuple(list(a) + [0] * (width - len(a))),
        tuple(list(b) + [0] * (width - len(b))),
    )


def _cmp(a: tuple[int, ...], b: tuple[int, ...]) -> Comparison:
    if a == b:
        return Comparison.SAME
    return Comparison.OLDER if a < b else Comparison.NEWER


def _semver_key(value: str) -> tuple[tuple[int, int, int], list[object]]:
    match = _SEMVER.match(value)
    if not match:
        raise VersionError(f"{value!r} is not a valid semantic version")
    core = (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
    )
    pre = match.group("pre")
    if pre is None:
        # A release outranks any of its pre-releases.
        return core, []
    identifiers: list[object] = []
    for part in pre.split("."):
        identifiers.append((0, int(part), "") if part.isdigit() else (1, 0, part))
    return core, identifiers


def compare(
    installed: str | None,
    available: str | None,
    scheme: str = "auto",
    pattern: str | None = None,
) -> Comparison:
    """Compare an *installed* version against the *available* (USB) version.

    Returns :attr:`Comparison.UNKNOWN` rather than guessing whenever either
    side is missing or cannot be parsed under *scheme*; callers treat unknown
    as "ask the technician", never as "safe to overwrite".
    """
    scheme = (scheme or "auto").strip().lower()
    if scheme not in SCHEMES:
        raise VersionError(f"unknown version scheme {scheme!r}")

    left = extract(installed, pattern)
    right = extract(available, pattern)
    if not left or not right:
        return Comparison.UNKNOWN

    if scheme == "string":
        return Comparison.SAME if left.lower() == right.lower() else Comparison.UNKNOWN

    if scheme == "auto":
        try:
            return _cmp(*_pad(_numeric_tuple(left), _numeric_tuple(right)))
        except VersionError:
            return (
                Comparison.SAME if left.lower() == right.lower() else Comparison.UNKNOWN
            )

    if scheme == "numeric":
        try:
            return _cmp(*_pad(_numeric_tuple(left), _numeric_tuple(right)))
        except VersionError:
            return Comparison.UNKNOWN

    if scheme == "date":
        lm, rm = _DATE.match(left), _DATE.match(right)
        if not lm or not rm:
            return Comparison.UNKNOWN
        return _cmp(
            tuple(int(g) for g in lm.groups()), tuple(int(g) for g in rm.groups())
        )

    # semver
    try:
        lcore, lpre = _semver_key(left)
        rcore, rpre = _semver_key(right)
    except VersionError:
        return Comparison.UNKNOWN
    if lcore != rcore:
        return Comparison.OLDER if lcore < rcore else Comparison.NEWER
    if lpre == rpre:
        return Comparison.SAME
    if not lpre:  # installed is a release, available is a pre-release
        return Comparison.NEWER
    if not rpre:
        return Comparison.OLDER
    return Comparison.OLDER if lpre < rpre else Comparison.NEWER


def is_valid(value: str | None, scheme: str = "auto") -> bool:
    """Return True when *value* parses under *scheme* (used by the validator)."""
    if value is None or not str(value).strip():
        return False
    text = str(value).strip()
    scheme = (scheme or "auto").lower()
    if scheme in ("string", "auto"):
        return True
    if scheme == "semver":
        return _SEMVER.match(text) is not None
    if scheme == "date":
        return _DATE.match(text) is not None
    if scheme == "numeric":
        return bool(_NUM_RUN.findall(text))
    return False
