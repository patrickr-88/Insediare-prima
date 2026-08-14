"""Per-application information shown in the GUI's details panel."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import app_entry, registry_entry, windows_probe, write_repository
from usbinstaller.app import InstallerApp
from usbinstaller.engine.details import describe, format_size
from usbinstaller.models import InstalledApp
from usbinstaller.repository import Repository
from usbinstaller.security.checksums import sha256_file
from usbinstaller.versioning import Comparison

DETECT = {"method": "windows_registry", "display_name": "^Firefox"}


@pytest.fixture
def repo(tmp_path: Path) -> Repository:
    write_repository(
        tmp_path,
        [
            app_entry(
                "firefox",
                name="Firefox",
                version="141.0",
                description="Web browser",
                macos=False,
                version_scheme="numeric",
                detection=DETECT,
                windows={
                    "installer": "installers/windows/firefox/setup.exe",
                    "type": "exe",
                    "arguments": ["/S"],
                    "architectures": ["x64"],
                    "requires_admin": True,
                },
            ),
            app_entry("maconly", windows=False),
            app_entry("disabled", enabled=False),
        ],
    )
    return Repository.load(tmp_path)


def details_for(repo, system, app_id, installed=None, **kwargs):
    application = repo.catalogue.get(app_id)
    return describe(application, repo, system, installed, **kwargs)


class TestStaticFacts:
    def test_installer_facts_are_reported(self, repo, windows_system):
        details = details_for(repo, windows_system, "firefox")
        assert details.name == "Firefox"
        assert details.version == "141.0"
        assert details.installer_path == "installers/windows/firefox/setup.exe"
        assert details.installer_exists is True
        assert details.installer_size > 0
        assert details.installer_type == "exe"
        assert details.arguments == ("/S",)
        assert details.requires_admin is True
        assert details.architectures == ("x64",)
        assert details.detection_method == "windows_registry"

    def test_missing_installer_is_visible(self, repo, windows_system):
        repo.resolve("installers/windows/firefox/setup.exe").unlink()
        details = details_for(repo, windows_system, "firefox")
        assert details.installer_exists is False
        assert details.status_line == "Installer missing from the drive"

    def test_size_formatting(self):
        assert format_size(None) == "—"
        assert format_size(512) == "512 B"
        assert format_size(2048) == "2.0 KB"
        assert format_size(5 * 1024**2) == "5.0 MB"
        assert format_size(3 * 1024**3) == "3.0 GB"


class TestCompatibility:
    def test_compatible_application(self, repo, windows_system):
        details = details_for(repo, windows_system, "firefox")
        assert details.compatible is True
        assert details.incompatibility is None

    def test_wrong_platform(self, repo, windows_system):
        details = details_for(repo, windows_system, "maconly")
        assert details.compatible is False
        assert "No Windows installer" in details.incompatibility
        assert details.status_line == details.incompatibility

    def test_wrong_architecture(self, repo):
        from usbinstaller.sysdetect.system import detect_system

        system = detect_system(windows_probe(machine="x86"))
        details = details_for(repo, system, "firefox")
        assert details.compatible is False
        assert "Requires x64" in details.incompatibility
        assert "this computer is x86" in details.incompatibility

    def test_disabled_application(self, repo, windows_system):
        details = details_for(repo, windows_system, "disabled")
        assert details.compatible is False
        assert "Disabled" in details.incompatibility


class TestChecksumState:
    def test_missing_checksum(self, repo, windows_system):
        assert details_for(repo, windows_system, "firefox").checksum_state == "missing"

    def test_recorded_checksum_is_not_re_hashed_unless_asked(self, tmp_path, windows_system):
        write_repository(
            tmp_path,
            [app_entry("a", macos=False)],
            checksums={"installers/windows/a/setup.exe": {"sha256": "0" * 64}},
        )
        repo = Repository.load(tmp_path)
        assert details_for(repo, windows_system, "a").checksum_state == "recorded"

    def test_verification_detects_a_good_file(self, tmp_path, windows_system):
        write_repository(tmp_path, [app_entry("a", macos=False)])
        digest = sha256_file(tmp_path / "installers/windows/a/setup.exe")
        write_repository(
            tmp_path,
            [app_entry("a", macos=False)],
            checksums={"installers/windows/a/setup.exe": {"sha256": digest}},
        )
        repo = Repository.load(tmp_path)
        details = details_for(repo, windows_system, "a", verify_checksum=True)
        assert details.checksum_state == "verified"

    def test_verification_detects_tampering(self, tmp_path, windows_system):
        write_repository(
            tmp_path,
            [app_entry("a", macos=False)],
            checksums={"installers/windows/a/setup.exe": {"sha256": "0" * 64}},
        )
        repo = Repository.load(tmp_path)
        details = details_for(repo, windows_system, "a", verify_checksum=True)
        assert details.checksum_state == "mismatch"
        assert "will not be run" in details.status_line


class TestInstalledState:
    def test_not_installed(self, repo, windows_system):
        details = details_for(
            repo, windows_system, "firefox", InstalledApp(installed=False)
        )
        assert details.status_line == "Not installed"
        assert details.comparison is Comparison.UNKNOWN

    def test_up_to_date(self, repo, windows_system):
        details = details_for(
            repo, windows_system, "firefox", InstalledApp(True, version="141.0")
        )
        assert details.comparison is Comparison.SAME
        assert details.status_line == "Up to date (141.0)"

    def test_update_available(self, repo, windows_system):
        details = details_for(
            repo, windows_system, "firefox", InstalledApp(True, version="140.0")
        )
        assert details.comparison is Comparison.OLDER
        assert details.status_line == "Update available (140.0 → 141.0)"

    def test_newer_installed(self, repo, windows_system):
        details = details_for(
            repo, windows_system, "firefox", InstalledApp(True, version="142.0")
        )
        assert details.status_line == "Newer version installed (142.0)"

    def test_uncomparable_versions(self, repo, windows_system):
        details = details_for(
            repo, windows_system, "firefox", InstalledApp(True, version="Gold")
        )
        assert "not comparable" in details.status_line


class TestDependencies:
    def test_missing_dependencies_are_flagged(self, tmp_path, windows_system):
        write_repository(tmp_path, [app_entry("a", dependencies=["ghost", "b"]), app_entry("b")])
        repo = Repository.load(tmp_path)
        details = details_for(repo, windows_system, "a")
        assert details.dependencies == ("ghost", "b")
        assert details.missing_dependencies == ("ghost",)


class TestRendering:
    def test_render_contains_the_facts_a_technician_needs(self, repo, windows_system):
        details = details_for(
            repo,
            windows_system,
            "firefox",
            InstalledApp(True, version="140.0", location="C:\\Program Files\\Firefox"),
        )
        text = details.render()
        assert "Firefox  141.0" in text
        assert "Update available (140.0 → 141.0)" in text
        assert "Web browser" in text
        assert "installers/windows/firefox/setup.exe" in text
        assert "/S" in text
        assert "required" in text  # administrator
        assert "C:\\Program Files\\Firefox" in text
        assert "not recorded" in text  # checksum guidance

    def test_render_without_optional_information(self, repo, windows_system):
        text = details_for(repo, windows_system, "maconly").render()
        assert "No Windows installer" in text
        assert "—" in text  # no installer, no size

    def test_to_dict_is_json_friendly(self, repo, windows_system):
        import json

        payload = details_for(
            repo, windows_system, "firefox", InstalledApp(True, version="140.0")
        ).to_dict()
        assert json.loads(json.dumps(payload))["comparison"] == "older"
        assert payload["status"].startswith("Update available")


class TestServiceLayer:
    def test_details_for_runs_detection(self, repo, tmp_path):
        app = InstallerApp.create(repo.root, probe=windows_probe())
        app.detection.registry_reader = lambda: [registry_entry("Firefox", "140.0")]
        details = app.details_for("firefox")
        assert details.installed.installed is True
        assert details.comparison is Comparison.OLDER

    def test_details_can_skip_detection(self, repo):
        app = InstallerApp.create(repo.root, probe=windows_probe())
        details = app.details_for("firefox", detect=False)
        assert details.installed is None

    def test_all_details_covers_every_entry_including_incompatible_ones(self, repo):
        app = InstallerApp.create(repo.root, probe=windows_probe())
        ids = {d.id for d in app.all_details()}
        assert ids == {"firefox", "maconly", "disabled"}

    def test_unknown_id_raises(self, repo):
        from usbinstaller.errors import ConfigSchemaError

        app = InstallerApp.create(repo.root, probe=windows_probe())
        with pytest.raises(ConfigSchemaError):
            app.details_for("ghost")

    def test_reload_picks_up_a_catalogue_edit(self, repo, tmp_path):
        app = InstallerApp.create(repo.root, probe=windows_probe())
        assert len(app.repository.catalogue) == 3

        app.editor.remove_application("disabled")
        assert len(app.repository.catalogue) == 3  # the old view is unchanged
        assert len(app.reload().repository.catalogue) == 2

    def test_validate_returns_a_report(self, repo):
        app = InstallerApp.create(repo.root, probe=windows_probe())
        assert app.validate().ok is True
