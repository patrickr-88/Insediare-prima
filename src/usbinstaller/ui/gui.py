"""Tkinter graphical interface, launched from the USB drive.

Three jobs, in the order a technician meets them:

1. **Inform** — what this computer is, what is on the drive, and for each
   application what will actually run, what is already installed and how the
   versions compare.
2. **Install** — select, review a plan, confirm, watch progress, read a report,
   retry failures.
3. **Extend** — add a new application to the drive without leaving the tool or
   hand-editing JSON.

Tkinter ships with the official Python builds on Windows and macOS, so the GUI
adds nothing to bundle. All logic lives in the service layer
(:mod:`usbinstaller.app`) and in :mod:`usbinstaller.config.authoring`, so this
module stays a thin, replaceable view. Installation runs on a worker thread;
the UI thread only ever reads from a queue.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Any

from ..app import InstallerApp
from ..config.authoring import (
    ApplicationDraft,
    AuthoringError,
    InstallerDraft,
    installer_filter,
    suggest_detection,
    suggest_id,
    suggest_os,
    suggest_type,
)
from ..config.catalogue import INSTALLER_TYPES
from ..engine.executor import render_results
from ..engine.planner import render_plan
from ..errors import InstallerError
from ..models import OS, Arch, InstallationPlan
from ..sysdetect.system import describe as describe_system
from ..version import APP_NAME, __version__

PAD = 10
MUTED = "#5c5c5c"
WARN = "#b45309"
BAD = "#b91c1c"
GOOD = "#15803d"


class InstallerWindow(tk.Tk):
    """The main window: software list on the left, details on the right."""

    def __init__(self, app: InstallerApp) -> None:
        super().__init__()
        self.app = app
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.vars: dict[str, tk.BooleanVar] = {}
        self.rows: dict[str, dict[str, Any]] = {}
        self.details_cache: dict[str, Any] = {}
        self.result = None
        self.progress_window: ProgressWindow | None = None
        self._worker: threading.Thread | None = None

        self.title(f"{APP_NAME} {__version__}")
        self.geometry("980x680")
        self.minsize(820, 560)

        self._build_menu()
        self._build_header()
        self._build_body()
        self._build_footer()
        self.after(100, self._drain_events)
        self.after(150, self._detect_in_background)

    # -- chrome ----------------------------------------------------------

    def _build_menu(self) -> None:
        menu = tk.Menu(self)

        drive = tk.Menu(menu, tearoff=0)
        drive.add_command(label="Add Application…", command=self.add_application)
        drive.add_command(label="Remove Application…", command=self.remove_application)
        drive.add_separator()
        drive.add_command(label="Validate Drive…", command=self.validate_drive)
        drive.add_command(label="Update Checksums", command=self.update_checksums)
        drive.add_separator()
        drive.add_command(label="Reload Drive", command=self.reload_drive)
        drive.add_command(label="Where Are My Logs?", command=self.open_logs)
        menu.add_cascade(label="Drive", menu=drive)

        help_menu = tk.Menu(menu, tearoff=0)
        help_menu.add_command(label="About", command=self.show_about)
        help_menu.add_command(label="This Computer", command=self.show_system)
        menu.add_cascade(label="Help", menu=help_menu)

        self.config(menu=menu)

    def _build_header(self) -> None:
        header = ttk.Frame(self, padding=(PAD, PAD, PAD, 0))
        header.pack(fill="x")

        ttk.Label(
            header,
            text=self.app.repository.settings.repository_name.upper(),
            font=("TkDefaultFont", 14, "bold"),
        ).grid(row=0, column=0, sticky="w")

        system = self.app.system
        summary = (
            f"{system.os_name}  ·  {_arch_label(system)}  ·  {system.hostname}"
            f"  ·  Administrator: {'yes' if system.is_admin else 'NO'}"
        )
        ttk.Label(header, text=summary).grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Label(
            header, text=f"Drive: {self.app.repository.root}", foreground=MUTED
        ).grid(row=2, column=0, sticky="w")

        if not system.is_admin:
            ttk.Label(
                header,
                text=(
                    "Not running as administrator — applications that require it "
                    "will be blocked before anything is installed."
                ),
                foreground=WARN,
            ).grid(row=3, column=0, sticky="w", pady=(4, 0))
        if self.app.dry_run:
            ttk.Label(
                header,
                text="DRY RUN — nothing will be installed on this computer.",
                foreground=WARN,
            ).grid(row=4, column=0, sticky="w", pady=(4, 0))

        ttk.Separator(self).pack(fill="x", padx=PAD, pady=(PAD, 0))

    def _build_body(self) -> None:
        body = ttk.PanedWindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, padx=PAD, pady=PAD)

        left = ttk.Frame(body)
        body.add(left, weight=3)
        ttk.Label(
            left, text="Available Software", font=("TkDefaultFont", 11, "bold")
        ).pack(anchor="w")

        canvas = tk.Canvas(left, highlightthickness=0, width=430)
        scrollbar = ttk.Scrollbar(left, orient="vertical", command=canvas.yview)
        self.list_frame = ttk.Frame(canvas)
        self.list_frame.bind(
            "<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=self.list_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True, pady=(6, 0))
        scrollbar.pack(side="right", fill="y", pady=(6, 0))

        right = ttk.Frame(body)
        body.add(right, weight=2)
        ttk.Label(right, text="Details", font=("TkDefaultFont", 11, "bold")).pack(
            anchor="w"
        )
        self.details = scrolledtext.ScrolledText(
            right, wrap="word", width=44, height=22, state="disabled"
        )
        self.details.pack(fill="both", expand=True, pady=(6, 0))
        self._set_details(
            "Click an application to see what will run, where it comes from on "
            "this drive, and what is already installed on this computer."
        )

        self._populate_list()

    def _populate_list(self) -> None:
        for child in self.list_frame.winfo_children():
            child.destroy()
        self.vars.clear()
        self.rows.clear()

        applications = self.app.available()
        if not applications:
            ttk.Label(
                self.list_frame,
                text=(
                    "No software on this drive is compatible with this computer.\n\n"
                    "Use Drive → Add Application… to put some on the drive."
                ),
                foreground=MUTED,
                justify="left",
            ).pack(anchor="w")
            return

        category = None
        for application in sorted(
            applications, key=lambda a: (a.category.lower(), a.name.lower())
        ):
            if application.category != category:
                category = application.category
                ttk.Label(
                    self.list_frame, text=category, font=("TkDefaultFont", 10, "bold")
                ).pack(anchor="w", pady=(10, 2))

            payload = application.payload_for(self.app.system.os)
            admin = "  🔒" if payload and payload.requires_admin else ""
            variable = tk.BooleanVar(value=application.selected_by_default)
            self.vars[application.id] = variable

            check = ttk.Checkbutton(
                self.list_frame,
                text=f"{application.name}  {application.version}{admin}",
                variable=variable,
                command=self._update_count,
            )
            check.pack(anchor="w")
            # Bound through a factory, not a default-argument lambda, so each
            # row closes over its own id.
            show = self._details_handler(application.id)
            check.bind("<Button-1>", show)

            status = ttk.Label(self.list_frame, text="checking…", foreground=MUTED)
            status.pack(anchor="w", padx=(24, 0))
            status.bind("<Button-1>", show)
            self.rows[application.id] = {"status": status, "check": check}

    def _details_handler(self, app_id: str) -> Callable[[Any], None]:
        def handler(_event: Any) -> None:
            self.show_details(app_id)

        return handler

    def _build_footer(self) -> None:
        footer = ttk.Frame(self, padding=(PAD, 0, PAD, PAD))
        footer.pack(fill="x")
        ttk.Separator(footer).pack(fill="x", pady=(0, 8))

        self.count_label = ttk.Label(footer, text="")
        self.count_label.pack(side="left")

        ttk.Button(footer, text="Review Installation", command=self.review).pack(
            side="right"
        )
        ttk.Button(
            footer, text="Add Application…", command=self.add_application
        ).pack(side="right", padx=6)
        ttk.Button(footer, text="None", command=lambda: self._select_all(False)).pack(
            side="right", padx=(0, 6)
        )
        ttk.Button(footer, text="All", command=lambda: self._select_all(True)).pack(
            side="right"
        )
        self._update_count()

    # -- information -----------------------------------------------------

    def _detect_in_background(self) -> None:
        """Fill in "what is already installed" without freezing the window."""
        ids = list(self.vars)
        if not ids:
            return

        def worker() -> None:
            for app_id in ids:
                try:
                    self.events.put(("detected", self.app.details_for(app_id)))
                except InstallerError as exc:  # pragma: no cover - defensive
                    self.events.put(("detect-error", (app_id, str(exc))))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_details(self, details) -> None:
        self.details_cache[details.id] = details
        row = self.rows.get(details.id)
        if row is None:
            return
        colour = MUTED
        if details.checksum_state == "mismatch" or not details.installer_exists:
            colour = BAD
        elif details.installed is not None and details.installed.installed:
            colour = GOOD if details.comparison.value == "same" else WARN
        row["status"].config(text=details.status_line, foreground=colour)

    def show_details(self, app_id: str) -> None:
        details = self.details_cache.get(app_id)
        if details is None:
            try:
                details = self.app.details_for(app_id)
            except InstallerError as exc:
                self._set_details(str(exc))
                return
            self.details_cache[app_id] = details
        self._set_details(details.render())

    def _set_details(self, text: str) -> None:
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", text)
        self.details.configure(state="disabled")

    # -- selection -------------------------------------------------------

    def _select_all(self, value: bool) -> None:
        for variable in self.vars.values():
            variable.set(value)
        self._update_count()

    def selected_ids(self) -> list[str]:
        return [app_id for app_id, var in self.vars.items() if var.get()]

    def _update_count(self) -> None:
        self.count_label.config(
            text=f"{len(self.selected_ids())} of {len(self.vars)} selected"
        )

    # -- installation ----------------------------------------------------

    def review(self) -> None:
        selection = self.selected_ids()
        if not selection:
            messagebox.showinfo(APP_NAME, "Select at least one application first.")
            return
        try:
            plan = self.app.plan(selection)
        except InstallerError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return

        privileges = self.app.privileges_for(plan)
        text = render_plan(plan)
        if privileges.required:
            text += "\n\n" + privileges.message(self.app.system.os)

        if not plan.executable_items:
            messagebox.showinfo(APP_NAME, text + "\n\nThere is nothing to install.")
            return
        if privileges.blocked and not self.app.dry_run:
            messagebox.showerror(APP_NAME, text)
            return
        if PlanDialog(self, text, dry_run=self.app.dry_run).confirmed:
            self._start_install(plan)

    def _start_install(self, plan: InstallationPlan) -> None:
        self.progress_window = ProgressWindow(self, len(plan.executable_items))

        def worker() -> None:
            try:
                result = self.app.install(
                    plan,
                    on_progress=lambda index, total, item: self.events.put(
                        ("progress", (index, total, item))
                    ),
                    on_complete=lambda item: self.events.put(("done", item)),
                )
                self.events.put(("finished", result))
            except Exception as exc:  # noqa: BLE001 - surfaced in the UI
                self.events.put(("error", exc))

        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()

    def retry_failed(self, ids: list[str]) -> None:
        self._start_install(self.app.plan(ids, force=True))

    # -- drive maintenance -----------------------------------------------

    def add_application(self) -> None:
        dialog = AddApplicationDialog(self, self.app)
        if dialog.added:
            self.reload_drive()
            messagebox.showinfo(APP_NAME, dialog.summary)

    def remove_application(self) -> None:
        if RemoveApplicationDialog(self, self.app).removed:
            self.reload_drive()

    def validate_drive(self) -> None:
        TextDialog(
            self, "Drive Validation", self.app.validate(check_checksums=True).render()
        )

    def update_checksums(self) -> None:
        from ..repository_manager import RepositoryManager

        try:
            result = RepositoryManager(self.app.repository).generate_checksums()
        except InstallerError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        body = "\n".join(result["lines"]) or "Nothing to record."
        TextDialog(
            self,
            "Checksums",
            f"{body}\n\n{result['written']} checksum(s) recorded in\n{result['path']}",
        )
        self.reload_drive()

    def reload_drive(self) -> None:
        try:
            self.app = self.app.reload()
        except InstallerError as exc:
            messagebox.showerror(APP_NAME, f"The drive could not be reloaded:\n\n{exc}")
            return
        self.details_cache.clear()
        self._populate_list()
        self._update_count()
        self._detect_in_background()

    def open_logs(self) -> None:
        directory = (
            self.app.log.directory if self.app.log else self.app.repository.logs_dir
        )
        TextDialog(
            self,
            "Logs",
            f"This run's log directory:\n\n{directory}\n\n"
            "It contains installation.log (what happened, with timestamps), "
            "results.json (machine-readable outcomes), plan.json (what was "
            "decided) and system.json (this computer).\n\n"
            "Attach those files to a ticket when reporting a failure — none of "
            "them contain passwords or licence keys.",
        )

    def show_about(self) -> None:
        TextDialog(
            self,
            "About",
            f"{APP_NAME} {__version__}\n\n"
            "Portable software deployment from a USB drive.\n\n"
            f"Drive: {self.app.repository.root}\n"
            f"Applications on this drive: {len(self.app.repository.catalogue)}\n"
            f"Compatible with this computer: {len(self.app.available())}\n\n"
            "Nothing is installed until you confirm the plan. User files are "
            "never modified, software is never uninstalled, and a newer "
            "installed version is never replaced with an older one.",
        )

    def show_system(self) -> None:
        TextDialog(self, "This Computer", describe_system(self.app.system))

    # -- event pump ------------------------------------------------------

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                self._handle_event(kind, payload)
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _handle_event(self, kind: str, payload: Any) -> None:
        if kind == "detected":
            self._apply_details(payload)
            return
        if kind == "detect-error":
            app_id, message = payload
            row = self.rows.get(app_id)
            if row is not None:
                row["status"].config(text=message, foreground=BAD)
            return

        window = self.progress_window
        if window is None:
            return
        if kind == "progress":
            index, total, item = payload
            window.set_progress(index, total, item.application.name)
        elif kind == "done":
            status = (payload.status.value if payload.status else "unknown").upper()
            window.append(
                f"{payload.application.name}: {status}"
                + (f" — {payload.error}" if payload.error else "")
            )
        elif kind == "finished":
            self.result = payload
            window.finish(
                render_results(
                    payload, self.app.log.directory if self.app.log else None
                ),
                failed=[i.app_id for i in payload.failures],
            )
            self.details_cache.clear()
            self._detect_in_background()
        elif kind == "error":
            window.finish(f"The installation could not be completed:\n\n{payload}", [])


# --------------------------------------------------------------------------
# Dialogs
# --------------------------------------------------------------------------


class TextDialog(tk.Toplevel):
    """A read-only scrollable message window (reports, help)."""

    def __init__(self, parent: tk.Wm, title: str, text: str) -> None:
        super().__init__(parent)  # type: ignore[arg-type]
        self.title(title)
        self.geometry("720x480")
        self.transient(parent)
        area = scrolledtext.ScrolledText(self, wrap="word")
        area.pack(fill="both", expand=True, padx=PAD, pady=PAD)
        area.insert("1.0", text)
        area.configure(state="disabled")
        ttk.Button(self, text="Close", command=self.destroy).pack(
            side="right", padx=PAD, pady=(0, PAD)
        )


class PlanDialog(tk.Toplevel):
    """Modal confirmation showing the full installation plan."""

    def __init__(self, parent: tk.Tk, text: str, *, dry_run: bool) -> None:
        super().__init__(parent)
        self.title("Review Installation")
        self.confirmed = False
        self.transient(parent)
        self.grab_set()
        self.geometry("720x560")

        area = scrolledtext.ScrolledText(self, wrap="word", height=24)
        area.pack(fill="both", expand=True, padx=PAD, pady=PAD)
        area.insert("1.0", text)
        area.configure(state="disabled")

        buttons = ttk.Frame(self, padding=(PAD, 0, PAD, PAD))
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(
            buttons,
            text="Start Dry Run" if dry_run else "Install",
            command=self._accept,
        ).pack(side="right", padx=6)
        parent.wait_window(self)

    def _accept(self) -> None:
        self.confirmed = True
        self.destroy()


class ProgressWindow(tk.Toplevel):
    """Live progress, then the final report."""

    def __init__(self, parent: InstallerWindow, total: int) -> None:
        super().__init__(parent)
        self.parent = parent
        self.title("Installing")
        self.geometry("680x480")
        self.transient(parent)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", lambda: None)  # no closing mid-install

        self.heading = ttk.Label(
            self, text=f"Installing 0 of {total}", font=("TkDefaultFont", 11, "bold")
        )
        self.heading.pack(anchor="w", padx=PAD, pady=(PAD, 4))
        self.current = ttk.Label(self, text="Preparing…")
        self.current.pack(anchor="w", padx=PAD)

        self.bar = ttk.Progressbar(self, maximum=max(total, 1), length=600)
        self.bar.pack(fill="x", padx=PAD, pady=8)

        self.log = scrolledtext.ScrolledText(self, height=16, wrap="word")
        self.log.pack(fill="both", expand=True, padx=PAD, pady=(0, PAD))
        self.log.configure(state="disabled")

        self.buttons = ttk.Frame(self, padding=(PAD, 0, PAD, PAD))
        self.buttons.pack(fill="x")

    def set_progress(self, index: int, total: int, name: str) -> None:
        self.heading.config(text=f"Installing {index} of {total}")
        self.current.config(text=name)
        self.bar.config(value=index - 1, maximum=max(total, 1))

    def append(self, line: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def finish(self, summary: str, failed: list[str]) -> None:
        self.bar.config(value=self.bar["maximum"])
        self.heading.config(text="Complete")
        self.current.config(text="")
        self.append("\n" + summary)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        ttk.Button(self.buttons, text="Close", command=self.destroy).pack(side="right")
        if failed:

            def retry() -> None:
                self.destroy()
                self.parent.retry_failed(failed)

            ttk.Button(
                self.buttons, text=f"Retry {len(failed)} failed", command=retry
            ).pack(side="right", padx=6)


class RemoveApplicationDialog(tk.Toplevel):
    """Remove an application from the drive's catalogue."""

    def __init__(self, parent: tk.Tk, app: InstallerApp) -> None:
        super().__init__(parent)
        self.app = app
        self.removed = False
        self.title("Remove Application")
        self.geometry("480x240")
        self.transient(parent)
        self.grab_set()

        ttk.Label(self, text="Application to remove from this drive:").pack(
            anchor="w", padx=PAD, pady=(PAD, 4)
        )
        self.choice = ttk.Combobox(
            self, state="readonly", values=[a.id for a in app.repository.catalogue]
        )
        self.choice.pack(fill="x", padx=PAD)

        self.delete_files = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            self,
            text="Also delete its installer files from the drive",
            variable=self.delete_files,
        ).pack(anchor="w", padx=PAD, pady=PAD)
        ttk.Label(
            self,
            text=(
                "This changes the USB drive only. Nothing is uninstalled from "
                "this or any other computer."
            ),
            foreground=MUTED,
            wraplength=440,
            justify="left",
        ).pack(anchor="w", padx=PAD)

        buttons = ttk.Frame(self, padding=PAD)
        buttons.pack(fill="x", side="bottom")
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="Remove", command=self._remove).pack(
            side="right", padx=6
        )
        parent.wait_window(self)

    def _remove(self) -> None:
        app_id = self.choice.get()
        if not app_id:
            return
        if not messagebox.askyesno(
            APP_NAME, f"Remove '{app_id}' from this drive?", parent=self
        ):
            return
        try:
            self.app.editor.remove_application(
                app_id, delete_files=self.delete_files.get()
            )
        except AuthoringError as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self)
            return
        self.removed = True
        self.destroy()


