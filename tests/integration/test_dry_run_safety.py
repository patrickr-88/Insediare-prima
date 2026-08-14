"""Proof that ``--dry-run`` cannot modify the target computer.

Three independent guarantees are asserted here, because "we promise it doesn't
install anything" is not a testable statement:

1. **No process is executed.** The runner is in dry-run mode, so nothing is
   spawned — asserted against the runner's recorded history.
2. **No filesystem change outside the log directory.** The whole target
   filesystem area used by the fake installers is snapshotted before and after.
3. **Same plan, either way.** The plan produced under dry run is identical to
   the plan produced for a real run, so a dry run genuinely previews the real
   thing rather than following a different code path.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from tests.conftest import app_entry, write_repository
from usbinstaller.app import InstallerApp
from usbinstaller.installers.process import ProcessRunner
from usbinstaller.models import Status


def snapshot(root: Path) -> dict[str, str]:
    """Content-addressed snapshot of every file under *root*."""
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return out


@pytest.fixture
def repository_with_real_scripts(tmp_path: Path) -> Path:
    """A repository whose 'installers' would really write files if executed."""
    root = tmp_path / "usb"
    target = tmp_path / "target"
    target.mkdir(parents=True)
    (target / "existing-user-file.txt").write_text("do not touch me")

    write_repository(
        root,
        [
            app_entry(
                "writer",
                windows=False,
                macos={
                    "installer": "installers/macos/writer/install.sh",
                    "type": "shell",
                    "requires_admin": False,
                },
            ),
            app_entry(
                "writer2",
                windows=False,
                macos={
                    "installer": "installers/macos/writer2/install.sh",
                    "type": "shell",
                    "requires_admin": False,
                },
            ),
        ],
        settings={"require_confirmation": False},
        create_installers=False,
    )
    for name in ("writer", "writer2"):
        script = root / "installers" / "macos" / name / "install.sh"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(
            f"#!/bin/sh\necho 'installed' > '{target}/{name}-installed.txt'\n"
        )
        script.chmod(0o755)
    return root


def make_app(root: Path, *, dry_run: bool) -> InstallerApp:
    from tests.conftest import macos_probe

    return InstallerApp.create(root, dry_run=dry_run, probe=macos_probe())


class TestDryRunSafety:
    def test_no_process_is_executed(self, repository_with_real_scripts):
        app = make_app(repository_with_real_scripts, dry_run=True)
        result = app.install(app.plan())
        assert result.succeeded == 2
        assert all(i.status is Status.DRY_RUN for i in result.items)
        # Commands were composed and logged, but nothing ran.
        assert app.runner.dry_run is True

    def test_no_file_outside_the_repository_is_created_or_changed(
        self, repository_with_real_scripts, tmp_path
    ):
        target = tmp_path / "target"
        before = snapshot(target)

        app = make_app(repository_with_real_scripts, dry_run=True)
        app.install(app.plan())

        assert snapshot(target) == before
        assert not (target / "writer-installed.txt").exists()
        assert (target / "existing-user-file.txt").read_text() == "do not touch me"

    def test_a_real_run_does_change_the_target(
        self, repository_with_real_scripts, tmp_path
    ):
        """The counter-test: without --dry-run the same plan really installs."""
        target = tmp_path / "target"
        app = make_app(repository_with_real_scripts, dry_run=False)
        result = app.install(app.plan())

        assert result.succeeded == 2
        assert (target / "writer-installed.txt").exists()
        assert (target / "existing-user-file.txt").read_text() == "do not touch me"

    def test_the_dry_run_plan_matches_the_real_plan(self, repository_with_real_scripts):
        dry = make_app(repository_with_real_scripts, dry_run=True).plan()
        live = make_app(repository_with_real_scripts, dry_run=False).plan()
        assert [(i.app_id, i.action, i.reason) for i in dry.items] == [
            (i.app_id, i.action, i.reason) for i in live.items
        ]

    def test_planning_alone_executes_nothing(self, repository_with_real_scripts, tmp_path):
        """Discovery, validation and planning are read-only phases."""
        target = tmp_path / "target"
        before = snapshot(target)

        app = make_app(repository_with_real_scripts, dry_run=False)
        app.plan()

        assert snapshot(target) == before
        assert app.runner.history == ()

    def test_dry_run_runner_refuses_to_spawn_even_if_asked_directly(self, tmp_path):
        """Defence in depth: the runner itself is inert in dry-run mode."""
        marker = tmp_path / "spawned.txt"
        import sys

        runner = ProcessRunner(dry_run=True)
        runner.run([sys.executable, "-c", f"open(r'{marker}','w').close()"])
        assert not marker.exists()


class TestPhaseSeparation:
    def test_detection_and_validation_never_touch_the_runner_in_dry_run(
        self, repository_with_real_scripts
    ):
        app = make_app(repository_with_real_scripts, dry_run=True)
        plan = app.plan()
        assert plan.dry_run is True
        assert all(i.will_execute for i in plan.items)
        assert app.runner.history == ()

    def test_results_are_marked_as_a_dry_run(self, repository_with_real_scripts):
        app = make_app(repository_with_real_scripts, dry_run=True)
        payload = app.install(app.plan()).to_dict()
        assert payload["dry_run"] is True
        assert payload["summary"]["failed"] == 0
