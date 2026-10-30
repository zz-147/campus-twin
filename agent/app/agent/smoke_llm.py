# -*- coding: utf-8 -*-
"""真实 LLM 一键验证（key 就绪后由 lead 直接运行）。

用法（在 ``D:\\zdsh\\campus-twin\\agent`` 目录下）：

    :: 1) 直连 LLM 冒烟（不需要后端，只验证 key/端点/模型可用）
    ..\\.venv\\Scripts\\python.exe -m app.agent.smoke_llm

    :: 2) 指定 provider/model 覆盖
    ..\\.venv\\Scripts\\python.exe -m app.agent.smoke_llm --provider deepseek --model deepseek-chat

    :: 3) 完整 Agent 链路（真实后端 http://127.0.0.1:8000 + 工具调用）
    ..\\.venv\\Scripts\\python.exe -m app.agent.smoke_llm --full --question "教学楼 PC-JX1 为什么掉线？"

    :: 4) 离线 mock 自检（不需要 key，可随时验证脚本本身）
    ..\\.venv\\Scripts\\python.exe -m app.agent.smoke_llm --provider mock

退出码：0=成功；2=配置缺失（key/base_url/model）；3=模型调用失败。
安全：只打印 key 的「已配置/长度」，绝不回显 key 明文。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import List, Optional

from app.agent.agent import OpsAgent
from app.agent.backend import HttpBackendClient
from app.agent.providers.base import LLMProviderError, ProviderConfigError
from app.agent.providers.factory import build_provider
from app.agent.tools import OpsToolkit
from app.config import AgentSettings, resolve_provider_config
from app.agent.redact import key_hint


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="campus-twin AI Agent LLM 连通性冒烟测试")
    parser.add_argument(
        "--provider",
        default=None,
        help="覆盖 LLM_PROVIDER（deepseek/openai/ollama/openai_compat/mock）；默认读取 .env/环境变量",
    )
    parser.add_argument("--model", default=None, help="覆盖 LLM_MODEL（可选）")
    parser.add_argument("--question", default=None, help="自定义问题")
    parser.add_argument("--full", action="store_true", help="跑完整 Agent（调用真实后端工具）")
    parser.add_argument("--show-trace", action="store_true", help="打印 Agent trace JSON")
    args = parser.parse_args(argv)

    settings = AgentSettings.from_env()
    # 解析最终生效的配置（覆盖 provider/model 后），用于展示与构造 provider
    try:
        config = resolve_provider_config(settings, args.provider, args.model)
        provider = build_provider(config)
    except ProviderConfigError as exc:
        print("=" * 64)
        print("campus-twin AI 运维 Agent —— LLM 连通性验证")
        print("[配置错误] %s" % exc, file=sys.stderr)
        return 2

    print("=" * 64)
    print("campus-twin AI 运维 Agent —— LLM 连通性验证")
    print("provider     : %s" % config.provider)
    print("model        : %s" % (config.model or "(未配置)"))
    print("base_url     : %s" % (config.base_url or "(provider 默认)"))
    print("API key      : %s（来源：%s）" % (key_hint(config.api_key), config.key_source or "未配置"))
    print("data_source  : %s" % settings.data_source)
    print("=" * 64)

    if config.provider != "mock" and not config.api_key:
        # ollama/openai_compat 本地端点可能确实不需要 key，这里只提示不阻断
        print("[提示] 当前没有读到 LLM_API_KEY。若使用本地 Ollama/自建端点可忽略；"
              "若使用 DeepSeek/OpenAI，请把 key 写入 campus-twin/agent/.env 的 LLM_API_KEY=...")

    if args.full:
        return _run_full_agent(provider, settings, args)

    question = args.question or "这是连通性测试。请用一句中文确认你能正常回复，并说明你是什么模型。"
    messages = [
        {"role": "system", "content": "你是网络运维助手，回答简洁、使用中文。"},
        {"role": "user", "content": question},
    ]
    print("发起直连 LLM 调用 ...")
    started = time.perf_counter()
    try:
        reply = provider.chat(messages, tools=None)
    except LLMProviderError as exc:
        print("[LLM 调用失败] %s" % exc, file=sys.stderr)
        return 3
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    print("✓ 调用成功：%.1f ms" % elapsed_ms)
    print("模型回复：")
    print((reply.content or "(空回复)")[:1000])
    if reply.usage:
        print("tokens：%s" % json.dumps(reply.usage, ensure_ascii=False))
    print("")
    print("下一步建议：运行完整链路验证 → python -m app.agent.smoke_llm --full")
    return 0


def _run_full_agent(provider, settings: AgentSettings, args) -> int:
    backend = HttpBackendClient(settings.backend_base_url, settings.backend_timeout_s)
    toolkit = OpsToolkit(backend)
    agent = OpsAgent(
        provider=provider,
        toolkit=toolkit,
        max_steps=settings.max_steps,
        max_tool_calls_per_step=settings.max_tool_calls_per_step,
    )
    question = args.question or "请检查当前校园网是否有故障，给出根因、依据和排障建议。"
    print("发起完整 Agent 调用（后端 %s）..." % settings.backend_base_url)
    print("问题：%s" % question)
    try:
        result = agent.run(question)
    except LLMProviderError as exc:
        print("[LLM 调用失败] %s" % exc, file=sys.stderr)
        return 3
    print("")
    print("provider=%s model=%s elapsed=%.1f ms" % (
        result.provider,
        result.model,
        result.elapsed_ms,
    ))
    print("工具调用序列：%s" % (" → ".join(result.trace.get("tool_sequence") or []) or "(无)"))
    print("工具错误数：%s" % result.trace.get("tool_error_count"))
    print("停止原因：%s" % result.trace.get("stop_reason"))
    print("-" * 64)
    print(result.answer)
    print("-" * 64)
    if args.show_trace:
        print(json.dumps(result.trace, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
