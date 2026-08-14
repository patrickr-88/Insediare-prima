"""Per-application information for the interface.

Everything the technician needs to answer "what exactly will this do to my
machine?" — the installer that will run, its arguments, whether it is verified,
what is installed right now, and how the two versions compare.

Assembled here rather than in the GUI so it is testable without a display, and
so the CLI can show the same facts.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models import OS, Application, Arch, InstalledApp, SystemInfo
from ..security.paths import extension_allowed
from ..versioning import Comparison, compare


@dataclass(frozen=True)
class ApplicationDetails:
    """A flat, display-ready description of one application on this machine."""

    id: str
    name: str
    version: str
    category: str
    description: str
    enabled: bool

    compatible: bool
    incompatibility: str | None

    installer_path: str | None
    installer_exists: bool
    installer_size: int | None
    installer_type: str | None
    arguments: tuple[str, ...]
    requires_admin: bool
    architectures: tuple[str, ...]
    detection_method: str

    checksum_state: str  # "recorded" | "missing" | "verified" | "mismatch"
    dependencies: tuple[str, ...]
    missing_dependencies: tuple[str, ...]

    installed: InstalledApp | None
    comparison: Comparison

    @property
    def installed_version(self) -> str | None:
        return self.installed.version if self.installed else None

    @property
    def status_line(self) -> str:
        """One-line status, the way the list and the details header show it."""
        if not self.compatible:
            return self.incompatibility or "Not available on this computer"
        if not self.installer_exists:
            return "Installer missing from the drive"
        if self.checksum_state == "mismatch":
            return "Checksum mismatch — this installer will not be run"
        if self.installed is None or not self.installed.installed:
            return "Not installed"
        installed = self.installed.version or "version unknown"
        return {
            Comparison.OLDER: f"Update available ({installed} → {self.version})",
            Comparison.SAME: f"Up to date ({installed})",
            Comparison.NEWER: f"Newer version installed ({installed})",
            Comparison.UNKNOWN: f"Installed ({installed}); versions not comparable",
        }[self.comparison]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "category": self.category,
            "description": self.description,
            "enabled": self.enabled,
            "compatible": self.compatible,
            "incompatibility": self.incompatibility,
            "installer": self.installer_path,
            "installer_exists": self.installer_exists,
            "installer_size": self.installer_size,
            "installer_type": self.installer_type,
            "arguments": list(self.arguments),
            "requires_admin": self.requires_admin,
            "architectures": list(self.architectures),
            "detection_method": self.detection_method,
            "checksum_state": self.checksum_state,
            "dependencies": list(self.dependencies),
            "missing_dependencies": list(self.missing_dependencies),
            "installed": self.installed.to_dict() if self.installed else None,
            "comparison": self.comparison.value,
            "status": self.status_line,
        }

    def render(self) -> str:
        """The text shown in the GUI's information panel."""
        lines = [
            self.name + (f"  {self.version}" if self.version else ""),
            self.status_line,
            "",
        ]
        if self.description:
            lines += [self.description, ""]

        rows: list[tuple[str, str]] = [
            ("Category", self.category),
            ("On this drive", self.version or "no version declared"),
        ]
        if self.installed and self.installed.installed:
            rows.append(("Installed", self.installed.version or "version unknown"))
            if self.installed.location:
                rows.append(("Location", self.installed.location))
        rows += [
            ("Installer", self.installer_path or "—"),
            ("Type", (self.installer_type or "—").upper()),
            ("Size", format_size(self.installer_size)),
            ("Arguments", " ".join(self.arguments) if self.arguments else "none"),
            ("Administrator", "required" if self.requires_admin else "not required"),
            (
                "Architectures",
                ", ".join(self.architectures) if self.architectures else "any",
            ),
            ("Detection", self.detection_method),
            ("Checksum", _CHECKSUM_LABELS[self.checksum_state]),
        ]
        if self.dependencies:
            rows.append(("Requires", ", ".join(self.dependencies)))
        if self.missing_dependencies:
            rows.append(("Missing", ", ".join(self.missing_dependencies)))

        width = max(len(label) for label, _ in rows)
        for label, value in rows:
            # Paths are long enough to wrap in a narrow panel, which would break
            # the column alignment — give them a line of their own instead.
            if len(value) > 30:
                lines += [label, f"    {value}"]
            else:
                lines.append(f"{label.ljust(width)}   {value}")
        return "\n".join(lines)


