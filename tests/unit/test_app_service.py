"""The service layer that both front-ends drive, plus platform helpers."""

from __future__ import annotations

import platform as stdlib_platform
import sys
from pathlib import Path

import pytest

from tests.conftest import app_entry, windows_probe, write_repository
from usbinstaller.app import InstallerApp
from usbinstaller.installers.macos import _find_bundle, _find_package, _rmdir
from usbinstaller.installers.process import CommandResult, RecordingRunner
from usbinstaller.models import OS
from usbinstaller.repository import Repository
from usbinstaller.sysdetect.privileges import (
    can_write,
    drop_privileges_warning,
    sudo_available,
)
from usbinstaller.sysdetect.system import SystemProbe, running_frozen


class TestInstallerApp:
    def test_create_detects_the_machine_and_loads_the_repository(self, tmp_path):
        write_repository(tmp_path, [app_entry("a")])
        app = InstallerApp.create(tmp_path, probe=windows_probe())
        assert app.system.os is OS.WINDOWS
        assert app.repository.root == tmp_path.resolve()
        assert [a.id for a in app.available()] == ["a"]

    def test_dry_run_propagates_to_an_injected_runner(self, tmp_path):
        write_repository(tmp_path, [app_entry("a")])
        runner = RecordingRunner()
        app = InstallerApp(
            repository=Repository.load(tmp_path),
            system=InstallerApp.create(tmp_path, probe=windows_probe()).system,
            dry_run=True,
            runner=runner,
        )
        assert app.runner.dry_run is True

    def test_with_log_creates_a_session_and_writes_system_json(self, tmp_path):
        write_repository(tmp_path, [app_entry("a")])
        app = InstallerApp.create(tmp_path, probe=windows_probe(), with_log=True)
        assert (app.log.directory / "system.json").exists()
        app.log.close()

    def test_plan_is_written_to_the_log(self, tmp_path):
        write_repository(tmp_path, [app_entry("a")])
        app = InstallerApp.create(tmp_path, probe=windows_probe(), with_log=True)
        app.plan()
        assert (app.log.directory / "plan.json").exists()
        app.log.close()

    def test_default_selection_respects_selected_by_default(self, tmp_path):
        write_repository(
            tmp_path,
            [app_entry("on"), app_entry("off", selected_by_default=False)],
        )
        app = InstallerApp.create(tmp_path, probe=windows_probe())
        assert app.planner.default_selection() == ["on"]

    def test_previous_failures_is_empty_without_any_logs(self, tmp_path):
        write_repository(tmp_path, [app_entry("a")])
        app = InstallerApp.create(tmp_path, probe=windows_probe())
        assert app.previous_failures() == []

    def test_retry_plan_narrows_an_existing_plan(self, tmp_path):
        write_repository(tmp_path, [app_entry("a"), app_entry("b")])
        app = InstallerApp.create(tmp_path, probe=windows_probe())
        plan = app.plan()
        assert [i.app_id for i in app.retry_plan(plan, ["b"]).items] == ["b"]

    def test_log_retention_prunes_old_sessions(self, tmp_path):
        write_repository(tmp_path, [app_entry("a")], settings={"log_retention": 1})
        for name in ("2020-01-01_00-00-00", "2020-01-02_00-00-00"):
            stale = tmp_path / "logs" / name
            stale.mkdir(parents=True)
            (stale / "installation.log").write_text("old")
        app = InstallerApp.create(tmp_path, probe=windows_probe(), with_log=True)
        remaining = sorted(p.name for p in (tmp_path / "logs").iterdir())
        app.log.close()
        assert "2020-01-01_00-00-00" not in remaining

    def test_detection_runner_is_live_even_in_dry_run(self, tmp_path):
        """A dry run must still see what is really installed."""
        write_repository(tmp_path, [app_entry("a")])
        app = InstallerApp.create(tmp_path, probe=windows_probe(), dry_run=True)
        assert app.runner.dry_run is True
        assert app.detection.runner.dry_run is False


