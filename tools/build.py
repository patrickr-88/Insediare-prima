#!/usr/bin/env python3
"""Build the frozen executables with PyInstaller.

Produces, for the host platform:

* ``USBInstaller`` / ``USBInstaller.exe`` — GUI + CLI in one binary,
* ``repository-manager`` / ``repository-manager.exe`` — drive maintenance,
* a versioned zip in ``dist/`` ready to copy onto a USB drive.

Because the engine has no third-party runtime dependencies, the bundle is just
CPython plus this package: the target computer needs no Python installed, and
there is no dependency tree to audit at deployment time.

Usage::

    python -m pip install pyinstaller
    python tools/build.py                 # host platform
    python tools/build.py --onedir        # faster start-up, folder layout
    python tools/build.py --universal2    # macOS universal (arm64 + x86_64)
"""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from usbinstaller.version import __version__  # noqa: E402

DIST = ROOT / "dist"
BUILD = ROOT / "build"

TARGETS = [
    # (executable name, entry module, windowed?)
    ("USBInstaller", "src/usbinstaller/__main__.py", True),
    ("repository-manager", "src/usbinstaller/repository_manager.py", False),
]


def platform_tag() -> tuple[str, str]:
    """Return ``(os_label, arch_label)`` used in the artefact name."""
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Windows":
        return "Windows", "arm64" if "arm" in machine else "x64"
    if system == "Darwin":
        return "macOS", "arm64" if machine in ("arm64", "aarch64") else "x86_64"
    return system, machine


def pyinstaller_available() -> bool:
    return shutil.which("pyinstaller") is not None or _module_available("PyInstaller")


def _module_available(name: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(name) is not None


def build_target(
    name: str,
    entry: str,
    windowed: bool,
    *,
    onedir: bool,
    universal2: bool,
    clean: bool,
) -> None:
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--name",
        name,
        "--paths",
        str(ROOT / "src"),
        "--distpath",
        str(DIST),
        "--workpath",
        str(BUILD / "pyinstaller"),
        "--specpath",
        str(BUILD),
        "--onedir" if onedir else "--onefile",
        # Everything the engine needs is imported dynamically through the
        # installer/detector registries, so name the packages explicitly.
        "--hidden-import",
        "usbinstaller.installers.windows",
        "--hidden-import",
        "usbinstaller.installers.macos",
        "--hidden-import",
        "usbinstaller.detection.windows",
        "--hidden-import",
        "usbinstaller.detection.macos",
        "--hidden-import",
        "usbinstaller.detection.generic",
    ]
    if windowed and platform.system() in ("Windows", "Darwin"):
        command.append("--windowed")
    else:
        command.append("--console")
    if universal2 and platform.system() == "Darwin":
        command += ["--target-architecture", "universal2"]
    if clean:
        command.append("--clean")
    command.append(str(ROOT / entry))

    print("+", " ".join(command))
    # Developer build tool: the command is assembled here, not user input.
    subprocess.run(command, check=True, cwd=ROOT)  # noqa: S603


def package(os_label: str, arch_label: str) -> Path:
    """Zip the built artefacts with a versioned, platform-tagged name."""
    archive = DIST / f"USBInstaller-{__version__}-{os_label}-{arch_label}.zip"
    archive.unlink(missing_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, _entry, _windowed in TARGETS:
            for candidate in (DIST / name, DIST / f"{name}.exe", DIST / f"{name}.app"):
                if candidate.is_file():
                    zf.write(candidate, candidate.name)
                elif candidate.is_dir():
                    for path in candidate.rglob("*"):
                        if path.is_file():
                            zf.write(path, str(path.relative_to(DIST)))
        # Ship the launchers and the starter repository layout alongside.
        for extra in ("repository/README.md", "README.md", "INSTALLATION.md"):
            source = ROOT / extra
            if source.exists():
                zf.write(source, Path(extra).name)
        for launcher in (ROOT / "repository").glob("Launch*"):
            zf.write(launcher, launcher.name)
    return archive


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build USBInstaller executables")
    parser.add_argument("--onedir", action="store_true", help="folder layout")
    parser.add_argument(
        "--universal2", action="store_true", help="macOS universal binary"
    )
    parser.add_argument("--clean", action="store_true", help="clean PyInstaller caches")
    parser.add_argument("--skip-package", action="store_true", help="do not zip")
    args = parser.parse_args(argv)

    if not pyinstaller_available():
        print(
            "PyInstaller is not installed. Run:\n"
            "  python -m pip install -e '.[build]'",
            file=sys.stderr,
        )
        return 2

    DIST.mkdir(exist_ok=True)
    for name, entry, windowed in TARGETS:
        build_target(
            name,
            entry,
            windowed,
            onedir=args.onedir,
            universal2=args.universal2,
            clean=args.clean,
        )

    os_label, arch_label = platform_tag()
    if args.universal2 and os_label == "macOS":
        arch_label = "universal"
    if args.skip_package:
        print(f"Built {os_label} {arch_label} artefacts in {DIST}")
        return 0

    archive = package(os_label, arch_label)
    print(f"\nRelease artefact: {archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
