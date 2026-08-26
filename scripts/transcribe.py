#!/usr/bin/env python3
"""
步骤1: Whisper 转录 —— 将 mp3 转换为带时间戳的文字稿
支持中文/英文,自动检测语言。用 faster-whisper(GPU加速)。
"""
import ctypes
import logging
import os
import re
import sys
import tempfile
import warnings
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

import ctranslate2
from faster_whisper import WhisperModel

logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
warnings.filterwarnings(
    "ignore", message=r"`huggingface_hub` cache-system uses symlinks.*"
)

WORK = Path(__file__).resolve().parent.parent
MP3 = WORK / "mp3"
TRANSCRIPTS = WORK / "transcripts"
LOGS = WORK / "logs"


def _recognize(model, audio: Path, use_vad: bool):
    options = {
        "language": None,
        "vad_filter": use_vad,
        "beam_size": 5,
        "word_timestamps": False,
    }
    if use_vad:
        options["vad_parameters"] = {"min_silence_duration_ms": 600}
    segments, info = model.transcribe(str(audio), **options)
    return list(segments), info


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
    for library in ("cublas64_12.dll", "cudnn64_9.dll"):
        try:
            ctypes.WinDLL(library)
        except OSError:
            return False
    return True


def _load_model(model_name: str):
    preference = os.environ.get("CLASSNOTES_DEVICE", "auto").strip().lower()
    if preference not in {"auto", "cuda", "cpu"}:
        raise ValueError("CLASSNOTES_DEVICE 只支持 auto/cuda/cpu")

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


def transcribe(mp3: Path, model_name: str = "small") -> Path:
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
    return out

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python transcribe.py <mp3文件名(在mp3/目录里)>")
        sys.exit(1)
    name = sys.argv[1]
    mp3 = MP3 / name
    if not mp3.exists():
        print(f"错误: {mp3} 不存在。请把录音放进 mp3/ 目录。")
        sys.exit(1)
    transcribe(mp3)