class AddApplicationDialog(tk.Toplevel):
    """Add a new application to the drive.

    The technician picks an installer file; the dialog infers the platform,
    the installer type and a detection rule, copies the file into
    ``installers/<os>/<id>/``, writes the catalogue entry and records its
    SHA-256. Everything is validated before anything is written, and *Preview*
    shows exactly what will change.
    """

    def __init__(self, parent: tk.Tk, app: InstallerApp) -> None:
        super().__init__(parent)
        self.app = app
        self.added = False
        self.summary = ""
        self.sources: dict[OS, Path] = {}

        self.title("Add Application to this Drive")
        self.geometry("660x660")
        self.transient(parent)
        self.grab_set()

        container = ttk.Frame(self, padding=PAD)
        container.pack(fill="both", expand=True)
        container.columnconfigure(1, weight=1)

        ttk.Label(
            container,
            text="Add software to the USB drive",
            font=("TkDefaultFont", 12, "bold"),
        ).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(
            container,
            text=(
                "The installer is copied onto the drive, added to the catalogue "
                "and checksummed.\nThis changes the drive only — nothing is "
                "installed on this computer."
            ),
            foreground=MUTED,
            justify="left",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, PAD))

        self.name_var = tk.StringVar()
        self.id_var = tk.StringVar()
        self.version_var = tk.StringVar()
        self.category_var = tk.StringVar(value="Uncategorised")
        self.description_var = tk.StringVar()
        self.arguments_var = tk.StringVar()
        self.admin_var = tk.BooleanVar(value=True)
        self.detect_var = tk.BooleanVar(value=True)
        self.scheme_var = tk.StringVar(value="auto")

        row = 2
        row = self._entry(container, row, "Display name", self.name_var)
        self.name_var.trace_add("write", self._suggest_id)
        row = self._entry(container, row, "ID", self.id_var)
        row = self._entry(container, row, "Version", self.version_var)
        row = self._entry(container, row, "Category", self.category_var)
        row = self._entry(container, row, "Description", self.description_var)

        ttk.Label(container, text="Version scheme").grid(
            row=row, column=0, sticky="w", pady=3
        )
        ttk.Combobox(
            container,
            textvariable=self.scheme_var,
            state="readonly",
            values=["auto", "numeric", "semver", "date", "string"],
        ).grid(row=row, column=1, columnspan=2, sticky="ew", pady=3)
        row += 1

        ttk.Separator(container).grid(
            row=row, column=0, columnspan=3, sticky="ew", pady=PAD
        )
        row += 1
        ttk.Label(
            container, text="Installers", font=("TkDefaultFont", 10, "bold")
        ).grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1

        self.file_labels: dict[OS, ttk.Label] = {}
        self.type_vars: dict[OS, tk.StringVar] = {}

        def chooser(target: OS) -> Callable[[], None]:
            return lambda: self._choose(target)

        def clearer(target: OS) -> Callable[[], None]:
            return lambda: self._clear(target)

        for os_, label in ((OS.WINDOWS, "Windows"), (OS.MACOS, "macOS")):
            ttk.Button(
                container,
                text=f"Choose {label} installer…",
                command=chooser(os_),
            ).grid(row=row, column=0, sticky="w", pady=3)
            file_label = ttk.Label(container, text="none selected", foreground=MUTED)
            file_label.grid(row=row, column=1, columnspan=2, sticky="w", padx=(8, 0))
            self.file_labels[os_] = file_label
            row += 1

            type_var = tk.StringVar()
            self.type_vars[os_] = type_var
            ttk.Label(container, text=f"{label} type").grid(
                row=row, column=0, sticky="w"
            )
            ttk.Combobox(
                container,
                textvariable=type_var,
                state="readonly",
                values=list(INSTALLER_TYPES[os_]),
            ).grid(row=row, column=1, sticky="ew")
            ttk.Button(
                container, text="Clear", command=clearer(os_)
            ).grid(row=row, column=2, sticky="e", padx=(6, 0))
            row += 1

        ttk.Separator(container).grid(
            row=row, column=0, columnspan=3, sticky="ew", pady=PAD
        )
        row += 1

        row = self._entry(container, row, "Silent arguments", self.arguments_var)
        ttk.Label(
            container,
            text="e.g.  /S    or    /VERYSILENT /NORESTART    (leave empty if unsure)",
            foreground=MUTED,
        ).grid(row=row, column=1, columnspan=2, sticky="w")
        row += 1

        ttk.Checkbutton(
            container,
            text="Requires administrator privileges",
            variable=self.admin_var,
        ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(6, 0))
        row += 1
        ttk.Checkbutton(
            container,
            text="Detect whether it is already installed (recommended)",
            variable=self.detect_var,
        ).grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1

        self.message = ttk.Label(
            container, text="", foreground=BAD, justify="left", wraplength=600
        )
        self.message.grid(row=row, column=0, columnspan=3, sticky="w", pady=(PAD, 0))

        buttons = ttk.Frame(self, padding=(PAD, 0, PAD, PAD))
        buttons.pack(fill="x", side="bottom")
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="Add to Drive", command=self._save).pack(
            side="right", padx=6
        )
        ttk.Button(buttons, text="Preview", command=self._preview).pack(side="right")

        parent.wait_window(self)

    # -- form helpers ----------------------------------------------------

    def _entry(self, parent: ttk.Frame, row: int, label: str, var: tk.StringVar) -> int:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3)
        ttk.Entry(parent, textvariable=var).grid(
            row=row, column=1, columnspan=2, sticky="ew", pady=3
        )
        return row + 1

    def _suggest_id(self, *_args: object) -> None:
        """Keep the ID in step with the name until the technician edits it."""
        current = self.id_var.get().strip()
        previous = suggest_id(self.name_var.get()[:-1]) if self.name_var.get() else ""
        if not current or current == previous:
            self.id_var.set(suggest_id(self.name_var.get()))

    def _choose(self, os_: OS) -> None:
        filters = [*installer_filter(os_), ("All files", "*.*")]
        chosen = filedialog.askopenfilename(
            parent=self, title=f"Choose the {os_.value} installer", filetypes=filters
        )
        if not chosen:
            return
        path = Path(chosen)
        detected = suggest_os(path)
        if detected is not None and detected is not os_:
            messagebox.showwarning(
                APP_NAME,
                f"{path.name} looks like a {detected.value} installer, but you "
                f"chose it as the {os_.value} one. Check the type below.",
                parent=self,
            )
        self.sources[os_] = path
        self.file_labels[os_].config(text=path.name, foreground="")
        inferred = suggest_type(path, os_)
        if inferred:
            self.type_vars[os_].set(inferred)
        if not self.name_var.get().strip():
            self.name_var.set(path.stem)

    def _clear(self, os_: OS) -> None:
        self.sources.pop(os_, None)
        self.file_labels[os_].config(text="none selected", foreground=MUTED)
        self.type_vars[os_].set("")

    def _draft(self) -> ApplicationDraft:
        import shlex

        name = self.name_var.get().strip()
        try:
            arguments = shlex.split(self.arguments_var.get().strip())
        except ValueError:
            arguments = self.arguments_var.get().split()

        installers = []
        for os_, source in self.sources.items():
            installer_type = self.type_vars[os_].get().strip()
            installers.append(
                InstallerDraft(
                    os=os_,
                    source=source,
                    type=installer_type,
                    arguments=arguments,
                    requires_admin=self.admin_var.get(),
                    detection=(
                        suggest_detection(os_, name) if self.detect_var.get() else None
                    ),
                    app_bundle=(
                        f"{name}.app"
                        if os_ is OS.MACOS and installer_type == "dmg"
                        else None
                    ),
                )
            )
        return ApplicationDraft(
            id=self.id_var.get().strip(),
            name=name,
            version=self.version_var.get().strip(),
            description=self.description_var.get().strip(),
            category=self.category_var.get().strip() or "Uncategorised",
            version_scheme=self.scheme_var.get(),
            installers=tuple(installers),
        )

    # -- actions ---------------------------------------------------------

    def _preview(self) -> None:
        try:
            result = self.app.editor.add_application(self._draft(), preview=True)
        except AuthoringError as exc:
            self.message.config(text=str(exc), foreground=BAD)
            return
        self.message.config(text=result.summary, foreground=MUTED)

    def _save(self) -> None:
        draft = self._draft()
        try:
            result = self.app.editor.add_application(draft)
        except AuthoringError as exc:
            self.message.config(text=str(exc), foreground=BAD)
            return
        except OSError as exc:
            self.message.config(
                text=f"The drive could not be written to: {exc}", foreground=BAD
            )
            return
        self.added = True
        self.summary = (
            f"{draft.name} was added to this drive.\n\n"
            f"{result.summary}\n\n"
            "It now appears in the software list."
        )
        self.destroy()


def _arch_label(system) -> str:
    if system.os is OS.MACOS:
        return {Arch.ARM64: "Apple Silicon", Arch.X64: "Intel"}.get(
            system.arch, system.arch.value
        )
    return system.arch.value


def run_gui(repository: str | Path | None = None, dry_run: bool = False) -> int:
    """Launch the GUI. Returns a CLI-compatible exit code."""
    try:
        app = InstallerApp.create(repository, dry_run=dry_run, with_log=True)
    except InstallerError as exc:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(APP_NAME, str(exc))
        root.destroy()
        return 2

    window = InstallerWindow(app)
    window.mainloop()
    result = window.result
    if result is None:
        return 0
    return 1 if result.failed else 0
