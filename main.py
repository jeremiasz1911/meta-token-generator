#!/usr/bin/env python3
"""
Meta Page Token Generator — desktop GUI (macOS / Windows).

Flow:
  short-lived User token → long-lived User token (intermediate)
  → /me/accounts → Page Access Token (FINAL — for the WordPress plugin)
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import webbrowser
from typing import Callable

import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

from meta_api import (
    GRAPH_API_VERSION,
    DiagnosticReport,
    FacebookPage,
    MetaAPIError,
    PagePost,
    TokenExchangeResult,
    exchange_user_token,
    fetch_all_pages,
    fetch_published_posts,
    run_full_diagnostics,
    verify_page_token_belongs,
)
from safe_log import get_logger, log_file_path, setup_logging

setup_logging()
log = get_logger()

# Default label color — never reset with fg="" (TclError on macOS).
_LABEL_FG_DEFAULT = "#000000"
_LABEL_FG_OK = "#1a7f37"
_LABEL_FG_ERR = "#b00020"


class MaskedEntry(ttk.Frame):
    """Entry that can toggle between masked and plain text."""

    def __init__(
        self,
        master: tk.Misc,
        *,
        show_toggle: bool = True,
        width: int = 48,
    ) -> None:
        super().__init__(master)
        self._masked = True
        self.var = tk.StringVar()
        self.entry = ttk.Entry(self, textvariable=self.var, show="•", width=width)
        self.entry.grid(row=0, column=0, sticky="ew")
        self.columnconfigure(0, weight=1)

        if show_toggle:
            self.toggle_btn = ttk.Button(self, text="Show / Hide", width=12, command=self.toggle)
            self.toggle_btn.grid(row=0, column=1, padx=(6, 0))
        else:
            self.toggle_btn = None

    def toggle(self) -> None:
        self._masked = not self._masked
        self.entry.configure(show="•" if self._masked else "")

    def get(self) -> str:
        return self.var.get()

    def set(self, value: str) -> None:
        self.var.set(value)

    def clear(self) -> None:
        self.var.set("")
        self._masked = True
        self.entry.configure(show="•")

    def set_masked(self, masked: bool = True) -> None:
        self._masked = masked
        self.entry.configure(show="•" if masked else "")


class App(ttk.Frame):
    def __init__(self, root: tk.Tk) -> None:
        super().__init__(root, padding=12)
        self.root = root
        self.pages: list[FacebookPage] = []
        self.posts: list[PagePost] = []
        self._busy = False
        self._selected_page: FacebookPage | None = None
        self._long_lived_user_token: str = ""
        self._last_error_text = ""
        self._last_report: DiagnosticReport | None = None

        self.grid(sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        self._build_ui()
        self._set_status(f"Ready. Log: {log_file_path()}")
        log.info("GUI started Graph API %s", GRAPH_API_VERSION)

    def _build_ui(self) -> None:
        canvas = tk.Canvas(self, highlightthickness=0)
        vscroll = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        self._inner = ttk.Frame(canvas, padding=(0, 0, 8, 0))
        self._inner.bind(
            "<Configure>",
            lambda _e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        self._canvas_window = canvas.create_window((0, 0), window=self._inner, anchor="nw")
        canvas.configure(yscrollcommand=vscroll.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        vscroll.grid(row=0, column=1, sticky="ns")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        def _on_canvas_configure(event: tk.Event) -> None:  # type: ignore[type-arg]
            canvas.itemconfigure(self._canvas_window, width=event.width)

        canvas.bind("<Configure>", _on_canvas_configure)

        parent = self._inner
        parent.columnconfigure(0, weight=1)
        row = 0

        ttk.Label(parent, text="Meta Page Token Generator", style="Title.TLabel").grid(
            row=row, column=0, sticky="w"
        )
        row += 1

        ttk.Label(
            parent,
            text=(
                "Exchange a User Access Token → get Page Access Tokens from /me/accounts. "
                "Only the Page Access Token goes into the WordPress plugin."
            ),
            wraplength=860,
        ).grid(row=row, column=0, sticky="w", pady=(2, 4))
        row += 1

        ttk.Label(
            parent,
            text=f"Graph API: {GRAPH_API_VERSION}  ·  Debug log: logs/meta_token_generator.log",
            style="Muted.TLabel",
        ).grid(row=row, column=0, sticky="w", pady=(0, 8))
        row += 1

        # Credentials
        cred = ttk.LabelFrame(parent, text="Meta Credentials", padding=10)
        cred.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        cred.columnconfigure(1, weight=1)
        row += 1

        ttk.Label(cred, text="Meta App ID").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=3)
        self.app_id_var = tk.StringVar()
        ttk.Entry(cred, textvariable=self.app_id_var).grid(row=0, column=1, sticky="ew", pady=3)

        ttk.Label(cred, text="Meta App Secret").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=3)
        self.app_secret = MaskedEntry(cred)
        self.app_secret.grid(row=1, column=1, sticky="ew", pady=3)

        ttk.Label(cred, text="User Access Token (short-lived)").grid(
            row=2, column=0, sticky="w", padx=(0, 8), pady=3
        )
        self.short_lived_user_token_entry = MaskedEntry(cred)
        self.short_lived_user_token_entry.grid(row=2, column=1, sticky="ew", pady=3)

        btn_row = ttk.Frame(cred)
        btn_row.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.generate_btn = ttk.Button(
            btn_row, text="Generate Long-Lived Token + Load Pages", command=self.on_generate
        )
        self.generate_btn.pack(side="left")
        self.diag_btn = ttk.Button(btn_row, text="Run Diagnostics", command=self.on_run_diagnostics)
        self.diag_btn.pack(side="left", padx=(8, 0))
        ttk.Button(btn_row, text="Clear Sensitive Data", command=self.on_clear).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(btn_row, text="Open Log File", command=self.open_log_file).pack(
            side="left", padx=(8, 0)
        )

        ttk.Label(
            cred,
            text=(
                "Meta may invalidate access tokens when permissions, passwords, security settings, "
                "app access, Page access, or other account conditions change."
            ),
            wraplength=820,
            style="Muted.TLabel",
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))

        # Diagnostics
        diag = ttk.LabelFrame(parent, text="Diagnostics", padding=10)
        diag.grid(row=row, column=0, sticky="nsew", pady=(0, 8))
        diag.columnconfigure(0, weight=1)
        diag.rowconfigure(0, weight=1)
        parent.rowconfigure(row, weight=2)
        row += 1

        self.diag_text = scrolledtext.ScrolledText(diag, height=10, wrap="word", font=("Menlo", 11))
        self.diag_text.grid(row=0, column=0, sticky="nsew")
        self.diag_text.insert(
            "1.0",
            "Click “Generate…” or “Run Diagnostics”.\n"
            "FINAL token for the plugin = Page Access Token from /me/accounts "
            "(never the Long-Lived User Token).\n",
        )
        self.diag_text.configure(state="disabled")

        diag_btns = ttk.Frame(diag)
        diag_btns.grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Button(diag_btns, text="Copy Error Details", command=self.copy_error_details).pack(
            side="left"
        )
        ttk.Button(diag_btns, text="Copy Full Report", command=self.copy_full_report).pack(
            side="left", padx=(6, 0)
        )

        # Intermediate long-lived user token
        ll = ttk.LabelFrame(parent, text="Long-Lived User Access Token", padding=10)
        ll.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        ll.columnconfigure(0, weight=1)
        row += 1

        self.ll_info = ttk.Label(
            ll,
            text=(
                "Intermediate token — do not use this as the Page plugin token.\n"
                "Not generated yet."
            ),
            wraplength=820,
            style="Warn.TLabel",
        )
        self.ll_info.grid(row=0, column=0, sticky="w")

        self.long_lived_user_token_entry = MaskedEntry(ll, show_toggle=False)
        self.long_lived_user_token_entry.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.long_lived_user_token_entry.entry.configure(state="readonly")

        ll_btns = ttk.Frame(ll)
        ll_btns.grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Button(
            ll_btns,
            text="Copy (intermediate only)",
            command=self.copy_long_lived_user_token,
        ).pack(side="left")
        ttk.Button(
            ll_btns, text="Show / Hide", command=self.long_lived_user_token_entry.toggle
        ).pack(side="left", padx=(6, 0))

        # Pages list
        pages_frame = ttk.LabelFrame(parent, text="Available Facebook Pages", padding=10)
        pages_frame.grid(row=row, column=0, sticky="nsew", pady=(0, 8))
        pages_frame.columnconfigure(0, weight=1)
        pages_frame.rowconfigure(0, weight=1)
        parent.rowconfigure(row, weight=2)
        row += 1

        cols = ("name", "id", "tasks")
        self.pages_tree = ttk.Treeview(
            pages_frame, columns=cols, show="headings", height=5, selectmode="browse"
        )
        self.pages_tree.heading("name", text="Page Name")
        self.pages_tree.heading("id", text="Page ID")
        self.pages_tree.heading("tasks", text="Tasks")
        self.pages_tree.column("name", width=260, stretch=True)
        self.pages_tree.column("id", width=160, stretch=False)
        self.pages_tree.column("tasks", width=280, stretch=True)
        scroll_p = ttk.Scrollbar(pages_frame, orient="vertical", command=self.pages_tree.yview)
        self.pages_tree.configure(yscrollcommand=scroll_p.set)
        self.pages_tree.grid(row=0, column=0, sticky="nsew")
        scroll_p.grid(row=0, column=1, sticky="ns")
        self.pages_tree.bind("<<TreeviewSelect>>", self.on_page_select)

        # FINAL TOKEN FOR PLUGIN
        final = ttk.LabelFrame(parent, text="FINAL TOKEN FOR PLUGIN", padding=10)
        final.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        final.columnconfigure(1, weight=1)
        row += 1

        ttk.Label(
            final,
            text="This is the Page Access Token to use in your Facebook Page plugin.",
            style="Emph.TLabel",
            wraplength=820,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        ttk.Label(final, text="Page:").grid(row=1, column=0, sticky="w", padx=(0, 8))
        self.final_page_name = ttk.Label(final, text="—")
        self.final_page_name.grid(row=1, column=1, sticky="w")

        ttk.Label(final, text="Page ID:").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=(4, 0))
        self.final_page_id = ttk.Label(final, text="—")
        self.final_page_id.grid(row=2, column=1, sticky="w", pady=(4, 0))

        ttk.Label(final, text="Page Access Token:").grid(
            row=3, column=0, sticky="nw", padx=(0, 8), pady=(4, 0)
        )
        # IMPORTANT:
        # The plugin requires the Page Access Token returned by /me/accounts,
        # not the long-lived User Access Token.
        self.page_access_token_entry = MaskedEntry(final, show_toggle=False)
        self.page_access_token_entry.grid(row=3, column=1, sticky="ew", pady=(4, 0))
        self.page_access_token_entry.entry.configure(state="readonly")

        final_btns = ttk.Frame(final)
        final_btns.grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(
            final_btns, text="Show / Hide", command=self.page_access_token_entry.toggle
        ).pack(side="left")
        ttk.Button(final_btns, text="Copy Token", command=self.copy_final_page_token).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(final_btns, text="Copy Page ID", command=self.copy_page_id).pack(
            side="left", padx=(6, 0)
        )
        self.test_btn = ttk.Button(final_btns, text="Test Token", command=self.on_test_page)
        self.test_btn.pack(side="left", padx=(6, 0))
        self.posts_btn = ttk.Button(
            final_btns, text="Test Last 3 Posts", command=self.on_load_posts
        )
        self.posts_btn.pack(side="left", padx=(6, 0))

        self.test_result = tk.Label(
            final,
            text="",
            wraplength=820,
            justify="left",
            anchor="w",
            fg=_LABEL_FG_DEFAULT,
        )
        self.test_result.grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))

        # Posts display
        posts_frame = ttk.LabelFrame(parent, text="Last 3 Posts (published_posts)", padding=10)
        posts_frame.grid(row=row, column=0, sticky="nsew", pady=(0, 8))
        posts_frame.columnconfigure(0, weight=1)
        posts_frame.rowconfigure(0, weight=1)
        parent.rowconfigure(row, weight=2)
        row += 1

        self.posts_view = scrolledtext.ScrolledText(
            posts_frame, height=10, wrap="word", font=("Menlo", 11)
        )
        self.posts_view.grid(row=0, column=0, sticky="nsew")
        self.posts_view.insert("1.0", "Select a Page, then click “Test Last 3 Posts”.\n")
        self.posts_view.configure(state="disabled")

        ttk.Button(
            posts_frame, text="Open Selected Permalink in Browser", command=self.open_selected_post
        ).grid(row=1, column=0, sticky="w", pady=(6, 0))

        # Status
        status = ttk.Frame(self)
        status.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        status.columnconfigure(0, weight=1)
        self.status_var = tk.StringVar(value="")
        ttk.Label(status, textvariable=self.status_var).grid(row=0, column=0, sticky="w")
        self.progress = ttk.Progressbar(status, mode="indeterminate", length=160)
        self.progress.grid(row=0, column=1, sticky="e", padx=(8, 0))

    # -------------------------------------------------------------- helpers
    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    def _set_diag_text(self, text: str) -> None:
        self.diag_text.configure(state="normal")
        self.diag_text.delete("1.0", "end")
        self.diag_text.insert("1.0", text)
        self.diag_text.configure(state="disabled")

    def _set_posts_text(self, text: str) -> None:
        self.posts_view.configure(state="normal")
        self.posts_view.delete("1.0", "end")
        self.posts_view.insert("1.0", text)
        self.posts_view.configure(state="disabled")

    def _clear_test_result(self) -> None:
        # Never use fg="" — causes TclError: unknown color name ""
        self.test_result.configure(text="", fg=_LABEL_FG_DEFAULT)

    def _set_test_result(self, text: str, *, ok: bool = True) -> None:
        self.test_result.configure(text=text, fg=_LABEL_FG_OK if ok else _LABEL_FG_ERR)

    def _set_busy(self, busy: bool, status: str | None = None) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        self.generate_btn.configure(state=state)
        self.diag_btn.configure(state=state)
        self.test_btn.configure(state=state)
        self.posts_btn.configure(state=state)
        if busy:
            self.progress.start(12)
            if status:
                self._set_status(status)
        else:
            self.progress.stop()
            if status:
                self._set_status(status)

    def _run_async(
        self,
        work: Callable[[], object],
        on_success: Callable[[object], None],
        *,
        busy_message: str,
        buttons: tuple[ttk.Button, ...] | None = None,
    ) -> None:
        if self._busy:
            return

        disabled: list[ttk.Button] = []
        if buttons:
            for b in buttons:
                b.configure(state="disabled")
                disabled.append(b)

        self._set_busy(True, busy_message)

        def worker() -> None:
            try:
                result = work()
            except MetaAPIError as exc:
                err = exc
                self.root.after(0, lambda: self._on_async_error(err, disabled))
            except Exception as exc:  # noqa: BLE001
                log.exception("Unexpected GUI worker error")
                err = MetaAPIError(
                    f"Unexpected error: {exc.__class__.__name__}: {exc}",
                    operation="UNEXPECTED",
                )
                self.root.after(0, lambda: self._on_async_error(err, disabled))
            else:
                self.root.after(0, lambda: self._on_async_ok(result, on_success, disabled))

        threading.Thread(target=worker, daemon=True).start()

    def _on_async_ok(
        self,
        result: object,
        on_success: Callable[[object], None],
        disabled: list[ttk.Button],
    ) -> None:
        for b in disabled:
            b.configure(state="normal")
        self._set_busy(False)
        on_success(result)

    def _on_async_error(self, exc: MetaAPIError, disabled: list[ttk.Button]) -> None:
        for b in disabled:
            b.configure(state="normal")
        self._set_busy(False, "Error — see Diagnostics / log.")
        self._clear_test_result()
        self._last_error_text = exc.format_for_user()
        extra = self._last_report.full_report_text() if self._last_report else ""
        self._set_diag_text(self._last_error_text + ("\n\n" + extra if extra else ""))
        self._set_test_result(exc.message, ok=False)
        self._show_error_dialog(exc)

    def _show_error_dialog(self, exc: MetaAPIError) -> None:
        win = tk.Toplevel(self.root)
        win.title("Meta API Error")
        win.transient(self.root)
        win.grab_set()
        win.geometry("520x420")
        win.minsize(420, 320)

        body = scrolledtext.ScrolledText(win, wrap="word", font=("Menlo", 11))
        body.pack(fill="both", expand=True, padx=12, pady=12)
        body.insert("1.0", exc.format_for_user())
        body.configure(state="disabled")

        btns = ttk.Frame(win)
        btns.pack(fill="x", padx=12, pady=(0, 12))

        def copy_details() -> None:
            self.root.clipboard_clear()
            self.root.clipboard_append(exc.safe_details_for_clipboard())
            self._set_status("Copied error details (no secrets).")

        ttk.Button(btns, text="Copy Error Details", command=copy_details).pack(side="left")
        ttk.Button(btns, text="Open Log File", command=self.open_log_file).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(btns, text="Close", command=win.destroy).pack(side="right")

    def _copy_text(self, value: str, label: str) -> None:
        if not value:
            messagebox.showinfo("Copy", f"No {label} to copy.")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(value)
        self._set_status(f"Copied {label} to clipboard.")

    def _set_readonly_entry(self, widget: MaskedEntry, value: str) -> None:
        widget.entry.configure(state="normal")
        widget.set(value)
        widget.set_masked(True)
        widget.entry.configure(state="readonly")

    def open_log_file(self) -> None:
        path = log_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("Log file created.\n", encoding="utf-8")
        try:
            if sys.platform == "darwin":
                subprocess.run(["open", str(path)], check=False)
            elif sys.platform == "win32":
                os.startfile(str(path))  # type: ignore[attr-defined]
            else:
                subprocess.run(["xdg-open", str(path)], check=False)
            self._set_status(f"Opened log: {path}")
        except OSError as exc:
            messagebox.showerror("Open Log File", f"Could not open log file:\n{path}\n\n{exc}")

    def copy_error_details(self) -> None:
        text = self._last_error_text or "No error details yet."
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._set_status("Copied error details (no secrets).")

    def copy_full_report(self) -> None:
        if self._last_report:
            text = self._last_report.full_report_text()
        else:
            text = self.diag_text.get("1.0", "end").strip() or "No report yet."
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._set_status("Copied diagnostic report (no secrets).")

    # -------------------------------------------------------------- actions
    def on_generate(self) -> None:
        app_id = self.app_id_var.get().strip()
        app_secret = self.app_secret.get().strip()
        short_lived_user_token = self.short_lived_user_token_entry.get().strip()

        def work() -> tuple[TokenExchangeResult, list[FacebookPage], str | None]:
            exchanged = exchange_user_token(app_id, app_secret, short_lived_user_token)
            pages_error: str | None = None
            pages: list[FacebookPage] = []
            try:
                pages = fetch_all_pages(exchanged.long_lived_user_token)
            except MetaAPIError as exc:
                pages_error = exc.format_for_user()
            return exchanged, pages, pages_error

        def ok(result: object) -> None:
            exchanged, pages, pages_error = result  # type: ignore[misc]
            assert isinstance(exchanged, TokenExchangeResult)
            assert isinstance(pages, list)

            self._long_lived_user_token = exchanged.long_lived_user_token

            days = exchanged.approx_days
            days_txt = f" (~{days} days)" if days is not None else ""
            expires_txt = (
                f"expires_in={exchanged.expires_in}{days_txt}"
                if exchanged.expires_in is not None
                else "expires_in not provided by Meta"
            )
            self.ll_info.configure(
                text=(
                    "Intermediate token — do not use this as the Page plugin token.\n"
                    f"Generated · token_type: {exchanged.token_type}  ·  {expires_txt}\n"
                    "Next step: select a Page below and copy the Page Access Token."
                )
            )
            self._set_readonly_entry(
                self.long_lived_user_token_entry, exchanged.long_lived_user_token
            )
            self._populate_pages(pages)

            summary_lines = [
                "USER TOKEN",
                "✓ Valid (exchange succeeded)",
                "",
                "TOKEN EXCHANGE",
                "✓ Long-Lived User Token generated (INTERMEDIATE — not for plugin)",
                "",
                "FACEBOOK PAGES",
            ]
            if pages_error:
                summary_lines.append("✗ /me/accounts failed")
                summary_lines.append(pages_error)
                self._set_status("Long-lived token OK — Page list failed.")
            elif not pages:
                summary_lines.append("✗ /me/accounts returned 0 pages")
                self._set_status("Long-lived token OK — no Pages returned.")
            else:
                summary_lines.append("✓ /me/accounts successful")
                summary_lines.append(f"✓ {len(pages)} Pages found")
                summary_lines.append("")
                summary_lines.append("Select a Page to retrieve the FINAL Page Access Token.")
                self._set_status(f"Loaded {len(pages)} Facebook Page(s). Select one for the plugin token.")
            self._set_diag_text("\n".join(summary_lines))

        self._run_async(
            work,
            ok,
            busy_message="Exchanging token and loading Pages...",
            buttons=(self.generate_btn, self.diag_btn),
        )

    def on_run_diagnostics(self) -> None:
        app_id = self.app_id_var.get().strip()
        app_secret = self.app_secret.get().strip()
        short_lived_user_token = self.short_lived_user_token_entry.get().strip()

        self._set_diag_text("Running diagnostics…\n")

        def work() -> DiagnosticReport:
            return run_full_diagnostics(app_id, app_secret, short_lived_user_token)

        def ok(result: object) -> None:
            report = result  # type: ignore[misc]
            assert isinstance(report, DiagnosticReport)
            self._last_report = report
            self._set_diag_text(report.checklist_text() + "\n\n" + report.full_report_text())

            if report.exchanged:
                exchanged = report.exchanged
                self._long_lived_user_token = exchanged.long_lived_user_token
                self.ll_info.configure(
                    text=(
                        "Intermediate token — do not use this as the Page plugin token.\n"
                        f"Generated · token_type: {exchanged.token_type}"
                    )
                )
                self._set_readonly_entry(
                    self.long_lived_user_token_entry, exchanged.long_lived_user_token
                )

            if report.pages:
                self._populate_pages(report.pages)

            if report.failed_step:
                self._last_error_text = report.full_report_text()
                self._set_status(f"Diagnostics stopped at: {report.failed_step}")
                messagebox.showwarning(
                    "Diagnostics",
                    f"Stopped at: {report.failed_step}\n\nSee Diagnostics panel / log.",
                )
            else:
                self._set_status(f"Diagnostics OK — {report.pages_count} page(s).")
                messagebox.showinfo(
                    "Diagnostics",
                    f"All steps succeeded.\nPages found: {report.pages_count}\n\n"
                    "Select a Page and use Copy Token (Page Access Token) for the plugin.",
                )

        self._run_async(
            work,
            ok,
            busy_message="Running diagnostics against Meta Graph API...",
            buttons=(self.generate_btn, self.diag_btn),
        )

    def _populate_pages(self, pages: list[FacebookPage]) -> None:
        self.pages = pages
        self.pages_tree.delete(*self.pages_tree.get_children())
        self._clear_selection_ui()
        for idx, page in enumerate(pages):
            self.pages_tree.insert(
                "",
                "end",
                iid=str(idx),
                values=(page.name, page.id, page.tasks_display()),
            )

    def _clear_selection_ui(self) -> None:
        self._selected_page = None
        self.final_page_name.configure(text="—")
        self.final_page_id.configure(text="—")
        self._set_readonly_entry(self.page_access_token_entry, "")
        self._clear_test_result()
        self.posts = []
        self._set_posts_text("Select a Page, then click “Test Last 3 Posts”.\n")

    def on_page_select(self, _event: object = None) -> None:
        sel = self.pages_tree.selection()
        if not sel:
            return
        try:
            idx = int(sel[0])
        except ValueError:
            return
        if idx < 0 or idx >= len(self.pages):
            return

        page = self.pages[idx]
        self._selected_page = page

        # IMPORTANT:
        # The plugin requires the Page Access Token returned by /me/accounts,
        # not the long-lived User Access Token.
        page_access_token = page.page_access_token

        self.final_page_name.configure(text=page.name)
        self.final_page_id.configure(text=page.id)
        self._set_readonly_entry(self.page_access_token_entry, page_access_token)
        self._clear_test_result()

        diag = (
            f"SELECTED PAGE\n"
            f"✓ {page.name}\n"
            f"✓ Page ID: {page.id}\n\n"
            f"PAGE ACCESS TOKEN\n"
            f"✓ Retrieved from /me/accounts (data[].access_token)\n"
            f"  (not the Long-Lived User Token)\n\n"
            f"Validating Page token…"
        )
        self._set_diag_text(diag)
        self._auto_validate_selected_page(page)

    def _auto_validate_selected_page(self, page: FacebookPage) -> None:
        page_id = page.id
        page_access_token = page.page_access_token

        def work() -> tuple[str, str, list[PagePost]]:
            returned_id, name = verify_page_token_belongs(page_id, page_access_token)
            posts = fetch_published_posts(page_id, page_access_token, limit=3)
            return returned_id, name, posts

        def ok(result: object) -> None:
            returned_id, name, posts = result  # type: ignore[misc]
            self.posts = posts
            self._set_test_result(
                "✓ Page token belongs to selected Page\n"
                "✓ Page Access Token VALID\n"
                "✓ published_posts accessible\n"
                "✓ Token ready for plugin",
                ok=True,
            )
            self._render_posts(posts)
            self._set_diag_text(
                f"USER TOKEN\n✓ Valid\n\n"
                f"TOKEN EXCHANGE\n✓ Long-Lived User Token generated (intermediate)\n\n"
                f"FACEBOOK PAGES\n✓ /me/accounts successful\n✓ {len(self.pages)} Pages found\n\n"
                f"SELECTED PAGE\n✓ {name}\n✓ Page ID: {returned_id}\n\n"
                f"PAGE ACCESS TOKEN\n✓ Retrieved\n✓ Token identifies correct Page\n\n"
                f"PUBLISHED POSTS\n✓ Accessible\n✓ {len(posts)} posts retrieved\n\n"
                f"FINAL RESULT\n✓ PAGE ACCESS TOKEN READY FOR PLUGIN"
            )
            self._set_status("Page Access Token ready for plugin.")

        self._run_async(
            work,
            ok,
            busy_message="Validating Page Access Token + published_posts...",
            buttons=(self.test_btn, self.posts_btn),
        )

    def copy_long_lived_user_token(self) -> None:
        self._copy_text(
            self.long_lived_user_token_entry.get(),
            "Long-Lived User Access Token (intermediate — NOT for plugin)",
        )

    def copy_final_page_token(self) -> None:
        # IMPORTANT:
        # The plugin requires the Page Access Token returned by /me/accounts,
        # not the long-lived User Access Token.
        if self._selected_page is not None:
            token = self._selected_page.page_access_token
        else:
            token = self.page_access_token_entry.get()
        self._copy_text(token, "Page Access Token (FINAL — for plugin)")

    def copy_page_id(self) -> None:
        text = self.final_page_id.cget("text")
        self._copy_text(text if text != "—" else "", "Page ID")

    def on_test_page(self) -> None:
        page = self._selected_page
        if not page:
            messagebox.showinfo("Test Token", "Select a Facebook Page first.")
            return

        def work() -> tuple[str, str, list[PagePost]]:
            returned_id, name = verify_page_token_belongs(page.id, page.page_access_token)
            posts = fetch_published_posts(page.id, page.page_access_token, limit=3)
            return returned_id, name, posts

        def ok(result: object) -> None:
            _pid, _name, posts = result  # type: ignore[misc]
            self.posts = posts
            self._set_test_result(
                "✓ Page token belongs to selected Page\n"
                "✓ Page Access Token VALID\n"
                "✓ published_posts accessible\n"
                "✓ Token ready for plugin",
                ok=True,
            )
            self._render_posts(posts)
            self._set_status("Page Access Token is working.")

        self._run_async(
            work,
            ok,
            busy_message="Testing Page Access Token...",
            buttons=(self.test_btn,),
        )

    def on_load_posts(self) -> None:
        page = self._selected_page
        if not page:
            messagebox.showinfo("Test Last 3 Posts", "Select a Facebook Page first.")
            return

        def work() -> list[PagePost]:
            return fetch_published_posts(page.id, page.page_access_token, limit=3)

        def ok(result: object) -> None:
            posts = result  # type: ignore[misc]
            assert isinstance(posts, list)
            self.posts = posts
            self._render_posts(posts)
            self._set_test_result(
                f"✓ published_posts accessible\n✓ {len(posts)} posts retrieved",
                ok=True,
            )
            self._set_status(f"Loaded {len(posts)} post(s) via published_posts.")

        self._run_async(
            work,
            ok,
            busy_message="Loading published_posts...",
            buttons=(self.posts_btn,),
        )

    def _render_posts(self, posts: list[PagePost]) -> None:
        if not posts:
            self._set_posts_text("No posts returned for this Page.\n")
            return
        blocks: list[str] = []
        for i, post in enumerate(posts, start=1):
            blocks.append(
                f"Post #{i}\n"
                f"ID: {post.id or '—'}\n"
                f"Date: {post.created_time or '—'}\n"
                f"Message: {post.message or '(no message)'}\n"
                f"Image: {post.full_picture or '—'}\n"
                f"URL: {post.permalink_url or '—'}\n"
            )
        self._set_posts_text("\n".join(blocks))

    def open_selected_post(self) -> None:
        if not self.posts:
            messagebox.showinfo("Open Post", "Load posts first.")
            return
        # Open first post with a permalink (simple UX).
        for post in self.posts:
            if post.permalink_url:
                webbrowser.open(post.permalink_url)
                self._set_status("Opened post in browser.")
                return
        messagebox.showinfo("Open Post", "No permalink_url on loaded posts.")

    def on_clear(self) -> None:
        if not messagebox.askyesno(
            "Clear Sensitive Data",
            "Clear App Secret, tokens, Page tokens, and results from memory?",
        ):
            return
        self.app_secret.clear()
        self.short_lived_user_token_entry.clear()
        self._long_lived_user_token = ""
        self._set_readonly_entry(self.long_lived_user_token_entry, "")
        self.ll_info.configure(
            text=(
                "Intermediate token — do not use this as the Page plugin token.\n"
                "Not generated yet."
            )
        )
        self.pages = []
        self.pages_tree.delete(*self.pages_tree.get_children())
        self._clear_selection_ui()
        self._last_error_text = ""
        self._last_report = None
        self._set_diag_text("Sensitive data cleared.\n")
        self._set_status("Sensitive data cleared.")


def configure_styles(root: tk.Tk) -> None:
    style = ttk.Style(root)
    preferred = ("aqua", "clam", "alt", "default")
    for name in preferred:
        if name in style.theme_names():
            style.theme_use(name)
            break
    style.configure("Title.TLabel", font=("Helvetica Neue", 18, "bold"))
    style.configure("Muted.TLabel", foreground="#555555")
    style.configure("Warn.TLabel", foreground="#8a5a00")
    style.configure("Emph.TLabel", foreground="#0b5fff", font=("Helvetica Neue", 12, "bold"))


def main() -> None:
    root = tk.Tk()
    root.title("Meta Page Token Generator")
    root.geometry("920x820")
    root.minsize(800, 680)
    configure_styles(root)
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
