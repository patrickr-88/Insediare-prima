"""Structured, per-run logging.

Each run creates ``logs/<YYYY-MM-DD_HH-MM-SS>/`` containing:

``installation.log``
    Human-readable, timestamped narrative of the run.
``results.json``
    Machine-readable plan + outcomes, for automation and for ``--retry``.
``system.json``
    The detected environment, so a failure report is self-contained.

Secrets are never written: any argument that looks like a password, token or
key is redacted before it reaches either the log or the JSON files.
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import ExecutionResult, InstallationPlan, SystemInfo
from .version import APP_NAME, __version__

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
REDACTED = "***REDACTED***"

#: Argument names whose *values* must never be logged.
_SECRET_SWITCH = re.compile(
    r"^(?P<flag>[-/]{1,2}(?:pass(?:word)?|pwd|token|secret|apikey|api[-_]?key|"
    r"credential|licen[cs]e[-_]?key|serial|pin)[=:]?)(?P<value>.*)$",
    re.IGNORECASE,
)
#: Inline ``KEY=value`` style secrets.
_SECRET_ASSIGN = re.compile(
    r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*(?:PASS(?:WORD)?|TOKEN|SECRET|KEY|CREDENTIAL))"
    r"=(?P<value>.+)$",
    re.IGNORECASE,
)


def redact(argument: str) -> str:
    """Redact one command-line argument if it carries a secret."""
    text = str(argument)
    match = _SECRET_SWITCH.match(text)
    if match:
        flag, value = match.group("flag"), match.group("value")
        if flag.endswith(("=", ":")):
            return flag + REDACTED
        if value:
            return f"{flag}={REDACTED}"
        # A bare "--password" carries no secret itself; the *next* argument
        # does, and redact_command() handles that.
        return flag
    match = _SECRET_ASSIGN.match(text)
    if match:
        return f"{match.group('key')}={REDACTED}"
    return text


def redact_command(command: Sequence[str]) -> list[str]:
    """Redact a whole argv vector, including ``--password value`` pairs."""
    out: list[str] = []
    redact_next = False
    for argument in command:
        if redact_next:
            out.append(REDACTED)
            redact_next = False
            continue
        cleaned = redact(argument)
        out.append(cleaned)
        bare = str(argument).lstrip("-/").lower()
        if bare in (
            "password",
            "pass",
            "pwd",
            "token",
            "secret",
            "apikey",
            "api-key",
            "credential",
            "serial",
            "pin",
        ):
            redact_next = True
    return out


@dataclass
class LogSession:
    """A single run's log directory and logger."""

    directory: Path
    logger: logging.Logger
    started_at: datetime
    #: True when logs had to be written somewhere other than the repository
    #: (e.g. the USB drive is read-only or write-protected).
    fallback: bool = False

    # -- lifecycle -------------------------------------------------------

    @classmethod
    def create(
        cls,
        logs_root: Path,
        *,
        name: str | None = None,
        console: bool = False,
        level: int = logging.INFO,
    ) -> LogSession:
        started = datetime.now()
        stamp = name or started.strftime("%Y-%m-%d_%H-%M-%S")
        directory, fallback = _prepare_directory(logs_root, stamp)

        logger = logging.getLogger(f"usbinstaller.session.{stamp}.{id(directory)}")
        logger.setLevel(level)
        logger.propagate = False
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()

        file_handler = logging.FileHandler(
            directory / "installation.log", encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
        logger.addHandler(file_handler)

        if console:
            stream = logging.StreamHandler()
            stream.setFormatter(logging.Formatter("%(message)s"))
            stream.setLevel(logging.WARNING)
            logger.addHandler(stream)

        session = cls(directory, logger, started, fallback)
        logger.info("%s %s starting", APP_NAME, __version__)
        if fallback:
            logger.warning(
                "Repository log directory is not writable; logging to %s", directory
            )
        return session

    def close(self) -> None:
        for handler in list(self.logger.handlers):
            handler.flush()
            handler.close()
            self.logger.removeHandler(handler)

    def __enter__(self) -> LogSession:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- convenience -----------------------------------------------------

    def info(self, message: str, *args: Any) -> None:
        self.logger.info(message, *args)

    def warning(self, message: str, *args: Any) -> None:
        self.logger.warning(message, *args)

    def error(self, message: str, *args: Any) -> None:
        self.logger.error(message, *args)

    def command(self, argv: Sequence[str]) -> None:
        self.logger.info("exec: %s", " ".join(redact_command(argv)))

    @property
    def log_file(self) -> Path:
        return self.directory / "installation.log"

    # -- artefacts -------------------------------------------------------

    def write_system(self, system: SystemInfo, extra: dict[str, Any] | None = None) -> Path:
        payload = {
            "application": {"name": APP_NAME, "version": __version__},
            "generated_at": _now_iso(),
            "system": system.to_dict(),
        }
        if extra:
            payload.update(extra)
        return self._write_json("system.json", payload)

    def write_plan(self, plan: InstallationPlan) -> Path:
        payload = {
            "generated_at": _now_iso(),
            "dry_run": plan.dry_run,
            "system": plan.system.to_dict(),
            "items": [item.to_dict() for item in plan.items],
        }
        return self._write_json("plan.json", payload)

    def write_results(
        self, result: ExecutionResult, system: SystemInfo | None = None
    ) -> Path:
        payload = result.to_dict()
        payload["generated_at"] = _now_iso()
        payload["application"] = {"name": APP_NAME, "version": __version__}
        if system is not None:
            payload["system"] = system.to_dict()
        return self._write_json("results.json", payload)

    def _write_json(self, filename: str, payload: dict[str, Any]) -> Path:
        path = self.directory / filename
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=False, default=str) + "\n",
            encoding="utf-8",
        )
        return path


