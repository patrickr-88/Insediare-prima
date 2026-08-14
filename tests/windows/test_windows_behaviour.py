"""Windows-specific behaviour.

These tests run on any host: Windows APIs are reached only through the
:class:`SystemProbe` seam and the injected registry reader. Tests that need a
genuine Windows machine are marked ``@pytest.mark.windows`` and skipped
elsewhere — see docs/TESTING.md for the manual procedure.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.conftest import app_entry, registry_entry, windows_probe, write_repository
from usbinstaller.app import InstallerApp
from usbinstaller.installers import (
    WindowsExeInstaller,
    WindowsMsiInstaller,
    WindowsMsixInstaller,
    WindowsPowerShellInstaller,
)
from usbinstaller.installers.base import InstallContext
from usbinstaller.installers.process import CommandResult, RecordingRunner
from usbinstaller.models import OS, Action, Arch, PlatformPayload, Status

on_windows = pytest.mark.skipif(sys.platform != "win32", reason="requires Windows")


@pytest.fixture
def windows_repo(tmp_path: Path) -> Path:
    write_repository(
        tmp_path,
        [
            app_entry(
                "firefox",
                name="Mozilla Firefox",
                version="141.0",
                macos=False,
                detection={"method": "windows_registry", "display_name": "^Mozilla Firefox"},
            ),
            app_entry(
                "chrome",
                name="Google Chrome",
                version="127.0",
                macos=False,
                windows={
                    "installer": "installers/windows/chrome/chrome.msi",
                    "type": "msi",
                    "arguments": ["/qn"],
                },
                detection={"method": "windows_registry", "display_name": "^Google Chrome$"},
            ),
        ],
        settings={"require_confirmation": False},
    )
    return tmp_path


class TestWindowsPlatformDetection:
    def test_windows_11_detection(self):
        from usbinstaller.sysdetect.system import detect_system

        info = detect_system(windows_probe(version="10.0.22631", edition="Pro"))
        assert info.os is OS.WINDOWS
        assert info.os_name == "Windows 11 Pro"
        assert info.arch is Arch.X64

    def test_windows_10_detection(self):
        from usbinstaller.sysdetect.system import detect_system

        info = detect_system(windows_probe(version="10.0.19045", edition="Home"))
        assert info.os_name == "Windows 10 Home"

    def test_32_bit_windows(self):
        from usbinstaller.sysdetect.system import detect_system

        assert detect_system(windows_probe(machine="x86")).arch is Arch.X86

    def test_arm64_windows(self):
        from usbinstaller.sysdetect.system import detect_system

        assert detect_system(windows_probe(machine="ARM64")).arch is Arch.ARM64


class TestWindowsPlanning:
    def test_clean_machine_installs_everything(self, windows_repo):
        app = InstallerApp.create(windows_repo, probe=windows_probe(), dry_run=True)
        app.detection.registry_reader = lambda: []
        plan = app.plan()
        assert {i.action for i in plan.items} == {Action.INSTALL}

    def test_existing_installation_is_recognised(self, windows_repo):
        app = InstallerApp.create(windows_repo, probe=windows_probe(), dry_run=True)
        app.detection.registry_reader = lambda: [
            registry_entry("Mozilla Firefox (x64 en-GB)", "141.0"),
            registry_entry("Google Chrome", "126.0"),
        ]
        actions = {i.app_id: i.action for i in app.plan().items}
        assert actions["firefox"] is Action.SKIP
        assert actions["chrome"] is Action.UPGRADE

    def test_x86_machine_is_offered_only_compatible_software(self, tmp_path):
        write_repository(
            tmp_path,
            [
                app_entry(
                    "x64app",
                    macos=False,
                    windows={
                        "installer": "installers/windows/x64app/setup.exe",
                        "type": "exe",
                        "architectures": ["x64"],
                    },
                ),
                app_entry("anyapp", macos=False),
            ],
        )
        app = InstallerApp.create(tmp_path, probe=windows_probe(machine="x86"))
        assert {a.id for a in app.available()} == {"anyapp"}

    def test_admin_requirement_is_surfaced(self, windows_repo):
        app = InstallerApp.create(
            windows_repo, probe=windows_probe(admin=False), dry_run=False
        )
        app.detection.registry_reader = lambda: []
        plan = app.plan()
        privileges = app.privileges_for(plan)
        assert privileges.blocked
        assert "Run as administrator" in privileges.message(OS.WINDOWS)


class TestWindowsCommandConstruction:
    def _context(self, system, runner=None):
        return InstallContext(
            system=system,
            runner=runner or RecordingRunner(),
            repository_root=Path("E:\\"),
            extra={"system_root": "C:\\Windows"},
        )

    def test_exe_silent_switches_come_from_configuration(self, windows_system):
        payload = PlatformPayload(
            installer="i/setup.exe", type="exe", arguments=["/VERYSILENT", "/NORESTART"]
        )
        command = WindowsExeInstaller().build_command(
            Path("E:\\i\\setup.exe"), payload, self._context(windows_system)
        )
        assert command == ["E:\\i\\setup.exe", "/VERYSILENT", "/NORESTART"]

    def test_msi_command_shape(self, windows_system):
        payload = PlatformPayload(installer="i/app.msi", type="msi")
        command = WindowsMsiInstaller().build_command(
            Path("E:\\i\\app.msi"), payload, self._context(windows_system)
        )
        assert command[0] == "C:\\Windows\\System32\\msiexec.exe"
        assert command[1:3] == ["/i", "E:\\i\\app.msi"]

    def test_msi_3010_is_success_with_a_reboot_note(self, windows_system):
        runner = RecordingRunner(default_result=CommandResult(("msiexec",), exit_code=3010))
        outcome = WindowsMsiInstaller().install(
            None,
            PlatformPayload(installer="i/app.msi", type="msi"),
            Path("E:\\i\\app.msi"),
            self._context(windows_system, runner),
        )
        assert outcome.success and "reboot" in outcome.message

    def test_powershell_and_msix_use_the_system_powershell(self, windows_system):
        context = self._context(windows_system)
        ps = WindowsPowerShellInstaller().build_command(
            Path("E:\\i\\install.ps1"),
            PlatformPayload(installer="i/install.ps1", type="powershell"),
            context,
        )
        msix = WindowsMsixInstaller().build_command(
            Path("E:\\i\\app.msix"),
            PlatformPayload(installer="i/app.msix", type="msix"),
            context,
        )
        expected = "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"
        assert ps[0] == expected and msix[0] == expected
        assert "-ExecutionPolicy" in ps and "Bypass" in ps

    def test_execution_policy_bypass_is_process_scoped_only(self, windows_system):
        """Nothing may change machine-wide policy or disable protections."""
        command = WindowsPowerShellInstaller().build_command(
            Path("E:\\i\\install.ps1"),
            PlatformPayload(installer="i/install.ps1", type="powershell"),
            self._context(windows_system),
        )
        joined = " ".join(command)
        assert "Set-ExecutionPolicy" not in joined
        assert "Set-MpPreference" not in joined
        assert "DisableRealtimeMonitoring" not in joined


class TestWindowsRegistryReader:
    @on_windows
    @pytest.mark.windows
    def test_reads_real_uninstall_entries(self):
        from usbinstaller.detection.windows import read_uninstall_entries

        entries = read_uninstall_entries()
        assert isinstance(entries, list)
        assert any(entry.get("DisplayName") for entry in entries)

    def test_reader_returns_empty_off_windows(self):
        """On a non-Windows host the reader degrades quietly."""
        from usbinstaller.detection.windows import read_uninstall_entries

        if sys.platform == "win32":  # pragma: no cover - covered by the test above
            pytest.skip("covered by the Windows-only test")
        assert read_uninstall_entries() == []


class TestWindowsEndToEndDryRun:
    def test_dry_run_produces_a_full_report_without_executing(self, windows_repo):
        app = InstallerApp.create(
            windows_repo, probe=windows_probe(), dry_run=True, with_log=True
        )
        app.detection.registry_reader = lambda: []
        result = app.install(app.plan())

        assert result.failed == 0
        assert all(i.status is Status.DRY_RUN for i in result.items)
        # The command that *would* run was composed and logged…
        assert "msiexec.exe" in app.log.log_file.read_text()
        # …but no process was ever started.
        assert app.runner.history == ()
        payload = json.loads((app.log.directory / "results.json").read_text())
        assert payload["dry_run"] is True
