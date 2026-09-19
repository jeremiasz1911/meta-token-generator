#!/usr/bin/env python3
"""
Meta Page Token Generator — macOS desktop GUI.

Convert a Meta User Access Token to a long-lived token and retrieve Page Access Tokens.
HTTP work runs in background threads; secrets are never printed to the terminal.
"""

from __future__ import annotations

import threading
import webbrowser
from typing import Callable

import tkinter as tk
from tkinter import messagebox, ttk

from meta_api import (
    GRAPH_API_VERSION,
    FacebookPage,
    MetaAPIError,
    PagePost,
    TokenExchangeResult,
    exchange_user_token,
    fetch_all_pages,
    fetch_last_posts,
    test_page_token,
)


class MaskedEntry(ttk.Frame):
    """Entry that can toggle between masked and plain text, with optional Show/Hide."""

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

        self.grid(sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        self._build_ui()
        self._set_status("Ready.")

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        row = 0

        title = ttk.Label(self, text="Meta Page Token Generator", style="Title.TLabel")
        title.grid(row=row, column=0, sticky="w")
        row += 1

        subtitle = ttk.Label(
            self,
            text="Convert a Meta User Access Token to a long-lived token and retrieve Page Access Tokens.",
            wraplength=860,
        )
        subtitle.grid(row=row, column=0, sticky="w", pady=(2, 4))
        row += 1

        api_lbl = ttk.Label(self, text=f"Graph API: {GRAPH_API_VERSION}", style="Muted.TLabel")
        api_lbl.grid(row=row, column=0, sticky="w", pady=(0, 8))
        row += 1

        # Credentials
        cred = ttk.LabelFrame(self, text="Meta Credentials", padding=10)
        cred.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        cred.columnconfigure(1, weight=1)
        row += 1

        ttk.Label(cred, text="Meta App ID").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=3)
        self.app_id_var = tk.StringVar()
        ttk.Entry(cred, textvariable=self.app_id_var).grid(row=0, column=1, sticky="ew", pady=3)

        ttk.Label(cred, text="Meta App Secret").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=3)
        self.app_secret = MaskedEntry(cred)
        self.app_secret.grid(row=1, column=1, sticky="ew", pady=3)

        ttk.Label(cred, text="User Access Token").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=3)
        self.user_token = MaskedEntry(cred)
        self.user_token.grid(row=2, column=1, sticky="ew", pady=3)

        btn_row = ttk.Frame(cred)
        btn_row.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.generate_btn = ttk.Button(
            btn_row,
            text="Generate Long-Lived Token",
            command=self.on_generate,
        )
        self.generate_btn.pack(side="left")
        ttk.Button(btn_row, text="Clear Sensitive Data", command=self.on_clear).pack(side="left", padx=(8, 0))

        notice = ttk.Label(
            cred,
            text=(
                "Meta may invalidate access tokens when permissions, passwords, security settings, "
                "app access, Page access, or other account conditions change."
            ),
            wraplength=820,
            style="Muted.TLabel",
        )
        notice.grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))

        # Long-lived token
        ll = ttk.LabelFrame(self, text="Long-Lived User Access Token", padding=10)
        ll.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        ll.columnconfigure(0, weight=1)
        row += 1

        self.ll_info = ttk.Label(ll, text="Not generated yet.", wraplength=820)
        self.ll_info.grid(row=0, column=0, sticky="w")

        self.ll_token = MaskedEntry(ll, show_toggle=False)
        self.ll_token.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.ll_token.entry.configure(state="readonly")

        ll_btns = ttk.Frame(ll)
        ll_btns.grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Button(ll_btns, text="Copy", command=self.copy_ll_token).pack(side="left")
        ttk.Button(ll_btns, text="Show / Hide", command=self.ll_token.toggle).pack(side="left", padx=(6, 0))

        # Pages
        pages_frame = ttk.LabelFrame(self, text="Available Facebook Pages", padding=10)
        pages_frame.grid(row=row, column=0, sticky="nsew", pady=(0, 8))
        pages_frame.columnconfigure(0, weight=1)
        pages_frame.rowconfigure(0, weight=1)
        self.rowconfigure(row, weight=2)
        row += 1

        cols = ("name", "id", "tasks")
        self.pages_tree = ttk.Treeview(
            pages_frame,
            columns=cols,
            show="headings",
            height=6,
            selectmode="browse",
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

        # Selected page
        sel = ttk.LabelFrame(self, text="Selected Page", padding=10)
        sel.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        sel.columnconfigure(1, weight=1)
        row += 1

        ttk.Label(sel, text="Page Name:").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.sel_name = ttk.Label(sel, text="—")
        self.sel_name.grid(row=0, column=1, sticky="w")

        ttk.Label(sel, text="Page ID:").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(4, 0))
        self.sel_id = ttk.Label(sel, text="—")
        self.sel_id.grid(row=1, column=1, sticky="w", pady=(4, 0))

        ttk.Label(sel, text="Page Access Token:").grid(row=2, column=0, sticky="nw", padx=(0, 8), pady=(4, 0))
        self.page_token = MaskedEntry(sel, show_toggle=False)
        self.page_token.grid(row=2, column=1, sticky="ew", pady=(4, 0))
        self.page_token.entry.configure(state="readonly")

        sel_btns = ttk.Frame(sel)
        sel_btns.grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(sel_btns, text="Show / Hide", command=self.page_token.toggle).pack(side="left")
        ttk.Button(sel_btns, text="Copy Page Token", command=self.copy_page_token).pack(side="left", padx=(6, 0))
        ttk.Button(sel_btns, text="Copy Page ID", command=self.copy_page_id).pack(side="left", padx=(6, 0))
        self.test_btn = ttk.Button(sel_btns, text="Test Page Token", command=self.on_test_page)
        self.test_btn.pack(side="left", padx=(6, 0))
        self.posts_btn = ttk.Button(sel_btns, text="Load Last 3 Posts", command=self.on_load_posts)
        self.posts_btn.pack(side="left", padx=(6, 0))

        self.test_result = tk.Label(sel, text="", wraplength=820, justify="left", anchor="w")
        self.test_result.grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))

        # Posts
        posts_frame = ttk.LabelFrame(self, text="Last 3 Posts", padding=10)
        posts_frame.grid(row=row, column=0, sticky="nsew", pady=(0, 8))
        posts_frame.columnconfigure(0, weight=1)
        posts_frame.rowconfigure(0, weight=1)
        self.rowconfigure(row, weight=3)
        row += 1

        post_cols = ("id", "created", "message", "permalink", "picture", "attachments")
        self.posts_tree = ttk.Treeview(
            posts_frame,
            columns=post_cols,
            show="headings",
            height=5,
            selectmode="browse",
        )
        headings = {
            "id": "ID",
            "created": "Published",
            "message": "Message",
            "permalink": "Permalink",
            "picture": "full_picture",
            "attachments": "Attachments",
        }
        widths = {
            "id": 120,
            "created": 140,
            "message": 220,
            "permalink": 160,
            "picture": 140,
            "attachments": 160,
        }
        for c in post_cols:
            self.posts_tree.heading(c, text=headings[c])
            self.posts_tree.column(c, width=widths[c], stretch=True)
        scroll_posts = ttk.Scrollbar(posts_frame, orient="vertical", command=self.posts_tree.yview)
        self.posts_tree.configure(yscrollcommand=scroll_posts.set)
        self.posts_tree.grid(row=0, column=0, sticky="nsew")
        scroll_posts.grid(row=0, column=1, sticky="ns")

        ttk.Button(
            posts_frame,
            text="Open Post in Browser",
            command=self.open_selected_post,
        ).grid(row=1, column=0, sticky="w", pady=(6, 0))

        # Status bar
        status = ttk.Frame(self)
        status.grid(row=row, column=0, sticky="ew")
        status.columnconfigure(0, weight=1)
        self.status_var = tk.StringVar(value="")
        ttk.Label(status, textvariable=self.status_var).grid(row=0, column=0, sticky="w")
        self.progress = ttk.Progressbar(status, mode="indeterminate", length=160)
        self.progress.grid(row=0, column=1, sticky="e", padx=(8, 0))

    # -------------------------------------------------------------- helpers
    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    def _set_busy(self, busy: bool, status: str | None = None) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        self.generate_btn.configure(state=state)
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
            except Exception as exc:  # noqa: BLE001 — surface unexpected errors in GUI
                err = MetaAPIError(f"Unexpected error: {exc.__class__.__name__}: {exc}")
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
        self._set_busy(False, "Error.")
        self.test_result.configure(text="", fg="")
        messagebox.showerror("Meta API Error", exc.format_for_user())

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

    # -------------------------------------------------------------- actions
    def on_generate(self) -> None:
        app_id = self.app_id_var.get().strip()
        app_secret = self.app_secret.get().strip()
        user_token = self.user_token.get().strip()

        def work() -> tuple[TokenExchangeResult, list[FacebookPage], str | None]:
            exchanged = exchange_user_token(app_id, app_secret, user_token)
            pages_error: str | None = None
            pages: list[FacebookPage] = []
            try:
                pages = fetch_all_pages(exchanged.access_token)
            except MetaAPIError as exc:
                pages_error = exc.format_for_user()
            return exchanged, pages, pages_error

        def ok(result: object) -> None:
            exchanged, pages, pages_error = result  # type: ignore[misc]
            assert isinstance(exchanged, TokenExchangeResult)
            assert isinstance(pages, list)

            days = exchanged.approx_days
            days_txt = f" (~{days} days)" if days is not None else ""
            expires_txt = (
                f"expires_in={exchanged.expires_in}{days_txt}"
                if exchanged.expires_in is not None
                else "expires_in not provided by Meta"
            )
            self.ll_info.configure(
                text=(
                    f"Long-lived User Access Token generated.\n"
                    f"token_type: {exchanged.token_type}  ·  {expires_txt}"
                )
            )
            self._set_readonly_entry(self.ll_token, exchanged.access_token)
            self._populate_pages(pages)

            if pages_error:
                self._set_status("Long-lived token OK — Page list failed.")
                messagebox.showwarning(
                    "Pages not loaded",
                    "Long-lived User Access Token was generated and is shown above.\n\n"
                    "Listing Facebook Pages failed:\n\n"
                    + pages_error
                    + "\n\nAdd pages_show_list to the User Token and try again, "
                    "or use Graph API Explorer with Pages selected.",
                )
            elif not pages:
                self._set_status("Long-lived token OK — no Pages returned.")
                messagebox.showwarning(
                    "No Pages found",
                    "Long-lived User Access Token was generated.\n\n"
                    "Meta returned no Pages for /me/accounts.\n\n"
                    "Check:\n"
                    "• Permission pages_show_list\n"
                    "• You are admin/editor of at least one Page\n"
                    "• In Graph API Explorer, select the Pages you manage when creating the token",
                )
            else:
                self._set_status(f"Loaded {len(pages)} Facebook Page(s).")

        self._run_async(
            work,
            ok,
            busy_message="Contacting Meta Graph API...",
            buttons=(self.generate_btn,),
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
        self.sel_name.configure(text="—")
        self.sel_id.configure(text="—")
        self._set_readonly_entry(self.page_token, "")
        self.test_result.configure(text="", fg="")
        self.posts = []
        self.posts_tree.delete(*self.posts_tree.get_children())

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
        self.sel_name.configure(text=page.name)
        self.sel_id.configure(text=page.id)
        self._set_readonly_entry(self.page_token, page.access_token)
        self.test_result.configure(text="", fg="")

    def copy_ll_token(self) -> None:
        self._copy_text(self.ll_token.get(), "Long-Lived User Access Token")

    def copy_page_token(self) -> None:
        self._copy_text(self.page_token.get(), "Page Access Token")

    def copy_page_id(self) -> None:
        self._copy_text(self.sel_id.cget("text") if self.sel_id.cget("text") != "—" else "", "Page ID")

    def on_test_page(self) -> None:
        page = self._selected_page
        if not page:
            messagebox.showinfo("Test Page Token", "Select a Facebook Page first.")
            return

        def work() -> tuple[str, str]:
            return test_page_token(page.id, page.access_token)

        def ok(result: object) -> None:
            pid, name = result  # type: ignore[misc]
            self.test_result.configure(
                text=f"Page Access Token is working\nName: {name}\nID: {pid}",
                fg="#1a7f37",
            )
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
            messagebox.showinfo("Load Posts", "Select a Facebook Page first.")
            return

        def work() -> list[PagePost]:
            return fetch_last_posts(page.id, page.access_token, limit=3)

        def ok(result: object) -> None:
            posts = result  # type: ignore[misc]
            assert isinstance(posts, list)
            self.posts = posts
            self.posts_tree.delete(*self.posts_tree.get_children())
            for idx, post in enumerate(posts):
                msg = post.message.replace("\n", " ")
                if len(msg) > 120:
                    msg = msg[:117] + "…"
                self.posts_tree.insert(
                    "",
                    "end",
                    iid=str(idx),
                    values=(
                        post.id,
                        post.created_time,
                        msg,
                        post.permalink_url or "—",
                        post.full_picture or "—",
                        post.attachments_summary,
                    ),
                )
            if not posts:
                self._set_status("No posts returned for this Page.")
            else:
                self._set_status(f"Loaded {len(posts)} post(s).")

        self._run_async(
            work,
            ok,
            busy_message="Loading last 3 posts...",
            buttons=(self.posts_btn,),
        )

    def open_selected_post(self) -> None:
        sel = self.posts_tree.selection()
        if not sel:
            messagebox.showinfo("Open Post", "Select a post in the table first.")
            return
        try:
            idx = int(sel[0])
        except ValueError:
            return
        if idx < 0 or idx >= len(self.posts):
            return
        url = self.posts[idx].permalink_url
        if not url:
            messagebox.showinfo("Open Post", "This post has no permalink_url.")
            return
        webbrowser.open(url)
        self._set_status("Opened post in browser.")

    def on_clear(self) -> None:
        if not messagebox.askyesno(
            "Clear Sensitive Data",
            "Clear App Secret, tokens, Page tokens, and results from memory?",
        ):
            return
        self.app_secret.clear()
        self.user_token.clear()
        self._set_readonly_entry(self.ll_token, "")
        self.ll_info.configure(text="Not generated yet.")
        self.pages = []
        self.pages_tree.delete(*self.pages_tree.get_children())
        self._clear_selection_ui()
        self._set_status("Sensitive data cleared.")


def configure_styles(root: tk.Tk) -> None:
    style = ttk.Style(root)
    # Prefer a native-looking theme on macOS when available.
    preferred = ("aqua", "clam", "alt", "default")
    for name in preferred:
        if name in style.theme_names():
            style.theme_use(name)
            break
    style.configure("Title.TLabel", font=("Helvetica Neue", 18, "bold"))
    style.configure("Muted.TLabel", foreground="#555555")


def main() -> None:
    root = tk.Tk()
    root.title("Meta Page Token Generator")
    root.geometry("900x700")
    root.minsize(780, 600)
    configure_styles(root)
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
