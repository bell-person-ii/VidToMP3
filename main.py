"""VidToAudio - 영상 파일에서 음원만 추출하는 GUI 프로그램."""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import converter as cv
import transcriber as tr

CPU_COUNT = os.cpu_count() or 1
FORMATS_BY_LABEL = {f.label: f for f in cv.AUDIO_FORMATS.values()}
MODELS_BY_LABEL = {m.label: m for m in tr.MODELS}
VIDEO_FILETYPES = [("동영상 파일", " ".join("*" + e for e in cv.VIDEO_EXTENSIONS)), ("모든 파일", "*.*")]


def format_duration(seconds: float) -> str:
    minutes, secs = divmod(int(round(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def describe_info(info: cv.MediaInfo) -> str:
    channels = {1: "모노", 2: "스테레오"}.get(info.channels, f"{info.channels}채널" if info.channels else "")
    parts = [info.codec.upper(), f"{info.sample_rate} Hz" if info.sample_rate else "", channels,
             f"길이 {format_duration(info.duration)}" if info.duration else ""]
    return "원본 오디오: " + " · ".join(p for p in parts if p)


class App(tk.Tk):
    def __init__(self, ffmpeg: str):
        super().__init__()
        self.ffmpeg = ffmpeg
        self.title("VidToAudio - 영상에서 음원 추출")
        self.scale = self.winfo_fpixels("1i") / 96  # 고해상도(DPI) 화면 대응
        self.geometry(f"{self._px(780)}x{self._px(560)}")
        self.minsize(self._px(660), self._px(480))

        self.src_var = tk.StringVar()
        self.dst_var = tk.StringVar()
        self.format_var = tk.StringVar(value=cv.AUDIO_FORMATS["mp3"].label)
        self.bitrate_var = tk.StringVar(value="192k")
        self.threads_var = tk.IntVar(value=CPU_COUNT)
        self.copy_var = tk.BooleanVar(value=True)
        self.text_var = tk.BooleanVar(value=False)
        self.text_unavailable = tr.unavailable_reason()
        self.model_var = tk.StringVar(value=tr.MODELS[0].label)
        self.model_hint_var = tk.StringVar()
        self.info_var = tk.StringVar()
        self.plan_var = tk.StringVar()
        self.status_var = tk.StringVar(value="변환할 영상 파일을 선택하세요.")

        self.info: cv.MediaInfo | None = None
        self.loaded_src = ""
        self.extractor: cv.Extractor | None = None
        self.transcriber: tr.Transcriber | None = None
        self.events: queue.Queue = queue.Queue()
        self.started_at = 0.0
        self.audio_done_at = 0.0
        self.fraction = 0.0
        self.stage = ""
        self.last_output: Path | None = None

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll_events)

    def _px(self, value: int) -> int:
        return int(value * self.scale)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        pad = {"padx": 6, "pady": 4}
        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)

        files = ttk.LabelFrame(root, text="파일", padding=8)
        files.pack(fill="x")
        files.columnconfigure(1, weight=1)
        ttk.Label(files, text="영상 파일").grid(row=0, column=0, sticky="w", **pad)
        src_entry = ttk.Entry(files, textvariable=self.src_var)
        src_entry.grid(row=0, column=1, sticky="ew", **pad)
        src_entry.bind("<Return>", lambda _e: self._load_source())
        src_entry.bind("<FocusOut>", lambda _e: self._load_source())
        src_btn = ttk.Button(files, text="찾아보기…", command=self._browse_source)
        src_btn.grid(row=0, column=2, **pad)
        ttk.Label(files, textvariable=self.info_var, foreground="gray").grid(
            row=1, column=1, columnspan=2, sticky="w", padx=6)
        ttk.Label(files, text="저장 파일").grid(row=2, column=0, sticky="w", **pad)
        dst_entry = ttk.Entry(files, textvariable=self.dst_var)
        dst_entry.grid(row=2, column=1, sticky="ew", **pad)
        dst_btn = ttk.Button(files, text="변경…", command=self._browse_dest)
        dst_btn.grid(row=2, column=2, **pad)

        opts = ttk.LabelFrame(root, text="변환 옵션", padding=8)
        opts.pack(fill="x", pady=(8, 0))
        ttk.Label(opts, text="출력 형식").grid(row=0, column=0, sticky="w", **pad)
        self.format_box = ttk.Combobox(opts, textvariable=self.format_var, state="readonly",
                                       values=list(FORMATS_BY_LABEL), width=16)
        self.format_box.grid(row=0, column=1, sticky="w", **pad)
        self.format_box.bind("<<ComboboxSelected>>", lambda _e: self._on_format_change())
        ttk.Label(opts, text="음질(비트레이트)").grid(row=0, column=2, sticky="w", padx=(18, 6), pady=4)
        self.bitrate_box = ttk.Combobox(opts, textvariable=self.bitrate_var, state="readonly",
                                        values=cv.BITRATES, width=7)
        self.bitrate_box.grid(row=0, column=3, sticky="w", **pad)
        ttk.Label(opts, text="스레드 수").grid(row=0, column=4, sticky="w", padx=(18, 6), pady=4)
        threads_spin = ttk.Spinbox(opts, from_=1, to=CPU_COUNT, textvariable=self.threads_var,
                                   state="readonly", width=4, command=self._update_plan)
        threads_spin.grid(row=0, column=5, sticky="w", **pad)
        ttk.Label(opts, text=f"(CPU {CPU_COUNT}스레드)", foreground="gray").grid(row=0, column=6, sticky="w")
        copy_chk = ttk.Checkbutton(opts, variable=self.copy_var, command=self._update_plan,
                                   text="원본 오디오가 이미 같은 형식이면 재인코딩 없이 복사 (가장 빠르고 음질 손실 없음)")
        copy_chk.grid(row=1, column=0, columnspan=7, sticky="w", **pad)
        text_row = ttk.Frame(opts)
        text_row.grid(row=2, column=0, columnspan=7, sticky="w", **pad)
        text_chk = ttk.Checkbutton(text_row, variable=self.text_var, command=self._on_text_change,
                                   text="텍스트 파일(.txt)도 함께 만들기")
        text_chk.pack(side="left")
        self.model_box = ttk.Combobox(text_row, textvariable=self.model_var, state="readonly",
                                      values=list(MODELS_BY_LABEL), width=6)
        self.model_box.pack(side="left", padx=(8, 0))
        self.model_box.bind("<<ComboboxSelected>>", lambda _e: self._on_text_change())
        ttk.Label(text_row, textvariable=self.model_hint_var, foreground="gray").pack(side="left", padx=(8, 0))
        if self.text_unavailable:
            text_chk.state(["disabled"])
        ttk.Label(opts, textvariable=self.plan_var, foreground="#1a5fb4").grid(
            row=3, column=0, columnspan=7, sticky="w", **pad)

        self.progress = ttk.Progressbar(root, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(12, 0))
        row = ttk.Frame(root)
        row.pack(fill="x", pady=(6, 0))
        ttk.Label(row, textvariable=self.status_var).pack(side="left")
        self.start_btn = ttk.Button(row, text="변환 시작", command=self.start)
        self.start_btn.pack(side="right")
        self.cancel_btn = ttk.Button(row, text="취소", command=self.cancel, state="disabled")
        self.cancel_btn.pack(side="right", padx=6)
        ttk.Button(row, text="폴더 열기", command=self._open_folder).pack(side="right")

        self.log = ScrolledText(root, height=6, state="disabled", wrap="word", font="TkDefaultFont")
        self.log.pack(fill="both", expand=True, pady=(10, 0))

        # 변환 중에는 잠가 둘 위젯들
        self._lockable = [src_entry, src_btn, dst_entry, dst_btn, self.format_box,
                          self.bitrate_box, threads_spin, copy_chk, self.start_btn]
        if not self.text_unavailable:  # 쓸 수 없으면 변환이 끝난 뒤에도 계속 꺼 둔다
            self._lockable.append(text_chk)
        self._update_bitrate_state()
        self._update_text_state()

    def _selected_format(self) -> cv.AudioFormat:
        return FORMATS_BY_LABEL[self.format_var.get()]

    def _selected_model(self) -> tr.SpeechModel:
        return MODELS_BY_LABEL[self.model_var.get()]

    def _update_bitrate_state(self) -> None:
        lossy = self._selected_format().lossy
        self.bitrate_box.state(["!disabled"] if lossy and not self.extractor else ["disabled"])

    def _update_text_state(self) -> None:
        """음성 인식 모델 선택 상자와 안내 문구를 체크 상태와 선택한 모델에 맞춘다."""
        if self.text_unavailable:
            self.model_box.state(["disabled"])
            self.model_hint_var.set(self.text_unavailable)
            return
        self.model_box.state(["!disabled"] if self.text_var.get() and not self.extractor else ["disabled"])
        model = self._selected_model()
        hint = f"1시간 영상 기준 약 {model.minutes_per_hour}분"
        if not model.downloaded():
            hint += f" · 처음 한 번 {model.megabytes:,}MB 내려받음"
        self.model_hint_var.set(hint)

    def _on_text_change(self) -> None:
        self._update_text_state()
        self._update_plan()

    def _update_plan(self) -> None:
        if self.info is None:
            self.plan_var.set("")
            return
        plan = cv.make_plan(self.info, self._selected_format(), self.threads_var.get(), self.copy_var.get())
        text = f"\n이어서 음원의 말소리를 텍스트 파일로 저장 ('{self.model_var.get()}' 모델)" \
            if self._text_enabled() else ""
        self.plan_var.set(f"처리 방식: {plan.note}{text}")

    def _text_enabled(self) -> bool:
        return self.text_var.get() and not self.text_unavailable

    def _set_running(self, running: bool) -> None:
        for widget in self._lockable:
            widget.state(["disabled"] if running else ["!disabled"])
        self.cancel_btn.state(["!disabled"] if running else ["disabled"])
        self._update_bitrate_state()
        self._update_text_state()

    def _log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", f"[{time.strftime('%H:%M:%S')}] {text}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    # ------------------------------------------------------------------ 파일
    def _browse_source(self) -> None:
        current = self.src_var.get().strip().strip('"')
        path = filedialog.askopenfilename(title="영상 파일 선택", filetypes=VIDEO_FILETYPES,
                                          initialdir=os.path.dirname(current) or None)
        if path:
            self.src_var.set(os.path.normpath(path))
            self._load_source()

    def _load_source(self) -> bool:
        """입력한 영상 파일을 분석해 오디오 정보와 기본 저장 경로를 채운다."""
        text = self.src_var.get().strip().strip('"')
        if text == self.loaded_src:
            return self.info is not None
        self.loaded_src = text
        self.info = None
        self.info_var.set("")
        self._update_plan()
        if not text:
            return False
        src = Path(text)
        if not src.is_file():
            self.info_var.set("파일을 찾을 수 없습니다.")
            return False
        try:
            self.info = cv.probe(self.ffmpeg, src)
        except (cv.ConversionError, OSError, subprocess.TimeoutExpired) as exc:
            self.info_var.set(f"사용할 수 없는 파일입니다: {exc}")
            return False
        self.info_var.set(describe_info(self.info))
        self.dst_var.set(str(self._default_dest(src)))
        self._update_plan()
        self.progress["value"] = 0
        self.status_var.set("변환할 준비가 되었습니다.")
        return True

    def _default_dest(self, src: Path) -> Path:
        """기본 저장 경로: 영상과 같은 폴더, 같은 이름에 확장자만 바꾼다."""
        dst = src.with_suffix("." + self._selected_format().ext)
        return dst.with_name(f"{src.stem}_audio{dst.suffix}") if dst == src else dst

    def _browse_dest(self) -> None:
        fmt = self._selected_format()
        current = Path(self.dst_var.get().strip().strip('"') or f"audio.{fmt.ext}")
        path = filedialog.asksaveasfilename(
            title="저장할 파일", initialfile=current.name, defaultextension="." + fmt.ext,
            initialdir=str(current.parent) if current.parent.is_dir() else None,
            filetypes=[(fmt.label, "*." + fmt.ext)], confirmoverwrite=False,
        )
        if path:
            self.dst_var.set(os.path.normpath(path))

    def _on_format_change(self) -> None:
        dst = self.dst_var.get().strip().strip('"')
        if dst and Path(dst).name:
            self.dst_var.set(str(Path(dst).with_suffix("." + self._selected_format().ext)))
        self._update_bitrate_state()
        self._update_plan()

    def _open_folder(self) -> None:
        target = self.last_output if self.last_output and self.last_output.exists() else None
        dst = self.dst_var.get().strip().strip('"')
        folder = target.parent if target else Path(dst).parent if dst else None
        if sys.platform == "win32" and target:
            subprocess.Popen(f'explorer /select,"{target}"')
        elif folder and folder.is_dir():
            if sys.platform == "win32":
                os.startfile(folder)
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(folder)])

    # ------------------------------------------------------------------ 변환
    def start(self) -> None:
        if self.extractor:
            return
        if not self._load_source():
            messagebox.showwarning("변환 시작", self.info_var.get() or "영상 파일을 선택하세요.")
            return
        src = Path(self.loaded_src)
        fmt = self._selected_format()
        dst_text = self.dst_var.get().strip().strip('"')
        dst = Path(dst_text).with_suffix("." + fmt.ext) if dst_text and Path(dst_text).name \
            else self._default_dest(src)
        self.dst_var.set(str(dst))
        if dst.resolve() == src.resolve():
            messagebox.showerror("저장 파일", "원본 영상과 같은 파일에는 저장할 수 없습니다.")
            return
        txt = dst.with_suffix(".txt") if self._text_enabled() else None
        existing = ", ".join(f"'{p.name}'" for p in (dst, txt) if p and p.exists())
        if existing and not messagebox.askyesno("덮어쓰기", f"{existing} 파일이 이미 있습니다.\n덮어쓸까요?"):
            return

        def on_progress(step: str):
            return lambda fraction, stage: self.events.put(("progress", fraction, step + stage))

        on_log = lambda message: self.events.put(("log", message))  # noqa: E731
        self.extractor = cv.Extractor(
            self.ffmpeg, fmt, self.bitrate_var.get(), self.threads_var.get(), self.copy_var.get(),
            on_progress=on_progress("[1/2] " if txt else ""), on_log=on_log,
        )
        self.transcriber = tr.Transcriber(self.ffmpeg, self._selected_model(), self.threads_var.get(),
                                          on_progress("[2/2] "), on_log) if txt else None
        self.started_at = time.monotonic()
        self.fraction, self.stage = 0.0, "준비 중"
        self.progress["value"] = 0
        self._set_running(True)
        quality = self.bitrate_var.get() if fmt.lossy else "무손실"
        target = f"{dst.name} + {txt.name}" if txt else dst.name
        self._log(f"변환 시작: {src.name} → {target} ({fmt.label}, {quality})")
        threading.Thread(target=self._work, args=(self.extractor, self.transcriber, src, dst, txt),
                         daemon=True).start()

    def _work(self, extractor: cv.Extractor, transcriber: tr.Transcriber | None,
              src: Path, dst: Path, txt: Path | None) -> None:
        """백그라운드 스레드에서 실행된다. GUI는 메인 스레드에서만 다뤄야 하므로 큐로 결과를 전달한다."""
        stage = "audio"
        try:
            extractor.extract(src, dst)
            lines = 0
            if transcriber and txt:
                self.events.put(("audio_done", dst))
                stage = "text"
                lines = transcriber.transcribe(dst, txt)
            self.events.put(("done", dst, txt, lines))
        except cv.Cancelled:
            self.events.put(("cancelled", stage))
        except Exception as exc:
            self.events.put(("failed", exc, stage))

    def cancel(self) -> None:
        if self.extractor and not self.extractor.cancelled:
            self.extractor.cancel()
            if self.transcriber:
                self.transcriber.cancel()
            self.cancel_btn.state(["disabled"])
            self.status_var.set("취소하는 중…")

    def _poll_events(self) -> None:
        try:
            while True:
                kind, *args = self.events.get_nowait()
                if kind == "progress":
                    self.fraction, self.stage = args
                    self.progress["value"] = self.fraction * 100
                elif kind == "log":
                    self._log(args[0])
                elif kind == "audio_done":
                    self.audio_done_at = time.monotonic()
                    self.last_output = args[0]
                    self._log(f"음원 저장: {args[0]} ({self._took(self.started_at, self.audio_done_at)})")
                elif kind == "done":
                    self._on_done(*args)
                elif kind == "failed":
                    exc, stage = args
                    if stage == "text":  # 음원은 이미 저장됐다
                        self._finish("음원은 저장했지만 텍스트 변환에 실패했습니다.")
                        self._log(f"[텍스트 변환 실패] {exc}")
                        messagebox.showerror("텍스트 변환 실패", f"음원 파일은 저장되었습니다.\n\n{exc}")
                    else:
                        self._finish("변환에 실패했습니다.")
                        self._log(f"[실패] {exc}")
                        messagebox.showerror("변환 실패", str(exc))
                elif kind == "cancelled":
                    message = "텍스트 변환을 취소했습니다. 음원 파일은 저장되었습니다." if args[0] == "text" \
                        else "변환을 취소했습니다."
                    self._finish(message)
                    self.progress["value"] = 0
                    self._log(message)
        except queue.Empty:
            pass
        if self.extractor and not self.extractor.cancelled:
            elapsed = format_duration(time.monotonic() - self.started_at)
            self.status_var.set(f"{self.stage}…  {self.fraction * 100:.0f}%   경과 {elapsed}")
        self.after(100, self._poll_events)

    def _took(self, start: float, end: float) -> str:
        seconds = end - start
        text = f"{seconds:.1f}초"
        if self.info and self.info.duration and seconds > 0:
            ratio = self.info.duration / seconds
            text += f", 재생 시간 대비 {ratio:.1f}배 속도" if ratio < 10 else f", 재생 시간 대비 {ratio:.0f}배 속도"
        return text

    def _on_done(self, dst: Path, txt: Path | None, lines: int) -> None:
        now = time.monotonic()
        if txt:
            self.last_output = txt
            self._log(f"텍스트 저장: {txt} ({lines}줄, {self._took(self.audio_done_at, now)})")
            if not lines:
                self._log("말소리를 찾지 못해 텍스트 파일이 비어 있습니다.")
        else:
            self.last_output = dst
            self._log(f"완료: {dst} ({self._took(self.started_at, now)})")
        self._finish(f"완료 — 총 {self._took(self.started_at, now)}")
        self.progress["value"] = 100

    def _finish(self, status: str) -> None:
        self.extractor = None
        self.transcriber = None
        self._set_running(False)
        self.status_var.set(status)

    def _on_close(self) -> None:
        if self.extractor:
            if not messagebox.askyesno("종료", "변환이 진행 중입니다.\n중단하고 종료할까요?"):
                return
            self.cancel()
        self.destroy()


def main() -> None:
    if sys.stderr is None:  # pythonw로 실행하면 콘솔이 없어, 라이브러리가 경고를 출력하다 오류가 나지 않도록
        sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")
    if sys.platform == "win32":
        try:  # 고해상도 화면에서 글자가 흐릿하게 보이지 않도록
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    ffmpeg = cv.find_ffmpeg()
    if not ffmpeg:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "ffmpeg를 찾을 수 없음",
            "변환에 필요한 ffmpeg를 찾을 수 없습니다.\n\n"
            "명령 프롬프트에서 아래 명령을 실행한 뒤 다시 시작하세요.\n\n"
            "    pip install imageio-ffmpeg",
        )
        root.destroy()
        return
    App(ffmpeg).mainloop()


if __name__ == "__main__":
    main()
