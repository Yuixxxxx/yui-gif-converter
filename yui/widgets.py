"""Small, interruptible button highlights without layout changes or input delays."""

from __future__ import annotations

import ctypes
import os
import time

import customtkinter as ctk


def system_animations_enabled() -> bool:
    """Read the Windows reduced-motion preference; never change it."""
    if os.name == "nt":
        enabled = ctypes.c_int(1)
        if ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(enabled), 0):
            return bool(enabled.value)
    return True


class SoftButton(ctk.CTkButton):
    """Execute immediately, then fade a gentle highlight for 160 milliseconds."""

    PULSE_SECONDS = 0.16

    def __init__(self, *args, **kwargs):
        self._pulse_job = None
        self._pulse_started = 0.0
        super().__init__(*args, **kwargs)

    def _cancel_pulse(self):
        if self._pulse_job is not None:
            try:
                self.after_cancel(self._pulse_job)
            except Exception:
                pass
            self._pulse_job = None

    def _on_leave(self, event=None):
        self._cancel_pulse()
        super()._on_leave(event)

    def _on_release(self, event=None):
        if self._mouse_inside and self._state != "disabled":
            return self.invoke()

    def invoke(self):
        if self._state == "disabled":
            return None
        self._cancel_pulse()
        motion = getattr(self._root(), "_yui_motion_enabled", lambda: True)
        if motion():
            self._pulse_started = time.monotonic()
            self._pulse()
        if self._command is not None:
            return self._command()
        return None

    def _pulse(self):
        self._pulse_job = None
        if not self.winfo_exists() or self._state == "disabled":
            return
        progress = min(1.0, (time.monotonic() - self._pulse_started) / self.PULSE_SECONDS)
        base = self._hover_color if self._mouse_inside and self._hover else self._fg_color
        if base is None or base == "transparent":
            base = self._bg_color
        base = self._apply_appearance_mode(base)
        rgb = [value // 257 for value in self.winfo_rgb(base)]
        target = 255 if self._get_appearance_mode() == "dark" else 0
        amount = 0.13 * (1.0 - progress) ** 3
        color = "#" + "".join(f"{round(value + (target - value) * amount):02x}" for value in rgb)
        self._canvas.itemconfig("inner_parts", outline=color, fill=color)
        for label in (self._text_label, self._image_label):
            if label is not None:
                label.configure(bg=color)
        if progress < 1.0:
            self._pulse_job = self.after(16, self._pulse)

    def configure(self, require_redraw=False, **kwargs):
        # Never restore an old Create style after its command changes it to Cancel.
        if any(key in kwargs for key in ("state", "fg_color", "hover_color", "text_color")):
            self._cancel_pulse()
            require_redraw = True
        return super().configure(require_redraw=require_redraw, **kwargs)

    def destroy(self):
        self._cancel_pulse()
        super().destroy()
