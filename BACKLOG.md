# BACKLOG — 未修复问题记录

> 记录日期：2026-04-01
> 高优先级问题（1-5）已在同期修复，见 `langgraph_workflow.py` 提交记录。

---

## ⚠️ 中优先级

### M1：治理部门触发逻辑过于简陋
**文件**：`langgraph_workflow.py` — `should_run_governance` / `after_governance_route`

当前实现靠关键词列表匹配（如出现 `"sudo"` 就触发刑部），容易误判或漏判。

**建议**：引入 LLM 上下文分析，由门下省或尚书省基于语义判断是否需要触发治理部门，而非硬编码关键词。

---

### M2：模型配置硬编码
**文件**：`langgraph_workflow.py` 顶部，`llm = ChatDashScope(model="qwen-plus", ...)`

`models.json.example` 定义了 per-role 模型配置，但代码完全未读取。

**建议**：启动时加载 `models.json`（若存在），按角色分别初始化 LLM 实例；模型名称不再在代码中写死。

---

### M3：无全局超时
**文件**：`langgraph_workflow.py` — `run_langgraph_workflow`

整个工作流没有全局时间限制，理论上可无限运行。

**建议**：在 `run_langgraph_workflow` 入口用 `concurrent.futures.ThreadPoolExecutor` + `future.result(timeout=...)` 或 `signal.alarm` 设置全局超时上限（建议 1800s）。

---

### M4：问题分级未实现
**文件**：`ESCALATION.md`（已定义）、`langgraph_workflow.py`（未实现）

`ESCALATION.md` 定义了 P0-P3 分级和各级响应时限，代码中无任何分级逻辑。

**建议**：在 `EdictState` 增加 `priority: Literal["P0","P1","P2","P3"]` 字段，刑部节点检测到高风险时可将优先级升级为 P0/P1 并触发对应处理路径。

---

### M5：权限边界三处重复定义
**文件**：`SECURITY.md`、`PROTOCOL.md`、`roles/*.md`

`LEGAL_FLOWS` 和 `ROLE_CAPABILITIES` 已在代码中实现，但三份文档各自维护一套，易产生不一致。

**建议**：以代码中的 `LEGAL_FLOWS` / `ROLE_CAPABILITIES` 为单一权威来源，文档改为引用此处而非重复定义。

---

## 💡 低优先级

### L1：门下省缺少对六部结果的二次验收
**文件**：`langgraph_workflow.py` — `menxia_node` 及工作流图

`roles/menxia.md` 定义了"最终验收"职责，但 `menxia_node` 仅审核中书省方案，六部执行结果无任何验收节点。

**建议**：在 `finalize_node` 之前增加 `menxia_final_node`，由门下省对六部执行成果做最终质量把关。

---

### L2：六部执行结果结构信息丢失
**文件**：`langgraph_workflow.py` — `EdictState.ministry_results: Dict[str, str]`

所有部门输出被压缩为字符串，JSON 结构、置信度、子任务状态等信息丢失。

**建议**：将类型改为 `Dict[str, Dict[str, Any]]`，保留结构化输出；同步更新数据库序列化逻辑。

---

### L3：LLM 与错误返回格式不统一
**文件**：`langgraph_workflow.py` — `call_llm_with_retry` 及各节点

LLM 调用失败返回 `"Error: ..."` 字符串，而非抛出异常，导致上层节点难以区分正常输出和错误。

**建议**：统一用自定义异常 `EdictExecutionError` 传播错误，或在 `call_llm_with_retry` 返回 `Result[str, str]` 结构体，避免上层用字符串前缀检测错误。

---

### L4：缺少 LangSmith 监控集成
**文件**：`LANGGRAPH.md`（已提及）、`langgraph_workflow.py`（未实现）

文档承诺"LangSmith 集成"，代码中没有任何相关代码。

**建议**：检测环境变量 `LANGCHAIN_API_KEY`，若存在则自动启用 LangSmith tracing（设置 `LANGCHAIN_TRACING_V2=true`）。

---

### L5：工部使用 LLM 还是外部工具需澄清
**文件**：`roles/gongbu.md`

原始代码将工部与兵部并列使用 OpenCode（现已移除），但 `gongbu.md` 中未说明执行方式。移除 OpenCode 后工部已统一改用 LLM，需在角色文件中同步说明。

**建议**：在 `roles/gongbu.md` 顶部标注"通过 LLM 执行，不使用外部工具"，与兵部保持一致。
