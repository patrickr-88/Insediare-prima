"""OS, architecture, privilege and repository detection."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import FakeProbe, macos_probe, windows_probe
from usbinstaller.errors import RepositoryError
from usbinstaller.models import OS, Action, Application, Arch, PlanItem
from usbinstaller.repository import Repository
from usbinstaller.sysdetect.privileges import (
    PrivilegeRequirement,
    elevation_command,
    requirement_for,
)
from usbinstaller.sysdetect.system import (
    describe,
    detect_arch,
    detect_os,
    detect_os_name,
    detect_system,
    free_disk_bytes,
    is_admin,
    system_drive,
)


class TestOsDetection:
    @pytest.mark.parametrize(
        ("system_name", "expected"),
        [
            ("Windows", OS.WINDOWS),
            ("windows", OS.WINDOWS),
            ("Win32", OS.WINDOWS),
            ("Darwin", OS.MACOS),
            ("Linux", OS.LINUX),
            ("Haiku", OS.UNKNOWN),
            ("", OS.UNKNOWN),
        ],
    )
    def test_detect_os(self, system_name, expected):
        assert detect_os(FakeProbe(system=system_name)) is expected

    def test_windows_11_is_recognised_by_build_number(self):
        probe = windows_probe(version="10.0.22631", release="10", edition="Pro")
        assert detect_os_name(probe) == "Windows 11 Pro"

    def test_windows_10_stays_windows_10(self):
        probe = windows_probe(version="10.0.19045", release="10", edition="Home")
        assert detect_os_name(probe) == "Windows 10 Home"

    def test_macos_name_uses_the_marketing_version(self):
        assert detect_os_name(macos_probe(mac_version="14.5")) == "macOS 14.5"


class TestArchDetection:
    @pytest.mark.parametrize(
        ("machine", "expected"),
        [
            ("AMD64", Arch.X64),
            ("x86_64", Arch.X64),
            ("arm64", Arch.ARM64),
            ("aarch64", Arch.ARM64),
            ("x86", Arch.X86),
            ("i686", Arch.X86),
            ("sparc", Arch.UNKNOWN),
        ],
    )
    def test_architecture_aliases(self, machine, expected):
        assert detect_arch(FakeProbe(machine=machine)) is expected

    def test_apple_silicon_is_detected_through_rosetta(self):
        """A translated x86_64 process on Apple Silicon must report arm64."""
        probe = macos_probe(machine="x86_64", translated=True)
        assert detect_arch(probe) is Arch.ARM64

    def test_intel_mac_is_not_mistaken_for_apple_silicon(self):
        probe = macos_probe(machine="x86_64", translated=False)
        assert detect_arch(probe) is Arch.X64

    def test_rosetta_check_does_not_apply_to_windows(self):
        assert detect_arch(windows_probe(machine="AMD64", translated=True)) is Arch.X64


class TestPrivilegeDetection:
    def test_windows_admin(self):
        assert is_admin(windows_probe(admin=True)) is True
        assert is_admin(windows_probe(admin=False)) is False

    def test_posix_root(self):
        assert is_admin(macos_probe(admin=True)) is True
        assert is_admin(macos_probe(admin=False)) is False

    def test_detection_failure_is_not_fatal(self):
        class Broken(FakeProbe):
            def windows_is_admin(self):
                raise OSError("no api")

        assert is_admin(Broken(system="Windows")) is False


class TestSystemInfo:
    def test_windows_system_info(self):
        info = detect_system(windows_probe(), repository_path="E:\\")
        assert info.os is OS.WINDOWS
        assert info.arch is Arch.X64
        assert info.is_admin is True
        assert info.repository_path == "E:\\"
        assert info.free_disk_bytes > 0
        assert "Windows 11" in describe(info)

    def test_macos_system_info_describes_apple_silicon(self):
        info = detect_system(macos_probe(), repository_path="/Volumes/USB")
        assert "Apple Silicon" in describe(info)
        assert info.to_dict()["arch"] == "arm64"

    def test_disk_probe_failure_returns_none(self):
        class Broken(FakeProbe):
            def disk_free(self, path):
                raise OSError("gone")

        assert free_disk_bytes("/", Broken()) is None

    def test_system_drive(self, monkeypatch):
        monkeypatch.setenv("SystemDrive", "D:")
        assert system_drive(OS.WINDOWS) == "D:\\"
        assert system_drive(OS.MACOS) == "/"


class TestRepositoryDiscovery:
    def test_discovers_from_a_nested_directory(self, simple_repo: Repository, tmp_path):
        nested = tmp_path / "installers" / "windows"
        assert Repository.discover(nested) == tmp_path.resolve()

    def test_environment_override_wins(self, simple_repo, tmp_path, monkeypatch):
        monkeypatch.setenv("USB_INSTALLER_ROOT", str(tmp_path))
        assert Repository.discover(Path("/")) == tmp_path.resolve()

    def test_bad_environment_override_is_reported(self, tmp_path, monkeypatch):
        monkeypatch.setenv("USB_INSTALLER_ROOT", str(tmp_path / "nowhere"))
        with pytest.raises(RepositoryError, match="USB_INSTALLER_ROOT"):
            Repository.discover()

    def test_missing_repository_raises(self, tmp_path, monkeypatch):
        monkeypatch.delenv("USB_INSTALLER_ROOT", raising=False)
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.chdir(empty)
        with pytest.raises(RepositoryError, match="Could not locate"):
            Repository.discover(empty)

    def test_load_reports_a_missing_configuration_file(self, tmp_path):
        with pytest.raises(RepositoryError, match="missing configuration file"):
            Repository.load(tmp_path)

    def test_relative_round_trips(self, simple_repo: Repository):
        resolved = simple_repo.resolve("installers/windows/firefox/setup.exe")
        assert simple_repo.relative(resolved) == "installers/windows/firefox/setup.exe"


class TestPrivilegeRequirement:
    def _item(self, app_id: str, requires_admin: bool) -> PlanItem:
        return PlanItem(
            application=Application(id=app_id, name=app_id),
            action=Action.INSTALL,
            requires_admin=requires_admin,
        )

    def test_requirement_is_satisfied_when_admin(self, windows_system):
        requirement = requirement_for(windows_system, [self._item("a", True)])
        assert requirement.required and requirement.satisfied
        assert not requirement.blocked

    def test_requirement_blocks_without_admin(self):
        from usbinstaller.sysdetect.system import detect_system

        system = detect_system(windows_probe(admin=False))
        requirement = requirement_for(system, [self._item("firefox", True)])
        assert requirement.blocked
        message = requirement.message(OS.WINDOWS)
        assert "firefox" in message
        assert "Run as administrator" in message

    def test_macos_message_mentions_sudo(self):
        requirement = PrivilegeRequirement(True, False, ("vlc",))
        assert "sudo" in requirement.message(OS.MACOS)

    def test_no_requirement_when_nothing_needs_admin(self, windows_system):
        requirement = requirement_for(windows_system, [self._item("a", False)])
        assert not requirement.required
        assert "No administrator privileges" in requirement.message(OS.WINDOWS)

    def test_skipped_items_do_not_create_a_requirement(self, windows_system):
        item = self._item("a", True)
        item.action = Action.SKIP
        assert requirement_for(windows_system, [item]).required is False

    def test_elevation_command_is_platform_appropriate(self):
        assert elevation_command(OS.WINDOWS, ["app.exe"])[:3] == [
            "powershell",
            "-Command",
            "Start-Process",
        ]
        assert elevation_command(OS.MACOS, ["app"])[0] == "sudo"
        assert elevation_command(OS.MACOS, []) is None