class TestRepositoryHelpers:
    def test_is_writable(self, tmp_path):
        write_repository(tmp_path, [app_entry("a")])
        assert Repository.load(tmp_path).is_writable() is True

    def test_logs_dir_follows_the_setting(self, tmp_path):
        write_repository(
            tmp_path, [app_entry("a")], settings={"log_directory": "custom-logs"}
        )
        assert Repository.load(tmp_path).logs_dir.name == "custom-logs"

    def test_relative_falls_back_for_paths_outside_the_root(self, tmp_path):
        write_repository(tmp_path, [app_entry("a")])
        repository = Repository.load(tmp_path)
        assert repository.relative(Path("/somewhere/else")) == "/somewhere/else"

    def test_load_rejects_a_file_as_a_root(self, tmp_path):
        from usbinstaller.errors import RepositoryError

        target = tmp_path / "file.txt"
        target.write_text("x")
        with pytest.raises(RepositoryError, match="not a directory"):
            Repository.load(target)


class TestMacHelpers:
    def test_find_bundle_prefers_the_configured_name(self, tmp_path):
        (tmp_path / "Other.app").mkdir()
        (tmp_path / "Firefox.app").mkdir()
        assert _find_bundle(tmp_path, "Firefox.app").name == "Firefox.app"

    def test_find_bundle_returns_none_when_the_configured_name_is_absent(self, tmp_path):
        assert _find_bundle(tmp_path, "Missing.app") is None

    def test_find_bundle_picks_the_only_bundle(self, tmp_path):
        (tmp_path / "Solo.app").mkdir()
        assert _find_bundle(tmp_path, None).name == "Solo.app"

    def test_find_bundle_with_nothing_to_find(self, tmp_path):
        assert _find_bundle(tmp_path, None) is None
        assert _find_bundle(tmp_path / "gone", None) is None

    def test_find_package(self, tmp_path):
        (tmp_path / "Payload.pkg").write_bytes(b"x")
        assert _find_package(tmp_path).name == "Payload.pkg"
        assert _find_package(tmp_path / "gone") is None

    def test_rmdir_is_forgiving(self, tmp_path):
        _rmdir(tmp_path / "never-existed")

    def test_zip_archive_is_expanded_and_copied(self, tmp_path, macos_system, monkeypatch):
        import usbinstaller.installers.macos as module
        from usbinstaller.installers import MacAppInstaller
        from usbinstaller.installers.base import InstallContext
        from usbinstaller.models import PlatformPayload

        applications = tmp_path / "Applications"
        applications.mkdir()
        archive = tmp_path / "VSCode.zip"
        archive.write_bytes(b"PK")
        monkeypatch.setattr(module, "_find_bundle", lambda root, p: root / "Code.app")

        runner = RecordingRunner(default_result=CommandResult(("x",), exit_code=0))
        outcome = MacAppInstaller().install(
            None,
            PlatformPayload(
                installer="i/VSCode.zip",
                type="app",
                extra={"destination": str(applications)},
            ),
            archive,
            InstallContext(
                system=macos_system, runner=runner, repository_root=tmp_path
            ),
        )
        assert outcome.success
        assert any("-x" in c and "-k" in c for c in runner.history)

    def test_failed_archive_expansion_is_reported(self, tmp_path, macos_system):
        from usbinstaller.installers import MacAppInstaller
        from usbinstaller.installers.base import InstallContext
        from usbinstaller.models import PlatformPayload

        applications = tmp_path / "Applications"
        applications.mkdir()
        archive = tmp_path / "broken.zip"
        archive.write_bytes(b"not a zip")
        runner = RecordingRunner(default_result=CommandResult(("ditto",), exit_code=1))

        outcome = MacAppInstaller().install(
            None,
            PlatformPayload(
                installer="i/broken.zip",
                type="app",
                extra={"destination": str(applications)},
            ),
            archive,
            InstallContext(system=macos_system, runner=runner, repository_root=tmp_path),
        )
        assert not outcome.success
        assert "expand archive" in outcome.message

    def test_archive_without_a_bundle_is_reported(self, tmp_path, macos_system, monkeypatch):
        import usbinstaller.installers.macos as module
        from usbinstaller.installers import MacAppInstaller
        from usbinstaller.installers.base import InstallContext
        from usbinstaller.models import PlatformPayload

        applications = tmp_path / "Applications"
        applications.mkdir()
        archive = tmp_path / "empty.zip"
        archive.write_bytes(b"PK")
        monkeypatch.setattr(module, "_find_bundle", lambda root, p: None)
        runner = RecordingRunner(default_result=CommandResult(("ditto",), exit_code=0))

        outcome = MacAppInstaller().install(
            None,
            PlatformPayload(
                installer="i/empty.zip",
                type="app",
                extra={"destination": str(applications)},
            ),
            archive,
            InstallContext(system=macos_system, runner=runner, repository_root=tmp_path),
        )
        assert not outcome.success
        assert "no .app bundle" in outcome.message

    def test_dry_run_does_not_expand_archives(self, tmp_path, macos_system):
        from usbinstaller.installers import MacAppInstaller
        from usbinstaller.installers.base import InstallContext
        from usbinstaller.models import PlatformPayload

        runner = RecordingRunner()
        outcome = MacAppInstaller().install(
            None,
            PlatformPayload(installer="i/x.zip", type="app"),
            tmp_path / "x.zip",
            InstallContext(
                system=macos_system,
                runner=runner,
                repository_root=tmp_path,
                dry_run=True,
            ),
        )
        assert outcome.success and runner.history == ()


