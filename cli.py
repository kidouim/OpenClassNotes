#!/usr/bin/env python3
"""
OpenClassNotes —— 交互式命令行入口

把 mp3 录音自动转成笔记, 并接入 OpenKB 知识库。用户通过菜单操作, 无需懂内部。
数据全部保存在本地 (~/.class-notes/ 以及本项目的 notes/transcripts/knowledge)。

用法: python cli.py
"""
import os
import sys
from pathlib import Path


def _configure_windows_console():
    """Keep Chinese text and status symbols printable on GBK Windows terminals."""
    if os.name != "nt":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


_configure_windows_console()

WORK = Path(__file__).resolve().parent
sys.path.insert(0, str(WORK / "scripts"))

import config
import llm_client
import processing_state
import openkb_backend
from transcribe import transcribe
from summarize import summarize


# ---------- 菜单操作 ----------

def cmd_convert(force: bool = False):
    """转换新课: mp3 → 转录 → 清洗/提炼 → OpenKB 归档"""
    mp3_dir = WORK / "mp3"
    supported = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac"}
    audio_files = sorted(
        (p for p in mp3_dir.iterdir() if p.is_file() and p.suffix.lower() in supported),
        key=lambda p: p.name.lower(),
    ) if mp3_dir.exists() else []
    if not audio_files:
        print(f"\n⚠️  mp3/ 目录里没有支持的录音文件。\n     {mp3_dir}\n")
        return

    duplicate_stems = sorted(
        stem for stem in {p.stem.lower() for p in audio_files}
        if sum(p.stem.lower() == stem for p in audio_files) > 1
    )
    if duplicate_stems:
        print(f"\n❌ 存在同名不同格式的录音，会覆盖同一份笔记: {', '.join(duplicate_stems)}")
        print("请先为这些录音改成不同文件名。")
        return False

    state_path = WORK / "graphs/processing_state.json"
    state = processing_state.load_state(state_path)
    pending = []
    completed = []
    for audio in audio_files:
        record = processing_state.current_record(state, audio)
        transcript_ok = record and Path(record.get("transcript", "")).exists()
        note_ok = record and Path(record.get("note", "")).exists()
        if not force and record and record.get("stage") == "completed" and transcript_ok and note_ok:
            completed.append(audio)
        else:
            pending.append(audio)

    if not pending:
        print(f"\n✅ 没有新录音；已自动跳过 {len(completed)} 个完成项。")
        print("如需重做某门课程，请在主菜单选择 r。")
        return True

    action = "可重新处理" if force else "待处理"
    print(f"\n📂 发现 {len(pending)} 个{action}音频", end="")
    if completed:
        print(f"，已跳过 {len(completed)} 个完成项", end="")
    print(":")
    for i, f in enumerate(pending, 1):
        print(f"   {i}. {f.name}")
    prompt = (
        "\n选择要重新处理哪一个(编号,空=取消): "
        if force
        else "\n选择要处理哪一个(编号,空=全部): "
    )
    sel = input(prompt).strip()
    if sel == "":
        if force:
            print("已取消重新处理。")
            return True
        targets = pending
    else:
        try:
            targets = [pending[int(sel)-1]]
        except (ValueError, IndexError):
            print("无效选择"); return

    succeeded = []
    failed = []
    for audio in targets:
        print(f"\n========== 处理: {audio.name} ==========")
        record = None if force else processing_state.current_record(state, audio)
        try:
            if record and record.get("stage") in ("transcribed", "noted"):
                transcript = Path(record.get("transcript", ""))
            else:
                transcript = None
            if transcript and transcript.exists():
                print(f"[1/转录] 已有有效转写，断点续跑 → {transcript.name}")
            else:
                transcript = transcribe(audio)
                processing_state.update_record(state, audio, "transcribed", transcript=transcript)
                processing_state.save_state(state_path, state)

            record = processing_state.current_record(state, audio)
            note = Path(record.get("note", "")) if record and record.get("stage") == "noted" else None
            if note and note.exists():
                print(f"[2/清洗+提炼] 已有有效笔记，断点续跑 → {note.name}")
            else:
                note = summarize(transcript)
                processing_state.update_record(
                    state, audio, "noted", transcript=transcript, note=note
                )
                processing_state.save_state(state_path, state)
            succeeded.append((audio, note, transcript))
        except Exception as e:
            failed.append((audio.name, str(e)))
            previous = processing_state.current_record(state, audio)
            stage = previous.get("stage", "new") if previous else "new"
            processing_state.update_record(state, audio, stage, error=str(e))
            processing_state.save_state(state_path, state)
            print(f"处理 {audio.name} 失败: {e}")

    if not succeeded:
        print(f"\n❌ 转换失败：{len(failed)} 个音频均未生成笔记，未执行知识库归档。")
        return False

    try:
        print("[3/归档] 使用 OpenKB 编译本次笔记（会调用 LLM）...")
        archive = openkb_backend.sync_notes(note for _, note, _ in succeeded)
    except Exception as exc:
        archive = {
            "available": False,
            "added": 0,
            "skipped": 0,
            "failed": len(succeeded),
            "synced_files": [],
            "failed_files": [note.name for _, note, _ in succeeded],
            "warnings": [str(exc)],
        }
        print(f"         OpenKB 归档失败: {exc}")
    synced_files = set(archive.get("synced_files", []))
    for audio, note, transcript in succeeded:
        if archive["available"] and note.name in synced_files:
            processing_state.update_record(
                state, audio, "completed", transcript=transcript, note=note
            )
        else:
            processing_state.update_record(
                state, audio, "noted", transcript=transcript, note=note,
                error="OpenKB 归档尚未完成",
            )
    processing_state.save_state(state_path, state)
    if archive["available"] and archive["failed"] == 0:
        archive_text = (
            f"OpenKB 新编译 {archive['added']} 篇、跳过未变化 {archive['skipped']} 篇"
        )
    elif archive["available"]:
        archive_text = f"OpenKB 有 {archive['failed']} 篇归档失败"
    else:
        archive_text = "OpenKB 不可用，Markdown 笔记已保留"

    archive_ok = archive["available"] and archive["failed"] == 0
    status = "✅" if not failed and archive_ok else "⚠️"
    print(
        f"\n{status} 本次完成 {len(succeeded)} 篇笔记，失败 {len(failed)} 个；"
        f"{archive_text}。"
    )
    return not failed and archive_ok


