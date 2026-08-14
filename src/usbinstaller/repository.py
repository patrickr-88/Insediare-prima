"""The USB software repository: layout, discovery and loading.

The repository is *data*; the engine is *code*. Nothing in this module knows
which applications exist — it only knows where to find them. That separation is
what lets a technician swap Firefox 140 for Firefox 145 without touching source.

Layout::

    <root>/
      config/applications.json
      config/settings.json
      installers/<os>/<app>/<file>
      scripts/<os>/...
      checksums/checksums.json
      logs/<timestamp>/...
      documentation/
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .config.catalogue import Catalogue, load_catalogue
from .config.settings import Settings
from .errors import RepositoryError
from .security.checksums import ChecksumStore
from .security.paths import resolve_within

CONFIG_DIRNAME = "config"
APPLICATIONS_FILENAME = "applications.json"
SETTINGS_FILENAME = "settings.json"
CHECKSUMS_RELPATH = "checksums/checksums.json"
INSTALLERS_DIRNAME = "installers"
SCRIPTS_DIRNAME = "scripts"

#: How far up the tree :meth:`Repository.discover` will walk.
MAX_DISCOVERY_DEPTH = 8


@dataclass(frozen=True)
class Repository:
    """A located, loaded software repository."""

    root: Path
    catalogue: Catalogue
    settings: Settings
    checksums: ChecksumStore

    # -- layout ----------------------------------------------------------

    @property
    def config_dir(self) -> Path:
        return self.root / CONFIG_DIRNAME

    @property
    def installers_dir(self) -> Path:
        return self.root / INSTALLERS_DIRNAME

    @property
    def scripts_dir(self) -> Path:
        return self.root / SCRIPTS_DIRNAME

    @property
    def checksums_path(self) -> Path:
        return self.root / CHECKSUMS_RELPATH

    @property
    def logs_dir(self) -> Path:
        return self.root / (self.settings.log_directory or "logs")

    # -- path safety -----------------------------------------------------

    def resolve(self, relative_path: str) -> Path:
        """Resolve a configured, repository-relative path (never escapes root)."""
        return resolve_within(self.root, relative_path)

    def relative(self, path: Path) -> str:
        """Inverse of :meth:`resolve`, for logging and checksum keys."""
        try:
            return path.resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError:
            return str(path)

    # -- discovery -------------------------------------------------------

    @staticmethod
    def looks_like_repository(path: Path) -> bool:
        return (path / CONFIG_DIRNAME / APPLICATIONS_FILENAME).is_file()

    @classmethod
    def discover(cls, start: Path | str | None = None) -> Path:
        """Find the repository root.

        Search order:

        1. ``$USB_INSTALLER_ROOT`` (explicit override, e.g. for automation),
        2. *start* (default: the directory of the running executable/script)
           and each of its parents,
        3. the current working directory and its parents.
        """
        env_root = os.environ.get("USB_INSTALLER_ROOT")
        if env_root:
            candidate = Path(env_root).expanduser()
            if not cls.looks_like_repository(candidate):
                raise RepositoryError(
                    f"USB_INSTALLER_ROOT={env_root} does not contain "
                    f"{CONFIG_DIRNAME}/{APPLICATIONS_FILENAME}"
                )
            return candidate.resolve()

        starts: list[Path] = []
        if start is not None:
            starts.append(Path(start))
        else:
            starts.append(_executable_dir())
        starts.append(Path.cwd())

        seen: set[Path] = set()
        for origin in starts:
            origin = origin.resolve()
            for depth, candidate in enumerate([origin, *origin.parents]):
                if depth > MAX_DISCOVERY_DEPTH or candidate in seen:
                    continue
                seen.add(candidate)
                if cls.looks_like_repository(candidate):
                    return candidate

        raise RepositoryError(
            "Could not locate the software repository. Expected to find "
            f"{CONFIG_DIRNAME}/{APPLICATIONS_FILENAME} next to the application, "
            "or set USB_INSTALLER_ROOT to the repository root."
        )

    # -- loading ---------------------------------------------------------

    @classmethod
    def load(cls, root: Path | str | None = None) -> Repository:
        """Locate (if needed) and load a repository."""
        root_path = Path(root).resolve() if root is not None else cls.discover()
        if not root_path.is_dir():
            raise RepositoryError(f"repository root is not a directory: {root_path}")
        apps_path = root_path / CONFIG_DIRNAME / APPLICATIONS_FILENAME
        if not apps_path.is_file():
            raise RepositoryError(f"missing configuration file: {apps_path}")

        catalogue = load_catalogue(apps_path)
        settings = Settings.load(root_path / CONFIG_DIRNAME / SETTINGS_FILENAME)
        checksums = ChecksumStore.load(root_path / CHECKSUMS_RELPATH)
        return cls(
            root=root_path,
            catalogue=catalogue,
            settings=settings,
            checksums=checksums,
        )

    def is_writable(self) -> bool:
        """Whether logs can be written to the drive itself (USB may be read-only)."""
        probe = self.root / ".usbinstaller-write-probe"
        try:
            probe.touch()
            probe.unlink()
            return True
        except OSError:
            return False


def _executable_dir() -> Path:
    """Directory of the frozen executable, or of the package during development."""
    import sys

    if getattr(sys, "frozen", False):  # PyInstaller
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent
