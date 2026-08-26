#!/usr/bin/env python3
"""
本地配置管理 —— 管理 API key / 模型 / LLM 模式 等, 全部存本地。

配置存放: ~/.class-notes/config.json  (权限 600, 仅当前用户可读写)
数据存放: 项目内的笔记/转写/OpenKB 知识库, 以及 ~/.class-notes/ 配置。

首次使用会引导用户填写 API key; 也可用 build_ollama 之类, 但默认只填 key。
"""
import getpass
import json, os, sys
from pathlib import Path

WORK_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path.home() / ".class-notes"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULT_CONFIG = {
    "llm_mode": "api",           # 目前支持: "api"(deepseek) ; 预留 "claude"/"codex"
    "api": {
        "base_url": "https://api.deepseek.com/v1",   # deepseek 官方(OpenAI 兼容)
        "api_key": "",
        "model": "deepseek-chat",
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
            merged = {**DEFAULT_CONFIG, **data}
            merged["api"] = {**DEFAULT_CONFIG["api"], **merged.get("api", {})}
            return merged
    except Exception as e:
        print(f"⚠️  配置读取失败({e}), 已重置")
    return DEFAULT_CONFIG


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


def prompt_for_api_key() -> None:
    """首次引导: 交互式让用户输入 API key。"""
    print("\n首次使用 OpenClassNotes, 需要配置 LLM 的 API key。")
    print("当前支持 DeepSeek(最便宜、中文好)。需要先去 https://platform.deepseek.com 获取 api_key。")
    print("(或填任意 OpenAI 兼容服务的 base_url / key / model)\n")

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
    cfg["api"]["model"] = input(f"模型名称 [deepseek-chat]: ").strip() or "deepseek-chat"
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