def cmd_reprocess():
    """重新处理用户明确选择的一门已完成或未完成课程。"""
    return cmd_convert(force=True)


def cmd_search():
    """使用 OpenKB 对本地 Wiki 做有依据的问答。"""
    q = input(" 输入问题（会调用 LLM）: ").strip()
    if not q:
        return
    try:
        print("\n🔍 OpenKB 正在读取知识库...")
        answer = openkb_backend.ask(q)
        print("\n" + (answer.strip() or "(没有生成回答)"))
        return bool(answer.strip())
    except Exception as e:
        print(f"查询失败: {e}")
        return False


def cmd_graph():
    """生成并打开 OpenKB 原生三维知识图谱。"""
    try:
        result = openkb_backend.render_visualization(open_browser=True)
        if not result["nodes"]:
            print("\n⚠️  OpenKB 里还没有可视化页面。请先转换课程或使用菜单 4 同步已有笔记。")
            return False
        print(
            f"\n🕸️  OpenKB 原生图谱：{result['nodes']} 个节点 / "
            f"{result['edges']} 条连接"
        )
        if result["opened"]:
            print("   已使用默认浏览器打开。")
        else:
            print(f"   浏览器未能自动打开，请手动打开：{result['path']}")
        return True
    except Exception as e:
        print(f"图谱生成失败: {e}")
        return False


def cmd_sync():
    """把旧版本已有的 Markdown 笔记增量同步到 OpenKB。"""
    try:
        notes = sorted((WORK / "notes").glob("*.md"))
        if not notes:
            print("\n⚠️  notes/ 中没有可同步的笔记。")
            return False
        print(f"\n将增量检查 {len(notes)} 篇笔记；只有新增或修改内容会调用 LLM。")
        result = openkb_backend.sync_notes(notes)
        print(
            f"✅ OpenKB 同步完成：新增/更新 {result['added']}，"
            f"未变化 {result['skipped']}，失败 {result['failed']}。"
        )
        for warning in result.get("warnings", []):
            print(f"⚠️  {warning}")
        return result["failed"] == 0
    except Exception as e:
        print(f"同步失败: {e}")
        return False


