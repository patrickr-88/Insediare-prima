"""Installer abstraction, command construction and process execution.

No real vendor installer is ever executed here: commands are either built and
inspected, or run against tiny scripts created in a temporary directory.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from usbinstaller.errors import (
    InstallerExecutionError,
    InstallerTimeout,
    UnsupportedInstallerType,
)
from usbinstaller.installers import (
    InstallContext,
    MacAppInstaller,
    MacDmgInstaller,
    MacPkgInstaller,
    MacShellInstaller,
    ProcessRunner,
    RecordingRunner,
    WindowsExeInstaller,
    WindowsMsiInstaller,
    WindowsMsixInstaller,
    WindowsPowerShellInstaller,
    get_installer,
    registered_types,
)
from usbinstaller.installers.base import Installer, register
from usbinstaller.installers.process import CommandResult
from usbinstaller.models import OS, Application, PlatformPayload

pytestmark = pytest.mark.filterwarnings("ignore::ResourceWarning")


def context(system, runner=None, **kwargs) -> InstallContext:
    return InstallContext(
        system=system,
        runner=runner or RecordingRunner(),
        repository_root=Path("/repo"),
        extra={"system_root": "C:\\Windows"},
        **kwargs,
    )


def app(app_id="sample") -> Application:
    return Application(id=app_id, name=app_id.title(), version="1.0")


def payload(**kwargs) -> PlatformPayload:
    defaults = {"installer": "installers/windows/a/setup.exe", "type": "exe"}
    defaults.update(kwargs)
    return PlatformPayload(**defaults)


class TestRegistry:
    def test_every_declared_type_has_a_handler(self):
        from usbinstaller.config.catalogue import INSTALLER_TYPES

        for os_, types in INSTALLER_TYPES.items():
            for installer_type in types:
                assert get_installer(os_, installer_type) is not None

    def test_registered_types_are_partitioned_by_os(self):
        assert set(registered_types(OS.WINDOWS)) == {"exe", "msi", "msix", "powershell"}
        assert set(registered_types(OS.MACOS)) == {"dmg", "pkg", "app", "shell"}

    def test_unknown_type_raises(self):
        with pytest.raises(UnsupportedInstallerType):
            get_installer(OS.WINDOWS, "deb")

    def test_wrong_platform_raises(self):
        with pytest.raises(UnsupportedInstallerType):
            get_installer(OS.MACOS, "exe")

    def test_a_new_type_can_be_added_without_touching_existing_code(self, windows_system):
        class ZipInstaller(Installer):
            type = "zap"
            os = OS.WINDOWS

            def build_command(self, installer_path, payload_, ctx):
                return ["zap.exe", str(installer_path)]

        register(ZipInstaller)
        assert get_installer(OS.WINDOWS, "zap").type == "zap"


class TestWindowsCommands:
    def test_exe_uses_configured_silent_switches(self, windows_system):
        command = WindowsExeInstaller().build_command(
            Path("E:/i/setup.exe"), payload(arguments=["/S", "/NORESTART"]), context(windows_system)
        )
        assert command == ["E:/i/setup.exe", "/S", "/NORESTART"]

    def test_msi_goes_through_msiexec_with_i_before_the_package(self, windows_system):
        command = WindowsMsiInstaller().build_command(
            Path("E:/i/app.msi"), payload(type="msi"), context(windows_system)
        )
        assert command[0].endswith("msiexec.exe")
        assert command[1] == "/i"
        assert command[2] == "E:/i/app.msi"
        assert "/qn" in command

    def test_msi_configured_arguments_replace_the_defaults(self, windows_system):
        command = WindowsMsiInstaller().build_command(
            Path("E:/i/app.msi"),
            payload(type="msi", arguments=["/quiet", "ALLUSERS=1"]),
            context(windows_system),
        )
        assert command[-2:] == ["/quiet", "ALLUSERS=1"]
        assert "/qn" not in command

    def test_msi_reboot_required_counts_as_success(self, windows_system):
        runner = RecordingRunner(default_result=CommandResult(("msiexec",), exit_code=3010))
        outcome = WindowsMsiInstaller().install(
            app(), payload(type="msi"), Path("E:/i/app.msi"), context(windows_system, runner)
        )
        assert outcome.success
        assert "reboot" in outcome.message

    def test_msi_real_failure_is_still_a_failure(self, windows_system):
        runner = RecordingRunner(default_result=CommandResult(("msiexec",), exit_code=1603))
        outcome = WindowsMsiInstaller().install(
            app(), payload(type="msi"), Path("E:/i/app.msi"), context(windows_system, runner)
        )
        assert not outcome.success
        assert outcome.exit_code == 1603

    def test_msix_uses_add_appxpackage(self, windows_system):
        command = WindowsMsixInstaller().build_command(
            Path("E:/i/app.msix"), payload(type="msix"), context(windows_system)
        )
        assert command[0].endswith("powershell.exe")
        assert "Add-AppxPackage" in command

    def test_powershell_scripts_run_with_file_and_no_profile(self, windows_system):
        command = WindowsPowerShellInstaller().build_command(
            Path("E:/i/install.ps1"),
            payload(type="powershell", arguments=["-Mode", "Silent"]),
            context(windows_system),
        )
        assert "-NoProfile" in command and "-NonInteractive" in command
        assert command[command.index("-File") + 1] == "E:/i/install.ps1"
        assert command[-2:] == ["-Mode", "Silent"]

    def test_powershell_executable_is_an_absolute_system_path(self, windows_system):
        command = WindowsPowerShellInstaller().build_command(
            Path("E:/i/install.ps1"), payload(type="powershell"), context(windows_system)
        )
        assert command[0].startswith("C:\\Windows")


class TestMacCommands:
    def test_pkg_targets_the_system_volume(self, macos_system):
        command = MacPkgInstaller().build_command(
            Path("/Volumes/USB/app.pkg"), payload(type="pkg"), context(macos_system)
        )
        assert command == ["/usr/sbin/installer", "-pkg", "/Volumes/USB/app.pkg", "-target", "/"]

    def test_shell_scripts_run_through_sh(self, macos_system):
        command = MacShellInstaller().build_command(
            Path("/Volumes/USB/install.sh"),
            payload(type="shell", arguments=["--quiet"]),
            context(macos_system),
        )
        assert command == ["/bin/sh", "/Volumes/USB/install.sh", "--quiet"]


class TestDmgInstaller:
    def _runner(self, **results):
        return RecordingRunner(results, default_result=CommandResult(("x",), exit_code=0))

    def test_mount_copy_and_detach(self, macos_system, tmp_path, monkeypatch):
        applications = tmp_path / "Applications"
        applications.mkdir()
        runner = self._runner()

        # The bundle "appears" in the mountpoint once hdiutil has run.
        import usbinstaller.installers.macos as macos_module

        monkeypatch.setattr(
            macos_module, "_find_bundle", lambda root, preferred: root / "Firefox.app"
        )

        outcome = MacDmgInstaller().install(
            app("firefox"),
            payload(type="dmg", app_bundle="Firefox.app", extra={"destination": str(applications)}),
            tmp_path / "Firefox.dmg",
            context(macos_system, runner, workspace=tmp_path),
        )
        commands = [" ".join(c) for c in runner.history]
        assert outcome.success
        assert any("hdiutil attach" in c for c in commands)
        assert any("ditto" in c for c in commands)
        assert any("hdiutil detach" in c for c in commands)

    def test_image_is_always_detached_even_when_the_copy_fails(
        self, macos_system, tmp_path, monkeypatch
    ):
        applications = tmp_path / "Applications"
        applications.mkdir()
        runner = RecordingRunner(
            {"ditto": CommandResult(("ditto",), exit_code=1, stderr="No space left")},
            default_result=CommandResult(("x",), exit_code=0),
        )
        import usbinstaller.installers.macos as macos_module

        monkeypatch.setattr(
            macos_module, "_find_bundle", lambda root, preferred: root / "Firefox.app"
        )

        outcome = MacDmgInstaller().install(
            app("firefox"),
            payload(type="dmg", extra={"destination": str(applications)}),
            tmp_path / "Firefox.dmg",
            context(macos_system, runner, workspace=tmp_path),
        )
        assert not outcome.success
        assert any("detach" in " ".join(c) for c in runner.history)

    def test_failure_to_mount_is_reported(self, macos_system, tmp_path):
        runner = RecordingRunner(
            {"attach": CommandResult(("hdiutil",), exit_code=1, stderr="corrupt image")}
        )
        outcome = MacDmgInstaller().install(
            app(),
            payload(type="dmg"),
            tmp_path / "broken.dmg",
            context(macos_system, runner, workspace=tmp_path),
        )
        assert not outcome.success
        assert "mount" in outcome.message

    def test_dmg_containing_a_package_is_installed_with_installer(
        self, macos_system, tmp_path, monkeypatch
    ):
        runner = self._runner()
        import usbinstaller.installers.macos as macos_module

        monkeypatch.setattr(macos_module, "_find_bundle", lambda root, preferred: None)
        monkeypatch.setattr(
            macos_module, "_find_package", lambda root: root / "Payload.pkg"
        )
        outcome = MacDmgInstaller().install(
            app(), payload(type="dmg"), tmp_path / "x.dmg",
            context(macos_system, runner, workspace=tmp_path),
        )
        assert outcome.success
        assert any("/usr/sbin/installer" in " ".join(c) for c in runner.history)

    def test_dmg_with_no_recognisable_payload_fails_clearly(
        self, macos_system, tmp_path, monkeypatch
    ):
        import usbinstaller.installers.macos as macos_module

        monkeypatch.setattr(macos_module, "_find_bundle", lambda root, preferred: None)
        monkeypatch.setattr(macos_module, "_find_package", lambda root: None)
        outcome = MacDmgInstaller().install(
            app(), payload(type="dmg"), tmp_path / "x.dmg",
            context(macos_system, self._runner(), workspace=tmp_path),
        )
        assert not outcome.success
        assert "app_bundle" in outcome.message

    def test_dry_run_mounts_nothing(self, macos_system, tmp_path):
        runner = self._runner()
        outcome = MacDmgInstaller().install(
            app(), payload(type="dmg"), tmp_path / "x.dmg",
            context(macos_system, runner, dry_run=True, workspace=tmp_path),
        )
        assert outcome.success
        assert runner.history == ()

    def test_app_installer_refuses_a_missing_destination(self, macos_system, tmp_path):
        with pytest.raises(InstallerExecutionError, match="destination directory"):
            MacAppInstaller().install(
                app(),
                payload(type="app", extra={"destination": str(tmp_path / "nope")}),
                tmp_path / "App.app",
                context(macos_system, self._runner()),
            )

    def test_app_installer_removes_only_this_bundles_quarantine_flag(
        self, macos_system, tmp_path
    ):
        applications = tmp_path / "Applications"
        applications.mkdir()
        bundle = tmp_path / "App.app"
        bundle.mkdir()
        runner = self._runner()
        outcome = MacAppInstaller().install(
            app(),
            payload(type="app", extra={"destination": str(applications)}),
            bundle,
            context(macos_system, runner),
        )
        assert outcome.success
        quarantine = [c for c in runner.history if "xattr" in c[0]]
        assert quarantine and str(applications / "App.app") in quarantine[0]


class TestProcessRunner:
    def test_runs_a_real_command_and_captures_output(self, tmp_path):
        runner = ProcessRunner()
        result = runner.run([sys.executable, "-c", "print('hello')"])
        assert result.exit_code == 0
        assert "hello" in result.stdout

    def test_non_zero_exit_code_is_returned_not_raised(self):
        result = ProcessRunner().run([sys.executable, "-c", "raise SystemExit(7)"])
        assert result.exit_code == 7
        assert not result.succeeded()
        assert result.succeeded([7])

    def test_timeout_terminates_and_reports(self):
        runner = ProcessRunner(default_timeout=1)
        result = runner.run([sys.executable, "-c", "import time; time.sleep(30)"])
        assert result.timed_out
        assert result.exit_code == -1

    def test_run_checked_raises_on_timeout(self):
        runner = ProcessRunner(default_timeout=1)
        with pytest.raises(InstallerTimeout):
            runner.run_checked([sys.executable, "-c", "import time; time.sleep(30)"])

    def test_run_checked_raises_on_failure(self):
        with pytest.raises(InstallerExecutionError, match="exited with code"):
            ProcessRunner().run_checked([sys.executable, "-c", "raise SystemExit(3)"])

    def test_empty_command_is_refused(self):
        with pytest.raises(InstallerExecutionError, match="empty command"):
            ProcessRunner().run([])

    def test_missing_executable_is_refused_before_execution(self, tmp_path):
        with pytest.raises(InstallerExecutionError, match="not found"):
            ProcessRunner().run([str(tmp_path / "nope.exe")])

    def test_unknown_system_tool_is_refused(self):
        with pytest.raises(InstallerExecutionError, match="PATH"):
            ProcessRunner().run(["definitely-not-a-real-tool-xyz"])

    def test_directory_is_not_executable(self, tmp_path):
        with pytest.raises(InstallerExecutionError, match="not an executable"):
            ProcessRunner().run([str(tmp_path)])

    def test_no_shell_is_used(self, tmp_path):
        """Shell metacharacters must be inert: they are literal arguments."""
        marker = tmp_path / "pwned.txt"
        result = ProcessRunner().run(
            [
                sys.executable,
                "-c",
                "import sys; print(sys.argv[1])",
                f"; touch {marker}",
            ]
        )
        assert result.exit_code == 0
        assert f"; touch {marker}" in result.stdout
        assert not marker.exists()

    def test_output_is_truncated(self):
        runner = ProcessRunner(capture_bytes=50)
        result = runner.run([sys.executable, "-c", "print('x' * 5000)"])
        assert len(result.stdout) < 200
        assert "truncated" in result.stdout

    def test_dangerous_environment_is_scrubbed(self, monkeypatch):
        monkeypatch.setenv("LD_PRELOAD", "/tmp/evil.so")
        result = ProcessRunner().run(
            [sys.executable, "-c", "import os; print(os.environ.get('LD_PRELOAD'))"]
        )
        assert result.stdout.strip() == "None"

    def test_dry_run_runner_executes_nothing(self, tmp_path):
        marker = tmp_path / "created.txt"
        runner = ProcessRunner(dry_run=True)
        result = runner.run([sys.executable, "-c", f"open(r'{marker}', 'w').close()"])
        assert result.exit_code == 0
        assert not marker.exists()
        assert runner.history

    def test_on_command_callback_receives_every_command(self):
        seen = []
        runner = ProcessRunner(dry_run=True, on_command=seen.append)
        runner.run([sys.executable, "-c", "pass"])
        assert seen and seen[0][0] == sys.executable


class TestPreflight:
    def test_missing_installer_is_caught(self, tmp_path):
        with pytest.raises(InstallerExecutionError, match="installer not found"):
            WindowsExeInstaller().preflight(tmp_path / "nope.exe", payload())

    def test_directory_where_a_file_is_expected(self, tmp_path):
        with pytest.raises(InstallerExecutionError, match="expected a file"):
            WindowsExeInstaller().preflight(tmp_path, payload())

    def test_app_bundles_may_be_directories(self, tmp_path):
        bundle = tmp_path / "App.app"
        bundle.mkdir()
        MacAppInstaller().preflight(bundle, payload(type="app", installer="a/App.app"))