#: Short for the healthy states, explicit for the ones needing action.
_CHECKSUM_LABELS = {
    "verified": "verified (SHA-256 matches)",
    "recorded": "recorded",
    "missing": "not recorded — run Drive → Update Checksums",
    "mismatch": "MISMATCH — the file changed since it was recorded",
}


def format_size(size: int | None) -> str:
    """Human-readable file size."""
    if size is None:
        return "—"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"  # pragma: no cover - unreachable, kept for clarity


def describe(
    application: Application,
    repository,
    system: SystemInfo,
    installed: InstalledApp | None = None,
    *,
    verify_checksum: bool = False,
) -> ApplicationDetails:
    """Assemble the details for one application on this machine.

    Detection is *not* run here — pass an already-detected :class:`InstalledApp`
    so the caller controls when the (potentially slow) probes happen.
    """
    payload = application.payload_for(system.os)
    # "Compatible" means the technician could actually install it here, so a
    # disabled application counts as unavailable even though its installer
    # would otherwise run on this machine.
    incompatibility = _incompatibility(application, payload, system)
    compatible = incompatibility is None

    installer_path: str | None = None
    absolute: Path | None = None
    size: int | None = None
    exists = False
    checksum_state = "missing"

    if payload is not None:
        installer_path = payload.installer
        try:
            absolute = repository.resolve(payload.installer)
        except Exception:  # PathTraversalError — surfaced via status/validation
            absolute = None
        if absolute is not None and absolute.exists():
            exists = True
            if absolute.is_file():
                size = absolute.stat().st_size
            if not extension_allowed(payload.installer, payload.type):
                checksum_state = "missing"

        expected = payload.sha256 or repository.checksums.expected_for(payload.installer)
        if expected is None:
            checksum_state = "missing"
        elif verify_checksum and exists and absolute is not None:
            from ..security.checksums import digest_of

            checksum_state = "verified" if digest_of(absolute) == expected else "mismatch"
        else:
            checksum_state = "recorded"

    comparison = Comparison.UNKNOWN
    if installed is not None and installed.installed:
        comparison = compare(
            installed.version,
            application.version,
            application.version_scheme,
            application.version_pattern,
        )

    catalogue_ids = set(repository.catalogue.ids)
    return ApplicationDetails(
        id=application.id,
        name=application.name,
        version=application.version,
        category=application.category,
        description=application.description,
        enabled=application.enabled,
        compatible=bool(compatible),
        incompatibility=incompatibility,
        installer_path=installer_path,
        installer_exists=exists,
        installer_size=size,
        installer_type=payload.type if payload else None,
        arguments=tuple(payload.arguments) if payload else (),
        requires_admin=bool(payload.requires_admin) if payload else False,
        architectures=tuple(a.value for a in payload.architectures) if payload else (),
        detection_method=application.detection_for(system.os).method,
        checksum_state=checksum_state,
        dependencies=application.dependencies,
        missing_dependencies=tuple(
            d for d in application.dependencies if d not in catalogue_ids
        ),
        installed=installed,
        comparison=comparison,
    )


def _incompatibility(application: Application, payload, system: SystemInfo) -> str | None:
    if not application.enabled:
        return "Disabled in this drive's configuration"
    if payload is None:
        label = "Windows" if system.os is OS.WINDOWS else system.os.value
        return f"No {label} installer on this drive"
    if not payload.supports_arch(system.arch):
        wanted = ", ".join(a.value for a in payload.architectures)
        return f"Requires {wanted}; this computer is {_arch_label(system.arch)}"
    return None


def _arch_label(arch: Arch) -> str:
    return {Arch.ARM64: "arm64", Arch.X64: "x64", Arch.X86: "x86"}.get(arch, arch.value)
