"""Plan execution: failure isolation, verification, retry, cancellation."""

from __future__ import annotations

from pathlib import Path

from tests.conftest import app_entry, registry_entry, write_repository
from usbinstaller.detection.base import DetectionContext
from usbinstaller.engine.executor import Executor, render_results, retry_items
from usbinstaller.engine.planner import Planner
from usbinstaller.installers.base import InstallContext
from usbinstaller.installers.process import CommandResult, RecordingRunner
from usbinstaller.logging_session import LogSession
from usbinstaller.models import Action, Status


def build(tmp_path: Path, system, applications, *, results=None, registry=(), **kwargs):
    """Wire a planner + executor over a synthetic repository."""
    write_repository(tmp_path, applications, settings={"require_confirmation": False})
    from usbinstaller.repository import Repository

    repository = Repository.load(tmp_path)
    runner = RecordingRunner(
        results or {}, default_result=CommandResult(("mock",), exit_code=0)
    )
    detection = DetectionContext(
        system=system, runner=runner, registry_reader=lambda: list(registry),
        path_exists=lambda p: False,
    )
    planner = Planner(repository=repository, system=system, detection=detection)
    executor = Executor(
        repository=repository,
        context=InstallContext(
            system=system,
            runner=runner,
            repository_root=repository.root,
            extra={"system_root": "C:\\Windows"},
            **{k: v for k, v in kwargs.items() if k in {"dry_run"}},
        ),
        detection=detection,
        **{k: v for k, v in kwargs.items() if k not in {"dry_run"}},
    )
    return planner, executor, runner


class TestHappyPath:
    def test_all_applications_install(self, tmp_path, windows_system):
        planner, executor, runner = build(
            tmp_path, windows_system, [app_entry("a"), app_entry("b")]
        )
        result = executor.execute(planner.plan())
        assert result.succeeded == 2
        assert result.failed == 0
        assert all(i.status is Status.SUCCESS for i in result.items)
        assert len(runner.history) == 2

    def test_timings_and_exit_codes_are_recorded(self, tmp_path, windows_system):
        planner, executor, _ = build(tmp_path, windows_system, [app_entry("a")])
        item = executor.execute(planner.plan()).items[0]
        assert item.exit_code == 0
        assert item.started_at and item.finished_at
        assert item.duration_seconds >= 0


class TestFailureHandling:
    def test_one_failure_does_not_stop_the_rest(self, tmp_path, windows_system):
        planner, executor, _ = build(
            tmp_path,
            windows_system,
            [app_entry("a"), app_entry("bad"), app_entry("c")],
            results={"bad": CommandResult(("setup",), exit_code=1603, stderr="fatal")},
        )
        result = executor.execute(planner.plan())
        assert result.succeeded == 2
        assert result.failed == 1
        failed = result.failures[0]
        assert failed.app_id == "bad"
        assert failed.exit_code == 1603
        assert "1603" in failed.error

    def test_continue_on_failure_can_be_switched_off(self, tmp_path, windows_system):
        planner, executor, _ = build(
            tmp_path,
            windows_system,
            [app_entry("bad"), app_entry("later")],
            results={"bad": CommandResult(("setup",), exit_code=1)},
            continue_on_failure=False,
        )
        result = executor.execute(planner.plan())
        assert result.failed == 1
        assert result.items[1].status is Status.CANCELLED
        assert "earlier installation failed" in result.items[1].error

    def test_timeout_is_recorded_as_a_failure(self, tmp_path, windows_system):
        planner, executor, _ = build(
            tmp_path,
            windows_system,
            [app_entry("slow")],
            results={"slow": CommandResult(("setup",), exit_code=-1, timed_out=True)},
        )
        item = executor.execute(planner.plan()).items[0]
        assert item.status is Status.FAILED
        assert "timed out" in item.error

    def test_custom_success_exit_codes_are_honoured(self, tmp_path, windows_system):
        planner, executor, _ = build(
            tmp_path,
            windows_system,
            [
                app_entry(
                    "a",
                    macos=False,
                    windows={
                        "installer": "installers/windows/a/setup.exe",
                        "type": "exe",
                        "success_exit_codes": [0, 3010],
                    },
                )
            ],
            results={"setup.exe": CommandResult(("setup",), exit_code=3010)},
        )
        assert executor.execute(planner.plan()).items[0].status is Status.SUCCESS

    def test_plan_errors_are_reported_without_running_anything(
        self, tmp_path, windows_system
    ):
        planner, executor, runner = build(tmp_path, windows_system, [app_entry("a")])
        plan = planner.plan()
        plan.items[0].action = Action.ERROR
        plan.items[0].reason = "installer missing"

        result = executor.execute(plan)
        assert result.failed == 1
        assert runner.history == ()

    def test_retry_count_retries_before_giving_up(self, tmp_path, windows_system):
        planner, executor, runner = build(
            tmp_path,
            windows_system,
            [app_entry("flaky")],
            results={"flaky": CommandResult(("setup",), exit_code=1)},
            retry_count=2,
        )
        result = executor.execute(planner.plan())
        assert result.failed == 1
        assert len(runner.history) == 3  # initial attempt + two retries


