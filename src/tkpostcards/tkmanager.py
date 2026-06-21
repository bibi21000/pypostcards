#!/usr/bin/env python3
"""
Postcard collection viewer and editor.
Usage: tkmanager --data-dir /path/to/data
"""
from __future__ import annotations

import gettext
import locale
import sys
import threading
import webbrowser
from datetime import date, datetime
from pathlib import Path

import click
import configparser
import tkinter as tk
from tkinter import messagebox, ttk

from libpostcards.model import Model

from . import cli

try:
    from .libs.similar import PostcardSearcher
    SEARCHER_AVAILABLE = True
except ImportError:
    SEARCHER_AVAILABLE = False

try:
    from PIL import Image, ImageTk
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

# ─────────────────────────────────────────────────────────────────────────────
#  i18n
# ─────────────────────────────────────────────────────────────────────────────
LOCALE_DIR = Path(__file__).parent / "translations"

# Placeholder so static analyzers (ruff) recognize `_` as defined.
# Overwritten with the real translation function by setup_i18n().
_ = gettext.gettext


def setup_i18n():
    """Set up gettext translations using the system locale."""
    global _
    try:
        sys_lang, _country = locale.getlocale()
        lang = (sys_lang or "en").split("_")[0]
    except Exception:
        lang = "en"
    try:
        t = gettext.translation("tkpostcards", localedir=str(LOCALE_DIR), languages=[lang])
    except FileNotFoundError:
        t = gettext.NullTranslations()
    t.install()
    _ = t.gettext
    return _


def _translatable_field_labels():  # pragma: no cover
    """Never called at runtime.

    Field labels are looked up dynamically (e.g. ``_(lk)`` where ``lk`` is a
    variable from ``App.TEXT_FIELDS`` / ``App.LIST_FIELDS`` or similar
    tables), so ``pybabel extract -k _`` cannot see them as literal calls.
    This function exists purely so the extractor picks up these strings.
    """
    return [
        # App.TEXT_FIELDS
        _("field_title"),
        _("field_title2"),
        _("field_description"),
        _("field_recto_ocr"),
        _("field_verso_ocr"),
        _("field_recto_text"),
        _("field_verso_text"),
        # App.LIST_FIELDS
        _("field_address"),
        _("field_poi"),
        # CoordDialog
        _("coord_lat"),
        _("coord_lon"),
        # App._build_thumbs
        _("side_recto"),
        _("side_verso"),
    ]


# ─────────────────────────────────────────────────────────────────────────────
#  Palette
# ─────────────────────────────────────────────────────────────────────────────
BG_MAIN     = "#1a1a2e"
BG_CARD     = "#16213e"
BG_FIELD    = "#0f3460"
BG_INPUT    = "#1a2a4a"
BG_GALLERY  = "#0d1117"
FG_TEXT     = "#e0e0e0"
FG_LABEL    = "#a0b4c8"
FG_ACCENT   = "#e94560"
FG_ACCENT2  = "#f5a623"
FG_LINK     = "#4fc3f7"
BTN_CLEAN   = "#1a5fa8"
BTN_DIRTY   = "#e94560"

FONT_TITLE  = ("Georgia", 13, "bold")
FONT_LABEL  = ("Courier", 9)
FONT_INPUT  = ("Courier", 10)
FONT_NAV    = ("Georgia", 11, "bold")
FONT_SMALL  = ("Courier", 8)
THUMB_SIZE  = (220, 160)

# Gallery
GALL_W      = 160   # thumbnail width
GALL_H      = 110   # thumbnail height
GALL_PAD    = 6
GALL_BORDER = "#2a3f6a"
GALL_SEL    = "#e94560"
GALL_HOVER  = "#f5a623"


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────
def load_pil(path: Path, w: int, h: int) -> "Image.Image | None":
    """Load a resized PIL image. Thread-safe."""
    if not PIL_AVAILABLE:
        return None
    try:
        img = Image.open(path)
        img.thumbnail((w, h), Image.LANCZOS)
        return img
    except Exception:
        return None


def pil_to_tk(img: "Image.Image") -> ImageTk.PhotoImage:
    return ImageTk.PhotoImage(img)


def parse_date(raw: str) -> date | None:
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            pass
    return None


def context_menu(widget: tk.Widget):
    """Right-click copy/paste context menu for any widget."""
    m = tk.Menu(widget, tearoff=0, bg=BG_FIELD, fg=FG_TEXT,
                activebackground=FG_ACCENT, activeforeground="#fff")
    m.add_command(label=_("ctx_cut"),        command=lambda: widget.event_generate("<<Cut>>"))
    m.add_command(label=_("ctx_copy"),       command=lambda: widget.event_generate("<<Copy>>"))
    m.add_command(label=_("ctx_paste"),      command=lambda: widget.event_generate("<<Paste>>"))
    m.add_separator()
    m.add_command(label=_("ctx_select_all"), command=lambda: widget.event_generate("<<SelectAll>>"))

    def popup(event):
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                m.grab_release()
            except tk.TclError:
                pass
    widget.bind("<Button-3>", popup)


def sep(parent, color=FG_ACCENT):
    tk.Frame(parent, bg=color, height=1).pack(fill=tk.X, padx=8, pady=6)


# ─────────────────────────────────────────────────────────────────────────────
#  Full-screen image viewer
# ─────────────────────────────────────────────────────────────────────────────
class ImageViewer(tk.Toplevel):
    def __init__(self, parent, path: Path, title: str, t):
        super().__init__(parent)
        self.title(title)
        self.configure(bg=BG_MAIN)
        self.resizable(True, True)
        self._t = t
        self._ref = None
        self._zoom = 1.0
        self._orig = None

        tb = tk.Frame(self, bg=BG_CARD, pady=4)
        tb.pack(fill=tk.X)
        for label, cmd in [("🔍+", self._zi), ("🔍−", self._zo),
                            ("1:1", self._zr), (_("zoom_fit"), self._zf)]:
            tk.Button(tb, text=label, command=cmd, bg=BG_FIELD, fg=FG_TEXT,
                      relief=tk.FLAT, padx=8).pack(side=tk.LEFT, padx=4)
        self._zlbl = tk.Label(tb, text="100%", bg=BG_CARD, fg=FG_ACCENT2, font=FONT_LABEL)
        self._zlbl.pack(side=tk.RIGHT, padx=8)

        frm = tk.Frame(self, bg=BG_MAIN)
        frm.pack(fill=tk.BOTH, expand=True)
        self.cv = tk.Canvas(frm, bg="#0a0a1a", cursor="crosshair", highlightthickness=0)
        vsb = ttk.Scrollbar(frm, orient=tk.VERTICAL,   command=self.cv.yview)
        hsb = ttk.Scrollbar(frm, orient=tk.HORIZONTAL, command=self.cv.xview)
        self.cv.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side=tk.RIGHT,  fill=tk.Y)
        hsb.pack(side=tk.BOTTOM, fill=tk.X)
        self.cv.pack(fill=tk.BOTH, expand=True)
        self.cv.bind("<MouseWheel>", self._wheel)
        self.cv.bind("<Button-4>",   self._wheel)
        self.cv.bind("<Button-5>",   self._wheel)

        if PIL_AVAILABLE:
            try:
                self._orig = Image.open(path)
                self.geometry("950x720")
                self.after(80, self._zf)
            except Exception as e:
                self._err(str(e))
        else:
            self._err("pip install Pillow")

    def _render(self):
        if not self._orig:
            return
        w = max(1, int(self._orig.width  * self._zoom))
        h = max(1, int(self._orig.height * self._zoom))
        img = self._orig.resize((w, h), Image.LANCZOS)
        self._ref = ImageTk.PhotoImage(img)
        self.cv.delete("all")
        self.cv.create_image(0, 0, anchor=tk.NW, image=self._ref)
        self.cv.configure(scrollregion=(0, 0, w, h))
        self._zlbl.config(text=f"{int(self._zoom * 100)}%")

    def _zi(self):
        self._zoom = min(self._zoom * 1.25, 8.0)
        self._render()

    def _zo(self):
        self._zoom = max(self._zoom / 1.25, 0.05)
        self._render()

    def _zr(self):
        self._zoom = 1.0
        self._render()

    def _zf(self):
        if not self._orig:
            return
        self.update_idletasks()
        cw = self.cv.winfo_width()  or 880
        ch = self.cv.winfo_height() or 640
        self._zoom = min(cw / self._orig.width, ch / self._orig.height, 1.0)
        self._render()

    def _wheel(self, e):
        if e.num == 4 or e.delta > 0:
            self._zi()
        else:
            self._zo()

    def _err(self, msg):
        self.cv.create_text(400, 300, text=msg, fill=FG_ACCENT,
                            font=("Courier", 12), justify=tk.CENTER)


# ─────────────────────────────────────────────────────────────────────────────
#  Calendar / date picker
# ─────────────────────────────────────────────────────────────────────────────
class DatePicker(tk.Toplevel):
    def __init__(self, parent, current: date | None, on_save, t):
        super().__init__(parent)
        self.title(_("date_dialog_title"))
        self.configure(bg=BG_MAIN)
        self.resizable(False, False)
        self._on_save = on_save
        self._t = t
        self._sel = current
        self._view = (current or date.today()).replace(day=1)
        self._build()
        self._draw()
        self.geometry("310x280")
        self.after_idle(self._safe_grab)

    def _safe_grab(self):
        try:
            if self.winfo_exists():
                self.grab_set()
        except tk.TclError:
            pass

    def _build(self):
        nav = tk.Frame(self, bg=BG_CARD)
        nav.pack(fill=tk.X, pady=4)
        tk.Button(nav, text="◀", command=self._prev, bg=BG_FIELD, fg=FG_TEXT,
                  relief=tk.FLAT, padx=8).pack(side=tk.LEFT, padx=6)
        self._mlbl = tk.Label(nav, text="", bg=BG_CARD, fg=FG_ACCENT2, font=FONT_TITLE)
        self._mlbl.pack(side=tk.LEFT, expand=True)
        tk.Button(nav, text="▶", command=self._next, bg=BG_FIELD, fg=FG_TEXT,
                  relief=tk.FLAT, padx=8).pack(side=tk.RIGHT, padx=6)
        self._grid = tk.Frame(self, bg=BG_MAIN)
        self._grid.pack(padx=10, pady=4, fill=tk.BOTH, expand=True)
        bot = tk.Frame(self, bg=BG_MAIN)
        bot.pack(fill=tk.X, padx=10, pady=(0, 8))
        tk.Button(bot, text=_("btn_save_close"), command=self._save,
                  bg=FG_ACCENT, fg="#fff", font=FONT_LABEL,
                  relief=tk.FLAT, padx=10).pack(side=tk.RIGHT, padx=4)
        tk.Button(bot, text=_("btn_cancel"), command=self.destroy,
                  bg=BG_FIELD, fg=FG_TEXT, font=FONT_LABEL,
                  relief=tk.FLAT, padx=10).pack(side=tk.RIGHT, padx=4)

    def _draw(self):
        import calendar
        for w in self._grid.winfo_children():
            w.destroy()
        self._mlbl.config(text=self._view.strftime("%B %Y").capitalize())
        for c, d in enumerate([
            _("day_mon"), _("day_tue"), _("day_wed"),
            _("day_thu"), _("day_fri"), _("day_sat"), _("day_sun"),
        ]):
            tk.Label(self._grid, text=d, bg=BG_MAIN, fg=FG_LABEL,
                     font=("Courier", 8, "bold"), width=3).grid(row=0, column=c, padx=1)
        for r, week in enumerate(calendar.monthcalendar(self._view.year, self._view.month), 1):
            for c, day in enumerate(week):
                if day == 0:
                    tk.Label(self._grid, text="", bg=BG_MAIN, width=3).grid(row=r, column=c)
                else:
                    d = date(self._view.year, self._view.month, day)
                    sel = (self._sel == d)
                    tk.Button(self._grid, text=str(day), width=3,
                              bg=FG_ACCENT if sel else BG_FIELD,
                              fg="#fff" if sel else FG_TEXT,
                              font=FONT_LABEL, relief=tk.FLAT, padx=2,
                              command=lambda dd=d: self._pick(dd)).grid(
                        row=r, column=c, padx=1, pady=1)

    def _prev(self):
        y, m = self._view.year, self._view.month - 1
        if m == 0:
            y, m = y - 1, 12
        self._view = date(y, m, 1)
        self._draw()

    def _next(self):
        y, m = self._view.year, self._view.month + 1
        if m == 13:
            y, m = y + 1, 1
        self._view = date(y, m, 1)
        self._draw()

    def _pick(self, d: date):
        self._sel = d
        self._draw()

    def _save(self):
        self._on_save(self._sel)
        self.destroy()


# ─────────────────────────────────────────────────────────────────────────────
#  Date field widget (entry + calendar)
# ─────────────────────────────────────────────────────────────────────────────
class DateField(tk.Frame):
    def __init__(self, parent, on_change=None, **kw):
        super().__init__(parent, bg=BG_CARD, **kw)
        self._on_change = on_change
        self._date: date | None = None
        self.var = tk.StringVar()
        self.var.trace_add("write", self._on_write)
        e = tk.Entry(self, textvariable=self.var, width=14,
                     bg=BG_INPUT, fg=FG_TEXT, insertbackground=FG_TEXT,
                     font=FONT_INPUT, relief=tk.FLAT)
        e.pack(side=tk.LEFT, padx=(0, 4))
        context_menu(e)
        tk.Button(self, text=_("date_pick"), command=self._pick,
                  bg=BG_FIELD, fg=FG_TEXT, font=FONT_LABEL,
                  relief=tk.FLAT, padx=6, cursor="hand2").pack(side=tk.LEFT, padx=2)
        tk.Button(self, text=_("date_clear"), command=self._clear,
                  bg="#5a1a1a", fg=FG_TEXT, font=FONT_LABEL,
                  relief=tk.FLAT, padx=4, cursor="hand2").pack(side=tk.LEFT, padx=2)
        tk.Label(self, text=_("date_format_hint"),
                 bg=BG_CARD, fg=FG_LABEL, font=("Courier", 8)).pack(side=tk.LEFT, padx=6)

    def _on_write(self, *_):
        raw = self.var.get()
        if raw:
            d = parse_date(raw)
            if d:
                self._date = d
        else:
            self._date = None
        if self._on_change:
            self._on_change()

    def _pick(self):
        DatePicker(self, self._date, self._set, self._t)

    def _set(self, d: date | None):
        self._date = d
        self.var.set(d.isoformat() if d else "")
        if self._on_change:
            self._on_change()

    def _clear(self):
        self._set(None)

    def get_value(self) -> str | None:
        raw = self.var.get().strip()
        if not raw:
            return None
        d = parse_date(raw)
        return d.isoformat() if d else (raw or None)

    def set_value(self, v: str | None):
        if v is None:
            self.var.set("")
            self._date = None
        else:
            d = parse_date(str(v))
            self._date = d
            self.var.set(d.isoformat() if d else str(v))


