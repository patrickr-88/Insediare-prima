"""Installer abstraction: one class per installer type, selected by a registry.

Adding support for a new package format means writing a new subclass and
decorating it with :func:`register` — no existing code changes, and the
configuration schema picks the type up through
``usbinstaller.config.catalogue.INSTALLER_TYPES``.
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import InstallerExecutionError, UnsupportedInstallerType
from ..models import OS, Application, PlatformPayload, SystemInfo
from .process import CommandResult, ProcessRunner


@dataclass
class InstallContext:
    """Everything an installer needs, injected rather than looked up globally."""

    system: SystemInfo
    runner: ProcessRunner
    repository_root: Path
    dry_run: bool = False
    default_timeout: int = 1800
    #: Receives human-readable progress lines.
    log: object | None = None
    #: Scratch directory for mount points and extractions.
    workspace: Path | None = None
    extra: dict = field(default_factory=dict)

    def timeout_for(self, payload: PlatformPayload) -> int:
        return payload.timeout_seconds or self.default_timeout

    def note(self, message: str) -> None:
        if self.log is not None and hasattr(self.log, "info"):
            self.log.info(message)  # type: ignore[attr-defined]


@dataclass(frozen=True)
class InstallOutcome:
    """What happened when an installer ran."""

    success: bool
    exit_code: int | None = None
    message: str = ""
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    commands: tuple[tuple[str, ...], ...] = ()

    @classmethod
    def from_result(
        cls, result: CommandResult, payload: PlatformPayload, note: str = ""
    ) -> InstallOutcome:
        ok = result.succeeded(payload.success_exit_codes)
        if ok:
            message = note or "installed successfully"
        elif result.timed_out:
            message = "installer timed out and was terminated"
        else:
            message = (
                f"installer exited with code {result.exit_code} "
                f"(expected {', '.join(str(c) for c in payload.success_exit_codes)})"
            )
        return cls(
            success=ok,
            exit_code=result.exit_code,
            message=message,
            stdout=result.stdout,
            stderr=result.stderr,
            timed_out=result.timed_out,
            commands=(result.command,),
        )


class Installer(ABC):  # noqa: B024 - subclasses override install() or build_command()
    """Base class for every installer type."""

    #: Configuration value this installer handles (``exe``, ``pkg``…).
    type: str = ""
    #: Platform this installer is valid on.
    os: OS = OS.UNKNOWN
    #: Whether the type normally needs administrator privileges.
    requires_admin_by_default: bool = True

    def supports(self, os_: OS) -> bool:
        return self.os is os_

    def build_command(
        self, installer_path: Path, payload: PlatformPayload, context: InstallContext
    ) -> list[str]:
        """Return the argv vector to execute. Override for simple installers."""
        raise NotImplementedError

    def install(
        self,
        app: Application,
        payload: PlatformPayload,
        installer_path: Path,
        context: InstallContext,
    ) -> InstallOutcome:
        """Install *app*. The default implementation runs one command.

        Multi-step formats (DMG mount → copy → detach) override this method.
        """
        command = self.build_command(installer_path, payload, context)
        context.note(f"Running: {' '.join(command)}")
        if context.dry_run:
            return InstallOutcome(
                success=True,
                exit_code=None,
                message="dry run: installer was not executed",
                commands=(tuple(command),),
            )
        result = context.runner.run(command, timeout=context.timeout_for(payload))
        return InstallOutcome.from_result(result, payload)

    def verify(
        self,
        app: Application,
        payload: PlatformPayload,
        installer_path: Path,
        context: InstallContext,
    ) -> str | None:
        """Optional post-install sanity check; return a problem description."""
        return None

    def preflight(self, installer_path: Path, payload: PlatformPayload) -> None:
        """Raise when the installer file itself is unusable."""
        if not installer_path.exists():
            raise InstallerExecutionError(f"installer not found: {installer_path}")
        if payload.type != "app" and installer_path.is_dir():
            raise InstallerExecutionError(
                f"expected a file, found a directory: {installer_path}"
            )


_REGISTRY: dict[tuple[OS, str], Installer] = {}


def register(installer):
    """Register an installer for its ``(os, type)`` pair."""
    instance = installer() if isinstance(installer, type) else installer
    if not instance.type or instance.os is OS.UNKNOWN:
        raise ValueError("installer must declare both 'type' and 'os'")
    _REGISTRY[(instance.os, instance.type)] = instance
    return installer


def get_installer(os_: OS, installer_type: str) -> Installer:
    """Look up the handler for an installer type.

    Raises:
        UnsupportedInstallerType: when nothing is registered — this is what
            stops the engine executing a file just because it exists on the USB
            drive.
    """
    try:
        return _REGISTRY[(os_, str(installer_type).lower())]
    except KeyError:
        raise UnsupportedInstallerType(
            f"no installer registered for type {installer_type!r} on {os_.value}"
        ) from None


def registered_types(os_: OS | None = None) -> tuple[str, ...]:
    return tuple(
        sorted(t for (o, t) in _REGISTRY if os_ is None or o is os_)
    )
