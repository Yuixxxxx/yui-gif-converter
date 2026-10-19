"""Yui Gif Converter main window and dialogs."""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import traceback
import secrets
from concurrent.futures import CancelledError, ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Callable, Optional

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageOps

from . import APP_NAME, AUTHOR, PROFILE_URL, SUPPORT_URL, TERMS_VERSION, VERSION, YEAR, engine, legal
from .engine import MediaItem, fmt_duration, fmt_size, fmt_time, parse_time, short_name
from .paths import data_dir, get_ffmpeg, is_frozen, is_local_path, log_path, resource
from .preview import GifPlayer
from .presets import BUILTIN_DESCRIPTIONS, PresetError, PresetStore
from .settings import Settings, SettingsWriter
from . import source_bundle
from .widgets import SoftButton, system_animations_enabled
from .dialogs import ask_confirmation
from .windows import child_environment, copy_file, system_executable

MAX_MEDIA_ITEMS = 200
MAX_DROP_DATA_CHARS = 1024 * 1024
STAGE_CONTENT_INSET = 8  # Keep rectangular child backgrounds inside the rounded border.

# ── look ────────────────────────────────────────────────────────────────────
# (light, dark) pairs; CustomTkinter picks one by appearance mode.
BG = ("#f6f5f8", "#0d0c10")
CARD = ("#ffffff", "#15141a")
CARD_2 = ("#f1eff5", "#1c1a22")
CARD_HOVER = ("#ece9f2", "#221f29")
BORDER = ("#e2dfe8", "#2a2733")
TEXT = ("#16141a", "#f4f2f7")
MUTED = ("#6b6676", "#9a95a6")
DIM = ("#9d98a8", "#615c6c")
ACCENT = ("#e0457b", "#ff6b9d")
ACCENT_HOVER = ("#c93a6c", "#ff8ab2")
ACCENT_TEXT = ("#ffffff", "#1b0f15")
ACCENT_SOFT = ("#fde4ee", "#3a1f2b")
SUCCESS = ("#1f9d55", "#7fe0a0")
ERROR = ("#d93636", "#ff8a80")
FONT = "Segoe UI"

SIZE_OPTIONS = [("10 MB (max)", 10.0), ("8 MB", 8.0), ("5 MB", 5.0), ("3 MB", 3.0), ("2 MB", 2.0), ("1 MB", 1.0)]
FPS_OPTIONS = [("Auto", 0), ("33 fps", 33), ("25 fps", 25), ("20 fps", 20), ("15 fps", 15),
               ("12 fps", 12), ("10 fps", 10), ("8 fps", 8), ("5 fps", 5)]
SPEED_OPTIONS = [("0.25×", 0.25), ("0.5×", 0.5), ("0.75×", 0.75), ("1× normal", 1.0), ("1.25×", 1.25),
                 ("1.5×", 1.5), ("2×", 2.0), ("3×", 3.0), ("4×", 4.0)]
LOOP_OPTIONS = [("Forever", 0), ("Play once", -1), ("2 times", 1), ("3 times", 2), ("5 times", 4)]
CANVAS_OPTIONS = [("Auto (first file)", "auto"), ("1:1 Square", "1:1"), ("16:9 Wide", "16:9"),
                  ("9:16 Vertical", "9:16"), ("4:3", "4:3"), ("3:4", "3:4"), ("4:5 Portrait", "4:5")]
TRANSITION_OPTIONS = [(label, key) for key, label in engine.TRANSITIONS.items()]
TRANSITION_LENGTHS = [("0.3 s", 0.3), ("0.5 s", 0.5), ("0.75 s", 0.75), ("1 s", 1.0), ("1.5 s", 1.5)]
PHOTO_SECONDS = [("0.5 s", 0.5), ("1 s", 1.0), ("1.5 s", 1.5), ("2 s", 2.0), ("3 s", 3.0),
                 ("4 s", 4.0), ("5 s", 5.0), ("8 s", 8.0)]
SIDE_OPTIONS = [("Auto", 0), ("320 px", 320), ("480 px", 480), ("640 px", 640), ("800 px", 800),
                ("1080 px", 1080), ("1280 px", 1280)]
DITHER_OPTIONS = [("Auto", "auto"), ("Bayer · smaller", "bayer"), ("Floyd–Steinberg", "floyd"),
                  ("Sierra · smooth", "sierra"), ("None · flat", "none")]
PRIORITY_LABELS = [("Auto", "auto"), ("Smooth", "smooth"), ("Sharp", "sharp")]
THEMES = [("Dark", "dark"), ("Light", "light"), ("System", "system")]

FILE_TYPES = [
    ("Videos and images", " ".join(f"*{e}" for e in sorted(engine.MEDIA_EXTS))),
    ("Videos", " ".join(f"*{e}" for e in sorted(engine.VIDEO_EXTS))),
    ("Images", " ".join(f"*{e}" for e in sorted(engine.IMAGE_EXTS))),
    ("All files", "*.*"),
]


def label_for(table: list[tuple[str, object]], value) -> str:
    for label, v in table:
        if v == value:
            return label
    numeric = [(label, v) for label, v in table if isinstance(v, (int, float)) and isinstance(value, (int, float))]
    if numeric:
        return min(numeric, key=lambda lv: abs(float(lv[1]) - float(value)))[0]
    return table[0][0]


def no_window() -> dict:
    return {"creationflags": engine.CREATE_NO_WINDOW} if os.name == "nt" else {}


def log_exception(exc_type, exc, tb) -> None:
    try:
        path = log_path()
        if not is_local_path(path):
            return
        if path.is_file() and path.stat().st_size > 256 * 1024:
            path.write_text("", encoding="utf-8")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"--- {APP_NAME} {VERSION}\n")
            fh.write(f"{exc_type.__name__}\n")
            for frame in traceback.extract_tb(tb):
                fh.write(f"  {Path(frame.filename).name}:{frame.lineno} ({frame.name})\n")
    except (OSError, RuntimeError):
        pass


def set_window_icon(win) -> None:
    icon = resource("assets", "icon.ico")
    if icon.exists():
        try:
            win.iconbitmap(str(icon))
        except Exception:
            pass


# ── media list row ──────────────────────────────────────────────────────────


class ItemRow(ctk.CTkFrame):
    THUMB = (88, 50)

    def __init__(self, master, app: "App", item: MediaItem, index: int, count: int) -> None:
        super().__init__(master, fg_color=CARD_2, corner_radius=12)
        self.app = app
        self.item = item
        self.grid_columnconfigure(2, weight=1)

        self.number_label = ctk.CTkLabel(
            self, text=str(index + 1), width=22, font=app.font(12, "bold"), text_color=DIM
        )
        self.number_label.grid(row=0, column=0, rowspan=3, padx=(10, 2), pady=10)

        self.thumb = ctk.CTkLabel(
            self, text="…", width=self.THUMB[0], height=self.THUMB[1], fg_color=BORDER,
            corner_radius=6, text_color=DIM, font=app.font(11),
        )
        self.thumb.grid(row=0, column=1, rowspan=3, padx=(4, 12), pady=10)
        if item.thumb is not None:
            self.set_thumb(item.thumb)

        ctk.CTkLabel(
            self, text=short_name(item.path.name, 38), width=1, wraplength=260,
            font=app.font(13, "bold"), text_color=TEXT, anchor="w", justify="left"
        ).grid(row=0, column=2, sticky="ew", pady=(10, 0))
        ctk.CTkLabel(
            self, text=item.summary, font=app.font(11), text_color=MUTED, anchor="w"
        ).grid(row=1, column=2, sticky="nw")

        controls = ctk.CTkFrame(self, fg_color="transparent")
        controls.grid(row=2, column=2, sticky="w", pady=(4, 10))
        if item.is_video:
            ctk.CTkLabel(controls, text="Trim", font=app.font(11), text_color=MUTED).pack(side="left", padx=(0, 6))
            self.start_entry = self._entry(controls, fmt_time(item.trim_start), self._commit_trim)
            self.start_entry.pack(side="left")
            ctk.CTkLabel(controls, text="–", font=app.font(12), text_color=MUTED).pack(side="left", padx=4)
            self.end_entry = self._entry(controls, fmt_time(item.trim_end), self._commit_trim)
            self.end_entry.pack(side="left")
        else:
            ctk.CTkLabel(controls, text="Show for", font=app.font(11), text_color=MUTED).pack(side="left", padx=(0, 6))
            self.hold_entry = self._entry(controls, f"{item.hold:g}", self._commit_hold)
            self.hold_entry.pack(side="left")
            ctk.CTkLabel(controls, text="s", font=app.font(11), text_color=MUTED).pack(side="left", padx=(4, 0))

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=0, column=3, rowspan=3, padx=(4, 10))
        self.move_buttons = []
        for symbol, tip, enabled, cmd in (
            ("↑", "Move up", index > 0, lambda: app.move_item(item, -1)),
            ("↓", "Move down", index < count - 1, lambda: app.move_item(item, 1)),
            ("✕", "Remove", True, lambda: app.remove_item(item)),
        ):
            button = SoftButton(
                buttons, text=symbol, width=30, height=30, corner_radius=8, fg_color="transparent",
                hover_color=CARD_HOVER, text_color=TEXT if enabled else DIM, font=app.font(14),
                state="normal" if enabled else "disabled", command=cmd,
            )
            button.pack(side="left", padx=1)
            if tip != "Remove":
                self.move_buttons.append(button)

    def update_position(self, index: int, count: int) -> None:
        position = (index, count)
        if getattr(self, "_position", None) == position:
            return
        self._position = position
        self.number_label.configure(text=str(index + 1))
        for button, enabled in zip(self.move_buttons, (index > 0, index < count - 1)):
            button.configure(state="normal" if enabled else "disabled", text_color=TEXT if enabled else DIM)

    def _entry(self, parent, value: str, commit: Callable[[], None]) -> ctk.CTkEntry:
        entry = ctk.CTkEntry(
            parent, width=62, height=30, corner_radius=8, justify="center", font=self.app.font(12),
            fg_color=CARD, border_color=BORDER, text_color=TEXT,
        )
        entry.insert(0, value)
        entry.bind("<Return>", lambda e: (commit(), "break")[1])
        entry.bind("<Control-Return>", lambda e: (self.app.on_primary(), "break")[1])
        entry.bind("<FocusOut>", lambda e: commit())
        return entry

    @staticmethod
    def _set(entry: ctk.CTkEntry, text: str) -> None:
        entry.delete(0, "end")
        entry.insert(0, text)

    def _commit_trim(self) -> None:
        item = self.item
        start = parse_time(self.start_entry.get())
        end = parse_time(self.end_entry.get())
        start = item.trim_start if start is None else min(max(0.0, start), max(0.0, item.duration - 0.1))
        end = item.trim_end if end is None else min(max(start + 0.1, end), item.duration)
        item.start = start
        item.end = 0.0 if end >= item.duration - 0.01 else end
        self._set(self.start_entry, fmt_time(item.trim_start))
        self._set(self.end_entry, fmt_time(item.trim_end))
        self.app.on_items_changed()

    def _commit_hold(self) -> None:
        value = parse_time(self.hold_entry.get())
        if value is not None:
            self.item.hold = min(60.0, max(0.1, value))
        self._set(self.hold_entry, f"{self.item.hold:g}")
        self.app.on_items_changed()

    def set_thumb(self, pil: Image.Image) -> None:
        w, h = self.THUMB
        im = pil.copy()
        im.thumbnail((w * 2, h * 2))
        r = min(w / im.width, h / im.height)
        size = (max(1, int(im.width * r)), max(1, int(im.height * r)))
        self._thumb_img = ctk.CTkImage(light_image=im, dark_image=im, size=size)
        self.thumb.configure(image=self._thumb_img, text="", fg_color="transparent")


# ── main window ─────────────────────────────────────────────────────────────


