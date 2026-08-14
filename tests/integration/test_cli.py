"""Command-line interface behaviour and exit codes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.conftest import app_entry, macos_probe, write_repository
from usbinstaller import cli
from usbinstaller.repository import Repository
from usbinstaller.repository_manager import RepositoryManager


@pytest.fixture
def usb(tmp_path: Path) -> Path:
    """A small repository whose macOS installers are runnable shell scripts."""
    root = tmp_path / "usb"
    target = tmp_path / "target"
    target.mkdir()
    entries = []
    for app_id, version, code in (("alpha", "1.0", 0), ("beta", "2.0", 0), ("broken", "1.0", 1)):
        relative = f"installers/macos/{app_id}/install.sh"
        script = root / relative
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(
            "#!/bin/sh\nexit 1\n"
            if code
            else f"#!/bin/sh\necho '{version}' > '{target}/{app_id}.marker'\n"
        )
        script.chmod(0o755)
        entries.append(
            {
                "id": app_id,
                "name": app_id.title(),
                "version": version,
                "category": "Testing",
                "macos": {"installer": relative, "type": "shell", "requires_admin": False},
            }
        )
    write_repository(root, entries, settings={"require_confirmation": False},
                     create_installers=False)
    RepositoryManager(Repository.load(root)).generate_checksums()
    return root


def patch_platform(monkeypatch, probe):
    """Make every code path believe it is running on the given machine."""
    import usbinstaller.app as app_module
    from usbinstaller.sysdetect.system import detect_system as real_detect

    monkeypatch.setattr(
        app_module,
        "detect_system",
        lambda _probe=None, repository_path=None: real_detect(
            probe, repository_path=repository_path
        ),
    )


@pytest.fixture(autouse=True)
def force_macos(monkeypatch):
    """Every CLI test runs as if it were on an Apple Silicon Mac."""
    patch_platform(monkeypatch, macos_probe())


def run(args, capsys) -> tuple[int, str]:
    code = cli.main(args)
    return code, capsys.readouterr().out


class TestInformationalCommands:
    def test_version(self, capsys):
        code, out = run(["--version"], capsys)
        assert code == cli.EXIT_OK
        assert "USB Software Installer" in out

    def test_info(self, usb, capsys):
        code, out = run(["--repository", str(usb), "--info"], capsys)
        assert code == cli.EXIT_OK
        assert "Apple Silicon" in out

    def test_info_as_json(self, usb, capsys):
        code, out = run(["--repository", str(usb), "--info", "--json"], capsys)
        assert json.loads(out)["os"] == "macos"

    def test_list(self, usb, capsys):
        code, out = run(["--repository", str(usb), "--list"], capsys)
        assert code == cli.EXIT_OK
        assert "alpha" in out and "beta" in out
        assert "3 application(s)" in out

    def test_list_as_json(self, usb, capsys):
        code, out = run(["--repository", str(usb), "--list", "--json"], capsys)
        assert {entry["id"] for entry in json.loads(out)} == {"alpha", "beta", "broken"}

    def test_no_command_prints_help_and_system(self, usb, capsys):
        code, out = run(["--repository", str(usb)], capsys)
        assert code == cli.EXIT_OK
        assert "usage:" in out
        assert "Computer:" in out


class TestValidation:
    def test_validate_clean_repository(self, usb, capsys):
        code, out = run(["--repository", str(usb), "--validate-config"], capsys)
        assert code == cli.EXIT_OK
        assert "Validation passed" in out

    def test_validate_broken_repository_exits_three(self, tmp_path, capsys):
        write_repository(tmp_path, [app_entry("a")], create_installers=False)
        code, out = run(["--repository", str(tmp_path), "--validate-config"], capsys)
        assert code == cli.EXIT_INVALID_CONFIG
        assert "FAILED" in out

    def test_verify_rehashes_installers(self, usb, capsys):
        code, out = run(["--repository", str(usb), "--verify"], capsys)
        assert code == cli.EXIT_OK
        assert "checksum verified" in out

    def test_verify_detects_tampering(self, usb, capsys):
        (usb / "installers/macos/alpha/install.sh").write_text("#!/bin/sh\nexit 0\n")
        code, out = run(["--repository", str(usb), "--verify"], capsys)
        assert code == cli.EXIT_INVALID_CONFIG
        assert "MISMATCH" in out

    def test_validate_as_json(self, usb, capsys):
        code, out = run(["--repository", str(usb), "--validate-config", "--json"], capsys)
        assert json.loads(out)["ok"] is True

    def test_missing_repository_is_a_usage_error(self, tmp_path, capsys, monkeypatch):
        monkeypatch.delenv("USB_INSTALLER_ROOT", raising=False)
        empty = tmp_path / "empty"
        empty.mkdir()
        code = cli.main(["--repository", str(empty), "--list"])
        assert code == cli.EXIT_USAGE


class TestPlanningAndDryRun:
    def test_plan_shows_the_plan_and_installs_nothing(self, usb, tmp_path, capsys):
        code, out = run(["--repository", str(usb), "--install-all", "--plan"], capsys)
        assert code == cli.EXIT_OK
        assert "INSTALLATION PLAN" in out
        assert not (tmp_path / "target" / "alpha.marker").exists()

    def test_dry_run_installs_nothing(self, usb, tmp_path, capsys):
        code, out = run(
            ["--repository", str(usb), "--install-all", "--dry-run"], capsys
        )
        assert code == cli.EXIT_OK
        assert "DRY RUN" in out
        assert "No changes were made" in out
        assert not (tmp_path / "target" / "alpha.marker").exists()

    def test_skip_removes_applications_from_the_run(self, usb, tmp_path, capsys):
        code, out = run(
            [
                "--repository",
                str(usb),
                "--install",
                "alpha",
                "beta",
                "--skip",
                "beta",
                "--yes",
            ],
            capsys,
        )
        assert code == cli.EXIT_OK
        assert (tmp_path / "target" / "alpha.marker").exists()
        assert not (tmp_path / "target" / "beta.marker").exists()


class TestInstallation:
    def test_install_named_applications(self, usb, tmp_path, capsys):
        code, out = run(
            ["--repository", str(usb), "--install", "alpha", "--yes"], capsys
        )
        assert code == cli.EXIT_OK
        assert (tmp_path / "target" / "alpha.marker").exists()
        assert "Successful: 1" in out

    def test_failure_sets_exit_code_one_but_others_still_install(
        self, usb, tmp_path, capsys
    ):
        code, out = run(["--repository", str(usb), "--install-all", "--yes"], capsys)
        assert code == cli.EXIT_FAILED
        assert "Failed:     1" in out
        assert (tmp_path / "target" / "alpha.marker").exists()

    def test_retry_reruns_only_failures(self, usb, tmp_path, capsys):
        run(["--repository", str(usb), "--install-all", "--yes"], capsys)
        (usb / "installers/macos/broken/install.sh").write_text(
            f"#!/bin/sh\necho '1.0' > '{tmp_path}/target/broken.marker'\n"
        )
        (usb / "installers/macos/broken/install.sh").chmod(0o755)
        RepositoryManager(Repository.load(usb)).generate_checksums()

        code, out = run(["--repository", str(usb), "--retry", "--yes"], capsys)
        assert code == cli.EXIT_OK
        assert "Retrying: broken" in out
        assert (tmp_path / "target" / "broken.marker").exists()

    def test_retry_with_nothing_to_do(self, usb, capsys):
        code, out = run(["--repository", str(usb), "--retry"], capsys)
        assert code == cli.EXIT_OK
        assert "No failed installations" in out

    def test_confirmation_prompt_can_cancel(self, usb, tmp_path, monkeypatch, capsys):
        write_repository  # noqa: B018 - keep the import obvious in this test file
        (usb / "config" / "settings.json").write_text(
            json.dumps({"require_confirmation": True})
        )
        monkeypatch.setattr("builtins.input", lambda _prompt: "n")
        code, out = run(["--repository", str(usb), "--install", "alpha"], capsys)
        assert code == cli.EXIT_CANCELLED
        assert "Cancelled" in out
        assert not (tmp_path / "target" / "alpha.marker").exists()

    def test_confirmation_prompt_can_proceed(self, usb, tmp_path, monkeypatch, capsys):
        (usb / "config" / "settings.json").write_text(
            json.dumps({"require_confirmation": True})
        )
        monkeypatch.setattr("builtins.input", lambda _prompt: "y")
        code, _ = run(["--repository", str(usb), "--install", "alpha"], capsys)
        assert code == cli.EXIT_OK
        assert (tmp_path / "target" / "alpha.marker").exists()

    def test_unknown_application_is_reported_in_the_plan(self, usb, capsys):
        code, out = run(
            ["--repository", str(usb), "--install", "ghost", "--yes"], capsys
        )
        assert "[ERROR]" in out
        assert code == cli.EXIT_FAILED

    def test_json_results(self, usb, capsys):
        code, out = run(
            ["--repository", str(usb), "--install", "alpha", "--yes", "--json"], capsys
        )
        payload = json.loads(out[out.index("{") :])
        assert payload["summary"]["successful"] == 1

    def test_privileges_block_installation(self, usb, capsys, monkeypatch):
        """An application needing admin must not run without admin."""
        patch_platform(monkeypatch, macos_probe(admin=False))
        config = json.loads((usb / "config" / "applications.json").read_text())
        for entry in config["applications"]:
            entry["macos"]["requires_admin"] = True
        (usb / "config" / "applications.json").write_text(json.dumps(config))

        code, out = run(["--repository", str(usb), "--install", "alpha", "--yes"], capsys)
        assert code == cli.EXIT_PRIVILEGES
        assert "NOT currently held" in out


class TestChecksumGeneration:
    def test_generate_checksums_writes_the_store(self, tmp_path, capsys):
        write_repository(tmp_path, [app_entry("a")])
        code, out = run(["--repository", str(tmp_path), "--generate-checksums"], capsys)
        assert code == cli.EXIT_OK
        store = json.loads((tmp_path / "checksums" / "checksums.json").read_text())
        assert "installers/windows/a/setup.exe" in store

    def test_generate_checksums_dry_run_writes_nothing(self, tmp_path, capsys):
        write_repository(tmp_path, [app_entry("a")])
        run(
            ["--repository", str(tmp_path), "--generate-checksums", "--dry-run"], capsys
        )
        assert not (tmp_path / "checksums" / "checksums.json").exists()

    def test_generate_checksums_reports_missing_installers(self, tmp_path, capsys):
        write_repository(tmp_path, [app_entry("a")], create_installers=False)
        code, out = run(["--repository", str(tmp_path), "--generate-checksums"], capsys)
        assert code == cli.EXIT_INVALID_CONFIG
        assert "file not found" in out
