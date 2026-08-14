"""Administrator / root privilege handling.

Deliberate non-goals: this module never attempts to acquire privileges
silently, never caches a password, and never bypasses UAC, Gatekeeper or SIP.
The only escalation path offered is re-launching the whole application through
the operating system's own consent UI, which the technician sees and approves.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from ..models import OS, SystemInfo


@dataclass(frozen=True)
class PrivilegeRequirement:
    """Whether the current plan can run with the privileges we hold."""

    required: bool
    satisfied: bool
    app_ids: tuple[str, ...] = ()

    @property
    def blocked(self) -> bool:
        return self.required and not self.satisfied

    def message(self, os_: OS) -> str:
        if not self.required:
            return "No administrator privileges are required for this selection."
        if self.satisfied:
            return "Administrator privileges: held."
        apps = ", ".join(self.app_ids) if self.app_ids else "one or more applications"
        if os_ is OS.WINDOWS:
            how = (
                "Close this window, right-click the launcher and choose "
                "'Run as administrator'."
            )
        else:
            how = (
                "Re-run the installer with 'sudo', or start the app and approve "
                "the macOS authentication prompt."
            )
        return (
            f"Administrator privileges are required by: {apps}.\n"
            f"They are NOT currently held. {how}"
        )


def requirement_for(system: SystemInfo, plan_items) -> PrivilegeRequirement:
    """Compute the privilege requirement for the executable part of a plan."""
    needing = tuple(
        item.app_id
        for item in plan_items
        if getattr(item, "will_execute", False) and item.requires_admin
    )
    return PrivilegeRequirement(
        required=bool(needing), satisfied=system.is_admin, app_ids=needing
    )


def elevation_command(os_: OS, argv: list[str] | None = None) -> list[str] | None:
    """The command a technician would run to re-launch with privileges.

    Returned for *display*; the application does not execute it on the user's
    behalf, so the escalation is always an explicit human action.
    """
    argv = list(argv if argv is not None else sys.argv)
    if not argv:
        return None
    if os_ is OS.WINDOWS:
        return ["powershell", "-Command", "Start-Process", "-Verb", "RunAs", argv[0]]
    return ["sudo", *argv]


def can_write(path: Path | str) -> bool:
    """Whether the current user can create files in *path*."""
    return os.access(str(path), os.W_OK | os.X_OK)


def macos_authorization_available() -> bool:  # pragma: no cover - macOS only
    """Whether ``osascript`` is present to show the standard consent dialog."""
    return Path("/usr/bin/osascript").exists()


def drop_privileges_warning(system: SystemInfo) -> str | None:
    """Warn when running as root but nothing in the plan needs it."""
    if system.os is not OS.WINDOWS and system.is_admin and os.environ.get("SUDO_USER"):
        return (
            "Running as root. Applications that do not require administrator "
            "privileges are still installed as root; this is expected for "
            "system-wide installers."
        )
    return None


def sudo_available() -> bool:
    """Whether ``sudo`` exists (used to explain options, not to invoke it)."""
    try:
        result = subprocess.run(  # noqa: S607 - read-only capability probe
            ["which", "sudo"],  # noqa: S607 - probe only; sudo is never invoked
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        return False
    return result.returncode == 0
