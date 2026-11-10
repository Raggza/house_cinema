"""The console window (Windows, Linux): what the hub prints, in a window that the tray icon
shows and hides. Closing it only hides it; the hub keeps running in the tray.

Tk runs on the main thread. Other threads (tray, hub) never touch Tk: they call
``ConsoleWindow.call_soon`` and the window runs the function from its own loop.
"""

from __future__ import annotations

import queue
import sys
import tkinter as tk
from collections.abc import Callable
from tkinter import font as tkfont

from source.desktop.logs import LogBuffer

POLL_MS = 100


class ConsoleWindow:
    def __init__(self, buffer: LogBuffer, *, title: str, icon_image=None,
                 actions: list[tuple[str, Callable[[], None]]] = ()):
        self._buffer = buffer
        self._seen = 0
        self._calls: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        self.root = tk.Tk()
        self.root.withdraw()  # starts hidden: the hub lives in the tray
        self.root.title(title)
        self.root.geometry("980x560")
        self.root.minsize(560, 300)
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        if icon_image is not None:
            try:
                from PIL import ImageTk

                self._icon = ImageTk.PhotoImage(icon_image)
                self.root.iconphoto(True, self._icon)
            except Exception:  # an icon is never worth failing for
                pass

        bar = tk.Frame(self.root, padx=8, pady=6)
        bar.pack(side=tk.TOP, fill=tk.X)
        for label, action in actions:
            tk.Button(bar, text=label, command=action, padx=10).pack(side=tk.LEFT, padx=(0, 6))
        tk.Button(bar, text="Ẩn cửa sổ", command=self.hide, padx=10).pack(side=tk.RIGHT)
        tk.Label(self.root, anchor="w", padx=10, fg="#6b7280",
                 text="Đóng cửa sổ này không tắt hub: hub vẫn chạy ở khay hệ thống. "
                      "Muốn tắt hẳn: chuột phải biểu tượng → Thoát.").pack(side=tk.BOTTOM, fill=tk.X)

        body = tk.Frame(self.root)
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        family = next((f for f in ("Cascadia Mono", "Consolas", "Menlo", "DejaVu Sans Mono")
                       if f in tkfont.families(self.root)), "Courier")
        self.text = tk.Text(body, wrap=tk.NONE, bg="#0c0c0c", fg="#d4d4d4", insertbackground="#d4d4d4",
                            font=(family, 10), borderwidth=0, padx=8, pady=6, spacing1=0, spacing3=0)
        yscroll = tk.Scrollbar(body, orient=tk.VERTICAL, command=self.text.yview)
        xscroll = tk.Scrollbar(body, orient=tk.HORIZONTAL, command=self.text.xview)
        self.text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        yscroll.pack(side=tk.RIGHT, fill=tk.Y)
        xscroll.pack(side=tk.BOTTOM, fill=tk.X)
        self.text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.text.tag_configure("warn", foreground="#fbbf24")
        self.text.tag_configure("error", foreground="#f87171")
        # Read-only, but text can still be selected and copied (Ctrl+C).
        nav = {"Up", "Down", "Left", "Right", "Prior", "Next", "Home", "End"}
        self.text.bind("<Key>", lambda e: None if (e.keysym in nav or (e.state & 0x4 and e.keysym.lower() in ("c", "a")))
                       else "break")
        self.text.bind("<Control-a>", lambda e: (self.text.tag_add("sel", "1.0", "end"), "break")[1])

        self.root.after(POLL_MS, self._poll)

    # ------------------------------------------------------------------ for other threads

    def call_soon(self, func: Callable[[], None]) -> None:
        """Run ``func`` on the Tk thread (safe to call from any thread)."""
        self._calls.put(func)

    # ------------------------------------------------------------------ Tk thread only

    def show(self) -> None:
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(200, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()
        self.text.see(tk.END)

    def hide(self) -> None:
        self.root.withdraw()

    def toggle(self) -> None:
        if self.root.state() == "withdrawn":
            self.show()
        else:
            self.hide()

    def copy_to_clipboard(self, text: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update()  # keep it on the clipboard after the call returns

    def run(self) -> None:
        self.root.mainloop()

    def quit(self) -> None:
        self.root.quit()

    def _poll(self) -> None:
        while True:
            try:
                func = self._calls.get_nowait()
            except queue.Empty:
                break
            try:
                func()
            except Exception as exc:  # a failed menu action must not stop the window
                print(f"Desktop action failed: {exc}", file=sys.stderr)
        lines, self._seen = self._buffer.since(self._seen)
        if lines:
            at_end = self.text.yview()[1] >= 0.999
            for line in lines:
                tag = ("error",) if (" ERROR " in line or " CRITICAL " in line or "error:" in line.lower()[:40]) \
                    else ("warn",) if " WARNING " in line else ()
                self.text.insert(tk.END, line + "\n", tag)
            excess = int(self.text.index("end-1c").split(".")[0]) - 5000
            if excess > 0:
                self.text.delete("1.0", f"{excess + 1}.0")
            if at_end:
                self.text.see(tk.END)
        self.root.after(POLL_MS, self._poll)
