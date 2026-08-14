"""Repository validation: everything that can be checked without installing.

The validator is intentionally exhaustive and never raises for a *data*
problem — it collects findings so a technician sees the whole picture in one
pass, the way a linter does.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import ConfigError
from ..models import OS, Arch
from ..security.paths import extension_allowed, is_suspicious
from ..versioning import is_valid as version_is_valid
from .catalogue import PREREQUISITE_TYPES
from .settings import Settings

INFO = "info"
WARNING = "warning"
ERROR = "error"

_SEVERITY_ORDER = {ERROR: 0, WARNING: 1, INFO: 2}


@dataclass(frozen=True)
class Finding:
    """One validation result."""

    severity: str
    message: str
    app_id: str | None = None
    location: str | None = None

    def __str__(self) -> str:
        mark = {INFO: "✓", WARNING: "⚠", ERROR: "✗"}[self.severity]
        prefix = f"[{self.app_id}] " if self.app_id else ""
        return f"{mark} {prefix}{self.message}"

    def to_dict(self) -> dict[str, str | None]:
        return {
            "severity": self.severity,
            "message": self.message,
            "app_id": self.app_id,
            "location": self.location,
        }


@dataclass
class ValidationReport:
    """Collected findings plus convenience accessors."""

    findings: list[Finding] = field(default_factory=list)

    def add(
        self,
        severity: str,
        message: str,
        app_id: str | None = None,
        location: str | None = None,
    ) -> None:
        self.findings.append(Finding(severity, message, app_id, location))

    def info(self, message: str, **kw: str | None) -> None:
        self.add(INFO, message, **kw)  # type: ignore[arg-type]

    def warn(self, message: str, **kw: str | None) -> None:
        self.add(WARNING, message, **kw)  # type: ignore[arg-type]

    def error(self, message: str, **kw: str | None) -> None:
        self.add(ERROR, message, **kw)  # type: ignore[arg-type]

    def of(self, severity: str) -> list[Finding]:
        return [f for f in self.findings if f.severity == severity]

    @property
    def errors(self) -> list[Finding]:
        return self.of(ERROR)

    @property
    def warnings(self) -> list[Finding]:
        return self.of(WARNING)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def summary(self) -> str:
        if self.errors:
            return (
                f"Validation FAILED with {len(self.errors)} error(s) "
                f"and {len(self.warnings)} warning(s)."
            )
        if self.warnings:
            return f"Validation completed with {len(self.warnings)} warning(s)."
        return "Validation passed."

    def sorted_findings(self) -> list[Finding]:
        return sorted(self.findings, key=lambda f: _SEVERITY_ORDER[f.severity])

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "summary": self.summary,
            "counts": {
                ERROR: len(self.errors),
                WARNING: len(self.warnings),
                INFO: len(self.of(INFO)),
            },
            "findings": [f.to_dict() for f in self.findings],
        }

    def render(self) -> str:
        lines = [str(f) for f in self.sorted_findings()]
        lines.append("")
        lines.append(self.summary)
        return "\n".join(lines)


def validate_repository(
    root: Path | str,
    *,
    check_checksums: bool = False,
    platforms: Iterable[OS] = (OS.WINDOWS, OS.MACOS),
) -> ValidationReport:
    """Validate a repository on disk.

    ``check_checksums`` additionally re-hashes every installer, which is slow
    but is what ``--verify`` does before a technician trusts a drive.
    """
    # Imported lazily: ``usbinstaller.repository`` imports this package, and a
    # module-level import here would close the cycle.
    from ..repository import (
        APPLICATIONS_FILENAME,
        CONFIG_DIRNAME,
        Repository,
    )

    report = ValidationReport()
    root_path = Path(root)

    apps_file = root_path / CONFIG_DIRNAME / APPLICATIONS_FILENAME
    if not apps_file.is_file():
        report.error(f"missing configuration file: {apps_file}")
        return report

    try:
        repo = Repository.load(root_path)
    except ConfigError as exc:
        report.error(str(exc), location=str(apps_file))
        return report

    report.info(f"{apps_file.name} is valid JSON and matches the schema")
    report.info(f"{len(repo.catalogue)} application(s) found")
    report.info(f"{len(repo.catalogue.ids)} application ID(s) are unique")

    _validate_settings(repo, report)
    _validate_dependencies(repo, report)

    platform_list = list(platforms)
    counts = {os_: 0 for os_ in platform_list}
    missing_checksums = 0

    for app in repo.catalogue:
        if not app.version:
            report.warn("no version declared; upgrade detection is disabled", app_id=app.id)
        elif not version_is_valid(app.version, app.version_scheme):
            report.error(
                f"version {app.version!r} is not valid for scheme "
                f"{app.version_scheme!r}",
                app_id=app.id,
            )

        for prereq in app.prerequisites:
            if prereq.type not in PREREQUISITE_TYPES:
                report.error(f"unknown prerequisite {prereq.type!r}", app_id=app.id)
            elif prereq.type in ("min_os_version", "min_free_disk_mb", "path_exists"):
                if prereq.value in (None, ""):
                    report.error(
                        f"prerequisite {prereq.type!r} requires a value", app_id=app.id
                    )

        if not app.enabled:
            report.info("disabled in configuration; will not be offered", app_id=app.id)

        for os_ in platform_list:
            payload = app.payload_for(os_)
            if payload is None:
                continue
            counts[os_] += 1
            _validate_payload(repo, app.id, os_, payload, report, check_checksums)
            if payload.sha256 is None and repo.checksums.expected_for(
                payload.installer
            ) is None:
                missing_checksums += 1

    for os_ in platform_list:
        report.info(f"{counts[os_]} {os_.value} installer(s) declared")

    if missing_checksums:
        severity = ERROR if repo.settings.strict_checksums else WARNING
        report.add(
            severity,
            f"{missing_checksums} installer(s) have no checksum"
            + (" (strict_checksums is enabled)" if severity == ERROR else ""),
        )
    elif len(repo.catalogue):
        report.info("every declared installer has a checksum")

    return report


def _validate_settings(repo, report: ValidationReport) -> None:
    from ..repository import SETTINGS_FILENAME

    settings_file = repo.config_dir / SETTINGS_FILENAME
    if not settings_file.is_file():
        report.info("no settings.json; using built-in defaults")
        return
    try:
        raw = json.loads(settings_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        report.error(f"settings.json is not valid JSON: {exc}")
        return
    if not isinstance(raw, dict):
        report.error("settings.json must contain a JSON object")
        return
    unknown = sorted(k for k in raw if k not in Settings.field_names())
    for key in unknown:
        report.warn(f"settings.json: unknown setting {key!r} is ignored")
    if repo.settings.strict_checksums:
        report.info("strict checksum mode is enabled")


def _validate_dependencies(repo, report: ValidationReport) -> None:
    from ..engine.dependencies import find_cycles

    ids = set(repo.catalogue.ids)
    graph = {app.id: list(app.dependencies) for app in repo.catalogue}

    for app in repo.catalogue:
        for dep in app.dependencies:
            if dep not in ids:
                report.error(f"depends on unknown application {dep!r}", app_id=app.id)
                continue
            dep_app = repo.catalogue.get(dep)
            if dep_app is not None and not dep_app.enabled:
                report.warn(
                    f"depends on {dep!r}, which is disabled in configuration",
                    app_id=app.id,
                )

    for cycle in find_cycles(graph):
        report.error("circular dependency: " + " -> ".join(cycle))


def _validate_payload(
    repo,
    app_id: str,
    os_: OS,
    payload,
    report: ValidationReport,
    check_checksums: bool,
) -> None:
    where = f"{os_.value} installer"

    reason = is_suspicious(payload.installer)
    if reason is not None:
        report.error(
            f"{where}: unsafe path {payload.installer!r} ({reason})", app_id=app_id
        )
        return

    try:
        path = repo.resolve(payload.installer)
    except ConfigError as exc:
        report.error(f"{where}: {exc}", app_id=app_id)
        return
    except Exception as exc:  # PathTraversalError
        report.error(f"{where}: {exc}", app_id=app_id)
        return

    if not extension_allowed(payload.installer, payload.type):
        report.error(
            f"{where}: {Path(payload.installer).name} does not have a valid "
            f"extension for installer type {payload.type!r}",
            app_id=app_id,
        )

    exists = path.exists()
    if not exists:
        report.error(
            f"{where}: file not found ({payload.installer})",
            app_id=app_id,
            location=str(path),
        )
    elif payload.type != "app" and path.is_dir():
        report.error(
            f"{where}: expected a file but {payload.installer} is a directory",
            app_id=app_id,
        )
    elif path.is_file() and path.stat().st_size == 0:
        report.error(f"{where}: {payload.installer} is empty", app_id=app_id)

    for arch in payload.architectures:
        if arch is Arch.UNKNOWN:
            report.error(f"{where}: unsupported architecture", app_id=app_id)

    for action in payload.post_install:
        if action.type == "script" and action.script:
            script_reason = is_suspicious(action.script)
            if script_reason is not None:
                report.error(
                    f"{where}: unsafe post-install script path ({script_reason})",
                    app_id=app_id,
                )
            elif not repo.resolve(action.script).exists():
                report.error(
                    f"{where}: post-install script not found ({action.script})",
                    app_id=app_id,
                )

    declared = payload.sha256
    stored = repo.checksums.expected_for(payload.installer)
    if declared and stored and declared != stored:
        report.error(
            f"{where}: sha256 in applications.json disagrees with checksums.json",
            app_id=app_id,
        )

    if check_checksums and exists:
        expected = declared or stored
        if expected is None:
            report.warn(f"{where}: no checksum recorded", app_id=app_id)
        else:
            from ..security.checksums import digest_of

            actual = digest_of(path)
            if actual != expected:
                report.error(
                    f"{where}: checksum MISMATCH for {payload.installer} "
                    f"(expected {expected}, got {actual})",
                    app_id=app_id,
                )
            else:
                report.info(f"{where}: checksum verified", app_id=app_id)
