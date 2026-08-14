"""Shared fixtures: fake system probes and synthetic USB repositories.

No test in this suite touches a real registry, mounts a real disk image, or
executes a real vendor installer. Platform behaviour is exercised through
:class:`FakeProbe` and the recording process runner, so the Windows and macOS
suites both run on any host.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from usbinstaller.detection.base import DetectionContext
from usbinstaller.installers.process import CommandResult, RecordingRunner
from usbinstaller.models import OS, Arch, SystemInfo
from usbinstaller.repository import Repository
from usbinstaller.sysdetect.system import SystemProbe, detect_system

# --------------------------------------------------------------------------
# System probes
# --------------------------------------------------------------------------


class FakeProbe(SystemProbe):
    """A scriptable stand-in for the real platform APIs."""

    def __init__(
        self,
        system: str = "Windows",
        release: str = "10",
        version: str = "10.0.22631",
        machine: str = "AMD64",
        mac_version: str = "",
        admin: bool = True,
        edition: str | None = "Pro",
        translated: bool = False,
        free: int = 100 * 1024**3,
        hostname: str = "TEST-PC",
    ) -> None:
        self._system = system
        self._release = release
        self._version = version
        self._machine = machine
        self._mac_version = mac_version
        self._admin = admin
        self._edition = edition
        self._translated = translated
        self._free = free
        self._hostname = hostname

    def system(self) -> str:
        return self._system

    def release(self) -> str:
        return self._release

    def version(self) -> str:
        return self._version

    def machine(self) -> str:
        return self._machine

    def mac_ver(self):
        return (self._mac_version, ("", "", ""), self._machine)

    def win32_edition(self):
        return self._edition

    def hostname(self) -> str:
        return self._hostname

    def geteuid(self) -> int:
        return 0 if self._admin else 501

    def windows_is_admin(self) -> bool:
        return self._admin

    def is_translated(self) -> bool:
        return self._translated

    def disk_free(self, path: str) -> int:
        return self._free

    def python_version(self) -> str:
        return "3.11.0"


def windows_probe(**kwargs) -> FakeProbe:
    defaults = dict(system="Windows", release="10", version="10.0.22631", machine="AMD64")
    defaults.update(kwargs)
    return FakeProbe(**defaults)


def macos_probe(**kwargs) -> FakeProbe:
    defaults = dict(
        system="Darwin",
        release="23.5.0",
        version="Darwin Kernel Version 23.5.0",
        machine="arm64",
        mac_version="14.5",
        edition=None,
        hostname="test-mac",
    )
    defaults.update(kwargs)
    return FakeProbe(**defaults)


@pytest.fixture
def windows_system() -> SystemInfo:
    return detect_system(windows_probe(), repository_path="E:\\")


@pytest.fixture
def macos_system() -> SystemInfo:
    return detect_system(macos_probe(), repository_path="/Volumes/SoftwareInstaller")


@pytest.fixture
def intel_macos_system() -> SystemInfo:
    return detect_system(macos_probe(machine="x86_64"))


# --------------------------------------------------------------------------
# Repository building
# --------------------------------------------------------------------------


def make_installer(path: Path, content: bytes = b"MOCK INSTALLER\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def write_repository(
    root: Path,
    applications: list[dict],
    settings: dict | None = None,
    checksums: dict | None = None,
    *,
    create_installers: bool = True,
) -> Path:
    """Materialise a repository on disk from plain dictionaries."""
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "checksums").mkdir(parents=True, exist_ok=True)
    (root / "config" / "applications.json").write_text(
        json.dumps({"schema_version": 1, "applications": applications}, indent=2),
        encoding="utf-8",
    )
    if settings is not None:
        (root / "config" / "settings.json").write_text(
            json.dumps(settings, indent=2), encoding="utf-8"
        )
    if checksums is not None:
        (root / "checksums" / "checksums.json").write_text(
            json.dumps(checksums, indent=2), encoding="utf-8"
        )
    if create_installers:
        for application in applications:
            for key in ("windows", "macos"):
                payload = application.get(key)
                if payload and payload.get("installer"):
                    make_installer(root / payload["installer"])
    return root


def app_entry(
    app_id: str = "sample",
    *,
    name: str | None = None,
    version: str = "1.0.0",
    windows: dict | None = None,
    macos: dict | None = None,
    **extra,
) -> dict:
    """Build a catalogue entry with sensible defaults."""
    entry: dict = {
        "id": app_id,
        "name": name or app_id.title(),
        "version": version,
        "category": "Testing",
    }
    if windows is not False:
        entry["windows"] = windows or {
            "installer": f"installers/windows/{app_id}/setup.exe",
            "type": "exe",
            "arguments": ["/S"],
        }
    if macos is not False:
        entry["macos"] = macos or {
            "installer": f"installers/macos/{app_id}/app.pkg",
            "type": "pkg",
        }
    entry.update(extra)
    return entry


@pytest.fixture
def simple_repo(tmp_path: Path) -> Repository:
    """A three-application repository with real (mock) installer files."""
    write_repository(
        tmp_path,
        [
            app_entry("firefox", name="Mozilla Firefox", version="141.0"),
            app_entry("vlc", name="VLC", version="3.0.21"),
            app_entry(
                "7zip",
                name="7-Zip",
                version="25.01",
                macos=False,
            ),
        ],
        settings={"require_confirmation": False},
    )
    return Repository.load(tmp_path)


@pytest.fixture
def recording_runner() -> RecordingRunner:
    return RecordingRunner()


@pytest.fixture
def detection_context(windows_system: SystemInfo, recording_runner) -> DetectionContext:
    return DetectionContext(
        system=windows_system,
        runner=recording_runner,
        registry_reader=lambda: [],
        path_exists=lambda _p: False,
    )


def registry_entry(
    display_name: str,
    version: str = "1.0",
    location: str = "C:\\Program Files\\App",
    publisher: str = "Vendor",
    key: str = "{GUID}",
) -> dict[str, str]:
    return {
        "key": key,
        "hive": "HKLM",
        "DisplayName": display_name,
        "DisplayVersion": version,
        "InstallLocation": location,
        "Publisher": publisher,
    }


def ok(exit_code: int = 0, stdout: str = "") -> CommandResult:
    return CommandResult(("mock",), exit_code=exit_code, stdout=stdout)


@pytest.fixture
def sample_arch() -> Arch:
    return Arch.X64


@pytest.fixture
def sample_os() -> OS:
    return OS.WINDOWS
