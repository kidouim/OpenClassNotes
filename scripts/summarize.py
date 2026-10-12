#!/usr/bin/env python3
"""
步骤2: LLM 清洗术语并提炼重点 —— 同一次调用返回局部纠错补丁和结构化笔记
不依赖任何代理 / agent, 直接调用户自己配置的 LLM。
输出: 清洗后的 transcripts/<原名>.txt、notes/<原名>.md
"""
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

# 让 scripts/ 下的模块(如 llm_client / config)可被 import
sys.path.insert(0, str(Path(__file__).resolve().parent))
import llm_client

WORK = Path(__file__).resolve().parent.parent
TRANSCRIPTS = WORK / "transcripts"
NOTES = WORK / "notes"
RAW_TRANSCRIPTS = Path.home() / ".class-notes" / "raw_transcripts"

CORRECTIONS_OPEN = "<OPENCLASSNOTES_CORRECTIONS>"
CORRECTIONS_CLOSE = "</OPENCLASSNOTES_CORRECTIONS>"
NOTE_OPEN = "<OPENCLASSNOTES_NOTE>"
NOTE_CLOSE = "</OPENCLASSNOTES_NOTE>"
MIN_CORRECTION_CONFIDENCE = 0.95
ALLOWED_CORRECTION_TYPES = {
    "technical_term",
    "homophone",
    "asr_fragment",
    "punctuation",
}

SYSTEM_PROMPT = """你是一名学生, 正在把自己的课堂录音转写稿整理成课后复习笔记。

任务目标:
把口语化的转写稿, 整理成一份【干净、结构化、便于复习】的文本笔记。

关键取舍(务必遵守):
- 只保留"语言信息"(概念、定义、逻辑、例子、老师强调的话)。
- 公式: 不记录表达式和推导；保留公式名称、老师口述的用途、适用条件、易错点和考试强调。
  若老师只报出名字，则只写名字，不使用模型自身知识补充解释。
- 识别并突出这些"考试重要信号":
  · 考试范围/占比(如"这个占30%"、必考)
  · 重点强调(如"重点"、"记一下"、"这是考点")
  · 考试结构(如"考试分三部分")
- 不主动臆造老师没说的知识。
- 不展开转写稿里没有解释的缩写, 不补充外部背景, 不换算单位。
- 可纠正上下文唯一、置信度至少 0.95 的语音识别错误，例如专业术语同音字、
  近音词、明显断裂片段和标点；这属于转写纠错，不属于补充知识。
- 每一处纠错都必须列入 corrections，from 必须是对应 Lxxxxx 行中的逐字子串。
- 不得纠正数字、比例、单位、肯定/否定关系或老师表达的确定程度。
- 不得自由改写整句；无法唯一判断的内容不列入 corrections，并在笔记中标注 [unclear]。
- 笔记只能使用 corrections 中列出的纠正结果，不得静默纠正其他内容。

输出要求(简洁干净, 不啰嗦):
- 用与原课程相同的语言。
- Markdown, 结构: 一个大标题, 若干 ## 大节, 之下用要点列表。
- 若课程提到公式、定理或变换，单列 "## 公式名称"（英文用 "## Formula Names"）；
  每项只写老师实际说出的名称，不写表达式、推导，也不凭模型知识补全。
- 开头用 2-3 行 "TL;DR" 概括全课; 结尾列出考试信号并标注【占比/必考/背景】等。
  中文笔记使用标题 "## 注意"，英文笔记使用标题 "## Notes"。
- 关键数字、考试信号和重要结论后保留转写稿中的时间戳, 例如 [12.3 - 18.6]。
- 标注不确定内容时只写 [unclear]，不要追加 "likely/probably/可能是" 之类猜测。
- 输入转写的每一行以 Lxxxxx 标识；该标识只用于 corrections，不得出现在笔记中。

输出协议（严格遵守，不加代码围栏或解释）：
<OPENCLASSNOTES_CORRECTIONS>
[{"line": 1, "from": "付里叶变换", "to": "傅里叶变换", "type": "homophone", "confidence": 0.99}]
</OPENCLASSNOTES_CORRECTIONS>
<OPENCLASSNOTES_NOTE>
这里放 Markdown 笔记；没有纠错时 corrections 必须为 []。
</OPENCLASSNOTES_NOTE>

corrections 只列高置信度、局部、可直接替换的修改。"""


def _numbered_lines(raw: str) -> list[str]:
    return [f"L{index:05d} | {line}" for index, line in enumerate(raw.splitlines(), 1)]


