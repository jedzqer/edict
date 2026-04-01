# edict-dev · 三省六部制 AI 协作系统

> **🏛️ 用三省六部制组织多 Agent 协作，以当前 LangGraph 实现为准。**

---

## 项目概览

`edict-dev` 是一个基于 LangGraph 的多 Agent 工作流实验项目。当前实现的主链是：

`中书省（规划） → 门下省（初审） → 尚书省（分阶段派发） → 门下省（终验） → finalize`

其中：

- 礼部、兵部、工部由尚书省在内部按阶段派发，同阶段可并行执行
- 刑部、户部、吏部按语义分析结果作为条件治理节点介入
- 兵部、礼部、工部可直接使用 `work/` 目录工具

---

## 目录结构

```text
edict/
├── README.md
├── INSTALL.md
├── SKILL.md
├── PROTOCOL.md
├── ESCALATION.md
├── models.json.example
├── langgraph_workflow.py
├── requirements.txt
├── roles/
├── tasks/        # 运行后生成的任务产物
├── history/      # 汇总日志
├── work/         # 执行部门可操作目录
└── tasks.db      # SQLite 状态库（运行后生成）
```

---

## 快速开始

```bash
# 1. 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 准备模型配置
cp models.json.example models.json

# 4. 编辑 models.json，填写 model/api_key/temperature

# 5. 运行工作流
python langgraph_workflow.py "开发一个 Flask API 项目"
```

---

## 核心特性

1. **驳回重做循环**：门下省审核不通过时自动退回中书省重做。
2. **职责分离**：中书省负责规划，门下省负责审核，尚书省负责派发和调度。
3. **原生 Tool Calling**：礼部/兵部/工部可直接读写 `work/`，工部可执行命令。
4. **合法流转校验**：通过 `LEGAL_FLOWS` 和角色边界限制跨部门越权。
5. **分阶段调度**：尚书省支持“阶段间串行、阶段内并行”。
6. **状态留痕**：SQLite、`tasks/{task_id}` 文件和 `history/` 日志同时保留。
7. **条件治理**：刑部、户部、吏部按需进入治理链路。

---

## 当前架构

```text
START
  │
  ▼
中书省（规划）
  │
  ▼
门下省（初审）
  ├─ 不通过 → 中书省重做
  └─ 通过
      │
      ▼
    尚书省（分阶段派发）
      ├─ 阶段内并行：礼部 / 兵部 / 工部
      └─ 条件治理：刑部 / 户部 / 吏部
              │
              ▼
        门下省（终验）
              │
              ▼
           finalize
```

---

## 使用方式

CLI：

```bash
source .venv/bin/activate
python langgraph_workflow.py "创建 Python Flask API 项目"
```

Python：

```python
from langgraph_workflow import run_langgraph_workflow

result = run_langgraph_workflow("帮我创建 Flask 项目")
```

可选开启 LangSmith tracing：

```bash
export LANGCHAIN_API_KEY="your-langsmith-key"
export LANGCHAIN_PROJECT="edict"
```

---

## 运行产物

每次运行会在 `tasks/{task_id}/` 下生成：

- `session.log`：当前任务会话日志
- `*.output`：节点和部门输出
- `result_langgraph.json`：最终状态快照

全局还会生成：

- `tasks.db`：SQLite 状态库
- `history/YYYYMMDD.log`：跨任务汇总日志

---

## 文档索引

| 文档 | 说明 |
|------|------|
| [INSTALL.md](INSTALL.md) | 安装、配置与排障 |
| [SKILL.md](SKILL.md) | Skill 用法与触发建议 |
| [PROTOCOL.md](PROTOCOL.md) | 通信规则；前半为当前实现约束 |
| [ESCALATION.md](ESCALATION.md) | 升级/申诉设计；当前未完全自动化 |
| [models.json.example](models.json.example) | 模型配置模板 |
| [langgraph_workflow.py](langgraph_workflow.py) | LangGraph 主实现 |

---

## 已实现与未实现

已实现：

- 三省主链
- 门下省封驳循环
- 尚书省分阶段派发
- 礼部/兵部/工部工具执行
- 刑部/户部/吏部按需治理
- SQLite 与文件日志留痕

尚未完整自动化：

- “太子”节点
- 越级汇报自动路由
- 申诉状态机
- 公堂协商机制

---

## 注意事项

1. 当前运行依赖已经收敛到 `requirements.txt`，按文件安装即可。
2. 当前主工作流主要读取 `models.json`；`DASHSCOPE_API_KEY` 不是当前入口的主要配置方式。
3. `models.json.example` 中的 `provider`、`endpoint` 目前主要用于说明；当前代码实际固定了 OpenAI 兼容 `base_url`。
4. 升级/申诉机制见 [ESCALATION.md](ESCALATION.md)，但当前只是制度设计，不是完整运行时逻辑。

---

## 下一步开发

- 添加“公堂”协商机制
- 强化角色在阻塞时的上报与协作提示词
- 补齐升级/申诉自动化路由
- Web 可视化监控
