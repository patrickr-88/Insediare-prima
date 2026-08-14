"""Application service layer: wires the engine together for the CLI and GUI.

Both front-ends drive the same object, so a behaviour implemented once (dry
run, retry, checksum policy) is identical in both.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .config.authoring import CatalogueEditor
from .detection.base import DetectionContext, detect_installed
from .engine.details import ApplicationDetails, describe
from .engine.executor import Executor, retry_items
from .engine.planner import Planner
from .errors import ConfigSchemaError
from .installers.base import InstallContext
from .installers.process import ProcessRunner
from .logging_session import LogSession, failed_ids, latest_session, prune_sessions
from .models import ExecutionResult, InstallationPlan, SystemInfo
from .repository import Repository
from .sysdetect.privileges import PrivilegeRequirement, requirement_for
from .sysdetect.system import SystemProbe, detect_system


@dataclass
class InstallerApp:
    """A loaded repository plus a detected machine, ready to plan and install."""

    repository: Repository
    system: SystemInfo
    dry_run: bool = False
    log: LogSession | None = None
    runner: ProcessRunner = field(default=None)  # type: ignore[assignment]
    detection: DetectionContext = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        settings = self.repository.settings
        if self.runner is None:
            self.runner = ProcessRunner(
                default_timeout=settings.default_timeout_seconds,
                capture_bytes=settings.output_capture_bytes,
                dry_run=self.dry_run,
            )
        else:
            # Honour dry-run even when a runner is injected (tests, automation).
            self.runner.dry_run = self.runner.dry_run or self.dry_run
        if self.detection is None:
            # Detection gets its own runner, never in dry-run mode: probing what
            # is installed is read-only, and stubbing it out would make a dry
            # run preview a machine that does not exist.
            self.detection = DetectionContext(
                system=self.system,
                runner=ProcessRunner(
                    default_timeout=60,
                    capture_bytes=settings.output_capture_bytes,
                    dry_run=False,
                    on_command=self.log.command if self.log is not None else None,
                ),
                application_dirs=settings.macos_application_dirs,
            )
        if self.log is not None:
            self.runner.on_command = self.log.command

    # -- construction ----------------------------------------------------

    @classmethod
    def create(
        cls,
        root: Path | str | None = None,
        *,
        dry_run: bool = False,
        probe: SystemProbe | None = None,
        log: LogSession | None = None,
        runner: ProcessRunner | None = None,
        with_log: bool = False,
        console_log: bool = False,
    ) -> InstallerApp:
        """Load the repository, detect the machine and (optionally) open a log."""
        repository = Repository.load(root)
        system = detect_system(probe, repository_path=repository.root)
        if log is None and with_log:
            log = LogSession.create(repository.logs_dir, console=console_log)
            log.write_system(system, {"repository": str(repository.root)})
            prune_sessions(repository.logs_dir, repository.settings.log_retention)
        return cls(
            repository=repository,
            system=system,
            dry_run=dry_run,
            log=log,
            runner=runner,  # type: ignore[arg-type]
        )

    # -- planning --------------------------------------------------------

    @property
    def planner(self) -> Planner:
        return Planner(
            repository=self.repository, system=self.system, detection=self.detection
        )

    def available(self):
        return self.planner.available()

    def plan(
        self, selected_ids: Iterable[str] | None = None, *, force: bool = False
    ) -> InstallationPlan:
        plan = self.planner.plan(selected_ids, dry_run=self.dry_run, force=force)
        if self.log is not None:
            self.log.write_plan(plan)
        return plan

    def privileges_for(self, plan: InstallationPlan) -> PrivilegeRequirement:
        return requirement_for(self.system, plan.items)

    # -- information -----------------------------------------------------

    def details_for(
        self, app_id: str, *, detect: bool = True, verify_checksum: bool = False
    ) -> ApplicationDetails:
        """Everything the interface shows about one application.

        Detection is run on demand (``detect=False`` keeps it purely static),
        which is what lets the GUI populate its list instantly and fill in
        "what is installed" as the technician clicks through.
        """
        application = self.repository.catalogue.get(app_id)
        if application is None:
            raise ConfigSchemaError(f"unknown application id: {app_id}")
        installed = (
            detect_installed(application, self.detection, self.system.os)
            if detect
            else None
        )
        return describe(
            application,
            self.repository,
            self.system,
            installed,
            verify_checksum=verify_checksum,
        )

    def all_details(self, *, detect: bool = False) -> list[ApplicationDetails]:
        """Details for every catalogue entry, compatible or not."""
        return [
            self.details_for(app.id, detect=detect)
            for app in self.repository.catalogue
        ]

    # -- authoring -------------------------------------------------------

    @property
    def editor(self) -> CatalogueEditor:
        """Write access to the catalogue (used by the GUI's Add Application)."""
        return CatalogueEditor(self.repository)

    def reload(self) -> InstallerApp:
        """Re-read the drive after it has been edited, keeping this machine's
        detected state and log session."""
        return InstallerApp(
            repository=Repository.load(self.repository.root),
            system=self.system,
            dry_run=self.dry_run,
            log=self.log,
            runner=self.runner,
        )

    def validate(self, *, check_checksums: bool = False):
        """Validate the drive; the GUI shows the report verbatim."""
        from .config.validator import validate_repository

        return validate_repository(
            self.repository.root, check_checksums=check_checksums
        )

    # -- execution -------------------------------------------------------

    def executor(self, **kwargs) -> Executor:
        settings = self.repository.settings
        context = InstallContext(
            system=self.system,
            runner=self.runner,
            repository_root=self.repository.root,
            dry_run=self.dry_run,
            default_timeout=settings.default_timeout_seconds,
            log=self.log,
            workspace=Path(tempfile.gettempdir()),
            extra={"system_root": _system_root()},
        )
        return Executor(
            repository=self.repository,
            context=context,
            detection=self.detection,
            log=self.log,
            continue_on_failure=settings.continue_on_failure,
            retry_count=settings.auto_retry_count,
            **kwargs,
        )

    def install(self, plan: InstallationPlan, **kwargs) -> ExecutionResult:
        result = self.executor(**kwargs).execute(plan)
        if self.log is not None:
            self.log.write_results(result, self.system)
        return result

    # -- retry -----------------------------------------------------------

    def previous_failures(self) -> list[str]:
        """Application ids that failed in the most recent recorded run."""
        session = latest_session(self.repository.logs_dir)
        return failed_ids(session / "results.json") if session else []

    def retry_plan(self, plan: InstallationPlan, ids: Iterable[str]) -> InstallationPlan:
        return retry_items(plan, ids)


def _system_root() -> str:
    import os

    return os.environ.get("SystemRoot", "C:\\Windows")
