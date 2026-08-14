"""Cross-platform detectors: filesystem presence and version commands."""

from __future__ import annotations

import os
import re
from pathlib import Path

from ..models import Application, DetectionSpec, InstalledApp
from .base import DetectionContext, register


@register
class PathExistsDetector:
    """Detect an installation purely by the presence of a path.

    Options:
        ``paths``: absolute paths on the *target* machine (``~`` and
            environment variables are expanded). The first hit wins.
    """

    method = "path_exists"

    def detect(
        self, app: Application, spec: DetectionSpec, context: DetectionContext
    ) -> InstalledApp:
        exists = context.path_exists or (lambda p: Path(p).exists())
        candidates = spec.options.get("paths") or []
        if isinstance(candidates, str):
            candidates = [candidates]
        if not candidates:
            return InstalledApp(
                installed=False,
                method=self.method,
                detail="detection.paths is not configured",
            )
        for raw in candidates:
            expanded = os.path.expandvars(os.path.expanduser(str(raw)))
            if exists(expanded):
                return InstalledApp(
                    installed=True, location=expanded, method=self.method
                )
        return InstalledApp(
            installed=False,
            method=self.method,
            detail="none of the configured paths exist",
        )


@register
class CommandVersionDetector:
    """Run an already-installed program's ``--version`` and parse the output.

    Options:
        ``command``: argv list; element 0 must be an absolute path to an
            existing executable (never a bare name resolved via ``PATH``).
        ``pattern``: regex whose first group is the version (default: the first
            dotted number in the output).
        ``timeout_seconds``: default 30.

    The command is only ever run against software already on the machine, and
    is executed without a shell like every other subprocess in the engine.
    """

    method = "command_version"
    default_pattern = r"(\d+(?:\.\d+)+)"

    def detect(
        self, app: Application, spec: DetectionSpec, context: DetectionContext
    ) -> InstalledApp:
        command = spec.options.get("command")
        if isinstance(command, str):
            command = [command]
        if not command or not isinstance(command, list):
            return InstalledApp(
                installed=False,
                method=self.method,
                detail="detection.command is not configured",
            )

        executable = os.path.expandvars(os.path.expanduser(str(command[0])))
        if not os.path.isabs(executable):
            return InstalledApp(
                installed=False,
                method=self.method,
                detail=(
                    "detection.command[0] must be an absolute path; refusing to "
                    "resolve a bare command name through PATH"
                ),
            )
        exists = context.path_exists or (lambda p: Path(p).exists())
        if not exists(executable):
            return InstalledApp(
                installed=False, method=self.method, detail=f"{executable} not found"
            )

        argv = [executable, *[str(a) for a in command[1:]]]
        result = context.runner.run(
            argv,
            timeout=int(spec.options.get("timeout_seconds") or 30),
            check_executable=False,
        )
        if result.timed_out:
            return InstalledApp(
                installed=False, method=self.method, detail="version command timed out"
            )
        if result.exit_code != 0:
            return InstalledApp(
                installed=False,
                method=self.method,
                detail=f"version command exited with {result.exit_code}",
            )

        pattern = spec.options.get("pattern") or self.default_pattern
        try:
            match = re.search(str(pattern), result.stdout or result.stderr)
        except re.error as exc:
            return InstalledApp(
                installed=False, method=self.method, detail=f"invalid pattern: {exc}"
            )
        version = None
        if match:
            version = (match.group(1) if match.groups() else match.group(0)).strip()
        return InstalledApp(
            installed=True,
            version=version,
            location=executable,
            method=self.method,
            detail=None if version else "version could not be parsed from output",
        )
