"""Plan execution: the only phase permitted to modify the target machine.

Guarantees:

* a failure never stops the run — remaining applications still install;
* nothing outside the plan is ever executed;
* in dry-run mode no installer process is started at all (the runner is put in
  dry-run mode *and* installers short-circuit, so the safety property holds even
  if a future installer forgets one of the two);
* every outcome is recorded with the detail needed to diagnose it later.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..detection.base import DetectionContext, detect_installed
from ..errors import InstallerError
from ..installers.base import InstallContext, InstallOutcome, get_installer
from ..logging_session import LogSession, redact_command
from ..models import (
    Action,
    ExecutionResult,
    InstallationPlan,
    PlanItem,
    PostInstallAction,
    Status,
)
from ..repository import Repository
from ..versioning import Comparison, compare

#: Called with (index, total, item) before each application is installed.
ProgressCallback = Callable[[int, int, PlanItem], None]
#: Called with (item,) after each application finishes.
CompletionCallback = Callable[[PlanItem], None]


@dataclass
class Executor:
    """Runs an installation plan."""

    repository: Repository
    context: InstallContext
    detection: DetectionContext
    log: LogSession | None = None
    on_progress: ProgressCallback | None = None
    on_complete: CompletionCallback | None = None
    #: Set from settings; kept as a field so tests can flip it directly.
    continue_on_failure: bool = True
    retry_count: int = 0
    verify_after_install: bool = True
    _cancelled: bool = field(default=False, init=False, repr=False)

    def cancel(self) -> None:
        """Request cancellation; takes effect between applications."""
        self._cancelled = True

    # -- execution -------------------------------------------------------

    def execute(self, plan: InstallationPlan) -> ExecutionResult:
        """Execute *plan* and return the aggregate result."""
        executable = plan.executable_items
        total = len(executable)
        self._log(
            "info",
            "Starting %s of %d application(s) on %s %s",
            "dry run" if plan.dry_run else "installation",
            total,
            plan.system.os_name,
            plan.system.arch.value,
        )

        index = 0
        for item in plan.items:
            if not item.will_execute:
                item.status = Status.SKIPPED if item.action is Action.SKIP else Status.FAILED
                if item.action is Action.ERROR and not item.error:
                    item.error = item.reason
                    self._log("error", "%s: %s", item.application.name, item.reason)
                continue

            index += 1
            if self._cancelled:
                item.status = Status.CANCELLED
                item.error = "cancelled by the technician"
                continue

            if self.on_progress is not None:
                self.on_progress(index, total, item)

            self._run_item(item, plan)

            if self.on_complete is not None:
                self.on_complete(item)

            if item.status is Status.FAILED and not self.continue_on_failure:
                self._log(
                    "error",
                    "Stopping after failure of %s (continue_on_failure is off)",
                    item.application.id,
                )
                for remaining in plan.items[plan.items.index(item) + 1 :]:
                    if remaining.will_execute and remaining.status is None:
                        remaining.status = Status.CANCELLED
                        remaining.error = "not attempted: an earlier installation failed"
                break

        result = ExecutionResult(items=plan.items, dry_run=plan.dry_run)
        self._log(
            "info",
            "Finished: %d successful, %d failed, %d skipped",
            result.succeeded,
            result.failed,
            result.skipped,
        )
        return result

    # -- one application -------------------------------------------------

    def _run_item(self, item: PlanItem, plan: InstallationPlan) -> None:
        app = item.application
        payload = app.payload_for(plan.system.os)
        assert payload is not None and item.installer_path is not None

        item.started_at = datetime.now().isoformat(timespec="seconds")
        started = time.monotonic()
        self._log(
            "info",
            "%s %s (%s) via %s",
            "Would install" if plan.dry_run else "Installing",
            app.name,
            app.version or "unversioned",
            item.installer_path,
        )

        attempts = max(1, 1 + max(0, self.retry_count))
        outcome: InstallOutcome | None = None
        for attempt in range(1, attempts + 1):
            try:
                installer = get_installer(plan.system.os, payload.type)
                installer_path = self.repository.resolve(payload.installer)
                installer.preflight(installer_path, payload)
                outcome = installer.install(app, payload, installer_path, self.context)
            except InstallerError as exc:
                outcome = InstallOutcome(success=False, message=str(exc))
            except OSError as exc:
                outcome = InstallOutcome(
                    success=False, message=f"operating system error: {exc}"
                )

            if outcome.success or attempt == attempts:
                break
            self._log(
                "warning",
                "%s failed (attempt %d/%d): %s — retrying",
                app.name,
                attempt,
                attempts,
                outcome.message,
            )

        assert outcome is not None
        item.duration_seconds = time.monotonic() - started
        item.finished_at = datetime.now().isoformat(timespec="seconds")
        item.exit_code = outcome.exit_code
        item.stdout_tail = outcome.stdout
        item.stderr_tail = outcome.stderr

        for command in outcome.commands:
            self._log("info", "exec: %s", " ".join(redact_command(command)))

        if not outcome.success:
            item.status = Status.FAILED
            item.error = outcome.message
            self._log(
                "error",
                "FAILED %s: %s (exit code %s) after %.1fs",
                app.name,
                outcome.message,
                outcome.exit_code,
                item.duration_seconds,
            )
            return

        if plan.dry_run:
            item.status = Status.DRY_RUN
            self._log("info", "DRY RUN %s: %s", app.name, outcome.message)
            return

        problem = self._verify(item, plan)
        if problem:
            item.status = Status.FAILED
            item.error = problem
            self._log("error", "VERIFY FAILED %s: %s", app.name, problem)
            return

        self._run_post_install(item, payload.post_install)
        item.status = Status.SUCCESS
        self._log(
            "info",
            "SUCCESS %s in %.1fs: %s",
            app.name,
            item.duration_seconds,
            outcome.message,
        )

    # -- verification ----------------------------------------------------

    def _verify(self, item: PlanItem, plan: InstallationPlan) -> str | None:
        """Re-run detection to confirm the installation actually landed.

        A detector that cannot see the application is only a failure when the
        application declares a real detection method — ``method: none`` means
        "we have no signal", not "the install failed".
        """
        if not self.verify_after_install:
            return None
        app = item.application
        spec = app.detection_for(plan.system.os)
        if spec.method == "none":
            return None

        found = detect_installed(app, self.detection, plan.system.os)
        item.installed = found
        if not found.installed:
            return (
                "the installer reported success but the application was not "
                f"detected afterwards ({found.detail or 'no detail'})"
            )
        if app.version and found.version:
            result = compare(
                found.version, app.version, app.version_scheme, app.version_pattern
            )
            if result is Comparison.OLDER:
                return (
                    f"installed version {found.version} is older than the expected "
                    f"{app.version}"
                )
        return None

    # -- post-install ----------------------------------------------------

    def _run_post_install(
        self, item: PlanItem, actions: Sequence[PostInstallAction]
    ) -> None:
        for action in actions:
            if action.type == "message":
                self._log("info", "%s: %s", item.application.name, action.message)
                continue
            if action.type != "script" or not action.script:
                continue
            try:
                script = self.repository.resolve(action.script)
            except InstallerError as exc:
                self._log("warning", "post-install script rejected: %s", exc)
                continue
            command = (
                ["/bin/sh", str(script), *action.arguments]
                if script.suffix.lower() in (".sh", ".command")
                else [str(script), *action.arguments]
            )
            result = self.context.runner.run(
                command, timeout=action.timeout_seconds or 300
            )
            if result.exit_code != 0:
                # Post-install steps are auxiliary: a failure is recorded but
                # does not turn a successful installation into a failed one.
                self._log(
                    "warning",
                    "post-install action for %s exited with %s: %s",
                    item.application.id,
                    result.exit_code,
                    result.output_tail,
                )

    # -- logging ---------------------------------------------------------

    def _log(self, level: str, message: str, *args: object) -> None:
        if self.log is None:
            return
        getattr(self.log, level, self.log.info)(message, *args)


def retry_items(plan: InstallationPlan, ids: Iterable[str]) -> InstallationPlan:
    """Return a copy of *plan* limited to *ids*, ready to re-run.

    Used by ``--retry`` and by the GUI's "Retry failed" button. Item state
    (status, error, timings) is cleared so the retry is recorded cleanly.
    """
    wanted = set(ids)
    items = []
    for item in plan.items:
        if item.app_id not in wanted:
            continue
        items.append(
            PlanItem(
                application=item.application,
                action=Action.INSTALL if item.action is Action.ERROR else item.action,
                reason="retry of a previously failed installation",
                installed=item.installed,
                installer_path=item.installer_path,
                requires_admin=item.requires_admin,
                checksum_ok=item.checksum_ok,
            )
        )
    return InstallationPlan(system=plan.system, items=tuple(items), dry_run=plan.dry_run)


def render_results(result: ExecutionResult, log_dir: Path | None = None) -> str:
    """The final report shown to the technician."""
    # +5 keeps the status column aligned: every row gets at least five dots,
    # so the leader length never has to be clamped for the longest name.
    width = max((len(i.application.name) for i in result.items), default=10) + 5
    lines = ["DRY RUN COMPLETE" if result.dry_run else "INSTALLATION COMPLETE", ""]
    symbol = {
        Status.SUCCESS: "SUCCESS",
        Status.DRY_RUN: "WOULD INSTALL",
        Status.FAILED: "FAILED",
        Status.SKIPPED: "SKIPPED",
        Status.CANCELLED: "CANCELLED",
        None: "NOT RUN",
    }
    for item in result.items:
        dots = "." * (width - len(item.application.name))
        detail = ""
        if item.status is Status.FAILED and item.error:
            detail = f"  ({item.error})"
        elif item.status is Status.SKIPPED and item.reason:
            detail = f"  ({item.reason})"
        lines.append(
            f"{item.application.name} {dots} {symbol[item.status]}{detail}"
        )
    lines += [
        "",
        f"Successful: {result.succeeded}",
        f"Skipped:    {result.skipped}",
        f"Failed:     {result.failed}",
    ]
    if result.failures:
        lines += [
            "",
            "Failed applications: " + ", ".join(i.app_id for i in result.failures),
            "Re-run with:  --retry",
        ]
    if log_dir is not None:
        lines += ["", f"Installation log: {log_dir}"]
    if result.dry_run:
        lines += ["", "No changes were made to this computer."]
    return "\n".join(lines)
