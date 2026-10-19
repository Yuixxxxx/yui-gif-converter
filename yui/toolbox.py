"""Themed media tools with draft edits and cancellable local frame extraction."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog

import customtkinter as ctk
from PIL import Image, ImageTk

from . import engine, media_tools, overlays
from .dialogs import ask_confirmation
from .paths import is_local_path
from .transforms import EFFECTS, PALETTE_MODES, validate_edit_options
from .widgets import SoftButton


class Toolbox(ctk.CTkToplevel):
    def __init__(self, parent, *, items: tuple, options: engine.RenderOptions,
                 on_apply, on_split, on_close=None):
        if not items:
            raise engine.MediaError("Add media before opening the media tools.")
        previous_grab, previous_focus = parent.grab_current(), parent.focus_get()
        super().__init__(parent)
        from .ui import BG, CARD, CARD_2, BORDER, TEXT, MUTED, ACCENT, ACCENT_HOVER, ACCENT_TEXT, FONT, set_window_icon
        self.theme = dict(bg=BG, card=CARD, card2=CARD_2, border=BORDER, text=TEXT, muted=MUTED,
                          accent=ACCENT, hover=ACCENT_HOVER, accent_text=ACCENT_TEXT, font=FONT)
        self.configure(fg_color=BG)
        self.title("Media tools · Yui")
        self.transient(parent)
        # Tk reports screen pixels, while CTk accepts logical geometry units.
        # Applying the physical screen size twice pushes the footer off-screen
        # at 150/200% display scaling.
        scale = ctk.ScalingTracker.get_window_scaling(self)
        screen_width, screen_height = self.winfo_screenwidth(), self.winfo_screenheight()
        width = max(1, min(1040, int(max(1, screen_width - 60) / scale)))
        height = max(1, min(780, int(max(1, screen_height - 100) / scale)))
        self._initial_logical_size = (width, height)
        self._sidebar_width = min(340, max(280, int(width * .43)))
        self.geometry(f"{width}x{height}+{max(0, (screen_width-round(width*scale))//2)}+{max(0, (screen_height-round(height*scale))//2)}")
        self.minsize(min(880, width), min(600, height))
        set_window_icon(self)
        self.items = tuple(replace(item) for item in items)
        self.options = validate_edit_options(replace(options))
        self._initial = self._key(self.options)
        self._on_apply, self._on_split, self._on_close = on_apply, on_split, on_close
        self._previous_grab, self._previous_focus = previous_grab, previous_focus
        self._closed = self._exporting = self._confirming = False
        self._cancel = threading.Event()
        self._preview_cancel = threading.Event()
        self._export_cancel = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="yui-tools")
        self._events = queue.Queue()
        self._holders = []
        self._lock = threading.Lock()
        self._jobs = {}
        self._generation = 0
        self._rendered_key = None
        self._image = self._photo = None
        self._selected = 0
        self._offset = 0.0
        self._updating = False
        self._build()
        self._source_changed()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda event: self.close())
        self._schedule("poll", 30, self._poll)
        self._schedule("modal", 80, self._activate_modal)

    @staticmethod
    def _key(opts):
        return (opts.crop, opts.effect, opts.reverse, opts.palette_colors, opts.palette_mode)

    def _font(self, size=13, weight="normal"):
        return ctk.CTkFont(family=self.theme["font"], size=size, weight=weight)

    def _label(self, parent, text, **kwargs):
        return ctk.CTkLabel(parent, text=text, font=kwargs.pop("font", self._font()),
                            text_color=kwargs.pop("text_color", self.theme["text"]), **kwargs)

    def _button(self, parent, text, command, *, primary=False, **kwargs):
        return SoftButton(parent, text=text, command=command, height=36, corner_radius=8,
                          font=self._font(12, "bold"), fg_color=self.theme["accent"] if primary else self.theme["card2"],
                          hover_color=self.theme["hover"] if primary else self.theme["border"],
                          text_color=self.theme["accent_text"] if primary else self.theme["text"], **kwargs)

    def _entry(self, parent, variable, **kwargs):
        return ctk.CTkEntry(parent, textvariable=variable, height=32, font=self._font(12),
                           fg_color=self.theme["bg"], border_color=self.theme["border"], text_color=self.theme["text"], **kwargs)

    def _menu(self, parent, values, variable, command):
        return ctk.CTkOptionMenu(parent, values=list(values), variable=variable, command=command,
                                height=34, font=self._font(12), fg_color=self.theme["card2"],
                                button_color=self.theme["border"], button_hover_color=self.theme["hover"],
                                text_color=self.theme["text"], dropdown_fg_color=self.theme["card2"],
                                dropdown_text_color=self.theme["text"], dropdown_hover_color=self.theme["border"])

    def _build(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, columnspan=2, sticky="ew", padx=20, pady=(16, 12))
        header.grid_columnconfigure(0, weight=1)
        self._label(header, "Media tools", font=self._font(22, "bold")).grid(row=0, column=0, sticky="w")
        self._label(header, "Preview a clip, refine the GIF, or save individual frames.", text_color=self.theme["muted"],
                    wraplength=max(120, self._initial_logical_size[0] - 40), justify="left").grid(row=1, column=0, sticky="w")

        preview = ctk.CTkFrame(self, fg_color=self.theme["card"], corner_radius=14, border_width=1, border_color=self.theme["border"])
        preview.grid(row=1, column=0, sticky="nsew", padx=(20, 10), pady=(0, 10))
        self.preview_panel = preview
        preview.grid_columnconfigure(0, weight=1)
        preview.grid_rowconfigure(2, weight=1)
        self._label(preview, "Source clip", anchor="w").grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 4))
        self.source_names = [f"{index+1}. {engine.short_name(item.path.name, 48)}" for index, item in enumerate(self.items)]
        self.source_var = tk.StringVar(value=self.source_names[0])
        self.source_menu = self._menu(preview, self.source_names, self.source_var, self._source_changed)
        self.source_menu.configure(width=1, dynamic_resizing=False)
        self.source_menu.grid(row=1, column=0, sticky="ew", padx=14)
        self.canvas = tk.Canvas(preview, width=1, height=1, background=self._apply_appearance_mode(self.theme["card"]), borderwidth=0, highlightthickness=0)
        self.canvas.grid(row=2, column=0, sticky="nsew", padx=10, pady=10)
        self.canvas.bind("<Configure>", lambda event: self._schedule("display", 50, self._display))
        self.frame_label = self._label(preview, "Preview: 0.0s", text_color=self.theme["muted"], font=self._font(12))
        self.frame_label.grid(row=3, column=0, sticky="w", padx=14)
        self.frame_slider = ctk.CTkSlider(preview, from_=0, to=1, height=18, command=self._change_frame)
        self.frame_slider.grid(row=4, column=0, sticky="ew", padx=14, pady=(6, 14))

        controls = ctk.CTkScrollableFrame(self, width=self._sidebar_width, fg_color=self.theme["card"], corner_radius=14,
                                          border_width=1, border_color=self.theme["border"])
        self.controls = controls
        controls.grid(row=1, column=1, sticky="nsew", padx=(0, 20), pady=(0, 10))
        controls.grid_columnconfigure(0, weight=1)
        self._label(controls, "Crop", font=self._font(16, "bold")).grid(row=0, column=0, sticky="w", padx=10, pady=(10, 2))
        self._label(controls, "Percent of each source; values must stay inside it.", font=self._font(11), text_color=self.theme["muted"]).grid(row=1, column=0, sticky="w", padx=10)
        crop = ctk.CTkFrame(controls, fg_color="transparent")
        crop.grid(row=2, column=0, sticky="ew", padx=10, pady=6)
        crop.grid_columnconfigure((0, 1, 2, 3), weight=1, uniform="crop")
        values = self.options.crop or (0, 0, 1, 1)
        self.crop_vars = []
        for column, (name, value) in enumerate(zip(("Left %", "Top %", "Width %", "Height %"), values)):
            self._label(crop, name, font=self._font(11)).grid(row=0, column=column, sticky="w", padx=2)
            variable = tk.StringVar(value=f"{value*100:g}")
            self.crop_vars.append(variable)
            self._entry(crop, variable, width=64).grid(row=1, column=column, sticky="ew", padx=2)
            variable.trace_add("write", self._draft_changed)
        self.reset_button = self._button(controls, "Reset crop", self.reset_crop)
        self.reset_button.grid(row=3, column=0, sticky="ew", padx=10, pady=(0, 12))
        self._label(controls, "Colour & playback", font=self._font(16, "bold")).grid(row=4, column=0, sticky="w", padx=10, pady=(4, 8))
        self.effect_var = tk.StringVar(value=EFFECTS[self.options.effect])
        self.effect_menu = self._menu(controls, EFFECTS.values(), self.effect_var, self._draft_changed)
        self.effect_menu.grid(row=5, column=0, sticky="ew", padx=10)
        self.reverse_var = tk.BooleanVar(value=self.options.reverse)
        self.reverse_check = ctk.CTkCheckBox(controls, text="Reverse playback", variable=self.reverse_var, command=self._draft_changed,
                                            font=self._font(12), text_color=self.theme["text"], fg_color=self.theme["accent"], hover_color=self.theme["hover"])
        self.reverse_check.grid(row=6, column=0, sticky="w", padx=12, pady=12)
        palette = ctk.CTkFrame(controls, fg_color="transparent")
        palette.grid(row=7, column=0, sticky="ew", padx=10)
        palette.grid_columnconfigure((0, 1), weight=1, uniform="palette")
        self._label(palette, "GIF colours", font=self._font(12)).grid(row=0, column=0, sticky="w")
        self._label(palette, "Palette analysis", font=self._font(12)).grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.colors_var = tk.StringVar(value=str(self.options.palette_colors))
        color_values = list(dict.fromkeys(["16", "32", "64", "128", "256", str(self.options.palette_colors)]))
        self.colors_menu = self._menu(palette, color_values, self.colors_var, self._draft_changed)
        self.colors_menu.grid(row=1, column=0, sticky="ew")
        self.palette_var = tk.StringVar(value=PALETTE_MODES[self.options.palette_mode])
        self.palette_menu = self._menu(palette, PALETTE_MODES.values(), self.palette_var, self._draft_changed)
        self.palette_menu.grid(row=1, column=1, sticky="ew", padx=(8, 0))
        self._label(controls, "Palette choices are applied when creating the GIF.", font=self._font(11), text_color=self.theme["muted"]).grid(row=8, column=0, sticky="w", padx=10, pady=(4, 14))

        self._label(controls, "Split selected clip", font=self._font(16, "bold")).grid(row=9, column=0, sticky="w", padx=10)
        self._label(controls, "Time after this clip's trim start (seconds).", font=self._font(11), text_color=self.theme["muted"]).grid(row=10, column=0, sticky="w", padx=10)
        self.split_var = tk.StringVar(value="1")
        self.split_entry = self._entry(controls, self.split_var)
        self.split_entry.grid(row=11, column=0, sticky="ew", padx=10, pady=(6, 4))
        self.split_button = self._button(controls, "Split clip & apply edits", self.split)
        self.split_button.grid(row=12, column=0, sticky="ew", padx=10, pady=(0, 16))

        self._label(controls, "Extract PNG frames", font=self._font(16, "bold")).grid(row=13, column=0, sticky="w", padx=10)
        self._label(controls, "Source times; crop, effect and watermark included.\nReverse changes frame order. Up to 300 frames.", font=self._font(11), text_color=self.theme["muted"], justify="left").grid(row=14, column=0, sticky="w", padx=10, pady=(2, 6))
        extraction = ctk.CTkFrame(controls, fg_color="transparent")
        extraction.grid(row=15, column=0, sticky="ew", padx=10)
        extraction.grid_columnconfigure((0, 1, 2), weight=1, uniform="extract")
        self.start_var, self.end_var, self.fps_var = tk.StringVar(value="0"), tk.StringVar(value="1"), tk.StringVar(value="5")
        for column, (label, variable) in enumerate((("Start s", self.start_var), ("End s", self.end_var), ("FPS", self.fps_var))):
            self._label(extraction, label, font=self._font(11)).grid(row=0, column=column, sticky="w", padx=2)
            self._entry(extraction, variable, width=78).grid(row=1, column=column, sticky="ew", padx=2)
            variable.trace_add("write", self._update_extract_count)
        self.size_var = tk.StringVar(value="640 px")
        self.size_menu = self._menu(controls, ("320 px", "640 px", "960 px", "1280 px", "1920 px"), self.size_var, lambda value: None)
        self.size_menu.grid(row=16, column=0, sticky="ew", padx=10, pady=8)
        self.count_label = self._label(controls, "", font=self._font(11), text_color=self.theme["muted"])
        self.count_label.grid(row=17, column=0, sticky="w", padx=10)
        self.extract_button = self._button(controls, "Save frames ZIP…", self.extract)
        self.extract_button.grid(row=18, column=0, sticky="ew", padx=10, pady=(6, 4))
        self.cancel_export_button = self._button(controls, "Cancel extraction", self.cancel_extraction, state="disabled")
        self.cancel_export_button.grid(row=19, column=0, sticky="ew", padx=10, pady=(0, 10))

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=2, column=0, columnspan=2, sticky="ew", padx=20, pady=(0, 14))
        footer.grid_columnconfigure(0, weight=1)
        self.footer = footer
        status_width = min(570, max(80, self._initial_logical_size[0] - 284))
        self.status = self._label(footer, "Loading preview…", font=self._font(12), text_color=self.theme["muted"], wraplength=status_width, justify="left", anchor="w")
        self.status.grid(row=0, column=0, sticky="ew", padx=(0, 12))
        self.close_button = self._button(footer, "Close", self.close, width=80)
        self.close_button.grid(row=0, column=1, padx=(0, 8))
        self.apply_button = self._button(footer, "Apply to GIF", self.apply, primary=True, width=124, state="disabled")
        self.apply_button.grid(row=0, column=2)

    def _activate_modal(self):
        if not self._closed and self.winfo_viewable() and self.grab_current() in (None, self, self._previous_grab):
            self.grab_set()
            self.focus_set()

    def _schedule(self, name, delay, callback):
        old = self._jobs.pop(name, None)
        if old is not None:
            try:
                self.after_cancel(old)
            except tk.TclError:
                pass
        if not self._closed:
            self._jobs[name] = self.after(delay, lambda: (self._jobs.pop(name, None), callback()))

    def _read_options(self):
        try:
            crop = tuple(float(variable.get()) / 100 for variable in self.crop_vars)
            return validate_edit_options(replace(self.options, crop=crop,
                effect=next(key for key, value in EFFECTS.items() if value == self.effect_var.get()),
                reverse=bool(self.reverse_var.get()), palette_colors=int(self.colors_var.get()),
                palette_mode=next(key for key, value in PALETTE_MODES.items() if value == self.palette_var.get())))
        except (ValueError, StopIteration, OverflowError):
            raise engine.MediaError("Use valid crop percentages and one of the available palette choices.") from None

    def _draft_changed(self, *args):
        if self._updating or self._closed:
            return
        self._rendered_key = None
        self.apply_button.configure(state="disabled")
        self._schedule("preview", 200, self._load_preview)

    def reset_crop(self):
        self._updating = True
        for variable, value in zip(self.crop_vars, ("0", "0", "100", "100")):
            variable.set(value)
        self._updating = False
        self._draft_changed()

    def _source_changed(self, value=None):
        self._selected = self.source_names.index(self.source_var.get())
        item = self.items[self._selected]
        self._offset = 0
        self.frame_slider.configure(to=max(.01, item.clip_seconds - .01), state="normal" if item.is_video else "disabled")
        self.frame_slider.set(0)
        self.frame_label.configure(text="Preview: 0.0s" if item.is_video else "Preview: selected image")
        self.split_var.set(f"{item.clip_seconds / 2:.3f}")
        self.start_var.set("0")
        self.end_var.set(f"{item.clip_seconds:.6f}".rstrip("0").rstrip("."))
        self.split_button.configure(state="normal" if item.is_video and item.clip_seconds >= .2 else "disabled")
        self._update_extract_count()
        self._draft_changed()

    def _change_frame(self, value):
        self._offset = float(value)
        self.frame_label.configure(text=f"Preview: {self._offset:.2f}s")
        self._draft_changed()

    def _load_preview(self):
        if self._closed:
            return
        try:
            opts = self._read_options()
            item = self.items[self._selected]
            size = media_tools.frame_canvas(item, opts, 640)
        except Exception as exc:
            self.status.configure(text=str(exc))
            self.apply_button.configure(state="disabled")
            return
        self._generation += 1
        generation, index, offset = self._generation, self._selected, self._offset
        self._preview_cancel.set()
        self._preview_cancel = threading.Event()
        self.status.configure(text="Loading preview…")
        self._worker("preview", generation, self._preview_cancel, lambda cancelled, holder: self._preview(item, opts, size, offset, cancelled, holder),
                     extra=(index, offset, self._key(opts)))

    @staticmethod
    def _preview(item, opts, size, offset, cancelled, holder):
        image = overlays.preview_frame(item, opts, size, offset=offset, should_cancel=cancelled, proc_holder=holder).convert("RGBA")
        if opts.overlays:
            image.alpha_composite(overlays.compose_overlay(opts.overlays, size, should_cancel=cancelled))
        return image.convert("RGB")

    def _display(self):
        if self._closed or self._image is None:
            return
        width, height = max(1, self.canvas.winfo_width() - 16), max(1, self.canvas.winfo_height() - 16)
        image = self._image.copy()
        image.thumbnail((width, height), Image.Resampling.LANCZOS)
        self._photo = ImageTk.PhotoImage(image, master=self)
        self.canvas.delete("all")
        self.canvas.create_image(self.canvas.winfo_width()/2, self.canvas.winfo_height()/2, image=self._photo)

    def _worker(self, kind, generation, event, function, *, extra=None):
        holder = {}
        with self._lock:
            self._holders.append(holder)

        def run():
            cancelled = lambda: self._cancel.is_set() or event.is_set()
            try:
                if cancelled():
                    raise engine.RenderCancelled()
                result = function(cancelled, holder)
                self._events.put((kind, generation, result, None, extra))
            except Exception as exc:
                self._events.put((kind, generation, None, exc, extra))
            finally:
                with self._lock:
                    self._holders.remove(holder)
        self._pool.submit(run)

    def _poll(self):
        if self._closed:
            return
        while True:
            try:
                kind, generation, result, error, extra = self._events.get_nowait()
            except queue.Empty:
                break
            if kind == "progress":
                if self._exporting:
                    self.status.configure(text=f"{round(result*100)}% · {extra}")
            elif kind == "preview":
                try:
                    current_key = self._key(self._read_options())
                except engine.MediaError:
                    continue
                if generation != self._generation or extra != (self._selected, self._offset, current_key):
                    continue
                if error is None:
                    self._image, self._rendered_key = result, current_key
                    self._display()
                    if not self._exporting:
                        self.status.configure(text="Preview ready. Apply edits to use them in the GIF.")
                        self.apply_button.configure(state="normal")
                elif not isinstance(error, engine.RenderCancelled):
                    self.status.configure(text=f"Preview unavailable: {error}")
            elif kind == "extract":
                self._exporting = False
                self.extract_button.configure(state="normal")
                self.cancel_export_button.configure(state="disabled")
                self.apply_button.configure(state="normal" if self._rendered_key is not None else "disabled")
                if error is None:
                    self.status.configure(text=f"Saved frames: {Path(result).name}")
                elif isinstance(error, engine.RenderCancelled):
                    self.status.configure(text="Extraction cancelled. No partial archive was saved.")
                else:
                    self.status.configure(text=f"Could not extract frames: {error}")
        self._schedule("poll", 40, self._poll)

    def _range(self):
        start, end, fps = (engine.parse_time(value.get()) for value in (self.start_var, self.end_var, self.fps_var))
        if any(value is None for value in (start, end, fps)):
            raise engine.MediaError("Enter a valid start, end and FPS.")
        return media_tools.frame_range(self.items[self._selected], start=start, end=end, fps=fps)

    def _update_extract_count(self, *args):
        if not hasattr(self, "count_label") or self._closed:
            return
        try:
            first, last, fps, count = self._range()
            self.count_label.configure(text=f"{count} PNG frames · {last-first:g}s selected")
        except engine.MediaError as exc:
            self.count_label.configure(text=str(exc))

    def split(self):
        if self._exporting or self._closed:
            return
        try:
            at = engine.parse_time(self.split_var.get())
            if at is None:
                raise engine.MediaError("Enter a valid split time in seconds.")
            parts = media_tools.split_item(self.items[self._selected], at)
            opts = self._read_options()
            self._on_split(self._selected, parts)
            self._on_apply(opts)
        except Exception as exc:
            self.status.configure(text=f"Could not split this clip: {exc}")
            return
        self.close(force=True)

    def extract(self, output=None):
        if self._exporting or self._closed:
            return
        try:
            first, last, fps, count = self._range()
            opts = self._read_options()
            max_side = int(self.size_var.get().split()[0])
            if output is None:
                output = filedialog.asksaveasfilename(parent=self, title="Save PNG frames ZIP", defaultextension=".zip",
                    initialfile="yui-frames.zip", filetypes=[("PNG frame archive", "*.zip")], confirmoverwrite=False)
                if not output:
                    return
            output = Path(output)
            if not is_local_path(output) or output.suffix.lower() != ".zip":
                raise engine.MediaError("Choose a ZIP destination on a local drive.")
        except Exception as exc:
            self.status.configure(text=str(exc))
            return
        self._exporting = True
        self._export_cancel = threading.Event()
        self.extract_button.configure(state="disabled")
        self.cancel_export_button.configure(state="normal")
        self.apply_button.configure(state="disabled")
        self.status.configure(text=f"Extracting {count} frames… Existing archives will be kept.")
        item = self.items[self._selected]
        protected = tuple(item.path for item in self.items)
        self._worker("extract", 0, self._export_cancel, lambda cancelled, holder: media_tools.extract_frames(
            item, opts, output, start=first, end=last, fps=fps, max_side=max_side,
            should_cancel=cancelled, proc_holder=holder, protected_paths=protected,
            progress=lambda value, message: self._events.put(("progress", 0, value, None, message))))

    def cancel_extraction(self):
        if self._exporting:
            self._export_cancel.set()
            self.cancel_export_button.configure(state="disabled")
            self.status.configure(text="Cancelling extraction…")

    def apply(self):
        if self._closed or self._exporting:
            return
        try:
            opts = self._read_options()
            if self._rendered_key != self._key(opts):
                self.status.configure(text="Wait for the current preview before applying edits.")
                return
            self._on_apply(opts)
        except Exception as exc:
            self.status.configure(text=f"Could not apply edits: {exc}")
            return
        self._initial = self._key(opts)
        self.close(force=True)

    def _confirm(self, title, message, **kwargs):
        self._confirming = True
        try:
            return ask_confirmation(self, title, message, **kwargs)
        finally:
            self._confirming = False

    def close(self, force=False):
        if self._closed:
            return True
        if not force:
            if self._confirming:
                return False
            if self._exporting and not self._confirm("Cancel frame extraction?", "Frames are still being extracted. Cancel extraction and close media tools?", confirm_text="Cancel extraction", cancel_text="Keep saving"):
                return False
            try:
                dirty = self._key(self._read_options()) != self._initial
            except engine.MediaError:
                dirty = True
            if dirty and not self._confirm("Discard media edits?", "These crop, effect and palette edits have not been applied to the GIF. Discard them?", confirm_text="Discard edits", cancel_text="Keep editing"):
                return False
        self._closed = True
        self._cancel.set()
        self._preview_cancel.set()
        self._export_cancel.set()
        for job in tuple(self._jobs.values()):
            try:
                self.after_cancel(job)
            except tk.TclError:
                pass
        self._jobs.clear()
        with self._lock:
            holders = list(self._holders)
        for holder in holders:
            process = holder.get("proc")
            if process and process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
        self._pool.shutdown(wait=False, cancel_futures=True)
        try:
            if self.grab_current() is self:
                self.grab_release()
            if self._previous_grab is not None and self._previous_grab.winfo_exists():
                self._previous_grab.grab_set()
            if self._previous_focus is not None and self._previous_focus.winfo_exists():
                self._previous_focus.focus_set()
        except tk.TclError:
            pass
        self.destroy()
        if self._on_close is not None:
            self._on_close()
        return True