class TestVerification:
    def _apps(self):
        return [
            app_entry(
                "firefox",
                name="Firefox",
                version="141.0",
                macos=False,
                detection={"method": "windows_registry", "display_name": "^Firefox"},
            )
        ]

    def test_success_requires_the_application_to_be_detectable_afterwards(
        self, tmp_path, windows_system
    ):
        planner, executor, _ = build(tmp_path, windows_system, self._apps())
        plan = planner.plan()
        # Detection still reports nothing installed after a "successful" run.
        item = executor.execute(plan).items[0]
        assert item.status is Status.FAILED
        assert "not detected afterwards" in item.error

    def test_detection_after_install_confirms_success(self, tmp_path, windows_system):
        planner, executor, _ = build(tmp_path, windows_system, self._apps())
        plan = planner.plan()
        executor.detection.registry_reader = lambda: [registry_entry("Firefox", "141.0")]
        assert executor.execute(plan).items[0].status is Status.SUCCESS

    def test_verification_can_be_disabled(self, tmp_path, windows_system):
        planner, executor, _ = build(
            tmp_path, windows_system, self._apps(), verify_after_install=False
        )
        assert executor.execute(planner.plan()).items[0].status is Status.SUCCESS

    def test_applications_without_detection_are_not_failed_for_being_invisible(
        self, tmp_path, windows_system
    ):
        planner, executor, _ = build(tmp_path, windows_system, [app_entry("a")])
        assert executor.execute(planner.plan()).items[0].status is Status.SUCCESS

    def test_older_version_after_install_is_a_failure(self, tmp_path, windows_system):
        planner, executor, _ = build(tmp_path, windows_system, self._apps())
        plan = planner.plan()
        executor.detection.registry_reader = lambda: [registry_entry("Firefox", "100.0")]
        item = executor.execute(plan).items[0]
        assert item.status is Status.FAILED
        assert "older than the expected" in item.error


class TestPostInstall:
    def test_post_install_script_runs_after_success(self, tmp_path, windows_system):
        (tmp_path / "scripts" / "windows").mkdir(parents=True)
        (tmp_path / "scripts" / "windows" / "after.sh").write_text("echo hi")
        planner, executor, runner = build(
            tmp_path,
            windows_system,
            [
                app_entry(
                    "a",
                    macos=False,
                    windows={
                        "installer": "installers/windows/a/setup.exe",
                        "type": "exe",
                        "post_install": [
                            {"type": "script", "script": "scripts/windows/after.sh"},
                            {"type": "message", "message": "Reboot recommended"},
                        ],
                    },
                )
            ],
        )
        result = executor.execute(planner.plan())
        assert result.items[0].status is Status.SUCCESS
        assert any("after.sh" in " ".join(c) for c in runner.history)

    def test_failing_post_install_does_not_fail_the_installation(
        self, tmp_path, windows_system
    ):
        (tmp_path / "scripts" / "windows").mkdir(parents=True)
        (tmp_path / "scripts" / "windows" / "after.sh").write_text("exit 1")
        planner, executor, _ = build(
            tmp_path,
            windows_system,
            [
                app_entry(
                    "a",
                    macos=False,
                    windows={
                        "installer": "installers/windows/a/setup.exe",
                        "type": "exe",
                        "post_install": [
                            {"type": "script", "script": "scripts/windows/after.sh"}
                        ],
                    },
                )
            ],
            results={"after.sh": CommandResult(("sh",), exit_code=1)},
        )
        assert executor.execute(planner.plan()).items[0].status is Status.SUCCESS


class TestCancellation:
    def test_cancelling_stops_before_the_next_application(self, tmp_path, windows_system):
        planner, executor, runner = build(
            tmp_path, windows_system, [app_entry("a"), app_entry("b")]
        )
        executor.on_complete = lambda item: executor.cancel()
        result = executor.execute(planner.plan())
        assert result.items[0].status is Status.SUCCESS
        assert result.items[1].status is Status.CANCELLED
        assert len(runner.history) == 1


class TestRetryAndReporting:
    def test_retry_items_keeps_only_the_failures(self, tmp_path, windows_system):
        planner, executor, _ = build(
            tmp_path,
            windows_system,
            [app_entry("ok"), app_entry("bad")],
            results={"bad": CommandResult(("setup",), exit_code=1)},
        )
        plan = planner.plan()
        result = executor.execute(plan)
        retry = retry_items(plan, [i.app_id for i in result.failures])
        assert [i.app_id for i in retry.items] == ["bad"]
        assert retry.items[0].status is None
        assert "retry" in retry.items[0].reason

    def test_render_results_summarises(self, tmp_path, windows_system):
        planner, executor, _ = build(
            tmp_path,
            windows_system,
            [app_entry("ok"), app_entry("bad")],
            results={"bad": CommandResult(("setup",), exit_code=1)},
        )
        text = render_results(executor.execute(planner.plan()), Path("/logs/run"))
        assert "INSTALLATION COMPLETE" in text
        assert "Successful: 1" in text
        assert "Failed:     1" in text
        assert "/logs/run" in text

    def test_progress_callbacks_report_position(self, tmp_path, windows_system):
        planner, executor, _ = build(
            tmp_path, windows_system, [app_entry("a"), app_entry("b")]
        )
        seen = []
        executor.on_progress = lambda index, total, item: seen.append((index, total))
        executor.execute(planner.plan())
        assert seen == [(1, 2), (2, 2)]


class TestLoggingIntegration:
    def test_a_run_writes_a_complete_log_directory(self, tmp_path, windows_system):
        planner, executor, _ = build(
            tmp_path,
            windows_system,
            [app_entry("ok"), app_entry("bad")],
            results={"bad": CommandResult(("setup",), exit_code=1, stderr="nope")},
        )
        with LogSession.create(tmp_path / "logs") as session:
            executor.log = session
            plan = planner.plan()
            session.write_system(windows_system)
            session.write_plan(plan)
            result = executor.execute(plan)
            session.write_results(result, windows_system)

        directory = session.directory
        assert (directory / "installation.log").exists()
        assert (directory / "results.json").exists()
        assert (directory / "system.json").exists()

        log_text = (directory / "installation.log").read_text()
        assert "FAILED" in log_text
        assert "SUCCESS" in log_text
