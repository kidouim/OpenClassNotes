#!/usr/bin/env python3
"""
本地配置管理 —— 管理 API key / 模型 / LLM 模式 等, 全部存本地。

配置存放: ~/.class-notes/config.json。
数据存放: 项目内的笔记/转写/OpenKB 知识库, 以及 ~/.class-notes/ 配置。

首次使用会引导用户填写 API key，默认使用 DeepSeek Flash。
"""
import getpass
from copy import deepcopy
import json, os, sys
from pathlib import Path

WORK_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path.home() / ".class-notes"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULT_CONFIG = {
    "llm_mode": "api",           # 目前支持: "api"(deepseek) ; 预留 "claude"/"codex"
    "transcription": {
        "device": "auto",
        "language": "auto",
    },
    "api": {
        "base_url": "https://api.deepseek.com",   # DeepSeek 官方 OpenAI 兼容接口
        "api_key": "",
        "model": "deepseek-flash",
    },
}


def get_config_dir() -> Path:
    """返回配置目录(不存在则创建)。"""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    return CONFIG_DIR


def ensure_config() -> dict:
    """确保配置文件存在; 若缺失则创建默认配置。返回配置 dict。"""
    if not CONFIG_FILE.exists():
        save_config(DEFAULT_CONFIG)
    return load_config()


def load_config() -> dict:
    """读配置。若损坏则重置为默认。"""
    try:
        if CONFIG_FILE.exists():
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            # 合并默认, 防止字段缺失
            merged = {**deepcopy(DEFAULT_CONFIG), **data}
            merged["api"] = {**DEFAULT_CONFIG["api"], **merged.get("api", {})}
            merged["transcription"] = {
                **DEFAULT_CONFIG["transcription"],
                **merged.get("transcription", {}),
            }
            return merged
    except Exception as e:
        print(f"⚠️  配置读取失败({e}), 已重置")
    return deepcopy(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    """写配置到本地; 权限设为 600(仅用户可读)。"""
    get_config_dir()
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(CONFIG_FILE, 0o600)


def has_api_key() -> bool:
    """是否已配置可用的 API key。"""
    cfg = load_config()
    if cfg.get("llm_mode") != "api":
        return False
    return bool(cfg.get("api", {}).get("api_key"))


def get_transcription_device() -> str:
    """Return the persisted Whisper device preference."""
    device = load_config().get("transcription", {}).get("device", "auto")
    return device if device in {"auto", "cuda", "cpu"} else "auto"


def get_transcription_language() -> str:
    """Return the persisted Whisper language preference."""
    language = load_config().get("transcription", {}).get("language", "auto")
    return language if language in {"auto", "en", "zh"} else "auto"


def prompt_for_transcription_device() -> None:
    """Let the user persistently choose automatic, GPU, or CPU transcription."""
    labels = {"auto": "自动（推荐）", "cuda": "GPU (CUDA)", "cpu": "CPU"}
    choices = {"1": "auto", "2": "cuda", "3": "cpu"}
    current = get_transcription_device()
    print(f"\n当前转录设备: {labels[current]}")
    print(" 1) 自动（检测到可用 CUDA 时使用 GPU，否则使用 CPU）")
    print(" 2) GPU (CUDA)")
    print(" 3) CPU")
    selected = input("请选择 [1/2/3，空=取消]: ").strip()
    if not selected:
        print("已取消，设置未变。")
        return
    device = choices.get(selected)
    if not device:
        print("无效选项，设置未变。")
        return
    cfg = load_config()
    cfg.setdefault("transcription", {})["device"] = device
    save_config(cfg)
    print(f"✅ 转录设备已设为: {labels[device]}")


def prompt_for_transcription_language() -> None:
    """Let the user persistently choose automatic, English, or Chinese ASR."""
    labels = {"auto": "自动检测", "en": "English", "zh": "中文"}
    choices = {"1": "auto", "2": "en", "3": "zh"}
    current = get_transcription_language()
    print(f"\n当前录音语言: {labels[current]}")
    print(" 1) 自动检测")
    print(" 2) English")
    print(" 3) 中文")
    selected = input("请选择 [1/2/3，空=取消]: ").strip()
    if not selected:
        print("已取消，设置未变。")
        return
    language = choices.get(selected)
    if not language:
        print("无效选项，设置未变。")
        return
    cfg = load_config()
    cfg.setdefault("transcription", {})["language"] = language
    save_config(cfg)
    print(f"✅ 录音语言已设为: {labels[language]}")


def prompt_for_api_key() -> None:
    """首次引导: 交互式让用户输入 API key。"""
    print("\nOpenClassNotes · API 配置")
    print("默认使用 DeepSeek Flash。获取 Key: https://platform.deepseek.com/api_keys")
    print("官方 Base URL: https://api.deepseek.com · 模型: deepseek-flash")
    print("也可填写其他 OpenAI 兼容服务的地址、Key 和模型。\n")

    cfg = load_config()

    # 若已有 key, 提示覆盖
    if cfg["api"]["api_key"]:
        user_input = input("检测到已配置 key, 是否保留[Y/n]? ").strip().lower()
        if user_input in ("", "y", "yes"):
            print("已保留现有配置。")
            return
        cfg["api"]["api_key"] = ""

    cfg["api"]["base_url"] = input(f"API Base URL [{cfg['api']['base_url']}]: ").strip() or cfg["api"]["base_url"]
    cfg["api"]["api_key"] = getpass.getpass("API Key: ").strip()
    cfg["api"]["model"] = input(f"模型名称 [{cfg['api']['model']}]: ").strip() or cfg["api"]["model"]
    cfg["llm_mode"] = "api"

    save_config(cfg)
    print("\n✅ 配置已保存到本地:", CONFIG_FILE)


if __name__ == "__main__":
    # 提供一个简单入口, 方便测
    if "--info" in sys.argv:
        cfg = load_config()
        print("llm_mode:", cfg["llm_mode"])
        print("base_url:", cfg["api"]["base_url"])
        print("model:", cfg["api"]["model"])
        print("api_key 已配置:", has_api_key())
    else:
        prompt_for_api_key()
