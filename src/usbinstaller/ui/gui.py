"""Tkinter graphical interface.

Tkinter is part of the standard library on the official Windows and macOS
Python builds, so the GUI adds no third-party dependency and nothing extra to
bundle. The window is intentionally plain: a technician wants a checklist, a
plan, a progress bar and a result — not a themed dashboard.

Installation runs on a worker thread; the UI thread only ever reads from a
queue, which keeps the window responsive while an installer is running.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk
from typing import Any

from ..app import InstallerApp
from ..engine.executor import render_results
from ..engine.planner import render_plan
from ..errors import InstallerError
from ..models import InstallationPlan
from ..sysdetect.system import describe
from ..version import APP_NAME, __version__

PADDING = 10


class InstallerWindow(tk.Tk):
    """The main window: selection → plan → progress → results."""

    def __init__(self, app: InstallerApp) -> None:
        super().__init__()
        self.app = app
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.vars: dict[str, tk.BooleanVar] = {}
        self.result = None
        self._worker: threading.Thread | None = None

        self.title(f"{APP_NAME} {__version__}")
        self.geometry("760x620")
        self.minsize(640, 520)

        self._build_header()
        self._build_selection()
        self._build_footer()
        self.after(100, self._drain_events)

    # -- layout ----------------------------------------------------------

    def _build_header(self) -> None:
        header = ttk.Frame(self, padding=PADDING)
        header.pack(fill="x")
        ttk.Label(
            header, text="USB SOFTWARE INSTALLER", font=("TkDefaultFont", 14, "bold")
        ).pack(anchor="w")
        ttk.Label(header, text=describe(self.app.system), justify="left").pack(
            anchor="w", pady=(6, 0)
        )
        if self.app.dry_run:
            ttk.Label(
                header,
                text="DRY RUN — no changes will be made to this computer.",
                foreground="#b45309",
            ).pack(anchor="w", pady=(6, 0))
        ttk.Separator(self).pack(fill="x", padx=PADDING)

    def _build_selection(self) -> None:
        body = ttk.Frame(self, padding=PADDING)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Available Software", font=("TkDefaultFont", 11, "bold")).pack(
            anchor="w"
        )

        canvas = tk.Canvas(body, highlightthickness=0)
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
        self.list_frame = ttk.Frame(canvas)
        self.list_frame.bind(
            "<Configure>",
            lambda _e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.create_window((0, 0), window=self.list_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True, pady=(6, 0))
        scrollbar.pack(side="right", fill="y", pady=(6, 0))

        applications = self.app.available()
        if not applications:
            ttk.Label(
                self.list_frame,
                text=(
                    "No applications in this repository are compatible with this "
                    "computer."
                ),
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
                ).pack(anchor="w", pady=(8, 2))
            variable = tk.BooleanVar(value=application.selected_by_default)
            self.vars[application.id] = variable
            payload = application.payload_for(self.app.system.os)
            admin = "  [admin]" if payload and payload.requires_admin else ""
            label = f"{application.name} {application.version}{admin}"
            check = ttk.Checkbutton(
                self.list_frame,
                text=label,
                variable=variable,
                command=self._update_count,
            )
            check.pack(anchor="w")
            if application.description:
                ttk.Label(
                    self.list_frame,
                    text=f"      {application.description}",
                    foreground="#555555",
                ).pack(anchor="w")

    def _build_footer(self) -> None:
        footer = ttk.Frame(self, padding=PADDING)
        footer.pack(fill="x")
        ttk.Separator(footer).pack(fill="x", pady=(0, 8))

        self.count_label = ttk.Label(footer, text="")
        self.count_label.pack(side="left")

        ttk.Button(footer, text="Review Installation", command=self._review).pack(
            side="right"
        )
        ttk.Button(footer, text="None", command=lambda: self._select_all(False)).pack(
            side="right", padx=4
        )
        ttk.Button(footer, text="All", command=lambda: self._select_all(True)).pack(
            side="right"
        )
        self._update_count()

    # -- selection helpers ----------------------------------------------

    def _select_all(self, value: bool) -> None:
        for variable in self.vars.values():
            variable.set(value)
        self._update_count()

    def selected_ids(self) -> list[str]:
        return [app_id for app_id, var in self.vars.items() if var.get()]

    def _update_count(self) -> None:
        count = len(self.selected_ids())
        self.count_label.config(text=f"{count} application(s) selected")

    # -- workflow --------------------------------------------------------

    def _review(self) -> None:
        selection = self.selected_ids()
        if not selection:
            messagebox.showinfo(APP_NAME, "Select at least one application.")
            return
        try:
            plan = self.app.plan(selection)
        except InstallerError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return

        privileges = self.app.privileges_for(plan)
        text = render_plan(plan)
        if privileges.blocked:
            text += "\n\n" + privileges.message(self.app.system.os)

        if not plan.executable_items:
            messagebox.showinfo(APP_NAME, text + "\n\nNothing to install.")
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

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                self._handle_event(kind, payload)
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _handle_event(self, kind: str, payload: Any) -> None:
        window = getattr(self, "progress_window", None)
        if window is None:
            return
        if kind == "progress":
            index, total, item = payload
            window.set_progress(index, total, item.application.name)
        elif kind == "done":
            window.append(
                f"{payload.application.name}: "
                f"{(payload.status.value if payload.status else 'unknown').upper()}"
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
        elif kind == "error":
            window.finish(f"The installation could not be completed:\n\n{payload}", [])

    def retry_failed(self, ids: list[str]) -> None:
        plan = self.app.plan(ids, force=True)
        self._start_install(plan)


class PlanDialog(tk.Toplevel):
    """Modal confirmation showing the full installation plan."""

    def __init__(self, parent: tk.Tk, text: str, *, dry_run: bool) -> None:
        super().__init__(parent)
        self.title("Review Installation")
        self.confirmed = False
        self.transient(parent)
        self.grab_set()
        self.geometry("680x520")

        area = scrolledtext.ScrolledText(self, wrap="word", height=24)
        area.pack(fill="both", expand=True, padx=PADDING, pady=PADDING)
        area.insert("1.0", text)
        area.configure(state="disabled")

        buttons = ttk.Frame(self, padding=(PADDING, 0, PADDING, PADDING))
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
        self.geometry("640x460")
        self.transient(parent)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", lambda: None)  # no closing mid-install

        self.heading = ttk.Label(
            self, text=f"Installing 0 of {total}", font=("TkDefaultFont", 11, "bold")
        )
        self.heading.pack(anchor="w", padx=PADDING, pady=(PADDING, 4))
        self.current = ttk.Label(self, text="Preparing…")
        self.current.pack(anchor="w", padx=PADDING)

        self.bar = ttk.Progressbar(self, maximum=max(total, 1), length=560)
        self.bar.pack(fill="x", padx=PADDING, pady=8)

        self.log = scrolledtext.ScrolledText(self, height=16, wrap="word")
        self.log.pack(fill="both", expand=True, padx=PADDING, pady=(0, PADDING))
        self.log.configure(state="disabled")

        self.buttons = ttk.Frame(self, padding=(PADDING, 0, PADDING, PADDING))
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
