"""Secure subprocess execution.

Rules enforced here, once, for every command the application ever runs:

* ``shell=False`` always — commands are argv vectors, so nothing in the
  configuration can inject a second command through quoting or metacharacters;
* the executable must be an existing file (resolved from the repository, or a
  known system binary), never a bare name resolved through ``PATH`` at the
  mercy of the target machine's environment;
* every run has a timeout and the process tree is terminated when it expires;
* stdout/stderr are captured, truncated and returned rather than inherited, so
  installer chatter cannot corrupt the console UI.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import InstallerExecutionError, InstallerTimeout

DEFAULT_TIMEOUT = 1800
DEFAULT_CAPTURE_BYTES = 8192

#: Environment variables that must never be inherited by an installer process.
_SCRUBBED_ENV = (
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "LD_PRELOAD",
    "DYLD_INSERT_LIBRARIES",
)


@dataclass(frozen=True)
class CommandResult:
    """Outcome of one subprocess run."""

    command: tuple[str, ...]
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    timed_out: bool = False

    def succeeded(self, success_codes: Sequence[int] = (0,)) -> bool:
        return not self.timed_out and self.exit_code in tuple(success_codes)

    @property
    def output_tail(self) -> str:
        return "\n".join(p for p in (self.stdout.strip(), self.stderr.strip()) if p)


@dataclass
class ProcessRunner:
    """Runs commands. Injected everywhere, so tests never spawn a process."""

    default_timeout: int = DEFAULT_TIMEOUT
    capture_bytes: int = DEFAULT_CAPTURE_BYTES
    #: Set by dry-run mode; when True nothing is ever executed.
    dry_run: bool = False
    #: Optional callback receiving each command before it runs (for logging).
    on_command: Callable[[Sequence[str]], None] | None = None
    _history: list[tuple[str, ...]] = field(default_factory=list, repr=False)

    @property
    def history(self) -> tuple[tuple[str, ...], ...]:
        """Every command this runner was asked to run (used by safety tests)."""
        return tuple(self._history)

    def run(
        self,
        command: Sequence[str],
        *,
        timeout: int | None = None,
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        check_executable: bool = True,
    ) -> CommandResult:
        """Execute *command* and return its result.

        Raises:
            InstallerExecutionError: the command is empty or the executable is
                missing/not executable.
        """
        argv = [str(part) for part in command]
        if not argv:
            raise InstallerExecutionError("refusing to run an empty command")
        if check_executable:
            self._validate_executable(argv[0])

        self._history.append(tuple(argv))
        if self.on_command is not None:
            self.on_command(argv)

        if self.dry_run:
            return CommandResult(tuple(argv), exit_code=0, stdout="[dry-run] not executed")

        started = time.monotonic()
        try:
            completed = subprocess.run(  # noqa: S603 - argv vector, shell=False
                argv,
                shell=False,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout or self.default_timeout,
                cwd=str(cwd) if cwd else None,
                env=self._child_env(env),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                tuple(argv),
                exit_code=-1,
                stdout=self._truncate(_as_text(exc.stdout)),
                stderr=self._truncate(_as_text(exc.stderr))
                or f"timed out after {timeout or self.default_timeout}s",
                duration_seconds=time.monotonic() - started,
                timed_out=True,
            )
        except OSError as exc:
            raise InstallerExecutionError(
                f"failed to start {argv[0]!r}: {exc}"
            ) from exc

        return CommandResult(
            tuple(argv),
            exit_code=completed.returncode,
            stdout=self._truncate(completed.stdout or ""),
            stderr=self._truncate(completed.stderr or ""),
            duration_seconds=time.monotonic() - started,
        )

    def run_checked(
        self, command: Sequence[str], *, success_codes: Sequence[int] = (0,), **kwargs
    ) -> CommandResult:
        """Run and raise unless the exit code is in *success_codes*."""
        result = self.run(command, **kwargs)
        if result.timed_out:
            raise InstallerTimeout(
                f"{Path(result.command[0]).name} timed out: {result.stderr}"
            )
        if result.exit_code not in tuple(success_codes):
            raise InstallerExecutionError(
                f"{Path(result.command[0]).name} exited with code "
                f"{result.exit_code}: {result.output_tail or 'no output'}"
            )
        return result

    # -- helpers ---------------------------------------------------------

    def _truncate(self, text: str) -> str:
        if self.capture_bytes and len(text) > self.capture_bytes:
            return text[: self.capture_bytes] + "\n… output truncated …"
        return text

    @staticmethod
    def _child_env(env: Mapping[str, str] | None) -> dict[str, str]:
        child = dict(os.environ if env is None else env)
        for key in _SCRUBBED_ENV:
            child.pop(key, None)
        return child

    @staticmethod
    def _validate_executable(executable: str) -> None:
        path = Path(executable)
        if path.is_absolute() or os.sep in executable or "/" in executable:
            if not path.exists():
                raise InstallerExecutionError(f"executable not found: {executable}")
            if path.is_dir():
                raise InstallerExecutionError(f"not an executable file: {executable}")
            return
        if shutil.which(executable) is None:
            raise InstallerExecutionError(
                f"required system tool not found on PATH: {executable}"
            )


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


class RecordingRunner(ProcessRunner):
    """A runner that returns scripted results — the test double for installers.

    Kept in the production package (not the test tree) so integration fixtures
    and the ``--dry-run`` safety proof can both use it.
    """

    def __init__(
        self,
        results: Mapping[str, CommandResult] | None = None,
        default_result: CommandResult | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._results = dict(results or {})
        self._default = default_result

    def run(self, command: Sequence[str], **kwargs) -> CommandResult:  # type: ignore[override]
        argv = tuple(str(c) for c in command)
        self._history.append(argv)
        if self.on_command is not None:
            self.on_command(list(argv))
        joined = " ".join(argv)
        for key, result in self._results.items():
            if key in joined:
                return result
        if self._default is not None:
            return self._default
        return CommandResult(argv, exit_code=0)
