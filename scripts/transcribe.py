#!/usr/bin/env python3
"""
步骤1: Whisper 转录 —— 将 mp3 转换为带时间戳的文字稿
支持中文/英文,自动检测语言。用 faster-whisper(GPU加速)。
"""
import ctypes
import logging
import os
import re
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

# Python 3.8+ no longer searches every directory in PATH for dependent DLLs.
# Make a locally installed CUDA Toolkit visible to CTranslate2 on Windows.
_DLL_DIRECTORY_HANDLES = []
if os.name == "nt":
    runtime_dirs = []
    cuda_root = os.environ.get("CUDA_PATH", "")
    if cuda_root:
        runtime_dirs.append(Path(cuda_root) / "bin")

    cudnn_override = os.environ.get("CLASSNOTES_CUDNN_PATH", "")
    if cudnn_override:
        runtime_dirs.append(Path(cudnn_override))

    # Reuse a complete cuDNN 9 runtime from an installed PyTorch environment.
    runtime_dirs.append(Path(sys.prefix) / "Lib" / "site-packages" / "torch" / "lib")
    program_data = os.environ.get("ProgramData", "")
    if program_data:
        for distribution in ("miniconda3", "anaconda3"):
            envs = Path(program_data) / distribution / "envs"
            if envs.is_dir():
                runtime_dirs.extend(envs.glob("*/Lib/site-packages/torch/lib"))

    valid_runtime_dirs = []
    for runtime_dir in runtime_dirs:
        if runtime_dir.is_dir() and runtime_dir not in valid_runtime_dirs:
            valid_runtime_dirs.append(runtime_dir)
            _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(str(runtime_dir)))
    if valid_runtime_dirs:
        os.environ["PATH"] = os.pathsep.join(
            [*(str(path) for path in valid_runtime_dirs), os.environ.get("PATH", "")]
        )

import ctranslate2
from faster_whisper import WhisperModel
import config as app_config

logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
warnings.filterwarnings(
    "ignore", message=r"`huggingface_hub` cache-system uses symlinks.*"
)

WORK = Path(__file__).resolve().parent.parent
MP3 = WORK / "mp3"
TRANSCRIPTS = WORK / "transcripts"
LOGS = WORK / "logs"


def _recognize(model, audio: Path, use_vad: bool):
    language = os.environ.get("CLASSNOTES_LANGUAGE", "").strip().lower()
    if not language:
        language = app_config.get_transcription_language()
    if language not in {"auto", "en", "zh"}:
        raise ValueError("CLASSNOTES_LANGUAGE 只支持 auto/en/zh")
    options = {
        "language": None if language == "auto" else language,
        "vad_filter": use_vad,
        "beam_size": 5,
        "word_timestamps": False,
    }
    if use_vad:
        options["vad_parameters"] = {"min_silence_duration_ms": 600}
    segments, info = model.transcribe(str(audio), **options)
    collected = []
    duration = max(float(getattr(info, "duration", 0) or 0), 1.0)
    next_report = 5
    print("         识别进度: 0%", flush=True)
    for segment in segments:
        collected.append(segment)
        percent = min(99, int(segment.end / duration * 100))
        if percent >= next_report:
            print(f"         识别进度: {percent}%", flush=True)
            next_report = (percent // 5 + 1) * 5
    print("         识别进度: 100%", flush=True)
    return collected, info


def _visible_character_count(segments) -> int:
    return sum(len(re.sub(r"\s+", "", seg.text or "")) for seg in segments)


def _has_sparse_long_segment(segments) -> bool:
    """VAD 偶尔把很长的低电平语音合成一个几乎为空的片段。"""
    for seg in segments:
        duration = max(0.0, seg.end - seg.start)
        characters = len(re.sub(r"[^A-Za-z0-9一-龥]", "", seg.text or ""))
        if duration >= 20 and characters < max(8, int(duration * 0.5)):
            return True
    return False


def _cuda_runtime_available() -> bool:
    """A display driver alone is not enough for CTranslate2 CUDA on Windows."""
    if ctranslate2.get_cuda_device_count() <= 0:
        return False
    if os.name != "nt":
        return True
    for library in ("cublas64_12.dll", "cudnn64_9.dll", "cudnn_ops64_9.dll"):
        try:
            ctypes.WinDLL(library)
        except OSError:
            return False
    return True


def _device_preference() -> str:
    preference = os.environ.get("CLASSNOTES_DEVICE", "").strip().lower()
    if not preference:
        preference = app_config.get_transcription_device()
    if preference not in {"auto", "cuda", "cpu"}:
        raise ValueError("CLASSNOTES_DEVICE 只支持 auto/cuda/cpu")
    return preference


def _load_model(model_name: str):
    preference = _device_preference()

    cuda_available = _cuda_runtime_available()
    if preference == "cuda" and not cuda_available:
        raise RuntimeError("指定了 CUDA，但 CTranslate2 没有检测到可用的 NVIDIA GPU")

    use_cuda = preference == "cuda" or (preference == "auto" and cuda_available)
    if use_cuda:
        try:
            return (
                WhisperModel(model_name, device="cuda", compute_type="int8_float16"),
                "cuda",
                preference,
            )
        except Exception as exc:
            if preference == "cuda":
                raise
            print(f"         GPU 初始化失败，自动改用 CPU: {exc}")

    return WhisperModel(model_name, device="cpu", compute_type="int8"), "cpu", preference


def _transcribe_in_process(
    mp3: Path,
    model_name: str = "small",
    completion_marker: Path | None = None,
) -> Path:
    print(f"[1/转录] 处理: {mp3.name}")
    print("         正在加载语音模型；首次运行会自动下载，请稍候...")
    model, device, preference = _load_model(model_name)
    print(f"         模型: {model_name} ({'GPU' if device == 'cuda' else 'CPU'})")

    try:
        segments, info = _recognize(model, mp3, use_vad=True)
    except Exception as exc:
        if device != "cuda" or preference == "cuda":
            raise
        print(f"         GPU 转写失败，自动改用 CPU 重试: {exc}")
        model = WhisperModel(model_name, device="cpu", compute_type="int8")
        device = "cpu"
        segments, info = _recognize(model, mp3, use_vad=True)
    if _has_sparse_long_segment(segments):
        print("         检测到异常长且稀疏的语音片段，关闭 VAD 自动重试...")
        fallback_segments, fallback_info = _recognize(model, mp3, use_vad=False)
        primary_size = _visible_character_count(segments)
        fallback_size = _visible_character_count(fallback_segments)
        if fallback_size >= max(primary_size + 20, int(primary_size * 1.15)):
            segments, info = fallback_segments, fallback_info
            print("         已采用无 VAD 重试结果")
        else:
            print("         重试结果未明显改善，保留原转写")
    print(f"         检测语言: {info.language} (概率{info.language_probability:.0%})")

    # 输出文件: transcripts/<原名>.txt
    TRANSCRIPTS.mkdir(parents=True, exist_ok=True)
    out = TRANSCRIPTS / (mp3.stem + ".txt")
    lines = []
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=TRANSCRIPTS, prefix=f".{out.name}.", delete=False
        ) as f:
            temp_path = Path(f.name)
            for seg in segments:
                ts = f"[{seg.start:7.1f} - {seg.end:7.1f}]"
                line = f"{ts} {seg.text.strip()}"
                lines.append(line)
                f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        if not lines:
            raise ValueError(f"没有识别出有效语音: {mp3.name}")
        os.replace(temp_path, out)
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()
    print(f"         完成 → {out.name} (共{len(lines)}句)")
    if completion_marker is not None:
        completion_marker.write_text(str(out), encoding="utf-8")
        sys.stdout.flush()
        sys.stderr.flush()
        # Some Windows CUDA/cuDNN combinations crash while destroying the model.
        # This worker owns no pipeline state, so skip native cleanup after success.
        os._exit(0)
    return out


