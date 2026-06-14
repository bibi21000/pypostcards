#!/usr/bin/env python3
"""
tkscan - Batch scanning application for postcards.
Supports SANE on Linux and WIA/TWAIN on Windows.
"""

import os
import re
import sys
import time
import threading
import configparser
import gettext
import locale
import logging
import subprocess
import tempfile
from pathlib import Path
from datetime import datetime

import click
from PIL import Image, ImageTk
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from . import cli

# ──────────────────────────────────────────────────────────────────────────────
# Paths & i18n setup
# ──────────────────────────────────────────────────────────────────────────────

APP_DIR = Path(__file__).parent.resolve()
TRANSLATIONS_DIR = APP_DIR / "translations"
CONFIG_FILE = APP_DIR / "postcards.conf"


I18N_DOMAIN = "tkpostcards"


def setup_i18n(lang: str | None = None) -> gettext.NullTranslations:
    """Return a translation object for the requested language."""
    if lang is None:
        lc, _ = locale.getdefaultlocale()
        lang = (lc or "en")[:2]
    try:
        translation = gettext.translation(
            I18N_DOMAIN,
            localedir=str(TRANSLATIONS_DIR),
            languages=[lang],
        )
    except FileNotFoundError:
        translation = gettext.NullTranslations()
    return translation


# ──────────────────────────────────────────────────────────────────────────────
# Scanner back-end
#   Linux   : subprocess -> scanimage (supports escl, airscan, epson2, pixma...)
#   Windows : WIA via pywin32
# ──────────────────────────────────────────────────────────────────────────────

# ---------- device listing ---------------------------------------------------

def list_scanners() -> list[str]:
    """Return device strings of all available scanners."""
    if sys.platform.startswith("win"):
        return _list_scanners_windows()
    return _list_scanners_scanimage()


def _list_scanners_scanimage() -> list[str]:
    """
    Parse `scanimage -L` output.
    Each line looks like:
      device `escl:https://192.168.1.28:443' is a Brother MFC-L3740CDW ...
    We return the device string inside the backticks (e.g. escl:https://...).
    """
    try:
        result = subprocess.run(
            ["scanimage", "-L"],
            capture_output=True, text=True, timeout=20
        )
        devices = []
        for line in result.stdout.splitlines():
            m = re.match(r"device\s+`([^']+)'", line)
            if m:
                devices.append(m.group(1))
        return devices
    except FileNotFoundError:
        logging.warning("scanimage not found - install sane-utils")
        return []
    except subprocess.TimeoutExpired:
        logging.warning("scanimage -L timed out")
        return []
    except Exception as exc:
        logging.warning("list_scanners error: %s", exc)
        return []


def _list_scanners_windows() -> list[str]:
    try:
        import win32com.client  # type: ignore
        wia = win32com.client.Dispatch("WIA.DeviceManager")
        return [d.Properties("Name").Value for d in wia.DeviceInfos]
    except Exception:
        return []


# ---------- scanning ---------------------------------------------------------

# Map our format names -> scanimage --format values
_SCANIMAGE_FMT = {
    "tiff": "tiff",
    "png":  "png",
    "jpeg": "jpeg",
}


def do_scan(scanner_name: str, resolution: int, fmt: str, dest_path: Path,
            scan_area=None, crop_border: int = 0,
            jpeg_quality: int = 85, png_compress: int = 6,
            tiff_compression: str = "deflate") -> Path:
    """Perform a scan and save to *dest_path*. Returns the saved path."""
    if sys.platform.startswith("win"):
        return _scan_windows(scanner_name, resolution, fmt, dest_path)
    return _scan_scanimage(scanner_name, resolution, fmt, dest_path,
                            scan_area=scan_area, crop_border=crop_border,
                            jpeg_quality=jpeg_quality, png_compress=png_compress,
                            tiff_compression=tiff_compression)