def _chunk_numbered_transcript(raw: str, limit: int = 12000) -> list[str]:
    """Split on transcript lines while preserving globally stable line IDs."""
    numbered = _numbered_lines(raw)
    if not numbered:
        return []
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in numbered:
        addition = len(line) + (1 if current else 0)
        if current and size + addition > limit:
            chunks.append("\n".join(current))
            current = []
            size = 0
        current.append(line)
        size += len(line) + (1 if len(current) > 1 else 0)
    if current:
        chunks.append("\n".join(current))
    return chunks


def _parse_model_response(response: str) -> tuple[str, list[dict], bool]:
    """Parse the correction envelope; accept legacy plain Markdown safely."""
    text = response.strip()
    markers = (CORRECTIONS_OPEN, CORRECTIONS_CLOSE, NOTE_OPEN, NOTE_CLOSE)
    if any(marker in text for marker in markers):
        fenced = re.fullmatch(r"```(?:text|markdown)?\s*\n(.*)\n```", text, re.DOTALL)
        if fenced:
            text = fenced.group(1).strip()
    has_marker = any(marker in text for marker in markers)
    pattern = re.compile(
        rf"^\s*{re.escape(CORRECTIONS_OPEN)}\s*(.*?)\s*"
        rf"{re.escape(CORRECTIONS_CLOSE)}\s*"
        rf"{re.escape(NOTE_OPEN)}\s*(.*?)\s*"
        rf"{re.escape(NOTE_CLOSE)}\s*$",
        re.DOTALL,
    )
    match = pattern.fullmatch(text)
    if not match:
        if has_marker:
            raise ValueError("模型返回的清洗协议不完整，未改写转写稿或笔记")
        if not text:
            raise ValueError("模型返回了空笔记")
        return text, [], False

    corrections_text = match.group(1).strip()
    note = match.group(2).strip()
    if not note:
        raise ValueError("模型返回的 Markdown 笔记为空")
    if corrections_text.startswith("```"):
        corrections_text = re.sub(r"^```(?:json)?\s*", "", corrections_text)
        corrections_text = re.sub(r"\s*```$", "", corrections_text)
    try:
        corrections = json.loads(corrections_text)
    except json.JSONDecodeError:
        # The note body is still usable. Ignoring malformed corrections is safe
        # because it guarantees that no unvalidated transcript edit is applied.
        return note, [], False
    if not isinstance(corrections, list) or not all(
        isinstance(item, dict) for item in corrections
    ):
        return note, [], False
    return note, corrections, True


def _numeric_tokens(text: str) -> list[str]:
    return re.findall(r"\d+(?:\.\d+)?%?", text)


def _negation_signature(text: str) -> tuple[int, ...]:
    lowered = text.lower()
    chinese = tuple(lowered.count(token) for token in ("不", "没", "无", "未", "非"))
    english = tuple(
        len(re.findall(rf"\b{token}\b", lowered))
        for token in ("no", "not", "never", "without")
    )
    return chinese + english


def _validated_edits(raw: str, proposals: list[dict]) -> tuple[list[str], list[dict], int]:
    """Apply only exact, non-overlapping, high-confidence local replacements."""
    lines = raw.splitlines()
    edits_by_line: dict[int, list[tuple[int, int, str, dict]]] = {}
    accepted: list[dict] = []
    rejected = 0
    seen: set[tuple[int, str, str]] = set()

    for proposal in proposals:
        line_number = proposal.get("line")
        old = proposal.get("from")
        new = proposal.get("to")
        kind = proposal.get("type")
        confidence = proposal.get("confidence")
        valid_scalar = (
            isinstance(line_number, int)
            and not isinstance(line_number, bool)
            and isinstance(old, str)
            and isinstance(new, str)
            and isinstance(kind, str)
            and isinstance(confidence, (int, float))
            and not isinstance(confidence, bool)
        )
        if not valid_scalar:
            rejected += 1
            continue
        key = (line_number, old, new)
        if key in seen:
            continue
        seen.add(key)
        if (
            not 1 <= line_number <= len(lines)
            or kind not in ALLOWED_CORRECTION_TYPES
            or not MIN_CORRECTION_CONFIDENCE <= float(confidence) <= 1.0
            or not old
            or not new
            or old == new
            or "\n" in old
            or "\r" in old
            or "\n" in new
            or "\r" in new
            or len(old) > 120
            or len(new) > 120
            or len(new) > len(old) * 2 + 8
            or _numeric_tokens(old) != _numeric_tokens(new)
            or _negation_signature(old) != _negation_signature(new)
        ):
            rejected += 1
            continue
        line = lines[line_number - 1]
        if line.count(old) != 1:
            rejected += 1
            continue
        start = line.index(old)
        end = start + len(old)
        if any(
            start < existing_end and end > existing_start
            for existing_start, existing_end, _, _ in edits_by_line.get(line_number, [])
        ):
            rejected += 1
            continue
        normalized = {
            "line": line_number,
            "from": old,
            "to": new,
            "type": kind,
            "confidence": float(confidence),
        }
        edits_by_line.setdefault(line_number, []).append((start, end, new, normalized))
        accepted.append(normalized)

    for line_number, edits in edits_by_line.items():
        line = lines[line_number - 1]
        for start, end, replacement, _ in sorted(edits, reverse=True):
            line = line[:start] + replacement + line[end:]
        lines[line_number - 1] = line
    suffix = "\n" if raw.endswith(("\n", "\r")) else ""
    return "\n".join(lines) + suffix, accepted, rejected


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()


