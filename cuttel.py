import json
import os
import shlex
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# ----------------------------
# Pixel-Clip Stapler v1.1
# FFmpeg front-end with transitions + x264/x265 + NVENC (RTX-friendly)
# ----------------------------

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

# UI preset list (maps to x264/x265 presets directly; for NVENC we map to slow/medium/fast-ish)
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

    # Prefer container duration
    fmt = data.get("format") or {}
    dur = None
    if "duration" in fmt and fmt["duration"] not in (None, ""):
        try:
            dur = float(fmt["duration"])
        except ValueError:
            dur = None

    # Fallback: max stream duration
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
    """
    NVENC presets are not the same as x264/x265. We'll map loosely.
    This avoids using p1..p7 which varies by FFmpeg build/options.
    """
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
    """
    quality:
      - For x264/x265: CRF (lower = better, bigger)
      - For NVENC: CQ (similar idea; lower = better, bigger)
    """
    args = ["-c:v", vcodec]

    if vcodec in ("libx264", "libx265"):
        args += ["-preset", ui_preset, "-crf", str(quality)]
        if vcodec == "libx264":
            args += ["-profile:v", "high"]
        else:
            # x265 default is fine; we keep 8-bit yuv420p for broad compatibility
            pass

    elif vcodec in ("h264_nvenc", "hevc_nvenc"):
        nvenc_preset = map_ui_preset_to_nvenc(ui_preset)
        # NVENC rate control:
        # -rc vbr for sane quality/size tradeoff, controlled via -cq
        # (YouTube upload use-case: we mainly want fast + reasonable size)
        args += ["-preset", nvenc_preset, "-rc", "vbr", "-cq", str(quality), "-b:v", "0"]
        if vcodec == "h264_nvenc":
            args += ["-profile:v", "high"]
        else:
            # hevc tag for better Apple playback compatibility; harmless elsewhere
            args += ["-tag:v", "hvc1"]

    else:
        raise ValueError(f"Unknown codec: {vcodec}")

    return args


