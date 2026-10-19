"""Check themed confirmations using real Tk windows and isolated local state."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
import traceback


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main():
    work = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "work" / "dialogs-check").resolve()
    work.mkdir(parents=True, exist_ok=True)
    # Set these before importing ui/settings: no real user profile is touched.
    os.environ["APPDATA"] = str(work / "profile")
    os.environ["LOCALAPPDATA"] = str(work / "local-profile")

    import customtkinter as ctk
    import tkinter as tk
    from yui.dialogs import ConfirmationDialog, ask_confirmation
    from yui.ui import ACCENT, BG, TEXT, MUTED

    checks, callback_errors = [], []
    active = []
    root = None
    report = {"ok": False, "count": 0, "checks": checks,
              "inspection": "Real off-screen Tk windows; dark/light colors, 100/150/200% layout, modal keyboard actions and teardown"}

    def check(condition, name):
        if not condition:
            raise AssertionError(name)
        checks.append(name)

    def descendants(widget):
        for child in widget.winfo_children():
            yield child
            yield from descendants(child)

    def belongs(focus, button):
        return focus is not None and (focus == button or str(focus).startswith(str(button) + "."))

    def spin(seconds=.16):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            root.update()
            time.sleep(.003)

    def create(owner=None, **kwargs):
        dialog = ConfirmationDialog(owner or root,
                                    title=kwargs.pop("title", "Discard design changes?"),
                                    message=kwargs.pop("message", "Close without applying your design changes to the GIF?"),
                                    **kwargs)
        active.append(dialog)
        # Retain the implementation's requested/scaled size while keeping all
        # synthetic windows away from user windows and accounts.
        dialog.geometry("+12000+12000")
        return dialog

    def modal(dialog, action, expected, name):
        errors = []

        def invoke():
            try:
                action(dialog)
            except Exception as exc:
                errors.append(exc)
                dialog.cancel()

        token = root.after(240, invoke)
        def timed_out():
            errors.append(AssertionError(name + ": modal action did not dismiss the dialog"))
            dialog.cancel()

        fallback = root.after(4000, timed_out)
        try:
            result = dialog.show()
        finally:
            for job in (token, fallback):
                try:
                    root.after_cancel(job)
                except tk.TclError:
                    pass
        if errors:
            raise errors[0]
        check(result is expected and dialog.result is expected, name)
        check(not dialog.winfo_exists(), name + ": dialog is destroyed")
        return result

    try:
        ctk.set_appearance_mode("dark")
        ctk.set_widget_scaling(1)
        ctk.set_window_scaling(1)
        root = ctk.CTk()
        root.title("Yui themed-dialog synthetic test")
        root.geometry("1100x760+12000+12000")
        root.report_callback_exception = lambda *args: callback_errors.append(str(args[1]))
        spin()
        root.focus_force()
        root.update()

        for theme, index in (("dark", 1), ("light", 0)):
            ctk.set_appearance_mode(theme)
            for scale in (1, 1.5, 2):
                ctk.set_widget_scaling(scale)
                ctk.set_window_scaling(scale)
                dialog = create()
                prefix = f"{theme} {scale:g}x"
                check(dialog.result is False, prefix + ": unopened result safely defaults to cancel")

                def inspect_and_cancel(win, prefix=prefix, index=index):
                    check(win.winfo_ismapped(), prefix + ": native confirmation is mapped")
                    check(win.cget("bg").lower() == BG[index].lower(), prefix + ": actual window uses theme background")
                    labels = [widget for widget in descendants(win) if isinstance(widget, ctk.CTkLabel)]
                    message_labels = [widget for widget in labels if widget.cget("text") ==
                                      "Close without applying your design changes to the GIF?"]
                    title_labels = [widget for widget in labels if widget.cget("text") == "Discard design changes?"]
                    check(bool(message_labels), prefix + ": message remains readable")
                    for label in message_labels:
                        check(label._label.cget("fg").lower() == MUTED[index].lower(),
                              prefix + ": message uses theme secondary text")
                    for label in title_labels:
                        check(label._label.cget("fg").lower() == TEXT[index].lower(),
                              prefix + ": title uses theme text")
                    check(win.confirm_button._apply_appearance_mode(win.confirm_button.cget("fg_color")).lower()
                          == ACCENT[index].lower(), prefix + ": confirmation action uses theme accent")
                    check(win.cancel_button.cget("text") == "Keep editing"
                          and win.confirm_button.cget("text") == "Discard changes",
                          prefix + ": action labels explain the decision")
                    width, height = win.winfo_width(), win.winfo_height()
                    for label, widget in (("cancel", win.cancel_button), ("confirm", win.confirm_button)):
                        x = widget.winfo_rootx() - win.winfo_rootx()
                        y = widget.winfo_rooty() - win.winfo_rooty()
                        check(widget.winfo_ismapped() and x >= 0 and y >= 0
                              and x + widget.winfo_width() <= width
                              and y + widget.winfo_height() <= height,
                              prefix + f": {label} action fits the native dialog")
                        check(widget._text_label.winfo_width() >= widget._text_label.winfo_reqwidth(),
                              prefix + f": {label} action text is not clipped")
                    for widget in message_labels + title_labels:
                        x = widget.winfo_rootx() - win.winfo_rootx()
                        y = widget.winfo_rooty() - win.winfo_rooty()
                        check(x >= 0 and y >= 0 and x + widget.winfo_width() <= width
                              and y + widget.winfo_height() <= height,
                              prefix + ": readable labels fit the scaled dialog")
                    check(width <= win.winfo_screenwidth() and height <= win.winfo_screenheight(),
                          prefix + ": scaled dialog fits the display")
                    check(win.grab_current() == win, prefix + ": local modal grab belongs to this dialog")
                    check(belongs(win.focus_get(), win.cancel_button) or
                          (win.focus_get() == win and win._focus_action == "cancel"),
                          prefix + f": safe action receives initial focus (actual {win.focus_get()})")
                    win.cancel_button.invoke()

                modal(dialog, inspect_and_cancel, False, prefix + ": cancel button retains draft")

        ctk.set_appearance_mode("dark")
        ctk.set_widget_scaling(1)
        ctk.set_window_scaling(1)
        modal(create(), lambda win: win.confirm_button.invoke(), True, "explicit confirm button discards draft")
        modal(create(), lambda win: win.event_generate("<Return>"),
              False, "Return on the default action keeps editing")

        def escape(win):
            win.confirm_button.focus_force()
            root.update()
            win.confirm_button._text_label.event_generate("<Escape>")

        modal(create(), escape, False, "Escape cancels even while the destructive action is focused")
        modal(create(), lambda win: win.tk.call(win.protocol("WM_DELETE_WINDOW")), 
              False, "native title-bar close retains draft")

        def tab_then_cancel(win):
            win.cancel_button.focus_force()
            root.update()
            win.cancel_button._text_label.event_generate("<Tab>")
            root.update()
            check(belongs(win.focus_get(), win.confirm_button), "Tab reaches confirm action")
            win.confirm_button._text_label.event_generate("<Shift-Tab>")
            root.update()
            check(belongs(win.focus_get(), win.cancel_button), "Shift+Tab returns to safe action")
            win.cancel()

        modal(create(), tab_then_cancel, False, "keyboard navigation preserves explicit choice")

        owner = ctk.CTkToplevel(root)
        owner.title("Synthetic designer owner")
        owner.geometry("640x400+12000+12000")
        owner_button = ctk.CTkButton(owner, text="Synthetic owner action")
        owner_button.pack(padx=30, pady=30)
        spin()
        owner.grab_set()
        owner_button.focus_force()
        spin(.06)
        check(belongs(owner.focus_get(), owner_button), "synthetic owner has a focused action before nesting")
        modal(create(owner), lambda win: win.cancel(), False, "nested confirmation can cancel")
        spin(.06)
        check(owner.grab_current() == owner, "cancel restores the editor's prior modal grab")
        check(belongs(owner.focus_get(), owner_button), "cancel restores the editor's prior focus")
        modal(create(owner), lambda win: win.confirm(), True, "nested confirmation can confirm")
        check(owner.grab_current() == owner, "confirm restores the editor's prior modal grab")
        owner.grab_release()

        orphan = create(owner)
        modal(orphan, lambda win: owner.destroy(), False, "owner destruction safely cancels waiting confirmation")
        orphan.cancel()
        orphan.cancel()
        orphan.confirm()
        check(orphan.result is False, "repeated cleanup cannot turn an owner-destroyed result into confirmation")

        def helper_action():
            dialogs = [widget for widget in root.winfo_children() if isinstance(widget, ConfirmationDialog)]
            check(len(dialogs) == 1, "helper opens exactly one themed confirmation")
            check(dialogs[0].confirm_button.cget("text") == "Continue"
                  and dialogs[0].cancel_button.cget("text") == "Stay here",
                  "helper preserves supplied action labels")
            dialogs[0].confirm()

        token = root.after(240, helper_action)
        fallback = root.after(4000, lambda: [win.cancel() for win in root.winfo_children()
                                          if isinstance(win, ConfirmationDialog)])
        try:
            result = ask_confirmation(root, "Yui confirmation", "Synthetic local-only action?",
                                      confirm_text="Continue", cancel_text="Stay here")
        finally:
            root.after_cancel(token)
            root.after_cancel(fallback)
        check(result is True, "helper returns the native dialog's explicit confirmation")
        check(not callback_errors, "theme, scaling and modal teardown produce no Tk callback errors")
        report["ok"] = True
    except Exception as exc:
        report["error"] = str(exc)
        report["traceback"] = traceback.format_exc()
    finally:
        for dialog in active:
            try:
                dialog.cancel()
            except (tk.TclError, RuntimeError):
                pass
        if root is not None:
            try:
                root.destroy()
            except tk.TclError:
                pass
        ctk.set_widget_scaling(1)
        ctk.set_window_scaling(1)
        report["count"] = len(checks)
        report["callback_errors"] = callback_errors
        (work / "dialogs-test.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("ok", "count", "inspection")}))
    if not report["ok"]:
        print(report.get("traceback", report.get("error", "Confirmation checks failed")), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
