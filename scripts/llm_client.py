#!/usr/bin/env python3
"""
统一 LLM 客户端 —— 抽象对 LLM 的调用, 供 summarize 等使用。

设计目标:
  - 不依赖任何外部 agent / 代理(不再用 litellm 代理)。
  - 从本地配置读 API key, 每次调用直接调 API。
  - 预留 "claude"/"codex" 等 agent 模式接口, 但目前未启用。

用法:
  from llm_client import generate, call_llm
  text = generate("你是笔记助手", "请整理...")
"""
import json, time
import urllib.error
import urllib.request
from urllib.parse import urlparse

import config


class LLMRequestError(RuntimeError):
    """LLM 请求最终失败；调用方不得把错误信息当作模型输出保存。"""


def _api_chat(base_url, api_key, model, messages, temperature=0.4, max_tokens=2000) -> str:
    """核心: 调 OpenAI 兼容格式 API (deepseek 也兼容)。"""
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    # Flash enables thinking by default. Disable it for note generation;
    # other OpenAI-compatible providers may reject this DeepSeek parameter.
    if urlparse(base_url).hostname == "api.deepseek.com":
        payload["thinking"] = {"type": "disabled"}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    last_error = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read().decode())
                content = data["choices"][0]["message"]["content"]
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("LLM 返回了空内容")
                return content
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403, 404):
                raise LLMRequestError(f"LLM 请求被拒绝(HTTP {e.code})，请检查 API 配置") from e
            last_error = e
            print(f"   LLM 调用失败(第{attempt+1}/3): HTTP {e.code}")
        except Exception as e:
            last_error = e
            print(f"   LLM 调用失败(第{attempt+1}/3): {e}")
        if attempt < 2:
            time.sleep(3)
    detail = f"{type(last_error).__name__}: {last_error}" if last_error else "未知错误"
    raise LLMRequestError(f"LLM 调用多次失败: {detail}") from last_error


def generate(system_prompt: str, user_text: str, temperature=0.4, max_tokens=2000) -> str:
    """统一入口。根据配置的 llm_mode 分发。

    目前只实现 "api"(deepseek/OpenAI兼容)。claude/codex 预留分支。
    """
    cfg = config.load_config()
    mode = cfg.get("llm_mode", "api")

    if mode == "claude":
        # TODO: 预留 —— 调用 claude code (agent 模式)
        raise NotImplementedError("claude 模式尚未实现")
    if mode == "codex":
        # TODO: 预留 —— 调用 codex (agent 模式)
        raise NotImplementedError("codex 模式尚未实现")

    # 默认: api 模式
    api = cfg.get("api", {})
    if not api.get("api_key"):
        raise RuntimeError("未配置 API key。请先运行 `python scripts/config.py` 配置。")
    return _api_chat(
        base_url=api["base_url"],
        api_key=api["api_key"],
        model=api["model"],
        messages=[{"role": "system", "content": system_prompt},
                  {"role": "user", "content": user_text}],
        temperature=temperature,
        max_tokens=max_tokens,
    )


def check_llm() -> bool:
    """健康检查: 用一次简短调用验证 key 是否可用。

    成功 = 返回了非空且有内容的文本(不苛刻要求特定词)。
    """
    if not config.has_api_key():
        return False
    try:
        resp = generate("你是助手", "请只回复: OK")
        resp = (resp or "").strip()
        return bool(resp) and len(resp) < 200  # 有内容且合理的长度
    except Exception:
        return False


# 向后兼容: summarize.py 里用了 call_llm(text), 这里提供同签名别名
def call_llm(text: str, system_prompt: str | None = None) -> str:
    return generate(system_prompt or "你是助手", text)


if __name__ == "__main__":
    import sys
    import config as _cfg
    # 快速测试: python scripts/llm_client.py --selftest
    if "--selftest" in sys.argv:
        ok = check_llm()
        print("LLM 连通:", "✅" if ok else "❌ (未配置或 key 无效)")
        if ok:
            print("示例回复:", generate("你是助手", "请用一句话介绍OpenClassNotes"))
    else:
        print("LLM 客户端。用法: python scripts/llm_client.py --selftest")
