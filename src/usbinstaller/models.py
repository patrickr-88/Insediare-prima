"""Typed domain models shared by every layer of the application.

These are plain, immutable-ish dataclasses with no I/O so they can be
constructed freely in tests.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class OS(str, enum.Enum):
    """Operating systems the engine understands."""

    WINDOWS = "windows"
    MACOS = "macos"
    LINUX = "linux"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, value: str) -> OS:
        try:
            return cls(str(value).strip().lower())
        except ValueError:
            return cls.UNKNOWN


class Arch(str, enum.Enum):
    """CPU architectures the engine understands."""

    X64 = "x64"
    X86 = "x86"
    ARM64 = "arm64"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, value: str) -> Arch:
        """Map an alias (``amd64``, ``aarch64``, ``apple_silicon``…) to a member."""
        key = str(value).strip().lower()
        return _ARCH_ALIASES.get(key, cls.UNKNOWN)


_ARCH_ALIASES: dict[str, Arch] = {
    "x64": Arch.X64,
    "x86_64": Arch.X64,
    "amd64": Arch.X64,
    "intel": Arch.X64,
    "x86": Arch.X86,
    "i386": Arch.X86,
    "i686": Arch.X86,
    "win32": Arch.X86,
    "arm64": Arch.ARM64,
    "aarch64": Arch.ARM64,
    "apple_silicon": Arch.ARM64,
    "arm64e": Arch.ARM64,
}


class Action(str, enum.Enum):
    """What the planner decided to do with an application."""

    INSTALL = "install"
    UPGRADE = "upgrade"
    SKIP = "skip"
    ERROR = "error"


class Status(str, enum.Enum):
    """Outcome of an executed plan item."""

    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    DRY_RUN = "dry_run"


@dataclass(frozen=True)
class SystemInfo:
    """Everything the engine knows about the machine it is running on."""

    os: OS
    os_version: str
    os_name: str
    arch: Arch
    is_admin: bool
    hostname: str
    repository_path: str | None = None
    free_disk_bytes: int | None = None
    python_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "os": self.os.value,
            "os_name": self.os_name,
            "os_version": self.os_version,
            "arch": self.arch.value,
            "is_admin": self.is_admin,
            "hostname": self.hostname,
            "repository_path": self.repository_path,
            "free_disk_bytes": self.free_disk_bytes,
            "python_version": self.python_version,
        }


@dataclass(frozen=True)
class DetectionSpec:
    """How to decide whether an application is already installed."""

    method: str = "none"
    #: Method-specific options, e.g. ``{"display_name": "Mozilla Firefox.*"}``.
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Prerequisite:
    """A declarative condition that must hold before an app may be installed.

    Unlike a dependency (another catalogue application that gets installed
    first), a prerequisite is a fact about the machine that the engine can only
    check, never satisfy — OS version, free disk, admin rights.
    """

    type: str
    value: Any = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "value": self.value, "message": self.message}


@dataclass(frozen=True)
class PostInstallAction:
    """An optional action run after a successful installation."""

    type: str
    #: Repository-relative script path for ``script`` actions.
    script: str | None = None
    arguments: list[str] = field(default_factory=list)
    message: str | None = None
    timeout_seconds: int | None = None


@dataclass(frozen=True)
class PlatformPayload:
    """The per-OS half of an application definition."""

    installer: str
    type: str
    arguments: list[str] = field(default_factory=list)
    architectures: tuple[Arch, ...] = ()
    requires_admin: bool = False
    sha256: str | None = None
    timeout_seconds: int | None = None
    success_exit_codes: tuple[int, ...] = (0,)
    detection: DetectionSpec | None = None
    post_install: tuple[PostInstallAction, ...] = ()
    #: DMG-specific: name of the ``.app`` bundle inside the mounted image.
    app_bundle: str | None = None
    #: Extra key/values preserved verbatim for installer-specific behaviour.
    extra: dict[str, Any] = field(default_factory=dict)

    def supports_arch(self, arch: Arch) -> bool:
        if not self.architectures:
            return True
        return arch in self.architectures


@dataclass(frozen=True)
class Application:
    """A single configured application, across all platforms."""

    id: str
    name: str
    version: str = ""
    description: str = ""
    category: str = "Uncategorised"
    enabled: bool = True
    version_scheme: str = "auto"
    version_pattern: str | None = None
    dependencies: tuple[str, ...] = ()
    prerequisites: tuple[Prerequisite, ...] = ()
    detection: DetectionSpec = field(default_factory=DetectionSpec)
    platforms: dict[OS, PlatformPayload] = field(default_factory=dict)
    selected_by_default: bool = True

    def payload_for(self, os_: OS) -> PlatformPayload | None:
        return self.platforms.get(os_)

    def supports(self, os_: OS, arch: Arch) -> bool:
        payload = self.platforms.get(os_)
        return payload is not None and payload.supports_arch(arch)

    def detection_for(self, os_: OS) -> DetectionSpec:
        payload = self.platforms.get(os_)
        if payload is not None and payload.detection is not None:
            return payload.detection
        return self.detection


@dataclass(frozen=True)
class InstalledApp:
    """The result of probing the machine for an existing installation."""

    installed: bool
    version: str | None = None
    location: str | None = None
    method: str = "none"
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "installed": self.installed,
            "version": self.version,
            "location": self.location,
            "method": self.method,
            "detail": self.detail,
        }


@dataclass
class PlanItem:
    """One line of the installation plan."""

    application: Application
    action: Action
    reason: str = ""
    installed: InstalledApp | None = None
    installer_path: str | None = None
    requires_admin: bool = False
    checksum_ok: bool | None = None
    #: Populated by the executor.
    status: Status | None = None
    exit_code: int | None = None
    error: str | None = None
    duration_seconds: float = 0.0
    started_at: str | None = None
    finished_at: str | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""

    @property
    def app_id(self) -> str:
        return self.application.id

    @property
    def will_execute(self) -> bool:
        return self.action in (Action.INSTALL, Action.UPGRADE)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.application.id,
            "name": self.application.name,
            "version": self.application.version,
            "action": self.action.value,
            "reason": self.reason,
            "installer": self.installer_path,
            "requires_admin": self.requires_admin,
            "checksum_ok": self.checksum_ok,
            "installed": self.installed.to_dict() if self.installed else None,
            "status": self.status.value if self.status else None,
            "exit_code": self.exit_code,
            "error": self.error,
            "duration_seconds": round(self.duration_seconds, 3),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


@dataclass(frozen=True)
class InstallationPlan:
    """An ordered, validated set of plan items plus the system it targets."""

    system: SystemInfo
    items: tuple[PlanItem, ...]
    dry_run: bool = False

    def by_action(self, action: Action) -> tuple[PlanItem, ...]:
        return tuple(item for item in self.items if item.action is action)

    @property
    def executable_items(self) -> tuple[PlanItem, ...]:
        return tuple(item for item in self.items if item.will_execute)

    @property
    def requires_admin(self) -> bool:
        return any(item.requires_admin for item in self.executable_items)


@dataclass(frozen=True)
class ExecutionResult:
    """Aggregate outcome of executing a plan."""

    items: tuple[PlanItem, ...]
    dry_run: bool = False

    def count(self, status: Status) -> int:
        return sum(1 for item in self.items if item.status is status)

    @property
    def succeeded(self) -> int:
        return self.count(Status.SUCCESS) + self.count(Status.DRY_RUN)

    @property
    def failed(self) -> int:
        return self.count(Status.FAILED)

    @property
    def skipped(self) -> int:
        return self.count(Status.SKIPPED)

    @property
    def failures(self) -> tuple[PlanItem, ...]:
        return tuple(i for i in self.items if i.status is Status.FAILED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "summary": {
                "successful": self.succeeded,
                "failed": self.failed,
                "skipped": self.skipped,
                "total": len(self.items),
            },
            "items": [item.to_dict() for item in self.items],
        }