def cmd_config():
    """配置 API key"""
    config.prompt_for_api_key()


def cmd_device():
    """配置 Whisper 转录设备"""
    config.prompt_for_transcription_device()


def cmd_language():
    """配置 Whisper 录音语言"""
    config.prompt_for_transcription_language()


def cmd_guide():
    """使用指南"""
    print("""
《OpenClassNotes 使用指南》
==========================
1. 配好 API key(菜单 5)后, 在菜单 1 选择"转换新课"。
2. 把录音(mp3/wav/m4a/flac/ogg/aac)放进 mp3/ 文件夹, 然后选择要处理的。
3. 系统会自动: 本地转录 → 同次调用清洗术语并提炼笔记 → 编译进 OpenKB。
   已完成录音自动跳过; 中途失败会从最近成功阶段继续。
4. 菜单 r 可选择一门课程重新处理，用于应用新版清洗规则；空输入会取消。
5. 菜单 2 是 OpenKB 问答；菜单 3 打开原生 3D/思维导图/径向图谱。
6. 从旧版本升级时，菜单 4 可把 notes/ 中已有笔记增量同步进 OpenKB。
7. 菜单 7 可选择自动、GPU (CUDA) 或 CPU 转录；默认自动。
8. 菜单 8 可选择自动检测、English 或中文；已知语言时建议明确选择。

产出位置:
  transcripts/       → 自动纠错后的带时间戳转写稿
  notes/             → 整理好的笔记(md)
  graphs/processing_state.json → 断点续跑状态
  knowledge/wiki/    → OpenKB 生成的本地 Wiki
  knowledge/output/visualize/graph.html → OpenKB 原生交互图谱
  ~/.class-notes/    → 本地配置及隐藏原始转写备份

录音、转写、笔记和知识库保存在本地；清洗/提炼共用一次模型处理，OpenKB 编译和问答也会调用你配置的 LLM API。
为避免重复花费，未变化的笔记不会重新编译。
""")


# ---------- 主菜单 ----------

MAIN_MENU = """
📚 OpenClassNotes — 上课录音自动笔记 + OpenKB 知识库
========================================================
 1) 🗂️   转换新课   (mp3 → 笔记 → OpenKB)
 2) 🔍   向知识库提问
 3) 🕸️   打开 OpenKB 3D 图谱
 4) 🔄   同步已有笔记
 5) ⚙️   配置 API key
 6) ❓   使用指南
 7) 🖥️   选择转录设备 (自动/GPU/CPU)
 8) 🌐   选择录音语言 (自动/English/中文)
 r) ♻️   重新处理课程
 q)     退出
========================================================
"""

MENU_HANDLERS = {
    "1": cmd_convert,
    "2": cmd_search,
    "3": cmd_graph,
    "4": cmd_sync,
    "5": cmd_config,
    "6": cmd_guide,
    "7": cmd_device,
    "8": cmd_language,
    "r": cmd_reprocess,
}


def safe_input(prompt_text="") -> str:
    """健壮的 input: 遇 EOF/Ctrl+C 安全退出, 不会崩。"""
    try:
        return input(prompt_text)
    except (EOFError, KeyboardInterrupt):
        raise SystemExit("\n已退出")


def main():
    # 首次运行: 检测未配置 key, 引导
    if not config.has_api_key():
        print("\nℹ️  检测到尚未配置 LLM API key。")
        config.prompt_for_api_key()

    while True:
        print(MAIN_MENU)
        choice = safe_input("请选择操作: ").strip().lower()
        if choice in ("q", "quit", "exit"):
            print("再见!")
            break
        handler = MENU_HANDLERS.get(choice)
        if handler:
            try:
                handler()
                safe_input("\n按回车继续...")
            except SystemExit:
                raise
            except Exception as e:
                print(f"\n⚠️  出错了: {e}")
        else:
            print("无效选项, 请重试。")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        print("\n已退出")
