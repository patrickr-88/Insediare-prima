"""Writing to the catalogue: adding, updating and removing applications.

The rest of the engine only ever *reads* ``applications.json``. This module is
the single place that writes it, so every rule about doing that safely lives
here rather than being duplicated in the GUI:

* the edited document is re-parsed through the normal loader **before** it is
  written, so a change that would break the drive is rejected rather than
  saved;
* installer files are copied into ``installers/<os>/<id>/`` under the same path
  containment rules the engine enforces at install time;
* writes are atomic and keep a ``.bak`` of the previous catalogue;
* if anything fails part-way, copied files are removed again.

The GUI drives this API; nothing here imports or requires a UI.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import ConfigError, ConfigSchemaError, InstallerError
from ..models import OS, Arch, DetectionSpec
from ..security.paths import ALLOWED_EXTENSIONS, extension_allowed, resolve_within
from ..versioning import SCHEMES
from ..versioning import is_valid as version_is_valid
from .catalogue import DETECTION_METHODS, INSTALLER_TYPES, parse_catalogue

#: Windows device names that can never be used as a directory or file name.
_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

_ID_ALLOWED = re.compile(r"[^a-z0-9._-]+")


class AuthoringError(InstallerError):
    """A catalogue edit was refused."""


# --------------------------------------------------------------------------
# Suggestions — used by the GUI to pre-fill its form
# --------------------------------------------------------------------------


def suggest_id(name: str) -> str:
    """Turn a display name into a usable application id (``7-Zip`` → ``7-zip``)."""
    slug = _ID_ALLOWED.sub("-", str(name).strip().lower()).strip("-.")
    return slug or "application"


def suggest_type(path: Path | str, os_: OS) -> str | None:
    """Infer the installer type from a file's extension."""
    name = str(path).lower()
    for installer_type in INSTALLER_TYPES.get(os_, ()):
        if any(name.endswith(ext) for ext in ALLOWED_EXTENSIONS.get(installer_type, ())):
            return installer_type
    return None


def suggest_os(path: Path | str) -> OS | None:
    """Infer which platform an installer file belongs to."""
    for os_ in (OS.WINDOWS, OS.MACOS):
        if suggest_type(path, os_) is not None:
            return os_
    return None


def suggest_detection(os_: OS, name: str, app_bundle: str | None = None) -> DetectionSpec:
    """A sensible default detection rule for a newly added application."""
    if os_ is OS.WINDOWS:
        return DetectionSpec("windows_registry", {"display_name": f"^{re.escape(name)}"})
    return DetectionSpec("macos_app_bundle", {"bundle": app_bundle or f"{name}.app"})


def installer_filter(os_: OS) -> list[tuple[str, str]]:
    """File-dialog filters for *os_*, as ``(label, pattern)`` pairs."""
    labels = {
        "exe": "Windows installer",
        "msi": "Windows Installer package",
        "msix": "MSIX/APPX package",
        "powershell": "PowerShell script",
        "dmg": "Disk image",
        "pkg": "Installer package",
        "app": "Application bundle or archive",
        "shell": "Shell script",
    }
    out: list[tuple[str, str]] = []
    for installer_type in INSTALLER_TYPES.get(os_, ()):
        patterns = " ".join(f"*{ext}" for ext in ALLOWED_EXTENSIONS[installer_type])
        out.append((labels.get(installer_type, installer_type), patterns))
    return out


# --------------------------------------------------------------------------
# The edit request
# --------------------------------------------------------------------------