class TestRealSystemProbe:
    """The probe's own methods, exercised against the host they wrap."""

    def test_probe_matches_the_standard_library(self):
        probe = SystemProbe()
        assert probe.system() == stdlib_platform.system()
        assert probe.machine() == stdlib_platform.machine()
        assert probe.release() == stdlib_platform.release()
        assert probe.version() == stdlib_platform.version()
        assert probe.python_version() == stdlib_platform.python_version()

    def test_hostname_and_disk_are_readable(self):
        probe = SystemProbe()
        assert isinstance(probe.hostname(), str) and probe.hostname()
        assert probe.disk_free(str(Path.home())) > 0

    def test_mac_ver_and_win32_edition_never_raise(self):
        probe = SystemProbe()
        assert isinstance(probe.mac_ver(), tuple)
        assert probe.win32_edition() is None or isinstance(probe.win32_edition(), str)

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX only")
    def test_geteuid_on_posix(self):
        assert isinstance(SystemProbe().geteuid(), int)

    def test_is_translated_is_false_off_apple_silicon(self):
        # sysctl is absent or returns nothing on Linux/Windows.
        assert SystemProbe().is_translated() in (False, True)

    def test_running_frozen_is_false_during_tests(self):
        assert running_frozen() is False


class TestPrivilegeHelpers:
    def test_can_write(self, tmp_path):
        assert can_write(tmp_path) is True
        assert can_write(tmp_path / "does-not-exist") is False

    def test_sudo_available_returns_a_bool(self):
        assert isinstance(sudo_available(), bool)

    def test_root_warning_only_appears_under_sudo(self, monkeypatch, macos_system):
        monkeypatch.delenv("SUDO_USER", raising=False)
        assert drop_privileges_warning(macos_system) is None

        monkeypatch.setenv("SUDO_USER", "technician")
        message = drop_privileges_warning(macos_system)
        assert message is not None and "root" in message

    def test_no_warning_on_windows(self, monkeypatch, windows_system):
        monkeypatch.setenv("SUDO_USER", "technician")
        assert drop_privileges_warning(windows_system) is None
