"""PDF Voice Reader - reads PDF files aloud in an Indian-English female voice,
highlighting each word on the page as it is spoken.

Run:   python pdf_voice_reader.py [file.pdf]
Build: see build_exe.bat (creates a single portable PDFVoiceReader.exe)
"""
from __future__ import annotations

import bisect
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import tkinter as tk
import traceback
import wave
from tkinter import filedialog, messagebox, simpledialog, ttk

from PIL import Image, ImageDraw, ImageTk
import pymupdf

from reader_core import Document, align_word_times, estimate_word_times
from tts_engines import TTSError, all_voices

APP_NAME = "PDF Voice Reader"
APP_VERSION = "1.0.0"
DEFAULT_CHARS_PER_SEC = 14.5   # rough speaking rate at 1x, refined while reading
MIN_SPEED, MAX_SPEED = 0.5, 2.5

SENTENCE_FILL = (255, 228, 0, 75)
WORD_FILL = (255, 120, 0, 120)
WORD_LINE = (230, 81, 0, 255)


# ====================================================================== utils
def resource_path(name: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def settings_path() -> str:
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    folder = os.path.join(base, "PDFVoiceReader")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "settings.json")


def fmt_time(ms: float) -> str:
    s = int(max(ms, 0) // 1000)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class Settings(dict):
    def __init__(self):
        super().__init__(voice="edge:en-IN-NeerjaNeural", speed=1.0, volume=0.9,
                         follow=True, positions={}, recent=[])
        try:
            with open(settings_path(), "r", encoding="utf-8") as f:
                self.update(json.load(f))
        except Exception:
            pass

    def save(self):
        try:
            with open(settings_path(), "w", encoding="utf-8") as f:
                json.dump(self, f, indent=1)
        except Exception:
            pass


# ============================================================ speech worker
class Speaker:
    """Synthesises upcoming sentences in a background thread (pre-buffering)."""

    AHEAD = 4

    def __init__(self, tmpdir: str):
        self.tmpdir = tmpdir
        self.cv = threading.Condition()
        self.doc: Document | None = None
        self.voice = None
        self.speed = 1.0
        self.gen = 0
        self.cache: dict[int, object] = {}
        self.errors: dict[int, str] = {}
        self.busy: int | None = None
        self.want: list[int] = []
        self.alive = True
        threading.Thread(target=self._run, daemon=True).start()

    def configure(self, doc, voice, speed):
        with self.cv:
            self.doc, self.voice, self.speed = doc, voice, speed
            self.gen += 1
            for s in self.cache.values():
                self._delete(s)
            self.cache.clear()
            self.errors.clear()
            self.want = []
            self.cv.notify_all()

    def request(self, cur: int):
        with self.cv:
            n = len(self.doc.chunks) if self.doc else 0
            self.want = [i for i in range(cur, min(cur + self.AHEAD, n))]
            # forget audio we no longer need
            for i in [i for i in self.cache if i < cur - 1 or i > cur + self.AHEAD + 2]:
                self._delete(self.cache.pop(i))
            self.cv.notify_all()

    def get(self, idx: int):
        with self.cv:
            if idx in self.errors:
                raise TTSError(self.errors.pop(idx))
            return self.cache.get(idx)

    def stop(self):
        with self.cv:
            self.alive = False
            self.cv.notify_all()

    @staticmethod
    def _delete(s):
        if s is not None and s.path:
            try:
                os.remove(s.path)
            except OSError:
                pass

    def _next_job(self):
        for i in self.want:
            if i not in self.cache and i not in self.errors:
                return i
        return None

    def _run(self):
        while True:
            with self.cv:
                while self.alive and (self.doc is None or self._next_job() is None):
                    self.cv.wait()
                if not self.alive:
                    return
                idx = self._next_job()
                gen, voice, speed = self.gen, self.voice, self.speed
                text = self.doc.chunks[idx].text
                self.busy = idx
            path = os.path.join(self.tmpdir, f"g{gen}_c{idx}{voice.ext}")
            result, error = None, None
            try:
                result = voice.synthesize(text, speed, path)
            except TTSError as e:
                error = str(e)
            except Exception as e:  # never let the worker die
                error = f"Speech error: {e}"
            with self.cv:
                self.busy = None
                if gen != self.gen:
                    self._delete(result)
                elif error:
                    self.errors[idx] = error
                else:
                    self.cache[idx] = result
                self.cv.notify_all()


# ============================================================ timeline widget
class Timeline(tk.Canvas):
    """Clickable / draggable progress bar."""

    def __init__(self, master, on_seek, on_preview, **kw):
        super().__init__(master, height=26, highlightthickness=0, cursor="hand2", **kw)
        self.on_seek, self.on_preview = on_seek, on_preview
        self.fraction = 0.0
        self.dragging = False
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", self._release)

    def set_fraction(self, f: float):
        if not self.dragging:
            self.fraction = min(max(f, 0.0), 1.0)
            self.redraw()

    def _frac_from_x(self, x):
        w = max(self.winfo_width() - 16, 1)
        return min(max((x - 8) / w, 0.0), 1.0)

    def _press(self, e):
        self.dragging = True
        self._drag(e)

    def _drag(self, e):
        self.fraction = self._frac_from_x(e.x)
        self.redraw()
        self.on_preview(self.fraction)

    def _release(self, e):
        self.fraction = self._frac_from_x(e.x)
        self.dragging = False
        self.redraw()
        self.on_seek(self.fraction)

    def redraw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        y = h // 2
        x0, x1 = 8, max(w - 8, 9)
        xf = x0 + (x1 - x0) * self.fraction
        self.create_line(x0, y, x1, y, width=6, fill="#d0d4da", capstyle="round")
        self.create_line(x0, y, xf, y, width=6, fill="#c2185b", capstyle="round")
        r = 8 if self.dragging else 7
        self.create_oval(xf - r, y - r, xf + r, y + r, fill="#ffffff", outline="#c2185b", width=2)


# ==================================================================== the app
class App:
    def __init__(self, root: tk.Tk, initial_file: str | None = None):
        self.root = root
        self.settings = Settings()
        self.tmpdir = tempfile.mkdtemp(prefix="pdfvoicereader_")
        self.doc: Document | None = None
        self.voices = all_voices()
        self.voice = next((v for v in self.voices if v.key == self.settings["voice"]), self.voices[0])
        self.speed = float(self.settings.get("speed", 1.0))

        from audio_player import AudioPlayer
        try:
            self.player = AudioPlayer()
        except Exception as e:
            messagebox.showerror(APP_NAME, f"No audio output device could be opened.\n\n{e}")
            raise SystemExit(1)
        self.player.set_volume(float(self.settings.get("volume", 0.9)))
        self.speaker = Speaker(self.tmpdir)

        # playback state
        self.state = "stopped"          # stopped | playing | paused
        self.cur = 0                    # current chunk
        self.cur_times: list[float] = []
        self.cur_word = -1              # index inside the chunk
        self.dur_1x: dict[int, float] = {}
        self.cps = DEFAULT_CHARS_PER_SEC
        self._prefix: list[float] | None = None
        self._last_save = time.time()
        self._buffering_since = None

        # view state
        self.view_page = 0
        self.zoom = 1.0
        self.fit_width = True
        self._base_img = None
        self._base_key = None
        self._photo = None
        self._img_offset = (0, 0)
        self._drawn_key = None
        self._resize_job = None
        self._speed_job = None

        self._build_ui()
        self._bind_keys()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(40, self.tick)
        if initial_file:
            self.root.after(200, lambda: self.open_file(initial_file))
        else:
            self._show_welcome()

    # ------------------------------------------------------------- UI layout
    def _build_ui(self):
        r = self.root
        r.title(APP_NAME)
        r.geometry("1100x820")
        r.minsize(720, 520)
        try:
            r.iconbitmap(resource_path("app.ico"))
        except Exception:
            pass
        style = ttk.Style()
        style.configure("Big.TButton", font=("Segoe UI Symbol", 14), padding=(8, 2))
        style.configure("Play.TButton", font=("Segoe UI Symbol", 16, "bold"), padding=(14, 2))
        style.configure("Now.TLabel", font=("Segoe UI", 11), foreground="#333")

        self._build_menu()

        # --- top toolbar --------------------------------------------------
        top = ttk.Frame(r, padding=(6, 4))
        top.pack(side="top", fill="x")
        ttk.Button(top, text="\U0001F4C2 Open PDF", command=self.ask_open).pack(side="left")
        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=6)
        ttk.Button(top, text="◀", width=3, command=lambda: self.show_page(self.view_page - 1)).pack(side="left")
        self.page_var = tk.StringVar(value="0")
        pe = ttk.Entry(top, textvariable=self.page_var, width=5, justify="center")
        pe.pack(side="left", padx=2)
        pe.bind("<Return>", self._goto_page_entry)
        self.page_total = ttk.Label(top, text="/ 0")
        self.page_total.pack(side="left")
        ttk.Button(top, text="▶", width=3, command=lambda: self.show_page(self.view_page + 1)).pack(side="left")
        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=6)
        ttk.Button(top, text="−", width=3, command=lambda: self.set_zoom(self.zoom / 1.15)).pack(side="left")
        self.zoom_lbl = ttk.Label(top, text="100%", width=6, anchor="center")
        self.zoom_lbl.pack(side="left")
        ttk.Button(top, text="+", width=3, command=lambda: self.set_zoom(self.zoom * 1.15)).pack(side="left")
        ttk.Button(top, text="Fit width", command=self.set_fit_width).pack(side="left", padx=(4, 0))
        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=6)
        self.follow_var = tk.BooleanVar(value=bool(self.settings.get("follow", True)))
        ttk.Checkbutton(top, text="Follow reading", variable=self.follow_var,
                        command=self._follow_changed).pack(side="left")

        # voice selector (right side)
        vf = ttk.Frame(top)
        vf.pack(side="right", padx=(12, 0))
        ttk.Label(vf, text="Voice:").pack(side="left")
        self.voice_var = tk.StringVar(value=self.voice.label)
        vc = ttk.Combobox(vf, textvariable=self.voice_var, state="readonly", width=48,
                          values=[v.label for v in self.voices])
        vc.pack(side="left", padx=4)
        vc.bind("<<ComboboxSelected>>", self._voice_changed)

        # --- status bar (packed first so it sits at the very bottom) -------
        self.status = ttk.Label(r, text="", relief="sunken", anchor="w", padding=(6, 1))
        self.status.pack(side="bottom", fill="x")

        # --- bottom: transport + timeline ---------------------------------
        bottom = ttk.Frame(r, padding=(8, 4, 8, 6))
        bottom.pack(side="bottom", fill="x")

        self.now_lbl = ttk.Label(bottom, text="", style="Now.TLabel", wraplength=1000, justify="left")
        self.now_lbl.pack(side="top", fill="x", pady=(0, 4))

        tl_row = ttk.Frame(bottom)
        tl_row.pack(side="top", fill="x")
        self.time_lbl = ttk.Label(tl_row, text="0:00", width=8, anchor="e")
        self.time_lbl.pack(side="left")
        bg = ttk.Style().lookup("TFrame", "background") or "#f0f0f0"
        self.timeline = Timeline(tl_row, self._timeline_seek, self._timeline_preview, bg=bg)
        self.timeline.pack(side="left", fill="x", expand=True, padx=6)
        self.total_lbl = ttk.Label(tl_row, text="0:00", width=9, anchor="w")
        self.total_lbl.pack(side="left")

        ctl = ttk.Frame(bottom)
        ctl.pack(side="top", fill="x", pady=(4, 0))
        ttk.Button(ctl, text="⏮", style="Big.TButton", width=3, command=self.prev_sentence).pack(side="left")
        self.play_btn = ttk.Button(ctl, text="▶ Play", style="Play.TButton", width=8, command=self.toggle_play)
        self.play_btn.pack(side="left", padx=4)
        ttk.Button(ctl, text="⏹", style="Big.TButton", width=3, command=self.stop).pack(side="left")
        ttk.Button(ctl, text="⏭", style="Big.TButton", width=3, command=self.next_sentence).pack(side="left", padx=(4, 0))
        ttk.Button(ctl, text="Read this page", command=self.read_view_page).pack(side="left", padx=(12, 0))
        ttk.Button(ctl, text="Go to reading", command=self.goto_reading).pack(side="left", padx=4)

        # volume
        volf = ttk.Frame(ctl)
        volf.pack(side="right")
        ttk.Label(volf, text="\U0001F50A").pack(side="left")
        self.vol_var = tk.DoubleVar(value=self.player.volume * 100)
        ttk.Scale(volf, from_=0, to=100, variable=self.vol_var, length=90,
                  command=lambda v: self.player.set_volume(float(v) / 100)).pack(side="left", padx=4)

        # speed
        spf = ttk.Frame(ctl)
        spf.pack(side="right", padx=16)
        ttk.Label(spf, text="Speed").pack(side="left")
        ttk.Button(spf, text="−", width=3, command=lambda: self.change_speed(-0.1)).pack(side="left", padx=(4, 0))
        self.speed_var = tk.DoubleVar(value=self.speed)
        ttk.Scale(spf, from_=MIN_SPEED, to=MAX_SPEED, variable=self.speed_var, length=140,
                  command=self._speed_slider).pack(side="left", padx=2)
        ttk.Button(spf, text="+", width=3, command=lambda: self.change_speed(+0.1)).pack(side="left")
        self.speed_lbl = ttk.Label(spf, text=f"{self.speed:.2f}x", width=6, anchor="center")
        self.speed_lbl.pack(side="left", padx=4)

        # --- page canvas ----------------------------------------------------
        mid = ttk.Frame(r)
        mid.pack(side="top", fill="both", expand=True)
        self.canvas = tk.Canvas(mid, bg="#5f6368", highlightthickness=0)
        vs = ttk.Scrollbar(mid, orient="vertical", command=self.canvas.yview)
        hs = ttk.Scrollbar(mid, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        vs.pack(side="right", fill="y")
        hs.pack(side="bottom", fill="x")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.canvas.bind("<Double-Button-1>", self._on_canvas_double_click)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Button-4>", lambda e: self._on_wheel(e, 120))
        self.canvas.bind("<Button-5>", lambda e: self._on_wheel(e, -120))
        self.canvas.bind("<Control-MouseWheel>", self._on_ctrl_wheel)
        self.canvas.bind("<Enter>", lambda e: self.canvas.focus_set())

    def _build_menu(self):
        m = tk.Menu(self.root)
        fm = tk.Menu(m, tearoff=False)
        fm.add_command(label="Open PDF...", accelerator="Ctrl+O", command=self.ask_open)
        self.recent_menu = tk.Menu(fm, tearoff=False)
        fm.add_cascade(label="Recent files", menu=self.recent_menu)
        fm.add_separator()
        fm.add_command(label="Export audio (MP3 / WAV)...", command=self.export_audio)
        fm.add_separator()
        fm.add_command(label="Exit", command=self.on_close)
        m.add_cascade(label="File", menu=fm)

        pm = tk.Menu(m, tearoff=False)
        pm.add_command(label="Play / Pause", accelerator="Space", command=self.toggle_play)
        pm.add_command(label="Stop", accelerator="Esc", command=self.stop)
        pm.add_command(label="Previous sentence", accelerator="Left", command=self.prev_sentence)
        pm.add_command(label="Next sentence", accelerator="Right", command=self.next_sentence)
        pm.add_command(label="Read from current page", command=self.read_view_page)
        pm.add_command(label="Restart from beginning", accelerator="Home", command=self.restart)
        pm.add_command(label="Jump to time...", accelerator="Ctrl+G", command=self.ask_jump_time)
        pm.add_separator()
        pm.add_command(label="Faster", accelerator="]", command=lambda: self.change_speed(+0.1))
        pm.add_command(label="Slower", accelerator="[", command=lambda: self.change_speed(-0.1))
        pm.add_command(label="Normal speed", accelerator="=", command=lambda: self.set_speed(1.0))
        m.add_cascade(label="Playback", menu=pm)

        vm = tk.Menu(m, tearoff=False)
        vm.add_command(label="Zoom in", accelerator="Ctrl++", command=lambda: self.set_zoom(self.zoom * 1.15))
        vm.add_command(label="Zoom out", accelerator="Ctrl+-", command=lambda: self.set_zoom(self.zoom / 1.15))
        vm.add_command(label="Fit width", accelerator="Ctrl+0", command=self.set_fit_width)
        vm.add_separator()
        vm.add_command(label="Next page", accelerator="PgDn", command=lambda: self.show_page(self.view_page + 1))
        vm.add_command(label="Previous page", accelerator="PgUp", command=lambda: self.show_page(self.view_page - 1))
        vm.add_command(label="Go to reading position", command=self.goto_reading)
        m.add_cascade(label="View", menu=vm)

        hm = tk.Menu(m, tearoff=False)
        hm.add_command(label="How to use / shortcuts", command=self.show_help)
        hm.add_command(label="About", command=lambda: messagebox.showinfo(
            APP_NAME, f"{APP_NAME} {APP_VERSION}\n\nReads PDFs aloud in an Indian-English voice "
                      "with live word highlighting.\n\nOnline voices: Microsoft Neural TTS (Neerja).\n"
                      "Offline voices: those installed in Windows."))
        m.add_cascade(label="Help", menu=hm)
        self.root.config(menu=m)
        self._refresh_recent()

    def _bind_keys(self):
        r = self.root

        def k(seq, fn):
            def handler(e):
                if isinstance(e.widget, (tk.Entry, ttk.Entry, ttk.Combobox)) and seq in (
                        "<space>", "<Left>", "<Right>", "<Home>", "<bracketleft>", "<bracketright>", "<equal>"):
                    return None
                fn()
                return "break"
            r.bind_all(seq, handler)

        k("<space>", self.toggle_play)
        k("<Escape>", self.stop)
        k("<Left>", self.prev_sentence)
        k("<Right>", self.next_sentence)
        k("<Home>", self.restart)
        k("<Prior>", lambda: self.show_page(self.view_page - 1))
        k("<Next>", lambda: self.show_page(self.view_page + 1))
        k("<Control-o>", self.ask_open)
        k("<Control-g>", self.ask_jump_time)
        k("<Control-plus>", lambda: self.set_zoom(self.zoom * 1.15))
        k("<Control-equal>", lambda: self.set_zoom(self.zoom * 1.15))
        k("<Control-minus>", lambda: self.set_zoom(self.zoom / 1.15))
        k("<Control-0>", self.set_fit_width)
        k("<bracketright>", lambda: self.change_speed(+0.1))
        k("<bracketleft>", lambda: self.change_speed(-0.1))
        k("<equal>", lambda: self.set_speed(1.0))
        k("<Up>", lambda: self.canvas.yview_scroll(-3, "units"))
        k("<Down>", lambda: self.canvas.yview_scroll(3, "units"))
        # Space must not also "click" a focused button (that would toggle twice).
        for cls in ("TButton", "TCheckbutton", "TScale"):
            r.unbind_class(cls, "<Key-space>")
        for cls in ("TScale",):
            r.unbind_class(cls, "<Left>")
            r.unbind_class(cls, "<Right>")

    def _show_welcome(self):
        self.canvas.delete("all")
        self.canvas.create_text(
            20, 20, anchor="nw", fill="white", font=("Segoe UI", 14),
            text=("Welcome to PDF Voice Reader\n\n"
                  "• Click  \U0001F4C2 Open PDF  (or press Ctrl+O)\n"
                  "• Press Play (or Space) to listen\n"
                  "• Double-click any text to start reading from there\n"
                  "• Click or drag the timeline to jump anywhere\n"
                  "• Use [ and ] to change speed\n\n"
                  "Default voice: Neerja - Indian English female (needs internet).\n"
                  "Offline Windows voices are also listed in the Voice menu."))
        self.set_status("Open a PDF to begin.")

    def show_help(self):
        messagebox.showinfo(APP_NAME + " - Help", (
            "PLAYBACK\n"
            "  Space ............ Play / Pause\n"
            "  Esc .............. Stop (back to start of sentence)\n"
            "  Left / Right ..... Previous / next sentence\n"
            "  Home ............. Restart from the beginning\n"
            "  Ctrl+G ........... Jump to a time\n"
            "  [  /  ]  /  = .... Slower / faster / normal speed\n\n"
            "NAVIGATION\n"
            "  Double-click text  Read from that sentence\n"
            "  Timeline ......... Click or drag to jump anywhere\n"
            "  PgUp / PgDn ...... Previous / next page\n"
            "  Ctrl + / - / 0 ... Zoom in / out / fit width\n"
            "  Ctrl + mouse wheel Zoom\n\n"
            "VOICES\n"
            "  'Neerja' is a natural Indian-English female voice (internet needed).\n"
            "  Offline voices are the ones installed in Windows. To get the Indian\n"
            "  female offline voice 'Heera': Settings > Time & Language > Speech >\n"
            "  Add voices > English (India).\n\n"
            "Your last reading position is remembered for every PDF.\n"
            "File > Export audio saves the reading as an MP3/WAV audiobook."))

    def set_status(self, text):
        self.status.config(text=text)

    # ------------------------------------------------------------ open file
    def ask_open(self):
        path = filedialog.askopenfilename(title="Open PDF", filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")])
        if path:
            self.open_file(path)

    def open_file(self, path):
        path = os.path.abspath(path)
        try:
            doc = Document(path)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Could not open the PDF:\n{path}\n\n{e}")
            return
        self._save_position()
        self.player.unload()
        if self.doc:
            self.doc.close()
        self.doc = doc
        self.state = "stopped"
        self.dur_1x.clear()
        self._prefix = None
        self.cur_times, self.cur_word = [], -1
        self._base_key = self._drawn_key = None
        self.speaker.configure(doc, self.voice, self.speed)
        self.root.title(f"{os.path.basename(path)} - {APP_NAME}")
        self.page_total.config(text=f"/ {doc.page_count}")

        recent = [p for p in self.settings.get("recent", []) if p != path]
        self.settings["recent"] = [path] + recent[:9]
        self._refresh_recent()

        self.cur = 0
        saved = self.settings.get("positions", {}).get(path)
        if not doc.chunks:
            self.show_page(0)
            messagebox.showwarning(APP_NAME, (
                "No readable text was found in this PDF.\n\n"
                "It is probably a scanned document (pictures of pages). "
                "Text must be selectable for it to be read aloud. "
                "Use an OCR tool to convert it to a searchable PDF first."))
            self.set_status("No text found in this PDF.")
            self._update_play_button()
            return
        if isinstance(saved, int) and 0 < saved < len(doc.chunks):
            page = doc.chunks[saved].page + 1
            if messagebox.askyesno(APP_NAME, f"Continue from where you stopped last time (page {page})?"):
                self.cur = saved
        self.show_page(doc.chunks[self.cur].page)
        self._update_now_label()
        self._update_play_button()
        self.set_status(f"Loaded {len(doc.chunks)} sentences on {doc.page_count} pages. Press Space or Play to start.")

    def _refresh_recent(self):
        self.recent_menu.delete(0, "end")
        rec = self.settings.get("recent", [])
        if not rec:
            self.recent_menu.add_command(label="(none)", state="disabled")
        for p in rec:
            self.recent_menu.add_command(label=p, command=lambda p=p: self.open_file(p))

    # ------------------------------------------------------------ rendering
    def _on_canvas_resize(self, _e=None):
        if self._resize_job:
            self.root.after_cancel(self._resize_job)
        self._resize_job = self.root.after(120, self._apply_resize)

    def _apply_resize(self):
        self._resize_job = None
        self.now_lbl.config(wraplength=max(self.root.winfo_width() - 40, 300))
        if self.doc:
            self.render(force=True)

    def _fit_zoom(self):
        page = self.doc.pdf[self.view_page]
        w = max(self.canvas.winfo_width() - 24, 200)
        return w / page.rect.width

    def set_zoom(self, z):
        if not self.doc:
            return
        self.fit_width = False
        self.zoom = min(max(z, 0.3), 6.0)
        self.render(force=True)

    def set_fit_width(self):
        self.fit_width = True
        if self.doc:
            self.render(force=True)

    def show_page(self, pno, scroll_top=True):
        if not self.doc:
            return
        pno = min(max(pno, 0), self.doc.page_count - 1)
        changed = pno != self.view_page
        self.view_page = pno
        self.page_var.set(str(pno + 1))
        self.render(force=True)
        if changed and scroll_top:
            self.canvas.yview_moveto(0)

    def _goto_page_entry(self, _e=None):
        try:
            self.show_page(int(self.page_var.get()) - 1)
        except ValueError:
            self.page_var.set(str(self.view_page + 1))
        self.canvas.focus_set()

    def _base_image(self):
        if self.fit_width:
            self.zoom = self._fit_zoom()
        key = (self.view_page, round(self.zoom, 4))
        if self._base_key != key:
            page = self.doc.pdf[self.view_page]
            pix = page.get_pixmap(matrix=pymupdf.Matrix(self.zoom, self.zoom), alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            self._base_img = img.convert("RGBA")
            self._base_key = key
            self.zoom_lbl.config(text=f"{int(round(self.zoom * 100 / (96 / 72)))}%")
        return self._base_img

    def _highlight_rects(self):
        """(sentence_line_rects, word_rect) on the page being viewed."""
        if not self.doc or not self.doc.chunks:
            return [], None
        chunk = self.doc.chunks[self.cur]
        lines: dict[tuple, list] = {}
        word_rect = None
        for n, wid in enumerate(chunk.word_ids):
            w = self.doc.words[wid]
            if w.page != self.view_page:
                continue
            key = (w.block, w.line)
            r = lines.get(key)
            lines[key] = list(w.rect) if r is None else [min(r[0], w.rect[0]), min(r[1], w.rect[1]),
                                                         max(r[2], w.rect[2]), max(r[3], w.rect[3])]
            if n == self.cur_word:
                word_rect = w.rect
        return list(lines.values()), word_rect

    def render(self, force=False):
        if not self.doc:
            return
        sent, word = self._highlight_rects()
        key = (self.view_page, round(self.zoom, 4), self.cur, self.cur_word, self.fit_width,
               self.canvas.winfo_width())
        if not force and key == self._drawn_key:
            return
        self._drawn_key = key
        base = self._base_image()
        z = self.zoom
        if sent or word:
            overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
            d = ImageDraw.Draw(overlay)
            for x0, y0, x1, y1 in sent:
                d.rectangle([x0 * z - 2, y0 * z - 1, x1 * z + 2, y1 * z + 1], fill=SENTENCE_FILL)
            if word:
                x0, y0, x1, y1 = word
                d.rounded_rectangle([x0 * z - 2, y0 * z - 2, x1 * z + 2, y1 * z + 2], radius=3,
                                    fill=WORD_FILL)
                d.line([x0 * z, y1 * z + 2, x1 * z, y1 * z + 2], fill=WORD_LINE, width=max(2, int(z)))
            img = Image.alpha_composite(base, overlay)
        else:
            img = base
        self._photo = ImageTk.PhotoImage(img)
        cw = self.canvas.winfo_width()
        xoff = max((cw - img.width) // 2, 0)
        yoff = 8
        self._img_offset = (xoff, yoff)
        self.canvas.delete("all")
        self.canvas.create_image(xoff, yoff, anchor="nw", image=self._photo)
        self.canvas.configure(scrollregion=(0, 0, max(cw, img.width + 2 * xoff), img.height + 2 * yoff))
        self.page_var.set(str(self.view_page + 1))

        # keep the spoken word in view
        target = word or (sent[0] if sent else None)
        if target and self.follow_var.get() and self.state == "playing":
            total_h = img.height + 2 * yoff
            top, bottom = self.canvas.yview()
            y = (target[1] * z + yoff) / total_h
            y2 = (target[3] * z + yoff) / total_h
            if y < top + 0.02 or y2 > bottom - 0.08:
                view_h = bottom - top
                self.canvas.yview_moveto(max(0.0, y - view_h * 0.3))

    # ---------------------------------------------------------- mouse input
    def _on_wheel(self, e, delta=None):
        delta = delta if delta is not None else e.delta
        top, bottom = self.canvas.yview()
        if delta < 0 and bottom >= 0.999 and self.doc and self.view_page < self.doc.page_count - 1:
            self.show_page(self.view_page + 1)
            return
        if delta > 0 and top <= 0.001 and self.doc and self.view_page > 0:
            self.show_page(self.view_page - 1, scroll_top=False)
            self.canvas.yview_moveto(1.0)
            return
        self.canvas.yview_scroll(-3 if delta > 0 else 3, "units")

    def _on_ctrl_wheel(self, e):
        self.set_zoom(self.zoom * (1.1 if e.delta > 0 else 1 / 1.1))

    def _on_canvas_double_click(self, e):
        if not self.doc or not self.doc.chunks:
            return
        cx, cy = self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)
        xoff, yoff = self._img_offset
        x, y = (cx - xoff) / self.zoom, (cy - yoff) / self.zoom
        wid = self.doc.word_at(self.view_page, x, y)
        if wid is None:
            return
        idx = self.doc.chunk_of_word(wid)
        if idx is not None:
            self.seek_chunk(idx, play=True)

    # ------------------------------------------------------------- playback
    def toggle_play(self):
        if not self.doc or not self.doc.chunks:
            if not self.doc:
                self.ask_open()
            return
        if self.state == "playing":
            self.player.pause()
            self.state = "paused"
            self.set_status("Paused.")
        else:
            if self.state == "paused" and self.player.loaded:
                self.player.resume()
            self.state = "playing"
            self.set_status("Reading...")
            if self.follow_var.get():
                self.goto_reading()
        self._update_play_button()

    def stop(self):
        self.player.unload()
        self.state = "stopped"
        self.cur_word = -1
        self._update_play_button()
        self.render()
        self.set_status("Stopped. Press Play to continue from the highlighted sentence.")

    def restart(self):
        self.seek_chunk(0, play=self.state == "playing")

    def prev_sentence(self):
        if self.doc and self.doc.chunks:
            # within the first 1.5 s go to the previous sentence, otherwise restart this one
            back = 1 if (not self.player.loaded or self.player.position_ms() < 1500) else 0
            self.seek_chunk(max(self.cur - back, 0), play=self.state == "playing")

    def next_sentence(self):
        if self.doc and self.doc.chunks:
            self.seek_chunk(min(self.cur + 1, len(self.doc.chunks) - 1), play=self.state == "playing")

    def read_view_page(self):
        if not self.doc:
            return
        idx = self.doc.first_chunk_at_or_after_page(self.view_page)
        if idx is not None:
            self.seek_chunk(idx, play=True)

    def goto_reading(self):
        if self.doc and self.doc.chunks:
            p = self.doc.chunks[self.cur].page
            if p != self.view_page:
                self.show_page(p)

    def seek_chunk(self, idx, play=None):
        if not self.doc or not self.doc.chunks:
            return
        self.player.unload()
        self.cur = min(max(idx, 0), len(self.doc.chunks) - 1)
        self.cur_times, self.cur_word = [], -1
        self.speaker.request(self.cur)
        if play is True:
            self.state = "playing"
        elif play is False:
            self.state = "stopped"
        elif self.state == "paused":
            self.state = "stopped"
        self._update_play_button()
        self._update_now_label()
        page = self.doc.chunks[self.cur].page
        if page != self.view_page:
            self.show_page(page)
        else:
            self.render(force=True)
        self._scroll_to_chunk()

    def _scroll_to_chunk(self):
        sent, _ = self._highlight_rects()
        if not sent or not self._base_img:
            return
        total_h = self._base_img.height + 16
        top, bottom = self.canvas.yview()
        y = (sent[0][1] * self.zoom + 8) / total_h
        if y < top or y > bottom - 0.1:
            self.canvas.yview_moveto(max(0.0, y - (bottom - top) * 0.3))

    def ask_jump_time(self):
        if not self.doc or not self.doc.chunks:
            return
        s = simpledialog.askstring(APP_NAME, "Jump to time (e.g. 12:30 or 1:05:00):", parent=self.root)
        if not s:
            return
        try:
            parts = [float(p) for p in s.strip().split(":")]
            secs = 0.0
            for p in parts:
                secs = secs * 60 + p
        except ValueError:
            messagebox.showerror(APP_NAME, "Please type a time like 12:30")
            return
        self.seek_chunk(self._chunk_at_ms(secs * 1000), play=self.state == "playing")

    def _update_play_button(self):
        self.play_btn.config(text="⏸ Pause" if self.state == "playing" else "▶ Play")

    def _update_now_label(self):
        if self.doc and self.doc.chunks:
            c = self.doc.chunks[self.cur]
            t = c.text if len(c.text) < 400 else c.text[:400] + "..."
            self.now_lbl.config(text=f"Page {c.page + 1}  •  {t}")
        else:
            self.now_lbl.config(text="")

    # --------------------------------------------------- speed and voice
    def _speed_slider(self, v):
        self._set_speed_value(round(float(v) * 20) / 20, from_slider=True)

    def change_speed(self, delta):
        self.set_speed(round((self.speed + delta) * 10) / 10)

    def set_speed(self, s):
        self._set_speed_value(s)

    def _set_speed_value(self, s, from_slider=False):
        s = min(max(s, MIN_SPEED), MAX_SPEED)
        if abs(s - self.speed) < 1e-6:
            return
        self.speed = s
        if not from_slider:
            self.speed_var.set(s)
        self.speed_lbl.config(text=f"{s:.2f}x")
        self.settings["speed"] = s
        self._prefix = None
        if self._speed_job:
            self.root.after_cancel(self._speed_job)
        self._speed_job = self.root.after(350, self._apply_speed)

    def _apply_speed(self):
        self._speed_job = None
        self._reconfigure_speech()
        self.set_status(f"Speed set to {self.speed:.2f}x")

    def _voice_changed(self, _e=None):
        label = self.voice_var.get()
        v = next((v for v in self.voices if v.label == label), None)
        if v and v is not self.voice:
            self.voice = v
            self.settings["voice"] = v.key
            self.dur_1x.clear()
            self._prefix = None
            self._reconfigure_speech()
            self.set_status(f"Voice: {v.label}")
        self.canvas.focus_set()

    def _reconfigure_speech(self):
        """Voice or speed changed: re-synthesise from the current sentence."""
        was_loaded = self.player.loaded
        self.player.unload()
        self.cur_times, self.cur_word = [], -1
        if self.state == "paused" and was_loaded:
            self.state = "stopped"
            self._update_play_button()
        if self.doc:
            self.speaker.configure(self.doc, self.voice, self.speed)
            self.speaker.request(self.cur)

    def _follow_changed(self):
        self.settings["follow"] = bool(self.follow_var.get())
        if self.follow_var.get():
            self.goto_reading()

    # ------------------------------------------------------------ timeline
    def _chunk_ms(self, i):
        d = self.dur_1x.get(i)
        if d is None:
            d = len(self.doc.chunks[i].text) / self.cps * 1000.0 + 250
        return d / self.speed

    def _prefix_sums(self):
        if self._prefix is None:
            acc, out = 0.0, [0.0]
            for i in range(len(self.doc.chunks)):
                acc += self._chunk_ms(i)
                out.append(acc)
            self._prefix = out
        return self._prefix

    def _chunk_at_ms(self, ms):
        p = self._prefix_sums()
        i = bisect.bisect_right(p, ms) - 1
        return min(max(i, 0), len(self.doc.chunks) - 1)

    def _timeline_preview(self, frac):
        if not self.doc or not self.doc.chunks:
            return
        p = self._prefix_sums()
        ms = frac * p[-1]
        i = self._chunk_at_ms(ms)
        self.time_lbl.config(text=fmt_time(ms))
        self.set_status(f"Release to jump to {fmt_time(ms)}  (page {self.doc.chunks[i].page + 1})")

    def _timeline_seek(self, frac):
        if not self.doc or not self.doc.chunks:
            return
        p = self._prefix_sums()
        self.seek_chunk(self._chunk_at_ms(frac * p[-1]), play=self.state == "playing")
        self.set_status("Reading..." if self.state == "playing" else "Press Play to start from here.")

    def _update_timeline(self):
        if not self.doc or not self.doc.chunks:
            self.timeline.set_fraction(0)
            return
        p = self._prefix_sums()
        pos = p[self.cur]
        if self.player.loaded:
            pos += min(self.player.position_ms(), self._chunk_ms(self.cur))
        total = p[-1]
        if not self.timeline.dragging:
            self.time_lbl.config(text=fmt_time(pos))
        estimated = len(self.dur_1x) < len(self.doc.chunks)
        self.total_lbl.config(text=("~" if estimated else "") + fmt_time(total))
        self.timeline.set_fraction(pos / total if total else 0)

    # ------------------------------------------------------------ main loop
    def tick(self):
        try:
            self._tick()
        except Exception:
            traceback.print_exc()
        self.root.after(40, self.tick)

    def _tick(self):
        if self.doc and self.doc.chunks:
            self.speaker.request(self.cur)
            if self.state == "playing":
                if not self.player.loaded:
                    self._start_current_chunk()
                else:
                    pos = self.player.position_ms()
                    if self.cur_times:
                        w = bisect.bisect_right(self.cur_times, pos) - 1
                        w = max(w, 0)
                        if w != self.cur_word:
                            self.cur_word = w
                            self._show_reading_page()
                            self.render()
                    if self.player.finished():
                        self.player.unload()
                        self._advance()
            if time.time() - self._last_save > 20:
                self._save_position()
        self._update_timeline()

    def _start_current_chunk(self):
        try:
            synth = self.speaker.get(self.cur)
        except TTSError as e:
            self.state = "paused"
            self._update_play_button()
            self.set_status("Speech error - see message.")
            messagebox.showerror(APP_NAME, str(e))
            return
        if synth is None:
            if self._buffering_since is None:
                self._buffering_since = time.time()
            elif time.time() - self._buffering_since > 0.4:
                self.set_status("Preparing voice...")
            return
        if self._buffering_since is not None:
            self._buffering_since = None
            self.set_status("Reading...")
        chunk = self.doc.chunks[self.cur]
        if synth.path is None or synth.duration_ms <= 0:
            self._advance()
            return
        # learn real durations so the timeline becomes accurate
        if self.cur not in self.dur_1x:
            self.dur_1x[self.cur] = synth.duration_ms * self.speed
            self._prefix = None
            if len(chunk.text) > 20:
                cps = len(chunk.text) / (synth.duration_ms * self.speed / 1000.0)
                self.cps = self.cps * 0.85 + cps * 0.15
        words = [self.doc.words[i].text for i in chunk.word_ids]
        if synth.boundaries:
            self.cur_times = align_word_times(words, synth.boundaries, synth.duration_ms)
        else:
            self.cur_times = estimate_word_times(words, synth.duration_ms)
        self.cur_word = -1
        try:
            self.player.play(synth.path, synth.duration_ms)
        except Exception as e:
            self.state = "paused"
            self._update_play_button()
            messagebox.showerror(APP_NAME, f"Could not play audio: {e}")
            return
        self._update_now_label()
        self._show_reading_page()
        self.render()

    def _show_reading_page(self):
        if self.follow_var.get():
            p = self.doc.chunks[self.cur].page
            if p != self.view_page:
                self.show_page(p)

    def _advance(self):
        if self.cur + 1 >= len(self.doc.chunks):
            self.state = "stopped"
            self.cur_word = -1
            self.cur = 0
            self._update_play_button()
            self._update_now_label()
            self.render()
            self.set_status("Finished reading the document.")
            return
        self.cur += 1
        self.cur_times, self.cur_word = [], -1
        self._update_now_label()
        self.speaker.request(self.cur)

    # ---------------------------------------------------------------- export
    def export_audio(self):
        if not self.doc or not self.doc.chunks:
            messagebox.showinfo(APP_NAME, "Open a PDF with text first.")
            return
        n = self.doc.page_count
        first = simpledialog.askinteger(APP_NAME, f"Export from page (1-{n}):", initialvalue=1,
                                        minvalue=1, maxvalue=n, parent=self.root)
        if first is None:
            return
        last = simpledialog.askinteger(APP_NAME, f"Export to page ({first}-{n}):", initialvalue=n,
                                       minvalue=first, maxvalue=n, parent=self.root)
        if last is None:
            return
        voice, speed = self.voice, self.speed
        ext = voice.ext
        default = os.path.splitext(os.path.basename(self.doc.path))[0] + ext
        out = filedialog.asksaveasfilename(title="Save audio as", defaultextension=ext, initialfile=default,
                                           filetypes=[("Audio", "*" + ext)])
        if not out:
            return
        chunks = [c for c in self.doc.chunks if first - 1 <= c.page <= last - 1]
        if not chunks:
            messagebox.showinfo(APP_NAME, "There is no text on those pages.")
            return

        win = tk.Toplevel(self.root)
        win.title("Exporting audio")
        win.transient(self.root)
        win.resizable(False, False)
        ttk.Label(win, text=f"Creating {os.path.basename(out)} ...", padding=10).pack()
        bar = ttk.Progressbar(win, length=360, maximum=len(chunks))
        bar.pack(padx=14)
        lbl = ttk.Label(win, text="", padding=6)
        lbl.pack()
        state = {"done": 0, "cancel": False, "error": None, "finished": False}
        ttk.Button(win, text="Cancel", command=lambda: state.update(cancel=True)).pack(pady=(0, 10))

        exp_dir = tempfile.mkdtemp(dir=self.tmpdir)

        def work():
            wav_out = None
            try:
                with open(out, "wb") as mp3_out:
                    for i, c in enumerate(chunks):
                        if state["cancel"]:
                            break
                        path = os.path.join(exp_dir, f"e{i}{ext}")
                        s = voice.synthesize(c.text, speed, path)
                        if s.path:
                            if ext == ".mp3":
                                with open(s.path, "rb") as f:
                                    mp3_out.write(f.read())
                            else:
                                with wave.open(s.path, "rb") as w:
                                    if wav_out is None:
                                        mp3_out.close()
                                        wav_out = wave.open(out, "wb")
                                        wav_out.setparams(w.getparams())
                                    wav_out.writeframes(w.readframes(w.getnframes()))
                            os.remove(s.path)
                        state["done"] = i + 1
            except Exception as e:
                state["error"] = str(e)
            finally:
                if wav_out is not None:
                    wav_out.close()
                state["finished"] = True

        threading.Thread(target=work, daemon=True).start()

        def poll():
            bar["value"] = state["done"]
            lbl.config(text=f"{state['done']} / {len(chunks)} sentences")
            if not state["finished"]:
                win.after(200, poll)
                return
            win.destroy()
            shutil.rmtree(exp_dir, ignore_errors=True)
            if state["error"]:
                messagebox.showerror(APP_NAME, f"Export failed:\n{state['error']}")
            elif state["cancel"]:
                try:
                    os.remove(out)
                except OSError:
                    pass
                self.set_status("Export cancelled.")
            else:
                messagebox.showinfo(APP_NAME, f"Audio saved:\n{out}")

        poll()

    # ----------------------------------------------------------------- exit
    def _save_position(self):
        self._last_save = time.time()
        if self.doc and self.doc.chunks:
            self.settings.setdefault("positions", {})[self.doc.path] = self.cur
            pos = self.settings["positions"]
            if len(pos) > 200:  # keep the file small
                for k in list(pos)[:-200]:
                    pos.pop(k, None)
        self.settings["volume"] = self.player.volume
        self.settings.save()

    def on_close(self):
        self._save_position()
        self.speaker.stop()
        self.player.quit()
        if self.doc:
            self.doc.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        self.root.destroy()


def selftest(out_path):
    """Used by the build pipeline to check the packaged .exe starts correctly."""
    lines = []
    try:
        import edge_tts  # noqa: F401
        from audio_player import AudioPlayer  # noqa: F401
        voices = all_voices()
        lines.append("voices: " + "; ".join(v.label for v in voices))
        doc = pymupdf.open()
        doc.new_page().insert_text((72, 72), "Self test. Hello world.")
        path = os.path.join(tempfile.gettempdir(), "pdfvoicereader_selftest.pdf")
        doc.save(path)
        d = Document(path)
        lines.append(f"chunks: {[c.text for c in d.chunks]}")
        d.close()
        lines.append("SELFTEST OK")
    except Exception:
        lines.append(traceback.format_exc())
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--selftest":
        selftest(sys.argv[2])
        return
    if sys.platform == "win32":
        try:  # crisp text on high-DPI screens
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    initial = sys.argv[1] if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]) else None
    App(root, initial)
    root.mainloop()


if __name__ == "__main__":
    main()