@dataclass
class InstallerDraft:
    """One platform's installer, as chosen by the technician."""

    os: OS
    #: The file the technician picked, anywhere on their machine.
    source: Path
    type: str
    arguments: list[str] = field(default_factory=list)
    requires_admin: bool = True
    architectures: tuple[Arch, ...] = ()
    detection: DetectionSpec | None = None
    app_bundle: str | None = None
    success_exit_codes: tuple[int, ...] = (0,)
    #: Set when the file is already inside the repository and must not be copied.
    already_in_repository: bool = False

    def destination_relative(self, app_id: str) -> str:
        """Where this installer will live on the drive."""
        if self.already_in_repository:
            return str(self.source).replace("\\", "/")
        return f"installers/{self.os.value}/{app_id}/{safe_filename(self.source.name)}"

    def to_payload(self, app_id: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "installer": self.destination_relative(app_id),
            "type": self.type,
            "requires_admin": bool(self.requires_admin),
        }
        if self.arguments:
            payload["arguments"] = list(self.arguments)
        if self.architectures:
            payload["architectures"] = [a.value for a in self.architectures]
        if self.app_bundle:
            payload["app_bundle"] = self.app_bundle
        if tuple(self.success_exit_codes) != (0,):
            payload["success_exit_codes"] = list(self.success_exit_codes)
        if self.detection is not None:
            payload["detection"] = {
                "method": self.detection.method,
                **self.detection.options,
            }
        return payload


@dataclass
class ApplicationDraft:
    """A complete application the technician wants to add to the drive."""

    id: str
    name: str
    version: str = ""
    description: str = ""
    category: str = "Uncategorised"
    version_scheme: str = "auto"
    dependencies: tuple[str, ...] = ()
    enabled: bool = True
    selected_by_default: bool = True
    installers: tuple[InstallerDraft, ...] = ()

    def to_entry(self) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "category": self.category,
            "enabled": self.enabled,
            "selected_by_default": self.selected_by_default,
            "version_scheme": self.version_scheme,
        }
        if self.dependencies:
            entry["dependencies"] = list(self.dependencies)
        for installer in self.installers:
            key = "windows" if installer.os is OS.WINDOWS else "macos"
            entry[key] = installer.to_payload(self.id)
        return entry


@dataclass(frozen=True)
class AddResult:
    """What an edit did (or, in preview mode, would do)."""

    app_id: str
    copied: tuple[tuple[str, str], ...] = ()  # (source, repository-relative)
    checksums: tuple[str, ...] = ()
    catalogue_path: str = ""
    preview: bool = False

    @property
    def summary(self) -> str:
        verb = "would be added" if self.preview else "added"
        lines = [f"{self.app_id} {verb} to the catalogue."]
        for _source, relative in self.copied:
            lines.append(f"  installer {'→ ' if self.preview else ''}{relative}")
        if self.checksums and not self.preview:
            lines.append(f"  {len(self.checksums)} checksum(s) recorded")
        return "\n".join(lines)


def safe_filename(name: str) -> str:
    """Reduce a chosen file's name to something safe to write onto the drive."""
    base = Path(str(name).replace("\\", "/")).name
    base = re.sub(r"[^A-Za-z0-9._ +()-]", "_", base).strip(" .")
    stem, _, extension = base.rpartition(".")
    if stem and stem.lower() in _RESERVED_NAMES:
        stem = f"{stem}_file"
        base = f"{stem}.{extension}"
    if not base:
        raise AuthoringError("the installer file name is empty after sanitisation")
    return base


# --------------------------------------------------------------------------
# The editor
# --------------------------------------------------------------------------


