"""Command-line interface.

Flag-style commands (``--list``, ``--install-all``, …) as specified, so the
tool drops straight into existing IT automation. Every command returns a
meaningful exit code:

===  =========================================
0    success
1    one or more installations failed
2    usage error / repository could not be loaded
3    configuration validation failed
4    cancelled by the technician
5    required privileges are missing
===  =========================================
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from .app import InstallerApp
from .config.validator import validate_repository
from .engine.executor import render_results
from .engine.planner import render_plan
from .errors import InstallerError
from .models import Action
from .repository import Repository
from .sysdetect.system import describe
from .version import APP_NAME, __version__

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_INVALID_CONFIG = 3
EXIT_CANCELLED = 4
EXIT_PRIVILEGES = 5


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="usbinstaller",
        description=f"{APP_NAME} — portable software deployment from a USB drive.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  usbinstaller --list\n"
            "  usbinstaller --install-all --dry-run\n"
            "  usbinstaller --install firefox vscode --yes\n"
            "  usbinstaller --validate-config\n"
            "  usbinstaller --verify\n"
            "  usbinstaller --generate-checksums\n"
            "  usbinstaller --retry\n"
        ),
    )

    commands = parser.add_argument_group("commands")
    group = commands.add_mutually_exclusive_group()
    group.add_argument("--list", action="store_true", help="list available software")
    group.add_argument(
        "--install-all", action="store_true", help="install everything compatible"
    )
    group.add_argument(
        "--install", nargs="+", metavar="ID", help="install the named applications"
    )
    group.add_argument(
        "--retry", action="store_true", help="re-run applications that failed last time"
    )
    group.add_argument(
        "--validate-config", action="store_true", help="validate the repository"
    )
    group.add_argument(
        "--verify",
        action="store_true",
        help="validate the repository and re-hash every installer",
    )
    group.add_argument(
        "--generate-checksums",
        action="store_true",
        help="write checksums.json for every configured installer",
    )
    group.add_argument("--info", action="store_true", help="show detected system info")
    group.add_argument("--gui", action="store_true", help="launch the graphical interface")
    group.add_argument(
        "--version", action="store_true", help="show the application version"
    )

    options = parser.add_argument_group("options")
    options.add_argument(
        "--repository",
        metavar="PATH",
        help="repository root (default: auto-detected next to the executable)",
    )
    options.add_argument(
        "--plan",
        action="store_true",
        help="show the installation plan and exit without installing",
    )
    options.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and plan, but execute no installers",
    )
    options.add_argument(
        "--yes", "-y", action="store_true", help="skip the confirmation prompt"
    )
    options.add_argument(
        "--force",
        action="store_true",
        help="install even when an equal or newer version is present",
    )
    options.add_argument(
        "--skip", nargs="+", metavar="ID", default=[], help="exclude these applications"
    )
    options.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON output"
    )
    options.add_argument(
        "--no-log", action="store_true", help="do not write a log directory"
    )
    options.add_argument(
        "--prune", action="store_true", help="with --generate-checksums, drop stale entries"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(f"{APP_NAME} {__version__}")
        return EXIT_OK

    try:
        return _dispatch(args, parser)
    except InstallerError as exc:
        _fail(str(exc), args.json)
        return EXIT_USAGE
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print("\nCancelled.", file=sys.stderr)
        return EXIT_CANCELLED


def _dispatch(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.validate_config or args.verify:
        return _cmd_validate(args)
    if args.generate_checksums:
        return _cmd_generate_checksums(args)
    if args.gui:
        return _cmd_gui(args)

    app = InstallerApp.create(
        args.repository,
        dry_run=args.dry_run,
        with_log=not args.no_log and _writes_anything(args),
    )

    if args.info:
        return _cmd_info(app, args)
    if args.list:
        return _cmd_list(app, args)
    if args.install_all or args.install or args.retry or args.plan:
        return _cmd_install(app, args)

    # No command given: show what we found and how to proceed.
    print(describe(app.system))
    print()
    parser.print_help()
    return EXIT_OK


def _writes_anything(args: argparse.Namespace) -> bool:
    return bool(args.install_all or args.install or args.retry)


# -- commands ------------------------------------------------------------


def _cmd_info(app: InstallerApp, args: argparse.Namespace) -> int:
    if args.json:
        print(json.dumps(app.system.to_dict(), indent=2))
    else:
        print(describe(app.system))
    return EXIT_OK


def _cmd_list(app: InstallerApp, args: argparse.Namespace) -> int:
    apps = app.available()
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "id": a.id,
                        "name": a.name,
                        "version": a.version,
                        "category": a.category,
                        "description": a.description,
                        "requires_admin": bool(
                            (p := a.payload_for(app.system.os)) and p.requires_admin
                        ),
                        "installer_type": p.type if p else None,
                    }
                    for a in apps
                ],
                indent=2,
            )
        )
        return EXIT_OK

    if not apps:
        print(
            f"No applications in this repository are compatible with "
            f"{app.system.os_name} ({app.system.arch.value})."
        )
        return EXIT_OK

    print(f"Available software for {app.system.os_name} ({app.system.arch.value}):\n")
    width = max(len(a.id) for a in apps)
    current_category = None
    for a in sorted(apps, key=lambda x: (x.category.lower(), x.name.lower())):
        if a.category != current_category:
            current_category = a.category
            print(f"  {current_category}")
        payload = a.payload_for(app.system.os)
        admin = " [admin]" if payload and payload.requires_admin else ""
        print(f"    {a.id:<{width}}  {a.name} {a.version}{admin}")
    print(f"\n{len(apps)} application(s).")
    return EXIT_OK


def _cmd_install(app: InstallerApp, args: argparse.Namespace) -> int:
    if args.retry:
        selection = app.previous_failures()
        if not selection:
            print("No failed installations were recorded in the previous run.")
            return EXIT_OK
        print(f"Retrying: {', '.join(selection)}")
    elif args.install:
        selection = list(args.install)
    else:
        selection = [a.id for a in app.available()]

    skip = set(args.skip)
    selection = [i for i in selection if i not in skip]
    if not selection:
        print("Nothing to install: every selected application was skipped.")
        return EXIT_OK

    plan = app.plan(selection, force=args.force)

    if args.json and args.plan:
        print(json.dumps([item.to_dict() for item in plan.items], indent=2))
        return EXIT_OK

    print(render_plan(plan))

    if args.plan:
        return EXIT_OK

    privileges = app.privileges_for(plan)
    if privileges.blocked and not app.dry_run:
        print()
        print(privileges.message(app.system.os))
        return EXIT_PRIVILEGES

    errors = plan.by_action(Action.ERROR)
    if not plan.executable_items:
        if errors:
            print(f"\nNothing can be installed: {len(errors)} application(s) in error.")
            return EXIT_FAILED
        print("\nNothing to do.")
        return EXIT_OK

    if not args.yes and app.repository.settings.require_confirmation and not args.dry_run:
        if not _confirm("\nContinue? [y/N] "):
            print("Cancelled. No changes were made.")
            return EXIT_CANCELLED

    print()
    result = app.install(plan, on_progress=_progress)
    print()
    print(render_results(result, app.log.directory if app.log else None))

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    return EXIT_FAILED if result.failed else EXIT_OK


def _cmd_validate(args: argparse.Namespace) -> int:
    root = Path(args.repository) if args.repository else Repository.discover()
    report = validate_repository(root, check_checksums=args.verify)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print("Configuration Validation\n")
        print(report.render())
    return EXIT_OK if report.ok else EXIT_INVALID_CONFIG


def _cmd_generate_checksums(args: argparse.Namespace) -> int:
    from .repository_manager import RepositoryManager

    root = Path(args.repository) if args.repository else Repository.discover()
    manager = RepositoryManager(Repository.load(root))
    report = manager.generate_checksums(dry_run=args.dry_run, prune=args.prune)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for line in report["lines"]:
            print(line)
        print(
            f"\n{report['written']} checksum(s) "
            f"{'would be ' if args.dry_run else ''}recorded in {report['path']}"
        )
    return EXIT_OK if not report["errors"] else EXIT_INVALID_CONFIG


def _cmd_gui(args: argparse.Namespace) -> int:
    try:
        from .ui.gui import run_gui
    except ImportError as exc:  # pragma: no cover - depends on the host build
        print(
            f"The graphical interface is unavailable ({exc}).\n"
            "Use the command line instead, e.g. 'usbinstaller --list'.",
            file=sys.stderr,
        )
        return EXIT_USAGE
    return run_gui(repository=args.repository, dry_run=args.dry_run)


# -- helpers -------------------------------------------------------------


def _progress(index: int, total: int, item) -> None:
    bar_width = 20
    filled = int(bar_width * (index - 1) / total) if total else bar_width
    bar = "█" * filled + "░" * (bar_width - filled)
    print(f"Installing {index} of {total}  {bar}  {item.application.name}", flush=True)


def _confirm(prompt: str) -> bool:
    try:
        answer = input(prompt)
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def _fail(message: str, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"error": message}), file=sys.stderr)
    else:
        print(f"Error: {message}", file=sys.stderr)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
