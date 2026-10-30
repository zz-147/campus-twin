# -*- coding: utf-8 -*-
"""把 Agent 的 23 个只读工具包装成 LangChain Tool（供 bind_tools / ToolNode 使用）。

- 与现有工具**同名同参数**（名称/描述/JSON schema 参数都来自 ``OpsToolkit.schemas()``）；
- 参数校验用 pydantic 动态模型（与 function-calling schema 对齐），LangChain
  ``bind_tools`` 会把 schema 传给模型；
- 真正的执行仍然走 ``OpsToolkit.call()``（只读、失败 ok=false 回填），因此
  LangGraph 版本与 classic 版本的行为一致。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Type

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import Field, create_model

from app.agent.tools import DIAGNOSIS_TOOL_NAMES, OpsToolkit

_TYPE_MAP = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
}


def _build_args_model(tool_name: str, parameters: Dict[str, Any]) -> Type[Any]:
    properties = parameters.get("properties") or {}
    required = set(parameters.get("required") or [])
    fields: Dict[str, Any] = {}
    for prop_name, prop_schema in properties.items():
        annotation = _TYPE_MAP.get(str(prop_schema.get("type") or "string"), Any)
        description = str(prop_schema.get("description") or "")
        if prop_name in required:
            fields[prop_name] = (annotation, Field(..., description=description))
        else:
            fields[prop_name] = (Optional[annotation], Field(default=None, description=description))
    model_name = "".join(part.capitalize() for part in tool_name.split("_")) + "Args"
    return create_model(model_name, **fields)  # type: ignore[call-overload]


def build_langchain_tools(
    toolkit: OpsToolkit, names: Optional[List[str]] = None
) -> List[BaseTool]:
    """把只读工具集包装成 LangChain ``StructuredTool`` 列表。"""
    allowed = set(names) if names is not None else None
    tools: List[BaseTool] = []
    for spec in toolkit.schemas():
        function = spec.get("function") or {}
        name = str(function.get("name") or "")
        if not name or (allowed is not None and name not in allowed):
            continue
        parameters = function.get("parameters") or {}

        def _invoke(_name: str = name, **kwargs: Any) -> Any:
            envelope = toolkit.call(_name, kwargs)
            if envelope.get("ok"):
                return envelope.get("data")
            return {"ok": False, "error": envelope.get("error"), "tool": _name}

        try:
            args_schema = _build_args_model(name, parameters)
            tools.append(
                StructuredTool.from_function(
                    func=_invoke,
                    name=name,
                    description=str(function.get("description") or ""),
                    args_schema=args_schema,
                )
            )
        except Exception:  # noqa: BLE001 - 单个工具 schema 转换失败不影响其它工具
            tools.append(
                StructuredTool.from_function(
                    func=_invoke,
                    name=name,
                    description=str(function.get("description") or ""),
                )
            )
    return tools


def build_diagnosis_tools(toolkit: OpsToolkit) -> List[BaseTool]:
    """诊断 Agent 专用工具（8 后端只读 + 5 MCP 只读工具白名单）。"""
    return build_langchain_tools(toolkit, names=list(DIAGNOSIS_TOOL_NAMES))


def build_chat_model(provider_config: Any) -> Any:
    """构造 LangChain ``ChatOpenAI``（DeepSeek/OpenAI/任意兼容端点）。

    mock provider 或缺少 key 时返回 None（graph 内回退到现有 mock 诊断循环）。
    """
    if provider_config is None:
        return None
    provider_name = str(getattr(provider_config, "provider", ""))
    api_key = str(getattr(provider_config, "api_key", "") or "")
    if provider_name == "mock":
        return None
    if not api_key and provider_name in ("deepseek", "openai"):
        return None
    try:
        from langchain_openai import ChatOpenAI
    except ImportError:  # pragma: no cover - 依赖已安装，防御性
        return None
    kwargs: Dict[str, Any] = {
        "model": str(getattr(provider_config, "model", "") or "deepseek-chat"),
        "api_key": api_key or "not-needed",
        "temperature": float(getattr(provider_config, "temperature", 0.2) or 0.2),
        "timeout": float(getattr(provider_config, "timeout_s", 30.0) or 30.0),
        "max_retries": int(getattr(provider_config, "max_retries", 2) or 0),
    }
    base_url = str(getattr(provider_config, "base_url", "") or "")
    if base_url:
        kwargs["base_url"] = base_url
    try:
        return ChatOpenAI(**kwargs)
    except Exception:  # pragma: no cover - 配置异常时回退
        return None
