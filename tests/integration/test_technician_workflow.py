"""End-to-end technician workflow against a simulated USB drive.

This is the scenario from the specification: a drive carrying eight
applications is plugged into a machine, and the tool must detect the platform,
validate the drive, work out what is already installed, plan, install,
tolerate a failure, verify, log and report.

The "installers" are shell scripts that write marker files into a fake target
directory; detection reads those markers back. No vendor software is involved
and nothing outside the temporary directory is touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.conftest import macos_probe, windows_probe
from usbinstaller.app import InstallerApp
from usbinstaller.config.validator import validate_repository
from usbinstaller.engine.executor import render_results
from usbinstaller.engine.planner import render_plan
from usbinstaller.models import Action, Status
from usbinstaller.repository import Repository
from usbinstaller.repository_manager import RepositoryManager

APPLICATIONS = [
    ("firefox", "Mozilla Firefox", "141.0", "Browsers", 0),
    ("chrome", "Google Chrome", "127.0", "Browsers", 0),
    ("7zip", "7-Zip", "25.01", "Utilities", 0),
    ("vlc", "VLC", "3.0.21", "Media", 0),
    ("vscode", "Visual Studio Code", "1.92.0", "Development", 0),
    ("adobe-reader", "Adobe Acrobat Reader", "24.002", "Documents", 1),  # fails
    ("zoom", "Zoom", "6.1.6", "Communication", 0),
    ("notepadplusplus", "Notepad++", "8.6.9", "Utilities", 0),
]

#: Notepad++ is Windows-only, mirroring the real catalogue.
WINDOWS_ONLY = {"notepadplusplus", "7zip"}


@pytest.fixture
def usb_drive(tmp_path: Path) -> tuple[Path, Path]:
    """Create a simulated USB repository and a fake 'installed software' area."""
    root = tmp_path / "USB_INSTALLER"
    target = tmp_path / "target"
    target.mkdir()

    entries = []
    for app_id, name, version, category, exit_code in APPLICATIONS:
        entry = {
            "id": app_id,
            "name": name,
            "version": version,
            "category": category,
            "version_scheme": "numeric",
            # The marker file contains the installed version, so detection
            # exercises the same version-comparison path as a real machine.
            "detection": {
                "method": "command_version",
                "command": ["/bin/cat", str(target / f"{app_id}.marker")],
            },
        }
        for os_key in ("windows", "macos"):
            if os_key == "macos" and app_id in WINDOWS_ONLY:
                continue
            suffix = "ps1" if os_key == "windows" else "sh"
            relative = f"installers/{os_key}/{app_id}/install.{suffix}"
            script = root / relative
            script.parent.mkdir(parents=True, exist_ok=True)
            # Both variants are shell scripts; on Windows the engine would run
            # the .ps1 through PowerShell, which this suite does not exercise.
            script.write_text(
                f"#!/bin/sh\necho 'installer for {app_id} failed' >&2\nexit 1\n"
                if exit_code
                else f"#!/bin/sh\necho '{version}' > '{target}/{app_id}.marker'\n"
            )
            script.chmod(0o755)
            entry[os_key] = {
                "installer": relative,
                "type": "powershell" if os_key == "windows" else "shell",
                "requires_admin": False,
            }
        entries.append(entry)

    # 7-Zip depends on a runtime, to exercise ordering end to end.
    runtime = {
        "id": "vcruntime",
        "name": "Visual C++ Runtime",
        "version": "14.40",
        "category": "Runtimes",
        "detection": {
            "method": "command_version",
            "command": ["/bin/cat", str(target / "vcruntime.marker")],
        },
        "macos": {
            "installer": "installers/macos/vcruntime/install.sh",
            "type": "shell",
            "requires_admin": False,
        },
    }
    script = root / "installers/macos/vcruntime/install.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(f"#!/bin/sh\necho '14.40' > '{target}/vcruntime.marker'\n")
    script.chmod(0o755)
    entries.append(runtime)
    for entry in entries:
        if entry["id"] == "vlc":
            entry["dependencies"] = ["vcruntime"]

    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "applications.json").write_text(
        json.dumps({"schema_version": 1, "applications": entries}, indent=2)
    )
    (root / "config" / "settings.json").write_text(
        json.dumps({"require_confirmation": False, "continue_on_failure": True})
    )
    (root / "checksums").mkdir(exist_ok=True)
    RepositoryManager(Repository.load(root)).generate_checksums()
    return root, target


def macos_app(root: Path, **kwargs) -> InstallerApp:
    return InstallerApp.create(root, probe=macos_probe(), **kwargs)


class TestFullWorkflow:
    def test_step_1_to_5_detect_locate_and_validate(self, usb_drive):
        root, _ = usb_drive
        app = macos_app(root)

        assert app.system.os.value == "macos"
        assert app.system.arch.value == "arm64"
        assert Path(app.system.repository_path) == root

        report = validate_repository(root, check_checksums=True)
        assert report.ok, report.render()
        assert not report.warnings, report.render()

    def test_step_6_to_9_plan_shows_what_would_happen(self, usb_drive):
        root, target = usb_drive
        # Pretend Chrome is already installed, at a newer version than the USB.
        (target / "chrome.marker").write_text("200.0\n")

        app = macos_app(root)
        plan = app.plan()
        actions = {i.app_id: i.action for i in plan.items}

        assert actions["firefox"] is Action.INSTALL
        assert actions["chrome"] is Action.SKIP
        assert "newer version already installed" in next(
            i.reason for i in plan.items if i.app_id == "chrome"
        )
        # Windows-only applications are not offered on macOS at all.
        assert "notepadplusplus" not in actions

        text = render_plan(plan)
        assert "INSTALLATION PLAN" in text
        assert "[SKIP]" in text

    def test_dependencies_are_installed_before_their_dependents(self, usb_drive):
        root, _ = usb_drive
        plan = macos_app(root).plan(["vlc"])
        assert [i.app_id for i in plan.items] == ["vcruntime", "vlc"]

    def test_step_10_to_15_install_survive_a_failure_and_report(self, usb_drive):
        root, target = usb_drive
        app = macos_app(root, with_log=True)
        plan = app.plan()
        result = app.install(plan)

        # The deliberately broken installer failed; everything else succeeded.
        assert result.failed == 1
        assert [i.app_id for i in result.failures] == ["adobe-reader"]
        assert result.succeeded >= 5

        # Applications after the failure still ran.
        assert (target / "zoom.marker").exists()

        report = render_results(result, app.log.directory)
        assert "Successful:" in report
        assert "adobe-reader" in report

        # A complete log directory was produced.
        results = json.loads((app.log.directory / "results.json").read_text())
        assert results["summary"]["failed"] == 1
        failure = next(i for i in results["items"] if i["id"] == "adobe-reader")
        assert failure["exit_code"] == 1
        assert failure["started_at"] and failure["finished_at"]
        system = json.loads((app.log.directory / "system.json").read_text())
        assert system["system"]["os"] == "macos"

    def test_verification_catches_an_installer_that_lies_about_success(self, usb_drive):
        """An installer that exits 0 without installing must not be reported OK."""
        root, target = usb_drive
        liar = root / "installers/macos/zoom/install.sh"
        liar.write_text("#!/bin/sh\nexit 0\n")
        liar.chmod(0o755)
        RepositoryManager(Repository.load(root)).generate_checksums()

        result = macos_app(root).install(macos_app(root).plan(["zoom"]))
        item = result.items[0]
        assert item.status is Status.FAILED
        assert "not detected afterwards" in item.error

    def test_rerun_skips_what_is_already_installed(self, usb_drive):
        root, _ = usb_drive
        first = macos_app(root)
        first.install(first.plan())

        second = macos_app(root)
        plan = second.plan()
        actions = {i.app_id: i.action for i in plan.items}
        assert actions["firefox"] is Action.SKIP
        assert actions["adobe-reader"] is Action.INSTALL  # it never landed

    def test_upgrade_is_offered_when_the_usb_is_newer(self, usb_drive):
        root, target = usb_drive
        (target / "firefox.marker").write_text("140.0\n")
        plan = macos_app(root).plan(["firefox"])
        assert plan.items[0].action is Action.UPGRADE
        assert "140.0 → USB 141.0" in plan.items[0].reason

    def test_retry_reruns_only_the_failures(self, usb_drive):
        root, target = usb_drive
        app = macos_app(root, with_log=True)
        app.install(app.plan())

        assert app.previous_failures() == ["adobe-reader"]

        # Repair the broken installer, then retry.
        fixed = root / "installers/macos/adobe-reader/install.sh"
        fixed.write_text(
            f"#!/bin/sh\necho '24.002' > '{target}/adobe-reader.marker'\n"
        )
        fixed.chmod(0o755)
        RepositoryManager(Repository.load(root)).generate_checksums()

        retry_app = macos_app(root, with_log=True)
        failures = retry_app.previous_failures()
        result = retry_app.install(retry_app.plan(failures))
        assert result.failed == 0
        assert (target / "adobe-reader.marker").exists()

    def test_tampered_installer_is_refused(self, usb_drive):
        """A drive whose contents changed after checksumming must not execute."""
        root, target = usb_drive
        tampered = root / "installers/macos/firefox/install.sh"
        tampered.write_text(f"#!/bin/sh\ntouch '{target}/PWNED'\n")
        tampered.chmod(0o755)

        app = macos_app(root)
        item = app.plan(["firefox"]).items[0]
        assert item.action is Action.ERROR
        assert "MISMATCH" in item.reason

        result = app.install(app.plan(["firefox"]))
        assert result.failed == 1
        assert not (target / "PWNED").exists()


class TestWindowsSideOfTheWorkflow:
    def test_windows_sees_the_windows_only_applications(self, usb_drive):
        root, _ = usb_drive
        app = InstallerApp.create(root, probe=windows_probe())
        available = {a.id for a in app.available()}
        assert "notepadplusplus" in available
        assert "vcruntime" not in available  # macOS-only in this fixture

    def test_windows_plan_uses_the_windows_installer(self, usb_drive):
        root, _ = usb_drive
        app = InstallerApp.create(root, probe=windows_probe(), dry_run=True)
        item = next(i for i in app.plan().items if i.app_id == "firefox")
        assert item.installer_path.startswith("installers/windows/")
        assert item.action is Action.INSTALL
