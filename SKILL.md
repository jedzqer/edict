---
name: edict-dev
description: "三省六部制 AI 协作系统 - LangGraph 工作流版本"
metadata:
  {
    "edict": {
      "emoji": "🏛️",
      "roles": ["zhongshu", "menxia", "shangshu", "libu_admin", "hubu", "libu", "bingbu", "xingbu", "gongbu"],
      "workflow": "sequential",
      "experimental": ["langgraph"]
    }
  }
---

# edict-dev · 三省六部制 AI 协作系统

当前仓库以 `langgraph_workflow.py` 为唯一主入口，所有说明都以现有 LangGraph 实现为准。

## 架构

```text
用户输入
  ↓
📜 中书省（规划）
  ↓
🔍 门下省（初审）
  ├─ 封驳 → 中书省重做
  └─ 通过
      ↓
📮 尚书省（分阶段派发）
  ├─ 阶段内并行：🎨 礼部 / ⚔️ 兵部 / 🔧 工部
  └─ 条件治理：💰 户部 / ⚖️ 刑部 / 🏷️ 吏部
      ↓
🔍 门下省（终验）
  ↓
最终汇总
```

## 角色职责

| 角色 | 定位 | 当前实现方式 | 典型场景 |
|------|------|--------------|----------|
| 📜 中书省 | 需求理解、方案起草 | LangGraph 节点 | 拆解任务、制定步骤 |
| 🔍 门下省 | 审核封驳、终验 | LangGraph 节点 | 初审、终验、纠偏 |
| 📮 尚书省 | 协调派发、结果整合 | LangGraph 节点 | 多部门依赖、阶段调度 |
| 🏷️ 吏部 | 角色治理 | 条件治理节点 | 名册、能力映射、低频治理 |
| 💰 户部 | 资源治理 | 条件治理节点 | 成本、预算、资源评估 |
| 🎨 礼部 | 文事执行 | 尚书省内部调度 + Tool | 文档、翻译、润色 |
| ⚔️ 兵部 | 技术执行 | 尚书省内部调度 + Tool | 代码、脚本、调试 |
| ⚖️ 刑部 | 风险审查 | 条件治理节点 | 风险、权限、审计 |
| 🔧 工部 | 工事执行 | 尚书省内部调度 + Tool | 构建、部署、命令执行 |

## 什么时候适合触发

适合：

- 多步骤复杂任务
- 同时涉及代码、文档、部署、风险或资源约束
- 需要先规划再执行的任务
- 需要门下省验收把关的任务

不适合：

- 简单查询
- 单一步骤任务
- 不需要拆解和审核的即时回答

## 使用方式

CLI：

```bash
cd /path/to/edict
source .venv/bin/activate
pip install -r requirements.txt
cp models.json.example models.json
python langgraph_workflow.py "帮我创建 Flask 项目"
```

Python：

```python
from langgraph_workflow import run_langgraph_workflow

result = run_langgraph_workflow("帮我创建 Flask API 项目")
```

## 运行产物

当前状态和日志会保存在：

- `tasks.db`
- `tasks/{task_id}/session.log`
- `tasks/{task_id}/*.output`
- `tasks/{task_id}/result_langgraph.json`
- `history/YYYYMMDD.log`

## 当前实现边界

- 当前没有 `workflow.py` 传统入口
- 当前没有 `opencode` 依赖
- 当前没有 `spawn` 子代理编排
- 兵部/礼部/工部直接使用 LangChain agent + tools
- 升级/申诉机制目前主要写在文档中，尚未完整自动化

## 扩展建议

添加或调整角色时，通常需要同时修改：

1. `roles/{role}.md`
2. `langgraph_workflow.py` 中的角色映射和派发逻辑
3. `LEGAL_FLOWS`
4. `PROTOCOL.md`

## 注意事项

- 刑部和户部是条件治理节点，不建议默认强制每次串行执行
- 吏部属于低频治理节点
- 封驳机制可能导致多轮重试
- 当前主工作流主要依赖 `models.json` 而不是 `DASHSCOPE_API_KEY`