# ─────────────────────────────────────────────────────────────────────────────
#  Generic string list editor (address, poi, …)
# ─────────────────────────────────────────────────────────────────────────────
class ListEditor(tk.Toplevel):
    def __init__(self, parent, field_label: str, lines: list[str], on_save, t):
        super().__init__(parent)
        self.title(f"{_('list_editor_title')} — {field_label}")
        self.configure(bg=BG_MAIN)
        self._on_save = on_save
        self._t = t
        self.resizable(True, True)

        tk.Label(self, text=f"{field_label}  —  {_('list_editor_hint')}",
                 bg=BG_MAIN, fg=FG_ACCENT, font=FONT_TITLE).pack(padx=12, pady=(12, 4))

        self._lb = tk.Listbox(self, bg=BG_INPUT, fg=FG_TEXT,
                              selectbackground=FG_ACCENT, font=FONT_INPUT,
                              width=48, height=10, relief=tk.FLAT, activestyle="none")
        self._lb.pack(padx=12, pady=4, fill=tk.BOTH, expand=True)
        for line in lines:
            self._lb.insert(tk.END, line)

        # Input bar
        row = tk.Frame(self, bg=BG_MAIN)
        row.pack(fill=tk.X, padx=12, pady=4)
        self._evar = tk.StringVar()
        e = tk.Entry(row, textvariable=self._evar, width=36,
                     bg=BG_INPUT, fg=FG_TEXT, insertbackground=FG_TEXT,
                     font=FONT_INPUT, relief=tk.FLAT)
        e.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        e.bind("<Return>", lambda _: self._add_or_update())
        context_menu(e)
        for txt, cmd in [(_("btn_add"), self._add), (_("btn_update"), self._update)]:
            tk.Button(row, text=txt, command=cmd, bg=BG_FIELD, fg=FG_TEXT,
                      font=FONT_LABEL, relief=tk.FLAT, padx=6).pack(side=tk.LEFT, padx=2)

        # List controls
        ctrl = tk.Frame(self, bg=BG_MAIN)
        ctrl.pack(fill=tk.X, padx=12, pady=4)
        for txt, cmd, bg in [(_("btn_move_up"),   self._up,   BG_FIELD),
                              (_("btn_move_down"), self._down, BG_FIELD),
                              (_("btn_delete"),    self._del,  "#5a1a1a")]:
            tk.Button(ctrl, text=txt, command=cmd, bg=bg, fg=FG_TEXT,
                      font=FONT_LABEL, relief=tk.FLAT, padx=6).pack(side=tk.LEFT, padx=2)

        self._lb.bind("<<ListboxSelect>>", self._on_sel)

        bot = tk.Frame(self, bg=BG_MAIN)
        bot.pack(fill=tk.X, padx=12, pady=(4, 12))
        tk.Button(bot, text=_("btn_save_close"), command=self._save,
                  bg=FG_ACCENT, fg="#fff", font=FONT_LABEL,
                  relief=tk.FLAT, padx=10).pack(side=tk.RIGHT, padx=4)
        tk.Button(bot, text=_("btn_cancel"), command=self.destroy,
                  bg=BG_FIELD, fg=FG_TEXT, font=FONT_LABEL,
                  relief=tk.FLAT, padx=10).pack(side=tk.RIGHT, padx=4)

        self.update_idletasks()
        self.minsize(self.winfo_reqwidth(), self.winfo_reqheight())

    def _on_sel(self, _):
        s = self._lb.curselection()
        if s:
            self._evar.set(self._lb.get(s[0]))

    def _add(self):
        v = self._evar.get().strip()
        if v:
            self._lb.insert(tk.END, v)
            self._evar.set("")

    def _add_or_update(self):
        if self._lb.curselection():
            self._update()
        else:
            self._add()

    def _update(self):
        s = self._lb.curselection()
        if not s:
            return
        v = self._evar.get().strip()
        if v:
            i = s[0]
            self._lb.delete(i)
            self._lb.insert(i, v)
            self._lb.selection_set(i)

    def _del(self):
        s = self._lb.curselection()
        if s:
            self._lb.delete(s[0])
            self._evar.set("")

    def _up(self):
        s = self._lb.curselection()
        if not s or s[0] == 0:
            return
        i = s[0]
        v = self._lb.get(i)
        self._lb.delete(i)
        self._lb.insert(i - 1, v)
        self._lb.selection_set(i - 1)

    def _down(self):
        s = self._lb.curselection()
        if not s or s[0] == self._lb.size() - 1:
            return
        i = s[0]
        v = self._lb.get(i)
        self._lb.delete(i)
        self._lb.insert(i + 1, v)
        self._lb.selection_set(i + 1)

    def _save(self):
        self._on_save(list(self._lb.get(0, tk.END)))
        self.destroy()


# ─────────────────────────────────────────────────────────────────────────────
#  POI list editor (for a postcard's "poi" field)
# ─────────────────────────────────────────────────────────────────────────────
class PoiListEditor(tk.Toplevel):
    """Editor for a postcard's list of POI ids.

    Instead of free text, the input is a combobox listing all POI ids
    already present in the database (Model.list_pois()), with the option
    to type a new id to create one on save.
    """

    def __init__(self, parent, field_label: str, lines: list[str],
                on_save, model, t):
        super().__init__(parent)
        self.title(f"{_('list_editor_title')} — {field_label}")
        self.configure(bg=BG_MAIN)
        self._on_save = on_save
        self._model = model
        self._t = t
        self.resizable(True, True)

        try:
            self._known_pois = [p["id"] for p in model.list_pois()]
        except Exception:
            self._known_pois = []

        tk.Label(self, text=f"{field_label}  —  {_('list_editor_hint')}",
                 bg=BG_MAIN, fg=FG_ACCENT, font=FONT_TITLE).pack(padx=12, pady=(12, 4))

        self._lb = tk.Listbox(self, bg=BG_INPUT, fg=FG_TEXT,
                              selectbackground=FG_ACCENT, font=FONT_INPUT,
                              width=48, height=10, relief=tk.FLAT, activestyle="none")
        self._lb.pack(padx=12, pady=4, fill=tk.BOTH, expand=True)
        for line in lines:
            self._lb.insert(tk.END, line)

        # Input bar: combobox of known POIs + free entry for a new one
        row = tk.Frame(self, bg=BG_MAIN)
        row.pack(fill=tk.X, padx=12, pady=4)
        self._evar = tk.StringVar()
        self._combo = ttk.Combobox(row, textvariable=self._evar,
                                   values=self._known_pois, width=34,
                                   font=FONT_INPUT)
        self._combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        self._combo.bind("<Return>", lambda _: self._add_or_update())
        context_menu(self._combo)
        for txt, cmd in [(_("btn_add"), self._add), (_("btn_update"), self._update)]:
            tk.Button(row, text=txt, command=cmd, bg=BG_FIELD, fg=FG_TEXT,
                      font=FONT_LABEL, relief=tk.FLAT, padx=6).pack(side=tk.LEFT, padx=2)

        tk.Label(self, text=_("poi_editor_hint"), bg=BG_MAIN, fg=FG_LABEL,
                 font=("Courier", 8)).pack(padx=12, anchor=tk.W)

        # List controls
        ctrl = tk.Frame(self, bg=BG_MAIN)
        ctrl.pack(fill=tk.X, padx=12, pady=4)
        for txt, cmd, bg in [(_("btn_move_up"),   self._up,   BG_FIELD),
                              (_("btn_move_down"), self._down, BG_FIELD),
                              (_("btn_delete"),    self._del,  "#5a1a1a")]:
            tk.Button(ctrl, text=txt, command=cmd, bg=bg, fg=FG_TEXT,
                      font=FONT_LABEL, relief=tk.FLAT, padx=6).pack(side=tk.LEFT, padx=2)

        self._lb.bind("<<ListboxSelect>>", self._on_sel)

        bot = tk.Frame(self, bg=BG_MAIN)
        bot.pack(fill=tk.X, padx=12, pady=(4, 12))
        tk.Button(bot, text=_("btn_save_close"), command=self._save,
                  bg=FG_ACCENT, fg="#fff", font=FONT_LABEL,
                  relief=tk.FLAT, padx=10).pack(side=tk.RIGHT, padx=4)
        tk.Button(bot, text=_("btn_cancel"), command=self.destroy,
                  bg=BG_FIELD, fg=FG_TEXT, font=FONT_LABEL,
                  relief=tk.FLAT, padx=10).pack(side=tk.RIGHT, padx=4)

        self.update_idletasks()
        self.minsize(self.winfo_reqwidth(), self.winfo_reqheight())

    def _on_sel(self, _):
        s = self._lb.curselection()
        if s:
            self._evar.set(self._lb.get(s[0]))

    def _add(self):
        v = self._evar.get().strip()
        if not v:
            return
        self._lb.insert(tk.END, v)
        self._evar.set("")
        self._maybe_create_poi(v)

    def _add_or_update(self):
        if self._lb.curselection():
            self._update()
        else:
            self._add()

    def _update(self):
        s = self._lb.curselection()
        if not s:
            return
        v = self._evar.get().strip()
        if v:
            i = s[0]
            self._lb.delete(i)
            self._lb.insert(i, v)
            self._lb.selection_set(i)
            self._maybe_create_poi(v)

    def _maybe_create_poi(self, poi_id: str):
        """Create a skeleton POI in the database if it doesn't exist yet,
        and refresh the combobox suggestion list."""
        if poi_id in self._known_pois:
            return
        try:
            self._model._ensure_poi(poi_id)
        except Exception:
            pass
        self._known_pois.append(poi_id)
        self._combo.config(values=self._known_pois)

    def _del(self):
        s = self._lb.curselection()
        if s:
            self._lb.delete(s[0])
            self._evar.set("")

    def _up(self):
        s = self._lb.curselection()
        if not s or s[0] == 0:
            return
        i = s[0]
        v = self._lb.get(i)
        self._lb.delete(i)
        self._lb.insert(i - 1, v)
        self._lb.selection_set(i - 1)

    def _down(self):
        s = self._lb.curselection()
        if not s or s[0] == self._lb.size() - 1:
            return
        i = s[0]
        v = self._lb.get(i)
        self._lb.delete(i)
        self._lb.insert(i + 1, v)
        self._lb.selection_set(i + 1)

    def _save(self):
        self._on_save(list(self._lb.get(0, tk.END)))
        self.destroy()


