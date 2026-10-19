"""Local text, sticker and watermark designer with a PNG poster export."""

from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from tkinter import colorchooser, filedialog
from typing import Callable

import customtkinter as ctk
from PIL import Image, ImageTk

from . import engine, overlays
from .paths import is_local_path
from .widgets import SoftButton
from .dialogs import ask_confirmation

FONT_CHOICES = ("Segoe UI", "Arial", "Georgia", "Times New Roman", "Consolas")
MAX_LAYERS = 20
MAX_TEXT = 2000
ANCHOR_CHOICES = ("Top left", "Top right", "Center", "Bottom left", "Bottom right")


class Designer(ctk.CTkToplevel):
    """Draft editor: only Apply changes the application's GIF design."""

    def __init__(self, parent, *, source: engine.MediaItem, options: engine.RenderOptions,
                 canvas_size: tuple[int, int], layers: tuple = (), on_apply: Callable,
                 on_close: Callable | None = None, protected_paths: tuple = ()):
        super().__init__(parent)
        # Import at runtime; App opens the editor lazily so neither module cycles.
        from .ui import BG, CARD, CARD_2, BORDER, TEXT, MUTED, ACCENT, ACCENT_HOVER, ACCENT_TEXT, FONT, set_window_icon
        self.theme = dict(bg=BG, card=CARD, card2=CARD_2, border=BORDER, text=TEXT,
                          muted=MUTED, accent=ACCENT, hover=ACCENT_HOVER,
                          accent_text=ACCENT_TEXT, font=FONT)
        self.configure(fg_color=BG)
        self.transient(parent)
        self.title("Design GIF · Yui")
        screen_width, screen_height = self.winfo_screenwidth(), self.winfo_screenheight()
        # Tk reports the screen in physical pixels; CTk scales geometry sizes
        # from logical units, while leaving screen coordinates unscaled.
        window_scale = ctk.ScalingTracker.get_window_scaling(self)
        width = max(1, min(1060, int((screen_width - 60) / window_scale)))
        height = max(1, min(760, int((screen_height - 100) / window_scale)))
        left = max(0, (screen_width - round(width * window_scale)) // 2)
        top = max(0, (screen_height - round(height * window_scale)) // 2)
        self.geometry(f"{width}x{height}+{left}+{top}")
        self.minsize(min(940, width), min(620, height))
        set_window_icon(self)
        self.source = replace(source)
        self.options = replace(options)
        self.canvas_size = tuple(map(int, canvas_size))
        if min(self.canvas_size) <= 0:
            self.destroy()
            raise engine.MediaError("The design canvas needs a positive width and height.")
        scale = min(1.0, 800 / max(self.canvas_size))
        self.preview_size = tuple(max(1, round(side * scale)) for side in self.canvas_size)
        self.layers = tuple(overlays.validate_layers(layers))
        self._initial = self.layers
        self._selected = len(self.layers) - 1 if self.layers else None
        self._on_apply_callback, self._on_close_callback = on_apply, on_close
        self.protected_paths = tuple(protected_paths) + (self.source.path,)
        self._undo: list[tuple[tuple, int | None]] = []
        self._redo: list[tuple[tuple, int | None]] = []
        self._last_edit = (None, 0.0)
        self._closed = False
        self._confirmation_open = False
        self._cancel = threading.Event()
        self._frame_cancel, self._render_cancel = threading.Event(), threading.Event()
        self._frame_generation = self._render_generation = 0
        self._frame_image = None
        self._rendered = None
        self._rendered_layers = None
        self._image_tk = None
        self._bounds: list[tuple] = []
        self._view = (0.0, 0.0, 1.0, 1.0)
        self._drag = None
        self._offset = 0.0
        self._loading = True
        self._exporting = False
        self._pending_stickers = 0
        self._updating = False
        self._events: queue.Queue = queue.Queue()
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="yui-design")
        self._holders: list[dict] = []
        self._holder_lock = threading.Lock()
        self._jobs: dict[str, str] = {}
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda event: self.close())
        self.bind("<Control-z>", self._undo_key)
        self.bind("<Control-y>", self._redo_key)
        self.bind("<Control-Shift-Z>", self._redo_key)
        self._refresh_layers()
        self._schedule("poll", 30, self._poll)
        self._schedule("frame", 0, self._load_frame)
        self._schedule("modal", 80, self._activate_modal)

    def _activate_modal(self):
        if not self._closed and self.winfo_viewable():
            if self.grab_current() in (None, self):
                self.grab_set()
                self.focus_set()

    def _font(self, size=13, weight="normal"):
        return ctk.CTkFont(family=self.theme["font"], size=size, weight=weight)

    def _label(self, parent, text, **kwargs):
        kwargs.setdefault("font", self._font())
        kwargs.setdefault("text_color", self.theme["text"])
        return ctk.CTkLabel(parent, text=text, **kwargs)

    def _button(self, parent, text, command, *, primary=False, **kwargs):
        return SoftButton(parent, text=text, command=command, height=34, corner_radius=8,
                          font=self._font(12, "bold"), fg_color=self.theme["accent"] if primary else self.theme["card2"],
                          hover_color=self.theme["hover"] if primary else self.theme["border"],
                          text_color=self.theme["accent_text"] if primary else self.theme["text"], **kwargs)

    def _build(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, columnspan=2, sticky="ew", padx=20, pady=(16, 10))
        header.grid_columnconfigure(0, weight=1)
        self._label(header, "Text, stickers & watermarks", font=self._font(22, "bold")).grid(row=0, column=0, sticky="w")
        self._label(header, "Preview: first source clip. Design covers the GIF; poster saves this frame.",
                    text_color=self.theme["muted"]).grid(row=1, column=0, sticky="w", pady=(2, 0))
        self.undo_button = self._button(header, "Undo", self.undo, width=64)
        self.undo_button.grid(row=0, column=1, rowspan=2, padx=4)
        self.redo_button = self._button(header, "Redo", self.redo, width=64)
        self.redo_button.grid(row=0, column=2, rowspan=2, padx=4)

        left = ctk.CTkFrame(self, fg_color=self.theme["card"], corner_radius=14,
                            border_width=1, border_color=self.theme["border"])
        left.grid(row=1, column=0, sticky="nsew", padx=(20, 10), pady=(0, 8))
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(0, weight=1)
        canvas_bg = self._apply_appearance_mode(self.theme["card"])
        self.canvas = tk.Canvas(left, bg=canvas_bg, highlightthickness=0, borderwidth=0,
                                takefocus=True, cursor="hand2", width=580, height=480)
        self.canvas.grid(row=0, column=0, sticky="nsew", padx=10, pady=(10, 4))
        self.canvas.bind("<Configure>", lambda event: self._schedule("display", 45, self._display))
        self.canvas.bind("<Button-1>", self._pointer_down)
        self.canvas.bind("<B1-Motion>", self._pointer_move)
        self.canvas.bind("<ButtonRelease-1>", self._pointer_up)
        for key, dx, dy in (("Left", -1, 0), ("Right", 1, 0), ("Up", 0, -1), ("Down", 0, 1)):
            self.canvas.bind(f"<{key}>", lambda event, x=dx, y=dy: self.nudge(x, y, fine=not event.state & 1))
        self._label(left, "Drag to move · Arrow keys to nudge · Shift + arrow for bigger steps",
                    font=self._font(11), text_color=self.theme["muted"]).grid(row=1, column=0, pady=(2, 8))
        timeline = ctk.CTkFrame(left, fg_color="transparent")
        timeline.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 12))
        timeline.grid_columnconfigure(0, weight=1)
        self.frame_label = self._label(timeline, "Preview frame: 0.0s", font=self._font(12))
        self.frame_label.grid(row=0, column=0, sticky="w")
        self.frame_slider = ctk.CTkSlider(timeline, from_=0, to=max(.1, self.source.clip_seconds - .02),
                                         command=self._change_frame, height=18)
        self.frame_slider.set(0)
        self.frame_slider.grid(row=1, column=0, sticky="ew", pady=(5, 0))
        if not self.source.is_video:
            self.frame_slider.configure(state="disabled")
            self.frame_label.configure(text="Poster uses this image")

        self.sidebar = ctk.CTkScrollableFrame(self, width=290, fg_color=self.theme["card"],
                                              border_width=1, border_color=self.theme["border"], corner_radius=14)
        self.sidebar.grid(row=1, column=1, sticky="ns", padx=(0, 20), pady=(0, 8))
        self.sidebar.grid_columnconfigure(0, weight=1)
        adds = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        adds.grid(row=0, column=0, sticky="ew", pady=(4, 10))
        adds.grid_columnconfigure((0, 1), weight=1)
        self.add_text_button = self._button(adds, "+ Text", self.add_text, width=125)
        self.add_text_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.add_image_button = self._button(adds, "+ Image / sticker", self.add_image, width=125)
        self.add_image_button.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        self._label(adds, "Watermark", font=self._font(12, "bold")).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(14, 5))
        self.text_watermark_button = self._button(adds, "Text watermark", self.add_watermark, width=125)
        self.text_watermark_button.grid(row=2, column=0, sticky="ew", padx=(0, 4))
        self.image_watermark_button = self._button(
            adds, "Image watermark", lambda: self.add_watermark("image"), width=125)
        self.image_watermark_button.grid(row=2, column=1, sticky="ew", padx=(4, 0))
        self._label(adds, "Subtle corner branding · edit text, position and opacity below",
                    font=self._font(11), text_color=self.theme["muted"], wraplength=270,
                    justify="left").grid(row=3, column=0, columnspan=2, sticky="w", pady=(5, 0))
        self._label(self.sidebar, "Layers · top item is in front", font=self._font(12, "bold")).grid(row=1, column=0, sticky="w")
        self.layer_list = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        self.layer_list.grid(row=2, column=0, sticky="ew", pady=(5, 7))
        self.layer_list.grid_columnconfigure(0, weight=1)
        layer_actions = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        layer_actions.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        layer_actions.grid_columnconfigure((0, 1, 2, 3), weight=1)
        self.forward_button = self._button(layer_actions, "Forward", lambda: self.reorder(1), width=65)
        self.back_button = self._button(layer_actions, "Back", lambda: self.reorder(-1), width=50)
        self.duplicate_button = self._button(layer_actions, "Copy", self.duplicate, width=50)
        self.delete_button = self._button(layer_actions, "Delete", self.delete, width=58)
        for column, button in enumerate((self.forward_button, self.back_button, self.duplicate_button, self.delete_button)):
            button.grid(row=0, column=column, sticky="ew", padx=2)

        self.properties = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        self.properties.grid(row=4, column=0, sticky="ew")
        self.properties.grid_columnconfigure(0, weight=1)
        self.empty_label = self._label(self.properties, "Add text or an image to start.\nTransparent PNG / WebP stickers work best.",
                                       justify="left", wraplength=270, text_color=self.theme["muted"])
        self.empty_label.grid(row=0, column=0, sticky="w", pady=10)
        self.text_properties = ctk.CTkFrame(self.properties, fg_color="transparent")
        self.text_properties.grid_columnconfigure(0, weight=1)
        self._label(self.text_properties, "Text", font=self._font(12, "bold")).grid(row=0, column=0, sticky="w")
        self.text_box = ctk.CTkTextbox(self.text_properties, height=94, font=self._font(13),
                                      fg_color=self.theme["card2"], border_width=1, border_color=self.theme["border"])
        self.text_box.grid(row=1, column=0, sticky="ew", pady=(4, 8))
        self.text_box.bind("<<Modified>>", self._text_modified)
        self.font_menu = ctk.CTkOptionMenu(self.text_properties, values=list(FONT_CHOICES), font=self._font(12),
                                         command=lambda value: self._property_changed("font", value))
        self.font_menu.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        checks = ctk.CTkFrame(self.text_properties, fg_color="transparent")
        checks.grid(row=3, column=0, sticky="w", pady=(0, 8))
        self.bold_var, self.outline_var = tk.BooleanVar(value=True), tk.BooleanVar(value=True)
        self.bold_check = ctk.CTkCheckBox(checks, text="Bold", variable=self.bold_var, width=80,
                                         font=self._font(12), command=lambda: self._property_changed("bold", self.bold_var.get()))
        self.bold_check.pack(side="left")
        self.outline_check = ctk.CTkCheckBox(checks, text="Outline", variable=self.outline_var, width=94,
                                            font=self._font(12), command=lambda: self._property_changed("outline", self.outline_var.get()))
        self.outline_check.pack(side="left", padx=(8, 0))
        self.color_button = self._button(self.text_properties, "Text color", self.choose_color, width=125)
        self.color_button.grid(row=4, column=0, sticky="ew", pady=(0, 8))

        self.common_properties = ctk.CTkFrame(self.properties, fg_color="transparent")
        self.common_properties.grid_columnconfigure(0, weight=1)
        self._slider_widgets = {}
        for row, (key, title, low, high) in enumerate((("size", "Size", 1, 150), ("rotation", "Rotation", -180, 180), ("opacity", "Opacity", 0, 100))):
            label = self._label(self.common_properties, title, font=self._font(12, "bold"))
            label.grid(row=row * 2, column=0, sticky="w", pady=(6, 0))
            slider = ctk.CTkSlider(self.common_properties, from_=low, to=high, command=lambda value, k=key: self._slider_changed(k, value))
            slider.grid(row=row * 2 + 1, column=0, sticky="ew", pady=(5, 5))
            self._slider_widgets[key] = (label, slider)
        position = ctk.CTkFrame(self.common_properties, fg_color="transparent")
        position.grid(row=6, column=0, sticky="ew", pady=(10, 4))
        position.grid_columnconfigure((1, 3), weight=1)
        self.position_entries = {}
        for index, key in enumerate(("x", "y")):
            self._label(position, key.upper() + "%", font=self._font(12)).grid(row=0, column=index * 2, padx=(0, 5))
            entry = ctk.CTkEntry(position, width=75, font=self._font(12), justify="center")
            entry.grid(row=0, column=index * 2 + 1, sticky="ew", padx=(0, 6))
            entry.bind("<Return>", lambda event, k=key: self._commit_position(k))
            entry.bind("<FocusOut>", lambda event, k=key: self._commit_position(k))
            self.position_entries[key] = entry
        self.center_button = self._button(self.common_properties, "Center selected layer", self.center)
        self._label(self.common_properties, "Position preset", font=self._font(12, "bold")).grid(
            row=7, column=0, sticky="w", pady=(8, 4))
        self.anchor_menu = ctk.CTkOptionMenu(
            self.common_properties, values=list(ANCHOR_CHOICES), command=self.set_anchor,
            font=self._font(12), fg_color=self.theme["card2"], button_color=self.theme["accent"],
            button_hover_color=self.theme["hover"], text_color=self.theme["text"],
            dropdown_fg_color=self.theme["card"], dropdown_text_color=self.theme["text"],
            dropdown_hover_color=self.theme["border"])
        self.anchor_menu.set("Custom position")
        self.anchor_menu.grid(row=8, column=0, sticky="ew", pady=(0, 4))
        self.center_button.grid(row=9, column=0, sticky="ew", pady=(4, 8))

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=2, column=0, columnspan=2, sticky="ew", padx=20, pady=(0, 16))
        footer.grid_columnconfigure(0, weight=1)
        self.status = self._label(footer, "Loading preview…", font=self._font(12),
                                  text_color=self.theme["muted"], anchor="w", wraplength=510, justify="left")
        self.status.grid(row=0, column=0, columnspan=4, sticky="ew", pady=(0, 10))
        self.export_size = ctk.CTkOptionMenu(footer, values=["1600 px poster", "2048 px poster"], width=148, font=self._font(12))
        self.export_size.grid(row=1, column=0, sticky="w")
        self.export_button = self._button(footer, "Save poster PNG", self.save_poster, width=145)
        self.export_button.grid(row=1, column=1, padx=(8, 8))
        self.cancel_button = self._button(footer, "Cancel", self.close, width=76)
        self.cancel_button.grid(row=1, column=2, padx=(4, 8))
        self.apply_button = self._button(footer, "Apply to GIF", self.apply, primary=True, width=130)
        self.apply_button.grid(row=1, column=3)

    def _set_status(self, text):
        if not self._closed:
            self.status.configure(text=text)

    def _schedule(self, key, delay, callback):
        if self._closed:
            return
        previous = self._jobs.pop(key, None)
        if previous:
            self.after_cancel(previous)
        def run():
            self._jobs.pop(key, None)
            if not self._closed:
                callback()
        self._jobs[key] = self.after(delay, run)

    def _worker(self, kind, generation, cancel, function):
        holder = {"proc": None}
        def cancelled():
            return self._cancel.is_set() or cancel.is_set()
        def run():
            if cancelled():
                return
            with self._holder_lock:
                self._holders.append(holder)
            try:
                result = function(cancelled, holder)
                if not cancelled():
                    self._events.put((kind, generation, result, None))
            except Exception as exc:
                if not cancelled():
                    self._events.put((kind, generation, None, exc))
            finally:
                with self._holder_lock:
                    self._holders.remove(holder)
        self._pool.submit(run)

    def _load_frame(self):
        self._frame_cancel.set()
        self._render_cancel.set()
        self._render_generation += 1
        self._rendered_layers = None
        self._frame_cancel = threading.Event()
        self._frame_generation += 1
        generation = self._frame_generation
        offset = self._offset
        self._loading = True
        self._update_buttons()
        self._set_status("Loading preview frame…")
        self._worker("frame", generation, self._frame_cancel,
                     lambda cancelled, holder: overlays.preview_frame(
                         self.source, self.options, self.preview_size, offset=offset,
                         should_cancel=cancelled, proc_holder=holder))

    def _request_render(self):
        self._schedule("render", 40, self._render)

    def _render(self):
        if self._frame_image is None:
            return
        self._render_cancel.set()
        self._render_cancel = threading.Event()
        self._render_generation += 1
        generation = self._render_generation
        layers, base = self.layers, self._frame_image.copy()
        def render(cancelled, holder):
            design = overlays.compose_overlay(layers, self.preview_size, should_cancel=cancelled)
            image = Image.alpha_composite(base.convert("RGBA"), design)
            bounds = []
            for layer in layers:
                if cancelled():
                    raise engine.RenderCancelled()
                _, box = overlays.render_layer(layer, self.preview_size)
                bounds.append(box)
            return image, bounds, layers
        self._worker("render", generation, self._render_cancel, render)

    def _poll(self):
        while True:
            try:
                kind, generation, result, error = self._events.get_nowait()
            except queue.Empty:
                break
            if kind == "frame":
                if generation != self._frame_generation:
                    continue
                self._loading = False
                if error:
                    self._frame_image = None
                    self._rendered = None
                    self._bounds = []
                    self._display()
                    self._set_status(f"Could not load preview: {error}")
                else:
                    self._frame_image = result.convert("RGBA")
                    self._request_render()
                    self._set_status("Click a layer to edit it. Apply when your design is ready.")
            elif kind == "render":
                if generation != self._render_generation or (result and result[2] != self.layers):
                    continue
                if error:
                    self._set_status(f"Could not preview design: {error}")
                else:
                    self._rendered, self._bounds, _ = result
                    self._rendered_layers = result[2]
                    self._display()
            elif kind == "poster":
                self._exporting = False
                self._set_status(f"Could not save poster: {error}" if error else f"Poster saved: {result.name}")
            elif kind == "sticker":
                self._pending_stickers = max(0, self._pending_stickers - 1)
                if not self._pending_stickers:
                    self.add_image_button.configure(text="+ Image / sticker")
                if error:
                    self._set_status(f"Could not add sticker: {error}")
                else:
                    self._add(result)
            self._update_buttons()
        self._schedule("poll", 30, self._poll)

    def _display(self):
        if self._closed:
            return
        self.canvas.delete("all")
        if self._rendered is None:
            self.canvas.create_text(max(1, self.canvas.winfo_width()) / 2,
                                    max(1, self.canvas.winfo_height()) / 2,
                                    text="Loading preview…" if self._loading else "Preview unavailable",
                                    fill=self._apply_appearance_mode(self.theme["muted"]), font=(self.theme["font"], 13))
            return
        cw, ch = max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())
        scale = min((cw - 20) / self.preview_size[0], (ch - 20) / self.preview_size[1])
        w, h = max(1, round(self.preview_size[0] * scale)), max(1, round(self.preview_size[1] * scale))
        left, top = (cw - w) / 2, (ch - h) / 2
        self._view = (left, top, w, h)
        image = self._rendered.resize((w, h), Image.Resampling.LANCZOS)
        self._image_tk = ImageTk.PhotoImage(image, master=self)
        self.canvas.create_image(left, top, anchor="nw", image=self._image_tk)
        self.canvas.create_rectangle(left - 1, top - 1, left + w + 1, top + h + 1,
                                     outline=self._apply_appearance_mode(self.theme["border"]))
        self._draw_selection()

    def _draw_selection(self):
        self.canvas.delete("selection")
        if self._selected is None or self._selected >= len(self._bounds) or self._rendered_layers != self.layers:
            return
        left, top, width, height = self._view
        box = self._bounds[self._selected]
        sx, sy = width / self.preview_size[0], height / self.preview_size[1]
        x0, y0, x1, y1 = box
        self.canvas.create_rectangle(left + max(0, x0) * sx, top + max(0, y0) * sy,
                                     left + min(self.preview_size[0], x1) * sx,
                                     top + min(self.preview_size[1], y1) * sy,
                                     outline=self._apply_appearance_mode(self.theme["accent"]), width=2, dash=(5, 3), tags="selection")

    def _record(self, key, force=False):
        now = time.monotonic()
        if force or key != self._last_edit[0] or now - self._last_edit[1] > .65:
            self._undo.append((self.layers, self._selected))
            self._undo = self._undo[-60:]
        self._redo.clear()
        self._last_edit = (key, now)

    def _changed(self, *, refresh=False):
        if refresh:
            self._refresh_layers()
        self._request_render()
        self._update_buttons()

    def _add(self, layer):
        if len(self.layers) >= MAX_LAYERS:
            self._set_status("A design can contain up to 20 layers. Delete a layer before adding another.")
            return
        try:
            validated = tuple(overlays.validate_layers(self.layers + (layer,)))
        except Exception as exc:
            self._set_status(str(exc))
            return
        self._record("add", force=True)
        self.layers, self._selected = validated, len(validated) - 1
        self._changed(refresh=True)

    def add_text(self):
        self._add(overlays.OverlayLayer(text="Your text", x=.5, y=.25, size=.10))

    def add_image(self, path=None):
        self._queue_image(path)

    def add_watermark(self, kind="text", path=None):
        """Add editable branding using the same local-only design layers."""
        if self._closed:
            return
        if kind == "text":
            self._add(overlays.OverlayLayer(text="Yui", x=.82, y=.9, size=.06,
                                            opacity=.45, outline=True))
        elif kind == "image":
            self._queue_image(path, watermark=True)
        else:
            self._set_status("Choose a text or image watermark.")

    @staticmethod
    def _anchor_position(layer, anchor, canvas_size, bounds=None):
        """Keep the complete rendered tile inside a small canvas margin."""
        width, height = canvas_size
        if bounds is not None:
            tile_width, tile_height = bounds[2] - bounds[0], bounds[3] - bounds[1]
        else:
            # Fresh edits may be waiting on a preview. This estimate avoids
            # decoding an image or loading a font on Tk's event thread.
            pixels = layer.size * min(width, height)
            if layer.kind == "image":
                tile_width = tile_height = pixels
            else:
                lines = layer.text.splitlines() or [""]
                tile_width = min(width * .94, max(1, max(map(len, lines))) * pixels * .7 + 6)
                tile_height = max(1, len(lines)) * pixels * 1.35 + 6
            if abs(layer.rotation) > .001:
                # The diagonal bounds every rotation without extra image work.
                tile_width = tile_height = (tile_width ** 2 + tile_height ** 2) ** .5
        inset_x, inset_y = .035 + tile_width / (2 * width), .035 + tile_height / (2 * height)
        left, right = min(.5, inset_x), max(.5, 1 - inset_x)
        top, bottom = min(.5, inset_y), max(.5, 1 - inset_y)
        positions = {"Top left": (left, top), "Top right": (right, top),
                     "Center": (.5, .5), "Bottom left": (left, bottom),
                     "Bottom right": (right, bottom)}
        return replace(layer, x=positions[anchor][0], y=positions[anchor][1])

    def set_anchor(self, anchor):
        if self._selected is None or self._closed or anchor not in ANCHOR_CHOICES:
            return
        layer = self.layers[self._selected]
        bounds = self._bounds[self._selected] if self._rendered_layers == self.layers else None
        changed = self._anchor_position(layer, anchor, self.preview_size, bounds)
        if changed != layer:
            self._record("anchor", force=True)
            self.layers = self.layers[:self._selected] + (changed,) + self.layers[self._selected + 1:]
            self._sync_position()
            self._changed()
        self.anchor_menu.set(anchor)

    def _queue_image(self, path=None, *, watermark=False):
        if self._closed:
            return
        if len(self.layers) >= MAX_LAYERS:
            self._set_status("A design can contain up to 20 layers.")
            return
        if path is None:
            path = filedialog.askopenfilename(parent=self, title="Add image watermark" if watermark else "Add image or sticker",
                    filetypes=[("Images and stickers", "*.png *.webp *.jpg *.jpeg *.gif *.bmp *.tif *.tiff *.ico"), ("All files", "*.*")])
        if not path:
            return
        self._pending_stickers += 1
        self.add_image_button.configure(state="disabled", text="Loading sticker…")
        self._set_status("Reading watermark…" if watermark else "Reading sticker… Animated stickers use their first frame.")
        self._update_buttons()
        layer = overlays.OverlayLayer(kind="image", image=Path(path), x=.82 if watermark else .5,
                                      y=.9 if watermark else .5, size=.18 if watermark else .35,
                                      opacity=.45 if watermark else 1.)
        def validate(cancelled, holder):
            result = tuple(overlays.validate_layers((layer,)))[0]
            if cancelled():
                raise engine.RenderCancelled()
            _, bounds = overlays.render_layer(result, self.preview_size)
            if watermark:
                result = self._anchor_position(result, "Bottom right", self.preview_size, bounds)
            return result
        self._worker("sticker", 0, threading.Event(), validate)

    def select(self, index):
        self._selected = index if index is not None and 0 <= index < len(self.layers) else None
        self._last_edit = (None, 0.0)
        self._refresh_layers()
        self._draw_selection()

    def _refresh_layers(self):
        for child in self.layer_list.winfo_children():
            child.destroy()
        for index in reversed(range(len(self.layers))):
            layer = self.layers[index]
            name = layer.text.replace("\n", " ")[:27] if layer.kind == "text" else Path(layer.image).name[:27]
            name = name or "Empty text"
            button = self._button(self.layer_list, f"{index + 1}. {name}", lambda i=index: self.select(i),
                                  primary=index == self._selected, anchor="w")
            button.grid(row=len(self.layers) - index, column=0, sticky="ew", pady=2)
        self._sync_properties()
        self._update_buttons()

    def _sync_properties(self):
        self._updating = True
        try:
            if self._selected is None:
                self.text_properties.grid_remove()
                self.common_properties.grid_remove()
                self.empty_label.grid()
                return
            layer = self.layers[self._selected]
            self.empty_label.grid_remove()
            self.common_properties.grid(row=2, column=0, sticky="ew")
            if layer.kind == "text":
                self.text_properties.grid(row=1, column=0, sticky="ew")
                self.text_box.delete("1.0", "end")
                self.text_box.insert("1.0", layer.text)
                self.text_box._textbox.edit_modified(False)
                self.font_menu.set(layer.font)
                self.bold_var.set(layer.bold)
                self.outline_var.set(layer.outline)
                self.color_button.configure(text=f"Text color: {layer.color}")
            else:
                self.text_properties.grid_remove()
            for key, (label, slider) in self._slider_widgets.items():
                value = getattr(layer, key)
                slider.set(value if key == "rotation" else value * 100)
                self._update_slider_label(key, value)
            self._sync_position()
        finally:
            self._updating = False

    def _sync_position(self):
        if self._selected is None:
            return
        self.anchor_menu.set("Custom position")
        for key, entry in self.position_entries.items():
            entry.delete(0, "end")
            entry.insert(0, f"{getattr(self.layers[self._selected], key) * 100:.1f}")

    def _update_slider_label(self, key, value):
        label = self._slider_widgets[key][0]
        if key == "rotation":
            label.configure(text=f"Rotation: {value:.0f}°")
        else:
            label.configure(text=f"{key.capitalize()}: {value * 100:.0f}%")

    def _property_changed(self, key, value):
        if self._updating or self._selected is None or self._closed:
            return
        if key == "text" and len(value) > MAX_TEXT:
            self._set_status("Text can contain up to 2,000 characters per layer.")
            value = value[:MAX_TEXT]
            self.text_box.delete("1.0", "end")
            self.text_box.insert("1.0", value)
        current = self.layers[self._selected]
        if getattr(current, key) == value:
            return
        try:
            candidate = replace(current, **{key: value})
            changed = self.layers[:self._selected] + (candidate,) + self.layers[self._selected + 1:]
            validated = tuple(overlays.validate_layers(changed))[self._selected]
        except Exception as exc:
            self._set_status(str(exc))
            self._sync_properties()
            return
        self._record((self._selected, key))
        self.layers = self.layers[:self._selected] + (validated,) + self.layers[self._selected + 1:]
        if key == "text":
            # Update labels without destroying the focused text editor.
            for child in self.layer_list.winfo_children():
                if child.cget("text").startswith(f"{self._selected + 1}. "):
                    child.configure(text=f"{self._selected + 1}. {value.replace(chr(10), ' ')[:27] or 'Empty text'}")
                    break
        self._changed()

    def _slider_changed(self, key, value):
        value = float(value) if key == "rotation" else float(value) / 100
        self._property_changed(key, value)
        self._update_slider_label(key, value)

    def _text_modified(self, event=None):
        text_widget = self.text_box._textbox
        if not text_widget.edit_modified():
            return
        text_widget.edit_modified(False)
        if not self._updating:
            self._property_changed("text", self.text_box.get("1.0", "end-1c"))

    def _commit_position(self, key):
        if self._updating or self._selected is None:
            return "break"
        try:
            value = float(self.position_entries[key].get()) / 100
            if not 0 <= value <= 1:
                raise ValueError
        except ValueError:
            self._set_status("Position must be a number between 0 and 100 percent.")
            self._sync_position()
            return "break"
        self._property_changed(key, value)
        self._sync_position()
        return "break"

    def choose_color(self):
        if self._selected is None:
            return
        _, value = colorchooser.askcolor(color=self.layers[self._selected].color, parent=self, title="Text color")
        if value:
            self._property_changed("color", value)
            self.color_button.configure(text=f"Text color: {value}")

    def _pointer_down(self, event):
        self.canvas.focus_set()
        if self._rendered_layers != self.layers:
            self._set_status("Updating preview… You can also use the X / Y position controls.")
            return
        left, top, width, height = self._view
        px = (event.x - left) * self.preview_size[0] / width
        py = (event.y - top) * self.preview_size[1] / height
        index = next((index for index in reversed(range(len(self._bounds)))
                      if self._bounds[index][0] <= px <= self._bounds[index][2]
                      and self._bounds[index][1] <= py <= self._bounds[index][3]), None)
        self.select(index)
        if self._selected is not None:
            layer = self.layers[index]
            self._drag = (event.x, event.y, layer.x, layer.y, width, height)

    def _pointer_move(self, event):
        if self._drag is None or self._selected is None:
            return
        start_x, start_y, x, y, width, height = self._drag[:6]
        dx, dy = (event.x - start_x) / width, (event.y - start_y) / height
        layer = replace(self.layers[self._selected], x=max(0., min(1., x + dx)), y=max(0., min(1., y + dy)))
        if layer != self.layers[self._selected]:
            if len(self._drag) == 6:
                self._record("drag", force=True)
                self._drag = self._drag + (True,)
            self.layers = self.layers[:self._selected] + (layer,) + self.layers[self._selected + 1:]
            self._sync_position()
            self._changed()

    def _pointer_up(self, event):
        self._drag = None
        self._last_edit = (None, 0.)

    def nudge(self, dx, dy, *, fine=True):
        if self._selected is None:
            return "break"
        step = 1 if fine else 10
        layer = self.layers[self._selected]
        value = replace(layer, x=max(0., min(1., layer.x + dx * step / self.canvas_size[0])),
                        y=max(0., min(1., layer.y + dy * step / self.canvas_size[1])))
        if value != layer:
            self._record((self._selected, "nudge"))
            self.layers = self.layers[:self._selected] + (value,) + self.layers[self._selected + 1:]
            self._sync_position()
            self._changed()
        return "break"

    def center(self):
        if self._selected is None:
            return
        self._record("center", force=True)
        layer = replace(self.layers[self._selected], x=.5, y=.5)
        self.layers = self.layers[:self._selected] + (layer,) + self.layers[self._selected + 1:]
        self._sync_position()
        self.anchor_menu.set("Center")
        self._changed()

    def delete(self):
        if self._selected is None:
            return
        self._record("delete", force=True)
        self.layers = self.layers[:self._selected] + self.layers[self._selected + 1:]
        self._selected = min(self._selected, len(self.layers) - 1) if self.layers else None
        self._changed(refresh=True)

    def duplicate(self):
        if self._selected is None:
            return
        layer = self.layers[self._selected]
        self._add(replace(layer, x=min(1., layer.x + .03), y=min(1., layer.y + .03)))

    def reorder(self, direction):
        if self._selected is None:
            return
        target = self._selected + direction
        if not 0 <= target < len(self.layers):
            return
        self._record("reorder", force=True)
        layers = list(self.layers)
        layers[self._selected], layers[target] = layers[target], layers[self._selected]
        self.layers, self._selected = tuple(layers), target
        self._changed(refresh=True)

    def undo(self):
        if not self._undo:
            return
        self._redo.append((self.layers, self._selected))
        self.layers, self._selected = self._undo.pop()
        self._last_edit = (None, 0.)
        self._changed(refresh=True)

    def redo(self):
        if not self._redo:
            return
        self._undo.append((self.layers, self._selected))
        self.layers, self._selected = self._redo.pop()
        self._last_edit = (None, 0.)
        self._changed(refresh=True)

    def _undo_key(self, event):
        # Keep normal text editing shortcuts inside text boxes / entries.
        if not isinstance(event.widget, (tk.Text, tk.Entry)):
            self.undo()
            return "break"

    def _redo_key(self, event):
        if not isinstance(event.widget, (tk.Text, tk.Entry)):
            self.redo()
            return "break"

    def _change_frame(self, value):
        self._offset = float(value)
        self._loading = True
        self._update_buttons()
        self._set_status("Loading preview frame…")
        self.frame_label.configure(text=f"Preview frame: {engine.fmt_time(self._offset)}s")
        self._schedule("frame", 140, self._load_frame)

    def _update_buttons(self):
        selected = self._selected is not None
        self.undo_button.configure(state="normal" if self._undo else "disabled")
        self.redo_button.configure(state="normal" if self._redo else "disabled")
        for button in (self.delete_button, self.duplicate_button, self.center_button):
            button.configure(state="normal" if selected else "disabled")
        self.forward_button.configure(state="normal" if selected and self._selected < len(self.layers) - 1 else "disabled")
        self.back_button.configure(state="normal" if selected and self._selected > 0 else "disabled")
        self.add_text_button.configure(state="normal" if len(self.layers) < MAX_LAYERS else "disabled")
        self.text_watermark_button.configure(state="normal" if len(self.layers) < MAX_LAYERS else "disabled")
        self.image_watermark_button.configure(
            state="normal" if len(self.layers) < MAX_LAYERS and not self._pending_stickers else "disabled")
        self.anchor_menu.configure(state="normal" if selected else "disabled")
        if self.add_image_button.cget("text") != "Loading sticker…":
            self.add_image_button.configure(state="normal" if len(self.layers) < MAX_LAYERS else "disabled")
        ready = not self._loading and self._frame_image is not None and self._rendered_layers == self.layers
        self.export_button.configure(state="normal" if not self._exporting and ready else "disabled")
        self.apply_button.configure(state="normal" if not self._exporting and ready else "disabled")

    def save_poster(self, output=None, *, overwrite=False):
        if self._exporting or self._loading or self._frame_image is None or self._rendered_layers != self.layers:
            return
        if output is None:
            output = filedialog.asksaveasfilename(parent=self, title="Save poster PNG", defaultextension=".png",
                        initialfile="yui-poster.png", filetypes=[("PNG image", "*.png")], confirmoverwrite=False)
            if not output:
                return
            output = Path(output)
            if not is_local_path(output):
                self._set_status("Choose a PNG poster destination on a local drive.")
                return
            if output.exists():
                overwrite = self._confirm("Replace poster?", "Replace the existing PNG poster?",
                                          confirm_text="Replace poster", cancel_text="Keep existing")
                if not overwrite:
                    return
        output = Path(output)
        if not is_local_path(output):
            self._set_status("Choose a PNG poster destination on a local drive.")
            return
        layers, offset = self.layers, self._offset
        max_side = 2048 if self.export_size.get().startswith("2048") else 1600
        self._exporting = True
        self._set_status("Saving poster PNG… You can keep editing while it saves.")
        self._update_buttons()
        self._worker("poster", 0, threading.Event(), lambda cancelled, holder: overlays.export_poster(
            self.source, self.options, layers, output, canvas_size=self.canvas_size, offset=offset,
            max_side=max_side, overwrite=overwrite, should_cancel=cancelled, proc_holder=holder,
            protected_paths=self.protected_paths))

    def apply(self):
        if self._closed or self._exporting or self._loading or self._rendered_layers != self.layers:
            return
        try:
            layers = tuple(overlays.validate_layers(self.layers))
            self._on_apply_callback(layers)
        except Exception as exc:
            self._set_status(f"Could not apply design: {exc}")
            return
        self._initial = layers
        self.close(force=True)

    def _confirm(self, title, message, **kwargs):
        self._confirmation_open = True
        try:
            return ask_confirmation(self, title, message, **kwargs)
        finally:
            self._confirmation_open = False

    def close(self, force=False):
        if self._closed:
            return True
        if not force:
            if self._confirmation_open:
                return False
            if self._exporting and not self._confirm("Cancel poster export?", "A poster is still saving. Cancel it and close the editor?",
                                                   confirm_text="Cancel export", cancel_text="Keep saving"):
                return False
            if self.layers != self._initial and not self._confirm("Discard design changes?", "Your changes have not been applied to the GIF. Discard them and close the editor?",
                                                               confirm_text="Discard changes", cancel_text="Keep editing"):
                return False
        self._closed = True
        self._cancel.set()
        self._frame_cancel.set()
        self._render_cancel.set()
        for job in self._jobs.values():
            try:
                self.after_cancel(job)
            except tk.TclError:
                pass
        self._jobs.clear()
        with self._holder_lock:
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
        except tk.TclError:
            pass
        self.destroy()
        if self._on_close_callback is not None:
            self._on_close_callback()
        return True
