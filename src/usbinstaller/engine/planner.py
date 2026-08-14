"""Installation planning: decide what would happen, without doing any of it.

The planner is strictly read-only. It detects, compares versions, validates
installers and checksums, resolves dependency order and produces an
:class:`InstallationPlan`. Nothing here writes to the target machine — that is
what makes ``--dry-run`` provable rather than merely promised.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..detection.base import DetectionContext, detect_installed
from ..errors import (
    DependencyError,
    PathTraversalError,
    UnsupportedInstallerType,
)
from ..installers.base import get_installer
from ..models import (
    Action,
    Application,
    InstallationPlan,
    InstalledApp,
    PlanItem,
    Prerequisite,
    SystemInfo,
)
from ..repository import Repository
from ..security.paths import extension_allowed
from ..versioning import Comparison, compare
from .dependencies import resolve_order


@dataclass
class Planner:
    """Builds installation plans for a repository on a given machine."""

    repository: Repository
    system: SystemInfo
    detection: DetectionContext
    #: Skip the (slow) checksum re-hash; validation still reports missing entries.
    verify_checksums: bool = True

    # -- selection -------------------------------------------------------

    def available(self) -> tuple[Application, ...]:
        """Applications that are enabled and compatible with this machine."""
        return self.repository.catalogue.for_platform(self.system.os, self.system.arch)

    def default_selection(self) -> list[str]:
        return [a.id for a in self.available() if a.selected_by_default]

    # -- planning --------------------------------------------------------

    def plan(
        self,
        selected_ids: Iterable[str] | None = None,
        *,
        dry_run: bool = False,
        force: bool = False,
        include_dependencies: bool | None = None,
    ) -> InstallationPlan:
        """Build a plan for *selected_ids* (default: everything compatible).

        Args:
            force: plan an install even when an equal or newer version is
                already present. Never silently downgrades — the plan line
                still says what it is doing.
            include_dependencies: pull in catalogue dependencies that were not
                explicitly selected (defaults to the repository setting).
        """
        catalogue = self.repository.catalogue
        if include_dependencies is None:
            include_dependencies = self.repository.settings.auto_include_dependencies

        wanted = (
            list(dict.fromkeys(selected_ids))
            if selected_ids is not None
            else [a.id for a in self.available()]
        )

        items: list[PlanItem] = []
        unknown = [i for i in wanted if catalogue.get(i) is None]
        for app_id in unknown:
            items.append(
                PlanItem(
                    application=Application(id=app_id, name=app_id),
                    action=Action.ERROR,
                    reason=f"no application with id {app_id!r} in the catalogue",
                )
            )
        wanted = [i for i in wanted if i not in set(unknown)]

        graph = {app.id: list(app.dependencies) for app in catalogue}
        try:
            ordered = resolve_order(
                wanted, graph, include_dependencies=include_dependencies
            )
        except DependencyError as exc:
            for app_id in wanted:
                app = catalogue.get(app_id)
                items.append(
                    PlanItem(
                        application=app or Application(id=app_id, name=app_id),
                        action=Action.ERROR,
                        reason=str(exc),
                    )
                )
            return InstallationPlan(
                system=self.system, items=tuple(items), dry_run=dry_run
            )

        for app_id in ordered:
            app = catalogue.get(app_id)
            assert app is not None  # resolve_order validated membership
            items.append(self._plan_one(app, force=force, pulled_in=app_id not in wanted))

        return InstallationPlan(system=self.system, items=tuple(items), dry_run=dry_run)

    # -- per-application decision ---------------------------------------

    def _plan_one(self, app: Application, *, force: bool, pulled_in: bool) -> PlanItem:
        os_ = self.system.os
        item = PlanItem(application=app, action=Action.SKIP)
        if pulled_in:
            item.reason = "required as a dependency"

        if not app.enabled:
            item.action = Action.SKIP
            item.reason = "disabled in configuration"
            return item

        payload = app.payload_for(os_)
        if payload is None:
            item.action = Action.SKIP
            item.reason = f"no {os_.value} installer defined"
            return item

        if not payload.supports_arch(self.system.arch):
            item.action = Action.SKIP
            item.reason = (
                f"not supported on {self.system.arch.value} "
                f"(installer targets {', '.join(a.value for a in payload.architectures)})"
            )
            return item

        item.requires_admin = payload.requires_admin

        # --- installer file and type -----------------------------------
        try:
            installer_path = self.repository.resolve(payload.installer)
        except PathTraversalError as exc:
            item.action = Action.ERROR
            item.reason = str(exc)
            return item

        item.installer_path = payload.installer
        if not installer_path.exists():
            item.action = Action.ERROR
            item.reason = f"installer missing: {payload.installer}"
            return item

        if not extension_allowed(payload.installer, payload.type):
            item.action = Action.ERROR
            item.reason = (
                f"{Path(payload.installer).name} is not a valid file type for "
                f"installer type {payload.type!r}"
            )
            return item

        try:
            get_installer(os_, payload.type)
        except UnsupportedInstallerType as exc:
            item.action = Action.ERROR
            item.reason = str(exc)
            return item

        # --- checksum ---------------------------------------------------
        settings = self.repository.settings
        expected = payload.sha256 or self.repository.checksums.expected_for(
            payload.installer
        )
        if expected is None:
            item.checksum_ok = None
            if settings.strict_checksums:
                item.action = Action.ERROR
                item.reason = (
                    "no SHA-256 checksum recorded and strict checksum mode is "
                    "enabled"
                )
                return item
        elif self.verify_checksums:
            from ..security.checksums import digest_of

            try:
                actual = digest_of(installer_path)
            except OSError as exc:
                item.action = Action.ERROR
                item.reason = f"cannot read installer for verification: {exc}"
                return item
            item.checksum_ok = actual == expected
            if not item.checksum_ok:
                if settings.block_on_checksum_mismatch or settings.strict_checksums:
                    item.action = Action.ERROR
                    item.reason = (
                        "checksum MISMATCH — the installer on the drive does not "
                        "match the recorded SHA-256 and will not be executed"
                    )
                    return item
                item.reason = "WARNING: checksum mismatch"

        # --- prerequisites ---------------------------------------------
        problem = self._check_prerequisites(app.prerequisites)
        if problem:
            item.action = Action.ERROR
            item.reason = problem
            return item

        # --- already installed? ----------------------------------------
        installed = detect_installed(app, self.detection, os_)
        item.installed = installed

        if not installed.installed:
            item.action = Action.INSTALL
            item.reason = item.reason or "not installed"
            return item

        comparison = compare(
            installed.version, app.version, app.version_scheme, app.version_pattern
        )
        item.action, item.reason = _decide_upgrade(
            comparison, installed, app, force=force
        )
        return item

    def _check_prerequisites(self, prerequisites: Sequence[Prerequisite]) -> str | None:
        for prereq in prerequisites:
            failure = self._check_prerequisite(prereq)
            if failure:
                return prereq.message or failure
        return None

    def _check_prerequisite(self, prereq: Prerequisite) -> str | None:
        if prereq.type == "admin":
            return (
                None
                if self.system.is_admin
                else "administrator privileges are required but not held"
            )
        if prereq.type == "min_os_version":
            result = compare(self.system.os_version, str(prereq.value), "numeric")
            if result is Comparison.OLDER:
                return (
                    f"requires OS version {prereq.value} or newer "
                    f"(this machine reports {self.system.os_version})"
                )
            if result is Comparison.UNKNOWN:
                return (
                    f"cannot verify the required OS version {prereq.value} against "
                    f"{self.system.os_version!r}"
                )
            return None
        if prereq.type == "min_free_disk_mb":
            free = self.system.free_disk_bytes
            if free is None:
                return "free disk space could not be determined"
            try:
                required = int(prereq.value) * 1024 * 1024
            except (TypeError, ValueError):
                return f"invalid min_free_disk_mb value {prereq.value!r}"
            if free < required:
                return (
                    f"requires {prereq.value} MB free, "
                    f"{free // (1024 * 1024)} MB available"
                )
            return None
        if prereq.type == "path_exists":
            import os as _os

            path = _os.path.expandvars(_os.path.expanduser(str(prereq.value)))
            exists = self.detection.path_exists or (lambda p: Path(p).exists())
            return None if exists(path) else f"required path not found: {prereq.value}"
        return f"unknown prerequisite type {prereq.type!r}"


def _decide_upgrade(
    comparison: Comparison, installed: InstalledApp, app: Application, *, force: bool
) -> tuple[Action, str]:
    """Turn a version comparison into an action, never downgrading silently."""
    installed_version = installed.version or "unknown"
    usb_version = app.version or "unknown"

    if comparison is Comparison.OLDER:
        return (
            Action.UPGRADE,
            f"update available: installed {installed_version} → USB {usb_version}",
        )
    if comparison is Comparison.SAME:
        if force:
            return (
                Action.INSTALL,
                f"reinstall forced: {installed_version} already installed",
            )
        return Action.SKIP, f"already installed ({installed_version})"
    if comparison is Comparison.NEWER:
        if force:
            return (
                Action.INSTALL,
                f"DOWNGRADE forced: installed {installed_version} is newer than "
                f"USB {usb_version}",
            )
        return (
            Action.SKIP,
            f"newer version already installed ({installed_version} > {usb_version})",
        )

    # Unknown: we could not compare. Do not touch a working installation
    # unless the technician explicitly forces it.
    if force:
        return (
            Action.INSTALL,
            f"forced: installed version {installed_version} could not be compared "
            f"with USB version {usb_version}",
        )
    return (
        Action.SKIP,
        f"installed ({installed_version}); version cannot be compared with USB "
        f"{usb_version} — use --force to install anyway",
    )


def render_plan(plan: InstallationPlan) -> str:
    """Render a plan the way the confirmation screen shows it."""
    from ..sysdetect.system import describe

    lines = [
        "INSTALLATION PLAN",
        "",
        describe(plan.system),
        "",
        "Applications:",
        "",
    ]
    label = {
        Action.INSTALL: "[INSTALL]",
        Action.UPGRADE: "[UPGRADE]",
        Action.SKIP: "[SKIP]   ",
        Action.ERROR: "[ERROR]  ",
    }
    for item in plan.items:
        version = f" {item.application.version}" if item.application.version else ""
        reason = f" - {item.reason}" if item.reason else ""
        lines.append(f"{label[item.action]} {item.application.name}{version}{reason}")

    counts = {action: len(plan.by_action(action)) for action in Action}
    lines += [
        "",
        f"{counts[Action.INSTALL]} to install, {counts[Action.UPGRADE]} to upgrade, "
        f"{counts[Action.SKIP]} to skip, {counts[Action.ERROR]} in error",
    ]
    if plan.requires_admin:
        lines.append(
            "Administrator privileges are required: "
            + ("YES (held)" if plan.system.is_admin else "YES (NOT currently held)")
        )
    if plan.dry_run:
        lines += ["", "DRY RUN — no changes will be made to this computer."]
    return "\n".join(lines)