# ─────────────────────────────────────────────────────────────────────────────
#  GPS coordinates dialog
# ─────────────────────────────────────────────────────────────────────────────
class CoordDialog(tk.Toplevel):
    def __init__(self, parent, coord: list, on_save, t):
        super().__init__(parent)
        self.title(_("coord_title"))
        self.configure(bg=BG_MAIN)
        self._on_save = on_save
        self._t = t

        lat = str(coord[0]) if len(coord) >= 1 else ""
        lon = str(coord[1]) if len(coord) >= 2 else ""

        pad = dict(padx=14, pady=7)

        tk.Label(self, text=_("coord_header"), bg=BG_MAIN, fg=FG_ACCENT,
                 font=FONT_TITLE).grid(row=0, column=0, columnspan=3, pady=(14, 6))

        self._latv = tk.StringVar(value=lat)
        self._lonv = tk.StringVar(value=lon)

        for ri, (lk, var) in enumerate([("coord_lat", self._latv),
                                         ("coord_lon", self._lonv)], 1):
            tk.Label(self, text=_(lk), bg=BG_MAIN, fg=FG_LABEL,
                     font=FONT_LABEL, anchor=tk.E, width=12).grid(
                row=ri, column=0, sticky=tk.E, **pad)
            e = tk.Entry(self, textvariable=var, width=24,
                         bg=BG_INPUT, fg=FG_TEXT, insertbackground=FG_TEXT,
                         font=FONT_INPUT, relief=tk.FLAT)
            e.grid(row=ri, column=1, columnspan=2, **pad)
            context_menu(e)

        # Quick paste
        tk.Label(self, text=_("coord_paste_label"), bg=BG_MAIN, fg=FG_LABEL,
                 font=FONT_LABEL, anchor=tk.E, width=12).grid(
            row=3, column=0, sticky=tk.E, **pad)
        self._pastev = tk.StringVar()
        pe = tk.Entry(self, textvariable=self._pastev, width=24,
                      bg=BG_INPUT, fg=FG_TEXT, insertbackground=FG_TEXT,
                      font=FONT_INPUT, relief=tk.FLAT)
        pe.grid(row=3, column=1, **pad)
        pe.bind("<Return>", lambda _: self._parse_paste())
        context_menu(pe)
        tk.Button(self, text="⏎", command=self._parse_paste,
                  bg=BG_FIELD, fg=FG_TEXT, relief=tk.FLAT).grid(row=3, column=2, padx=4)

        tk.Label(self, text=_("coord_paste_hint"), bg=BG_MAIN, fg=FG_LABEL,
                 font=("Courier", 8)).grid(row=4, column=0, columnspan=3, padx=14, pady=(0, 6))

        btns = tk.Frame(self, bg=BG_MAIN)
        btns.grid(row=5, column=0, columnspan=3, pady=10)
        for txt, cmd, bg, fg in [
            (_("btn_open_osm"),   self._osm,         FG_LINK,   "#000"),
            (_("btn_copy_osm"),   self._copy_osm,    BG_FIELD,  FG_TEXT),
            (_("btn_reset"),      self._reset,       "#5a1a1a", FG_TEXT),
            (_("btn_save_close"), self._save,         FG_ACCENT, "#fff"),
            (_("btn_cancel"),     self.destroy,       BG_FIELD,  FG_TEXT),
        ]:
            tk.Button(btns, text=txt, command=cmd, bg=bg, fg=fg,
                      font=FONT_LABEL, relief=tk.FLAT, padx=10).pack(side=tk.LEFT, padx=5)

        self.resizable(False, False)
        self.update_idletasks()
        self.minsize(self.winfo_reqwidth(), self.winfo_reqheight())

    def _coords(self):
        try:
            return float(self._latv.get()), float(self._lonv.get())
        except ValueError:
            messagebox.showerror(_("error_title"), _("coord_error"), parent=self)
            return None, None

    def _osm(self):
        lat, lon = self._coords()
        if lat is not None:
            webbrowser.open(f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=14/{lat}/{lon}")

    def _copy_osm(self):
        lat, lon = self._coords()
        if lat is not None:
            url = f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=14/{lat}/{lon}"
            self.clipboard_clear()
            self.clipboard_append(url)
            messagebox.showinfo(_("osm_copied_title"), _("osm_copied"), parent=self)

    def _parse_paste(self):
        raw = self._pastev.get().strip()
        for sep_char in ["/", ",", ";", " "]:
            parts = [p.strip() for p in raw.split(sep_char, 1)]
            if len(parts) == 2:
                try:
                    self._latv.set(str(float(parts[0])))
                    self._lonv.set(str(float(parts[1])))
                    self._pastev.set("")
                    return
                except ValueError:
                    pass
        messagebox.showerror(_("error_title"), _("coord_parse_error"), parent=self)

    def _reset(self):
        self._latv.set("")
        self._lonv.set("")
        self._pastev.set("")

    def _save(self):
        lat_raw = self._latv.get().strip()
        lon_raw = self._lonv.get().strip()
        if not lat_raw and not lon_raw:
            self._on_save([])
            self.destroy()
            return
        lat, lon = self._coords()
        if lat is not None:
            self._on_save([lat, lon])
            self.destroy()


# ─────────────────────────────────────────────────────────────────────────────
#  Gallery view — pure Canvas drawing, zero dynamic widgets
#
#  Why pure Canvas?
#  The previous version created hundreds of tk.Frame/Label in _draw().
#  Each creation/destruction changed the inner Frame's size, which
#  triggered <Configure> on the Canvas, which re-ran _draw(), causing an
#  infinite loop → flicker. With create_image() / create_rectangle() /
#  create_text() the Canvas does not emit <Configure> events when redrawn.
# ─────────────────────────────────────────────────────────────────────────────
class GalleryView(tk.Toplevel):

    # Tile dimensions (canvas pixels)
    TILE_MARGIN  = 6    # space between tiles
    HDR_H        = 18   # header text height
    BADGE_H      = 14   # R/V badge height
    IMG_PAD      = 3    # padding around the image within the tile

    def __init__(self, parent: "App", t):
        super().__init__(parent)
        self._app = parent
        self._t   = t
        self.title(_("gallery_title"))
        self.configure(bg=BG_GALLERY)
        self.geometry("1150x780")
        self.minsize(700, 420)

        # Raw PIL cache  {(cid, side): PIL.Image | None}
        self._pil: dict[tuple, "Image.Image | None"] = {}
        # PERMANENT PhotoImage cache  {(cid, side): ImageTk.PhotoImage}
        # Never clear in bulk: each entry stays alive as long as the
        # gallery exists, otherwise the GC could free an image still
        # displayed by the Canvas (this was the cause of corrupted images).
        self._tkimg: dict[tuple, "ImageTk.PhotoImage"] = {}
        # Titles  {cid: str}
        self._titles: dict[int, str] = {}

        self._mode_var = tk.StringVar(value="RV")
        self._cols_var = tk.IntVar(value=4)
        self._sel_id: int | None = (parent._ids[parent._current_idx]
                                    if parent._ids else None)

        # Map of clickable areas  [(x0,y0,x1,y1, cid), …]
        self._hit_zones: list[tuple] = []

        self._loading      = False
        self._pending_draw: str | None = None
        self._last_cv_w    = 0   # remembered canvas width

        import queue
        self._queue: "queue.Queue" = queue.Queue()

        self._build_toolbar()
        self._build_canvas()
        self._build_statusbar()

        # Start loading as soon as the window has been drawn
        self.after(50, self._start_loading)
        self._poll_queue()

        # We do NOT bind <Configure> on self to avoid the loop.
        # We only watch for Canvas width changes.
        self._cv.bind("<Configure>", self._on_cv_configure)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── Construction ──────────────────────────────────────────────────────────
    def _build_toolbar(self):
        tb = tk.Frame(self, bg=BG_CARD, pady=6)
        tb.pack(fill=tk.X)

        tk.Label(tb, text=_("gallery_mode"), bg=BG_CARD, fg=FG_LABEL,
                 font=FONT_LABEL).pack(side=tk.LEFT, padx=(12, 4))
        for val, lbl in [("R",  _("gall_recto")),
                          ("V",  _("gall_verso")),
                          ("RV", _("gall_both"))]:
            tk.Radiobutton(tb, text=lbl, variable=self._mode_var, value=val,
                           command=self._schedule_draw,
                           bg=BG_CARD, fg=FG_TEXT, selectcolor=BG_FIELD,
                           activebackground=BG_CARD, activeforeground=FG_ACCENT2,
                           font=FONT_LABEL, relief=tk.FLAT, cursor="hand2",
                           indicatoron=0, padx=10, pady=3,
                           highlightthickness=0).pack(side=tk.LEFT, padx=2)

        tk.Label(tb, text=_("gallery_cols"), bg=BG_CARD, fg=FG_LABEL,
                 font=FONT_LABEL).pack(side=tk.LEFT, padx=(16, 4))
        for c in [2, 3, 4, 5, 6]:
            tk.Radiobutton(tb, text=str(c), variable=self._cols_var, value=c,
                           command=self._schedule_draw,
                           bg=BG_CARD, fg=FG_TEXT, selectcolor=BG_FIELD,
                           activebackground=BG_CARD, font=FONT_LABEL,
                           relief=tk.FLAT, cursor="hand2",
                           indicatoron=0, padx=8, pady=3,
                           highlightthickness=0).pack(side=tk.LEFT, padx=2)

        tk.Button(tb, text=_("gallery_refresh"), command=self._hard_refresh,
                  bg=BG_FIELD, fg=FG_TEXT, font=FONT_LABEL,
                  relief=tk.FLAT, padx=10).pack(side=tk.RIGHT, padx=12)

    def _build_canvas(self):
        frm = tk.Frame(self, bg=BG_GALLERY)
        frm.pack(fill=tk.BOTH, expand=True)
        self._vsb = ttk.Scrollbar(frm, orient=tk.VERTICAL)
        self._vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._cv = tk.Canvas(frm, bg=BG_GALLERY, highlightthickness=0,
                             yscrollcommand=self._vsb.set, cursor="hand2")
        self._cv.pack(fill=tk.BOTH, expand=True)
        self._vsb.config(command=self._cv.yview)
        self._cv.bind("<MouseWheel>",      self._wheel)
        self._cv.bind("<Button-4>",        self._wheel)
        self._cv.bind("<Button-5>",        self._wheel)
        self._cv.bind("<Button-1>",        self._on_click)
        self._cv.bind("<Double-Button-1>", self._on_dbl)

    def _build_statusbar(self):
        self._status = tk.StringVar(value="")
        tk.Label(self, textvariable=self._status, bg=BG_CARD, fg=FG_LABEL,
                 font=FONT_SMALL, anchor=tk.W, padx=8).pack(fill=tk.X)

    # ── Canvas resize (no loop) ─────────────────────────────────────────────
    def _on_cv_configure(self, event):
        """Triggered when the Canvas changes size.
        We only redraw if the WIDTH really changes (not the height: the
        height changes while scrolling, which doesn't need a redraw)."""
        if event.width != self._last_cv_w:
            self._last_cv_w = event.width
            self._schedule_draw()

    # ── Background loading ───────────────────────────────────────────────────
    def _start_loading(self):
        if self._loading:
            return
        to_load = [cid for cid in self._app._ids
                   if (cid, "R") not in self._pil or (cid, "V") not in self._pil]
        # Preload titles too
        for cid in self._app._ids:
            if cid not in self._titles:
                self._titles[cid] = self._read_title(cid)

        if not to_load:
            self._schedule_draw()
            return
        self._loading = True
        self._status.set(_("gallery_loading").format(done=0, total=len(to_load)))
        threading.Thread(target=self._worker, args=(to_load,), daemon=True).start()

    def _worker(self, ids: list[int]):
        for i, cid in enumerate(ids):
            for side in ("R", "V"):
                if (cid, side) not in self._pil:
                    path = self._app._find_gallery_image(cid, side)
                    img  = load_pil(path, GALL_W, GALL_H) if path else None
                    self._queue.put((cid, side, img, i + 1, len(ids)))
        self._queue.put(None)

    def _poll_queue(self):
        import queue as qm
        try:
            while True:
                item = self._queue.get_nowait()
                if item is None:
                    self._loading = False
                    self._status.set(
                        _("gallery_ready").format(total=len(self._app._ids)))
                    self._schedule_draw()
                else:
                    cid, side, img, done, total = item
                    self._pil[(cid, side)] = img
                    self._status.set(
                        _("gallery_loading").format(done=done, total=total))
        except qm.Empty:
            pass
        if self.winfo_exists():
            self.after(30, self._poll_queue)

    # ── Canvas drawing ────────────────────────────────────────────────────────
    def _schedule_draw(self):
        if self._pending_draw:
            try:
                self.after_cancel(self._pending_draw)
            except Exception:
                pass
        self._pending_draw = self.after(80, self._draw)

    def _draw(self):
        self._pending_draw = None
        if not self.winfo_exists():
            return

        self.update_idletasks()
        cv_w = self._cv.winfo_width()
        if cv_w < 10:
            # Window not drawn yet, retry
            self._pending_draw = self.after(100, self._draw)
            return

        mode = self._mode_var.get()
        cols = self._cols_var.get()
        M    = self.TILE_MARGIN

        # Compute tile width
        tile_w = max(80, (cv_w - M * (cols + 1)) // cols)

        # Image height depending on the mode
        if mode == "RV":
            # two images side by side → each image gets half
            img_w  = (tile_w - 2 * self.IMG_PAD - 2) // 2   # 2 px separator
            img_h  = int(img_w * GALL_H / GALL_W)
        else:
            img_w  = tile_w - 2 * self.IMG_PAD
            img_h  = int(img_w * GALL_H / GALL_W)

        tile_h = self.HDR_H + self.BADGE_H + img_h + 2 * self.IMG_PAD + 4

        hit: list[tuple] = []
        self._cv.delete("all")

        for pos, cid in enumerate(self._app._ids):
            row, col_i = divmod(pos, cols)
            x0 = M + col_i * (tile_w + M)
            y0 = M + row   * (tile_h + M)
            x1 = x0 + tile_w
            y1 = y0 + tile_h

            selected = (cid == self._sel_id)
            border   = GALL_SEL if selected else GALL_BORDER
            bw       = 3 if selected else 1

            # Tile background
            self._cv.create_rectangle(x0, y0, x1, y1,
                                      fill=BG_CARD, outline=border, width=bw)

            # Header
            self._cv.create_rectangle(x0, y0, x1, y0 + self.HDR_H,
                                      fill=BG_FIELD, outline="")
            title = self._titles.get(cid, "")
            hdr_txt = f"#{cid}" + (f"  {title[:28]}" if title else "")
            self._cv.create_text(x0 + 5, y0 + self.HDR_H // 2,
                                 text=hdr_txt, anchor=tk.W,
                                 fill=FG_ACCENT2, font=("Courier", 8, "bold"))

            # Images
            img_y = y0 + self.HDR_H + self.IMG_PAD

            if mode == "RV":
                half = (tile_w - 2 * self.IMG_PAD - 2) // 2
                rx0 = x0 + self.IMG_PAD
                self._draw_badge(rx0, img_y, rx0 + half, img_y + self.BADGE_H, "R")
                self._draw_image(cid, "R", rx0, img_y + self.BADGE_H,
                                 rx0 + half, img_y + self.BADGE_H + img_h)
                sep_x = x0 + self.IMG_PAD + half + 1
                self._cv.create_line(sep_x, img_y, sep_x, y1 - self.IMG_PAD,
                                     fill=FG_ACCENT, width=2)
                vx0 = sep_x + 1
                self._draw_badge(vx0, img_y, x1 - self.IMG_PAD, img_y + self.BADGE_H, "V")
                self._draw_image(cid, "V", vx0, img_y + self.BADGE_H,
                                 x1 - self.IMG_PAD, img_y + self.BADGE_H + img_h)
            else:
                ix0 = x0 + self.IMG_PAD
                ix1 = x1 - self.IMG_PAD
                self._draw_badge(ix0, img_y, ix1, img_y + self.BADGE_H, mode)
                self._draw_image(cid, mode, ix0, img_y + self.BADGE_H,
                                 ix1, img_y + self.BADGE_H + img_h)

            hit.append((x0, y0, x1, y1, cid))

        # Total canvas height
        n_rows = (len(self._app._ids) + cols - 1) // cols
        total_h = n_rows * (tile_h + M) + M
        self._cv.configure(scrollregion=(0, 0, cv_w, total_h))

        self._hit_zones = hit

    def _draw_badge(self, x0: int, y0: int, x1: int, y1: int, side: str):
        bg = "#1a3a1a" if side == "R" else "#1a1a3a"
        fg = "#6adc6a" if side == "R" else "#6a9adc"
        lbl = _("side_recto") if side == "R" else _("side_verso")
        self._cv.create_rectangle(x0, y0, x1, y1, fill=bg, outline="")
        self._cv.create_text((x0 + x1) // 2, (y0 + y1) // 2,
                             text=lbl, fill=fg, font=("Courier", 7, "bold"))

    def _draw_image(self, cid: int, side: str,
                    x0: int, y0: int, x1: int, y1: int):
        """Draw the image (or a placeholder) in the given canvas area.

        PhotoImages are kept in self._tkimg[(cid, side, w, h)] for the
        whole lifetime of the gallery. This prevents the garbage collector
        from freeing an image still referenced by the Canvas. The resize
        is computed from self._pil (raw, never modified PIL image) rather
        than an already-resized version, which avoids distortions and
        color artifacts.
        """
        w = x1 - x0
        h = y1 - y0
        if w <= 0 or h <= 0:
            return

        self._cv.create_rectangle(x0, y0, x1, y1, fill="#0a0a1a", outline="")

        pil_img = self._pil.get((cid, side))
        if pil_img is None:
            self._cv.create_text((x0 + x1) // 2, (y0 + y1) // 2,
                                 text="—", fill=FG_LABEL, font=("Courier", 9))
            return

        # Cache key includes the target size: if the window is resized,
        # the PhotoImage is recreated at the correct size.
        key = (cid, side, w, h)
        if key not in self._tkimg:
            pw, ph = pil_img.size
            scale = min(w / pw, h / ph)
            nw = max(1, int(pw * scale))
            nh = max(1, int(ph * scale))
            # Always resize from the original (raw) PIL image
            resized = pil_img.resize((nw, nh), Image.LANCZOS)
            self._tkimg[key] = ImageTk.PhotoImage(resized)

        tkimg = self._tkimg[key]
        # Center within the area
        iw = tkimg.width()
        ih = tkimg.height()
        cx = x0 + (w - iw) // 2
        cy = y0 + (h - ih) // 2
        self._cv.create_image(cx, cy, image=tkimg, anchor=tk.NW)

    # ── Interactions ──────────────────────────────────────────────────────────
    def _hit_test(self, event) -> int | None:
        """Return the cid under the cursor, accounting for scroll."""
        cy = self._cv.canvasy(event.y)
        cx = self._cv.canvasx(event.x)
        for (x0, y0, x1, y1, cid) in self._hit_zones:
            if x0 <= cx <= x1 and y0 <= cy <= y1:
                return cid
        return None

    def _on_click(self, event):
        cid = self._hit_test(event)
        if cid is not None:
            self._sel_id = cid
            self._schedule_draw()

    def _on_dbl(self, event):
        cid = self._hit_test(event)
        if cid is None:
            return
        if not self._app._ask_save_if_dirty():
            return
        idx = self._app._ids.index(cid)
        self._app._load_card(idx)
        self._app.lift()
        self._app.focus_force()
        self._sel_id = cid
        self._schedule_draw()

    def _wheel(self, e):
        if e.num == 4 or e.delta > 0:
            self._cv.yview_scroll(-3, "units")
        else:
            self._cv.yview_scroll(3,  "units")

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _read_title(self, cid: int) -> str:
        data = self._app.model.load_json(cid)
        return data.get("title") or data.get("title2") or ""

    def _hard_refresh(self):
        self._pil.clear()
        self._tkimg.clear()
        self._titles.clear()
        self._loading = False
        self._start_loading()

    def _on_close(self):
        self._app._gallery = None
        self.destroy()

    def notify_card_changed(self, cid: int):
        """Called by the main app after navigation/save."""
        self._sel_id = cid
        self._titles.pop(cid, None)
        self._titles[cid] = self._read_title(cid)
        for side in ("R", "V"):
            self._pil.pop((cid, side), None)
            # Remove all _tkimg entries for this card (every size)
            for key in [k for k in self._tkimg if k[0] == cid and k[1] == side]:
                del self._tkimg[key]
        self._schedule_draw()


# ─────────────────────────────────────────────────────────────────────────────
#  Collection editor (checkboxes from a predefined list)
# ─────────────────────────────────────────────────────────────────────────────
class CollectionEditor(tk.Toplevel):
    """Displays the available collections (read from postcards.conf) as
    checkboxes. Checked values are saved into the JSON.
    If the config is empty or missing, a free-text editor is used instead."""

    def __init__(self, parent, current: list[str],
                 available: list[str], on_save, t):
        super().__init__(parent)
        self.title(_("coll_editor_title"))
        self.configure(bg=BG_MAIN)
        self.resizable(False, True)
        self._on_save = on_save
        self._t = t
        self._vars: dict[str, tk.BooleanVar] = {}

        tk.Label(self, text=_("coll_editor_hint"),
                 bg=BG_MAIN, fg=FG_ACCENT, font=FONT_TITLE).pack(
            padx=14, pady=(12, 6))

        if available:
            # Checkbox list
            scroll_frm = tk.Frame(self, bg=BG_MAIN)
            scroll_frm.pack(fill=tk.BOTH, expand=True, padx=14, pady=4)

            cv = tk.Canvas(scroll_frm, bg=BG_MAIN, highlightthickness=0)
            vsb = ttk.Scrollbar(scroll_frm, orient=tk.VERTICAL, command=cv.yview)
            cv.configure(yscrollcommand=vsb.set)
            vsb.pack(side=tk.RIGHT, fill=tk.Y)
            cv.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

            inner = tk.Frame(cv, bg=BG_MAIN)
            cv.create_window((0, 0), window=inner, anchor=tk.NW)
            inner.bind("<Configure>",
                       lambda _: cv.configure(scrollregion=cv.bbox("all")))

            for val in available:
                var = tk.BooleanVar(value=(val in current))
                self._vars[val] = var
                tk.Checkbutton(
                    inner, text=val, variable=var,
                    bg=BG_MAIN, fg=FG_TEXT, selectcolor=BG_FIELD,
                    activebackground=BG_MAIN, activeforeground=FG_ACCENT2,
                    font=FONT_INPUT, relief=tk.FLAT,
                    anchor=tk.W, cursor="hand2"
                ).pack(fill=tk.X, padx=6, pady=2)

            # Select all / none buttons
            quick = tk.Frame(self, bg=BG_MAIN)
            quick.pack(fill=tk.X, padx=14, pady=(0, 4))
            tk.Button(quick, text=_("coll_select_all"), font=FONT_LABEL,
                      bg=BG_FIELD, fg=FG_TEXT, relief=tk.FLAT, padx=8,
                      command=lambda: [v.set(True) for v in self._vars.values()]
                      ).pack(side=tk.LEFT, padx=(0, 4))
            tk.Button(quick, text=_("coll_select_none"), font=FONT_LABEL,
                      bg=BG_FIELD, fg=FG_TEXT, relief=tk.FLAT, padx=8,
                      command=lambda: [v.set(False) for v in self._vars.values()]
                      ).pack(side=tk.LEFT)

        else:
            # No configuration: free-text entry
            tk.Label(self, text=_("coll_no_conf"),
                     bg=BG_MAIN, fg=FG_LABEL, font=FONT_LABEL,
                     wraplength=340).pack(padx=14, pady=(0, 4))
            self._free_var = tk.StringVar(value=", ".join(current))
            e = tk.Entry(self, textvariable=self._free_var, width=44,
                         bg=BG_INPUT, fg=FG_TEXT, insertbackground=FG_TEXT,
                         font=FONT_INPUT, relief=tk.FLAT)
            e.pack(padx=14, pady=4, fill=tk.X)
            context_menu(e)

        self._available = available

        bot = tk.Frame(self, bg=BG_MAIN)
        bot.pack(fill=tk.X, padx=14, pady=(4, 12))
        tk.Button(bot, text=_("btn_save_close"), command=self._save,
                  bg=FG_ACCENT, fg="#fff", font=FONT_LABEL,
                  relief=tk.FLAT, padx=10).pack(side=tk.RIGHT, padx=4)
        tk.Button(bot, text=_("btn_cancel"), command=self.destroy,
                  bg=BG_FIELD, fg=FG_TEXT, font=FONT_LABEL,
                  relief=tk.FLAT, padx=10).pack(side=tk.RIGHT, padx=4)

        h = min(350 + len(available) * 28, 520) if available else 160
        self.geometry(f"380x{h}")

    def _save(self):
        if self._vars:
            result = [v for v, var in self._vars.items() if var.get()]
        else:
            result = [c.strip()
                      for c in self._free_var.get().split(",")
                      if c.strip()]
        self._on_save(result)
        self.destroy()


# ─────────────────────────────────────────────────────────────────────────────
#  Doubles editor (list of integers referencing other cards)
# ─────────────────────────────────────────────────────────────────────────────
class DoublesEditor(tk.Toplevel):
    """Editor for a list of integers (duplicate card IDs).
    Individual entry with integer validation, plus a quick-add button."""

    def __init__(self, parent, current: list[int], on_save, t):
        super().__init__(parent)
        self.title(_("dbl_editor_title"))
        self.configure(bg=BG_MAIN)
        self.resizable(True, True)
        self._on_save = on_save
        self._t = t

        tk.Label(self, text=_("dbl_editor_hint"),
                 bg=BG_MAIN, fg=FG_ACCENT, font=FONT_TITLE).pack(
            padx=12, pady=(12, 4))

        self._lb = tk.Listbox(self, bg=BG_INPUT, fg=FG_TEXT,
                              selectbackground=FG_ACCENT, font=FONT_INPUT,
                              width=24, height=10, relief=tk.FLAT,
                              activestyle="none")
        self._lb.pack(padx=12, pady=4, fill=tk.BOTH, expand=True)
        for v in current:
            self._lb.insert(tk.END, str(v))

        # Input bar
        row = tk.Frame(self, bg=BG_MAIN)
        row.pack(fill=tk.X, padx=12, pady=4)
        self._evar = tk.StringVar()
        e = tk.Entry(row, textvariable=self._evar, width=14,
                     bg=BG_INPUT, fg=FG_TEXT, insertbackground=FG_TEXT,
                     font=FONT_INPUT, relief=tk.FLAT)
        e.pack(side=tk.LEFT, padx=(0, 6))
        e.bind("<Return>", lambda _: self._add())
        context_menu(e)
        tk.Button(row, text=_("btn_add"), command=self._add,
                  bg=BG_FIELD, fg=FG_TEXT, font=FONT_LABEL,
                  relief=tk.FLAT, padx=6).pack(side=tk.LEFT, padx=2)
        tk.Button(row, text=_("btn_update"), command=self._update,
                  bg=BG_FIELD, fg=FG_TEXT, font=FONT_LABEL,
                  relief=tk.FLAT, padx=6).pack(side=tk.LEFT, padx=2)

        # Controls
        ctrl = tk.Frame(self, bg=BG_MAIN)
        ctrl.pack(fill=tk.X, padx=12, pady=4)
        for txt, cmd, bg in [(_("btn_move_up"),   self._up,   BG_FIELD),
                              (_("btn_move_down"), self._down, BG_FIELD),
                              (_("btn_delete"),    self._del,  "#5a1a1a")]:
            tk.Button(ctrl, text=txt, command=cmd, bg=bg, fg=FG_TEXT,
                      font=FONT_LABEL, relief=tk.FLAT, padx=6).pack(
                side=tk.LEFT, padx=2)

        self._lb.bind("<<ListboxSelect>>", self._on_sel)

        bot = tk.Frame(self, bg=BG_MAIN)
        bot.pack(fill=tk.X, padx=12, pady=(4, 12))
        tk.Button(bot, text=_("btn_save_close"), command=self._save,
                  bg=FG_ACCENT, fg="#fff", font=FONT_LABEL,
                  relief=tk.FLAT, padx=10).pack(side=tk.RIGHT, padx=4)
        tk.Button(bot, text=_("btn_cancel"), command=self.destroy,
                  bg=BG_FIELD, fg=FG_TEXT, font=FONT_LABEL,
                  relief=tk.FLAT, padx=10).pack(side=tk.RIGHT, padx=4)

        self.geometry("300x360")

    def _on_sel(self, _):
        s = self._lb.curselection()
        if s:
            self._evar.set(self._lb.get(s[0]))

    def _parse(self) -> int | None:
        try:
            return int(self._evar.get().strip())
        except ValueError:
            messagebox.showerror(_("error_title"),
                                 _("dbl_not_integer"), parent=self)
            return None

    def _add(self):
        v = self._parse()
        if v is not None:
            self._lb.insert(tk.END, str(v))
            self._evar.set("")

    def _update(self):
        s = self._lb.curselection()
        if not s:
            return
        v = self._parse()
        if v is not None:
            i = s[0]
            self._lb.delete(i)
            self._lb.insert(i, str(v))
            self._lb.selection_set(i)

    def _del(self):
        s = self._lb.curselection()
        if s:
            self._lb.delete(s[0])
            self._evar.set("")

    def _up(self):
        s = self._lb.curselection()
        if not s or s[0] == 0:
            return
        i = s[0]
        v = self._lb.get(i)
        self._lb.delete(i)
        self._lb.insert(i - 1, v)
        self._lb.selection_set(i - 1)

    def _down(self):
        s = self._lb.curselection()
        if not s or s[0] == self._lb.size() - 1:
            return
        i = s[0]
        v = self._lb.get(i)
        self._lb.delete(i)
        self._lb.insert(i + 1, v)
        self._lb.selection_set(i + 1)

    def _save(self):
        result = []
        for i in range(self._lb.size()):
            try:
                result.append(int(self._lb.get(i)))
            except ValueError:
                pass
        self._on_save(result)
        self.destroy()


# ─────────────────────────────────────────────────────────────────────────────
#  Main application
# ─────────────────────────────────────────────────────────────────────────────
class App(tk.Tk):
    # (key, label_key, single_line, height)
    TEXT_FIELDS: list[tuple] = [
        ("title",       "field_title",       True,  1),
        ("title2",      "field_title2",      True,  1),
        ("description", "field_description", False, 2),
        ("recto_ocr",   "field_recto_ocr",   False, 4),
        ("verso_ocr",   "field_verso_ocr",   False, 4),
        ("recto_text",  "field_recto_text",  False, 2),
        ("verso_text",  "field_verso_text",  False, 2),
    ]
    # List fields: (key, label_key)
    LIST_FIELDS: list[tuple] = [
        ("address", "field_address"),
        ("poi",     "field_poi"),
    ]

    def __init__(self, datadir: Path, conf_file: Path = Path("postcards.conf")):
        super().__init__()
        self.conf_file = conf_file
        self.config_parser = self._load_config()

        self.datadir = datadir
        self.cards_dir = datadir / "cards"  # JSON sources

        section = self.config_parser["tkmanager"] \
            if self.config_parser.has_section("tkmanager") else {}

        images_subdir = section.get("images_dir", "size_div1")
        gallery_subdir = section.get("gallery_images_dir", "size_div3")
        self.images_dir = datadir / images_subdir          # full resolution PNGs
        self.gallery_images_dir = datadir / gallery_subdir  # gallery thumbnails

        self._t = setup_i18n()

        self.model = Model(datadir)

        self.collections: list[str] = self._load_collections()
        self.title(_("app_title"))
        self.configure(bg=BG_MAIN)
        self.geometry("1200x860")
        self.minsize(980, 700)

        self._ids: list[int] = []
        self._current_idx = 0
        self._data: dict = {}
        self._thumb_refs: dict = {}
        self._viewers: dict = {}
        self._field_dialogs: list = []
        self._coord: list = []
        self._dirty = False
        self._gallery: GalleryView | None = None
        self._search_win: SearchView | None = None
        self._text_search_win: "TextSearchView | None" = None
        self._doubles_win: "DoublesSearchView | None" = None
        self._poi_win: "PoiManagerView | None" = None
        self._nav_collection: str | None = self._load_last_filter()

        self._scan_ids()

        # If the saved filter leaves no cards, fall back to "all collections"
        if not self._ids and self._nav_collection:
            self._nav_collection = None
            self._scan_ids()

        self._build_ui()
        self._nav_filter_var.set(self._nav_collection if self._nav_collection else _("tsearch_all"))
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        if self._ids:
            start_idx = self._initial_index()
            self._load_card(start_idx)
        else:
            self.update_idletasks()
            try:
                messagebox.showwarning(
                    _("no_cards_title"),
                    _("no_cards_msg").format(dir=self.cards_dir),
                    parent=self)
            except tk.TclError:
                print(_("no_cards_msg").format(dir=self.cards_dir))

    # ── Configuration ─────────────────────────────────────────────────────────
    def _load_config(self) -> configparser.ConfigParser:
        """Read the postcards.conf ini file."""
        cfg = configparser.ConfigParser()
        try:
            cfg.read(self.conf_file, encoding="utf-8")
        except Exception:
            pass
        return cfg

    def _load_collections(self) -> list[str]:
        """Read the list of collections from the tkmanager section."""
        try:
            raw = self.config_parser.get("tkmanager", "collections", fallback="")
            return [c.strip() for c in raw.split(",") if c.strip()]
        except Exception:
            return []

    def _load_last_filter(self) -> str | None:
        """Read the last used collection filter from postcards.conf."""
        try:
            last_filter = self.config_parser.get("tkmanager", "last_filter", fallback="")
            if last_filter and last_filter in self.collections:
                return last_filter
        except Exception:
            pass
        return None

    def _initial_index(self) -> int:
        """Determine the initial card index from the saved last_id."""
        try:
            last_id = self.config_parser.get("tkmanager", "last_id", fallback="")
            if last_id:
                cid = int(last_id)
                if cid in self._ids:
                    return self._ids.index(cid)
        except Exception:
            pass
        return 0

    def _save_last_id(self, cid: int):
        """Persist the last edited card id and navigation filter to postcards.conf."""
        try:
            if not self.config_parser.has_section("tkmanager"):
                self.config_parser.add_section("tkmanager")
            self.config_parser.set("tkmanager", "last_id", str(cid))
            self.config_parser.set("tkmanager", "last_filter", self._nav_collection or "")
            with open(self.conf_file, "w", encoding="utf-8") as f:
                self.config_parser.write(f)
        except Exception as e:
            print(f"[postcards.conf] Update error: {e}")

    # ── Scan ──────────────────────────────────────────────────────────────────
    def _scan_ids(self):
        collection = self._nav_collection if getattr(self, "_nav_collection", None) else None
        try:
            cards = self.model.list_cards(collection=collection)
            ids = []
            for data in cards:
                try:
                    ids.append(int(data.get("id")))
                except (TypeError, ValueError):
                    pass
            self._ids = sorted(ids)
        except Exception:
            ids = []
            for f in self.cards_dir.glob("*.json"):
                try:
                    ids.append(int(f.stem))
                except ValueError:
                    pass
            self._ids = sorted(ids)

    # ── Dirty / save guard ────────────────────────────────────────────────────
    def _mark_dirty(self, *_):
        if not self._dirty:
            self._dirty = True
            self._btn_save.config(bg=BTN_DIRTY)

    def _mark_clean(self):
        self._dirty = False
        self._btn_save.config(bg=BTN_CLEAN)

    def _ask_save_if_dirty(self) -> bool:
        if not self._dirty:
            return True
        cid = self._ids[self._current_idx]
        ans = messagebox.askyesnocancel(
            _("unsaved_title"),
            _("unsaved_msg").format(id=cid), parent=self)
        if ans is True:
            self._save_json()
            return True
        if ans is False:
            return True
        return False

    # ── UI construction ───────────────────────────────────────────────────────
    def _build_ui(self):

        # Navigation bar
        nav = tk.Frame(self, bg=BG_CARD, pady=6)
        nav.pack(fill=tk.X)

        self._btn_prev = tk.Button(nav, text=_("nav_prev"), command=self._go_prev,
                                   bg=BG_FIELD, fg=FG_TEXT, font=FONT_NAV,
                                   relief=tk.FLAT, padx=14, pady=4, cursor="hand2")
        self._btn_prev.pack(side=tk.LEFT, padx=12)

        self._lbl_counter = tk.Label(nav, text="", bg=BG_CARD,
                                     fg=FG_ACCENT2, font=FONT_TITLE)
        self._lbl_counter.pack(side=tk.LEFT, padx=8)

        # Collection filter for navigation
        filt_frm = tk.Frame(nav, bg=BG_CARD)
        filt_frm.pack(side=tk.LEFT, padx=8)
        tk.Label(filt_frm, text=_("nav_filter_label"), bg=BG_CARD, fg=FG_LABEL,
                 font=FONT_LABEL).pack(side=tk.LEFT, padx=(0, 4))
        self._nav_filter_var = tk.StringVar(value=_("tsearch_all"))
        choices = [_("tsearch_all")] + self.collections
        self._nav_filter_menu = ttk.Combobox(filt_frm, textvariable=self._nav_filter_var,
                                             values=choices, width=14,
                                             font=FONT_INPUT, state="readonly")
        self._nav_filter_menu.pack(side=tk.LEFT)
        self._nav_filter_menu.bind("<<ComboboxSelected>>", self._on_nav_filter_changed)

        # Free id entry
        goto_frm = tk.Frame(nav, bg=BG_CARD)
        goto_frm.pack(side=tk.LEFT, padx=8)
        tk.Label(goto_frm, text=_("goto_label"), bg=BG_CARD, fg=FG_LABEL,
                 font=FONT_LABEL).pack(side=tk.LEFT, padx=(0, 4))
        self._goto_var = tk.StringVar()
        goto_entry = tk.Entry(goto_frm, textvariable=self._goto_var, width=6,
                              bg=BG_INPUT, fg=FG_TEXT, insertbackground=FG_TEXT,
                              font=FONT_INPUT, relief=tk.FLAT)
        goto_entry.pack(side=tk.LEFT, padx=(0, 4))
        goto_entry.bind("<Return>", lambda _: self._goto())
        context_menu(goto_entry)
        tk.Button(goto_frm, text=_("goto_btn"), command=self._goto,
                  bg=BG_FIELD, fg=FG_TEXT, font=FONT_LABEL,
                  relief=tk.FLAT, padx=6).pack(side=tk.LEFT)

        self._btn_more = tk.Button(nav, text=_("nav_more"),
                                   command=self._open_more_menu,
                                   bg=BG_FIELD, fg=FG_TEXT, font=FONT_NAV,
                                   relief=tk.FLAT, padx=14, pady=4, cursor="hand2")
        self._btn_more.pack(side=tk.RIGHT, padx=6)

        self._btn_next = tk.Button(nav, text=_("nav_next"), command=self._go_next,
                                   bg=BG_FIELD, fg=FG_TEXT, font=FONT_NAV,
                                   relief=tk.FLAT, padx=14, pady=4, cursor="hand2")
        self._btn_next.pack(side=tk.RIGHT, padx=12)

        self._btn_save = tk.Button(nav, text=_("nav_save"), command=self._save_json,
                                   bg=BTN_CLEAN, fg="#fff", font=FONT_NAV,
                                   relief=tk.FLAT, padx=14, pady=4, cursor="hand2")
        self._btn_save.pack(side=tk.RIGHT, padx=6)

        # Body
        body = tk.Frame(self, bg=BG_MAIN)
        body.pack(fill=tk.BOTH, expand=True, padx=10, pady=6)

        # Left column: thumbnails
        left = tk.Frame(body, bg=BG_CARD, width=275, relief=tk.FLAT)
        left.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 8))
        left.pack_propagate(False)
        self._build_thumbs(left)

        # Right column: scrollable fields
        right = tk.Frame(body, bg=BG_MAIN)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        cv = tk.Canvas(right, bg=BG_MAIN, highlightthickness=0)
        sb = ttk.Scrollbar(right, orient=tk.VERTICAL, command=cv.yview)
        cv.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        cv.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._ff = tk.Frame(cv, bg=BG_MAIN)
        self._fw = cv.create_window((0, 0), window=self._ff, anchor=tk.NW)
        self._ff.bind("<Configure>",
                      lambda _: cv.configure(scrollregion=cv.bbox("all")))
        cv.bind("<Configure>",
                lambda e: cv.itemconfig(self._fw, width=e.width))
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            cv.bind(seq, lambda e, c=cv: c.yview_scroll(
                -1 if (e.delta > 0 or e.num == 4) else 1, "units"))

        self._build_fields(self._ff)

    def _build_thumbs(self, parent):
        for lk, side in [("side_recto", "R"), ("side_verso", "V")]:
            tk.Label(parent, text=_(lk), bg=BG_CARD, fg=FG_ACCENT,
                     font=("Georgia", 10, "bold")).pack(pady=(14, 2))
            attr = "_lbl_r" if side == "R" else "_lbl_v"
            lbl = tk.Label(parent, bg=BG_CARD, cursor="hand2",
                           text=_("click_to_enlarge"), fg=FG_LABEL, font=FONT_LABEL)
            lbl.pack(padx=10, pady=4)
            lbl.bind("<Button-1>", lambda e, s=side: self._open_viewer(s))
            setattr(self, attr, lbl)
        tk.Label(parent, bg=BG_CARD).pack(expand=True)

    def _build_fields(self, parent):
        self._tw: dict[str, tk.Widget] = {}

        # Simple text fields
        for key, lk, single, height in self.TEXT_FIELDS:
            frm = tk.Frame(parent, bg=BG_CARD)
            frm.pack(fill=tk.X, padx=8, pady=3)
            tk.Label(frm, text=_(lk), bg=BG_CARD, fg=FG_LABEL,
                     font=FONT_LABEL, width=14, anchor=tk.NW).pack(
                side=tk.LEFT, padx=(10, 4), pady=5, anchor=tk.N)
            if single:
                w = tk.Entry(frm, bg=BG_INPUT, fg=FG_TEXT,
                             insertbackground=FG_TEXT, font=FONT_INPUT, relief=tk.FLAT)
                w.pack(side=tk.LEFT, fill=tk.X, expand=True, pady=5, padx=(0, 10))
                w.bind("<Key>", lambda _: self.after_idle(self._mark_dirty))
                context_menu(w)
            else:
                w = tk.Text(frm, height=height, wrap=tk.WORD,
                            bg=BG_INPUT, fg=FG_TEXT, insertbackground=FG_TEXT,
                            font=FONT_INPUT, relief=tk.FLAT, padx=6, pady=4, undo=True)
                w.pack(side=tk.LEFT, fill=tk.X, expand=True, pady=5, padx=(0, 10))
                w.bind("<Key>", lambda _: self.after_idle(self._mark_dirty))
                context_menu(w)
            self._tw[key] = w

        # Date
        sep(parent)
        df = tk.Frame(parent, bg=BG_CARD)
        df.pack(fill=tk.X, padx=8, pady=3)
        tk.Label(df, text=_("field_date"), bg=BG_CARD, fg=FG_LABEL,
                 font=FONT_LABEL, width=14, anchor=tk.W).pack(side=tk.LEFT, padx=(10, 4), pady=5)
        self._date_field = DateField(df, on_change=self._mark_dirty)
        self._date_field.pack(side=tk.LEFT, pady=5)

        # List fields (address, poi, …)
        sep(parent)
        self._list_labels: dict[str, tk.Label] = {}
        for key, lk in self.LIST_FIELDS:
            frm = tk.Frame(parent, bg=BG_CARD)
            frm.pack(fill=tk.X, padx=8, pady=3)
            tk.Label(frm, text=_(lk), bg=BG_CARD, fg=FG_LABEL,
                     font=FONT_LABEL, width=14, anchor=tk.W).pack(side=tk.LEFT, padx=(10, 4), pady=5)
            lbl = tk.Label(frm, text="", bg=BG_INPUT, fg=FG_TEXT,
                           font=FONT_INPUT, anchor=tk.W, justify=tk.LEFT,
                           relief=tk.FLAT, padx=6, wraplength=650)
            lbl.pack(side=tk.LEFT, fill=tk.X, expand=True, pady=5)
            tk.Button(frm, text=_("btn_edit"),
                      command=lambda k=key, lk=lk: self._edit_list(k, lk),
                      bg=BG_FIELD, fg=FG_ACCENT2, font=FONT_LABEL,
                      relief=tk.FLAT, padx=8, cursor="hand2").pack(side=tk.RIGHT, padx=8)
            self._list_labels[key] = lbl

        # Collections (choice from a predefined list)
        sep(parent)
        cf = tk.Frame(parent, bg=BG_CARD)
        cf.pack(fill=tk.X, padx=8, pady=3)
        tk.Label(cf, text=_("field_collections"), bg=BG_CARD, fg=FG_LABEL,
                 font=FONT_LABEL, width=14, anchor=tk.W).pack(side=tk.LEFT, padx=(10, 4), pady=5)
        self._lbl_collections = tk.Label(cf, text="", bg=BG_INPUT, fg=FG_TEXT,
                                         font=FONT_INPUT, anchor=tk.W, justify=tk.LEFT,
                                         relief=tk.FLAT, padx=6, wraplength=650)
        self._lbl_collections.pack(side=tk.LEFT, fill=tk.X, expand=True, pady=5)
        tk.Button(cf, text=_("btn_edit"), command=self._edit_collections,
                  bg=BG_FIELD, fg=FG_ACCENT2, font=FONT_LABEL,
                  relief=tk.FLAT, padx=8, cursor="hand2").pack(side=tk.RIGHT, padx=8)

        # Doubles (list of integers)
        sep(parent)
        dbl_f = tk.Frame(parent, bg=BG_CARD)
        dbl_f.pack(fill=tk.X, padx=8, pady=3)
        tk.Label(dbl_f, text=_("field_doubles"), bg=BG_CARD, fg=FG_LABEL,
                 font=FONT_LABEL, width=14, anchor=tk.W).pack(side=tk.LEFT, padx=(10, 4), pady=5)
        self._lbl_doubles = tk.Label(dbl_f, text="", bg=BG_INPUT, fg=FG_TEXT,
                                     font=FONT_INPUT, anchor=tk.W, justify=tk.LEFT,
                                     relief=tk.FLAT, padx=6, wraplength=650)
        self._lbl_doubles.pack(side=tk.LEFT, fill=tk.X, expand=True, pady=5)
        tk.Button(dbl_f, text=_("btn_edit"), command=self._edit_doubles,
                  bg=BG_FIELD, fg=FG_ACCENT2, font=FONT_LABEL,
                  relief=tk.FLAT, padx=8, cursor="hand2").pack(side=tk.RIGHT, padx=8)

        # GPS
        sep(parent)
        gf = tk.Frame(parent, bg=BG_CARD)
        gf.pack(fill=tk.X, padx=8, pady=3)
        tk.Label(gf, text=_("field_gps"), bg=BG_CARD, fg=FG_LABEL,
                 font=FONT_LABEL, width=14, anchor=tk.W).pack(side=tk.LEFT, padx=(10, 4), pady=5)
        self._lbl_coord = tk.Label(gf, text="", bg=BG_INPUT, fg=FG_TEXT,
                                   font=FONT_INPUT, anchor=tk.W, relief=tk.FLAT, padx=6)
        self._lbl_coord.pack(side=tk.LEFT, fill=tk.X, expand=True, pady=5)
        self._btn_osm = tk.Button(gf, text=_("btn_osm"), command=self._osm_direct,
                                   bg=FG_LINK, fg="#000", font=FONT_LABEL,
                                   relief=tk.FLAT, padx=8, cursor="hand2")
        self._btn_osm.pack(side=tk.RIGHT, padx=2)
        tk.Button(gf, text=_("btn_edit"), command=self._edit_coord,
                  bg=BG_FIELD, fg=FG_ACCENT2, font=FONT_LABEL,
                  relief=tk.FLAT, padx=8, cursor="hand2").pack(side=tk.RIGHT, padx=4)

        tk.Frame(parent, bg=BG_MAIN, height=24).pack()

    # ── Card loading ──────────────────────────────────────────────────────────
    def _load_card(self, idx: int):
        self._current_idx = idx
        cid = self._ids[idx]
        try:
            self._data = self.model.load_json(cid)
        except Exception as e:
            messagebox.showerror(_("error_title"),
                                 _("error_read").format(
                                     path=self.cards_dir / f"{cid}.json", err=e))
            return

        self._lbl_counter.config(
            text=f"#{cid}  ({idx + 1}/{len(self._ids)})")
        self._btn_prev.config(state=tk.NORMAL if idx > 0 else tk.DISABLED)
        self._btn_next.config(state=tk.NORMAL if idx < len(self._ids) - 1 else tk.DISABLED)

        for key, _lk, single, _h in self.TEXT_FIELDS:
            val = str(self._data.get(key) or "")
            w = self._tw[key]
            if isinstance(w, tk.Entry):
                w.delete(0, tk.END)
                w.insert(0, val)
            else:
                w.delete("1.0", tk.END)
                w.insert("1.0", val)

        self._date_field.set_value(self._data.get("date"))

        for key, _lk in self.LIST_FIELDS:
            vals = self._data.get(key) or []
            self._list_labels[key].config(
                text=" / ".join(vals) if vals else "")

        self._coord = self._data.get("coord") or []
        self._refresh_coord()

        # Collections
        cols_val = self._data.get("collections") or []
        self._lbl_collections.config(
            text=", ".join(cols_val) if cols_val else "")

        # Doubles
        dbl_val = self._data.get("doubles") or []
        self._lbl_doubles.config(
            text=", ".join(str(v) for v in dbl_val) if dbl_val else "")

        self._load_thumbs(cid)

        for v in list(self._viewers.values()):
            try:
                v.destroy()
            except Exception:
                pass
        self._viewers.clear()

        for win in list(self._field_dialogs):
            try:
                if win.winfo_exists():
                    win.destroy()
            except Exception:
                pass
        self._field_dialogs.clear()

        self._mark_clean()
        self._save_last_id(cid)

        # Notify the gallery
        if self._gallery and self._gallery.winfo_exists():
            self._gallery.notify_card_changed(cid)

    def _load_thumbs(self, cid: int):
        for side, attr in [("R", "_lbl_r"), ("V", "_lbl_v")]:
            lbl = getattr(self, attr)
            path = self._find_image(cid, side)
            if path and PIL_AVAILABLE:
                img = load_pil(path, *THUMB_SIZE)
                if img:
                    tk_img = pil_to_tk(img)
                    self._thumb_refs[f"{cid}_{side}"] = tk_img
                    lbl.config(image=tk_img, text="", relief=tk.RIDGE, bd=2)
                    continue
            lbl.config(image="", relief=tk.FLAT,
                       text=_("image_unavailable").format(side=side),
                       fg=FG_LABEL, font=FONT_LABEL)

    def _find_image(self, cid: int, side: str) -> Path | None:
        p = self.images_dir / f"{cid}_{side}.png"
        return p if p.exists() else None

    def _find_gallery_image(self, cid: int, side: str) -> Path | None:
        """Reduced image for the gallery (gallery_images_dir).
        Falls back to the full-resolution image."""
        p = self.gallery_images_dir / f"{cid}_{side}.png"
        if p.exists():
            return p
        return self._find_image(cid, side)   # fallback if missing

    def _refresh_coord(self):
        if len(self._coord) >= 2:
            self._lbl_coord.config(
                text=f"lat {self._coord[0]:.6f}  /  lon {self._coord[1]:.6f}")
            self._btn_osm.config(state=tk.NORMAL)
        else:
            self._lbl_coord.config(text="")
            self._btn_osm.config(state=tk.DISABLED)

    # ── Navigation ────────────────────────────────────────────────────────────
    # ── Collection filter for navigation ─────────────────────────────────────
    def _on_nav_filter_changed(self, _event=None):
        if not self._ask_save_if_dirty():
            # Revert the combobox to the previous value
            self._nav_filter_var.set(
                self._nav_collection if self._nav_collection else _("tsearch_all"))
            return
        choice = self._nav_filter_var.get()
        all_label = _("tsearch_all")
        current_cid = self._ids[self._current_idx] if self._ids else None

        self._nav_collection = None if (not choice or choice == all_label) else choice
        self._scan_ids()

        if not self._ids:
            messagebox.showinfo(_("info_title"),
                                _("nav_filter_empty"))
            self._nav_collection = None
            self._nav_filter_var.set(all_label)
            self._scan_ids()

        # Try to keep the same card visible if it's still in the filtered list
        if current_cid is not None and current_cid in self._ids:
            self._load_card(self._ids.index(current_cid))
        else:
            self._load_card(0)

    def _go_prev(self):
        if self._current_idx > 0 and self._ask_save_if_dirty():
            self._load_card(self._current_idx - 1)

    def _go_next(self):
        if self._current_idx < len(self._ids) - 1 and self._ask_save_if_dirty():
            self._load_card(self._current_idx + 1)

    def _goto(self):
        raw = self._goto_var.get().strip()
        if not raw:
            return
        try:
            cid = int(raw)
        except ValueError:
            messagebox.showwarning(_("info_title"),
                                   _("goto_not_found").format(id=raw))
            return
        if cid not in self._ids:
            messagebox.showwarning(_("info_title"),
                                   _("goto_not_found").format(id=cid))
            return
        if self._ask_save_if_dirty():
            self._goto_var.set("")
            self._load_card(self._ids.index(cid))

    def _on_close(self):
        if self._ask_save_if_dirty():
            self.model.close()
            self.destroy()

    # ── "More" dropdown menu ──────────────────────────────────────────────────
    def _open_more_menu(self):
        m = tk.Menu(self, tearoff=0, bg=BG_FIELD, fg=FG_TEXT,
                    activebackground=FG_ACCENT, activeforeground="#fff")
        m.add_command(label=_("nav_textsearch"), command=self._open_text_search)
        m.add_command(label=_("nav_similar"), command=self._open_search)
        m.add_command(label=_("nav_doubles"), command=self._open_doubles_search)
        m.add_command(label=_("nav_pois"), command=self._open_poi_manager)
        m.add_command(label=_("nav_gallery"), command=self._open_gallery)

        # Close the menu as soon as it loses focus (click outside,
        # Escape, etc.) instead of relying on tk_popup's own grab.
        m.bind("<FocusOut>", lambda _e: m.unpost())

        x = self._btn_more.winfo_rootx()
        y = self._btn_more.winfo_rooty() + self._btn_more.winfo_height()
        try:
            m.tk_popup(x, y)
        finally:
            # Let tk_popup's own grab manage closing the menu; releasing
            # the grab here immediately would prevent outside clicks from
            # dismissing it properly.
            pass

    # ── Gallery ───────────────────────────────────────────────────────────────
    def _open_gallery(self):
        if self._gallery and self._gallery.winfo_exists():
            self._gallery.lift()
            self._gallery.focus_force()
            return
        self._gallery = GalleryView(self, self._t)

    # ── URL-based search ──────────────────────────────────────────────────────
    def _open_search(self):
        if self._search_win and self._search_win.winfo_exists():
            self._search_win.lift()
            self._search_win.focus_force()
            return
        self._search_win = SearchView(self, self._t)

    # ── Text search ───────────────────────────────────────────────────────────
    def _open_text_search(self):
        if self._text_search_win and self._text_search_win.winfo_exists():
            self._text_search_win.lift()
            self._text_search_win.focus_force()
            return
        self._text_search_win = TextSearchView(self, self._t)

    # ── Missing doubles search ───────────────────────────────────────────────
    def _open_doubles_search(self):
        if self._doubles_win and self._doubles_win.winfo_exists():
            self._doubles_win.lift()
            self._doubles_win.focus_force()
            return
        self._doubles_win = DoublesSearchView(self, self._t)

    # ── POI manager ───────────────────────────────────────────────────────────
    def _open_poi_manager(self):
        if self._poi_win and self._poi_win.winfo_exists():
            self._poi_win.lift()
            self._poi_win.focus_force()
            return
        self._poi_win = PoiManagerView(self, self._t)

    # ── Viewer ────────────────────────────────────────────────────────────────
    def _open_viewer(self, side: str):
        cid = self._ids[self._current_idx]
        path = self._find_image(cid, side)
        if not path:
            messagebox.showinfo(_("info_title"),
                                _("image_not_found").format(side=side, id=cid))
            return
        key = f"{cid}_{side}"
        if key in self._viewers:
            try:
                self._viewers[key].lift()
                return
            except tk.TclError:
                pass
        title = f"#{cid} — {'Recto' if side == 'R' else 'Verso'}"
        v = ImageViewer(self, path, title, self._t)
        v.protocol("WM_DELETE_WINDOW", lambda k=key: self._close_viewer(k))
        self._viewers[key] = v

    def _close_viewer(self, key: str):
        if key in self._viewers:
            self._viewers[key].destroy()
            del self._viewers[key]

    # ── Collections ───────────────────────────────────────────────────────────
    def _edit_collections(self):
        current = list(self._data.get("collections") or [])
        def on_save(new_vals):
            self._data["collections"] = new_vals
            self._lbl_collections.config(
                text=", ".join(new_vals) if new_vals else "")
            self._mark_dirty()
        win = CollectionEditor(self, current, self.collections, on_save, self._t)
        self._field_dialogs.append(win)

    # ── Doubles ───────────────────────────────────────────────────────────────
    def _edit_doubles(self):
        current = list(self._data.get("doubles") or [])
        def on_save(new_vals):
            self._data["doubles"] = new_vals
            self._lbl_doubles.config(
                text=", ".join(str(v) for v in new_vals) if new_vals
                else "")
            self._mark_dirty()
        win = DoublesEditor(self, current, on_save, self._t)
        self._field_dialogs.append(win)

    # ── Lists ─────────────────────────────────────────────────────────────────
    def _edit_list(self, key: str, label_key: str):
        lines = list(self._data.get(key) or [])
        def on_save(new_lines):
            self._data[key] = new_lines
            self._list_labels[key].config(
                text=" / ".join(new_lines) if new_lines else "")
            self._mark_dirty()
        if key == "poi":
            win = PoiListEditor(self, _(label_key), lines, on_save, self.model, self._t)
        else:
            win = ListEditor(self, _(label_key), lines, on_save, self._t)
        self._field_dialogs.append(win)

    # ── GPS ───────────────────────────────────────────────────────────────────
    def _edit_coord(self):
        def on_save(c):
            self._coord = c
            self._data["coord"] = c
            self._refresh_coord()
            self._mark_dirty()
        win = CoordDialog(self, list(self._coord), on_save, self._t)
        self._field_dialogs.append(win)

    def _osm_direct(self):
        if len(self._coord) >= 2:
            lat, lon = self._coord[0], self._coord[1]
            webbrowser.open(
                f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=14/{lat}/{lon}")

    # ── Save ──────────────────────────────────────────────────────────────────
    def _save_json(self):
        cid = self._ids[self._current_idx]

        for key, _lk, single, _h in self.TEXT_FIELDS:
            w = self._tw[key]
            val = (w.get().strip() if isinstance(w, tk.Entry)
                   else w.get("1.0", tk.END).rstrip("\n")) or None
            self._data[key] = val

        self._data["date"]        = self._date_field.get_value()
        self._data["coord"]       = self._coord
        self._data["collections"] = self._data.get("collections") or []
        self._data["doubles"]     = self._data.get("doubles") or []

        try:
            self.model.write_json(self._data)
            self._mark_clean()
            orig = self._btn_save.cget("text")
            self._btn_save.config(text=_("nav_saved"), bg="#1a7a3a")
            self.after(1500, lambda: self._btn_save.config(text=orig, bg=BTN_CLEAN))
        except Exception as e:
            messagebox.showerror(_("error_title"),
                                 _("error_save").format(
                                     path=self.cards_dir / f"{cid}.json", err=e))


# ─────────────────────────────────────────────────────────────────────────────
#  URL-based search (PostcardSearcher)
# ─────────────────────────────────────────────────────────────────────────────
class SearchView(tk.Toplevel):
    """Image similarity search window using a URL.

    Top area : URL / threshold / max_results form + Search button
    Bottom area : Canvas results grid with percentage badges.
                  Clicking a thumbnail opens an ImageViewer (full-size recto).
    """

    # Result thumbnail dimensions
    RES_W   = 180
    RES_H   = 130
    RES_PAD = 8
    HDR_H   = 18
    BADGE_H = 20

    def __init__(self, parent: "App", t):
        super().__init__(parent)
        self._app    = parent
        self._t      = t
        self._tkimg: dict[tuple, "ImageTk.PhotoImage"] = {}
        self._hits: list[tuple] = []
        self._results: list[dict] = []
        self._searcher: "PostcardSearcher | None" = None
        self._last_cv_w = 0
        self._pending_draw: str | None = None

        self.title(_("search_title"))
        self.configure(bg=BG_MAIN)
        self.geometry("1100x750")
        self.minsize(700, 500)

        self._build_form()
        self._build_results()
        self._build_statusbar()
        self._cv.bind("<Configure>", self._on_cv_configure)

        # Load the index in the background to avoid blocking the UI
        self._load_index_async()

    # ── Index loading ─────────────────────────────────────────────────────────
    def _load_index_async(self):
        """Load postcards.pkl in a thread to avoid blocking the UI."""
        if not SEARCHER_AVAILABLE:
            return
        index_path = self._app.datadir / "postcards.pkl"
        self._status.set(_("search_index_loading"))
        self._btn_search.config(state=tk.DISABLED)

        def worker():
            try:
                searcher = PostcardSearcher()
                searcher.load_index(str(index_path))
                self.after(0, lambda s=searcher: self._on_index_loaded(s))
            except Exception as e:
                self.after(0, lambda err=e: self._on_index_error(err))

        threading.Thread(target=worker, daemon=True).start()

    def _on_index_loaded(self, searcher: "PostcardSearcher"):
        if not self.winfo_exists():
            return
        self._searcher = searcher
        self._status.set(_("search_index_ready"))
        self._btn_search.config(state=tk.NORMAL)

    def _on_index_error(self, err: Exception):
        if not self.winfo_exists():
            return
        self._status.set(_("search_index_error").format(err=err))
        # Button stays disabled if the index could not be loaded

    # ── Form ──────────────────────────────────────────────────────────────────
    def _build_form(self):
        form = tk.Frame(self, bg=BG_CARD, pady=10)
        form.pack(fill=tk.X, padx=0)

        # Row 1: URL
        row1 = tk.Frame(form, bg=BG_CARD)
        row1.pack(fill=tk.X, padx=14, pady=(0, 6))
        tk.Label(row1, text=_("search_url_label"), bg=BG_CARD, fg=FG_LABEL,
                 font=FONT_LABEL, width=14, anchor=tk.W).pack(side=tk.LEFT)
        self._url_var = tk.StringVar()
        url_entry = tk.Entry(row1, textvariable=self._url_var,
                             bg=BG_INPUT, fg=FG_TEXT, insertbackground=FG_TEXT,
                             font=FONT_INPUT, relief=tk.FLAT)
        url_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 8))
        url_entry.bind("<Return>", lambda _: self._run_search())
        context_menu(url_entry)

        # Row 2: threshold + max_results + button
        row2 = tk.Frame(form, bg=BG_CARD)
        row2.pack(fill=tk.X, padx=14, pady=(0, 4))

        tk.Label(row2, text=_("search_threshold_label"), bg=BG_CARD, fg=FG_LABEL,
                 font=FONT_LABEL, anchor=tk.W).pack(side=tk.LEFT)
        self._thr_var = tk.StringVar(value="0.7")
        thr_entry = tk.Entry(row2, textvariable=self._thr_var, width=6,
                             bg=BG_INPUT, fg=FG_TEXT, insertbackground=FG_TEXT,
                             font=FONT_INPUT, relief=tk.FLAT)
        thr_entry.pack(side=tk.LEFT, padx=(4, 20))
        context_menu(thr_entry)

        tk.Label(row2, text=_("search_maxresults_label"), bg=BG_CARD, fg=FG_LABEL,
                 font=FONT_LABEL, anchor=tk.W).pack(side=tk.LEFT)
        self._max_var = tk.StringVar(value="20")
        max_entry = tk.Entry(row2, textvariable=self._max_var, width=6,
                             bg=BG_INPUT, fg=FG_TEXT, insertbackground=FG_TEXT,
                             font=FONT_INPUT, relief=tk.FLAT)
        max_entry.pack(side=tk.LEFT, padx=(4, 20))
        context_menu(max_entry)

        self._btn_search = tk.Button(row2, text=_("search_btn"),
                                     command=self._run_search,
                                     bg=FG_ACCENT, fg="#fff", font=FONT_NAV,
                                     relief=tk.FLAT, padx=14, pady=3, cursor="hand2")
        self._btn_search.pack(side=tk.LEFT, padx=(0, 8))

        self._btn_clear = tk.Button(row2, text=_("search_clear"),
                                    command=self._clear_results,
                                    bg=BG_FIELD, fg=FG_TEXT, font=FONT_LABEL,
                                    relief=tk.FLAT, padx=10, pady=3, cursor="hand2")
        self._btn_clear.pack(side=tk.LEFT)

        # Separator
        tk.Frame(self, bg=FG_ACCENT, height=1).pack(fill=tk.X)

    # ── Results area (pure Canvas, same technique as GalleryView) ─────────────
    def _build_results(self):
        frm = tk.Frame(self, bg=BG_GALLERY)
        frm.pack(fill=tk.BOTH, expand=True)
        vsb = ttk.Scrollbar(frm, orient=tk.VERTICAL)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._cv = tk.Canvas(frm, bg=BG_GALLERY, highlightthickness=0,
                             yscrollcommand=vsb.set, cursor="hand2")
        self._cv.pack(fill=tk.BOTH, expand=True)
        vsb.config(command=self._cv.yview)
        self._cv.bind("<MouseWheel>", self._wheel)
        self._cv.bind("<Button-4>",   self._wheel)
        self._cv.bind("<Button-5>",   self._wheel)
        self._cv.bind("<Button-1>",   self._on_click)

    def _build_statusbar(self):
        self._status = tk.StringVar(value="")
        tk.Label(self, textvariable=self._status, bg=BG_CARD, fg=FG_LABEL,
                 font=FONT_SMALL, anchor=tk.W, padx=8).pack(fill=tk.X)

    # ── Search ────────────────────────────────────────────────────────────────
    def _run_search(self):
        url = self._url_var.get().strip()
        if not url:
            messagebox.showwarning(_("search_title"),
                                   _("search_no_url"), parent=self)
            return

        try:
            threshold   = float(self._thr_var.get())
            max_results = int(self._max_var.get())
        except ValueError:
            messagebox.showerror(_("error_title"),
                                 _("search_param_error"), parent=self)
            return

        if not SEARCHER_AVAILABLE or self._searcher is None:
            messagebox.showerror(_("error_title"),
                                 _("search_unavailable") if not SEARCHER_AVAILABLE
                                 else _("search_index_not_ready"), parent=self)
            return

        self._btn_search.config(state=tk.DISABLED, text=_("search_running"))
        self._status.set(_("search_running"))
        self._results = []
        self._draw()

        def worker():
            try:
                results = self._searcher.search_url(
                    image_url=url,
                    threshold=threshold,
                    max_results=max_results,
                )
            except Exception as e:
                results = []
                if self.winfo_exists():
                    self.after(0, lambda err=e: messagebox.showerror(
                        _("error_title"), str(err), parent=self))
            if self.winfo_exists():
                self.after(0, lambda r=results: self._on_results(r))

        threading.Thread(target=worker, daemon=True).start()

    def _on_results(self, results: list[dict]):
        if not self.winfo_exists():
            return
        self._results = results
        n = len(results)
        self._status.set(_("search_done").format(n=n))
        self._btn_search.config(state=tk.NORMAL, text=_("search_btn"))
        self._tkimg.clear()
        self._draw()

    def _clear_results(self):
        self._results = []
        self._tkimg.clear()
        self._url_var.set("")
        self._status.set("")
        self._cv.delete("all")
        self._hits = []

    # ── Canvas drawing ────────────────────────────────────────────────────────
    def _on_cv_configure(self, event):
        if event.width != self._last_cv_w:
            self._last_cv_w = event.width
            self._schedule_draw()

    def _schedule_draw(self):
        if self._pending_draw:
            try:
                self.after_cancel(self._pending_draw)
            except Exception:
                pass
        self._pending_draw = self.after(60, self._draw)

    def _draw(self):
        self._pending_draw = None
        if not self.winfo_exists():
            return
        self.update_idletasks()
        cv_w = self._cv.winfo_width()
        if cv_w < 10:
            self._pending_draw = self.after(100, self._draw)
            return

        self._cv.delete("all")
        self._hits = []

        if not self._results:
            self._cv.create_text(cv_w // 2, 60,
                                 text=_("search_empty"),
                                 fill=FG_LABEL, font=FONT_TITLE)
            self._cv.configure(scrollregion=(0, 0, cv_w, 120))
            return

        M      = self.RES_PAD
        tile_w = self.RES_W + 2 * M
        cols   = max(1, cv_w // tile_w)
        tile_w = (cv_w - M * (cols + 1)) // cols
        tile_h = self.HDR_H + self.BADGE_H + self.RES_H + 2 * M

        for pos, item in enumerate(self._results):
            # ── Extract score and path from the dict ─────────────────────────
            pct  = float(item.get("score", 0))
            path = Path(item["path"]) if item.get("path") else None

            # Derive cid and side from the file name (<cid>_R.ext)
            cid, side = self._parse_path(path)

            row, col = divmod(pos, cols)
            x0 = M + col * (tile_w + M)
            y0 = M + row * (tile_h + M)
            x1 = x0 + tile_w
            y1 = y0 + tile_h

            # Border color according to the score
            border = self._pct_color(pct)
            self._cv.create_rectangle(x0, y0, x1, y1,
                                      fill=BG_CARD, outline=border, width=2)

            # Header: id + title
            self._cv.create_rectangle(x0, y0, x1, y0 + self.HDR_H,
                                      fill=BG_FIELD, outline="")
            title = self._card_title(cid) if cid is not None else ""
            id_str = f"#{cid}" if cid is not None else "?"
            hdr = id_str + (f"  {title[:26]}" if title else "")
            self._cv.create_text(x0 + 5, y0 + self.HDR_H // 2,
                                 text=hdr, anchor=tk.W,
                                 fill=FG_ACCENT2, font=("Courier", 8, "bold"))

            # Score badge
            by0 = y0 + self.HDR_H
            by1 = by0 + self.BADGE_H
            self._cv.create_rectangle(x0, by0, x1, by1,
                                      fill=self._pct_bg(pct), outline="")
            self._cv.create_text((x0 + x1) // 2, (by0 + by1) // 2,
                                 text=f"{pct:.1f} %",
                                 fill="#fff", font=("Courier", 9, "bold"))

            # Image from the provided path directly
            iy0 = by1 + M
            iy1 = iy0 + self.RES_H
            self._draw_img(path, x0 + M, iy0, x1 - M, iy1)

            # Clickable area — store the full path for the ImageViewer
            self._hits.append((x0, y0, x1, y1, cid, path))

        n_rows  = (len(self._results) + cols - 1) // cols
        total_h = n_rows * (tile_h + M) + M
        self._cv.configure(scrollregion=(0, 0, cv_w, total_h))

    def _draw_img(self, path: "Path | None", x0: int, y0: int, x1: int, y1: int):
        """Display the image from its full path (provided by search_url)."""
        w = x1 - x0
        h = y1 - y0
        if w <= 0 or h <= 0:
            return
        self._cv.create_rectangle(x0, y0, x1, y1, fill="#0a0a1a", outline="")
        if path is None or not path.exists():
            self._cv.create_text((x0 + x1) // 2, (y0 + y1) // 2,
                                 text="—", fill=FG_LABEL, font=("Courier", 9))
            return
        # Key = (absolute path, w, h) for the permanent PhotoImage cache
        key = (str(path), w, h)
        if key not in self._tkimg:
            pil = load_pil(path, w, h)
            if pil is None:
                self._cv.create_text((x0 + x1) // 2, (y0 + y1) // 2,
                                     text="—", fill=FG_LABEL, font=("Courier", 9))
                return
            pw, ph = pil.size
            scale   = min(w / pw, h / ph)
            nw, nh  = max(1, int(pw * scale)), max(1, int(ph * scale))
            resized = pil.resize((nw, nh), Image.LANCZOS)
            self._tkimg[key] = ImageTk.PhotoImage(resized)
        tkimg = self._tkimg[key]
        cx = x0 + (w - tkimg.width())  // 2
        cy = y0 + (h - tkimg.height()) // 2
        self._cv.create_image(cx, cy, image=tkimg, anchor=tk.NW)

    # ── Click → ImageViewer ───────────────────────────────────────────────────
    def _on_click(self, event):
        cy = self._cv.canvasy(event.y)
        cx = self._cv.canvasx(event.x)
        for (x0, y0, x1, y1, cid, path) in self._hits:
            if x0 <= cx <= x1 and y0 <= cy <= y1:
                self._open_viewer(cid, path)
                return

    def _open_viewer(self, cid: int | None, path: "Path | None"):
        if path is None:
            return
        id_str = f"#{cid}" if cid is not None else path.name
        ImageViewer(self, path, id_str, self._t)

    # ── Helpers ───────────────────────────────────────────────────────────────
    @staticmethod
    def _parse_path(path: "Path | None") -> tuple["int | None", str]:
        """Extract (cid, side) from a file name such as '<cid>_R.ext'.
        Returns (None, 'R') if the name doesn't match the expected scheme."""
        if path is None:
            return None, "R"
        stem = path.stem          # e.g. "42_R" or "42_V"
        parts = stem.rsplit("_", 1)
        if len(parts) == 2:
            try:
                cid  = int(parts[0])
                side = parts[1].upper()  # "R" or "V"
                return cid, side
            except ValueError:
                pass
        return None, "R"

    def _card_title(self, cid: int) -> str:
        data = self._app.model.load_json(cid)
        return data.get("title") or data.get("title2") or ""

    @staticmethod
    def _pct_color(pct: float) -> str:
        """Border color: green → yellow → red according to the score."""
        if pct >= 80:
            return "#2ecc71"
        if pct >= 60:
            return "#f5a623"
        return "#e94560"

    @staticmethod
    def _pct_bg(pct: float) -> str:
        """Background of the percentage badge."""
        if pct >= 80:
            return "#1a5a2a"
        if pct >= 60:
            return "#5a4010"
        return "#5a1020"

    def _wheel(self, e):
        if e.num == 4 or e.delta > 0:
            self._cv.yview_scroll(-3, "units")
        else:
            self._cv.yview_scroll(3,  "units")


# ─────────────────────────────────────────────────────────────────────────────
#  Missing doubles search (PostcardSearcher.find_missing_doubles)
# ─────────────────────────────────────────────────────────────────────────────
class DoublesSearchView(tk.Toplevel):
    """Display potential duplicate postcards found by
    PostcardSearcher.find_missing_doubles().

    Each result is shown as a tile with the two thumbnails (file1 / file2)
    side by side and the similarity score as a badge. Clicking a thumbnail
    opens it in an ImageViewer.
    """

    # Tile / thumbnail dimensions
    RES_W   = 160
    RES_H   = 115
    RES_PAD = 8
    HDR_H   = 18
    BADGE_H = 20
    EDIT_H  = 22

    def __init__(self, parent: "App", t):
        super().__init__(parent)
        self._app = parent
        self._t   = t
        self._tkimg: dict[tuple, "ImageTk.PhotoImage"] = {}
        self._hits: list[tuple] = []
        self._edit_hits: list[tuple] = []
        self._results: list[dict] = []
        self._searcher: "PostcardSearcher | None" = None
        self._last_cv_w = 0
        self._pending_draw: str | None = None

        self.title(_("doubles_title"))
        self.configure(bg=BG_MAIN)
        self.geometry("1100x750")
        self.minsize(700, 500)

        self._build_form()
        self._build_results()
        self._build_statusbar()
        self._cv.bind("<Configure>", self._on_cv_configure)

        # Load the index in the background to avoid blocking the UI
        self._load_index_async()

    # ── Index loading ─────────────────────────────────────────────────────────
    def _load_index_async(self):
        """Load postcards.pkl in a thread to avoid blocking the UI."""
        if not SEARCHER_AVAILABLE:
            self._status.set(_("search_unavailable"))
            self._btn_run.config(state=tk.DISABLED)
            return
        index_path = self._app.datadir / "postcards.pkl"
        self._status.set(_("search_index_loading"))
        self._btn_run.config(state=tk.DISABLED)

        def worker():
            try:
                searcher = PostcardSearcher()
                searcher.load_index(str(index_path))
                self.after(0, lambda s=searcher: self._on_index_loaded(s))
            except Exception as e:
                self.after(0, lambda err=e: self._on_index_error(err))

        threading.Thread(target=worker, daemon=True).start()

    def _on_index_loaded(self, searcher: "PostcardSearcher"):
        if not self.winfo_exists():
            return
        self._searcher = searcher
        self._status.set(_("search_index_ready"))
        self._btn_run.config(state=tk.NORMAL)

    def _on_index_error(self, err: Exception):
        if not self.winfo_exists():
            return
        self._status.set(_("search_index_error").format(err=err))
        # Button stays disabled if the index could not be loaded

    # ── Form ──────────────────────────────────────────────────────────────────
    def _build_form(self):
        form = tk.Frame(self, bg=BG_CARD, pady=10)
        form.pack(fill=tk.X, padx=0)

        row = tk.Frame(form, bg=BG_CARD)
        row.pack(fill=tk.X, padx=14, pady=(0, 4))

        tk.Label(row, text=_("doubles_threshold_label"), bg=BG_CARD, fg=FG_LABEL,
                 font=FONT_LABEL, anchor=tk.W).pack(side=tk.LEFT)
        self._thr_var = tk.StringVar(value="90")
        thr_entry = tk.Entry(row, textvariable=self._thr_var, width=6,
                             bg=BG_INPUT, fg=FG_TEXT, insertbackground=FG_TEXT,
                             font=FONT_INPUT, relief=tk.FLAT)
        thr_entry.pack(side=tk.LEFT, padx=(4, 20))
        thr_entry.bind("<Return>", lambda _: self._run_search())
        context_menu(thr_entry)

        self._btn_run = tk.Button(row, text=_("doubles_run"),
                                  command=self._run_search,
                                  bg=FG_ACCENT, fg="#fff", font=FONT_NAV,
                                  relief=tk.FLAT, padx=14, pady=3, cursor="hand2")
        self._btn_run.pack(side=tk.LEFT, padx=(0, 8))

        self._btn_clear = tk.Button(row, text=_("search_clear"),
                                    command=self._clear_results,
                                    bg=BG_FIELD, fg=FG_TEXT, font=FONT_LABEL,
                                    relief=tk.FLAT, padx=10, pady=3, cursor="hand2")
        self._btn_clear.pack(side=tk.LEFT)

        tk.Frame(self, bg=FG_ACCENT, height=1).pack(fill=tk.X)

    # ── Results area (pure Canvas, same technique as SearchView) ───────────────
    def _build_results(self):
        frm = tk.Frame(self, bg=BG_GALLERY)
        frm.pack(fill=tk.BOTH, expand=True)
        vsb = ttk.Scrollbar(frm, orient=tk.VERTICAL)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._cv = tk.Canvas(frm, bg=BG_GALLERY, highlightthickness=0,
                             yscrollcommand=vsb.set, cursor="hand2")
        self._cv.pack(fill=tk.BOTH, expand=True)
        vsb.config(command=self._cv.yview)
        self._cv.bind("<MouseWheel>", self._wheel)
        self._cv.bind("<Button-4>",   self._wheel)
        self._cv.bind("<Button-5>",   self._wheel)
        self._cv.bind("<Button-1>",   self._on_click)

    def _build_statusbar(self):
        self._status = tk.StringVar(value="")
        tk.Label(self, textvariable=self._status, bg=BG_CARD, fg=FG_LABEL,
                 font=FONT_SMALL, anchor=tk.W, padx=8).pack(fill=tk.X)

    # ── Search ────────────────────────────────────────────────────────────────
    def _run_search(self):
        try:
            threshold = float(self._thr_var.get())
        except ValueError:
            messagebox.showerror(_("error_title"),
                                 _("search_param_error"), parent=self)
            return

        if not SEARCHER_AVAILABLE or self._searcher is None:
            messagebox.showerror(_("error_title"),
                                 _("search_unavailable") if not SEARCHER_AVAILABLE
                                 else _("search_index_not_ready"), parent=self)
            return

        self._btn_run.config(state=tk.DISABLED, text=_("search_running"))
        self._status.set(_("search_running"))
        self._results = []
        self._draw()

        def worker():
            try:
                results = self._searcher.find_missing_doubles(
                    self._app.model, threshold=threshold)
            except Exception as e:
                results = []
                if self.winfo_exists():
                    self.after(0, lambda err=e: messagebox.showerror(
                        _("error_title"), str(err), parent=self))
            if self.winfo_exists():
                self.after(0, lambda r=results: self._on_results(r))

        threading.Thread(target=worker, daemon=True).start()

    def _on_results(self, results: list[dict]):
        if not self.winfo_exists():
            return
        self._results = results
        n = len(results)
        self._status.set(_("doubles_done").format(n=n))
        self._btn_run.config(state=tk.NORMAL, text=_("doubles_run"))
        self._tkimg.clear()
        self._draw()

    def _clear_results(self):
        self._results = []
        self._tkimg.clear()
        self._status.set("")
        self._cv.delete("all")
        self._hits = []

    # ── Canvas drawing ────────────────────────────────────────────────────────
    def _on_cv_configure(self, event):
        if event.width != self._last_cv_w:
            self._last_cv_w = event.width
            self._schedule_draw()

    def _schedule_draw(self):
        if self._pending_draw:
            try:
                self.after_cancel(self._pending_draw)
            except Exception:
                pass
        self._pending_draw = self.after(60, self._draw)

    def _draw(self):
        self._pending_draw = None
        if not self.winfo_exists():
            return
        self.update_idletasks()
        cv_w = self._cv.winfo_width()
        if cv_w < 10:
            self._pending_draw = self.after(100, self._draw)
            return

        self._cv.delete("all")
        self._hits = []
        self._edit_hits = []

        if not self._results:
            self._cv.create_text(cv_w // 2, 60,
                                 text=_("search_empty"),
                                 fill=FG_LABEL, font=FONT_TITLE)
            self._cv.configure(scrollregion=(0, 0, cv_w, 120))
            return

        M       = self.RES_PAD
        pair_w  = 2 * self.RES_W + 2  # two thumbnails + separator
        tile_w  = pair_w + 2 * M
        cols    = max(1, cv_w // tile_w)
        tile_w  = (cv_w - M * (cols + 1)) // cols
        half    = (tile_w - 2 * M - 2) // 2
        img_h   = int(half * self.RES_H / self.RES_W)
        tile_h  = self.HDR_H + self.BADGE_H + img_h + self.EDIT_H + 2 * M

        for pos, item in enumerate(self._results):
            pct   = float(item.get("score", 0))
            file1 = Path(item["file1"]) if item.get("file1") else None
            file2 = Path(item["file2"]) if item.get("file2") else None
            id1   = item.get("id1")
            id2   = item.get("id2")

            row, col = divmod(pos, cols)
            x0 = M + col * (tile_w + M)
            y0 = M + row * (tile_h + M)
            x1 = x0 + tile_w
            y1 = y0 + tile_h

            border = self._pct_color(pct)
            self._cv.create_rectangle(x0, y0, x1, y1,
                                      fill=BG_CARD, outline=border, width=2)

            # Header
            self._cv.create_rectangle(x0, y0, x1, y0 + self.HDR_H,
                                      fill=BG_FIELD, outline="")
            hdr = _("doubles_pair").format(id1=id1, id2=id2)
            self._cv.create_text(x0 + 5, y0 + self.HDR_H // 2,
                                 text=hdr, anchor=tk.W,
                                 fill=FG_ACCENT2, font=("Courier", 8, "bold"))

            # Score badge
            by0 = y0 + self.HDR_H
            by1 = by0 + self.BADGE_H
            self._cv.create_rectangle(x0, by0, x1, by1,
                                      fill=self._pct_bg(pct), outline="")
            self._cv.create_text((x0 + x1) // 2, (by0 + by1) // 2,
                                 text=f"{pct:.1f} %",
                                 fill="#fff", font=("Courier", 9, "bold"))

            # Two thumbnails side by side
            iy0 = by1 + M
            iy1 = iy0 + img_h
            lx0 = x0 + M
            self._draw_img(file1, lx0, iy0, lx0 + half, iy1)
            sep_x = lx0 + half + 1
            self._cv.create_line(sep_x, iy0, sep_x, iy1, fill=FG_ACCENT, width=2)
            rx0 = sep_x + 1
            self._draw_img(file2, rx0, iy0, x1 - M, iy1)

            self._hits.append((lx0, iy0, lx0 + half, iy1, id1, file1))
            self._hits.append((rx0, iy0, x1 - M, iy1, id2, file2))

            # "Edit" buttons below each thumbnail, opening the postcard
            # in the main window
            ey0 = iy1 + 2
            ey1 = ey0 + self.EDIT_H - 4
            self._draw_edit_button(lx0, ey0, lx0 + half, ey1, id1)
            self._draw_edit_button(rx0, ey0, x1 - M, ey1, id2)

        n_rows  = (len(self._results) + cols - 1) // cols
        total_h = n_rows * (tile_h + M) + M
        self._cv.configure(scrollregion=(0, 0, cv_w, total_h))

    def _draw_edit_button(self, x0: int, y0: int, x1: int, y1: int, cid):
        """Draw an "Edit" badge that opens the card in the main window."""
        self._cv.create_rectangle(x0, y0, x1, y1,
                                  fill=BG_FIELD, outline=FG_LINK)
        self._cv.create_text((x0 + x1) // 2, (y0 + y1) // 2,
                             text=_("doubles_edit"),
                             fill=FG_LINK, font=("Courier", 8, "bold"))
        self._edit_hits.append((x0, y0, x1, y1, cid))


    def _draw_img(self, path: "Path | None", x0: int, y0: int, x1: int, y1: int):
        """Display the image from its full path."""
        w = x1 - x0
        h = y1 - y0
        if w <= 0 or h <= 0:
            return
        self._cv.create_rectangle(x0, y0, x1, y1, fill="#0a0a1a", outline="")
        if path is None or not path.exists():
            self._cv.create_text((x0 + x1) // 2, (y0 + y1) // 2,
                                 text="—", fill=FG_LABEL, font=("Courier", 9))
            return
        key = (str(path), w, h)
        if key not in self._tkimg:
            pil = load_pil(path, w, h)
            if pil is None:
                self._cv.create_text((x0 + x1) // 2, (y0 + y1) // 2,
                                     text="—", fill=FG_LABEL, font=("Courier", 9))
                return
            pw, ph = pil.size
            scale   = min(w / pw, h / ph)
            nw, nh  = max(1, int(pw * scale)), max(1, int(ph * scale))
            resized = pil.resize((nw, nh), Image.LANCZOS)
            self._tkimg[key] = ImageTk.PhotoImage(resized)
        tkimg = self._tkimg[key]
        cx = x0 + (w - tkimg.width())  // 2
        cy = y0 + (h - tkimg.height()) // 2
        self._cv.create_image(cx, cy, image=tkimg, anchor=tk.NW)

    # ── Click → ImageViewer / Edit ──────────────────────────────────────────────
    def _on_click(self, event):
        cy = self._cv.canvasy(event.y)
        cx = self._cv.canvasx(event.x)

        for (x0, y0, x1, y1, cid) in self._edit_hits:
            if x0 <= cx <= x1 and y0 <= cy <= y1:
                self._open_in_main(cid)
                return

        for (x0, y0, x1, y1, cid, path) in self._hits:
            if x0 <= cx <= x1 and y0 <= cy <= y1:
                self._open_viewer(cid, path)
                return

    def _open_viewer(self, cid, path: "Path | None"):
        if path is None:
            return
        id_str = f"#{cid}" if cid is not None else path.name
        ImageViewer(self, path, id_str, self._t)

    # ── "Edit" button → open the postcard in the main window ───────────────────
    def _open_in_main(self, cid):
        try:
            cid = int(cid)
        except (TypeError, ValueError):
            return
        if cid not in self._app._ids:
            messagebox.showwarning(_("info_title"),
                                   _("goto_not_found").format(id=cid),
                                   parent=self)
            return
        if not self._app._ask_save_if_dirty():
            return
        self._app._load_card(self._app._ids.index(cid))
        self._app.lift()
        self._app.focus_force()

    # ── Helpers ───────────────────────────────────────────────────────────────
    @staticmethod
    def _pct_color(pct: float) -> str:
        if pct >= 90:
            return "#e94560"
        if pct >= 80:
            return "#f5a623"
        return "#2ecc71"

    @staticmethod
    def _pct_bg(pct: float) -> str:
        if pct >= 90:
            return "#5a1020"
        if pct >= 80:
            return "#5a4010"
        return "#1a5a2a"

    def _wheel(self, e):
        if e.num == 4 or e.delta > 0:
            self._cv.yview_scroll(-3, "units")
        else:
            self._cv.yview_scroll(3,  "units")


# ─────────────────────────────────────────────────────────────────────────────
#  POI (points of interest) manager
# ─────────────────────────────────────────────────────────────────────────────
class PoiManagerView(tk.Toplevel):
    """Manage the points of interest (POIs) stored in the database.

    Left: a list of all POIs (id + description preview).
    Right: a detail form (id, description, GPS coordinates) with
    New / Save / Delete actions, backed by Model.list_pois() /
    get_poi() / write_poi() / delete_poi().
    """

    def __init__(self, parent: "App", t):
        super().__init__(parent)
        self._app = parent
        self._t   = t
        self.title(_("poi_title"))
        self.configure(bg=BG_MAIN)
        self.geometry("760x520")
        self.minsize(560, 400)
        self.resizable(True, True)

        self._pois: list[dict] = []
        self._selected_id: str | None = None
        self._coord: list | None = None
        self._is_new = False

        self._build_ui()
        self._reload()

    # ── Construction ─────────────────────────────────────────────────────────
    def _build_ui(self):
        body = tk.Frame(self, bg=BG_MAIN)
        body.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # Left: list
        left = tk.Frame(body, bg=BG_CARD)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 8))

        tk.Label(left, text=_("poi_list_label"), bg=BG_CARD, fg=FG_ACCENT,
                 font=FONT_TITLE).pack(anchor=tk.W, padx=8, pady=(8, 4))

        lb_frm = tk.Frame(left, bg=BG_CARD)
        lb_frm.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        vsb = ttk.Scrollbar(lb_frm, orient=tk.VERTICAL)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._lb = tk.Listbox(lb_frm, bg=BG_INPUT, fg=FG_TEXT,
                              selectbackground=FG_ACCENT, font=FONT_INPUT,
                              relief=tk.FLAT, activestyle="none",
                              yscrollcommand=vsb.set)
        self._lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.config(command=self._lb.yview)
        self._lb.bind("<<ListboxSelect>>", self._on_select)

        tk.Button(left, text=_("poi_new"), command=self._new,
                  bg=BG_FIELD, fg=FG_ACCENT2, font=FONT_LABEL,
                  relief=tk.FLAT, padx=8, cursor="hand2").pack(
            fill=tk.X, padx=8, pady=(0, 8))

        # Right: detail form
        right = tk.Frame(body, bg=BG_CARD, width=320)
        right.pack(side=tk.LEFT, fill=tk.Y)
        right.pack_propagate(False)

        tk.Label(right, text=_("poi_detail_label"), bg=BG_CARD, fg=FG_ACCENT,
                 font=FONT_TITLE).pack(anchor=tk.W, padx=10, pady=(10, 6))

        # Id
        idf = tk.Frame(right, bg=BG_CARD)
        idf.pack(fill=tk.X, padx=10, pady=4)
        tk.Label(idf, text=_("poi_id"), bg=BG_CARD, fg=FG_LABEL,
                 font=FONT_LABEL, width=12, anchor=tk.W).pack(side=tk.LEFT)
        self._id_var = tk.StringVar()
        self._id_entry = tk.Entry(idf, textvariable=self._id_var,
                                  bg=BG_INPUT, fg=FG_TEXT, insertbackground=FG_TEXT,
                                  font=FONT_INPUT, relief=tk.FLAT)
        self._id_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        context_menu(self._id_entry)

        # Description
        descf = tk.Frame(right, bg=BG_CARD)
        descf.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)
        tk.Label(descf, text=_("poi_description"), bg=BG_CARD, fg=FG_LABEL,
                 font=FONT_LABEL, anchor=tk.W).pack(anchor=tk.W)
        self._desc_txt = tk.Text(descf, height=6, wrap=tk.WORD,
                                 bg=BG_INPUT, fg=FG_TEXT, insertbackground=FG_TEXT,
                                 font=FONT_INPUT, relief=tk.FLAT, padx=6, pady=4,
                                 undo=True)
        self._desc_txt.pack(fill=tk.BOTH, expand=True, pady=(2, 0))
        context_menu(self._desc_txt)

        # GPS
        gf = tk.Frame(right, bg=BG_CARD)
        gf.pack(fill=tk.X, padx=10, pady=8)
        tk.Label(gf, text=_("field_gps"), bg=BG_CARD, fg=FG_LABEL,
                 font=FONT_LABEL, anchor=tk.W).pack(anchor=tk.W)
        coord_row = tk.Frame(gf, bg=BG_CARD)
        coord_row.pack(fill=tk.X, pady=(2, 0))
        self._lbl_coord = tk.Label(coord_row, text="", bg=BG_INPUT, fg=FG_TEXT,
                                   font=FONT_INPUT, anchor=tk.W, relief=tk.FLAT, padx=6)
        self._lbl_coord.pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Button(coord_row, text=_("btn_edit"), command=self._edit_coord,
                  bg=BG_FIELD, fg=FG_ACCENT2, font=FONT_LABEL,
                  relief=tk.FLAT, padx=8, cursor="hand2").pack(side=tk.RIGHT, padx=(4, 0))

        # Actions
        actions = tk.Frame(right, bg=BG_CARD)
        actions.pack(fill=tk.X, padx=10, pady=10, side=tk.BOTTOM)
        tk.Button(actions, text=_("btn_save_close"), command=self._save,
                  bg=FG_ACCENT, fg="#fff", font=FONT_LABEL,
                  relief=tk.FLAT, padx=10, cursor="hand2").pack(
            side=tk.LEFT, padx=(0, 6))
        tk.Button(actions, text=_("poi_delete"), command=self._delete,
                  bg="#5a1a1a", fg=FG_TEXT, font=FONT_LABEL,
                  relief=tk.FLAT, padx=10, cursor="hand2").pack(side=tk.LEFT)

        self._status = tk.StringVar(value="")
        tk.Label(self, textvariable=self._status, bg=BG_CARD, fg=FG_LABEL,
                 font=FONT_SMALL, anchor=tk.W, padx=8).pack(fill=tk.X)

    # ── Data ──────────────────────────────────────────────────────────────────
    def _reload(self):
        try:
            self._pois = self._app.model.list_pois()
        except Exception as e:
            self._status.set(str(e))
            self._pois = []

        self._lb.delete(0, tk.END)
        for poi in self._pois:
            desc = (poi.get("description") or "").strip()
            label = poi["id"] if not desc else f"{poi['id']}  —  {desc[:40]}"
            self._lb.insert(tk.END, label)

        self._status.set(_("poi_count").format(n=len(self._pois)))

    def _on_select(self, _event=None):
        sel = self._lb.curselection()
        if not sel:
            return
        poi = self._pois[sel[0]]
        self._load_poi(poi)

    def _load_poi(self, poi: dict):
        self._is_new = False
        self._selected_id = poi["id"]
        self._id_var.set(poi["id"])
        self._id_entry.config(state=tk.DISABLED)
        self._desc_txt.delete("1.0", tk.END)
        self._desc_txt.insert("1.0", poi.get("description") or "")
        self._coord = poi.get("coord")
        self._refresh_coord()

    def _new(self):
        self._is_new = True
        self._selected_id = None
        self._lb.selection_clear(0, tk.END)
        self._id_var.set("")
        self._id_entry.config(state=tk.NORMAL)
        self._desc_txt.delete("1.0", tk.END)
        self._coord = None
        self._refresh_coord()
        self._id_entry.focus_set()

    def _refresh_coord(self):
        if self._coord and len(self._coord) >= 2:
            self._lbl_coord.config(
                text=f"lat {self._coord[0]:.6f}  /  lon {self._coord[1]:.6f}")
        else:
            self._lbl_coord.config(text="")

    def _edit_coord(self):
        def on_save(c):
            self._coord = c
            self._refresh_coord()
        CoordDialog(self, list(self._coord or []), on_save, self._t)

    # ── Save / Delete ─────────────────────────────────────────────────────────
    def _save(self):
        poi_id = self._id_var.get().strip()
        if not poi_id:
            messagebox.showwarning(_("info_title"), _("poi_id_required"), parent=self)
            return

        description = self._desc_txt.get("1.0", tk.END).rstrip("\n") or None

        try:
            self._app.model.write_poi({
                "id": poi_id,
                "description": description,
                "coord": self._coord,
            })
        except Exception as e:
            messagebox.showerror(_("error_title"), str(e), parent=self)
            return

        self._is_new = False
        self._selected_id = poi_id
        self._id_entry.config(state=tk.DISABLED)
        self._reload()
        self._select_in_list(poi_id)
        self._status.set(_("poi_saved").format(id=poi_id))

    def _delete(self):
        if not self._selected_id:
            return
        if not messagebox.askyesno(_("info_title"),
                                   _("poi_delete_confirm").format(id=self._selected_id),
                                   parent=self):
            return
        try:
            self._app.model.delete_poi(self._selected_id)
        except Exception as e:
            messagebox.showerror(_("error_title"), str(e), parent=self)
            return
        self._new()
        self._reload()

    def _select_in_list(self, poi_id: str):
        for i, poi in enumerate(self._pois):
            if poi["id"] == poi_id:
                self._lb.selection_clear(0, tk.END)
                self._lb.selection_set(i)
                self._lb.see(i)
                return