def build_filter_graph(inputs, durations, has_audio, transition_name, tdur, fps=30, width=1920, height=1080):
    """
    Build filter_complex for:
    - Normalize video: scale+pad to 1080p, fps, format, SAR
    - Audio: if present, resample; if missing, generate silence of matching duration
    - Chain xfade + acrossfade
    """
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
            # Generate silence for the clip duration so acrossfade works
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


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Pixel Clip Stapler (FFmpeg) — v1.1")

        self.geometry("900x600")
        self.minsize(900, 600)

        self.files = []
        self._build_ui()

    def _build_ui(self):
        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)

        top = ttk.Frame(root)
        top.pack(fill="both", expand=True)

        self.listbox = tk.Listbox(top, selectmode=tk.EXTENDED)
        self.listbox.pack(side="left", fill="both", expand=True)

        btns = ttk.Frame(top)
        btns.pack(side="left", fill="y", padx=(10, 0))

        ttk.Button(btns, text="Add clips…", command=self.add_files).pack(fill="x", pady=(0, 6))
        ttk.Button(btns, text="Remove", command=self.remove_selected).pack(fill="x", pady=3)
        ttk.Button(btns, text="Move Up", command=lambda: self.move_selected(-1)).pack(fill="x", pady=3)
        ttk.Button(btns, text="Move Down", command=lambda: self.move_selected(+1)).pack(fill="x", pady=3)
        ttk.Separator(btns).pack(fill="x", pady=10)
        ttk.Button(btns, text="Clear", command=self.clear_all).pack(fill="x")

        mid = ttk.LabelFrame(root, text="Export settings", padding=10)
        mid.pack(fill="x", pady=(10, 0))

        row = 0

        ttk.Label(mid, text="Transition:").grid(row=row, column=0, sticky="w")
        self.transition_var = tk.StringVar(value=XFADE_TRANSITIONS[0][1])
        self.transition_menu = ttk.Combobox(
            mid,
            values=[f"{name}  ({code})" for name, code in XFADE_TRANSITIONS],
            state="readonly"
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
        self.codec_var = tk.StringVar(value=CODECS[0][1])
        self.codec_menu = ttk.Combobox(
            mid,
            values=[name for name, _ in CODECS],
            state="readonly",
            width=22
        )
        self.codec_menu.current(0)
        self.codec_menu.grid(row=row, column=1, sticky="w", padx=8)

        def on_codec_pick(_evt=None):
            idx = self.codec_menu.current()
            self.codec_var.set(CODECS[idx][1])
            # Nudge defaults if user switches families
            vcodec = self.codec_var.get()
            q = self.quality_var.get()
            # If you select x265 or hevc_nvenc and quality is x264-ish default, bump to a more typical HEVC-ish default.
            if vcodec in ("libx265", "hevc_nvenc") and q == 23:
                self.quality_var.set(28)
            # If you select x264/h264_nvenc and quality is hevc-ish default, pull back.
            if vcodec in ("libx264", "h264_nvenc") and q == 28:
                self.quality_var.set(23)

        self.codec_menu.bind("<<ComboboxSelected>>", on_codec_pick)

        ttk.Label(mid, text="Quality (CRF/CQ):").grid(row=row, column=2, sticky="w")
        self.quality_var = tk.IntVar(value=23)  # x264-ish default
        ttk.Spinbox(mid, from_=16, to=35, textvariable=self.quality_var, width=6).grid(row=row, column=3, sticky="w")

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

        ttk.Label(mid, text="Note: NVENC = fast; x264/x265 = slower but best compression.").grid(
            row=row, column=2, columnspan=2, sticky="w"
        )

        mid.grid_columnconfigure(1, weight=1)

        out = ttk.LabelFrame(root, text="Output", padding=10)
        out.pack(fill="x", pady=(10, 0))

        self.out_var = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Desktop", "stitched_1080p.mp4"))
        ttk.Entry(out, textvariable=self.out_var).pack(side="left", fill="x", expand=True)
        ttk.Button(out, text="Browse…", command=self.pick_output).pack(side="left", padx=(8, 0))

        bottom = ttk.Frame(root)
        bottom.pack(fill="x", pady=(10, 0))

        self.start_btn = ttk.Button(bottom, text="Stitch + Export", command=self.start)
        self.start_btn.pack(side="left")

        self.progress = ttk.Label(bottom, text="Idle.")
        self.progress.pack(side="left", padx=12)

        self.log = tk.Text(root, height=12)
        self.log.pack(fill="both", expand=False, pady=(10, 0))
        self.log.configure(state="disabled")

        try:
            ttk.Style().theme_use("clam")
        except tk.TclError:
            pass

    def log_line(self, s: str):
        self.log.configure(state="normal")
        self.log.insert("end", s.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def set_status(self, s: str):
        self.progress.config(text=s)

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
        for p in paths:
            if p not in self.files:
                self.files.append(p)
                self.listbox.insert("end", p)

    def remove_selected(self):
        sel = list(self.listbox.curselection())
        if not sel:
            return
        for idx in reversed(sel):
            self.files.pop(idx)
            self.listbox.delete(idx)

    def clear_all(self):
        self.files.clear()
        self.listbox.delete(0, "end")

    def move_selected(self, direction: int):
        sel = list(self.listbox.curselection())
        if not sel:
            return
        if sel != list(range(sel[0], sel[0] + len(sel))):
            messagebox.showinfo("Move", "Select a contiguous block to move.")
            return

        start = sel[0]
        end = sel[-1]
        new_start = start + direction
        new_end = end + direction
        if new_start < 0 or new_end >= len(self.files):
            return

        block = self.files[start:end + 1]
        del self.files[start:end + 1]
        for i, item in enumerate(block):
            self.files.insert(new_start + i, item)

        self.listbox.delete(0, "end")
        for f in self.files:
            self.listbox.insert("end", f)

        self.listbox.selection_clear(0, "end")
        for i in range(new_start, new_start + len(block)):
            self.listbox.selection_set(i)
        self.listbox.activate(new_start)

    def pick_output(self):
        path = filedialog.asksaveasfilename(
            title="Save output as",
            defaultextension=".mp4",
            filetypes=[("MP4 video", "*.mp4")]
        )
        if path:
            self.out_var.set(path)

    def start(self):
        if len(self.files) == 0:
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

        fps = int(self.fps_var.get())
        quality = int(self.quality_var.get())
        preset = self.preset_var.get()
        abitrate = int(self.abitrate_var.get())
        transition = self.transition_var.get()

        # resolve selected codec label -> codec id
        idx = self.codec_menu.current()
        vcodec = CODECS[idx][1] if idx >= 0 else self.codec_var.get()

        self.start_btn.config(state="disabled")
        self.set_status("Probing clips…")
        self.log_line("----")
        self.log_line("Starting…")
        self.log_line(f"Codec: {vcodec} | Quality(CRF/CQ): {quality} | Preset: {preset} | FPS: {fps}")

        def worker():
            try:
                durations = []
                has_audio = []

                for p in self.files:
                    self.log_line(f"ffprobe: {p}")
                    d = ffprobe_duration_seconds(p)
                    a = ffprobe_has_audio(p)
                    durations.append(d)
                    has_audio.append(a)
                    self.log_line(f"  duration: {d:.3f}s | audio: {'yes' if a else 'NO (silence will be added)'}")

                self.set_status("Building ffmpeg graph…")
                filt, vmap, amap = build_filter_graph(
                    self.files, durations, has_audio, transition, tdur, fps=fps, width=1920, height=1080
                )

                cmd = [FFMPEG, "-y"]
                for p in self.files:
                    cmd += ["-i", p]

                cmd += [
                    "-filter_complex", filt,
                    "-map", vmap,
                    "-map", amap,
                ]

                cmd += build_video_encode_args(vcodec, quality, preset)

                # universal output flags
                cmd += [
                    "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart",
                    "-c:a", "aac",
                    "-b:a", f"{abitrate}k",
                    out_path
                ]

                pretty = " ".join(shlex.quote(x) for x in cmd)
                self.log_line("")
                self.log_line("FFmpeg command:")
                self.log_line(pretty)
                self.log_line("")

                self.set_status("Encoding… (GPU goes brrr if NVENC)")
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

                for line in p.stdout:
                    self.log_line(line.rstrip())

                code = p.wait()
                if code != 0:
                    raise RuntimeError(f"ffmpeg failed with exit code {code}")

                self.set_status("Done.")
                self.log_line("")
                self.log_line(f"SUCCESS: {out_path}")
                messagebox.showinfo("Done", f"Export finished:\n{out_path}")

            except Exception as e:
                self.set_status("Error.")
                self.log_line("")
                self.log_line("ERROR:")
                self.log_line(str(e))
                messagebox.showerror("Error", str(e))
            finally:
                self.start_btn.config(state="normal")

        threading.Thread(target=worker, daemon=True).start()


if __name__ == "__main__":
    app = App()
    app.mainloop()