class App:
    def __init__(self, files: Optional[list[Path]] = None) -> None:
        self.settings = Settings.load()
        self.presets = PresetStore()
        ctk.set_appearance_mode(self.settings.theme)
        ctk.set_default_color_theme("dark-blue")

        self.root = self._make_root()
        self._system_animations = system_animations_enabled()
        self.root._yui_motion_enabled = lambda: self.settings.button_animations and self._system_animations
        self._closed = False
        self._progress_job = None
        self._background_procs = {}
        self._background_lock = threading.Lock()
        self._fonts: dict[tuple, ctk.CTkFont] = {}
        self.items: list[MediaItem] = []
        self.rows: list[ItemRow] = []
        self.results: list[engine.RenderResult] = []
        self.design_layers = ()
        self._designer = None
        self._toolbox = None
        self.busy = False
        self.loading = 0
        self._import_generation = 0
        self._import_sequence = 0
        self._import_next = 0
        self._import_results = {}
        self.cancel_flag = False
        self.proc_holder: dict = {"proc": None}
        self.ui_q: queue.Queue = queue.Queue()
        self.pool = ThreadPoolExecutor(max_workers=2)
        self.view = ""
        self._gate: Optional[ctk.CTkToplevel] = None
        self._logo_pil: Optional[Image.Image] = None
        self._save_copy_busy = False
        self._source_export_busy = False
        self._source_buttons = []
        self._source_status = None
        self._settings_writer = SettingsWriter()
        self._rows_job = None
        self._building_rows = False
        self._rows_by_item = {}
        self._committing_rows = False

        self.root.title(APP_NAME)
        self.root.minsize(960, 560)
        self._center(1120, 740)
        set_window_icon(self.root)
        self.root.report_callback_exception = self._on_tk_error

        self._build()
        self._show_view("empty")
        self._refresh_summary()
        if not self.dnd_files:
            self._set_status("Drag and drop is unavailable. Use Browse to add videos or images.", error=True)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Control-o>", lambda e: self.browse())
        self.root.bind("<Control-Return>", lambda e: self.on_primary())
        self.root.bind("<Control-s>", lambda e: self.save_result_as())
        self.root.bind("<Escape>", lambda e: self._on_escape())
        self.root.after(16, self._pump)
        self.root.after(80, self._repaint_primary)

        if self.settings.accepted_terms != TERMS_VERSION:
            self.root.after(150, self._terms_gate)
        if files:
            self.root.after(250, lambda: self.add_paths(files))

    # ── setup ──

    def _make_root(self):
        self.dnd_files = None
        try:
            from tkinterdnd2 import DND_FILES
            from tkinterdnd2.TkinterDnD import DnDWrapper, _require
        except Exception:
            return ctk.CTk(fg_color=BG)

        app = self

        class DnDWindow(ctk.CTk, DnDWrapper):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                try:
                    self.TkdndVersion = _require(self)
                    app.dnd_files = DND_FILES
                except Exception:
                    self.TkdndVersion = None

        return DnDWindow(fg_color=BG)

    def font(self, size: int, weight: str = "normal") -> ctk.CTkFont:
        key = (size, weight)
        if key not in self._fonts:
            self._fonts[key] = ctk.CTkFont(family=FONT, size=size, weight=weight)
        return self._fonts[key]

    def _center(self, w: int, h: int) -> None:
        self.root.update_idletasks()
        try:
            scaling = ctk.ScalingTracker.get_window_scaling(self.root)
        except Exception:
            scaling = 1.0
        sw = self.root.winfo_screenwidth() / scaling
        sh = self.root.winfo_screenheight() / scaling
        self.root.minsize(min(960, int(sw * .94)), min(560, int(sh * .88)))
        w, h = min(w, int(sw * 0.94)), min(h, int(sh * 0.88))
        x = max(0, int((sw - w) / 2))
        y = max(0, int((sh - h) / 3))
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _build(self) -> None:
        root = self.root
        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(1, weight=1)
        self._build_header()

        body = ctk.CTkFrame(root, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=24, pady=(4, 0))
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(0, weight=1)

        self.stage = ctk.CTkFrame(body, fg_color=CARD, corner_radius=18, border_width=1, border_color=BORDER)
        self.stage.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        self.stage.grid_columnconfigure(0, weight=1)
        self.stage.grid_rowconfigure(0, weight=1)
        self._build_empty_view()
        self._build_list_view()
        self._build_result_view()

        self._build_options(body)
        self._build_footer()
        if self.dnd_files:
            self._bind_drops(self.root)

    def _build_header(self) -> None:
        header = ctk.CTkFrame(self.root, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=24, pady=(18, 12))
        header.grid_columnconfigure(1, weight=1)

        logo_path = resource("assets", "logo.png")
        if logo_path.exists():
            logo = Image.open(logo_path)
            logo.load()
            self._logo_pil = logo
            self._logo = ctk.CTkImage(light_image=logo, dark_image=logo, size=(40, 40))
            ctk.CTkLabel(header, image=self._logo, text="").grid(row=0, column=0, rowspan=2, padx=(0, 12))
        ctk.CTkLabel(
            header, text=APP_NAME, font=self.font(22, "bold"), text_color=TEXT, anchor="w"
        ).grid(row=0, column=1, sticky="sw")
        self.tagline = ctk.CTkLabel(header, text="", font=self.font(12), text_color=MUTED, anchor="w")
        self.tagline.grid(row=1, column=1, sticky="nw")

        right = ctk.CTkFrame(header, fg_color="transparent")
        right.grid(row=0, column=2, rowspan=2, sticky="e")
        SoftButton(
            right, text="Settings", width=110, height=34, corner_radius=10, fg_color="transparent",
            hover_color=CARD_HOVER, border_width=1, border_color=BORDER, text_color=TEXT,
            font=self.font(12), command=self.open_settings,
        ).pack(side="left", padx=(8, 0))

    def _build_empty_view(self) -> None:
        view = ctk.CTkFrame(self.stage, fg_color="transparent", corner_radius=0)
        self.empty_view = view
        inner = ctk.CTkFrame(view, fg_color="transparent")
        inner.place(relx=0.5, rely=0.46, anchor="center")

        def badge(mode: int) -> Image.Image:
            image = Image.new("RGBA", (304, 304))
            draw = ImageDraw.Draw(image)
            draw.ellipse((0, 0, 303, 303), fill=ACCENT_SOFT[mode])
            draw.rounded_rectangle((140, 92, 164, 212), radius=8, fill=ACCENT[mode])
            draw.rounded_rectangle((92, 140, 212, 164), radius=8, fill=ACCENT[mode])
            return image

        self._drop_image = ctk.CTkImage(light_image=badge(0), dark_image=badge(1), size=(76, 76))
        self.drop_badge = ctk.CTkLabel(inner, text="", image=self._drop_image, width=76, height=76)
        self.drop_badge.pack()
        ctk.CTkLabel(
            inner, text="Drop videos or images here", font=self.font(20, "bold"), text_color=TEXT
        ).pack(pady=(18, 4))
        ctk.CTkLabel(
            inner, text="or click to browse  ·  add several to combine them into one GIF",
            font=self.font(13), text_color=MUTED,
        ).pack()
        ctk.CTkLabel(
            inner, text="MP4 · MOV · WebM · MKV · AVI · GIF · PNG · JPG · WebP · and more",
            font=self.font(11), text_color=DIM,
        ).pack(pady=(6, 0))

        chips = ctk.CTkFrame(inner, fg_color="transparent")
        chips.pack(pady=(26, 0))
        for text in ("Always 10 MB or less", "Mix images + video", "Private · works offline"):
            ctk.CTkLabel(
                chips, text=text, fg_color=CARD_2, corner_radius=12, height=28, font=self.font(12),
                text_color=MUTED,
            ).pack(side="left", padx=5, ipadx=10)

        for widget in self._walk(view):
            widget.bind("<Button-1>", lambda e: self.browse(), add="+")
            try:
                widget.configure(cursor="hand2")
            except Exception:
                pass

    def _build_list_view(self) -> None:
        view = ctk.CTkFrame(self.stage, fg_color="transparent", corner_radius=0)
        self.list_view = view
        view.grid_columnconfigure(0, weight=1)
        view.grid_rowconfigure(1, weight=1)

        bar = ctk.CTkFrame(view, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 10))
        bar.grid_columnconfigure(1, weight=1)
        self.count_label = ctk.CTkLabel(bar, text="", font=self.font(15, "bold"), text_color=TEXT)
        self.count_label.grid(row=0, column=0, sticky="w")

        self.mode_button = ctk.CTkSegmentedButton(
            bar, values=["Combine into one GIF", "One GIF per file"], command=self._on_mode,
            font=self.font(12), height=32, selected_color=ACCENT, selected_hover_color=ACCENT_HOVER,
            unselected_color=CARD_2, unselected_hover_color=CARD_HOVER, fg_color=CARD_2,
            text_color=TEXT,
        )
        self.mode_button.grid(row=0, column=1, padx=12)
        self.mode_button.set("Combine into one GIF" if self.settings.mode == "combine" else "One GIF per file")

        actions = ctk.CTkFrame(bar, fg_color="transparent")
        actions.grid(row=0, column=2, sticky="e")
        SoftButton(
            actions, text="＋ Add", width=80, height=32, corner_radius=10, fg_color=CARD_2,
            hover_color=CARD_HOVER, text_color=TEXT, font=self.font(12, "bold"), command=self.browse,
        ).pack(side="left", padx=(0, 6))
        SoftButton(
            actions, text="Clear", width=64, height=32, corner_radius=10, fg_color="transparent",
            hover_color=CARD_HOVER, text_color=MUTED, font=self.font(12), command=self.clear_items,
        ).pack(side="left")

        self.list_frame = ctk.CTkScrollableFrame(
            view, fg_color="transparent", scrollbar_button_color=BORDER, scrollbar_button_hover_color=DIM,
        )
        self.list_frame.grid(row=1, column=0, sticky="nsew", padx=10)
        self.list_frame.grid_columnconfigure(0, weight=1)

        self.summary = ctk.CTkLabel(view, text="", font=self.font(12), text_color=MUTED, anchor="w")
        self.summary.grid(row=2, column=0, sticky="ew", padx=20, pady=(8, 14))

    def _build_result_view(self) -> None:
        view = ctk.CTkFrame(self.stage, fg_color="transparent", corner_radius=0)
        self.result_view = view
        view.grid_columnconfigure(0, weight=1)
        view.grid_rowconfigure(2, weight=1)

        top = ctk.CTkFrame(view, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=20, pady=(18, 4))
        top.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(top, text="✓", width=30, height=30, corner_radius=15, fg_color=SUCCESS,
                     text_color=CARD, font=self.font(15, "bold")).grid(row=0, column=0, padx=(0, 10))
        self.result_title = ctk.CTkLabel(top, text="", font=self.font(17, "bold"), text_color=TEXT, anchor="w")
        self.result_title.grid(row=0, column=1, sticky="w")
        self.result_picker = ctk.CTkOptionMenu(
            top, values=["-"], command=self._on_pick_result, width=220, height=30, corner_radius=8,
            fg_color=CARD_2, button_color=CARD_2, button_hover_color=CARD_HOVER, text_color=TEXT,
            font=self.font(12), dropdown_font=self.font(12),
        )

        self.result_info = ctk.CTkLabel(view, text="", width=1, wraplength=520, justify="left",
                                      font=self.font(12), text_color=MUTED, anchor="w")
        self.result_info.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 10))

        self.preview_box = ctk.CTkFrame(view, fg_color=CARD_2, corner_radius=14)
        self.preview_box.grid(row=2, column=0, sticky="nsew", padx=20)
        self.preview_box.grid_propagate(False)
        self.preview_box.grid_rowconfigure(0, weight=1)
        self.preview_box.grid_columnconfigure(0, weight=1)
        self.preview_label = ctk.CTkLabel(self.preview_box, text="", text_color=MUTED)
        self.preview_label.grid(row=0, column=0)
        self.player = GifPlayer(self.preview_label, self.preview_box)

        playback = ctk.CTkFrame(view, fg_color="transparent")
        playback.grid(row=3, column=0, sticky="ew", padx=20, pady=(8, 0))
        playback.grid_columnconfigure(3, weight=1)
        self.pause_button = SoftButton(
            playback, text="Pause", width=90, height=30, corner_radius=8, fg_color=CARD_2,
            hover_color=CARD_HOVER, text_color=TEXT, font=self.font(12), state="disabled",
            command=self.toggle_preview,
        )
        self.pause_button.grid(row=0, column=0, padx=(0, 8))
        self.restart_button = SoftButton(
            playback, text="Restart", width=90, height=30, corner_radius=8, fg_color=CARD_2,
            hover_color=CARD_HOVER, text_color=TEXT, font=self.font(12), state="disabled",
            command=self.restart_preview,
        )
        self.restart_button.grid(row=0, column=1)
        self.result_design_button = SoftButton(
            playback, text="Design / poster", width=130, height=30, corner_radius=8,
            fg_color=CARD_2, hover_color=CARD_HOVER, text_color=TEXT, font=self.font(12),
            command=self.open_designer, state="disabled",
        )
        self.result_design_button.grid(row=0, column=2, padx=(8, 0))
        self.playback_status = ctk.CTkLabel(playback, text="", text_color=MUTED,
                                           font=self.font(11), anchor="e")
        self.playback_status.grid(row=0, column=3, sticky="e")
        self._playback_state = None

        actions = ctk.CTkFrame(view, fg_color="transparent")
        actions.grid(row=4, column=0, sticky="ew", padx=16, pady=(8, 12))
        for column in range(3):
            actions.grid_columnconfigure(column, weight=1, uniform="result-action")
        for index, (text, cmd) in enumerate((
            ("Open GIF", self.open_result), ("Save as…", self.save_result_as),
            ("Copy GIF", self.copy_result), ("Show in folder", self.show_result_in_folder),
            ("Edit & redo", lambda: self._show_view("list")), ("Start over", self.clear_items),
        )):
            button = SoftButton(
                actions, text=text, width=100, height=34, corner_radius=10,
                fg_color=ACCENT if index == 0 else CARD_2,
                hover_color=ACCENT_HOVER if index == 0 else CARD_HOVER,
                text_color=ACCENT_TEXT if index == 0 else TEXT, font=self.font(12), command=cmd,
            )
            button.grid(row=index // 3, column=index % 3, sticky="ew", padx=4, pady=4)
            if text == "Save as…":
                self.save_as_button = button
        self._shown_result: Optional[engine.RenderResult] = None
        self._result_labels = {}

    def _build_options(self, body) -> None:
        panel = ctk.CTkScrollableFrame(
            body, width=316, fg_color=CARD, corner_radius=18, border_width=1, border_color=BORDER,
            scrollbar_button_color=BORDER, scrollbar_button_hover_color=DIM,
        )
        panel.grid(row=0, column=1, sticky="ns")
        panel.grid_columnconfigure(0, weight=1)
        self.options_panel = panel
        self._opt_row = 0

        self._section(panel, "Presets", first=True)
        row = self._next_row()
        self._label(panel, "Quick preset", row)
        self.preset_picker = ctk.CTkOptionMenu(
            panel, values=["Custom", *self.presets.names()], command=self._apply_preset,
            width=150, height=30, corner_radius=8, fg_color=CARD_2, dynamic_resizing=False,
            button_color=CARD_2, button_hover_color=CARD_HOVER, text_color=TEXT,
            dropdown_fg_color=CARD, dropdown_text_color=TEXT, dropdown_hover_color=CARD_HOVER,
            font=self.font(12), dropdown_font=self.font(12),
        )
        self.preset_picker.grid(row=row, column=1, sticky="e", padx=(0, 12), pady=4)
        preset_actions = ctk.CTkFrame(panel, fg_color="transparent")
        preset_actions.grid(row=self._next_row(), column=0, columnspan=2, sticky="ew", padx=14, pady=(4, 2))
        SoftButton(preset_actions, text="Save current…", width=140, height=30, fg_color=CARD_2,
                   hover_color=CARD_HOVER, text_color=TEXT, font=self.font(12),
                   command=self._save_preset).pack(side="left", padx=(0, 8))
        self.delete_preset_button = SoftButton(
            preset_actions, text="Delete saved", width=120, height=30, fg_color=CARD_2,
            hover_color=CARD_HOVER, text_color=TEXT, font=self.font(12),
            command=self._delete_preset, state="disabled",
        )
        self.delete_preset_button.pack(side="left")
        self.preset_description = ctk.CTkLabel(
            panel, text="", wraplength=278, font=self.font(11), text_color=MUTED,
            anchor="w", justify="left",
        )
        self.preset_description.grid(row=self._next_row(), column=0, columnspan=2,
                                     sticky="ew", padx=14, pady=(4, 2))
        self._sync_presets()

        self._section(panel, "Editing tools")
        self.toolbox_button = SoftButton(
            panel, text="Crop, effects & frame tools…", height=36, corner_radius=10,
            fg_color=CARD_2, hover_color=CARD_HOVER, text_color=TEXT,
            font=self.font(12, "bold"), command=self.open_toolbox, state="disabled",
        )
        self.toolbox_button.grid(row=self._next_row(), column=0, columnspan=2,
                                 sticky="ew", padx=14, pady=(4, 2))
        self._note(panel, "Crop, reverse, adjust colors, split clips, or extract PNG frames.")
        self._section(panel, "Text, stickers & watermarks")
        self.design_button = SoftButton(
            panel, text="Text & stickers / poster…", height=36, corner_radius=10,
            fg_color=CARD_2, hover_color=CARD_HOVER, text_color=TEXT,
            font=self.font(12, "bold"), command=self.open_designer, state="disabled",
        )
        self.design_button.grid(row=self._next_row(), column=0, columnspan=2,
                                sticky="ew", padx=14, pady=(4, 2))
        self._note(panel, "Text or image watermarks, stickers and posters. Designs cover the whole GIF.")

        self._section(panel, "Size & quality")
        self._menu_row(panel, "Max file size", SIZE_OPTIONS, "max_mb")
        self._note(panel, "Every GIF is checked after encoding and shrunk until it fits.")
        self._segment_row(panel, "Priority", PRIORITY_LABELS, "priority")
        self._note(panel, "Smooth keeps more frames · Sharp keeps more pixels.")
        self._menu_row(panel, "Frame rate", FPS_OPTIONS, "fps")

        self._section(panel, "Playback")
        self._menu_row(panel, "Speed", SPEED_OPTIONS, "speed")
        self._menu_row(panel, "Loop", LOOP_OPTIONS, "loop")
        self._switch_row(panel, "Boomerang (forward, then back)", "boomerang")
        self._switch_row(panel, "Reverse playback", "reverse")

        self._section(panel, "Canvas & combining")
        self._menu_row(panel, "Canvas shape", CANVAS_OPTIONS, "aspect")
        self._segment_row(panel, "Fit", [("Fit", "fit"), ("Fill (crop)", "fill")], "fit")
        self._color_row(panel, "Background")
        self._menu_row(panel, "Transition", TRANSITION_OPTIONS, "transition")
        self._menu_row(panel, "Transition length", TRANSITION_LENGTHS, "transition_seconds")
        self._menu_row(panel, "Photo duration", PHOTO_SECONDS, "image_seconds")
        self._link(panel, "Apply photo duration to all images", self._apply_photo_seconds)

        self._section(panel, "Advanced")
        self._menu_row(panel, "Max dimension", SIDE_OPTIONS, "max_side")
        self._menu_row(panel, "Dithering", DITHER_OPTIONS, "dither")
        self._link(panel, "Reset all options", self._reset_options)

    def _build_footer(self) -> None:
        footer = ctk.CTkFrame(self.root, fg_color="transparent")
        footer.grid(row=2, column=0, sticky="ew", padx=24, pady=(14, 18))
        footer.grid_columnconfigure(0, weight=1)

        left = ctk.CTkFrame(footer, fg_color="transparent")
        left.grid(row=0, column=0, sticky="ew", padx=(0, 16))
        left.grid_columnconfigure(0, weight=1)
        self.status = ctk.CTkLabel(left, text="", font=self.font(13), text_color=MUTED, anchor="w")
        self.status.grid(row=0, column=0, sticky="ew")
        self.progress = ctk.CTkProgressBar(
            left, height=6, corner_radius=3, progress_color=ACCENT, fg_color=CARD_2, mode="determinate"
        )
        self.progress.set(0)
        self.progress.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.progress.grid_remove()

        self.primary = SoftButton(
            footer, text="Create GIF", width=230, height=48, corner_radius=14, fg_color=ACCENT,
            hover_color=ACCENT_HOVER, text_color=ACCENT_TEXT, text_color_disabled=DIM,
            font=self.font(15, "bold"), command=self.on_primary,
        )
        self.primary.grid(row=0, column=1, sticky="e")

        support = ctk.CTkFrame(self.root, fg_color="transparent")
        support.grid(row=3, column=0, sticky="ew", padx=24, pady=(0, 14))
        support.grid_columnconfigure(0, weight=1)
        self.support_message = ctk.CTkLabel(
            support, text="Yui is free to use. If it makes your day easier, a matcha helps keep the updates coming.",
            width=1, height=36, font=self.font(11), text_color=MUTED,
            anchor="w", justify="left", wraplength=600,
        )
        self.support_message.grid(row=0, column=0, sticky="ew", padx=(0, 16))
        self._support_image = None
        logo_path = resource("assets", "kofi-logo.png")
        if logo_path.exists():
            with Image.open(logo_path) as image:
                logo = image.convert("RGBA")
            self._support_image = ctk.CTkImage(light_image=logo, dark_image=logo, size=(24, 19))
        self.support_button = SoftButton(
            support, text="Buy me a matcha", width=170, height=36, corner_radius=18,
            image=self._support_image, compound="left", anchor="center",
            fg_color="#B2D58F", hover_color="#C1E29F",
            text_color="#182611", font=self.font(12, "bold"), command=self._open_support,
        )
        self.support_button.grid(row=0, column=1, sticky="e")
        self._profile_image = None
        profile_image = resource("assets", "juicychat-avatar.png")
        if profile_image.exists():
            with Image.open(profile_image) as image:
                avatar = ImageOps.fit(image.convert("RGBA"), (128, 128), method=Image.Resampling.LANCZOS)
            mask = Image.new("L", avatar.size)
            ImageDraw.Draw(mask).ellipse((0, 0, 127, 127), fill=255)
            avatar.putalpha(mask)
            self._profile_image = ctk.CTkImage(light_image=avatar, dark_image=avatar, size=(24, 24))
        self.profile_button = SoftButton(
            support, text="Yui on JuicyChat",
            image=self._profile_image, width=170, height=36, corner_radius=18,
            compound="left", anchor="center",
            fg_color="#7448C6", hover_color="#865BD6",
            text_color="white", font=self.font(12, "bold"), command=self._open_profile,
        )
        self.profile_button.grid(row=1, column=1, sticky="e", pady=(6, 0))

        def fit_support_message(event):
            width = int(support._reverse_widget_scaling(event.width))
            wrap = min(650, max(120, width - 186))
            if self.support_message.cget("wraplength") != wrap:
                self.support_message.configure(wraplength=wrap)

        support.bind("<Configure>", fit_support_message, add="+")

    def _open_support(self) -> None:
        self._open_external(SUPPORT_URL, "Ko-fi")

    def _open_profile(self) -> None:
        self._open_external(PROFILE_URL, "Yui on JuicyChat")

    def _open_external(self, url: str, name: str) -> None:
        self._set_status(f"Opening {name} in your browser…")

        def open_browser():
            import webbrowser

            try:
                opened = webbrowser.open(url, new=2)
            except Exception:
                opened = False
            if not opened:
                self._post(self._set_status, f"Open {url} in your browser.")

        threading.Thread(target=open_browser, daemon=True).start()

    def _repaint_primary(self) -> None:
        # CTk can leave its button label as a solid block on the first paint.
        for button in (self.primary, self.support_button, self.profile_button):
            text = button.cget("text")
            button.configure(text="")
            self.root.after(20, lambda button=button, text=text:
                            button.configure(text=text) if button.winfo_exists() and button.cget("text") == "" else None)

    # ── option widgets ──

    def _section(self, panel, title: str, first: bool = False) -> None:
        ctk.CTkLabel(panel, text=title.upper(), font=self.font(11, "bold"), text_color=DIM, anchor="w").grid(
            row=self._next_row(), column=0, columnspan=2, sticky="ew", padx=14, pady=(10 if first else 22, 6)
        )

    def _next_row(self) -> int:
        self._opt_row += 1
        return self._opt_row

    def _label(self, panel, text: str, row: int) -> None:
        ctk.CTkLabel(panel, text=text, font=self.font(12), text_color=TEXT, anchor="w").grid(
            row=row, column=0, sticky="w", padx=(14, 8), pady=4
        )

    def _note(self, panel, text: str) -> None:
        ctk.CTkLabel(
            panel, text=text, font=self.font(11), text_color=MUTED, anchor="w", justify="left", wraplength=280
        ).grid(row=self._next_row(), column=0, columnspan=2, sticky="ew", padx=14, pady=(0, 6))

    def _link(self, panel, text: str, cmd) -> None:
        SoftButton(
            panel, text=text, height=26, fg_color="transparent", hover_color=CARD_HOVER, text_color=ACCENT,
            font=self.font(12), anchor="w", command=cmd,
        ).grid(row=self._next_row(), column=0, columnspan=2, sticky="w", padx=8, pady=(2, 2))

    def _menu_row(self, panel, text: str, table, attr: str) -> None:
        row = self._next_row()
        self._label(panel, text, row)
        mapping = dict(table)

        def changed(label: str) -> None:
            setattr(self.settings, attr, mapping[label])
            self._settings_changed()

        menu = ctk.CTkOptionMenu(
            panel, values=[label for label, _ in table], command=changed, width=150, height=30,
            corner_radius=8, fg_color=CARD_2, button_color=CARD_2, button_hover_color=CARD_HOVER,
            text_color=TEXT, dropdown_fg_color=CARD, dropdown_hover_color=CARD_HOVER,
            dropdown_text_color=TEXT, font=self.font(12), dropdown_font=self.font(12),
        )
        def sync() -> None:
            mapping.clear()
            mapping.update(table)
            value = getattr(self.settings, attr)
            label = next((label for label, candidate in table if candidate == value), None)
            if label is None:
                units = {"max_mb": " MB", "fps": " fps", "speed": "×", "max_side": " px",
                         "transition_seconds": " s", "image_seconds": " s"}
                label = (f"{value + 1} times" if attr == "loop" else
                         f"{value:g}{units.get(attr, '')}" if isinstance(value, (int, float)) else str(value))
                mapping[label] = value
            menu.configure(values=list(mapping))
            menu.set(label)
        sync()
        menu.grid(row=row, column=1, sticky="e", padx=(0, 12), pady=4)
        self._option_widgets.append(sync)

    def _segment_row(self, panel, text: str, table, attr: str) -> None:
        row = self._next_row()
        self._label(panel, text, row)
        mapping = dict(table)

        def changed(label: str) -> None:
            setattr(self.settings, attr, mapping[label])
            self._settings_changed()

        seg = ctk.CTkSegmentedButton(
            panel, values=[label for label, _ in table], command=changed, width=150, height=30,
            font=self.font(12), selected_color=ACCENT, selected_hover_color=ACCENT_HOVER,
            unselected_color=CARD_2, unselected_hover_color=CARD_HOVER, fg_color=CARD_2, text_color=TEXT,
        )
        seg.set(label_for(table, getattr(self.settings, attr)))
        seg.grid(row=row, column=1, sticky="e", padx=(0, 12), pady=4)
        self._option_widgets.append(lambda: seg.set(label_for(table, getattr(self.settings, attr))))

    def _switch_row(self, panel, text: str, attr: str) -> None:
        def changed() -> None:
            setattr(self.settings, attr, bool(sw.get()))
            self._settings_changed()

        sw = ctk.CTkSwitch(
            panel, text=text, command=changed, font=self.font(12), text_color=TEXT, progress_color=ACCENT,
            onvalue=1, offvalue=0,
        )
        sw.grid(row=self._next_row(), column=0, columnspan=2, sticky="w", padx=14, pady=6)

        def sync() -> None:
            sw.select() if getattr(self.settings, attr) else sw.deselect()

        sync()
        self._option_widgets.append(sync)

    def _color_row(self, panel, text: str) -> None:
        row = self._next_row()
        self._label(panel, text, row)
        holder = ctk.CTkFrame(panel, fg_color="transparent")
        holder.grid(row=row, column=1, sticky="e", padx=(0, 12), pady=4)
        for swatch in ("#000000", "#ffffff"):
            SoftButton(
                holder, text="", width=26, height=26, corner_radius=13, fg_color=swatch, hover_color=swatch,
                border_width=1, border_color=BORDER, command=lambda c=swatch: self._set_background(c),
            ).pack(side="left", padx=(0, 6))
        self.color_button = SoftButton(
            holder, text="", width=84, height=28, corner_radius=8, border_width=1, border_color=BORDER,
            font=self.font(11), command=self._pick_background,
        )
        self.color_button.pack(side="left")
        self._option_widgets.append(self._sync_color)
        self._sync_color()

    def _sync_color(self) -> None:
        color = "#" + engine._hex_color(self.settings.background)
        r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
        light = (r * 299 + g * 587 + b * 114) / 1000 > 140
        self.color_button.configure(
            text=color.upper(), fg_color=color, hover_color=color, text_color="#111111" if light else "#ffffff"
        )

    def _pick_background(self) -> None:
        from tkinter import colorchooser

        _, chosen = colorchooser.askcolor(color=self.settings.background, parent=self.root, title="Background colour")
        if chosen:
            self._set_background(chosen)

    def _set_background(self, color: str) -> None:
        self.settings.background = color
        self._sync_color()
        self._settings_changed()

    @property
    def _option_widgets(self) -> list:
        if not hasattr(self, "_option_syncers"):
            self._option_syncers: list = []
        return self._option_syncers

    def _settings_changed(self) -> None:
        self.settings.sanitize()
        self._save_settings()
        self._refresh_summary()
        self._sync_presets()

    def _save_settings(self) -> None:
        self._settings_writer.submit(self.settings)

    def _sync_presets(self) -> None:
        names = self.presets.names()
        self.preset_picker.configure(values=["Custom", *names])
        selected = self.preset_picker.get()
        ordered = ([selected] if selected in names else []) + [name for name in names if name != selected]
        match = next((name for name in ordered
                      if all(getattr(self.settings, field) == value for field, value in self.presets.get(name).items())), "Custom")
        self.preset_picker.set(match)
        self.preset_description.configure(text=BUILTIN_DESCRIPTIONS.get(match,
            "Your conversion options. Save them as a preset to use again." if match == "Custom"
            else "Your saved conversion options, kept on this computer."))
        self.delete_preset_button.configure(state="normal" if match in names and not self.presets.is_builtin(match) else "disabled")

    def _apply_preset(self, name: str) -> None:
        if name == "Custom":
            self._sync_presets()
            return
        try:
            self.presets.apply(self.settings, name)
        except PresetError as exc:
            self._set_status(str(exc), error=True)
            return
        for sync in self._option_widgets:
            sync()
        self.preset_picker.set(name)
        self._settings_changed()
        self._set_status(f"Applied {name}. Existing trims and image durations are kept.")

    def _save_preset(self) -> None:
        dialog = ctk.CTkInputDialog(text="Name your conversion preset:", title="Save preset")
        name = dialog.get_input()
        if name is None:
            return
        overwrite = False
        if name.strip().casefold() in {value.casefold() for value in self.presets.names()}:
            if self.presets.is_builtin(name):
                self._set_status("Choose a different name; built-in presets are kept.", error=True)
                return
            overwrite = ask_confirmation(self.root, "Replace preset?", "Replace the saved options for this preset?",
                                         confirm_text="Replace preset", cancel_text="Keep existing")
            if not overwrite:
                return
        try:
            name = self.presets.save_current(name, self.settings, overwrite=overwrite)
        except PresetError as exc:
            self._set_status(str(exc), error=True)
            return
        self.preset_picker.set(name)
        self._sync_presets()
        self._set_status(f"Saved {name} on this computer.", ok=True)

    def _delete_preset(self) -> None:
        name = self.preset_picker.get()
        try:
            self.presets.delete(name)
        except PresetError as exc:
            self._set_status(str(exc), error=True)
            return
        self._sync_presets()
        self._set_status(f"Deleted preset {name}. Your current conversion options are kept.")

    def _apply_photo_seconds(self) -> None:
        stills = [it for it in self.items if not it.is_video]
        for it in stills:
            it.hold = self.settings.image_seconds
        if stills:
            for row in self.rows:
                if not row.item.is_video:
                    row._set(row.hold_entry, f"{row.item.hold:g}")
            self._refresh_summary()
            self._set_status(f"Set {len(stills)} image(s) to {self.settings.image_seconds:g} s.")
        else:
            self._set_status("No images in the list yet.")

    def _reset_options(self) -> None:
        self.settings.reset_options()
        for sync in self._option_widgets:
            sync()
        self._settings_changed()
        self._set_status("Options reset to defaults.")

    def _on_mode(self, label: str) -> None:
        self.settings.mode = "combine" if label.startswith("Combine") else "separate"
        self._settings_changed()

    # ── views ──

    def _show_view(self, name: str) -> None:
        if name == "list" and not self.items:
            name = "empty"
        for key, view in (("empty", self.empty_view), ("list", self.list_view), ("result", self.result_view)):
            if key == name:
                view.grid(row=0, column=0, sticky="nsew", padx=STAGE_CONTENT_INSET, pady=STAGE_CONTENT_INSET)
            else:
                view.grid_remove()
        if name != "result":
            self.player.stop()
        self.view = name
        self._refresh_summary()

    def _walk(self, widget):
        yield widget
        try:
            children = widget.winfo_children()
        except Exception:
            children = []
        for child in children:
            yield from self._walk(child)

    def _bind_drops(self, widget) -> None:
        if not self.dnd_files:
            return
        for w in self._walk(widget):
            try:
                w.drop_target_register(self.dnd_files)
                w.dnd_bind("<<Drop>>", self._on_drop)
                w.dnd_bind("<<DropEnter>>", self._on_drag_enter)
                w.dnd_bind("<<DropPosition>>", self._on_drag_enter)
                w.dnd_bind("<<DropLeave>>", self._on_drag_leave)
            except Exception:
                pass

    def _on_drag_enter(self, event):
        if self._can_accept_drop(event):
            self.stage.configure(border_color=ACCENT, border_width=2)
            return "copy"
        self.stage.configure(border_color=BORDER, border_width=1)
        return "refuse_drop"

    def _can_accept_drop(self, event) -> bool:
        actions = getattr(event, "actions", ("copy",)) or ()
        return (not self.busy and not self._closed and self.settings.accepted_terms == TERMS_VERSION
                and "copy" in actions)

    def _on_drag_leave(self, event):
        self.stage.configure(border_color=BORDER, border_width=1)
        return "copy" if self._can_accept_drop(event) else "refuse_drop"

    def _on_drop(self, event):
        self.stage.configure(border_color=BORDER, border_width=1)
        if not self._can_accept_drop(event):
            if self.busy:
                self._set_status("Please wait for the current GIF to finish.", error=True)
            return "refuse_drop"
        data = getattr(event, "data", "")
        if not isinstance(data, str) or not data or len(data) > MAX_DROP_DATA_CHARS:
            self._set_status("Drop local image or video files, or use Browse.", error=True)
            return "refuse_drop"
        try:
            parts = self.root.tk.splitlist(data)
        except Exception:
            self._set_status("Couldn't read those dropped files. Use Browse to add them.", error=True)
            return "refuse_drop"
        # splitlist already removes Tcl quoting. Stripping braces or whitespace
        # again corrupts valid filenames (including names containing braces).
        paths = [Path(p) for p in parts if p]
        if not paths:
            return "refuse_drop"
        if any(not path.is_absolute() or not is_local_path(path) for path in paths):
            self._set_status("Yui only opens files on local drives; network shares are disabled.", error=True)
            return "refuse_drop"
        self.add_paths(paths)
        return "copy"  # Import references the originals; never tell Explorer to move them.

    # ── adding & editing items ──

    def browse(self) -> None:
        if self.busy:
            return
        if self._toolbox is not None or self._designer is not None:
            self._set_status("Close the editor before adding more media.")
            return
        from tkinter import filedialog

        chosen = filedialog.askopenfilenames(parent=self.root, title="Add videos or images", filetypes=FILE_TYPES)
        if chosen:
            self.add_paths([Path(p) for p in chosen])

    def add_paths(self, paths: list[Path]) -> None:
        if self.busy or self._closed:
            return
        if getattr(self, "_toolbox", None) is not None or getattr(self, "_designer", None) is not None:
            self._set_status("Close the editor before adding more media.")
            return
        if any(not is_local_path(path) for path in paths):
            self._set_status("Yui only opens files on local drives; network shares are disabled.", error=True)
            return
        if not paths:
            return
        if self.view == "result":
            self._show_view("list")
        self.loading += 1
        token = (self._import_generation, self._import_sequence)
        self._import_sequence += 1
        self._refresh_summary()
        self._set_status("Reading media…")
        hold = self.settings.image_seconds
        threading.Thread(target=self._probe_worker, args=(paths, 0, hold, token), daemon=True).start()

    def _probe_worker(self, paths: list[Path], skipped: int, hold: float, token) -> None:
        def cancelled():
            return self._closed or token[0] != self._import_generation
        added: list[MediaItem] = []
        errors: list[str] = []
        expanded: list[Path] = []
        for path in paths:
            if cancelled():
                return
            try:
                if not is_local_path(path):
                    skipped += 1
                    continue
                if path.is_dir():
                    batch = []
                    for child in path.iterdir():
                        if cancelled():
                            return
                        if child.suffix.lower() in engine.MEDIA_EXTS and is_local_path(child) and child.is_file():
                            batch.append(child)
                        if len(batch) + len(expanded) >= MAX_MEDIA_ITEMS:
                            skipped += 1
                            break
                    expanded.extend(sorted(batch, key=lambda p: p.name.lower()))
                else:
                    expanded.append(path)
            except OSError:
                errors.append("A file or folder could not be read. Choose a local copy.")
            if len(expanded) >= MAX_MEDIA_ITEMS:
                expanded = expanded[:MAX_MEDIA_ITEMS]
                break
        media = [path for path in expanded if path.suffix.lower() in engine.MEDIA_EXTS]
        skipped += len(expanded) - len(media)
        try:
            ffmpeg = get_ffmpeg()
        except Exception:
            self._post(self._probe_done, [], ["The bundled converter is unavailable. Reinstall Yui."], skipped, token)
            return
        def read_media(path):
            if cancelled():
                return None, None
            holder = {"import_generation": token[0]}
            with self._background_lock:
                self._background_procs[id(holder)] = holder
            try:
                return engine.probe(path, ffmpeg=ffmpeg, hold=hold,
                                    should_cancel=cancelled, proc_holder=holder), None
            except Exception as exc:
                return None, str(exc) if isinstance(exc, engine.MediaError) else "A file could not be read. Try another local copy."
            finally:
                with self._background_lock:
                    self._background_procs.pop(id(holder), None)

        try:
            for item, error in self.pool.map(read_media, media):
                if cancelled():
                    return
                if item is not None:
                    added.append(item)
                elif error:
                    errors.append(error)
        except (RuntimeError, CancelledError):
            if cancelled():
                return
            raise
        if not media and not errors:
            errors.append("Those files aren't videos or images Yui can use.")
        self._post(self._probe_done, added, errors, skipped, token)

    def _probe_done(self, added: list[MediaItem], errors: list[str], skipped: int, token) -> None:
        if token[0] != self._import_generation:
            return
        self._import_results[token[1]] = (added, errors, skipped)
        while self._import_next in self._import_results:
            batch = self._import_results.pop(self._import_next)
            self._import_next += 1
            self.loading = max(0, self.loading - 1)
            self._apply_import(*batch)

    def _apply_import(self, added: list[MediaItem], errors: list[str], skipped: int) -> None:
        known = {os.path.normcase(os.path.abspath(item.path)) for item in self.items}
        unique = []
        for item in added:
            key = os.path.normcase(os.path.abspath(item.path))
            if key in known or len(self.items) + len(unique) >= MAX_MEDIA_ITEMS:
                skipped += 1
            else:
                known.add(key)
                unique.append(item)
        added = unique
        self.items.extend(added)
        for item in added:
            self.pool.submit(self._thumb_worker, item)
        if self.items:
            self._show_view("list")
        self._rebuild_rows()
        notes = []
        if added:
            notes.append(f"Added {len(added)} file{'s' if len(added) != 1 else ''}.")
        if errors:
            notes.append(f"Skipped {len(errors)}: {errors[0]}" + (" …" if len(errors) > 1 else ""))
        elif skipped:
            notes.append(f"Skipped {skipped} duplicate, unsupported or excess item(s); limit {MAX_MEDIA_ITEMS}.")
        self._set_status(" ".join(notes) or "Nothing added.", error=bool(errors) and not added)

    def cancel_import(self, quiet=False) -> None:
        generation = self._import_generation
        self._import_generation += 1
        self._import_sequence = self._import_next = self.loading = 0
        self._import_results.clear()
        with self._background_lock:
            holders = list(self._background_procs.values())
        for holder in holders:
            if holder.get("import_generation") == generation:
                proc = holder.get("proc")
                if proc and proc.poll() is None:
                    try:
                        proc.kill()
                    except OSError:
                        pass
        self._refresh_summary()
        if not quiet:
            self._set_status("Import cancelled. Files already in the list are kept.")

    def _thumb_worker(self, item: MediaItem) -> None:
        holder = {}
        with self._background_lock:
            self._background_procs[id(holder)] = holder
        try:
            item.thumb = engine.make_thumbnail(item, should_cancel=lambda: self._closed, proc_holder=holder)
        except Exception:
            return
        finally:
            with self._background_lock:
                self._background_procs.pop(id(holder), None)
        self._post(self._thumb_done, item)

    def _thumb_done(self, item: MediaItem) -> None:
        row = self._rows_by_item.get(id(item))
        if row is not None:
            try:
                row.set_thumb(item.thumb)
            except Exception:
                pass

    def _rebuild_rows(self) -> None:
        if self._rows_job is not None:
            self.root.after_cancel(self._rows_job)
            self._rows_job = None
        previous = {id(row.item): row for row in self.rows}
        current = {}
        pending = []
        for index, item in enumerate(self.items):
            row = previous.pop(id(item), None)
            if row is None:
                pending.append((index, item))
            else:
                row.update_position(index, len(self.items))
                if int(row.grid_info().get("row", -1)) != index:
                    row.grid(row=index, column=0, sticky="ew", padx=6, pady=4)
                current[id(item)] = row
        for row in previous.values():
            row.destroy()
        self._rows_by_item = current
        self.rows = [current[id(item)] for item in self.items if id(item) in current]
        self._building_rows = bool(pending)
        # Small edits stay immediate. Large imports yield between row creations
        # so clicking Clear, editing a ready row or moving the window still works.
        remaining = iter(pending)
        def build_chunk():
            self._rows_job = None
            if self._closed:
                return
            deadline = time.monotonic() + .025
            built = 0
            while True:
                pair = next(remaining, None)
                if pair is None:
                    self._building_rows = False
                    break
                index, item = pair
                row = ItemRow(self.list_frame, self, item, index, len(self.items))
                self._bind_drops(row)
                row.grid(row=index, column=0, sticky="ew", padx=6, pady=4)
                current[id(item)] = row
                built += 1
                if len(pending) > 8 and (built >= 6 or time.monotonic() >= deadline):
                    self._rows_job = self.root.after(16, build_chunk)
                    break
            self.rows = [current[id(item)] for item in self.items if id(item) in current]
            if not self._building_rows:
                self._refresh_summary()
        build_chunk()
        if self._building_rows:
            self._refresh_summary()

    def move_item(self, item: MediaItem, step: int) -> None:
        if self.busy:
            return
        i = self.items.index(item)
        j = i + step
        if 0 <= j < len(self.items):
            self.items[i], self.items[j] = self.items[j], self.items[i]
            self._rebuild_rows()

    def remove_item(self, item: MediaItem) -> None:
        if self.busy:
            return
        self.items.remove(item)
        self._rebuild_rows()
        if not self.items:
            self._show_view("empty")

    def clear_items(self) -> None:
        if self.busy:
            return
        self.cancel_import(quiet=True)
        self.items.clear()
        self.design_layers = ()
        self._rebuild_rows()
        self.results = []
        self._shown_result = None
        self.progress.grid_remove()
        self._show_view("empty")
        self._set_status("")

    def on_items_changed(self) -> None:
        if not self._committing_rows:
            self._refresh_summary()

    def _refresh_summary(self) -> None:
        cap = self.settings.render_options().cap_bytes / engine.MIB
        self.tagline.configure(text=f"Videos and images → GIFs that always fit in {cap:g} MB")
        n = len(self.items)
        can_design = bool(n and not self.busy and not self.loading and not self._building_rows)
        design_count = len(self.design_layers)
        self.design_button.configure(
            state="normal" if can_design else "disabled",
            text=f"Text & stickers ({design_count})…" if design_count else "Text & stickers / poster…",
        )
        self.result_design_button.configure(state="normal" if can_design else "disabled")
        self.toolbox_button.configure(state="normal" if can_design else "disabled")
        combine = self.settings.mode == "combine"
        self.count_label.configure(text=f"{n} item{'s' if n != 1 else ''}")
        if not self.busy:
            if self.loading:
                self.primary.configure(text="Cancel import")
            elif self._building_rows:
                self.primary.configure(text="Preparing list…")
            elif combine or n <= 1:
                self.primary.configure(text="Create GIF")
            else:
                self.primary.configure(text=f"Create {n} GIFs")
            self.primary.configure(state="disabled" if self._building_rows and not self.loading else "normal")
        if not n:
            self.summary.configure(text="")
            return
        opts = self.settings.render_options()
        try:
            if combine:
                plan = engine.estimate(self.items, opts)
                text = (
                    f"Total {fmt_duration(plan.total_seconds)}  ·  about {plan.width}×{plan.height} "
                    f"at {plan.fps:.0f} fps  ·  max {cap:g} MB"
                )
            else:
                text = f"{n} separate GIF{'s' if n != 1 else ''}  ·  each at most {cap:g} MB"
        except Exception:
            text = ""
        if combine and n > 1 and len({(it.width, it.height) for it in self.items}) > 1:
            text += "  ·  mixed sizes are " + ("cropped to fill" if opts.fit == "fill" else "letterboxed")
        if self.design_layers:
            text += f"  ·  {len(self.design_layers)} design layer{'s' if len(self.design_layers) != 1 else ''}"
        edits = []
        if opts.crop:
            edits.append("cropped")
        if opts.effect != "none":
            edits.append(opts.effect)
        if opts.reverse:
            edits.append("reversed")
        if opts.palette_colors != 256:
            edits.append(f"{opts.palette_colors} colors")
        if edits:
            text += "  ·  " + ", ".join(edits)
        self.summary.configure(text=text)

    def open_toolbox(self) -> None:
        if self.busy or self.loading or self._building_rows or self._save_copy_busy:
            self._set_status("Finish the current import, conversion or save before using editing tools.")
            return
        if not self.items:
            self.browse()
            return
        if self.settings.accepted_terms != TERMS_VERSION:
            self._terms_gate()
            return
        if self._designer is not None:
            self._designer.lift()
            self._designer.focus_set()
            return
        if self._toolbox is not None:
            self._toolbox.lift()
            self._toolbox.focus_set()
            return
        self._committing_rows = True
        try:
            for row in self.rows:
                row._commit_trim() if row.item.is_video else row._commit_hold()
        finally:
            self._committing_rows = False
        options = self.settings.render_options()
        options.overlays = self.design_layers
        try:
            from .toolbox import Toolbox
            self._toolbox = Toolbox(
                self.root, items=tuple(replace(item, thumb=None) for item in self.items), options=options,
                on_apply=self._apply_tool_options, on_split=self._split_clip,
                on_close=lambda: setattr(self, "_toolbox", None),
            )
        except Exception as exc:
            log_exception(type(exc), exc, exc.__traceback__)
            self._toolbox = None
            self._set_status("The editing tools could not open. Try a different local media file.", error=True)

    def _apply_tool_options(self, options) -> None:
        for field in ("crop", "effect", "reverse", "palette_colors", "palette_mode"):
            setattr(self.settings, field, getattr(options, field))
        self.settings.sanitize()
        for sync in self._option_widgets:
            sync()
        self._settings_changed()
        self._show_view("list")
        self._set_status("Edits applied. Choose Create GIF to export them.")

    def _split_clip(self, index, parts) -> None:
        if self.busy or self.loading or self._building_rows or not 0 <= index < len(self.items):
            raise engine.MediaError("Finish the current operation before splitting a clip.")
        if len(self.items) + len(parts) - 1 > MAX_MEDIA_ITEMS:
            raise engine.MediaError("The media list is full. Remove an item before splitting.")
        if len(parts) != 2 or any(part.path != self.items[index].path for part in parts):
            raise engine.MediaError("The selected source changed. Reopen the editing tools.")
        self.items[index:index + 1] = list(parts)
        self._rebuild_rows()
        self._show_view("list")
        self._set_status("Clip split into two editable segments. Originals are preserved.")

    def open_designer(self) -> None:
        if self.busy or self.loading or self._building_rows or self._save_copy_busy:
            self._set_status("Finish the current import, conversion or save before editing the design.")
            return
        if not self.items:
            self.browse()
            return
        if self.settings.accepted_terms != TERMS_VERSION:
            self._terms_gate()
            return
        if self._toolbox is not None:
            self._toolbox.lift()
            self._toolbox.focus_set()
            return
        if self._designer is not None:
            try:
                if self._designer.winfo_exists():
                    self._designer.lift()
                    self._designer.focus_set()
                    return
            except Exception:
                pass
            self._designer = None
        self._committing_rows = True
        try:
            for row in self.rows:
                row._commit_trim() if row.item.is_video else row._commit_hold()
        finally:
            self._committing_rows = False
        options = self.settings.render_options()
        source = replace(self.items[0], thumb=None)
        preview_items = self.items if self.settings.mode == "combine" else [source]
        try:
            from .designer import Designer
            plan = engine.estimate(preview_items, options)
            self._designer = Designer(
                self.root, source=source, options=options,
                canvas_size=(plan.width, plan.height), layers=self.design_layers,
                on_apply=self._apply_design, on_close=self._design_closed,
                protected_paths=tuple(item.path for item in self.items),
            )
        except Exception as exc:
            log_exception(type(exc), exc, exc.__traceback__)
            self._designer = None
            self._set_status("The designer could not open. Try a different local media file.", error=True)

    def _apply_design(self, layers) -> None:
        from .overlays import validate_layers
        self.design_layers = validate_layers(layers)
        self._show_view("list")
        if self.design_layers:
            self._set_status("Design applied. Choose Create GIF to export it, or reopen the designer to save a PNG poster.")
        else:
            self._set_status("Design cleared. The next GIF will use your media without overlays.")

    def _design_closed(self) -> None:
        self._designer = None

    # ── converting ──

    def on_primary(self) -> None:
        if self._toolbox is not None or self._designer is not None:
            self._set_status("Apply or close the editor before creating a GIF.")
            return
        if self.busy:
            self.cancel()
            return
        if self.loading:
            self.cancel_import()
            return
        if self._building_rows:
            return
        if not self.items:
            self.browse()
            return
        if self.settings.accepted_terms != TERMS_VERSION:
            self._terms_gate()
            return
        # FocusOut is queued by Tk and may arrive after the worker starts.
        self._committing_rows = True
        try:
            for row in self.rows:
                row._commit_trim() if row.item.is_video else row._commit_hold()
        finally:
            self._committing_rows = False
        self._refresh_summary()
        self.root.focus_set()
        self._start()

    def _start(self) -> None:
        if self._save_copy_busy:
            self._set_status("Finishing Save as… Please wait a moment.")
            return
        opts = self.settings.render_options()
        opts.overlays = self.design_layers
        out_dir = Path(self.settings.output_dir) if self.settings.output_dir else None
        if out_dir and not is_local_path(out_dir):
            self._set_status("Choose a local output folder in Settings.", error=True)
            return
        jobs: list[tuple[list[MediaItem], Path]] = []
        reserved: set[str] = set()

        def target(source: Path, count: int) -> Path:
            if self.settings.private_filenames:
                folder = out_dir if out_dir else engine.fallback_output_dir()
                source = folder / f"yui-{secrets.token_hex(6)}.source"
                count = 1
            path = engine.output_path(source, out_dir, count, self.settings.overwrite)
            n = 2
            base = path
            while str(path).lower() in reserved:
                path = engine.unique_path(base.with_name(f"{base.stem} ({n}){base.suffix}"))
                n += 1
            reserved.add(str(path).lower())
            return path

        snapshot = [replace(item, thumb=None) for item in self.items]
        try:
            if self.settings.mode == "combine" or len(snapshot) == 1:
                jobs.append((snapshot, target(snapshot[0].path, len(snapshot))))
            else:
                for item in snapshot:
                    jobs.append(([item], target(item.path, 1)))
        except (OSError, engine.MediaError):
            self._set_status("The output folder is unavailable. Choose a writable local folder in Settings.", error=True)
            return

        self.busy = True
        self.cancel_flag = False
        self.results = []
        self._shown_result = None
        self._show_view("list")
        self.primary.configure(text="Cancel", fg_color=CARD_2, hover_color=CARD_HOVER, text_color=TEXT)
        self.design_button.configure(state="disabled")
        self.result_design_button.configure(state="disabled")
        self.toolbox_button.configure(state="disabled")
        self.progress.set(0)
        self.progress.grid()
        self._set_status("Starting…")
        threading.Thread(target=self._render_worker, args=(jobs, opts, self.settings.overwrite), daemon=True).start()

    def _render_worker(self, jobs, opts: engine.RenderOptions, overwrite: bool) -> None:
        results: list[engine.RenderResult] = []
        errors: list[str] = []
        total = len(jobs)
        for k, (items, out) in enumerate(jobs):
            prefix = f"{k + 1}/{total} · " if total > 1 else ""

            def progress(frac: float, msg: str, k=k, prefix=prefix) -> None:
                self._post(self._ui_progress, (k + frac) / total, prefix + msg)

            try:
                results.append(
                    engine.render(
                        items, opts, out, progress=progress,
                        should_cancel=lambda: self.cancel_flag, proc_holder=self.proc_holder,
                        overwrite=overwrite,
                    )
                )
            except engine.RenderCancelled:
                self._post(self._ui_finished, results, ["Cancelled."], True)
                return
            except Exception as exc:
                if not isinstance(exc, engine.MediaError):
                    log_exception(type(exc), exc, exc.__traceback__)
                name = items[0].path.name if len(items) == 1 else "the combined GIF"
                message = str(exc) if isinstance(exc, engine.MediaError) else "Conversion failed. Try another local file or output folder."
                errors.append(f"Item {k + 1}: {message}" if total > 1 else message)
        self._post(self._ui_finished, results, errors, False)

    def _ui_progress(self, frac: float, message: str) -> None:
        self._stop_progress_animation()
        target = max(0.0, min(1.0, frac))
        if not self._system_animations:
            self.progress.set(target)
        else:
            initial = self.progress.get()
            started = time.monotonic()

            def step():
                self._progress_job = None
                if self._closed or not self.busy:
                    return
                position = min(1.0, (time.monotonic() - started) / 0.16)
                self.progress.set(initial + (target - initial) * (1.0 - (1.0 - position) ** 3))
                if position < 1.0:
                    self._progress_job = self.root.after(16, step)

            step()
        self._set_status(message)

    def _stop_progress_animation(self) -> None:
        if self._progress_job is not None:
            try:
                self.root.after_cancel(self._progress_job)
            except Exception:
                pass
            self._progress_job = None

    def _ui_finished(self, results: list[engine.RenderResult], errors: list[str], cancelled: bool) -> None:
        self._stop_progress_animation()
        self.busy = False
        self.primary.configure(fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=ACCENT_TEXT)
        self.progress.grid_remove()
        self.results = results
        self._refresh_summary()
        if cancelled and not results:
            self._set_status("Cancelled. Nothing was saved.")
            return
        if not results:
            self._set_status(errors[0] if errors else "Something went wrong.", error=True)
            return
        if self.settings.sound:
            try:
                import winsound

                winsound.MessageBeep(winsound.MB_OK)
            except Exception:
                pass
        self._show_results(results)
        saved = f"Saved {len(results)} GIF{'s' if len(results) != 1 else ''}"
        if errors:
            self._set_status(f"{saved}. {len(errors)} failed — {errors[0]}", error=True)
        elif len(results) == 1:
            r = results[0]
            folder = short_name(r.output.parent.name or str(r.output.parent), 38)
            self._set_status(f"{saved} · {fmt_size(r.size)} of {r.cap_bytes / engine.MIB:g} MB · {folder}",
                             ok=True)
        else:
            folder = short_name(results[0].output.parent.name or str(results[0].output.parent), 38)
            self._set_status(f"{saved} to {folder}", ok=True)
        if self.settings.open_when_done:
            self.open_result()

    def cancel(self) -> None:
        if not self.busy:
            return
        self.cancel_flag = True
        self._set_status("Cancelling…")
        proc = self.proc_holder.get("proc")
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    # ── results ──

    def _show_results(self, results: list[engine.RenderResult]) -> None:
        self._show_view("result")
        self._configure_result_picker(results, results[0])
        self.root.update_idletasks()
        self._preview(results[0])

    def _configure_result_picker(self, results, selected) -> None:
        self._result_labels = {}
        if len(results) == 1:
            self.result_title.configure(text="Your GIF is ready")
            self.result_picker.grid_remove()
        else:
            self.result_title.configure(text=f"{len(results)} GIFs are ready")
            names = [f"{index + 1} · {short_name(r.output.name, 26)}" for index, r in enumerate(results)]
            self._result_labels = dict(zip(names, results))
            self.result_picker.configure(values=names)
            label = next((label for label, result in self._result_labels.items() if result is selected), names[0])
            self.result_picker.set(label)
            self.result_picker.grid(row=0, column=2, sticky="e")

    def _on_pick_result(self, label: str) -> None:
        result = self._result_labels.get(label)
        if result is not None:
            self._preview(result)

    def _preview(self, result: engine.RenderResult) -> None:
        self._shown_result = result
        self.result_info.configure(
            text=f"{short_name(result.output.name, 50)}  ·  {fmt_size(result.size)} of {result.cap_bytes / engine.MIB:g} MB  ·  "
                 f"{result.width}×{result.height}  ·  {result.fps:.0f} fps  ·  {fmt_duration(result.seconds)}"
        )
        self.root.update_idletasks()  # settle the newly shown preview's size before decoding
        self.player.load(result.output)
        self._sync_playback_controls()

    def _sync_playback_controls(self) -> None:
        shown = self.view == "result"
        state = (shown, self.player.playable, self.player.paused, self.player.frames)
        if state == self._playback_state:
            return
        self._playback_state = state
        enabled = shown and self.player.playable
        self.pause_button.configure(text="Resume" if self.player.paused else "Pause",
                                    state="normal" if enabled else "disabled")
        self.restart_button.configure(state="normal" if enabled else "disabled")
        self.playback_status.configure(text=("Paused" if self.player.paused else "Playing") if enabled
                                      else "Still image" if shown and self.player.frames == 1 else "")

    def toggle_preview(self) -> None:
        if self.view == "result":
            self.player.resume() if self.player.paused else self.player.pause()
            self._sync_playback_controls()

    def restart_preview(self) -> None:
        if self.view == "result":
            self.player.restart()
            self._sync_playback_controls()

    def save_result_as(self) -> None:
        result = self._shown_result
        if self.view != "result" or result is None or self._save_copy_busy:
            return
        from tkinter import filedialog
        chosen = filedialog.asksaveasfilename(parent=self.root, title="Save GIF as",
            initialdir=str(result.output.parent), initialfile=result.output.name,
            defaultextension=".gif", filetypes=[("GIF image", "*.gif")])
        if not chosen:
            return
        target = Path(chosen)
        if not target.suffix:
            target = target.with_suffix(".gif")
        if target.suffix.lower() != ".gif":
            self._set_status("Use a .gif filename when saving the GIF.", error=True)
            return
        if not is_local_path(target):
            self._set_status("Choose a folder on a local drive; network shares are disabled.", error=True)
            return
        if any(target.resolve() == item.path.resolve() for item in self.items):
            self._set_status("Save to another filename so your input file is kept.", error=True)
            return
        overwrite = target.exists()
        self._save_copy_busy = True
        self.save_as_button.configure(state="disabled")
        self._set_status("Saving a copy…")
        def work():
            try:
                path = engine.save_copy(result.output, target, overwrite=overwrite, should_cancel=lambda: self._closed)
            except (engine.MediaError, engine.RenderCancelled) as exc:
                self._post(self._save_as_done, result, None, str(exc) or "Save cancelled.")
            except Exception as exc:
                log_exception(type(exc), exc, exc.__traceback__)
                self._post(self._save_as_done, result, None, "Could not save the copy. Check the output folder.")
            else:
                self._post(self._save_as_done, result, path, None)
        threading.Thread(target=work, daemon=True, name="Yui Save As").start()

    def _save_as_done(self, result, path, error) -> None:
        self._save_copy_busy = False
        self.save_as_button.configure(state="normal")
        if error:
            self._set_status(error, error=True)
            return
        saved = replace(result, output=path)
        self.results = [saved if current is result else current for current in self.results]
        if self.view == "result":
            selected = saved if self._shown_result is result else self._shown_result
            self._configure_result_picker(self.results, selected)
            if self._shown_result is result:
                self._preview(saved)
        self._set_status(f"Saved a copy as {short_name(path.name, 50)}. The original GIF is kept.", ok=True)

    def open_result(self) -> None:
        result = self._shown_result or (self.results[0] if self.results else None)
        if result and is_local_path(result.output) and result.output.suffix.lower() == ".gif" and result.output.is_file():
            try:
                os.startfile(result.output)  # type: ignore[attr-defined]
            except OSError:
                self._set_status("Couldn't open the GIF. Choose a GIF viewer or try opening it from its folder.", error=True)

    def show_result_in_folder(self) -> None:
        result = self._shown_result
        if result and is_local_path(result.output) and result.output.is_file():
            try:
                subprocess.Popen([system_executable("explorer"), "/select,", str(result.output)], env=child_environment(), **no_window())
            except (OSError, RuntimeError):
                self._set_status("Couldn't open the output folder.", error=True)

    def copy_result(self) -> None:
        result = self._shown_result
        if not result or not is_local_path(result.output) or not result.output.is_file():
            return
        try:
            copy_file(result.output, self.root.winfo_id())
            self._set_status("Copied GIF. Windows clipboard history and cloud sync are excluded.", ok=True)
        except OSError:
            self._set_status("Couldn't copy the GIF. The clipboard may be busy; try again.", error=True)

    # ── dialogs ──

    def _dialog(self, title: str, w: int, h: int) -> ctk.CTkToplevel:
        top = ctk.CTkToplevel(self.root, fg_color=BG)
        top.title(title)
        top.transient(self.root)
        self.root.update_idletasks()
        try:
            scaling = ctk.ScalingTracker.get_window_scaling(self.root)
        except Exception:
            scaling = 1.0
        # CTk geometry strings are in unscaled units; winfo_* values are physical pixels.
        rx, ry = self.root.winfo_rootx() / scaling, self.root.winfo_rooty() / scaling
        rw, rh = self.root.winfo_width() / scaling, self.root.winfo_height() / scaling
        sw, sh = self.root.winfo_screenwidth() / scaling, self.root.winfo_screenheight() / scaling
        w, h = min(w, int(sw * .94)), min(h, int(sh * .88))
        x = max(0, min(int(sw - w), int(rx + max(0, (rw - w) / 2))))
        y = max(0, min(int(sh - h - 40), int(ry + max(0, (rh - h) / 3))))
        top.geometry(f"{w}x{h}+{x}+{y}")
        top.after(250, lambda: set_window_icon(top))  # CTkToplevel sets its own icon after ~200 ms
        top.after(50, top.lift)
        top.after(60, top.focus_force)
        return top

    def _textbox(self, parent, text: str) -> ctk.CTkTextbox:
        box = ctk.CTkTextbox(
            parent, wrap="word", font=self.font(12), fg_color=CARD, text_color=TEXT, border_width=1,
            border_color=BORDER, corner_radius=12,
        )
        box.insert("1.0", text)
        box.configure(state="disabled")
        return box

    def _terms_gate(self) -> None:
        if getattr(self, "_gate", None) is not None and self._gate.winfo_exists():
            self._gate.lift()
            return
        top = self._dialog(f"Welcome to {APP_NAME}", 640, 600)
        self._gate = top
        top.grid_columnconfigure(0, weight=1)
        top.grid_rowconfigure(2, weight=1)
        top.protocol("WM_DELETE_WINDOW", self._decline_terms)

        ctk.CTkLabel(top, text=f"Welcome to {APP_NAME}", font=self.font(20, "bold"), text_color=TEXT,
                     anchor="w").grid(row=0, column=0, sticky="ew", padx=24, pady=(22, 2))
        ctk.CTkLabel(
            top, text="Before you start, please read and accept the Terms of Use and the Privacy Notice. "
                      "Yui is free and works offline. Your files stay on your computer.",
            font=self.font(12), text_color=MUTED, anchor="w", justify="left", wraplength=590,
        ).grid(row=1, column=0, sticky="ew", padx=24, pady=(0, 12))

        tabs = ctk.CTkTabview(top, fg_color=CARD_2, segmented_button_selected_color=ACCENT,
                              segmented_button_selected_hover_color=ACCENT_HOVER, text_color=TEXT)
        tabs.grid(row=2, column=0, sticky="nsew", padx=20)
        for name, key in (("Terms of Use", "terms"), ("Privacy Notice", "privacy")):
            tab = tabs.add(name)
            tab.grid_columnconfigure(0, weight=1)
            tab.grid_rowconfigure(0, weight=1)
            self._textbox(tab, legal.document(key)).grid(row=0, column=0, sticky="nsew")

        agree_var = ctk.BooleanVar(value=False)
        bottom = ctk.CTkFrame(top, fg_color="transparent")
        bottom.grid(row=3, column=0, sticky="ew", padx=24, pady=16)
        bottom.grid_columnconfigure(0, weight=1)
        accept = SoftButton(
            bottom, text="Agree & continue", width=160, height=38, corner_radius=10, fg_color=ACCENT,
            hover_color=ACCENT_HOVER, text_color=ACCENT_TEXT, text_color_disabled=DIM,
            font=self.font(13, "bold"), state="disabled", command=self._accept_terms,
        )
        ctk.CTkCheckBox(
            bottom, text="I have read and agree to the Terms of Use and the Privacy Notice",
            variable=agree_var, font=self.font(12), text_color=TEXT, fg_color=ACCENT, hover_color=ACCENT_HOVER,
            command=lambda: accept.configure(state="normal" if agree_var.get() else "disabled"),
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))
        SoftButton(
            bottom, text="Decline & exit", width=130, height=38, corner_radius=10, fg_color="transparent",
            hover_color=CARD_HOVER, border_width=1, border_color=BORDER, text_color=TEXT, font=self.font(12),
            command=self._decline_terms,
        ).grid(row=1, column=1, padx=(0, 8))
        accept.grid(row=1, column=2)
        top.after(120, lambda: top.grab_set() if top.winfo_exists() else None)

    def _accept_terms(self) -> None:
        self.settings.accepted_terms = TERMS_VERSION
        self._save_settings()
        if self._gate is not None:
            self._gate.grab_release()
            self._gate.destroy()
            self._gate = None
        self._set_status("Thanks! Drop some videos or images to get started.")

    def _decline_terms(self) -> None:
        self._on_close()

    def _build_about_tabs(self, tabs: ctk.CTkTabview) -> None:
        about = tabs.add("About")
        about.grid_columnconfigure(0, weight=1)
        if self._logo_pil is not None:
            big = ctk.CTkImage(light_image=self._logo_pil, dark_image=self._logo_pil, size=(84, 84))
            ctk.CTkLabel(about, image=big, text="").grid(row=0, column=0, pady=(24, 10))
        ctk.CTkLabel(about, text=APP_NAME, font=self.font(22, "bold"), text_color=TEXT).grid(row=1, column=0)
        ctk.CTkLabel(about, text=f"Version {VERSION}", font=self.font(12), text_color=MUTED).grid(row=2, column=0)
        ctk.CTkLabel(
            about,
            text="Free video and image conversion, with GIFs up to 10 MB.\n\n"
                 "Made by Yui. Files are processed on your computer.\n"
                 "No sign-in, uploads or tracking.",
            font=self.font(13), text_color=TEXT, wraplength=520, justify="center",
        ).grid(row=3, column=0, pady=(16, 16))
        ctk.CTkLabel(
            about, text=f"© {YEAR} {AUTHOR}. All rights reserved.\nUses FFmpeg (GPLv3) and other open-source "
                        "software — see Licenses.",
            font=self.font(11), text_color=DIM, justify="center",
        ).grid(row=6, column=0, pady=(24, 10))

        documents = {}
        for name, key in (("Terms", "terms"), ("Privacy", "privacy")):
            tab = tabs.add(name)
            tab.grid_columnconfigure(0, weight=1)
            tab.grid_rowconfigure(0, weight=1)
            documents[name] = (tab, key)

        lic = tabs.add("Licenses")
        lic.grid_columnconfigure(0, weight=1)
        lic.grid_rowconfigure(1, weight=1)
        files = legal.license_files()
        source_choice = "FFmpeg source"
        choices = ["Open-source credits", source_choice] + [name for name, _ in files]
        box = self._textbox(lic, "")
        box.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        cache = {}

        def show(choice: str) -> None:
            if choice == source_choice:
                source_controls.grid()
            else:
                source_controls.grid_remove()
            if choice not in cache:
                path = next((path for name, path in files if name == choice), None)
                if choice == source_choice:
                    cache[choice] = (
                        "FFmpeg component source\n\n"
                        "Save the exact source, build materials and notices for the bundled FFmpeg "
                        "and its enabled libraries. This works offline.\n\n"
                        "Yui's editable application project is not included.\n\n"
                        "The complete EXE also contains Sources/FFmpeg-corresponding-source.zip "
                        "in a standard ZIP archive that can be read without running the app."
                    )
                else:
                    cache[choice] = legal.read_license(path) if path else legal.document("notices")
            box.configure(state="normal")
            box.delete("1.0", "end")
            box.insert("1.0", cache[choice])
            box.yview_moveto(0)
            box.configure(state="disabled")

        picker = ctk.CTkOptionMenu(
            lic, values=choices, command=show, width=320, height=30, corner_radius=8, fg_color=CARD,
            button_color=CARD, button_hover_color=CARD_HOVER, text_color=TEXT, font=self.font(12),
            dropdown_font=self.font(12), dynamic_resizing=False,
        )
        picker.grid(row=0, column=0, sticky="w")
        self._license_picker = picker
        source_controls = ctk.CTkFrame(lic, fg_color="transparent")
        source_controls.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        source_controls.grid_columnconfigure(0, weight=1)
        self._source_status = ctk.CTkLabel(
            source_controls, text="Saving source ZIP…" if self._source_export_busy else "",
            font=self.font(12), text_color=MUTED, anchor="w", justify="left", wraplength=600,
        )
        self._source_status.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        source_actions = ctk.CTkFrame(source_controls, fg_color="transparent")
        source_actions.grid(row=1, column=0, sticky="w")
        self._source_buttons = []
        for title, kind in (("Save FFmpeg source…", "ffmpeg"),):
            button = SoftButton(
                source_actions, text=title, width=190, height=36, corner_radius=8,
                fg_color=CARD, hover_color=CARD_HOVER, text_color=TEXT, font=self.font(12),
                command=lambda kind=kind: self._save_bundled_source(kind),
                state="disabled" if self._source_export_busy else "normal",
            )
            button.pack(side="left", padx=(0, 8))
            self._source_buttons.append(button)
        source_controls.grid_remove()
        self._license_source_controls = source_controls
        loaded = set()

        def load_tab():
            name = tabs.get()
            if name in loaded:
                return
            if name in documents:
                tab, key = documents[name]
                self._textbox(tab, legal.document(key)).grid(row=0, column=0, sticky="nsew")
            elif name == "Licenses":
                show(picker.get())
            loaded.add(name)

        tabs.configure(command=load_tab)

    def _save_bundled_source(self, kind: str) -> None:
        if self._source_export_busy:
            return
        from tkinter import filedialog
        chosen = filedialog.asksaveasfilename(
            parent=self._settings_window, title="Save bundled source code",
            initialfile=source_bundle.filename(kind), defaultextension=".zip",
            filetypes=[("ZIP archive", "*.zip")],
        )
        if not chosen:
            return
        self._source_export_busy = True
        self._source_status.configure(text="Saving source ZIP…", text_color=MUTED)
        for button in self._source_buttons:
            button.configure(state="disabled")

        def save():
            try:
                source_bundle.export_source(Path(chosen), kind, overwrite=True, cancelled=lambda: self._closed)
            except source_bundle.SourceExportError as exc:
                self._post(self._source_export_finished, str(exc), True)
            else:
                self._post(self._source_export_finished, "Source ZIP saved.", False)

        self.pool.submit(save)

    def _source_export_finished(self, text: str, error: bool) -> None:
        self._source_export_busy = False
        if self._source_status is not None and self._source_status.winfo_exists():
            self._source_status.configure(text=text, text_color=ERROR if error else SUCCESS)
        for button in self._source_buttons:
            if button.winfo_exists():
                button.configure(state="normal")

    def open_settings(self) -> None:
        existing = getattr(self, "_settings_window", None)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            existing.focus_force()
            return
        top = self._dialog("Settings", 720, 660)
        self._settings_window = top
        top.grid_columnconfigure(0, weight=1)
        top.grid_rowconfigure(0, weight=1)
        tabs = ctk.CTkTabview(top, fg_color=CARD_2, segmented_button_selected_color=ACCENT,
                              segmented_button_selected_hover_color=ACCENT_HOVER, text_color=TEXT)
        tabs.grid(row=0, column=0, sticky="nsew", padx=18, pady=(10, 0))
        general = tabs.add("General")
        general.grid_columnconfigure(0, weight=1)
        general.grid_rowconfigure(0, weight=1)
        frame = ctk.CTkScrollableFrame(general, fg_color=CARD, corner_radius=16,
                                     border_width=1, border_color=BORDER)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.grid_columnconfigure(1, weight=1)
        r = 0

        def heading(text: str) -> None:
            nonlocal r
            ctk.CTkLabel(frame, text=text.upper(), font=self.font(11, "bold"), text_color=DIM, anchor="w").grid(
                row=r, column=0, columnspan=2, sticky="ew", padx=16, pady=(16 if r == 0 else 18, 6)
            )
            r += 1

        heading("Appearance")
        theme = ctk.CTkSegmentedButton(
            frame, values=[l for l, _ in THEMES], font=self.font(12), selected_color=ACCENT,
            selected_hover_color=ACCENT_HOVER, unselected_color=CARD_2, unselected_hover_color=CARD_HOVER,
            fg_color=CARD_2, text_color=TEXT,
            command=lambda label: self._set_theme(dict(THEMES)[label]),
        )
        theme.set(label_for(THEMES, self.settings.theme))
        theme.grid(row=r, column=0, columnspan=2, sticky="w", padx=16)
        r += 1
        motion = ctk.CTkSwitch(frame, text="Subtle button animations", font=self.font(12), text_color=TEXT,
                              progress_color=ACCENT)
        motion.select() if self.settings.button_animations else motion.deselect()
        motion.configure(command=lambda: (setattr(self.settings, "button_animations", bool(motion.get())),
                                          self._save_settings()))
        motion.grid(row=r, column=0, columnspan=2, sticky="w", padx=16, pady=(10, 0))
        r += 1

        heading("Save GIFs to")
        folder_label = ctk.CTkLabel(frame, text="", font=self.font(12), text_color=TEXT, anchor="w",
                                    wraplength=330, justify="left")

        def sync_folder() -> None:
            folder_label.configure(text=short_name(self.settings.output_dir, 64)
                                   if self.settings.output_dir else "Local GIFs folder: %LOCALAPPDATA%\\YuiGifConverter\\GIFs")

        def choose() -> None:
            from tkinter import filedialog

            chosen = filedialog.askdirectory(parent=top, title="Save GIFs to")
            if chosen:
                if not is_local_path(Path(chosen)):
                    self._set_status("Choose a folder on a local drive; network shares are disabled.", error=True)
                    return
                self.settings.output_dir = chosen
                self._save_settings()
                sync_folder()

        def reset_folder() -> None:
            self.settings.output_dir = ""
            self._save_settings()
            sync_folder()

        folder_label.grid(row=r, column=0, columnspan=2, sticky="ew", padx=16)
        r += 1
        buttons = ctk.CTkFrame(frame, fg_color="transparent")
        buttons.grid(row=r, column=0, columnspan=2, sticky="w", padx=12, pady=(6, 0))
        for text, cmd in (("Choose folder…", choose), ("Use local folder", reset_folder)):
            SoftButton(
                buttons, text=text, width=140, height=30, corner_radius=8, fg_color=CARD_2, hover_color=CARD_HOVER,
                text_color=TEXT, font=self.font(12), command=cmd,
            ).pack(side="left", padx=4)
        r += 1
        sync_folder()

        heading("When converting")
        for text, attr in (
            ("Replace a GIF with the same name (otherwise add a number)", "overwrite"),
            ("Open the GIF when it's finished", "open_when_done"),
            ("Play a sound when it's finished", "sound"),
        ):
            sw = ctk.CTkSwitch(frame, text=text, font=self.font(12), text_color=TEXT, progress_color=ACCENT)
            sw.configure(command=lambda s=sw, a=attr: (setattr(self.settings, a, bool(s.get())), self._save_settings()))
            sw.select() if getattr(self.settings, attr) else sw.deselect()
            sw.grid(row=r, column=0, columnspan=2, sticky="w", padx=16, pady=4)
            r += 1

        heading("Privacy")
        names = ctk.CTkSwitch(frame, text="Use anonymous GIF filenames", font=self.font(12),
                             text_color=TEXT, progress_color=ACCENT)
        names.select() if self.settings.private_filenames else names.deselect()
        names.configure(command=lambda: (setattr(self.settings, "private_filenames", bool(names.get())), self._save_settings()))
        names.grid(row=r, column=0, columnspan=2, sticky="w", padx=16, pady=4)
        r += 1
        ctk.CTkLabel(frame, text="GIF names omit source names. Visible content and the public creator links remain.",
                     font=self.font(11), text_color=MUTED, wraplength=520, justify="left", anchor="w").grid(
                         row=r, column=0, columnspan=2, sticky="ew", padx=16, pady=(0, 8))
        r += 1

        heading("Windows")
        sendto = ctk.CTkSwitch(frame, text="Show in Explorer's “Send to” menu", font=self.font(12),
                               text_color=TEXT, progress_color=ACCENT)
        sendto.configure(command=lambda: self._set_sendto(bool(sendto.get())))
        link = self._sendto_path()
        local_sendto = is_local_path(link)
        sendto.select() if local_sendto and link.exists() else sendto.deselect()
        if not local_sendto:
            sendto.configure(state="disabled", text="The “Send to” menu requires a local Windows profile")
        elif not is_frozen():
            sendto.configure(state="disabled", text="Show in Explorer's “Send to” menu (available in the .exe)")
        sendto.grid(row=r, column=0, columnspan=2, sticky="w", padx=16, pady=(4, 16))
        r += 1

        self._build_about_tabs(tabs)
        tabs.set("General")

        SoftButton(
            top, text="Done", width=100, height=34, corner_radius=10, fg_color=ACCENT, hover_color=ACCENT_HOVER,
            text_color=ACCENT_TEXT, font=self.font(12, "bold"), command=top.destroy,
        ).grid(row=1, column=0, sticky="e", padx=20, pady=14)

    def _set_theme(self, theme: str) -> None:
        self.settings.theme = theme
        self._save_settings()
        ctk.set_appearance_mode(theme)

    @staticmethod
    def _sendto_path() -> Path:
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "Microsoft" / "Windows" / "SendTo" / f"{APP_NAME}.lnk"

    def _set_sendto(self, enabled: bool) -> None:
        link = self._sendto_path()
        if not is_local_path(link):
            self._set_status("The “Send to” menu requires a local Windows profile.", error=True)
            return
        if not enabled:
            try:
                link.unlink()
            except OSError:
                pass
            self._set_status("Removed from the “Send to” menu.")
            return
        exe = sys.executable.replace("'", "''")
        lnk = str(link).replace("'", "''")
        script = (
            f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');"
            f"$s.TargetPath='{exe}';$s.IconLocation='{exe},0';$s.Description='{APP_NAME}';$s.Save()"
        )
        try:
            proc = subprocess.run(
                [system_executable("powershell"), "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, timeout=10, env=child_environment(), **no_window()
            )
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            self._set_status("Couldn't update the “Send to” shortcut.", error=True)
            return
        if proc.returncode == 0:
            self._set_status("Added. Right-click files → Send to → Yui Gif Converter.", ok=True)
        else:
            self._set_status("Couldn't add the “Send to” shortcut.", error=True)

    # ── plumbing ──

    def _post(self, fn, *args) -> None:
        if not self._closed:
            self.ui_q.put((fn, args))

    def _pump(self) -> None:
        deadline = time.monotonic() + 0.008
        try:
            while time.monotonic() < deadline:
                fn, args = self.ui_q.get_nowait()
                try:
                    fn(*args)
                except Exception:
                    log_exception(*sys.exc_info())
        except queue.Empty:
            pass
        if self._closed:
            return
        self._sync_playback_controls()
        try:
            self.root.after(8 if not self.ui_q.empty() else 16, self._pump)
        except Exception:
            pass

    def _set_status(self, text: str, error: bool = False, ok: bool = False) -> None:
        self.status.configure(text=text, text_color=ERROR if error else SUCCESS if ok else MUTED)

    def _on_escape(self) -> None:
        if self.busy:
            self.cancel()
        elif self.loading:
            self.cancel_import()
        elif self.view == "result":
            self._show_view("list")

    def _on_tk_error(self, exc_type, exc, tb) -> None:
        log_exception(exc_type, exc, tb)
        try:
            self._set_status("Something went wrong. Try again; technical details were saved without personal paths.", error=True)
        except Exception:
            pass

    def _on_close(self) -> None:
        if self._toolbox is not None:
            try:
                if not self._toolbox.close():
                    return
            except Exception:
                pass
            self._toolbox = None
        if self._designer is not None:
            try:
                if not self._designer.close():
                    return
            except Exception:
                pass
            self._designer = None
        self._closed = True
        if self._rows_job is not None:
            self.root.after_cancel(self._rows_job)
            self._rows_job = None
        self._stop_progress_animation()
        with self._background_lock:
            background = list(self._background_procs.values())
        for holder in background:
            proc = holder.get("proc")
            if proc and proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
        self.cancel_flag = True
        proc = self.proc_holder.get("proc")
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        self.player.stop()
        self._settings_writer.close(self.settings)
        self.pool.shutdown(wait=False, cancel_futures=True)
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self) -> None:
        self.root.mainloop()