class CatalogueEditor:
    """Adds, updates and removes applications in a repository's catalogue."""

    def __init__(self, repository) -> None:
        self.repository = repository

    # -- reading ---------------------------------------------------------

    @property
    def catalogue_path(self) -> Path:
        return self.repository.config_dir / "applications.json"

    def _document(self) -> dict[str, Any]:
        raw = json.loads(self.catalogue_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("applications"), list):
            raise AuthoringError(
                f"{self.catalogue_path} is not a valid catalogue document"
            )
        return raw

    # -- validation ------------------------------------------------------

    def validate_draft(self, draft: ApplicationDraft) -> list[str]:
        """Return human-readable problems with *draft*; empty means it is fine.

        This is what the GUI calls as the technician types, so the messages are
        phrased for a person rather than for a log.
        """
        problems: list[str] = []

        if not draft.id.strip():
            problems.append("An application ID is required.")
        elif _ID_ALLOWED.search(draft.id) or draft.id != draft.id.lower():
            problems.append(
                "The ID may only contain lower-case letters, digits, '-', '_' and '.'."
            )
        elif self.repository.catalogue.get(draft.id) is not None:
            problems.append(
                f"An application with the ID '{draft.id}' is already on this drive."
            )

        if not draft.name.strip():
            problems.append("A display name is required.")

        if draft.version and not version_is_valid(draft.version, draft.version_scheme):
            problems.append(
                f"Version '{draft.version}' is not valid for the "
                f"'{draft.version_scheme}' scheme."
            )
        if draft.version_scheme not in SCHEMES:
            problems.append(f"Unknown version scheme '{draft.version_scheme}'.")

        for dependency in draft.dependencies:
            if (
                self.repository.catalogue.get(dependency) is None
                and dependency != draft.id
            ):
                problems.append(
                    f"Dependency '{dependency}' is not on this drive. Add it first."
                )
            if dependency == draft.id:
                problems.append("An application cannot depend on itself.")

        if not draft.installers:
            problems.append("Choose at least one installer (Windows and/or macOS).")

        seen: set[OS] = set()
        for installer in draft.installers:
            if installer.os in seen:
                problems.append(
                    f"There are two {installer.os.value} installers for this "
                    "application; only one is allowed."
                )
            seen.add(installer.os)
            problems.extend(self._validate_installer(installer, draft.id))

        return problems

    def _validate_installer(self, installer: InstallerDraft, app_id: str) -> list[str]:
        problems: list[str] = []
        label = installer.os.value

        if installer.type not in INSTALLER_TYPES.get(installer.os, ()):
            problems.append(
                f"'{installer.type}' is not a valid {label} installer type "
                f"(expected {', '.join(INSTALLER_TYPES.get(installer.os, ()))})."
            )
        if installer.already_in_repository:
            try:
                resolved = self.repository.resolve(str(installer.source))
            except ConfigError as exc:
                return [*problems, f"{label}: {exc}"]
            if not resolved.exists():
                problems.append(f"{label}: {installer.source} is not on the drive.")
        else:
            if not installer.source.exists():
                problems.append(f"{label}: {installer.source} does not exist.")
            elif installer.source.is_dir() and installer.type != "app":
                problems.append(f"{label}: {installer.source} is a folder, not a file.")
            elif installer.source.is_file() and installer.source.stat().st_size == 0:
                problems.append(f"{label}: {installer.source.name} is empty.")

        name = (
            str(installer.source)
            if installer.already_in_repository
            else installer.source.name
        )
        if installer.type in INSTALLER_TYPES.get(installer.os, ()) and not (
            extension_allowed(name, installer.type)
        ):
            expected = ", ".join(ALLOWED_EXTENSIONS.get(installer.type, ()))
            problems.append(
                f"{label}: {Path(name).name} does not look like a "
                f"'{installer.type}' installer (expected {expected})."
            )

        if installer.detection is not None and (
            installer.detection.method not in DETECTION_METHODS
        ):
            problems.append(
                f"{label}: unknown detection method "
                f"'{installer.detection.method}'."
            )

        if not installer.already_in_repository:
            # Make sure the computed destination stays inside the repository.
            try:
                resolve_within(
                    self.repository.root, installer.destination_relative(app_id)
                )
            except ConfigError as exc:
                problems.append(f"{label}: {exc}")

        return problems

    # -- writing ---------------------------------------------------------

    def add_application(
        self, draft: ApplicationDraft, *, preview: bool = False
    ) -> AddResult:
        """Add *draft* to the drive.

        Copies each installer into ``installers/<os>/<id>/``, appends the
        catalogue entry, records checksums, and re-validates the whole
        repository before committing. With ``preview=True`` nothing is written —
        the returned result describes what would happen.

        Raises:
            AuthoringError: the draft is invalid, or the resulting catalogue
                would not load. Nothing is left behind in either case.
        """
        problems = self.validate_draft(draft)
        if problems:
            raise AuthoringError("\n".join(problems))

        document = self._document()
        entry = draft.to_entry()
        planned = [
            (str(i.source), i.destination_relative(draft.id))
            for i in draft.installers
            if not i.already_in_repository
        ]

        if preview:
            # Parse a candidate document so a preview still catches schema errors.
            candidate = dict(document)
            candidate["applications"] = [*document["applications"], entry]
            self._parse_or_raise(candidate)
            return AddResult(
                app_id=draft.id,
                copied=tuple(planned),
                catalogue_path=str(self.catalogue_path),
                preview=True,
            )

        copied: list[Path] = []
        try:
            for installer in draft.installers:
                if installer.already_in_repository:
                    continue
                destination = self.repository.resolve(
                    installer.destination_relative(draft.id)
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    raise AuthoringError(
                        f"{destination} already exists on the drive; remove it "
                        "first or choose a different application ID."
                    )
                if installer.source.is_dir():
                    shutil.copytree(installer.source, destination)
                else:
                    shutil.copy2(installer.source, destination)
                copied.append(destination)

            document["applications"] = [*document["applications"], entry]
            self._parse_or_raise(document)
            self._write_document(document)
        except Exception:
            for path in copied:
                _remove(path)
            raise

        digests = self._record_checksums(draft)
        return AddResult(
            app_id=draft.id,
            copied=tuple(planned),
            checksums=tuple(digests),
            catalogue_path=str(self.catalogue_path),
        )

    def update_application(self, app_id: str, **fields: Any) -> None:
        """Change simple top-level fields (``version``, ``enabled``, …)."""
        document = self._document()
        for entry in document["applications"]:
            if entry.get("id") == app_id:
                entry.update(fields)
                break
        else:
            raise AuthoringError(f"no application with ID '{app_id}' on this drive")
        self._parse_or_raise(document)
        self._write_document(document)

    def remove_application(self, app_id: str, *, delete_files: bool = False) -> list[str]:
        """Remove an application, optionally deleting its installers.

        Only files this catalogue declares for *app_id* are ever deleted, and
        only when ``delete_files`` is explicitly requested.
        """
        application = self.repository.catalogue.get(app_id)
        if application is None:
            raise AuthoringError(f"no application with ID '{app_id}' on this drive")

        dependents = [
            other.id
            for other in self.repository.catalogue
            if app_id in other.dependencies
        ]
        if dependents:
            raise AuthoringError(
                f"'{app_id}' is required by: {', '.join(dependents)}. "
                "Remove those first, or edit their dependencies."
            )

        document = self._document()
        document["applications"] = [
            e for e in document["applications"] if e.get("id") != app_id
        ]
        self._parse_or_raise(document)
        self._write_document(document)

        removed: list[str] = []
        if delete_files:
            for payload in application.platforms.values():
                try:
                    path = self.repository.resolve(payload.installer)
                except ConfigError:
                    continue
                if path.exists():
                    _remove(path)
                    removed.append(payload.installer)
                parent = path.parent
                if parent != self.repository.root and parent.is_dir():
                    try:
                        next(parent.iterdir())
                    except StopIteration:
                        parent.rmdir()
        return removed

    # -- helpers ---------------------------------------------------------

    def _parse_or_raise(self, document: dict[str, Any]) -> None:
        """Reject an edit that would produce a catalogue the engine cannot load."""
        try:
            parse_catalogue(document, source=self.catalogue_path)
        except ConfigSchemaError as exc:
            raise AuthoringError(f"the change would break the catalogue: {exc}") from exc

    def _write_document(self, document: dict[str, Any]) -> None:
        """Write atomically, keeping the previous catalogue as ``.bak``."""
        path = self.catalogue_path
        payload = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
        if path.exists():
            shutil.copy2(path, path.with_suffix(".json.bak"))
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(path)

    def _record_checksums(self, draft: ApplicationDraft) -> list[str]:
        store = self.repository.checksums
        digests: list[str] = []
        for installer in draft.installers:
            relative = installer.destination_relative(draft.id)
            path = self.repository.resolve(relative)
            if path.exists():
                store.update(relative, path)
                digests.append(relative)
        if digests:
            store.save(self.repository.checksums_path)
        return digests


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)
