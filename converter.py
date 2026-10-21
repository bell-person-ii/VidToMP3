"""영상 파일 1개에서 오디오를 추출하는 핵심 로직 (GUI와 독립적으로 동작).

MP3/AAC 인코더는 CPU 코어를 하나만 쓰므로, 오디오를 여러 구간으로 나눠 코어마다
동시에 인코딩한 뒤 이어 붙여서 변환 시간을 줄인다. 이음새에서 소리가 튀지 않도록
  - 오디오를 먼저 PCM으로 디코딩해 샘플 단위로 정확히 나누고
  - 구간 경계를 코덱 프레임 크기의 배수에 맞추며
  - 각 구간을 앞뒤로 몇 프레임씩 겹쳐 인코딩한 뒤 겹친 프레임을 버리고
  - MP3는 프레임 사이에 데이터를 나눠 담는 bit reservoir를 끈다.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
from collections import deque
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable

VIDEO_EXTENSIONS = (
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v",
    ".mpg", ".mpeg", ".ts", ".mts", ".m2ts", ".3gp", ".ogv", ".vob",
)


@dataclass(frozen=True)
class AudioFormat:
    label: str                 # GUI 표시용 이름
    ext: str                   # 출력 파일 확장자
    muxer: str                 # ffmpeg -f 값
    codec: str                 # ffmpeg -c:a 값
    lossy: bool                # 비트레이트 설정 가능 여부
    copyable: frozenset[str]   # 재인코딩 없이 그대로 복사할 수 있는 원본 코덱
    frame_samples: int = 0     # 0보다 크면 분할 병렬 인코딩 지원 (코덱 프레임 크기)
    chunk_muxer: str = ""      # 분할 인코딩 중간 결과 형식 (프레임 단위로 자를 수 있는 형식)
    preroll_frames: int = 0    # 구간 앞쪽으로 겹쳐 인코딩할 프레임 수 (인코더가 안정될 때까지)


# 프리롤 길이는 측정으로 정했다: 이보다 짧으면 MP3는 차이가 없지만, AAC는 비트 배분이
# 안정되기까지 약 6초가 걸려 이음새 직후 음질이 잠깐 낮아진다.
AUDIO_FORMATS: dict[str, AudioFormat] = {
    f.ext: f for f in (
        AudioFormat("MP3", "mp3", "mp3", "libmp3lame", True, frozenset({"mp3"}), 1152, "mp3", 48),
        AudioFormat("M4A (AAC)", "m4a", "ipod", "aac", True, frozenset({"aac", "alac"}), 1024, "adts", 256),
        AudioFormat("AAC", "aac", "adts", "aac", True, frozenset({"aac"}), 1024, "adts", 256),
        AudioFormat("WAV (무손실)", "wav", "wav", "pcm_s16le", False, frozenset({"pcm_s16le"})),
        AudioFormat("FLAC (무손실)", "flac", "flac", "flac", False, frozenset({"flac"})),
    )
}

BITRATES = ("96k", "128k", "192k", "256k", "320k")

MIN_CHUNK_SECONDS = 30   # 구간이 이보다 짧으면 겹쳐 인코딩하는 부분 때문에 나누는 이득이 적다
POSTROLL_FRAMES = 8      # 구간 뒤쪽으로 겹쳐 인코딩할 프레임 수
DECODE_WEIGHT = 0.2      # 전체 진행률에서 단계별 비중
ENCODE_WEIGHT = 0.75

# 인코더가 지원하는 샘플레이트 (원본이 여기에 없으면 48000Hz로 변환)
_ENCODER_RATES = {
    "libmp3lame": (8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000),
    "aac": (7350, 8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000, 64000, 88200, 96000),
}

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_AUDIO_STREAM_RE = re.compile(r"Stream #\d+:\d+.*?: Audio: (\w+)(?:.*?, (\d+) Hz, ([^,\n]+))?")
_PROGRESS_LINE_RE = re.compile(r"^[a-z0-9_]+=")

# 콘솔 없이(pythonw) 실행할 때 ffmpeg 창이 뜨지 않도록 한다.
_POPEN_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


class ConversionError(Exception):
    pass


class Cancelled(Exception):
    pass


def find_ffmpeg() -> str | None:
    """pip 패키지(imageio-ffmpeg)에 포함된 ffmpeg를 우선 사용하고, 없으면 PATH에서 찾는다."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


# --------------------------------------------------------------------- 분석
@dataclass(frozen=True)
class MediaInfo:
    codec: str              # 첫 번째 오디오 스트림의 코덱
    sample_rate: int        # 0 = 알 수 없음
    channels: int           # 0 = 알 수 없음
    duration: float | None  # 초


