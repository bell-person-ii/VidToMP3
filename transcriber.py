"""음원 파일의 말소리를 텍스트 파일로 바꾸는 음성 인식 (GUI와 독립적으로 동작).

Whisper 모델(faster-whisper)로 인터넷 없이 PC에서 처리한다. 모델 파일은 처음 한 번만
내려받아 프로그램 폴더의 models 폴더에 저장한다.
"""
from __future__ import annotations

import http.client
import importlib.util
import os
import subprocess
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from converter import _POPEN_FLAGS, Cancelled, ConversionError, LogCallback, ProgressCallback, _summarize_error


@dataclass(frozen=True)
class SpeechModel:
    label: str             # GUI 표시용 이름
    name: str              # models 폴더 안의 폴더 이름
    repo: str              # Hugging Face 저장소
    revision: str          # 커밋을 고정해 어느 PC에서든 같은 파일을 받는다
    files: dict[str, int]  # 파일 이름 → 크기(바이트)
    batch_size: int        # 한 번에 인식할 말소리 구간 수 (진행률·취소는 이 단위로 반영된다)
    minutes_per_hour: int  # 1시간 음원을 인식하는 데 걸리는 시간(분), 4코어 8스레드 노트북에서 측정

    @property
    def folder(self) -> Path:
        return Path(__file__).resolve().parent / "models" / self.name

    @property
    def megabytes(self) -> int:
        return round(sum(self.files.values()) / 1e6)

    def downloaded(self) -> bool:
        return all((self.folder / n).is_file() and (self.folder / n).stat().st_size == size
                   for n, size in self.files.items())


# 묶음 크기는 10분 음원으로 측정해 정했다. small은 8개씩 묶을 때 가장 빠르다(4개보다 20% 빠르고,
# 16개는 오히려 느리다). turbo는 묶어도 빨라지지 않고 결과가 늦게 나와 취소·진행률 반응만 느려진다.
MODELS = (
    SpeechModel("빠름", "small", "Systran/faster-whisper-small", "536b0662742c02347bc0e980a01041f333bce120",
                {"config.json": 2370, "model.bin": 483546902, "tokenizer.json": 2203239,
                 "vocabulary.txt": 459861},
                batch_size=8, minutes_per_hour=13),
    SpeechModel("정확", "large-v3-turbo", "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
                "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf",
                {"config.json": 2263, "model.bin": 1617884929, "preprocessor_config.json": 340,
                 "tokenizer.json": 2710337, "vocabulary.json": 1068114},
                batch_size=1, minutes_per_hour=33),
)

SAMPLE_RATE = 16000  # Whisper 입력 형식
_REQUIRED_MODULES = ("faster_whisper", "ctranslate2", "onnxruntime", "numpy")


def unavailable_reason() -> str | None:
    """텍스트 변환을 쓸 수 없으면 그 이유를 돌려준다. (모듈을 실제로 불러오지 않아 빠르다)"""
    if any(importlib.util.find_spec(m) is None for m in _REQUIRED_MODULES):
        return "텍스트 변환 구성 요소가 설치되지 않았습니다 (README의 '텍스트 변환' 참고)"
    return None


