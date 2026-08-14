"""macOS-specific behaviour, including Apple Silicon and Intel differences.

Runs on any host: ``hdiutil``, ``installer`` and ``ditto`` are reached only
through the injected process runner. Tests needing a real Mac are marked
``@pytest.mark.macos``.
"""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path

import pytest

from tests.conftest import app_entry, macos_probe, write_repository
from usbinstaller.app import InstallerApp
from usbinstaller.detection.base import DetectionContext
from usbinstaller.detection.macos import MacAppBundleDetector, read_plist
from usbinstaller.installers import MacDmgInstaller, MacPkgInstaller, MacShellInstaller
from usbinstaller.installers.base import InstallContext
from usbinstaller.installers.process import CommandResult, RecordingRunner
from usbinstaller.models import OS, Action, Arch, DetectionSpec, PlatformPayload

on_macos = pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS")


@pytest.fixture
def macos_repo(tmp_path: Path) -> Path:
    write_repository(
        tmp_path,
        [
            app_entry(
                "firefox",
                name="Firefox",
                version="141.0",
                windows=False,
                macos={
                    "installer": "installers/macos/firefox/Firefox.dmg",
                    "type": "dmg",
                    "app_bundle": "Firefox.app",
                },
                detection={"method": "macos_app_bundle", "bundle": "Firefox.app"},
            ),
            app_entry(
                "universal",
                windows=False,
                macos={
                    "installer": "installers/macos/universal/app.pkg",
                    "type": "pkg",
                    "architectures": ["x64", "arm64"],
                },
            ),
            app_entry(
                "intel-only",
                windows=False,
                macos={
                    "installer": "installers/macos/intel-only/app.pkg",
                    "type": "pkg",
                    "architectures": ["x64"],
                },
            ),
            app_entry(
                "silicon-only",
                windows=False,
                macos={
                    "installer": "installers/macos/silicon-only/app.pkg",
                    "type": "pkg",
                    "architectures": ["arm64"],
                },
            ),
        ],
        settings={"require_confirmation": False},
    )
    return tmp_path


class TestPlatformDetection:
    def test_apple_silicon(self):
        from usbinstaller.sysdetect.system import describe, detect_system

        info = detect_system(macos_probe(machine="arm64"))
        assert info.os is OS.MACOS
        assert info.arch is Arch.ARM64
        assert "Apple Silicon" in describe(info)

    def test_intel(self):
        from usbinstaller.sysdetect.system import describe, detect_system

        info = detect_system(macos_probe(machine="x86_64"))
        assert info.arch is Arch.X64
        assert "Intel" in describe(info)

    def test_rosetta_translated_process_reports_the_real_hardware(self):
        from usbinstaller.sysdetect.system import detect_system

        info = detect_system(macos_probe(machine="x86_64", translated=True))
        assert info.arch is Arch.ARM64

    def test_root_detection(self):
        from usbinstaller.sysdetect.system import detect_system

        assert detect_system(macos_probe(admin=True)).is_admin is True
        assert detect_system(macos_probe(admin=False)).is_admin is False

    @on_macos
    @pytest.mark.macos
    def test_real_machine_is_detected_as_macos(self):
        from usbinstaller.sysdetect.system import detect_system

        info = detect_system()
        assert info.os is OS.MACOS
        assert info.arch in (Arch.ARM64, Arch.X64)


class TestArchitectureFiltering:
    def test_apple_silicon_sees_arm_and_universal_software(self, macos_repo):
        app = InstallerApp.create(macos_repo, probe=macos_probe(machine="arm64"))
        assert {a.id for a in app.available()} == {"firefox", "universal", "silicon-only"}

    def test_intel_mac_sees_intel_and_universal_software(self, macos_repo):
        app = InstallerApp.create(macos_repo, probe=macos_probe(machine="x86_64"))
        assert {a.id for a in app.available()} == {"firefox", "universal", "intel-only"}

    def test_an_arm_only_package_is_never_planned_on_intel(self, macos_repo):
        app = InstallerApp.create(macos_repo, probe=macos_probe(machine="x86_64"))
        item = app.plan(["silicon-only"]).items[0]
        assert item.action is Action.SKIP
        assert "not supported on x64" in item.reason


