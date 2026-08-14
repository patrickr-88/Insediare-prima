"""Path containment rules for the (untrusted) USB repository.

Every path that comes out of the configuration file is resolved through
:func:`resolve_within` before it is opened or executed. The rules are:

* paths in configuration are always repository-relative,
* absolute paths and drive letters are rejected,
* ``..`` segments that escape the repository root are rejected,
* symlinks are resolved *before* the containment check, so a symlink on the
  USB drive cannot point at ``C:\\Windows`` or ``/usr/bin``.
"""

from __future__ import annotations

import ntpath
import posixpath
from pathlib import Path, PurePosixPath, PureWindowsPath

from ..errors import PathTraversalError

#: Installer file extensions the engine is willing to execute, by installer type.
ALLOWED_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "exe": (".exe",),
    "msi": (".msi",),
    "msix": (".msix", ".appx", ".msixbundle", ".appxbundle"),
    "powershell": (".ps1",),
    "dmg": (".dmg",),
    "pkg": (".pkg", ".mpkg"),
    "app": (".app", ".zip"),
    "shell": (".sh", ".command"),
}


def is_suspicious(relative_path: str) -> str | None:
    """Return a reason string when *relative_path* is not a safe relative path."""
    if not relative_path or not relative_path.strip():
        return "path is empty"
    if "\x00" in relative_path:
        return "path contains a NUL byte"
    if ntpath.isabs(relative_path) or posixpath.isabs(relative_path):
        return "path is absolute"
    # Windows drive-relative ("C:foo") and UNC ("\\\\server\\share") paths.
    drive, _ = ntpath.splitdrive(relative_path)
    if drive:
        return "path contains a drive or UNC prefix"
    parts = PureWindowsPath(relative_path).parts
    if any(part == ".." for part in parts):
        return "path contains a '..' segment"
    return None


def normalise(relative_path: str) -> PurePosixPath:
    """Normalise a config path (which may use ``\\`` separators) to POSIX form."""
    parts = [p for p in PureWindowsPath(relative_path).parts if p not in (".", "")]
    return PurePosixPath(*parts)


def resolve_within(root: Path, relative_path: str) -> Path:
    """Resolve *relative_path* under *root*, refusing anything that escapes it.

    Raises:
        PathTraversalError: if the path is absolute, contains ``..`` segments, or
            resolves (after following symlinks) outside *root*.
    """
    reason = is_suspicious(relative_path)
    if reason is not None:
        raise PathTraversalError(f"Refusing unsafe path {relative_path!r}: {reason}")

    root_resolved = Path(root).resolve()
    candidate = (root_resolved / normalise(relative_path)).resolve()

    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise PathTraversalError(
            f"Refusing path {relative_path!r}: resolves outside the repository "
            f"root ({candidate} not under {root_resolved})"
        )
    return candidate


def extension_allowed(path: Path | str, installer_type: str) -> bool:
    """Return True when *path*'s extension is valid for *installer_type*."""
    allowed = ALLOWED_EXTENSIONS.get(installer_type.lower())
    if not allowed:
        return False
    name = str(path).lower()
    return any(name.endswith(ext) for ext in allowed)
