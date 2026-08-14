"""macOS application detection (app bundles, package receipts)."""

from __future__ import annotations

import os
import plistlib
from pathlib import Path

from ..models import Application, DetectionSpec, InstalledApp
from .base import DetectionContext, register


def read_plist(path: str) -> dict:
    """Read an ``Info.plist`` (binary or XML). Returns ``{}`` when unreadable."""
    try:
        with open(path, "rb") as handle:
            data = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


@register
class MacAppBundleDetector:
    """Detect a ``.app`` bundle and read its version from ``Info.plist``.

    Options:
        ``bundle``: bundle name, e.g. ``Firefox.app`` (defaults to
            ``<name>.app``).
        ``paths``: explicit absolute paths to check instead of the standard
            application directories.
        ``version_key``: plist key to read (default
            ``CFBundleShortVersionString``, falling back to ``CFBundleVersion``).
    """

    method = "macos_app_bundle"

    def detect(
        self, app: Application, spec: DetectionSpec, context: DetectionContext
    ) -> InstalledApp:
        exists = context.path_exists or (lambda p: Path(p).exists())
        reader = context.read_plist or read_plist

        candidates: list[str] = []
        explicit = spec.options.get("paths")
        if isinstance(explicit, str):
            explicit = [explicit]
        if explicit:
            candidates.extend(str(p) for p in explicit)
        else:
            bundle = spec.options.get("bundle") or f"{app.name}.app"
            for directory in context.application_dirs:
                candidates.append(str(Path(os.path.expanduser(directory)) / bundle))

        version_key = spec.options.get("version_key") or "CFBundleShortVersionString"
        for candidate in candidates:
            expanded = os.path.expanduser(candidate)
            if not exists(expanded):
                continue
            info = reader(str(Path(expanded) / "Contents" / "Info.plist"))
            version = info.get(version_key) or info.get("CFBundleVersion")
            return InstalledApp(
                installed=True,
                version=str(version).strip() if version else None,
                location=expanded,
                method=self.method,
            )
        return InstalledApp(
            installed=False,
            method=self.method,
            detail="no matching application bundle found",
        )


@register
class MacPkgReceiptDetector:
    """Detect via ``pkgutil --pkg-info <bundle-id>``.

    Options:
        ``package_id``: the receipt id, e.g. ``com.microsoft.VSCode``.
    """

    method = "macos_pkg_receipt"

    def detect(
        self, app: Application, spec: DetectionSpec, context: DetectionContext
    ) -> InstalledApp:
        package_id = spec.options.get("package_id")
        if not package_id:
            return InstalledApp(
                installed=False,
                method=self.method,
                detail="detection.package_id is not configured",
            )
        result = context.runner.run(
            ["/usr/sbin/pkgutil", "--pkg-info", str(package_id)],
            timeout=30,
            check_executable=False,
        )
        if result.exit_code != 0:
            return InstalledApp(
                installed=False,
                method=self.method,
                detail=f"no receipt for {package_id}",
            )
        version, location = None, None
        for line in result.stdout.splitlines():
            key, _, value = line.partition(":")
            key, value = key.strip().lower(), value.strip()
            if key == "version":
                version = value
            elif key in ("location", "volume"):
                location = value or location
        return InstalledApp(
            installed=True,
            version=version,
            location=location,
            method=self.method,
            detail=str(package_id),
        )