class TestMacInstallerCommands:
    def _context(self, system, runner=None, **kwargs):
        return InstallContext(
            system=system,
            runner=runner or RecordingRunner(),
            repository_root=Path("/Volumes/USB"),
            **kwargs,
        )

    def test_pkg_uses_apples_installer_tool(self, macos_system):
        command = MacPkgInstaller().build_command(
            Path("/Volumes/USB/app.pkg"),
            PlatformPayload(installer="i/app.pkg", type="pkg"),
            self._context(macos_system),
        )
        assert command[0] == "/usr/sbin/installer"
        assert command[-1] == "/"

    def test_shell_installer_does_not_need_the_execute_bit(self, macos_system):
        command = MacShellInstaller().build_command(
            Path("/Volumes/USB/install.sh"),
            PlatformPayload(installer="i/install.sh", type="shell"),
            self._context(macos_system),
        )
        assert command[0] == "/bin/sh"

    def test_dmg_is_mounted_read_only_and_without_browsing(
        self, macos_system, tmp_path, monkeypatch
    ):
        import usbinstaller.installers.macos as module

        applications = tmp_path / "Applications"
        applications.mkdir()
        monkeypatch.setattr(module, "_find_bundle", lambda root, p: root / "Firefox.app")
        runner = RecordingRunner(default_result=CommandResult(("x",), exit_code=0))

        MacDmgInstaller().install(
            None,
            PlatformPayload(
                installer="i/Firefox.dmg",
                type="dmg",
                extra={"destination": str(applications)},
            ),
            tmp_path / "Firefox.dmg",
            self._context(macos_system, runner, workspace=tmp_path),
        )
        attach = next(c for c in runner.history if "attach" in c)
        assert "-readonly" in attach and "-nobrowse" in attach

    def test_gatekeeper_and_sip_are_never_disabled(self, macos_system, tmp_path, monkeypatch):
        """The engine must not weaken system security to install software."""
        import usbinstaller.installers.macos as module

        applications = tmp_path / "Applications"
        applications.mkdir()
        monkeypatch.setattr(module, "_find_bundle", lambda root, p: root / "App.app")
        runner = RecordingRunner(default_result=CommandResult(("x",), exit_code=0))

        MacDmgInstaller().install(
            None,
            PlatformPayload(
                installer="i/App.dmg", type="dmg", extra={"destination": str(applications)}
            ),
            tmp_path / "App.dmg",
            self._context(macos_system, runner, workspace=tmp_path),
        )
        joined = " ".join(" ".join(c) for c in runner.history)
        assert "spctl" not in joined  # never disables Gatekeeper
        assert "csrutil" not in joined  # never touches SIP
        assert "GatekeeperEnabled" not in joined
        # The only quarantine change is scoped to the bundle just installed.
        quarantine = [c for c in runner.history if "xattr" in c[0]]
        assert len(quarantine) == 1
        assert str(applications) in quarantine[0][-1]


class TestBundleDetection:
    def test_reads_a_real_binary_plist(self, tmp_path, macos_system):
        bundle = tmp_path / "Firefox.app" / "Contents"
        bundle.mkdir(parents=True)
        with open(bundle / "Info.plist", "wb") as handle:
            plistlib.dump(
                {"CFBundleShortVersionString": "141.0"}, handle, fmt=plistlib.FMT_BINARY
            )
        context = DetectionContext(system=macos_system, application_dirs=(str(tmp_path),))
        result = MacAppBundleDetector().detect(
            app_placeholder(), DetectionSpec("macos_app_bundle", {"bundle": "Firefox.app"}),
            context,
        )
        assert result.version == "141.0"

    def test_missing_plist_is_not_an_error(self, tmp_path):
        assert read_plist(str(tmp_path / "nope.plist")) == {}

    def test_user_applications_directory_is_searched(self, tmp_path, macos_system):
        user_apps = tmp_path / "home" / "Applications"
        (user_apps / "Tool.app" / "Contents").mkdir(parents=True)
        with open(user_apps / "Tool.app" / "Contents" / "Info.plist", "wb") as handle:
            plistlib.dump({"CFBundleShortVersionString": "2.0"}, handle)
        context = DetectionContext(
            system=macos_system,
            application_dirs=("/Applications", str(user_apps)),
        )
        result = MacAppBundleDetector().detect(
            app_placeholder(), DetectionSpec("macos_app_bundle", {"bundle": "Tool.app"}),
            context,
        )
        assert result.installed and result.version == "2.0"

    @on_macos
    @pytest.mark.macos
    def test_finder_is_detectable_on_a_real_mac(self, macos_system):
        context = DetectionContext(system=macos_system, application_dirs=("/System/Library/CoreServices",))
        result = MacAppBundleDetector().detect(
            app_placeholder(),
            DetectionSpec("macos_app_bundle", {"bundle": "Finder.app"}),
            context,
        )
        assert result.installed


def app_placeholder():
    from usbinstaller.models import Application

    return Application(id="x", name="X")