def _transcribe_with_cuda_worker(mp3: Path, model_name: str) -> Path:
    marker_file = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", prefix="openclassnotes-cuda-", suffix=".done", delete=False
    )
    marker = Path(marker_file.name)
    marker_file.close()
    marker.unlink()
    env = os.environ.copy()
    env["CLASSNOTES_DEVICE"] = "cuda"
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--cuda-worker",
        str(mp3.resolve()),
        model_name,
        str(marker),
    ]
    try:
        result = subprocess.run(command, env=env)
        if marker.exists():
            output = Path(marker.read_text(encoding="utf-8").strip())
            if output.exists() and output.stat().st_size:
                return output
        raise RuntimeError(f"GPU 转写子进程失败 (退出码 {result.returncode})")
    finally:
        marker.unlink(missing_ok=True)


def transcribe(mp3: Path, model_name: str = "small") -> Path:
    preference = _device_preference()
    language = os.environ.get("CLASSNOTES_LANGUAGE", "").strip().lower()
    if not language:
        language = app_config.get_transcription_language()
    language_label = {"auto": "自动检测", "en": "English", "zh": "中文"}.get(
        language, language
    )
    print(f"         录音语言: {language_label}")
    if os.name == "nt" and preference != "cpu" and _cuda_runtime_available():
        try:
            return _transcribe_with_cuda_worker(mp3, model_name)
        except Exception as exc:
            if preference == "cuda":
                raise
            print(f"         GPU 子进程失败，自动改用 CPU: {exc}")
            previous = os.environ.get("CLASSNOTES_DEVICE")
            os.environ["CLASSNOTES_DEVICE"] = "cpu"
            try:
                return _transcribe_in_process(mp3, model_name)
            finally:
                if previous is None:
                    os.environ.pop("CLASSNOTES_DEVICE", None)
                else:
                    os.environ["CLASSNOTES_DEVICE"] = previous
    return _transcribe_in_process(mp3, model_name)

if __name__ == "__main__":
    if len(sys.argv) >= 5 and sys.argv[1] == "--cuda-worker":
        _transcribe_in_process(Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4]))
    elif len(sys.argv) < 2:
        print("用法: python transcribe.py <mp3文件名(在mp3/目录里)>")
        sys.exit(1)
    else:
        name = sys.argv[1]
        mp3 = MP3 / name
        if not mp3.exists():
            print(f"错误: {mp3} 不存在。请把录音放进 mp3/ 目录。")
            sys.exit(1)
        transcribe(mp3)
