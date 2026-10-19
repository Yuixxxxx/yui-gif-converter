"""Small themed confirmations with safe keyboard defaults and nested modality."""

from __future__ import annotations

import tkinter as tk

import customtkinter as ctk

from .widgets import SoftButton


class ConfirmationDialog(ctk.CTkToplevel):
    def __init__(self, parent, *, title, message, confirm_text="Discard changes", cancel_text="Keep editing"):
        # CTk creates its Windows caption with an update(), so capture ownership first.
        previous_grab = parent.grab_current()
        previous_focus = parent.focus_get()
        previous_status = previous_grab.grab_status() if previous_grab is not None else None
        super().__init__(parent)
        from .ui import BG, CARD_2, CARD_HOVER, BORDER, TEXT, MUTED, ACCENT, ACCENT_HOVER, ACCENT_TEXT, FONT, set_window_icon

        self.withdraw()
        self.result = False
        self._resolved = False
        self._destroying = False
        self._previous_grab, self._previous_focus = previous_grab, previous_focus
        self._previous_status = previous_status
        self._parent = parent.winfo_toplevel()
        self._jobs = []
        self._focus_action = "cancel"
        self._activated = False
        self._border, self._accent = BORDER, ACCENT
        self.configure(fg_color=BG)
        self.title(title)
        self.transient(self._parent)
        self.resizable(False, False)
        self.grid_columnconfigure(0, weight=1)
        self.title_label = ctk.CTkLabel(self, text=title, text_color=TEXT,
                                      font=ctk.CTkFont(family=FONT, size=20, weight="bold"), anchor="w")
        self.title_label.grid(row=0, column=0, sticky="ew", padx=24, pady=(22, 8))
        self.message_label = ctk.CTkLabel(self, text=message, text_color=MUTED,
                                        font=ctk.CTkFont(family=FONT, size=13),
                                        wraplength=432, justify="left", anchor="w")
        self.message_label.grid(row=1, column=0, sticky="ew", padx=24, pady=(0, 22))
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.grid(row=2, column=0, sticky="ew", padx=24, pady=(0, 22))
        actions.grid_columnconfigure((0, 1), weight=1, uniform="confirmation")
        common = dict(height=40, corner_radius=9, border_width=2,
                      font=ctk.CTkFont(family=FONT, size=13, weight="bold"))
        self.cancel_button = SoftButton(actions, text=cancel_text, command=self.cancel,
                                        fg_color=CARD_2, hover_color=CARD_HOVER,
                                        text_color=TEXT, border_color=ACCENT, **common)
        self.cancel_button.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.confirm_button = SoftButton(actions, text=confirm_text, command=self.confirm,
                                         fg_color=ACCENT, hover_color=ACCENT_HOVER,
                                         text_color=ACCENT_TEXT, border_color=BORDER, **common)
        self.confirm_button.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.bind("<Escape>", lambda event: self._key_action(False))
        self.bind("<Return>", self._activate_focused)
        self.bind("<KP_Enter>", self._activate_focused)
        self.bind("<space>", self._activate_focused)
        self.bind("<Tab>", self._cycle_focus)
        self.bind("<Shift-Tab>", self._cycle_focus)
        self.bind("<ISO_Left_Tab>", self._cycle_focus)
        for name, button in (("cancel", self.cancel_button), ("confirm", self.confirm_button)):
            button.bind("<FocusIn>", lambda event, action=name: self._focus_changed(action), add="+")
        set_window_icon(self)
        self.update_idletasks()
        self._place()
        self.deiconify()
        self._jobs.append(self.after(20, self._activate))
        self._jobs.append(self.after(250, lambda: set_window_icon(self)))

    def _place(self):
        scale = ctk.ScalingTracker.get_window_scaling(self)
        widget_scale = ctk.ScalingTracker.get_widget_scaling(self)
        screen_width, screen_height = self.winfo_screenwidth(), self.winfo_screenheight()
        width = min(480, int(screen_width * .94 / scale))
        self.message_label.configure(wraplength=max(100, width - 48))
        self.update_idletasks()
        # Widget requests are physical pixels; geometry width/height are CTk units.
        height = max(190, int(self.winfo_reqheight() / scale) + 4)
        height = min(height, int(screen_height * .9 / scale))
        physical_width, physical_height = round(width * scale), round(height * scale)
        self._parent.update_idletasks()
        x = self._parent.winfo_rootx() + (self._parent.winfo_width() - physical_width) // 2
        y = self._parent.winfo_rooty() + (self._parent.winfo_height() - physical_height) // 3
        x = max(0, min(x, screen_width - physical_width))
        y = max(0, min(y, screen_height - physical_height - round(40 * widget_scale)))
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _activate(self):
        if not self._destroying and not self._activated and self.winfo_viewable():
            self._activated = True
            self.grab_set()
            self.lift()
            self.focus_force()
            self.cancel_button.focus_set()

    def _focus_changed(self, action):
        if self._destroying:
            return
        self._focus_action = action
        self.cancel_button.configure(border_color=self._accent if action == "cancel" else self._border)
        self.confirm_button.configure(border_color=self._accent if action == "confirm" else self._border)

    def _cycle_focus(self, event=None):
        button = self.confirm_button if self._focus_action == "cancel" else self.cancel_button
        button.focus_set()
        return "break"

    def _activate_focused(self, event=None):
        return self._key_action(self._focus_action == "confirm")

    def _key_action(self, result):
        self._finish(result)
        return "break"

    def cancel(self):
        self._finish(False)

    def confirm(self):
        self._finish(True)

    def _finish(self, result):
        if not self._resolved and not self._destroying:
            self.result = bool(result)
            self._resolved = True
            self.destroy()

    def show(self):
        if not self._destroying:
            try:
                self.wait_visibility()
                self._activate()
                self.wait_window()
            except tk.TclError:
                pass  # An owner window can be destroyed while the dialog waits.
        return self.result

    def destroy(self):
        if getattr(self, "_destroying", False):
            return
        self._destroying = True
        for job in self._jobs:
            try:
                self.after_cancel(job)
            except tk.TclError:
                pass
        try:
            if self.grab_current() is self:
                self.grab_release()
        except tk.TclError:
            pass
        super().destroy()
        try:
            previous = self._previous_grab
            if previous is not None and previous.winfo_exists() and self._parent.grab_current() is None:
                previous.grab_set_global() if self._previous_status == "global" else previous.grab_set()
            focus = self._previous_focus
            if focus is not None and focus.winfo_exists():
                focus.focus_set()
        except tk.TclError:
            pass


def ask_confirmation(parent, title, message, *, confirm_text="Discard changes", cancel_text="Keep editing") -> bool:
    return ConfirmationDialog(parent, title=title, message=message,
                              confirm_text=confirm_text, cancel_text=cancel_text).show()
