import json
import os
import shlex
import subprocess
import threading
import time
import hashlib
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# ----------------------------
# CUTTEL v1.6  (full single-file)
# - Remembers settings via cuttel.json (load on start, save on close + on Export)
# - Default output location switches to clip folder on first import (if output not custom)
# - Hover preview (popup after dwell) positioned near cursor (+20px), cached thumbnails
# - Drag & drop reorder with a visible insertion line
# - Cancel button (sends 'q', then terminates)
# - Scrollable log + Clear log button
# - Progress bar via FFmpeg: -progress pipe:1
# ----------------------------

DROP_LINE_COLOR = "#0b6b3a"  # dark green insertion line
PREVIEW_DWELL_MS = 400
PREVIEW_OFFSET_PX = 20
PREVIEW_CLAMP_W = 360  # rough clamp size for keeping popup on-screen
PREVIEW_CLAMP_H = 240

XFADE_TRANSITIONS = [
    ("Fade", "fade"),
    ("Wipe Left", "wipeleft"),
    ("Wipe Right", "wiperight"),
    ("Wipe Up", "wipeup"),
    ("Wipe Down", "wipedown"),
    ("Slide Left", "slideleft"),
    ("Slide Right", "slideright"),
    ("Smooth Left", "smoothleft"),
    ("Smooth Right", "smoothright"),
    ("Circle Open", "circleopen"),
    ("Circle Close", "circleclose"),
]

PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"]

CODECS = [
    ("H.264 (x264, CPU)", "libx264"),
    ("H.264 (NVENC, GPU)", "h264_nvenc"),
    ("H.265 (x265/HEVC, CPU)", "libx265"),
    ("H.265 (NVENC/HEVC, GPU)", "hevc_nvenc"),
]


def which_or_hint(exe_name: str) -> str:
    from shutil import which
    return which(exe_name) or exe_name


FFMPEG = which_or_hint("ffmpeg")
FFPROBE = which_or_hint("ffprobe")

SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cuttel.json")
DEFAULT_OUT = os.path.join(os.path.expanduser("~"), "Desktop", "stitched_1080p.mp4")