# ─────────────────────────────────────────────────────────────────────────────
#  Full-text search over the database
# ─────────────────────────────────────────────────────────────────────────────
class TextSearchView(tk.Toplevel):
    """Full-text search over all card fields stored in the database.

    - Free text: words are searched with AND across all text fields.
    - Optional collection filter (dropdown).
    - Results listed in a Listbox; double-click or Open button → editor.
    """

    def __init__(self, parent: "App", t):
        super().__init__(parent)
        self._app = parent
        self._t   = t
        self.title(_("tsearch_title"))
        self.configure(bg=BG_MAIN)
        self.geometry("800x560")
        self.minsize(600, 400)
        self.resizable(True, True)

        self._results: list[int] = []   # list of ids

        self._build_form()
        self._build_results()
        self._build_statusbar()
        self._run_search()

    # ── Construction ─────────────────────────────────────────────────────────
    def _build_form(self):
        form = tk.Frame(self, bg=BG_CARD, pady=8)
        form.pack(fill=tk.X)

        row = tk.Frame(form, bg=BG_CARD)
        row.pack(fill=tk.X, padx=14, pady=(0, 4))

        # Text entry
        tk.Label(row, text=_("tsearch_label"), bg=BG_CARD, fg=FG_LABEL,
                 font=FONT_LABEL).pack(side=tk.LEFT, padx=(0, 6))
        self._query_var = tk.StringVar()
        self._query_var.trace_add("write", lambda *_: self._run_search())
        e = tk.Entry(row, textvariable=self._query_var, width=36,
                     bg=BG_INPUT, fg=FG_TEXT, insertbackground=FG_TEXT,
                     font=FONT_INPUT, relief=tk.FLAT)
        e.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))
        e.bind("<Return>", lambda _: self._run_search())
        context_menu(e)
        e.focus_set()

        # Collection filter
        tk.Label(row, text=_("tsearch_coll_filter"), bg=BG_CARD, fg=FG_LABEL,
                 font=FONT_LABEL).pack(side=tk.LEFT, padx=(0, 4))
        self._coll_var = tk.StringVar(value=_("tsearch_all"))
        choices = [_("tsearch_all")] + self._app.collections
        self._coll_menu = ttk.Combobox(row, textvariable=self._coll_var,
                                       values=choices, width=20,
                                       font=FONT_INPUT, state="readonly")
        self._coll_menu.pack(side=tk.LEFT, padx=(0, 6))
        self._coll_menu.bind("<<ComboboxSelected>>", lambda _: self._run_search())

        # Clear button
        tk.Button(row, text=_("search_clear"), command=self._clear,
                  bg=BG_FIELD, fg=FG_TEXT, font=FONT_LABEL,
                  relief=tk.FLAT, padx=8).pack(side=tk.LEFT)

        tk.Frame(self, bg=FG_ACCENT, height=1).pack(fill=tk.X)

    def _build_results(self):
        frm = tk.Frame(self, bg=BG_MAIN)
        frm.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)

        # Listbox + scrollbar
        vsb = ttk.Scrollbar(frm, orient=tk.VERTICAL)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._lb = tk.Listbox(frm, bg=BG_INPUT, fg=FG_TEXT,
                              selectbackground=FG_ACCENT,
                              font=FONT_INPUT, relief=tk.FLAT,
                              activestyle="none",
                              yscrollcommand=vsb.set)
        self._lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.config(command=self._lb.yview)
        self._lb.bind("<Double-Button-1>", lambda _: self._open_selected())

        # Open button
        bot = tk.Frame(self, bg=BG_MAIN)
        bot.pack(fill=tk.X, padx=8, pady=(0, 8))
        tk.Button(bot, text=_("tsearch_open"), command=self._open_selected,
                  bg=FG_ACCENT, fg="#fff", font=FONT_LABEL,
                  relief=tk.FLAT, padx=12, cursor="hand2").pack(side=tk.LEFT)

    def _build_statusbar(self):
        self._status = tk.StringVar(value="")
        tk.Label(self, textvariable=self._status,
                 bg=BG_CARD, fg=FG_LABEL, font=FONT_SMALL,
                 anchor=tk.W, padx=8).pack(fill=tk.X)

    # ── Search ────────────────────────────────────────────────────────────────
    def _run_search(self):
        query = self._query_var.get().strip()
        coll_filter = self._coll_var.get()
        all_label   = _("tsearch_all")

        collection = None if (not coll_filter or coll_filter == all_label) else coll_filter
        search = query or None

        try:
            cards = self._app.model.list_cards(collection=collection, search=search)
        except Exception as e:
            self._status.set(f"Error: {e}")
            return

        self._results = []
        self._lb.delete(0, tk.END)

        for data in cards:
            try:
                cid = int(data.get("id"))
            except (TypeError, ValueError):
                continue
            self._results.append(cid)
            title = data.get("title") or data.get("title2") or ""
            colls = ", ".join(data.get("collections") or [])
            label = f"#{cid:>5}  {title[:40]}"
            if colls:
                label += f"  [{colls}]"
            self._lb.insert(tk.END, label)

        n = len(self._results)
        self._status.set(_("tsearch_results").format(n=n))

    def _clear(self):
        self._query_var.set("")
        self._coll_var.set(_("tsearch_all"))
        self._run_search()

    # ── Open ──────────────────────────────────────────────────────────────────
    def _open_selected(self):
        sel = self._lb.curselection()
        if not sel:
            return
        cid = self._results[sel[0]]
        if cid not in self._app._ids:
            messagebox.showwarning(_("info_title"),
                                   _("goto_not_found").format(id=cid),
                                   parent=self)
            return
        if not self._app._ask_save_if_dirty():
            return
        self._app._load_card(self._app._ids.index(cid))
        self._app.lift()
        self._app.focus_force()


# ─────────────────────────────────────────────────────────────────────────────
#  CLI
# ─────────────────────────────────────────────────────────────────────────────
@cli.command()
@click.pass_obj
def main(common):
    """Postcard collection viewer and editor."""
    datadir = common.datadir
    if datadir is None:
        cfg = configparser.ConfigParser()
        cfg.read(common.conffile, encoding="utf-8")
        datadir = cfg.get("DEFAULT", "datadir", fallback="data")

    dp = Path(datadir)
    if not dp.exists():
        click.echo(f"Directory not found: {dp}", err=True)
        sys.exit(1)

    if not PIL_AVAILABLE:
        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning("Missing Pillow",
                               "Pillow is not installed.\n\npip install Pillow")
        root.destroy()

    App(dp, conf_file=Path(common.conffile)).mainloop()


def run():
    """Standalone entry point (tkmanager script): runs `cli main`."""
    sys.argv.insert(1, "main")
    cli()