class Transcriber:
    def __init__(self, ffmpeg: str, model: SpeechModel, threads: int,
                 on_progress: ProgressCallback, on_log: LogCallback):
        self.ffmpeg = ffmpeg
        self.model = model
        self.threads = max(1, threads)
        self.on_progress = on_progress
        self.on_log = on_log
        self._cancel = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def cancel(self) -> None:
        with self._lock:
            self._cancel.set()
            proc = self._proc
        if proc:
            try:
                proc.kill()
            except OSError:
                pass

    def _check(self) -> None:
        if self._cancel.is_set():
            raise Cancelled

    def transcribe(self, audio: Path, dst: Path) -> int:
        """audio의 말소리를 dst 텍스트 파일로 저장하고, 저장한 줄 수를 돌려준다."""
        self._download_model()
        self.on_progress(0.0, "음성 인식 모델 불러오는 중")
        # 불러오는 데 몇 초 걸려서 필요할 때만 불러온다
        from faster_whisper import BatchedInferencePipeline, WhisperModel
        try:
            model = WhisperModel(str(self.model.folder), device="cpu", compute_type="int8",
                                 cpu_threads=self.threads)
        except Exception as exc:
            raise ConversionError(f"음성 인식 모델을 불러오지 못했습니다. "
                                  f"models\\{self.model.name} 폴더를 지운 뒤 다시 시도하세요. ({exc})")
        samples = self._decode(audio)
        duration = len(samples) / SAMPLE_RATE
        self._check()
        self.on_progress(0.0, "말소리 구간 찾는 중")
        segments, info = BatchedInferencePipeline(model).transcribe(samples, batch_size=self.model.batch_size)
        self.on_log(f"인식 모델: {self.model.label}({self.model.name}), "
                    f"인식한 언어: {info.language} (확신도 {info.language_probability:.0%})")
        self.on_progress(0.0, "텍스트 변환 중")  # 첫 묶음의 결과가 나오기까지 수십 초 걸린다

        tmp = dst.with_name(dst.name + ".part")
        lines = 0
        try:
            # BOM을 붙여 한글(HWP) 등 오래된 프로그램에서도 UTF-8로 제대로 열리게 한다
            with open(tmp, "w", encoding="utf-8-sig") as f:
                for segment in segments:
                    self._check()
                    if text := segment.text.strip():
                        f.write(text + "\n")
                        lines += 1
                    if duration:
                        self.on_progress(min(segment.end / duration, 0.99), "텍스트 변환 중")
            self._check()
            os.replace(tmp, dst)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        self.on_progress(1.0, "완료")
        return lines

    def _decode(self, audio: Path):
        """Whisper 입력 형식으로 디코딩한다. faster-whisper 내장 디코더(PyAV)는 PyAV 19 이상에서
        동작하지 않으므로 변환에 쓰는 ffmpeg로 직접 디코딩한다."""
        import numpy as np
        self.on_progress(0.0, "음원 읽는 중")
        with self._lock:
            self._check()
            self._proc = subprocess.Popen(
                [self.ffmpeg, "-hide_banner", "-nostdin", "-i", str(audio), "-map", "0:a:0",
                 "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "pipe:1"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=_POPEN_FLAGS,
            )
        try:
            raw, err = self._proc.communicate()
        finally:
            with self._lock:
                proc, self._proc = self._proc, None
        self._check()
        if proc.returncode != 0:
            raise ConversionError(_summarize_error(err.decode("utf-8", "replace").splitlines()[-15:]))
        return np.frombuffer(raw, np.float32)

    def _download_model(self) -> None:
        """모델 파일을 내려받는다. 이미 받은 파일은 건너뛰고, 중간에 멈춘 파일은 이어서 받는다."""
        model = self.model
        folder = model.folder
        missing = {name: size for name, size in model.files.items()
                   if not ((folder / name).is_file() and (folder / name).stat().st_size == size)}
        if not missing:
            return
        folder.mkdir(parents=True, exist_ok=True)
        self.on_log(f"음성 인식 모델({model.label})을 내려받습니다. 처음 한 번만 받으며 크기는 {model.megabytes}MB입니다.")
        total = sum(model.files.values())
        done = total - sum(missing.values())
        for name, size in missing.items():
            path = folder / name
            part = path.with_name(name + ".part")
            have = part.stat().st_size if part.exists() else 0
            if have > size:
                part.unlink()
                have = 0
            if have < size:
                url = f"https://huggingface.co/{model.repo}/resolve/{model.revision}/{name}"
                request = urllib.request.Request(url, headers={"Range": f"bytes={have}-"} if have else {})
                try:
                    with urllib.request.urlopen(request, timeout=30) as response:
                        if response.status != 206:  # 서버가 이어받기를 지원하지 않으면 처음부터
                            have = 0
                        with open(part, "ab" if have else "wb") as f:
                            while block := response.read(1 << 20):
                                self._check()
                                f.write(block)
                                have += len(block)
                                self.on_progress((done + have) / total,
                                                 f"음성 인식 모델 내려받는 중 ({(done + have) // 1_000_000}"
                                                 f"/{total // 1_000_000}MB)")
                except (urllib.error.URLError, http.client.HTTPException, ConnectionError, TimeoutError) as exc:
                    raise ConversionError(f"음성 인식 모델을 내려받지 못했습니다. 인터넷 연결을 확인하세요. ({exc})")
            if part.stat().st_size != size:
                part.unlink()
                raise ConversionError("음성 인식 모델을 내려받는 중 파일이 손상되었습니다. 다시 시도하세요.")
            os.replace(part, path)
            done += size