def run_cmd_capture(cmd_list):
    p = subprocess.Popen(cmd_list, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out, err = p.communicate()
    return p.returncode, out, err


def ffprobe_info(path: str) -> dict:
    cmd = [
        FFPROBE, "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        path
    ]
    code, out, err = run_cmd_capture(cmd)
    if code != 0:
        raise RuntimeError(f"ffprobe failed for:\n{path}\n\n{err.strip()}")
    return json.loads(out)


def ffprobe_duration_seconds(path: str) -> float:
    data = ffprobe_info(path)

    fmt = data.get("format") or {}
    dur = None
    if "duration" in fmt and fmt["duration"] not in (None, ""):
        try:
            dur = float(fmt["duration"])
        except ValueError:
            dur = None

    if not dur:
        mx = 0.0
        for s in data.get("streams") or []:
            sd = s.get("duration")
            if sd:
                try:
                    mx = max(mx, float(sd))
                except ValueError:
                    pass
        dur = mx if mx > 0 else None

    if not dur:
        raise RuntimeError(f"Could not read duration for:\n{path}")
    return dur


def ffprobe_has_audio(path: str) -> bool:
    data = ffprobe_info(path)
    for s in data.get("streams") or []:
        if (s.get("codec_type") or "").lower() == "audio":
            return True
    return False


def map_ui_preset_to_nvenc(ui_preset: str) -> str:
    slow_group = {"slow", "slower", "veryslow"}
    medium_group = {"medium"}
    fast_group = {"ultrafast", "superfast", "veryfast", "faster", "fast"}
    if ui_preset in slow_group:
        return "slow"
    if ui_preset in medium_group:
        return "medium"
    if ui_preset in fast_group:
        return "fast"
    return "medium"


def build_video_encode_args(vcodec: str, quality: int, ui_preset: str):
    args = ["-c:v", vcodec]

    if vcodec in ("libx264", "libx265"):
        args += ["-preset", ui_preset, "-crf", str(quality)]
        if vcodec == "libx264":
            args += ["-profile:v", "high"]

    elif vcodec in ("h264_nvenc", "hevc_nvenc"):
        nvenc_preset = map_ui_preset_to_nvenc(ui_preset)
        args += ["-preset", nvenc_preset, "-rc", "vbr", "-cq", str(quality), "-b:v", "0"]
        if vcodec == "h264_nvenc":
            args += ["-profile:v", "high"]
        else:
            args += ["-tag:v", "hvc1"]
    else:
        raise ValueError(f"Unknown codec: {vcodec}")

    return args


def build_filter_graph(inputs, durations, has_audio, transition_name, tdur, fps=30, width=1920, height=1080):
    n = len(inputs)
    if n < 1:
        raise ValueError("No inputs")

    parts = []

    for i in range(n):
        parts.append(
            f"[{i}:v]"
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
            f"fps={fps},format=yuv420p,setsar=1"
            f"[v{i}]"
        )

        if has_audio[i]:
            parts.append(f"[{i}:a]aresample=48000[a{i}]")
        else:
            d = durations[i]
            parts.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{d},asetpts=N/SR/TB[a{i}]")

    if n == 1:
        return ";".join(parts), "[v0]", "[a0]"

    v_prev = "v0"
    a_prev = "a0"

    for k in range(n - 1):
        timeline_len = sum(durations[:k+1]) - (k * tdur)
        offset = timeline_len - tdur
        if offset < 0:
            offset = 0.0

        v_out = f"vx{k+1}"
        a_out = f"ax{k+1}"

        parts.append(
            f"[{v_prev}][v{k+1}]"
            f"xfade=transition={transition_name}:duration={tdur}:offset={offset}"
            f"[{v_out}]"
        )
        parts.append(
            f"[{a_prev}][a{k+1}]"
            f"acrossfade=d={tdur}:c1=tri:c2=tri"
            f"[{a_out}]"
        )

        v_prev = v_out
        a_prev = a_out

    return ";".join(parts), f"[{v_prev}]", f"[{a_prev}]"


def fmt_time(sec: float) -> str:
    if sec < 0:
        sec = 0
    m = int(sec // 60)
    s = int(sec % 60)
    h = int(m // 60)
    m = m % 60
    if h > 0:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:d}:{s:02d}"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("CUTTEL (FFmpeg) — v1.6")
        self.geometry("1060x720")
        self.minsize(1060, 720)

        # runtime controls
        self.proc = None
        self.cancel_requested = threading.Event()
        self.total_out_seconds = 0.0

        # iid -> dict(path, dur, has_audio)
        self.items = {}
        self._iid_counter = 1

        # thumbnail cache (hover preview only)
        self.thumb_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cuttel_thumbs")
        os.makedirs(self.thumb_dir, exist_ok=True)

        # hover preview state
        self._hover_after_id = None
        self._hover_iid = None
        self._preview_win = None
        self._preview_label = None
        self._preview_photo = None  # keep ref

        # drag & drop state
        self._drag_iid = None
        self._drop_line_visible = False

        # settings
        self._settings = self._load_settings()

        self._build_ui()
        self._apply_settings(self._settings)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- settings ----------------
    def _load_settings(self) -> dict:
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception:
            return {}

    def _collect_settings(self) -> dict:
        def get_int(var, fallback):
            try:
                return int(var.get())
            except Exception:
                return fallback

        def get_float(var, fallback):
            try:
                return float(var.get())
            except Exception:
                return fallback

        try:
            codec_index = int(self.codec_menu.current())
        except Exception:
            codec_index = 0

        try:
            outp = self.out_var.get().strip()
        except Exception:
            outp = ""

        return {
            "codec_index": codec_index,
            "quality": get_int(self.quality_var, 28),
            "preset": str(self.preset_var.get()),
            "fps": get_int(self.fps_var, 30),
            "abitrate": get_int(self.abitrate_var, 128),
            "transition": str(self.transition_var.get()),
            "tdur": get_float(self.tdur_var, 0.5),
            "output_path": outp,
        }

    def _save_settings(self):
        try:
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(self._collect_settings(), f, indent=2)
        except Exception:
            pass  # no drama

    def _apply_settings(self, s: dict):
        # codec
        ci = s.get("codec_index")
        if isinstance(ci, int) and 0 <= ci < len(CODECS):
            self.codec_menu.current(ci)

        # quality/preset/fps/abitrate
        if "quality" in s:
            try:
                self.quality_var.set(int(s["quality"]))
            except Exception:
                pass

        if "preset" in s and s["preset"] in PRESETS:
            self.preset_var.set(s["preset"])

        if "fps" in s:
            try:
                self.fps_var.set(int(s["fps"]))
            except Exception:
                pass

        if "abitrate" in s:
            try:
                self.abitrate_var.set(int(s["abitrate"]))
            except Exception:
                pass

        # transition + tdur
        tr = s.get("transition")
        if isinstance(tr, str) and tr:
            self.transition_var.set(tr)
            for idx, (_name, code) in enumerate(XFADE_TRANSITIONS):
                if code == tr:
                    try:
                        self.transition_menu.current(idx)
                    except Exception:
                        pass
                    break

        if "tdur" in s:
            try:
                self.tdur_var.set(str(float(s["tdur"])))
            except Exception:
                pass

        # output path
        op = s.get("output_path")
        if isinstance(op, str) and op.strip():
            self.out_var.set(op.strip())

    def _on_close(self):
        self._save_settings()
        self.destroy()

    # ---------------- UI ----------------
    def _build_ui(self):
        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)

        top = ttk.Frame(root)
        top.pack(fill="both", expand=True)

        self.tree_frame = ttk.Frame(top)
        self.tree_frame.pack(side="left", fill="both", expand=True)

        self.tree = ttk.Treeview(
            self.tree_frame,
            columns=("duration", "audio"),
            show="tree headings",
            selectmode="extended",
            height=12
        )
        self.tree.heading("#0", text="Clip")
        self.tree.heading("duration", text="Duration")
        self.tree.heading("audio", text="Audio")
        self.tree.column("#0", width=760, anchor="w")
        self.tree.column("duration", width=110, anchor="center")
        self.tree.column("audio", width=70, anchor="center")
        self.tree.pack(side="left", fill="both", expand=True)

        self.tree_scroll = ttk.Scrollbar(self.tree_frame, orient="vertical", command=self.tree.yview)
        self.tree_scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=self.tree_scroll.set)

        # Drop line indicator
        self.drop_line = tk.Frame(self.tree, height=3, bg=DROP_LINE_COLOR)
        self.drop_line.place_forget()

        # Hover preview bindings
        self.tree.bind("<Motion>", self._on_tree_motion)
        self.tree.bind("<Leave>", self._on_tree_leave)

        # Drag & drop bindings
        self.tree.bind("<ButtonPress-1>", self._on_tree_press, add=True)
        self.tree.bind("<B1-Motion>", self._on_tree_drag)
        self.tree.bind("<ButtonRelease-1>", self._on_tree_release)

        btns = ttk.Frame(top)
        btns.pack(side="left", fill="y", padx=(10, 0))

        ttk.Button(btns, text="Add clips…", command=self.add_files).pack(fill="x", pady=(0, 6))
        ttk.Button(btns, text="Remove", command=self.remove_selected).pack(fill="x", pady=3)
        ttk.Button(btns, text="Move Up", command=lambda: self.move_selected(-1)).pack(fill="x", pady=3)
        ttk.Button(btns, text="Move Down", command=lambda: self.move_selected(+1)).pack(fill="x", pady=3)
        ttk.Separator(btns).pack(fill="x", pady=10)
        ttk.Button(btns, text="Clear", command=self.clear_all).pack(fill="x")

        # ---- settings
        mid = ttk.LabelFrame(root, text="Export settings", padding=10)
        mid.pack(fill="x", pady=(10, 0))

        row = 0

        ttk.Label(mid, text="Transition:").grid(row=row, column=0, sticky="w")
        self.transition_var = tk.StringVar(value=XFADE_TRANSITIONS[0][1])
        self.transition_menu = ttk.Combobox(
            mid, values=[f"{name}  ({code})" for name, code in XFADE_TRANSITIONS], state="readonly"
        )
        self.transition_menu.current(0)
        self.transition_menu.grid(row=row, column=1, sticky="we", padx=8)

        def on_transition_pick(_evt=None):
            idx = self.transition_menu.current()
            self.transition_var.set(XFADE_TRANSITIONS[idx][1])

        self.transition_menu.bind("<<ComboboxSelected>>", on_transition_pick)

        ttk.Label(mid, text="Transition duration (s):").grid(row=row, column=2, sticky="w")
        self.tdur_var = tk.StringVar(value="0.50")
        ttk.Entry(mid, textvariable=self.tdur_var, width=8).grid(row=row, column=3, sticky="w")

        row += 1

        ttk.Label(mid, text="Codec:").grid(row=row, column=0, sticky="w")
        self.codec_menu = ttk.Combobox(mid, values=[name for name, _ in CODECS], state="readonly", width=28)
        self.codec_menu.current(3)  # default: H.265 NVENC/HEVC
        self.codec_menu.grid(row=row, column=1, sticky="w", padx=8)

        ttk.Label(mid, text="Quality (CRF/CQ):").grid(row=row, column=2, sticky="w")
        self.quality_var = tk.IntVar(value=28)
        ttk.Spinbox(mid, from_=16, to=35, textvariable=self.quality_var, width=6).grid(row=row, column=3, sticky="w")

        def on_codec_pick(_evt=None):
            idx = self.codec_menu.current()
            vcodec = CODECS[idx][1]
            q = self.quality_var.get()
            if vcodec in ("libx265", "hevc_nvenc") and q == 23:
                self.quality_var.set(28)
            if vcodec in ("libx264", "h264_nvenc") and q == 28:
                self.quality_var.set(23)

        self.codec_menu.bind("<<ComboboxSelected>>", on_codec_pick)

        row += 1

        ttk.Label(mid, text="Preset (speed/size):").grid(row=row, column=0, sticky="w")
        self.preset_var = tk.StringVar(value="slow")
        ttk.Combobox(mid, values=PRESETS, textvariable=self.preset_var, state="readonly", width=12).grid(
            row=row, column=1, sticky="w", padx=8
        )

        ttk.Label(mid, text="FPS:").grid(row=row, column=2, sticky="w")
        self.fps_var = tk.IntVar(value=30)
        ttk.Spinbox(mid, from_=24, to=60, textvariable=self.fps_var, width=6).grid(row=row, column=3, sticky="w")

        row += 1

        ttk.Label(mid, text="Audio (kbps):").grid(row=row, column=0, sticky="w")
        self.abitrate_var = tk.IntVar(value=128)
        ttk.Spinbox(mid, from_=96, to=320, increment=32, textvariable=self.abitrate_var, width=6).grid(
            row=row, column=1, sticky="w", padx=8
        )

        ttk.Label(mid, text="Hover preview (dwell) + drag & drop reorder (insertion line).").grid(
            row=row, column=2, columnspan=2, sticky="w"
        )

        mid.grid_columnconfigure(1, weight=1)

        # ---- output
        out = ttk.LabelFrame(root, text="Output", padding=10)
        out.pack(fill="x", pady=(10, 0))

        self.out_var = tk.StringVar(value=DEFAULT_OUT)
        self.out_entry = ttk.Entry(out, textvariable=self.out_var)
        self.out_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(out, text="Browse…", command=self.pick_output).pack(side="left", padx=(8, 0))

        # ---- controls + progress
        ctrl = ttk.Frame(root)
        ctrl.pack(fill="x", pady=(10, 0))

        self.start_btn = ttk.Button(ctrl, text="Stitch + Export", command=self.start)
        self.start_btn.pack(side="left")

        self.cancel_btn = ttk.Button(ctrl, text="Cancel", command=self.cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=(8, 0))

        self.clear_log_btn = ttk.Button(ctrl, text="Clear log", command=self.clear_log)
        self.clear_log_btn.pack(side="left", padx=(8, 0))

        self.progress = ttk.Progressbar(ctrl, orient="horizontal", mode="determinate", length=500)
        self.progress.pack(side="left", padx=12, fill="x", expand=True)

        self.progress_label = ttk.Label(ctrl, text="Idle.")
        self.progress_label.pack(side="left", padx=(10, 0))

        # ---- scrollable log
        log_frame = ttk.LabelFrame(root, text="Log", padding=6)
        log_frame.pack(fill="both", expand=False, pady=(10, 0))

        self.log = tk.Text(log_frame, height=14, wrap="none")
        self.log.pack(side="left", fill="both", expand=True)

        self.log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        self.log_scroll.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=self.log_scroll.set)

        self.log.bind("<MouseWheel>", self._on_mousewheel)
        self.log.bind("<Button-4>", lambda e: self.log.yview_scroll(-1, "units"))
        self.log.bind("<Button-5>", lambda e: self.log.yview_scroll(+1, "units"))

        try:
            ttk.Style().theme_use("clam")
        except tk.TclError:
            pass

    # ---------------- log helpers ----------------
    def clear_log(self):
        self.log.delete("1.0", "end")

    def _on_mousewheel(self, event):
        lines = int(-1 * (event.delta / 120))
        self.log.yview_scroll(lines, "units")

    def ui(self, fn, *args):
        self.after(0, lambda: fn(*args))

    def log_line(self, s: str):
        self.log.insert("end", s.rstrip() + "\n")
        self.log.see("end")

    def set_progress(self, cur_sec: float, total_sec: float, speed: str = ""):
        if total_sec <= 0:
            self.progress["value"] = 0
            self.progress_label.config(text="Working…")
            return
        pct = max(0.0, min(100.0, (cur_sec / total_sec) * 100.0))
        self.progress["value"] = pct
        ttxt = f"{fmt_time(cur_sec)} / {fmt_time(total_sec)}"
        if speed:
            ttxt += f"  ({speed})"
        self.progress_label.config(text=ttxt)

    def set_status_text(self, s: str):
        self.progress_label.config(text=s)

    def set_busy_state(self, busy: bool):
        self.start_btn.config(state="disabled" if busy else "normal")
        self.cancel_btn.config(state="normal" if busy else "disabled")

    # ---------------- thumb cache (hover preview) ----------------
    def _thumb_key(self, path: str) -> str:
        st = os.stat(path)
        raw = f"{path}|{st.st_size}|{st.st_mtime_ns}".encode("utf-8", errors="ignore")
        return hashlib.md5(raw).hexdigest()

    def _thumb_path(self, path: str) -> str:
        return os.path.join(self.thumb_dir, self._thumb_key(path) + ".png")

    def _ensure_thumb(self, path: str) -> str | None:
        out_png = self._thumb_path(path)
        if os.path.exists(out_png):
            return out_png

        cmd = [
            FFMPEG, "-y",
            "-ss", "0.2",
            "-i", path,
            "-frames:v", "1",
            "-vf", "scale=320:-1",
            "-f", "image2",
            out_png
        ]
        code, _out, _err = run_cmd_capture(cmd)
        if code != 0 or not os.path.exists(out_png):
            return None
        return out_png

    # ---------------- hover preview popup ----------------
    def _preview_hide(self):
        if self._hover_after_id:
            try:
                self.after_cancel(self._hover_after_id)
            except Exception:
                pass
            self._hover_after_id = None
        self._hover_iid = None
        if self._preview_win:
            try:
                self._preview_win.destroy()
            except Exception:
                pass
        self._preview_win = None
        self._preview_label = None
        self._preview_photo = None

    def _preview_show_loading(self, iid: str, x_root: int, y_root: int):
        self._preview_hide()
        self._hover_iid = iid

        win = tk.Toplevel(self)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.geometry(f"+{x_root}+{y_root}")
        frm = ttk.Frame(win, padding=6)
        frm.pack(fill="both", expand=True)

        lbl = ttk.Label(frm, text="Loading preview…")
        lbl.pack()

        self._preview_win = win
        self._preview_label = lbl

    def _preview_update_image(self, iid: str, photo: tk.PhotoImage):
        if self._hover_iid != iid or not self._preview_win or not self._preview_label:
            return
        self._preview_photo = photo
        self._preview_label.configure(image=photo, text="")
        self._preview_label.image = photo

    def _preview_load_async(self, iid: str, path: str):
        def worker():
            thumb_file = self._ensure_thumb(path)
            if not thumb_file:
                return
            try:
                photo = tk.PhotoImage(file=thumb_file)
            except Exception:
                return
            self.ui(self._preview_update_image, iid, photo)
        threading.Thread(target=worker, daemon=True).start()

    def _clamp_preview_pos(self, x_root: int, y_root: int) -> tuple[int, int]:
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        if x_root + PREVIEW_CLAMP_W > sw:
            x_root = sw - PREVIEW_CLAMP_W - 10
        if y_root + PREVIEW_CLAMP_H > sh:
            y_root = sh - PREVIEW_CLAMP_H - 10
        if x_root < 10:
            x_root = 10
        if y_root < 10:
            y_root = 10
        return int(x_root), int(y_root)

    def _on_tree_motion(self, event):
        if self._drag_iid is not None:
            self._preview_hide()
            return

        iid = self.tree.identify_row(event.y)
        if not iid:
            self._preview_hide()
            return

        if iid == self._hover_iid:
            return

        self._preview_hide()

        # popup near cursor (+20px)
        x_root = self.winfo_pointerx() + PREVIEW_OFFSET_PX
        y_root = self.winfo_pointery() + PREVIEW_OFFSET_PX
        x_root, y_root = self._clamp_preview_pos(x_root, y_root)

        self._hover_iid = iid

        def show():
            if self._hover_iid != iid:
                return
            item = self.items.get(iid)
            if not item:
                return
            self._preview_show_loading(iid, x_root, y_root)
            self._preview_load_async(iid, item["path"])

        self._hover_after_id = self.after(PREVIEW_DWELL_MS, show)

    def _on_tree_leave(self, _event):
        self._preview_hide()
        self._drop_line_hide()

    # ---------------- drop line indicator ----------------
    def _drop_line_show(self, y: int):
        w = max(10, self.tree.winfo_width())
        self.drop_line.place(in_=self.tree, x=0, y=y, width=w, height=3)
        self._drop_line_visible = True

    def _drop_line_hide(self):
        if self._drop_line_visible:
            self.drop_line.place_forget()
            self._drop_line_visible = False

    def _compute_drop_line_y(self, event_y: int):
        iid = self.tree.identify_row(event_y)
        if not iid:
            return None
        bbox = self.tree.bbox(iid)
        if not bbox:
            return None
        _x, y, _w, h = bbox
        insert_after = event_y > (y + h / 2)
        line_y = y + (h if insert_after else 0)
        return iid, insert_after, int(line_y)

    # ---------------- drag & drop reorder ----------------
    def _on_tree_press(self, event):
        region = self.tree.identify_region(event.x, event.y)
        if region == "heading":
            return
        iid = self.tree.identify_row(event.y)
        if not iid:
            self._drag_iid = None
            self._drop_line_hide()
            return
        self._drag_iid = iid
        self._preview_hide()

    def _contiguous_in_current_order(self, iids: list[str]) -> bool:
        children = list(self.tree.get_children(""))
        idxs = [children.index(i) for i in iids if i in children]
        if not idxs:
            return False
        idxs_sorted = sorted(idxs)
        return idxs_sorted == list(range(idxs_sorted[0], idxs_sorted[0] + len(idxs_sorted)))

    def _on_tree_drag(self, event):
        if not self._drag_iid:
            return
        self._preview_hide()
        tgt = self._compute_drop_line_y(event.y)
        if not tgt:
            self._drop_line_hide()
            return
        _iid, _insert_after, line_y = tgt
        self._drop_line_show(line_y)

    def _on_tree_release(self, event):
        if not self._drag_iid:
            self._drop_line_hide()
            return

        self._drop_line_hide()

        drop_iid = self.tree.identify_row(event.y)
        if not drop_iid:
            self._drag_iid = None
            return

        children = list(self.tree.get_children(""))

        sel = list(self.tree.selection())
        if self._drag_iid in sel and len(sel) > 1 and self._contiguous_in_current_order(sel):
            block = [c for c in children if c in sel]
        else:
            block = [self._drag_iid]

        if drop_iid in block:
            self._drag_iid = None
            return

        bbox = self.tree.bbox(drop_iid)
        insert_after = False
        if bbox:
            _x, y, _w, h = bbox
            if event.y > y + (h / 2):
                insert_after = True

        base = [c for c in children if c not in block]

        try:
            drop_index = base.index(drop_iid)
        except ValueError:
            drop_index = len(base)

        if insert_after:
            drop_index += 1

        new_order = base[:drop_index] + block + base[drop_index:]

        for idx, iid in enumerate(new_order):
            self.tree.move(iid, "", idx)

        self.tree.selection_set(block)
        self.tree.focus(block[0])
        self._drag_iid = None

    # ---------------- list operations ----------------
    def _existing_paths_set(self):
        return {item["path"] for item in self.items.values()}

    def _should_autoset_output_on_first_import(self) -> bool:
        # Only auto-set output path if user didn't already choose something custom.
        current = (self.out_var.get() or "").strip()
        saved = (self._settings.get("output_path") or "").strip()
        if saved:
            # they had something saved -> don't override
            return False
        if not current:
            return True
        if os.path.normpath(current) == os.path.normpath(DEFAULT_OUT):
            return True
        return False

    def _set_default_output_folder_from_path(self, first_path: str):
        folder = os.path.dirname(first_path)
        out_path = os.path.join(folder, "stitched_1080p.mp4")
        self.out_var.set(out_path)

    def add_files(self):
        paths = filedialog.askopenfilenames(
            title="Select video clips",
            filetypes=[
                ("Video files", "*.mp4 *.mov *.mkv *.m4v *.avi *.webm"),
                ("All files", "*.*")
            ]
        )
        if not paths:
            return

        was_empty = (len(self.items) == 0)

        existing = self._existing_paths_set()
        new_iids = []

        for p in paths:
            if p in existing:
                continue
            iid = f"c{self._iid_counter}"
            self._iid_counter += 1

            base = os.path.basename(p)
            self.items[iid] = {"path": p, "dur": None, "has_audio": None}
            self.tree.insert("", "end", iid=iid, text=base, values=("…", "…"))
            new_iids.append(iid)

        # Default output path to the folder we selected clips from (first import batch)
        if was_empty and len(self.items) > 0 and self._should_autoset_output_on_first_import():
            self._set_default_output_folder_from_path(paths[0])
            self._save_settings()  # persist the new default immediately

        if new_iids:
            self._start_background_probe(new_iids)

    def _start_background_probe(self, iids):
        def worker():
            for iid in iids:
                item = self.items.get(iid)
                if not item:
                    continue
                path = item["path"]
                try:
                    d = ffprobe_duration_seconds(path)
                    a = ffprobe_has_audio(path)
                except Exception:
                    d = None
                    a = None

                def apply():
                    cur = self.items.get(iid)
                    if not cur:
                        return
                    cur["dur"] = d
                    cur["has_audio"] = a
                    self.tree.set(iid, "duration", fmt_time(d) if d is not None else "?")
                    self.tree.set(iid, "audio", "yes" if a else "no" if a is not None else "?")

                self.ui(apply)

        threading.Thread(target=worker, daemon=True).start()

    def remove_selected(self):
        sel = list(self.tree.selection())
        if not sel:
            return
        self._preview_hide()
        for iid in sel:
            self.items.pop(iid, None)
            self.tree.delete(iid)

    def clear_all(self):
        self._preview_hide()
        self._drop_line_hide()
        for iid in list(self.items.keys()):
            self.tree.delete(iid)
        self.items.clear()

    def move_selected(self, direction: int):
        sel = list(self.tree.selection())
        if not sel:
            return

        children = list(self.tree.get_children(""))
        idxs = [children.index(i) for i in sel if i in children]
        if not idxs:
            return

        idxs_sorted = sorted(idxs)
        if idxs_sorted != list(range(idxs_sorted[0], idxs_sorted[0] + len(idxs_sorted))):
            messagebox.showinfo("Move", "Select a contiguous block to move.")
            return

        start = idxs_sorted[0]
        end = idxs_sorted[-1]
        new_start = start + direction
        new_end = end + direction
        if new_start < 0 or new_end >= len(children):
            return

        block = children[start:end + 1]
        base = [c for c in children if c not in block]
        base.insert(new_start, "__BLOCK__")
        new_order = []
        for c in base:
            if c == "__BLOCK__":
                new_order.extend(block)
            else:
                new_order.append(c)

        for idx, iid in enumerate(new_order):
            self.tree.move(iid, "", idx)

        self.tree.selection_set(block)
        self.tree.focus(block[0])

    def pick_output(self):
        path = filedialog.asksaveasfilename(
            title="Save output as",
            defaultextension=".mp4",
            filetypes=[("MP4 video", "*.mp4")]
        )
        if path:
            self.out_var.set(path)
            self._save_settings()

    # ---------------- cancel ----------------
    def cancel(self):
        self.cancel_requested.set()
        p = self.proc
        if p and p.poll() is None:
            try:
                if p.stdin:
                    p.stdin.write("q\n")
                    p.stdin.flush()
            except Exception:
                pass
        self.ui(self.set_status_text, "Cancelling…")

    # ---------------- export ----------------
    def _ordered_paths(self):
        paths = []
        for iid in self.tree.get_children(""):
            item = self.items.get(iid)
            if item:
                paths.append(item["path"])
        return paths

    def start(self):
        paths = self._ordered_paths()
        if len(paths) == 0:
            messagebox.showwarning("No clips", "Add at least one clip.")
            return

        out_path = self.out_var.get().strip()
        if not out_path:
            messagebox.showwarning("Output missing", "Pick an output path.")
            return

        try:
            tdur = float(self.tdur_var.get().strip())
            if tdur <= 0 or tdur > 5:
                raise ValueError()
        except ValueError:
            messagebox.showwarning("Bad transition duration", "Transition duration must be a number between 0 and 5.")
            return

        # Save settings immediately (so your choices survive even if FFmpeg dies)
        self._save_settings()

        fps = int(self.fps_var.get())
        quality = int(self.quality_var.get())
        preset = self.preset_var.get()
        abitrate = int(self.abitrate_var.get())
        transition = self.transition_var.get()

        idx = self.codec_menu.current()
        vcodec = CODECS[idx][1] if idx >= 0 else "libx264"

        self.cancel_requested.clear()
        self.proc = None
        self.total_out_seconds = 0.0
        self.progress["value"] = 0

        self._preview_hide()
        self._drop_line_hide()
        self.set_busy_state(True)
        self.log.delete("1.0", "end")
        self.log_line("----")
        self.log_line("Starting…")
        self.log_line(f"Codec: {vcodec} | Quality(CRF/CQ): {quality} | Preset: {preset} | FPS: {fps}")

        def worker():
            try:
                durations = []
                has_audio = []

                self.ui(self.set_status_text, "Probing clips…")

                for pth in paths:
                    if self.cancel_requested.is_set():
                        raise RuntimeError("Cancelled.")
                    self.ui(self.log_line, f"ffprobe: {pth}")
                    d = ffprobe_duration_seconds(pth)
                    a = ffprobe_has_audio(pth)
                    durations.append(d)
                    has_audio.append(a)
                    self.ui(self.log_line, f"  duration: {d:.3f}s | audio: {'yes' if a else 'NO (silence will be added)'}")

                n = len(durations)
                out_total = sum(durations) - max(0, (n - 1)) * tdur
                if out_total < 0:
                    out_total = 0.0
                self.total_out_seconds = out_total

                self.ui(self.set_progress, 0.0, self.total_out_seconds, "")

                self.ui(self.set_status_text, "Building ffmpeg graph…")
                filt, vmap, amap = build_filter_graph(
                    paths, durations, has_audio, transition, tdur, fps=fps, width=1920, height=1080
                )

                cmd = [FFMPEG, "-y"]
                for pth in paths:
                    cmd += ["-i", pth]

                cmd += [
                    "-filter_complex", filt,
                    "-map", vmap,
                    "-map", amap,
                ]

                cmd += build_video_encode_args(vcodec, quality, preset)

                cmd += [
                    "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart",
                    "-c:a", "aac",
                    "-b:a", f"{abitrate}k",
                    "-progress", "pipe:1",
                    "-nostats",
                    out_path
                ]

                pretty = " ".join(shlex.quote(x) for x in cmd)
                self.ui(self.log_line, "")
                self.ui(self.log_line, "FFmpeg command:")
                self.ui(self.log_line, pretty)
                self.ui(self.log_line, "")

                self.ui(self.set_status_text, "Encoding…")

                p = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                    universal_newlines=True,
                )
                self.proc = p

                def read_stderr():
                    try:
                        for line in p.stderr:
                            if self.cancel_requested.is_set():
                                break
                            s = line.rstrip()
                            if s:
                                self.ui(self.log_line, s)
                    except Exception:
                        pass

                t_err = threading.Thread(target=read_stderr, daemon=True)
                t_err.start()

                cur_sec = 0.0
                speed = ""
                last_ui = 0.0

                for line in p.stdout:
                    if self.cancel_requested.is_set():
                        break
                    line = line.strip()
                    if not line:
                        continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip()

                        if k == "out_time_ms":
                            try:
                                cur_sec = float(v) / 1_000_000.0
                            except ValueError:
                                pass
                        elif k == "out_time":
                            try:
                                parts = v.split(":")
                                if len(parts) == 3:
                                    hh = int(parts[0])
                                    mm = int(parts[1])
                                    ss = float(parts[2])
                                    cur_sec = hh * 3600 + mm * 60 + ss
                            except Exception:
                                pass
                        elif k == "speed":
                            speed = v

                        now = time.time()
                        if now - last_ui > 0.15:
                            last_ui = now
                            self.ui(self.set_progress, cur_sec, self.total_out_seconds, speed)
                    else:
                        self.ui(self.log_line, line)

                if self.cancel_requested.is_set():
                    try:
                        if p.poll() is None:
                            p.terminate()
                            try:
                                p.wait(timeout=2)
                            except subprocess.TimeoutExpired:
                                p.kill()
                    except Exception:
                        pass
                    raise RuntimeError("Cancelled.")

                code = p.wait()
                if code != 0:
                    raise RuntimeError(f"ffmpeg failed with exit code {code}")

                self.ui(self.set_progress, self.total_out_seconds, self.total_out_seconds, speed)
                self.ui(self.set_status_text, "Done.")
                self.ui(self.log_line, "")
                self.ui(self.log_line, f"SUCCESS: {out_path}")
                self.ui(messagebox.showinfo, "Done", f"Export finished:\n{out_path}")

            except Exception as e:
                msg = str(e)
                if "Cancelled" in msg:
                    self.ui(self.set_status_text, "Cancelled.")
                    self.ui(self.log_line, "")
                    self.ui(self.log_line, "CANCELLED.")
                else:
                    self.ui(self.set_status_text, "Error.")
                    self.ui(self.log_line, "")
                    self.ui(self.log_line, "ERROR:")
                    self.ui(self.log_line, msg)
                    self.ui(messagebox.showerror, "Error", msg)
            finally:
                self.proc = None
                self.ui(self.set_busy_state, False)

        threading.Thread(target=worker, daemon=True).start()


if __name__ == "__main__":
    App().mainloop()