def probe(ffmpeg: str, src: Path) -> MediaInfo:
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostdin", "-i", str(src)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL, creationflags=_POPEN_FLAGS, timeout=120,
    )
    info = result.stderr
    if "Input #0" not in info:
        raise ConversionError(_summarize_error(info.splitlines()))
    audio = _AUDIO_STREAM_RE.search(info)
    if not audio:
        raise ConversionError("오디오 트랙이 없는 영상입니다")
    m = _DURATION_RE.search(info)
    duration = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else None
    return MediaInfo(audio[1], int(audio[2] or 0), _parse_channels(audio[3] or ""), duration or None)


def _parse_channels(text: str) -> int:
    text = text.strip()
    if text == "mono":
        return 1
    if text == "stereo":
        return 2
    if m := re.match(r"(\d+) channels", text):
        return int(m[1])
    if m := re.match(r"(\d+)\.(\d+)", text):  # 5.1, 7.1(wide) …
        return int(m[1]) + int(m[2])
    return 0


@dataclass(frozen=True)
class Plan:
    mode: str        # "copy" | "parallel" | "single"
    chunks: int      # 동시에 인코딩할 구간 수
    note: str        # 사용자에게 보여 줄 설명


def make_plan(info: MediaInfo, fmt: AudioFormat, threads: int, copy_when_possible: bool) -> Plan:
    if copy_when_possible and info.codec in fmt.copyable:
        return Plan("copy", 1, f"원본 오디오({info.codec})를 그대로 복사 — 재인코딩 없음, 가장 빠름")
    if not fmt.frame_samples:
        return Plan("single", 1, f"{fmt.label} 형식은 분할 인코딩을 지원하지 않아 단일 작업으로 인코딩")
    if fmt.codec != "libmp3lame" and info.channels not in (1, 2):
        return Plan("single", 1, "다채널 오디오라서 단일 작업으로 인코딩")
    chunks = min(threads, int((info.duration or 0) // MIN_CHUNK_SECONDS))
    if chunks < 2:
        reason = "스레드 수가 1개라서" if threads < 2 else "영상이 짧아서"
        return Plan("single", 1, f"{reason} 단일 작업으로 인코딩")
    return Plan("parallel", chunks, f"{chunks}개 구간으로 나눠 {chunks}개 스레드가 동시에 인코딩")


# --------------------------------------------------------------------- 변환
ProgressCallback = Callable[[float, str], None]
LogCallback = Callable[[str], None]


class Extractor:
    def __init__(self, ffmpeg: str, fmt: AudioFormat, bitrate: str, threads: int,
                 copy_when_possible: bool, on_progress: ProgressCallback, on_log: LogCallback):
        self.ffmpeg = ffmpeg
        self.fmt = fmt
        self.bitrate = bitrate
        self.threads = max(1, threads)
        self.copy_when_possible = copy_when_possible
        self.on_progress = on_progress
        self.on_log = on_log
        self._cancel = threading.Event()   # 사용자가 취소함
        self._abort = threading.Event()    # 구간 하나가 실패해 나머지 구간을 멈춤
        self._procs: set[subprocess.Popen] = set()
        self._lock = threading.Lock()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def cancel(self) -> None:
        with self._lock:
            self._cancel.set()
        self._kill_all()

    def _kill_all(self) -> None:
        with self._lock:
            procs = list(self._procs)
        for proc in procs:
            try:
                proc.kill()
            except OSError:
                pass

    def _report(self, fraction: float, stage: str) -> None:
        self.on_progress(max(0.0, min(fraction, 0.99)), stage)

    def _codec_args(self) -> list[str]:
        args = ["-c:a", self.fmt.codec]
        if self.fmt.lossy:
            args += ["-b:a", self.bitrate]
        return args

    def extract(self, src: Path, dst: Path) -> Plan:
        info = probe(self.ffmpeg, src)
        plan = make_plan(info, self.fmt, self.threads, self.copy_when_possible)
        self.on_log(f"처리 방식: {plan.note}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".part")
        try:
            if plan.mode == "parallel":
                try:
                    self._parallel(src, tmp, info, plan.chunks)
                except ConversionError as exc:
                    self.on_log(f"분할 인코딩에 실패해 단일 작업으로 다시 시도합니다. ({exc})")
                    plan = Plan("single", 1, "단일 작업으로 인코딩 (분할 인코딩 실패)")
                    self._single(src, tmp, info, copy=False)
            else:
                self._single(src, tmp, info, copy=plan.mode == "copy")
            os.replace(tmp, dst)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        self.on_progress(1.0, "완료")
        return plan

    def _single(self, src: Path, tmp: Path, info: MediaInfo, copy: bool) -> None:
        stage = "원본 오디오 복사 중" if copy else "인코딩 중"
        codec = ["-c:a", "copy"] if copy else self._codec_args()
        self._run(["-i", str(src), "-map", "0:a:0", *codec, "-f", self.fmt.muxer, str(tmp)],
                  on_time=lambda t: info.duration and self._report(t / info.duration, stage))

    def _parallel(self, src: Path, tmp: Path, info: MediaInfo, chunks: int) -> None:
        fmt = self.fmt
        rate = info.sample_rate if info.sample_rate in _ENCODER_RATES[fmt.codec] else 48000
        channels = 1 if info.channels == 1 else 2
        frame = 576 if fmt.codec == "libmp3lame" and rate < 32000 else fmt.frame_samples
        sample_bytes = 2 * channels  # s16le

        with tempfile.TemporaryDirectory(prefix="vidtoaudio_") as work_dir:
            work = Path(work_dir)

            # 1단계: 오디오를 한 번만 디코딩해 PCM으로 저장 (구간을 샘플 단위로 정확히 나누기 위해)
            pcm = work / "audio.pcm"
            self._run(["-i", str(src), "-map", "0:a:0", "-ac", str(channels), "-ar", str(rate),
                       "-f", "s16le", str(pcm)],
                      on_time=lambda t: info.duration and self._report(
                          DECODE_WEIGHT * t / info.duration, "오디오 디코딩 중"))
            total = pcm.stat().st_size // sample_bytes

            # 2단계: 프레임 경계에 맞춰 구간을 나누고 구간마다 별도 ffmpeg로 동시에 인코딩
            frames = total // frame
            bounds = [i * frames // chunks * frame for i in range(chunks)] + [total]
            starts = [max(b - fmt.preroll_frames * frame, 0) for b in bounds[:-1]]  # 프리롤 포함 시작점
            ends = [min(b + POSTROLL_FRAMES * frame, total) for b in bounds[1:-1]] + [total]  # 포스트롤 포함 끝점
            # 진행률은 겹쳐 인코딩하는 부분까지 포함한 길이로 나눠야 합치는 단계에서 뒤로 가지 않는다
            encode_seconds = sum(e - s for s, e in zip(starts, ends)) / rate
            processed = [0.0] * chunks
            stage = f"인코딩 중 ({chunks}개 스레드)"

            def on_chunk_time(i: int, seconds: float) -> None:
                processed[i] = seconds
                done = min(sum(processed) / encode_seconds, 1.0)
                self._report(DECODE_WEIGHT + ENCODE_WEIGHT * done, stage)

            def encode(i: int) -> Path:
                start, end = starts[i], ends[i]
                out = work / f"chunk{i}.{fmt.chunk_muxer}"
                extra = ["-reservoir", "0", "-write_xing", "0", "-id3v2_version", "0"] \
                    if fmt.chunk_muxer == "mp3" else []

                def feed(pipe: BinaryIO) -> None:
                    with open(pcm, "rb") as f:
                        f.seek(start * sample_bytes)
                        remaining = (end - start) * sample_bytes
                        while remaining > 0 and (block := f.read(min(remaining, 1 << 20))):
                            pipe.write(block)
                            remaining -= len(block)

                self._run(["-f", "s16le", "-ar", str(rate), "-ac", str(channels), "-i", "pipe:0",
                           *self._codec_args(), *extra, "-f", fmt.chunk_muxer, str(out)],
                          on_time=lambda t: on_chunk_time(i, t), feed=feed)
                return out

            with ThreadPoolExecutor(max_workers=chunks, thread_name_prefix="encode") as pool:
                futures = [pool.submit(encode, i) for i in range(chunks)]
                done, _ = wait(futures, return_when=FIRST_EXCEPTION)
                failed = next((f.exception() for f in done if f.exception()), None)
                if failed:  # 나머지 구간도 즉시 멈춘다
                    self._abort.set()
                    self._kill_all()
            self._abort.clear()
            if self.cancelled:
                raise Cancelled
            if failed:
                raise failed
            pcm.unlink()

            # 3단계: 겹쳐 인코딩한 앞뒤 프레임을 버리고 구간들을 이어 붙인다
            self._report(DECODE_WEIGHT + ENCODE_WEIGHT, "구간 합치는 중")
            joined = work / f"joined.{fmt.chunk_muxer}"
            frame_offsets = _mp3_frame_offsets if fmt.chunk_muxer == "mp3" else _adts_frame_offsets
            with open(joined, "wb") as out:
                for i, future in enumerate(futures):
                    data = future.result().read_bytes()
                    offsets = frame_offsets(data)
                    first = (bounds[i] - starts[i]) // frame
                    last = first + (bounds[i + 1] - bounds[i]) // frame if i < chunks - 1 else len(offsets) - 1
                    if last >= len(offsets):
                        raise ConversionError(f"{i + 1}번째 구간의 인코딩 결과가 예상보다 짧습니다")
                    out.write(data[offsets[first]:offsets[last]])

            # 4단계: 최종 형식으로 감싸고(MP3 길이 정보 헤더, M4A 컨테이너) 원본의 제목 등 메타데이터를 옮긴다
            self._run(["-i", str(joined), "-i", str(src), "-map", "0:a", "-map_metadata", "1",
                       "-c", "copy", "-f", fmt.muxer, str(tmp)])

    def _run(self, args: list[str], on_time: Callable[[float], object] | None = None,
             feed: Callable[[BinaryIO], None] | None = None) -> None:
        """ffmpeg를 실행하고, 처리한 시간(초)을 -progress 출력에서 읽어 on_time으로 알린다."""
        cmd = [self.ffmpeg, "-hide_banner", "-nostats", "-progress", "pipe:1", "-y", *args]
        tail: deque[str] = deque(maxlen=15)
        with self._lock:
            if self._cancel.is_set() or self._abort.is_set():
                raise Cancelled
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE if feed else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=_POPEN_FLAGS,
            )
            self._procs.add(proc)
        feeder = None
        if feed:
            feeder = threading.Thread(target=_feed_stdin, args=(proc, feed), daemon=True)
            feeder.start()
        try:
            for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if not _PROGRESS_LINE_RE.match(line):
                    if line:
                        tail.append(line)
                    continue
                key, _, value = line.partition("=")
                if key == "out_time_us" and value.isdigit() and on_time:
                    on_time(int(value) / 1e6)
            proc.wait()
            if feeder:
                feeder.join()
        finally:
            with self._lock:
                self._procs.discard(proc)
        if self._cancel.is_set() or self._abort.is_set():
            raise Cancelled
        if proc.returncode != 0:
            raise ConversionError(_summarize_error(tail))


def _feed_stdin(proc: subprocess.Popen, feed: Callable[[BinaryIO], None]) -> None:
    try:
        feed(proc.stdin)
    except OSError:  # ffmpeg가 먼저 끝난 경우 (취소 또는 오류) — 오류 내용은 ffmpeg 출력으로 보고된다
        pass
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass


# ------------------------------------------------------------ 프레임 해석
_MP3_BITRATES = {
    3: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),  # MPEG-1 Layer III
    2: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),     # MPEG-2/2.5 Layer III
}
_MP3_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


def _mp3_frame_offsets(data: bytes) -> list[int]:
    """MP3 프레임들의 시작 위치 목록(+ 끝 위치)을 돌려준다."""
    offsets, pos = [], 0
    while pos + 4 <= len(data):
        b1, b2 = data[pos + 1], data[pos + 2]
        version = (b1 >> 3) & 3
        try:
            if data[pos] != 0xFF or (b1 & 0xE0) != 0xE0 or (b1 >> 1) & 3 != 1:
                raise LookupError
            bitrate = _MP3_BITRATES[3 if version == 3 else 2][b2 >> 4] * 1000
            rate = _MP3_RATES[version][(b2 >> 2) & 3]
        except LookupError:
            raise ConversionError("MP3 프레임을 해석할 수 없습니다") from None
        size = (144 if version == 3 else 72) * bitrate // rate + ((b2 >> 1) & 1)
        if size <= 4:
            raise ConversionError("MP3 프레임을 해석할 수 없습니다")
        offsets.append(pos)
        pos += size
    if pos != len(data):
        raise ConversionError("MP3 데이터가 중간에 잘렸습니다")
    return offsets + [pos]


def _adts_frame_offsets(data: bytes) -> list[int]:
    """ADTS(AAC) 프레임들의 시작 위치 목록(+ 끝 위치)을 돌려준다."""
    offsets, pos = [], 0
    while pos + 7 <= len(data):
        size = ((data[pos + 3] & 3) << 11) | (data[pos + 4] << 3) | (data[pos + 5] >> 5)
        # 동기 신호 확인, 그리고 프레임마다 오디오 블록이 1개여야 프레임 단위로 자를 수 있다
        if data[pos] != 0xFF or (data[pos + 1] & 0xF6) != 0xF0 or size < 7 or data[pos + 6] & 3:
            raise ConversionError("AAC 프레임을 해석할 수 없습니다")
        offsets.append(pos)
        pos += size
    if pos != len(data):
        raise ConversionError("AAC 데이터가 중간에 잘렸습니다")
    return offsets + [pos]


def _summarize_error(lines) -> str:
    lines = [ln.strip() for ln in lines if ln.strip()]
    text = "\n".join(lines)
    if "Invalid data found" in text:
        return "손상되었거나 지원하지 않는 파일입니다"
    if "Permission denied" in text:
        return "파일에 접근할 수 없습니다 (권한 또는 다른 프로그램에서 사용 중)"
    if "No space left" in text:
        return "디스크 공간이 부족합니다"
    return lines[-1] if lines else "알 수 없는 오류"
