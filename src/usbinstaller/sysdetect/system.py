"""Operating system, architecture, privilege and disk detection.

All platform interrogation goes through :class:`SystemProbe`, a thin seam over
``platform``/``os``/``ctypes``. Tests subclass or monkey-patch the probe, which
is how the Windows-specific and macOS-specific suites run on any host without
touching real system APIs.
"""

from __future__ import annotations

import os
import platform as _platform
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from ..models import OS, Arch, SystemInfo

WINDOWS_EDITION_UNKNOWN = "Windows"


class SystemProbe:
    """Raw platform facts. Every method is individually overridable in tests."""

    def system(self) -> str:
        return _platform.system()

    def release(self) -> str:
        return _platform.release()

    def version(self) -> str:
        return _platform.version()

    def machine(self) -> str:
        return _platform.machine()

    def mac_ver(self) -> tuple[str, tuple[str, str, str], str]:
        return _platform.mac_ver()

    def win32_edition(self) -> str | None:
        getter = getattr(_platform, "win32_edition", None)
        if getter is None:
            return None
        try:
            return getter()
        except Exception:  # pragma: no cover - platform dependent
            return None

    def hostname(self) -> str:
        try:
            return socket.gethostname()
        except OSError:  # pragma: no cover - defensive
            return "unknown"

    def geteuid(self) -> int:
        return os.geteuid()  # type: ignore[attr-defined]

    def windows_is_admin(self) -> bool:  # pragma: no cover - Windows only
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]

    def is_translated(self) -> bool:
        """True when running under Rosetta 2 on an Apple Silicon Mac."""
        try:
            # sysctl is resolved through PATH deliberately: its location has
            # moved between macOS releases, and the call is read-only.
            out = subprocess.run(  # noqa: S607
                ["sysctl", "-in", "sysctl.proc_translated"],  # noqa: S607
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):  # pragma: no cover
            return False
        return out.stdout.strip() == "1"

    def disk_free(self, path: str) -> int:
        return shutil.disk_usage(path).free

    def python_version(self) -> str:
        return _platform.python_version()


def detect_os(probe: SystemProbe | None = None) -> OS:
    """Map ``platform.system()`` onto an :class:`OS` member."""
    probe = probe or SystemProbe()
    name = (probe.system() or "").strip().lower()
    if name.startswith("win"):
        return OS.WINDOWS
    if name == "darwin":
        return OS.MACOS
    if name == "linux":
        return OS.LINUX
    return OS.UNKNOWN


def detect_arch(probe: SystemProbe | None = None, os_: OS | None = None) -> Arch:
    """Detect the *machine's* architecture, seeing through Rosetta 2.

    A Python built for x86_64 running on Apple Silicon reports ``x86_64``. That
    would hand an Intel installer to an ARM Mac, so on macOS we ask the kernel
    whether the current process is translated and correct the answer.
    """
    probe = probe or SystemProbe()
    arch = Arch.parse(probe.machine())
    os_ = os_ if os_ is not None else detect_os(probe)
    if os_ is OS.MACOS and arch is Arch.X64 and probe.is_translated():
        return Arch.ARM64
    return arch


def detect_os_name(probe: SystemProbe | None = None, os_: OS | None = None) -> str:
    """A human-facing OS name, e.g. ``Windows 11 Pro`` or ``macOS Sequoia``."""
    probe = probe or SystemProbe()
    os_ = os_ if os_ is not None else detect_os(probe)
    if os_ is OS.WINDOWS:
        # Windows 11 still reports release "10"; build >= 22000 is Windows 11.
        release = probe.release()
        build = _build_number(probe.version())
        family = "Windows 11" if build >= 22000 else f"Windows {release}"
        edition = probe.win32_edition()
        return f"{family} {edition}".strip() if edition else family
    if os_ is OS.MACOS:
        version = probe.mac_ver()[0] or probe.release()
        return f"macOS {version}".strip()
    if os_ is OS.LINUX:
        return f"Linux {probe.release()}"
    return "Unknown operating system"


def detect_os_version(probe: SystemProbe | None = None, os_: OS | None = None) -> str:
    """A comparable OS version string (``10.0.22631``, ``14.5``)."""
    probe = probe or SystemProbe()
    os_ = os_ if os_ is not None else detect_os(probe)
    if os_ is OS.MACOS:
        return probe.mac_ver()[0] or probe.release()
    if os_ is OS.WINDOWS:
        return probe.version() or probe.release()
    return probe.release()


def _build_number(version: str) -> int:
    parts = (version or "").split(".")
    try:
        return int(parts[-1])
    except (ValueError, IndexError):
        return 0


def is_admin(probe: SystemProbe | None = None, os_: OS | None = None) -> bool:
    """Whether the process holds administrator/root privileges."""
    probe = probe or SystemProbe()
    os_ = os_ if os_ is not None else detect_os(probe)
    try:
        if os_ is OS.WINDOWS:
            return probe.windows_is_admin()
        return probe.geteuid() == 0
    except Exception:  # pragma: no cover - defensive: never crash on detection
        return False


def free_disk_bytes(path: str | Path = "/", probe: SystemProbe | None = None) -> int | None:
    probe = probe or SystemProbe()
    try:
        return probe.disk_free(str(path))
    except OSError:
        return None


def system_drive(os_: OS) -> str:
    """The volume installers will actually write to."""
    if os_ is OS.WINDOWS:
        return os.environ.get("SystemDrive", "C:") + "\\"
    return "/"


def detect_system(
    probe: SystemProbe | None = None,
    repository_path: str | Path | None = None,
) -> SystemInfo:
    """Assemble the full :class:`SystemInfo` for this machine."""
    probe = probe or SystemProbe()
    os_ = detect_os(probe)
    return SystemInfo(
        os=os_,
        os_name=detect_os_name(probe, os_),
        os_version=detect_os_version(probe, os_),
        arch=detect_arch(probe, os_),
        is_admin=is_admin(probe, os_),
        hostname=probe.hostname(),
        repository_path=str(repository_path) if repository_path else None,
        free_disk_bytes=free_disk_bytes(system_drive(os_), probe),
        python_version=probe.python_version(),
    )


def describe(info: SystemInfo) -> str:
    """Multi-line human summary used by the CLI and GUI headers."""
    free = (
        f"{info.free_disk_bytes / (1024 ** 3):.1f} GB"
        if info.free_disk_bytes is not None
        else "unknown"
    )
    return "\n".join(
        [
            f"Computer:      {info.hostname}",
            f"OS:            {info.os_name} ({info.os_version})",
            f"Architecture:  {_arch_label(info)}",
            f"Administrator: {'Yes' if info.is_admin else 'No'}",
            f"Free disk:     {free}",
            f"Repository:    {info.repository_path or 'not located'}",
        ]
    )


def _arch_label(info: SystemInfo) -> str:
    if info.os is OS.MACOS:
        return {
            Arch.ARM64: "Apple Silicon (arm64)",
            Arch.X64: "Intel (x86_64)",
        }.get(info.arch, info.arch.value)
    return info.arch.value


def running_frozen() -> bool:
    """True when executing from a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False))
