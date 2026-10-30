# -*- coding: utf-8 -*-
"""W11 LangGraph 编排引擎包。

- ``state.AgentState``：图状态（contract 字段 + 并行合并 reducer）
- ``graph.LangGraphEngine``：StateGraph（classify → Send fan-out 子 Agent → aggregate →
  interrupt 人机确认 → execute_action）+ SqliteSaver checkpointer
- ``langchain_tools``：16 个只读工具的 LangChain Tool 包装（bind_tools/ToolNode 可用）

通过 ``AGENT_ENGINE=langgraph|classic`` 在 API 层切换；classic 为原 W10 Orchestrator。
"""

from app.agent.graph.graph import GRAPH_VERSION, ROLE_NODES, LangGraphEngine  # noqa: F401
from app.agent.graph.state import AgentState  # noqa: F401
