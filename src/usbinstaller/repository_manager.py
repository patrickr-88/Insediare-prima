"""Repository maintenance: checksums, inventory, drift reporting.

This tool operates on the USB drive only. It never inspects or modifies the
computer it runs on, which is why it is safe to run on a technician's own
workstation while preparing a drive.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config.validator import validate_repository
from .models import OS
from .repository import Repository
from .security.checksums import digest_of
from .version import APP_NAME, __version__


@dataclass
class RepositoryManager:
    """Inventory and checksum maintenance for a repository."""

    repository: Repository

    # -- inventory -------------------------------------------------------

    def declared_installers(self) -> list[tuple[str, str, OS]]:
        """Every ``(app_id, relative_path, os)`` triple in the configuration."""
        out: list[tuple[str, str, OS]] = []
        for app in self.repository.catalogue:
            for os_, payload in app.platforms.items():
                out.append((app.id, payload.installer, os_))
        return out

    def present_installer_files(self) -> list[str]:
        """Files that actually exist under ``installers/`` (repository-relative)."""
        base = self.repository.installers_dir
        if not base.is_dir():
            return []
        files: list[str] = []
        for path in sorted(base.rglob("*")):
            # A .app bundle is a directory but is a single installer artefact.
            if path.is_dir() and path.name.endswith(".app"):
                files.append(self.repository.relative(path))
            elif path.is_file() and not _is_inside_bundle(path, base):
                files.append(self.repository.relative(path))
        return files

    def inventory(self) -> dict[str, Any]:
        """Compare what the configuration declares with what is on the drive."""
        declared = self.declared_installers()
        declared_paths = {rel for _, rel, _ in declared}
        present = set(self.present_installer_files())

        missing = sorted(
            rel for rel in declared_paths if not self.repository.resolve(rel).exists()
        )
        orphaned = sorted(present - declared_paths)
        unchecksummed = sorted(
            rel
            for rel in declared_paths
            if self.repository.checksums.expected_for(rel) is None
        )
        by_os = {
            os_.value: sum(1 for _, _, o in declared if o is os_)
            for os_ in (OS.WINDOWS, OS.MACOS)
        }
        return {
            "repository": str(self.repository.root),
            "applications": len(self.repository.catalogue),
            "enabled": len(self.repository.catalogue.enabled()),
            "installers_declared": len(declared_paths),
            "installers_by_os": by_os,
            "missing": missing,
            "orphaned": orphaned,
            "without_checksum": unchecksummed,
        }

    # -- checksums -------------------------------------------------------

    def generate_checksums(
        self, *, dry_run: bool = False, prune: bool = False
    ) -> dict[str, Any]:
        """Record the SHA-256 of every declared installer.

        Only paths named in the configuration are hashed — the manager never
        walks the drive and blesses whatever it finds, so a stray file dropped
        onto the USB stick cannot acquire a checksum and look legitimate.
        """
        store = self.repository.checksums
        lines: list[str] = []
        errors: list[str] = []
        written = 0
        seen: list[str] = []

        for app_id, relative, _os in sorted(self.declared_installers()):
            try:
                path = self.repository.resolve(relative)
            except Exception as exc:  # PathTraversalError
                errors.append(f"{app_id}: {exc}")
                lines.append(f"✗ {relative}: {exc}")
                continue
            if not path.exists():
                errors.append(f"{app_id}: installer missing ({relative})")
                lines.append(f"✗ {relative}: file not found")
                continue

            seen.append(relative)
            previous = store.expected_for(relative)
            digest = digest_of(path) if dry_run else store.update(relative, path)
            written += 1
            if previous is None:
                lines.append(f"+ {relative}  {digest}")
            elif previous != digest:
                lines.append(f"~ {relative}  {previous} → {digest}")
            else:
                lines.append(f"= {relative}  unchanged")

        removed: list[str] = []
        if prune:
            removed = store.prune(seen)
            lines.extend(f"- {key}  removed (no longer declared)" for key in removed)

        if not dry_run:
            store.save(self.repository.checksums_path)

        return {
            "path": str(self.repository.checksums_path),
            "written": written,
            "removed": removed,
            "errors": errors,
            "lines": lines,
            "dry_run": dry_run,
        }

    def verify_checksums(self) -> dict[str, Any]:
        """Re-hash every declared installer and report mismatches."""
        results = []
        for _, relative, _ in sorted(self.declared_installers()):
            path = self.repository.resolve(relative)
            result = self.repository.checksums.verify(relative, path)
            results.append(
                {
                    "path": relative,
                    "status": result.status,
                    "expected": result.expected,
                    "actual": result.actual,
                }
            )
        return {
            "ok": all(r["status"] == "ok" for r in results),
            "results": results,
        }


def _is_inside_bundle(path: Path, base: Path) -> bool:
    return any(part.endswith(".app") for part in path.relative_to(base).parts[:-1])


# -- command-line entry point -------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="repository-manager",
        description=(
            f"{APP_NAME} repository manager — prepares and audits a USB drive. "
            "Never modifies the computer it runs on."
        ),
    )
    parser.add_argument("--repository", metavar="PATH", help="repository root")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true", help="list the catalogue")
    group.add_argument("--validate", action="store_true", help="validate the repository")
    group.add_argument(
        "--checksums", action="store_true", help="generate or refresh checksums.json"
    )
    group.add_argument(
        "--verify-checksums", action="store_true", help="re-hash and compare"
    )
    group.add_argument("--status", action="store_true", help="inventory and drift report")
    group.add_argument("--version", action="store_true", help="show the version")
    parser.add_argument("--prune", action="store_true", help="drop stale checksum entries")
    parser.add_argument("--dry-run", action="store_true", help="make no changes")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.version:
        print(f"{APP_NAME} repository manager {__version__}")
        return 0

    root = Path(args.repository) if args.repository else Repository.discover()

    if args.validate:
        validation = validate_repository(root, check_checksums=True)
        print(
            json.dumps(validation.to_dict(), indent=2)
            if args.json
            else validation.render()
        )
        return 0 if validation.ok else 3

    manager = RepositoryManager(Repository.load(root))

    if args.list:
        _print_catalogue(manager, args.json)
        return 0

    if args.status:
        inventory = manager.inventory()
        if args.json:
            print(json.dumps(inventory, indent=2))
        else:
            print(_render_status(inventory))
        return 0 if not inventory["missing"] else 3

    if args.checksums:
        checksum_report = manager.generate_checksums(
            dry_run=args.dry_run, prune=args.prune
        )
        if args.json:
            print(json.dumps(checksum_report, indent=2))
        else:
            print("\n".join(checksum_report["lines"]))
            print(
                f"\n{checksum_report['written']} checksum(s) recorded in "
                f"{checksum_report['path']}"
            )
        return 0 if not checksum_report["errors"] else 3

    if args.verify_checksums:
        verify_report = manager.verify_checksums()
        if args.json:
            print(json.dumps(verify_report, indent=2))
        else:
            for entry in verify_report["results"]:
                mark = "✓" if entry["status"] == "ok" else "✗"
                print(f"{mark} {entry['path']}: {entry['status']}")
        return 0 if verify_report["ok"] else 1

    return 0  # pragma: no cover - argparse enforces a command


def _print_catalogue(manager: RepositoryManager, as_json: bool) -> None:
    catalogue = manager.repository.catalogue
    if as_json:
        print(
            json.dumps(
                [
                    {
                        "id": a.id,
                        "name": a.name,
                        "version": a.version,
                        "category": a.category,
                        "enabled": a.enabled,
                        "platforms": sorted(p.value for p in a.platforms),
                    }
                    for a in catalogue
                ],
                indent=2,
            )
        )
        return
    width = max((len(a.id) for a in catalogue), default=4)
    for app in catalogue:
        platforms = ",".join(sorted(p.value for p in app.platforms))
        state = "" if app.enabled else "  (disabled)"
        version = app.version or "-"
        print(f"{app.id:<{width}}  {version:<10}  {platforms:<15}  {app.name}{state}")
    print(f"\n{len(catalogue)} application(s).")


def _render_status(inventory: dict[str, Any]) -> str:
    lines = [
        "USB Repository Manager",
        "",
        f"Repository:  {inventory['repository']}",
        f"Applications: {inventory['applications']} "
        f"({inventory['enabled']} enabled)",
        f"Installers:   {inventory['installers_declared']} declared "
        f"({inventory['installers_by_os']['windows']} windows, "
        f"{inventory['installers_by_os']['macos']} macos)",
        f"Missing:      {len(inventory['missing'])}",
        f"Orphaned:     {len(inventory['orphaned'])}",
        f"No checksum:  {len(inventory['without_checksum'])}",
    ]
    for label, key in (
        ("Missing installers", "missing"),
        ("Files on the drive that no application declares", "orphaned"),
        ("Installers without a checksum", "without_checksum"),
    ):
        if inventory[key]:
            lines += ["", f"{label}:"]
            lines += [f"  {entry}" for entry in inventory[key]]
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
