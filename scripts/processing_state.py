"""音频流水线状态：自动跳过已完成文件，并从最近成功阶段继续。"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


STATE_VERSION = 1
STAGES = {"new", "transcribed", "noted", "completed"}


def load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") == STATE_VERSION and isinstance(data.get("files"), dict):
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError, AttributeError):
        pass
    return {"version": STATE_VERSION, "files": {}}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=".processing_state.", delete=False
        ) as f:
            temp_path = Path(f.name)
            json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()


def signature(audio: Path) -> dict[str, int]:
    stat = audio.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def current_record(state: dict, audio: Path) -> dict | None:
    record = state["files"].get(audio.name)
    if not record or record.get("signature") != signature(audio):
        return None
    return record


def update_record(
    state: dict,
    audio: Path,
    stage: str,
    transcript: Path | None = None,
    note: Path | None = None,
    error: str | None = None,
) -> dict:
    if stage not in STAGES:
        raise ValueError(f"未知处理阶段: {stage}")
    record = current_record(state, audio) or {}
    record.update(
        {
            "signature": signature(audio),
            "stage": stage,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    if transcript is not None:
        record["transcript"] = str(transcript)
    if note is not None:
        record["note"] = str(note)
    if error:
        record["last_error"] = error
    else:
        record.pop("last_error", None)
    state["files"][audio.name] = record
    return record