def _prepare_directory(logs_root: Path, stamp: str) -> tuple[Path, bool]:
    """Create the log directory, falling back to the temp dir if unwritable."""
    target = Path(logs_root) / stamp
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".write-probe"
        probe.touch()
        probe.unlink()
        return target, False
    except OSError:
        fallback = Path(tempfile.gettempdir()) / "usbinstaller-logs" / stamp
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback, True


def prune_sessions(logs_root: Path, keep: int) -> list[Path]:
    """Delete the oldest session directories beyond *keep*.

    Only directories this application created (``YYYY-MM-DD_HH-MM-SS`` named,
    containing ``installation.log``) are ever considered, so pointing the log
    directory at a populated folder cannot destroy unrelated data.
    """
    if keep <= 0 or not Path(logs_root).is_dir():
        return []
    pattern = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}")
    sessions = sorted(
        (
            p
            for p in Path(logs_root).iterdir()
            if p.is_dir() and pattern.match(p.name) and (p / "installation.log").exists()
        ),
        key=lambda p: p.name,
    )
    removed: list[Path] = []
    import shutil

    for stale in sessions[: max(0, len(sessions) - keep)]:
        shutil.rmtree(stale, ignore_errors=True)
        removed.append(stale)
    return removed


def latest_session(logs_root: Path) -> Path | None:
    """Most recent session directory, used by ``--retry``."""
    if not Path(logs_root).is_dir():
        return None
    candidates = [
        p
        for p in Path(logs_root).iterdir()
        if p.is_dir() and (p / "results.json").exists()
    ]
    return max(candidates, key=lambda p: p.name) if candidates else None


def failed_ids(results_file: Path) -> list[str]:
    """Read application ids that failed in a previous run."""
    try:
        data = json.loads(Path(results_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items: Iterable[dict[str, Any]] = data.get("items", []) if isinstance(data, dict) else []
    return [
        str(item.get("id"))
        for item in items
        if isinstance(item, dict) and item.get("status") == "failed" and item.get("id")
    ]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
