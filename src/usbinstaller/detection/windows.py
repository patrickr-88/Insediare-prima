"""Windows application detection (uninstall registry + filesystem probes)."""

from __future__ import annotations

import importlib
import os
import re
from pathlib import Path
from typing import Any

from ..models import Application, DetectionSpec, InstalledApp
from .base import DetectionContext, register

#: The three hives that between them list every per-machine and per-user
#: installation, including 32-bit apps on 64-bit Windows.
UNINSTALL_KEYS = (
    (r"HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    (r"HKLM", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    (r"HKCU", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
)


def read_uninstall_entries() -> list[dict[str, str]]:  # pragma: no cover - Windows only
    """Read every uninstall entry from the registry.

    Returns an empty list on non-Windows hosts so the detector degrades to
    "not detected" instead of raising.
    """
    # Imported dynamically: the module only exists on Windows, and importing it
    # this way keeps the whole file type-checkable on any host.
    try:
        winreg: Any = importlib.import_module("winreg")
    except ImportError:
        return []

    hives = {"HKLM": winreg.HKEY_LOCAL_MACHINE, "HKCU": winreg.HKEY_CURRENT_USER}
    entries: list[dict[str, str]] = []
    for hive_name, subkey in UNINSTALL_KEYS:
        try:
            with winreg.OpenKey(hives[hive_name], subkey) as key:
                count = winreg.QueryInfoKey(key)[0]
                for index in range(count):
                    try:
                        name = winreg.EnumKey(key, index)
                        with winreg.OpenKey(key, name) as sub:
                            entries.append(
                                {
                                    "key": name,
                                    "hive": hive_name,
                                    "DisplayName": _value(winreg, sub, "DisplayName"),
                                    "DisplayVersion": _value(
                                        winreg, sub, "DisplayVersion"
                                    ),
                                    "InstallLocation": _value(
                                        winreg, sub, "InstallLocation"
                                    ),
                                    "Publisher": _value(winreg, sub, "Publisher"),
                                }
                            )
                    except OSError:
                        continue
        except OSError:
            continue
    return entries


def _value(winreg: Any, key: Any, name: str) -> str:  # pragma: no cover - Windows only
    try:
        value, _ = winreg.QueryValueEx(key, name)
        return str(value)
    except OSError:
        return ""


@register
class WindowsRegistryDetector:
    """Match an application against the Windows uninstall registry.

    Options:
        ``display_name``: regular expression matched against ``DisplayName``.
        ``product_code``: exact uninstall key (MSI ProductCode GUID).
        ``publisher``: optional regular expression to disambiguate.
    """

    method = "windows_registry"

    def detect(
        self, app: Application, spec: DetectionSpec, context: DetectionContext
    ) -> InstalledApp:
        reader = context.registry_reader or read_uninstall_entries
        entries = reader()

        product_code = spec.options.get("product_code")
        name_pattern = spec.options.get("display_name") or re.escape(app.name)
        publisher_pattern = spec.options.get("publisher")

        try:
            name_re = re.compile(name_pattern, re.IGNORECASE)
            pub_re = (
                re.compile(publisher_pattern, re.IGNORECASE)
                if publisher_pattern
                else None
            )
        except re.error as exc:
            return InstalledApp(
                installed=False,
                method=self.method,
                detail=f"invalid detection pattern: {exc}",
            )

        for entry in entries:
            if product_code:
                if entry.get("key", "").lower() != str(product_code).lower():
                    continue
            else:
                display = entry.get("DisplayName") or ""
                if not display or not name_re.search(display):
                    continue
                if pub_re and not pub_re.search(entry.get("Publisher") or ""):
                    continue
            return InstalledApp(
                installed=True,
                version=(entry.get("DisplayVersion") or "").strip() or None,
                location=(entry.get("InstallLocation") or "").strip() or None,
                method=self.method,
                detail=entry.get("DisplayName") or entry.get("key"),
            )

        return InstalledApp(
            installed=False,
            method=self.method,
            detail=f"no uninstall entry matched {name_pattern!r}",
        )


@register
class WindowsPathDetector:
    """Detect by the presence of an installed executable.

    Options:
        ``paths``: list of absolute paths; ``%ProgramFiles%``-style variables
            and ``~`` are expanded.
        ``version_from``: optional ``"file_version"`` to read the PE version
            resource (Windows only; ignored elsewhere).
    """

    method = "windows_path"

    def detect(
        self, app: Application, spec: DetectionSpec, context: DetectionContext
    ) -> InstalledApp:
        exists = context.path_exists or (lambda p: Path(p).exists())
        candidates = spec.options.get("paths") or []
        if isinstance(candidates, str):
            candidates = [candidates]
        for raw in candidates:
            expanded = os.path.expandvars(os.path.expanduser(str(raw)))
            if exists(expanded):
                return InstalledApp(
                    installed=True,
                    version=_file_version(expanded)
                    if spec.options.get("version_from") == "file_version"
                    else None,
                    location=expanded,
                    method=self.method,
                )
        return InstalledApp(
            installed=False,
            method=self.method,
            detail="none of the configured paths exist",
        )


def _file_version(path: str) -> str | None:  # pragma: no cover - Windows only
    """Read a PE file version resource, if the platform provides an API."""
    try:
        import ctypes
        import ctypes.wintypes as wintypes
    except ImportError:
        return None
    try:
        size = ctypes.windll.version.GetFileVersionInfoSizeW(path, None)  # type: ignore[attr-defined]
        if not size:
            return None
        buffer = ctypes.create_string_buffer(size)
        ctypes.windll.version.GetFileVersionInfoW(path, 0, size, buffer)  # type: ignore[attr-defined]
        pointer = ctypes.c_void_p()
        length = wintypes.UINT()
        if not ctypes.windll.version.VerQueryValueW(  # type: ignore[attr-defined]
            buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)
        ):
            return None
        fixed = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32 * 4)).contents
        ms, ls = fixed[2], fixed[3]
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except Exception:
        return None