def _scan_scanimage(scanner_name: str, resolution: int, fmt: str, dest_path: Path,
                     scan_area=None, crop_border: int = 0,
                     jpeg_quality: int = 85, png_compress: int = 6,
                     tiff_compression: str = "deflate") -> Path:
    """
    Use `scanimage` CLI to acquire the image.
    scanimage writes PNM/TIFF/PNG/JPEG directly; we let Pillow re-encode
    for formats that scanimage may not support natively (e.g. TIFF with LZW).
    """
    si_fmt = _SCANIMAGE_FMT.get(fmt.lower(), "tiff")

    # scanimage can write directly to a file with --output-file (sane-utils >= 1.0.27)
    # Fall back to stdout for older versions.
    with tempfile.NamedTemporaryFile(suffix=f".{si_fmt}", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    cmd = [
        "scanimage",
        f"--device-name={scanner_name}",
        f"--resolution={resolution}",
        "--mode=Color",
        f"--format={si_fmt}",
        f"--output-file={tmp_path}",
    ]
    # Inject scan-area options when provided
    if scan_area:
        left, t, x, y = scan_area
        cmd += [f"-l {left}", f"-t {t}", f"-x {x}", f"-y {y}"]
    logging.debug("scanimage cmd: %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            # Some backends ignore --output-file; retry via stdout
            if not tmp_path.exists() or tmp_path.stat().st_size == 0:
                result2 = subprocess.run(
                    cmd[:-1],          # drop --output-file
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    timeout=120
                )
                if result2.returncode != 0:
                    raise RuntimeError(
                        result2.stderr.decode(errors="replace").strip()
                        or f"scanimage exit {result2.returncode}"
                    )
                tmp_path.write_bytes(result2.stdout)

        if not tmp_path.exists() or tmp_path.stat().st_size == 0:
            raise RuntimeError("scanimage produced an empty file")

        # Re-encode via Pillow (ensures correct format / compression)
        img = Image.open(str(tmp_path))
        img.load()
        if crop_border > 0:
            img = _crop_border(img, crop_border)
        _save_image(img, fmt, dest_path,
                     jpeg_quality=jpeg_quality,
                     png_compress=png_compress,
                     tiff_compression=tiff_compression)
        return dest_path

    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


def _scan_windows(scanner_name: str, resolution: int, fmt: str, dest_path: Path) -> Path:
    try:
        import win32com.client  # type: ignore
        wia = win32com.client.Dispatch("WIA.DeviceManager")
        device = None
        for info in wia.DeviceInfos:
            if info.Properties("Name").Value == scanner_name:
                device = info.Connect()
                break
        if device is None:
            raise RuntimeError("Scanner not found")
        item = device.Items[1]
        item.Properties("Horizontal Resolution").Value = resolution
        item.Properties("Vertical Resolution").Value = resolution
        image = item.Transfer()
        tmp = str(dest_path.with_suffix(".bmp"))
        image.SaveFile(tmp)
        img = Image.open(tmp)
        _save_image(img, fmt, dest_path)
        os.remove(tmp)
    except Exception as exc:
        raise RuntimeError(f"WIA scan error: {exc}") from exc
    return dest_path


def _auto_crop(img, threshold=240, min_margin=2):
    """
    Crop uniform light borders (white/near-white) from all four sides.
    threshold  : pixels with R,G,B all >= this value are background.
    min_margin : keep at least this many pixels on each side.
    """
    import numpy as np
    rgb = img.convert("RGB")
    arr = np.array(rgb, dtype=np.uint8)
    bg   = np.all(arr >= threshold, axis=2)
    rows = np.any(~bg, axis=1)
    cols = np.any(~bg, axis=0)
    if not rows.any() or not cols.any():
        return img
    top    = max(int(rows.argmax())              - min_margin, 0)
    bottom = min(int(len(rows) - rows[::-1].argmax()) + min_margin, img.height)
    left   = max(int(cols.argmax())              - min_margin, 0)
    right  = min(int(len(cols) - cols[::-1].argmax()) + min_margin, img.width)
    cropped = img.crop((left, top, right, bottom))
    logging.debug("auto_crop: %s -> %s", img.size, cropped.size)
    return cropped


def _crop_border(img: Image.Image, px: int) -> Image.Image:
    """Remove *px* pixels on every side of *img*."""
    w, h = img.size
    px = min(px, w // 2 - 1, h // 2 - 1)   # safety: never crop more than half
    if px <= 0:
        return img
    cropped = img.crop((px, px, w - px, h - px))
    logging.debug("crop_border %dpx: %s -> %s", px, img.size, cropped.size)
    return cropped


def _save_image(img: Image.Image, fmt: str, dest_path: Path,
                jpeg_quality: int = 85, png_compress: int = 6,
                tiff_compression: str = "deflate") -> None:
    fmt_upper = fmt.upper()
    if fmt_upper == "JPEG":
        img.save(str(dest_path), format="JPEG", quality=jpeg_quality, optimize=True)
    elif fmt_upper == "PNG":
        img.save(str(dest_path), format="PNG", compress_level=png_compress, optimize=True)
    elif fmt_upper == "TIFF":
        img.save(str(dest_path), format="TIFF", compression=tiff_compression)
    else:
        img.save(str(dest_path))


def simulate_scan(dest_path: Path, fmt: str) -> Path:
    """Create a dummy coloured image when no scanner is available."""
    import random
    colours = ["#D4A5A5", "#A5D4A5", "#A5A5D4", "#D4D4A5", "#D4A5D4"]
    bg = random.choice(colours)
    img = Image.new("RGB", (1748, 1240), bg)
    from PIL import ImageDraw, ImageFont
    draw = ImageDraw.Draw(img)
    text = f"SIMULATED SCAN\n{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 60)
    except Exception:
        font = ImageFont.load_default()
    draw.text((60, 560), text, fill="white", font=font)
    _save_image(img, fmt, dest_path)
    return dest_path


# ──────────────────────────────────────────────────────────────────────────────
# Config helpers
# ──────────────────────────────────────────────────────────────────────────────

# Default values used when a key is missing from the [tkscan] section of
# postcards.conf.
DEFAULT_CONFIG = {
    "scanner": "",
    "resolution": "300",
    "file_format": "tiff",
    "prefix": "scanned",
    "batch_interval": "30",
    "language": "",
    "scan_area_enabled": "false",
    "scan_area_left": "0",
    "scan_area_top": "0",
    "scan_area_width": "148",
    "scan_area_height": "105",
    "crop_border": "0",
    "jpeg_quality": "85",
    "png_compress": "6",
    "tiff_compression": "deflate",
}


def load_config() -> configparser.ConfigParser:
    """Load postcards.conf and ensure a [tkscan] section with defaults exists.

    The destination folder for scanned images is not part of [tkscan]: it
    comes from the [DEFAULT] "importdir" setting, inherited automatically
    by every section thanks to configparser.
    """
    cfg = configparser.ConfigParser()
    if CONFIG_FILE.exists():
        cfg.read(str(CONFIG_FILE))
    if not cfg.has_section("tkscan"):
        cfg.add_section("tkscan")
    for key, value in DEFAULT_CONFIG.items():
        if not cfg.has_option("tkscan", key):
            cfg.set("tkscan", key, value)
    return cfg


def save_config(cfg: configparser.ConfigParser) -> None:
    with open(str(CONFIG_FILE), "w", encoding="utf-8") as f:
        cfg.write(f)


# ──────────────────────────────────────────────────────────────────────────────
# Thumbnail window
# ──────────────────────────────────────────────────────────────────────────────

THUMB_SIZE = (160, 110)


class ThumbnailWindow(tk.Toplevel):
    """Independent window showing thumbnails of scanned images."""

    def __init__(self, parent: tk.Tk, gettext_func: callable) -> None:
        super().__init__(parent)
        self._ = gettext_func
        self.title(self._("Scanned Images"))
        self.geometry("900x520")
        self.minsize(400, 300)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._images: list[tuple[Path, ImageTk.PhotoImage]] = []
        self._photo_refs: list[ImageTk.PhotoImage] = []  # keep refs alive

        self._build_ui()

    def _build_ui(self) -> None:
        toolbar = ttk.Frame(self)
        toolbar.pack(fill=tk.X, padx=6, pady=4)
        self._count_var = tk.StringVar(value=f"{self._('Total scanned:')} 0")
        ttk.Label(toolbar, textvariable=self._count_var, font=("TkDefaultFont", 10, "bold")).pack(side=tk.LEFT)
        ttk.Label(toolbar, text=self._("(Double-click to enlarge)"), foreground="gray").pack(side=tk.RIGHT)

        container = ttk.Frame(self)
        container.pack(fill=tk.BOTH, expand=True)

        self._canvas = tk.Canvas(container, bg="#2b2b2b", highlightthickness=0)
        vsb = ttk.Scrollbar(container, orient=tk.VERTICAL, command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._frame = ttk.Frame(self._canvas)
        self._canvas_window = self._canvas.create_window((0, 0), window=self._frame, anchor="nw")
        self._frame.bind("<Configure>", self._on_frame_configure)
        self._canvas.bind("<Configure>", self._on_canvas_configure)

        self._empty_label = ttk.Label(self._frame, text=self._("No images yet"),
                                       foreground="gray", font=("TkDefaultFont", 14))
        self._empty_label.grid(row=0, column=0, padx=40, pady=40)

    def _on_frame_configure(self, _event=None) -> None:
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas_configure(self, event) -> None:
        self._canvas.itemconfig(self._canvas_window, width=event.width)
        self._relayout()

    def add_image(self, path: Path) -> None:
        """Add a new thumbnail (thread-safe via after())."""
        self.after(0, self._add_image_main, path)

    def _add_image_main(self, path: Path) -> None:
        try:
            img = Image.open(path)
            img.thumbnail(THUMB_SIZE, Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            self._images.append((path, photo))
            self._photo_refs.append(photo)
        except Exception as exc:
            logging.warning("Thumbnail error: %s", exc)
            return
        if len(self._images) == 1:
            self._empty_label.grid_remove()
        self._relayout()
        self._count_var.set(f"{self._('Total scanned:')} {len(self._images)}")

    def _relayout(self) -> None:
        cols = max(1, self._canvas.winfo_width() // (THUMB_SIZE[0] + 12))
        for widget in self._frame.winfo_children():
            widget.grid_forget()
        for idx, (path, photo) in enumerate(self._images):
            row, col = divmod(idx, cols)
            cell = ttk.Frame(self._frame, padding=0)
            cell.grid(row=row, column=col, padx=6, pady=6)
            lbl = tk.Label(cell, image=photo, bg="#2b2b2b", relief="flat", borderwidth=0, highlightthickness=0, cursor="hand2")
            lbl.image = photo  # extra ref
            lbl.pack()
            name = ttk.Label(cell, text=path.name[:22], foreground="#cccccc",
                              background="#2b2b2b", font=("TkDefaultFont", 8))
            name.pack()
            lbl.bind("<Double-Button-1>", lambda e, p=path: self._open_full(p))

    def _open_full(self, path: Path) -> None:
        try:
            img = Image.open(path)
        except Exception as exc:
            messagebox.showerror(self._("Error"), str(exc), parent=self)
            return
        win = tk.Toplevel(self)
        win.title(path.name)
        win.geometry("1024x768")
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        max_w, max_h = sw - 40, sh - 80
        img.thumbnail((max_w, max_h), Image.LANCZOS)
        photo = ImageTk.PhotoImage(img)
        lbl = tk.Label(win, image=photo, bg="black")
        lbl.image = photo
        lbl.pack(fill=tk.BOTH, expand=True)
        ttk.Button(win, text=self._("Close"), command=win.destroy).pack(pady=4)

    def _on_close(self) -> None:
        # Hide instead of destroy so it can be re-shown
        self.withdraw()


# ──────────────────────────────────────────────────────────────────────────────
# Main application window
# ──────────────────────────────────────────────────────────────────────────────

RESOLUTIONS = ["150", "300", "600", "1200"]
FILE_FORMATS = ["tiff", "png", "jpeg"]


class PostcardScannerApp(tk.Tk):
    """Main application window."""

    def __init__(self, cfg: configparser.ConfigParser, gettext_func: callable) -> None:
        super().__init__()
        self.cfg = cfg
        self._ = gettext_func

        self.title(self._("Postcard Scanner"))
        self.resizable(True, True)
        self.minsize(560, 480)

        # State
        self._scan_index = self._next_index()
        self._batch_running = False
        self._batch_paused = False
        self._batch_thread: threading.Thread | None = None
        self._stop_event  = threading.Event()
        self._pause_event = threading.Event()
        self._skip_event  = threading.Event()   # skip countdown -> scan now
        self._countdown_id: str | None = None
        self._remaining: int = 0

        self._build_ui()
        self._load_settings_to_ui()

        # Thumbnail window
        self._thumb_win = ThumbnailWindow(self, gettext_func)

        self.protocol("WM_DELETE_WINDOW", self._on_quit)
        self.after(200, self._refresh_scanners_bg)

    # ── UI construction ────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        _ = self._

        # ── Settings frame ────────────────────────────────────────────────
        settings_frame = ttk.LabelFrame(self, text=_("Settings"), padding=10)
        settings_frame.pack(fill=tk.X, padx=10, pady=(10, 4))
        settings_frame.columnconfigure(1, weight=1)

        row = 0
        # Scanner
        ttk.Label(settings_frame, text=_("Scanner:")).grid(row=row, column=0, sticky="w", pady=2)
        self._scanner_var = tk.StringVar()
        self._scanner_combo = ttk.Combobox(settings_frame, textvariable=self._scanner_var,
                                            state="readonly", width=38)
        self._scanner_combo.grid(row=row, column=1, sticky="ew", padx=(4, 0))
        ttk.Button(settings_frame, text=_("Refresh Scanners"),
                   command=self._refresh_scanners_bg).grid(row=row, column=2, padx=4)

        row += 1
        # Resolution
        ttk.Label(settings_frame, text=_("Resolution:")).grid(row=row, column=0, sticky="w", pady=2)
        self._resolution_var = tk.StringVar()
        ttk.Combobox(settings_frame, textvariable=self._resolution_var,
                     values=[f"{r} dpi" for r in RESOLUTIONS],
                     state="readonly", width=14).grid(row=row, column=1, sticky="w", padx=(4, 0))

        row += 1
        # File format
        ttk.Label(settings_frame, text=_("File format:")).grid(row=row, column=0, sticky="w", pady=2)
        self._format_var = tk.StringVar()
        ttk.Combobox(settings_frame, textvariable=self._format_var,
                     values=FILE_FORMATS, state="readonly", width=10).grid(
            row=row, column=1, sticky="w", padx=(4, 0))

        row += 1
        # Destination
        ttk.Label(settings_frame, text=_("Destination folder:")).grid(row=row, column=0, sticky="w", pady=2)
        self._dest_var = tk.StringVar()
        ttk.Entry(settings_frame, textvariable=self._dest_var).grid(
            row=row, column=1, sticky="ew", padx=(4, 0))
        ttk.Button(settings_frame, text=_("Browse"),
                   command=self._browse_dest).grid(row=row, column=2, padx=4)

        row += 1
        # Prefix
        ttk.Label(settings_frame, text=_("File prefix:")).grid(row=row, column=0, sticky="w", pady=2)
        self._prefix_var = tk.StringVar()
        ttk.Entry(settings_frame, textvariable=self._prefix_var, width=22).grid(
            row=row, column=1, sticky="w", padx=(4, 0))

        row += 1
        # Scan area
        self._area_enabled_var = tk.BooleanVar()
        ttk.Checkbutton(
            settings_frame,
            text=_("Scan area (mm):"),
            variable=self._area_enabled_var,
            command=self._toggle_area_fields,
        ).grid(row=row, column=0, sticky="w", pady=2)

        area_inner = ttk.Frame(settings_frame)
        area_inner.grid(row=row, column=1, sticky="w", padx=(4, 0))
        self._area_left_var  = tk.StringVar()
        self._area_top_var   = tk.StringVar()
        self._area_width_var = tk.StringVar()
        self._area_height_var= tk.StringVar()
        for label, var, col in [
            ("L:", self._area_left_var,   0),
            ("T:", self._area_top_var,    2),
            ("W:", self._area_width_var,  4),
            ("H:", self._area_height_var, 6),
        ]:
            ttk.Label(area_inner, text=label).grid(row=0, column=col, padx=(6, 1))
            e = ttk.Entry(area_inner, textvariable=var, width=6)
            e.grid(row=0, column=col + 1)
        ttk.Label(area_inner, text="mm", foreground="gray").grid(row=0, column=8, padx=4)
        self._area_entries = area_inner.winfo_children()  # for enable/disable

        row += 1
        # Border crop
        ttk.Label(settings_frame, text=_("Border crop (px):")).grid(
            row=row, column=0, sticky="w", pady=2)
        self._crop_border_var = tk.StringVar()
        vcmd2 = (self.register(lambda v: v.isdigit() or v == ""), "%P")
        ttk.Spinbox(settings_frame, textvariable=self._crop_border_var,
                    from_=0, to=500, width=6, validate="key",
                    validatecommand=vcmd2).grid(row=row, column=1, sticky="w", padx=(4, 0))
        ttk.Label(settings_frame, text=_("px (each side)"),
                  foreground="gray").grid(row=row, column=2, sticky="w", padx=4)

        row += 1
        # Compression settings (shown/hidden depending on format)
        self._jpeg_quality_var   = tk.StringVar()
        self._png_compress_var   = tk.StringVar()
        self._tiff_compress_var  = tk.StringVar()

        # JPEG quality row
        self._jpeg_quality_lbl = ttk.Label(settings_frame, text=_("JPEG quality:"))
        self._jpeg_quality_lbl.grid(row=row, column=0, sticky="w", pady=2)
        self._jpeg_quality_spin = ttk.Spinbox(settings_frame,
            textvariable=self._jpeg_quality_var, from_=1, to=100, width=5)
        self._jpeg_quality_spin.grid(row=row, column=1, sticky="w", padx=(4, 0))
        ttk.Label(settings_frame, text="% (1-100)", foreground="gray"
                  ).grid(row=row, column=2, sticky="w", padx=4)

        row += 1
        # PNG compress row
        self._png_compress_lbl = ttk.Label(settings_frame, text=_("PNG compress:"))
        self._png_compress_lbl.grid(row=row, column=0, sticky="w", pady=2)
        self._png_compress_spin = ttk.Spinbox(settings_frame,
            textvariable=self._png_compress_var, from_=0, to=9, width=5)
        self._png_compress_spin.grid(row=row, column=1, sticky="w", padx=(4, 0))
        ttk.Label(settings_frame, text="0-9 (0=none, 9=max)", foreground="gray"
                  ).grid(row=row, column=2, sticky="w", padx=4)

        row += 1
        # TIFF compression row
        self._tiff_compress_lbl = ttk.Label(settings_frame, text=_("TIFF compression:"))
        self._tiff_compress_lbl.grid(row=row, column=0, sticky="w", pady=2)
        self._tiff_compress_combo = ttk.Combobox(settings_frame,
            textvariable=self._tiff_compress_var,
            values=["deflate", "lzw", "jpeg", "none"], state="readonly", width=10)
        self._tiff_compress_combo.grid(row=row, column=1, sticky="w", padx=(4, 0))

        # Show only the row matching the current format
        self._format_var.trace_add("write", lambda *_: self._update_compress_ui())

        # ── Batch frame ────────────────────────────────────────────────────
        batch_frame = ttk.LabelFrame(self, text=_("Batch Mode"), padding=10)
        batch_frame.pack(fill=tk.X, padx=10, pady=4)
        batch_frame.columnconfigure(1, weight=1)

        ttk.Label(batch_frame, text=_("Batch interval (s):")).grid(
            row=0, column=0, sticky="w")
        self._interval_var = tk.StringVar()
        vcmd = (self.register(lambda v: v.isdigit() or v == ""), "%P")
        ttk.Spinbox(batch_frame, textvariable=self._interval_var,
                    from_=5, to=3600, width=8, validate="key",
                    validatecommand=vcmd).grid(row=0, column=1, sticky="w", padx=4)

        btn_frame = ttk.Frame(batch_frame)
        btn_frame.grid(row=1, column=0, columnspan=3, pady=(8, 0), sticky="w")

        self._start_btn = ttk.Button(btn_frame, text=_("Start Batch"),
                                      command=self._start_batch, width=14)
        self._start_btn.pack(side=tk.LEFT, padx=2)

        self._pause_btn = ttk.Button(btn_frame, text=_("Pause"),
                                      command=self._toggle_pause, state=tk.DISABLED, width=10)
        self._pause_btn.pack(side=tk.LEFT, padx=2)

        self._stop_btn = ttk.Button(btn_frame, text=_("Stop"),
                                     command=self._stop_batch, state=tk.DISABLED, width=10)
        self._stop_btn.pack(side=tk.LEFT, padx=2)

        # Countdown label (inside start button area)
        self._countdown_var = tk.StringVar(value="")
        self._countdown_lbl = ttk.Label(btn_frame, textvariable=self._countdown_var,
                                         font=("TkDefaultFont", 10, "bold"), foreground="#0066cc")
        self._countdown_lbl.pack(side=tk.LEFT, padx=10)

        # ── Manual scan + status ───────────────────────────────────────────
        action_frame = ttk.Frame(self)
        action_frame.pack(fill=tk.X, padx=10, pady=4)

        self._scan_btn = ttk.Button(action_frame, text=_("Scan Now"),
                                     command=self._manual_scan, style="Accent.TButton")
        self._scan_btn.pack(side=tk.LEFT, padx=2)

        self._status_var = tk.StringVar(value=f"{_('Status:')} {_('Ready')}")
        ttk.Label(action_frame, textvariable=self._status_var,
                  font=("TkDefaultFont", 10)).pack(side=tk.LEFT, padx=12)

        # ── Log ────────────────────────────────────────────────────────────
        log_frame = ttk.LabelFrame(self, text=_("Log"), padding=6)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(4, 10))

        self._log_text = tk.Text(log_frame, height=8, state=tk.DISABLED,
                                  wrap=tk.WORD, font=("Courier", 9))
        sb = ttk.Scrollbar(log_frame, command=self._log_text.yview)
        self._log_text.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        ttk.Button(self, text=_("Clear log"),
                   command=self._clear_log).pack(anchor="e", padx=10, pady=(0, 4))

    # ── Settings helpers ────────────────────────────────────────────────────

    def _load_settings_to_ui(self) -> None:
        s = self.cfg["tkscan"]
        res = s.get("resolution", "300")
        self._resolution_var.set(f"{res} dpi")
        self._format_var.set(s.get("file_format", "tiff"))
        self._dest_var.set(s.get("importdir", "import"))
        self._prefix_var.set(s.get("prefix", "scanned"))
        self._interval_var.set(s.get("batch_interval", "30"))
        self._area_enabled_var.set(s.get("scan_area_enabled", "false").lower() == "true")
        self._area_left_var.set(s.get("scan_area_left",   "0"))
        self._area_top_var.set(s.get("scan_area_top",    "0"))
        self._area_width_var.set(s.get("scan_area_width",  "148"))
        self._area_height_var.set(s.get("scan_area_height", "105"))
        self._toggle_area_fields()
        self._crop_border_var.set(s.get("crop_border", "0"))
        self._jpeg_quality_var.set(s.get("jpeg_quality", "85"))
        self._png_compress_var.set(s.get("png_compress", "6"))
        self._tiff_compress_var.set(s.get("tiff_compression", "deflate"))
        self._update_compress_ui()

    def _save_settings_from_ui(self) -> None:
        s = self.cfg["tkscan"]
        s["resolution"] = self._resolution_var.get().split()[0]
        s["file_format"] = self._format_var.get()
        self.cfg["DEFAULT"]["importdir"] = self._dest_var.get()
        s["prefix"] = self._prefix_var.get()
        s["batch_interval"] = self._interval_var.get()
        s["scanner"] = self._scanner_var.get()
        s["scan_area_enabled"] = str(self._area_enabled_var.get()).lower()
        s["scan_area_left"]    = self._area_left_var.get()
        s["scan_area_top"]     = self._area_top_var.get()
        s["scan_area_width"]   = self._area_width_var.get()
        s["scan_area_height"]  = self._area_height_var.get()
        s["crop_border"]       = self._crop_border_var.get()
        s["jpeg_quality"]      = self._jpeg_quality_var.get()
        s["png_compress"]      = self._png_compress_var.get()
        s["tiff_compression"]  = self._tiff_compress_var.get()
        save_config(self.cfg)

    def _browse_dest(self) -> None:
        folder = filedialog.askdirectory(
            title=self._("Select destination folder"),
            initialdir=self._dest_var.get() or str(Path.home()),
        )
        if folder:
            self._dest_var.set(folder)

    def _toggle_area_fields(self) -> None:
        """Enable or disable scan-area entry fields based on checkbox."""
        state = tk.NORMAL if self._area_enabled_var.get() else tk.DISABLED
        for widget in self._area_entries:
            if isinstance(widget, ttk.Entry):
                widget.config(state=state)

    def _update_compress_ui(self) -> None:
        """Show only the compression widgets relevant to the current format."""
        fmt = self._format_var.get().lower()
        jpeg_rows = [self._jpeg_quality_lbl, self._jpeg_quality_spin]
        png_rows  = [self._png_compress_lbl, self._png_compress_spin]
        tiff_rows = [self._tiff_compress_lbl, self._tiff_compress_combo]
        for widgets, visible in [
            (jpeg_rows, fmt == "jpeg"),
            (png_rows,  fmt == "png"),
            (tiff_rows, fmt == "tiff"),
        ]:
            for w in widgets:
                if visible:
                    w.grid()
                else:
                    w.grid_remove()

    # ── Scanner refresh ─────────────────────────────────────────────────────

    def _refresh_scanners_bg(self) -> None:
        def worker():
            scanners = list_scanners()
            self.after(0, self._update_scanner_list, scanners)
        threading.Thread(target=worker, daemon=True).start()

    def _update_scanner_list(self, scanners: list[str]) -> None:
        current = self._scanner_var.get()
        saved = self.cfg["tkscan"].get("scanner", "")
        self._scanner_combo["values"] = scanners
        if scanners:
            pick = current if current in scanners else (
                saved if saved in scanners else scanners[0])
            self._scanner_var.set(pick)
        else:
            self._scanner_var.set("")
        self._log(self._("Scanners found: {count}").format(count=len(scanners)))

    # ── File naming ─────────────────────────────────────────────────────────

    def _next_index(self) -> int:
        cfg_dest = self.cfg["tkscan"].get("importdir", "import")
        prefix = self.cfg["tkscan"].get("prefix", "scanned")
        dest = Path(cfg_dest)
        i = 1
        while True:
            for fmt in FILE_FORMATS:
                if (dest / f"{prefix}_{i}.{fmt}").exists():
                    i += 1
                    break
            else:
                break
        return i

    def _build_path(self) -> Path:
        dest = Path(self._dest_var.get())
        prefix = self._prefix_var.get() or "scanned"
        fmt = self._format_var.get()
        dest.mkdir(parents=True, exist_ok=True)
        i = getattr(self, "_scan_index", 1)
        while True:
            candidate = dest / f"{prefix}_{i}.{fmt}"
            if not candidate.exists():
                self._scan_index = i + 1
                return candidate
            i += 1
            self._scan_index = i

    # ── Scan logic ──────────────────────────────────────────────────────────

    def _do_scan(self) -> None:
        """Run one scan cycle (called from worker thread or directly)."""
        scanner = self._scanner_var.get()
        resolution_str = self._resolution_var.get().split()[0]
        resolution = int(resolution_str) if resolution_str.isdigit() else 300
        fmt = self._format_var.get()
        dest_path = self._build_path()

        # Build scan_area tuple from UI if enabled
        area = None
        if self._area_enabled_var.get():
            try:
                area = (
                    float(self._area_left_var.get()),
                    float(self._area_top_var.get()),
                    float(self._area_width_var.get()),
                    float(self._area_height_var.get()),
                )
            except ValueError:
                area = None

        try:
            crop_px = int(self._crop_border_var.get() or 0)
        except ValueError:
            crop_px = 0

        self.after(0, self._set_status, self._("Scanning..."))
        try:
            if scanner:
                self.after(0, self._log, f"-> {scanner} @ {resolution} dpi -> {dest_path.name}")
                do_scan(scanner, resolution, fmt, dest_path,
                        scan_area=area, crop_border=crop_px,
                        jpeg_quality=int(self._jpeg_quality_var.get() or 85),
                        png_compress=int(self._png_compress_var.get() or 6),
                        tiff_compression=self._tiff_compress_var.get() or "deflate")
            else:
                simulate_scan(dest_path, fmt)
                self.after(0, self._log, f"! {self._('Simulated scan (no scanner)')}: {dest_path.name}")
            self.after(0, self._on_scan_success, dest_path)
        except Exception as exc:
            self.after(0, self._on_scan_error, str(exc))

    def _on_scan_success(self, path: Path) -> None:
        self._set_status(self._("Scan complete"))
        self._log(f"+ {path.name}")
        self._save_settings_from_ui()
        self._thumb_win.add_image(path)
        if self._thumb_win.winfo_viewable() == 0:
            self._thumb_win.deiconify()

    def _on_scan_error(self, msg: str) -> None:
        self._set_status(self._("Scan failed"))
        self._log(f"x {self._('Scan failed')}: {msg}")

    def _manual_scan(self) -> None:
        self._save_settings_from_ui()
        threading.Thread(target=self._do_scan, daemon=True).start()

    # ── Batch logic ─────────────────────────────────────────────────────────

    def _start_batch(self) -> None:
        if self._batch_running:
            # During countdown: skip remaining wait and scan immediately
            self._skip_event.set()
            return
        self._save_settings_from_ui()
        try:
            interval = int(self._interval_var.get())
        except ValueError:
            interval = 30
        if interval < 1:
            interval = 1

        self._batch_running = True
        self._batch_paused = False
        self._stop_event.clear()
        self._pause_event.clear()
        self._skip_event.clear()

        self._start_btn.config(text=self._("Scan Now"))
        self._pause_btn.config(state=tk.NORMAL)
        self._stop_btn.config(state=tk.NORMAL)
        self._set_status(self._("Batch running"))
        self._log(self._("Batch started (interval={interval}s)").format(interval=interval))

        self._batch_thread = threading.Thread(
            target=self._batch_worker, args=(interval,), daemon=True)
        self._batch_thread.start()

    def _batch_worker(self, interval: int) -> None:
        while not self._stop_event.is_set():
            self._skip_event.clear()
            # Scan immediately
            self._do_scan()
            # Countdown - broken early by _stop_event or _skip_event
            end_time = time.time() + interval
            while not self._stop_event.is_set() and not self._skip_event.is_set():
                # Handle pause
                while self._pause_event.is_set() and not self._stop_event.is_set():
                    time.sleep(0.1)
                remaining = int(end_time - time.time())
                if remaining <= 0:
                    break
                self.after(0, self._update_countdown, remaining)
                time.sleep(0.25)
        self.after(0, self._batch_stopped)

    def _update_countdown(self, remaining: int) -> None:
        if self._batch_paused:
            self._countdown_var.set(f"|| {self._('Paused')}")
        else:
            self._countdown_var.set(
                f"{self._('Next scan in:')} {remaining}s")

    def _toggle_pause(self) -> None:
        if not self._batch_running:
            return
        self._batch_paused = not self._batch_paused
        if self._batch_paused:
            self._pause_event.set()
            self._pause_btn.config(text=self._("Resume"))
            self._set_status(self._("Paused"))
            self._log(f"|| {self._('Paused')}")
        else:
            self._pause_event.clear()
            self._pause_btn.config(text=self._("Pause"))
            self._set_status(self._("Batch running"))
            self._log(f"> {self._('Resume')}")

    def _stop_batch(self) -> None:
        self._stop_event.set()
        self._pause_event.clear()

    def _batch_stopped(self) -> None:
        self._batch_running = False
        self._batch_paused = False
        self._start_btn.config(text=self._("Start Batch"))
        self._pause_btn.config(text=self._("Pause"), state=tk.DISABLED)
        self._stop_btn.config(state=tk.DISABLED)
        self._countdown_var.set("")
        self._set_status(self._("Stopped"))
        self._log(f"# {self._('Stopped')}")

    # ── Utilities ────────────────────────────────────────────────────────────

    def _set_status(self, msg: str) -> None:
        self._status_var.set(f"{self._('Status:')} {msg}")

    def _log(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self._log_text.config(state=tk.NORMAL)
        self._log_text.insert(tk.END, f"[{ts}] {msg}\n")
        self._log_text.see(tk.END)
        self._log_text.config(state=tk.DISABLED)

    def _clear_log(self) -> None:
        self._log_text.config(state=tk.NORMAL)
        self._log_text.delete("1.0", tk.END)
        self._log_text.config(state=tk.DISABLED)

    def _on_quit(self) -> None:
        self._save_settings_from_ui()
        if self._batch_running:
            self._stop_event.set()
        self.destroy()


# ──────────────────────────────────────────────────────────────────────────────
# CLI entry point (click)
# ──────────────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--prefix", default=None, help="Override file prefix.")
@click.option("--resolution", default=None,
              type=click.Choice(RESOLUTIONS), help="Override scan resolution (dpi).")
@click.option("--format", "fmt", default=None,
              type=click.Choice(FILE_FORMATS), help="Override output format.")
@click.version_option("2.0.0", prog_name="tkscan")
@click.pass_obj
def main(common, prefix, resolution, fmt):
    """tkscan - batch scan your postcard collection."""
    global CONFIG_FILE
    if common and getattr(common, "conffile", None):
        CONFIG_FILE = Path(common.conffile)

    cfg = getattr(common, "cfg", None) or load_config()
    translation = getattr(common, "translation", None) or setup_i18n()
    gettext_func = translation.gettext

    # CLI overrides
    if prefix:
        cfg["tkscan"]["prefix"] = prefix
    if resolution:
        cfg["tkscan"]["resolution"] = resolution
    if fmt:
        cfg["tkscan"]["file_format"] = fmt

    # Launch GUI
    app = PostcardScannerApp(cfg, gettext_func)
    app.mainloop()


def run():
    """Standalone entry point (tkscaan script): runs `cli main`."""
    sys.argv.insert(1, "main")
    cli()


if __name__ == "__main__":
    run()
