"""Structured logging, secret redaction and log retention."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from usbinstaller.logging_session import (
    LogSession,
    failed_ids,
    latest_session,
    prune_sessions,
    redact,
    redact_command,
)
from usbinstaller.models import (
    Action,
    Application,
    ExecutionResult,
    InstallationPlan,
    PlanItem,
    Status,
)


class TestRedaction:
    @pytest.mark.parametrize(
        "argument",
        [
            "/password=hunter2",
            "--password=hunter2",
            "-token:abcdef",
            "--api-key=sk-12345",
            "LICENSE_KEY=AAAA-BBBB",
            "ADMIN_PASSWORD=hunter2",
            "/serial=1234-5678",
        ],
    )
    def test_secret_values_never_reach_the_log(self, argument):
        cleaned = redact(argument)
        assert "hunter2" not in cleaned
        assert "abcdef" not in cleaned
        assert "sk-12345" not in cleaned
        assert "AAAA-BBBB" not in cleaned
        assert "1234-5678" not in cleaned
        assert "REDACTED" in cleaned

    def test_ordinary_switches_are_untouched(self):
        assert redact("/VERYSILENT") == "/VERYSILENT"
        assert redact("INSTALLDIR=C:\\Apps") == "INSTALLDIR=C:\\Apps"

    def test_separated_password_arguments_are_redacted(self):
        cleaned = redact_command(["setup.exe", "--password", "hunter2", "/S"])
        assert cleaned == ["setup.exe", "--password", "***REDACTED***", "/S"]

    def test_command_redaction_preserves_structure(self):
        cleaned = redact_command(["msiexec", "/i", "app.msi", "/qn"])
        assert cleaned == ["msiexec", "/i", "app.msi", "/qn"]


class TestLogSession:
    def test_creates_a_timestamped_directory(self, tmp_path):
        with LogSession.create(tmp_path / "logs") as session:
            session.info("hello")
        assert session.directory.parent == tmp_path / "logs"
        assert "hello" in session.log_file.read_text()
        assert not session.fallback

    def test_falls_back_when_the_drive_is_read_only(self, tmp_path, monkeypatch):
        """A write-protected USB stick must not stop the run."""
        import usbinstaller.logging_session as module

        real_mkdir = Path.mkdir

        def refuse(self, *args, **kwargs):
            if str(self).startswith(str(tmp_path)):
                raise OSError("read-only file system")
            return real_mkdir(self, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", refuse)
        session = module.LogSession.create(tmp_path / "logs")
        try:
            assert session.fallback is True
            assert session.directory.exists()
        finally:
            session.close()

    def test_writes_system_plan_and_results(self, tmp_path, windows_system):
        application = Application(id="a", name="A", version="1.0")
        item = PlanItem(application=application, action=Action.INSTALL)
        item.status = Status.SUCCESS
        plan = InstallationPlan(system=windows_system, items=(item,))
        result = ExecutionResult(items=(item,))

        with LogSession.create(tmp_path / "logs") as session:
            session.write_system(windows_system, {"repository": "E:\\"})
            session.write_plan(plan)
            session.write_results(result, windows_system)

        system = json.loads((session.directory / "system.json").read_text())
        results = json.loads((session.directory / "results.json").read_text())
        assert system["system"]["os"] == "windows"
        assert system["repository"] == "E:\\"
        assert results["summary"]["successful"] == 1
        assert results["items"][0]["id"] == "a"

    def test_commands_are_logged_with_redaction(self, tmp_path):
        with LogSession.create(tmp_path / "logs") as session:
            session.command(["setup.exe", "/password=hunter2"])
        assert "hunter2" not in session.log_file.read_text()

    def test_two_sessions_do_not_share_handlers(self, tmp_path):
        first = LogSession.create(tmp_path / "logs", name="run-1")
        second = LogSession.create(tmp_path / "logs", name="run-2")
        first.info("only-in-first")
        second.info("only-in-second")
        first.close()
        second.close()
        assert "only-in-second" not in first.log_file.read_text()
        assert "only-in-first" not in second.log_file.read_text()


class TestRetention:
    def _make_session(self, root: Path, name: str) -> Path:
        directory = root / name
        directory.mkdir(parents=True)
        (directory / "installation.log").write_text("x")
        (directory / "results.json").write_text('{"items": []}')
        return directory

    def test_oldest_sessions_are_pruned(self, tmp_path):
        for day in range(1, 6):
            self._make_session(tmp_path, f"2026-08-0{day}_10-00-00")
        removed = prune_sessions(tmp_path, keep=2)
        assert len(removed) == 3
        assert sorted(p.name for p in tmp_path.iterdir()) == [
            "2026-08-04_10-00-00",
            "2026-08-05_10-00-00",
        ]

    def test_unrelated_directories_are_never_deleted(self, tmp_path):
        (tmp_path / "important-user-data").mkdir()
        self._make_session(tmp_path, "2026-08-01_10-00-00")
        prune_sessions(tmp_path, keep=0)
        prune_sessions(tmp_path, keep=1)
        assert (tmp_path / "important-user-data").exists()

    def test_keep_zero_disables_pruning(self, tmp_path):
        self._make_session(tmp_path, "2026-08-01_10-00-00")
        assert prune_sessions(tmp_path, keep=0) == []

    def test_latest_session_and_failed_ids(self, tmp_path):
        self._make_session(tmp_path, "2026-08-01_10-00-00")
        newest = self._make_session(tmp_path, "2026-08-02_10-00-00")
        (newest / "results.json").write_text(
            json.dumps(
                {
                    "items": [
                        {"id": "ok", "status": "success"},
                        {"id": "bad", "status": "failed"},
                    ]
                }
            )
        )
        assert latest_session(tmp_path) == newest
        assert failed_ids(newest / "results.json") == ["bad"]

    def test_failed_ids_tolerates_a_corrupt_file(self, tmp_path):
        broken = tmp_path / "results.json"
        broken.write_text("{ not json")
        assert failed_ids(broken) == []
        assert failed_ids(tmp_path / "missing.json") == []

    def test_latest_session_with_no_logs(self, tmp_path):
        assert latest_session(tmp_path / "nothing") is None