def _backup_raw_transcript(transcript: Path, raw: str) -> Path:
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    backup = RAW_TRANSCRIPTS / f"{transcript.stem}.{digest}.txt"
    if not backup.exists():
        _atomic_write(backup, raw)
    return backup

def call_llm(text: str, retry_hint: str = "") -> str:
    """调用 LLM(走 llm_client, 读本地配置的 key)。"""
    latin = len(re.findall(r"[A-Za-z]", text))
    latin_words = len(re.findall(r"\b[A-Za-z]{2,}\b", text))
    cjk = len(re.findall(r"[一-龥]", text))
    language = "English" if latin_words >= 3 and latin > cjk * 2 else "中文"
    return llm_client.generate(
        SYSTEM_PROMPT,
        f"转写稿主要语言为 {language}，最终笔记必须使用 {language}。"
        f"{retry_hint}"
        f"以下是一节课的带全局行号转写稿，请纠正明确的 ASR 错误并整理成笔记:\n\n{text}",
        temperature=0.2,
        max_tokens=4000,
    )

def summarize(transcript: Path) -> Path:
    print(f"[2/清洗+提炼] 读取: {transcript.name}")
    with open(transcript, encoding="utf-8") as f:
        raw = f.read()
    if not raw.strip():
        raise ValueError(f"转写稿为空，未生成笔记: {transcript.name}")
    print(f"         待清洗转写: {len(raw)} 字符")
    print("         调用已配置模型清洗术语并提炼重点...")

    # 按完整转写行分块，保留全局 Lxxxxx 行号供纠错补丁精确定位。
    parts = _chunk_numbered_transcript(raw)

    sections = []
    correction_proposals: list[dict] = []
    used_protocol = True
    for i, part in enumerate(parts):
        print(f"         处理第 {i+1}/{len(parts)} 段...")
        label = f"第{i+1}部分" if len(parts) > 1 else ""
        chunk_text = f"{label}\n{part}" if label else part
        parse_error = None
        for attempt in range(3):
            retry_hint = ""
            if attempt:
                retry_hint = (
                    "上次输出格式不完整。必须从 <OPENCLASSNOTES_CORRECTIONS> 开始，"
                    "并以 </OPENCLASSNOTES_NOTE> 完整结束；不要输出协议之外的内容。"
                )
                print(f"         第 {i+1} 段格式不完整，重试 {attempt+1}/3...")
            response = call_llm(chunk_text, retry_hint=retry_hint)
            try:
                note_section, proposals, structured = _parse_model_response(response)
                break
            except ValueError as exc:
                parse_error = exc
        else:
            raise parse_error
        sections.append(note_section)
        correction_proposals.extend(proposals)
        used_protocol = used_protocol and structured

    note = "\n\n" + "\n\n---\n\n".join(sections)
    cleaned, accepted, rejected = _validated_edits(raw, correction_proposals)
    out = NOTES / (transcript.stem + ".md")
    if accepted:
        _backup_raw_transcript(transcript, raw)
    _atomic_write(out, note)
    if accepted:
        _atomic_write(transcript, cleaned)
        print(f"         自动纠正 {len(accepted)} 处 ASR 文本；原稿已隐藏备份")
    elif not used_protocol:
        print("         模型未返回纠错清单，已兼容保留原转写")
    else:
        print("         未发现可安全自动纠正的 ASR 文本")
    if rejected:
        print(f"         已拒绝 {rejected} 处不满足安全规则的修改")
    print(f"         完成 → {out.name} ({len(note)} 字符)")
    return out

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("transcript", help="transcripts/ 里的文件名")
    a = p.parse_args()
    t = TRANSCRIPTS / a.transcript
    if not t.exists():
        print(f"错误: {t} 不存在")
        sys.exit(1)
    summarize(t)
