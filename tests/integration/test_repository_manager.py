"""Repository manager: inventory, checksum generation, drift reporting."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.conftest import app_entry, write_repository
from usbinstaller import repository_manager as rm
from usbinstaller.repository import Repository
from usbinstaller.repository_manager import RepositoryManager
from usbinstaller.security.checksums import sha256_file


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    write_repository(tmp_path, [app_entry("alpha"), app_entry("beta", macos=False)])
    return tmp_path


def manager(root: Path) -> RepositoryManager:
    return RepositoryManager(Repository.load(root))


class TestInventory:
    def test_counts_declared_installers_per_platform(self, repo):
        inventory = manager(repo).inventory()
        assert inventory["applications"] == 2
        assert inventory["installers_by_os"] == {"windows": 2, "macos": 1}
        assert inventory["missing"] == []

    def test_missing_installers_are_listed(self, repo):
        (repo / "installers/windows/alpha/setup.exe").unlink()
        inventory = manager(repo).inventory()
        assert inventory["missing"] == ["installers/windows/alpha/setup.exe"]

    def test_files_no_application_declares_are_flagged(self, repo):
        stray = repo / "installers" / "windows" / "leftover" / "old-setup.exe"
        stray.parent.mkdir(parents=True)
        stray.write_bytes(b"stale")
        assert "installers/windows/leftover/old-setup.exe" in manager(repo).inventory()[
            "orphaned"
        ]

    def test_app_bundles_count_as_one_artefact(self, tmp_path):
        write_repository(
            tmp_path,
            [
                app_entry(
                    "mac",
                    windows=False,
                    macos={"installer": "installers/macos/mac/App.app", "type": "app"},
                )
            ],
            create_installers=False,
        )
        bundle = tmp_path / "installers/macos/mac/App.app/Contents"
        bundle.mkdir(parents=True)
        (bundle / "Info.plist").write_bytes(b"<plist/>")
        present = manager(tmp_path).present_installer_files()
        assert present == ["installers/macos/mac/App.app"]


class TestChecksumGeneration:
    def test_records_a_digest_for_every_declared_installer(self, repo):
        report = manager(repo).generate_checksums()
        store = json.loads((repo / "checksums" / "checksums.json").read_text())
        assert report["written"] == 3
        assert len(store) == 3
        assert store["installers/windows/alpha/setup.exe"]["sha256"] == sha256_file(
            repo / "installers/windows/alpha/setup.exe"
        )

    def test_only_declared_files_are_blessed(self, repo):
        """A stray file dropped on the drive must not acquire a checksum."""
        stray = repo / "installers" / "windows" / "evil.exe"
        stray.write_bytes(b"malware")
        manager(repo).generate_checksums()
        store = json.loads((repo / "checksums" / "checksums.json").read_text())
        assert "installers/windows/evil.exe" not in store

    def test_changed_installers_are_reported_as_updates(self, repo):
        manager(repo).generate_checksums()
        (repo / "installers/windows/alpha/setup.exe").write_bytes(b"v2")
        report = manager(repo).generate_checksums()
        assert any("→" in line for line in report["lines"])

    def test_unchanged_installers_are_reported_as_unchanged(self, repo):
        manager(repo).generate_checksums()
        report = manager(repo).generate_checksums()
        assert all(
            line.startswith(("=", "-")) for line in report["lines"]
        ), report["lines"]

    def test_dry_run_writes_nothing(self, repo):
        manager(repo).generate_checksums(dry_run=True)
        assert not (repo / "checksums" / "checksums.json").exists()

    def test_prune_drops_entries_for_removed_installers(self, repo):
        manager(repo).generate_checksums()
        config = json.loads((repo / "config" / "applications.json").read_text())
        config["applications"] = [
            a for a in config["applications"] if a["id"] != "beta"
        ]
        (repo / "config" / "applications.json").write_text(json.dumps(config))

        report = manager(repo).generate_checksums(prune=True)
        assert report["removed"] == ["installers/windows/beta/setup.exe"]
        store = json.loads((repo / "checksums" / "checksums.json").read_text())
        assert "installers/windows/beta/setup.exe" not in store

    def test_missing_installer_is_an_error_not_a_crash(self, repo):
        (repo / "installers/windows/alpha/setup.exe").unlink()
        report = manager(repo).generate_checksums()
        assert report["errors"]
        assert any("file not found" in line for line in report["lines"])


class TestVerification:
    def test_verify_passes_for_an_intact_drive(self, repo):
        manager(repo).generate_checksums()
        assert manager(repo).verify_checksums()["ok"] is True

    def test_verify_detects_a_modified_installer(self, repo):
        manager(repo).generate_checksums()
        (repo / "installers/windows/alpha/setup.exe").write_bytes(b"tampered")
        report = manager(repo).verify_checksums()
        assert report["ok"] is False
        assert any(r["status"] == "mismatch" for r in report["results"])


class TestManagerCli:
    def test_status(self, repo, capsys):
        assert rm.main(["--repository", str(repo), "--status"]) == 0
        out = capsys.readouterr().out
        assert "USB Repository Manager" in out
        assert "Applications: 2" in out

    def test_status_reports_missing_installers_with_a_failure_code(self, repo, capsys):
        (repo / "installers/windows/alpha/setup.exe").unlink()
        assert rm.main(["--repository", str(repo), "--status"]) == 3

    def test_list(self, repo, capsys):
        assert rm.main(["--repository", str(repo), "--list"]) == 0
        assert "alpha" in capsys.readouterr().out

    def test_list_json(self, repo, capsys):
        rm.main(["--repository", str(repo), "--list", "--json"])
        assert len(json.loads(capsys.readouterr().out)) == 2

    def test_checksums_then_verify(self, repo, capsys):
        assert rm.main(["--repository", str(repo), "--checksums"]) == 0
        assert rm.main(["--repository", str(repo), "--verify-checksums"]) == 0

    def test_verify_checksums_fails_after_tampering(self, repo, capsys):
        rm.main(["--repository", str(repo), "--checksums"])
        (repo / "installers/windows/alpha/setup.exe").write_bytes(b"x")
        assert rm.main(["--repository", str(repo), "--verify-checksums"]) == 1

    def test_validate(self, repo, capsys):
        assert rm.main(["--repository", str(repo), "--validate"]) == 0

    def test_version(self, capsys):
        assert rm.main(["--version"]) == 0
        assert "repository manager" in capsys.readouterr().out

    def test_manager_never_touches_the_target_machine(self, repo, tmp_path, capsys):
        """Sanity check: the manager runs no subprocesses at all."""
        import subprocess

        calls = []
        original = subprocess.run

        def spy(*args, **kwargs):
            calls.append(args)
            return original(*args, **kwargs)

        subprocess.run = spy
        try:
            rm.main(["--repository", str(repo), "--checksums"])
            rm.main(["--repository", str(repo), "--status"])
            rm.main(["--repository", str(repo), "--validate"])
        finally:
            subprocess.run = original
        assert calls == []
