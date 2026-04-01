# edict-dev · 三省六部制 AI 协作系统

> **🏛️ 用 1300 年前的帝国智慧，设计现代 AI 协作架构**


---

## 📁 目录结构

```
edict-dev/
├── SKILL.md                    # Skill 定义和使用说明
├── README.md                   # 本文件
├── LANGGRAPH.md                # LangGraph 集成指南（重点）
├── PROTOCOL.md                 # 通信协议
├── SECURITY.md                 # 安全规范
├── ESCALATION.md               # 升级机制
├── models.json.example         # 模型配置示例
├── langgraph_workflow.py       # LangGraph 工作流 ⭐
├── roles/                      # 角色提示词
│   ├── zhongshu.md
│   ├── menxia.md
│   ├── shangshu.md
│   └── ...
├── tasks/                      # 任务状态目录（运行时自动创建）
├── history/                    # 历史日志（运行时自动创建）
└── work/                       # 实体工作区（AI通过Tool操作文件存放处）
```

---

### 快速开始

```bash
# 1. 安装依赖
pip install langgraph langchain langchain-openai

# 2. 设置 API Key (当前默认配置为兼容OpenAI接口的平台，如SiliconFlow)
export DASHSCOPE_API_KEY="your-api-key"

# 3. 运行工作流 - AI会自动在 work/ 文件夹下生成对应代码
python langgraph_workflow.py "开发一个 Flask API 项目"
```

### 核心特性

1. **驳回重做循环** - 门下省审核不通过自动返回中书省重做
2. **纯粹的职责分离** - 中书省专职项目架构设计，门下省把关审核，尚书省专职任务分发与调度
3. **原生 Tool Calling** - 各部门 AI 可直接操作 `work/` 工作区，兵部直接**写文件**，工部直接**执行指令**
4. **越权防护** - LEGAL_FLOWS + ROLE_CAPABILITIES 在代码层与提示词层双重验证执行边界
5. **尚书省并行调度** - 六部通过 ThreadPoolExecutor 并行执行
6. **状态持久化** - SQLite + LangGraph Checkpoint
7. **流式输出** - 实时查看执行进度

### 架构图

```
┌─────────────────────────────────────────────────────────┐
│                    START (用户输入)                      │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
            ┌────────────────┐
            │   中书省节点    │ ←───────────────────────┐
            │  (planning)    │                         │
            └────────┬───────┘                         │
                     │                                 │
                     ▼                                 │
            ┌────────────────┐     驳回                │
            │   门下省节点    │ ────────────────────────┘
            │   (review)     │
            └────────┬───────┘
                     │ 通过
                     ▼
            ┌────────────────┐
            │   尚书省节点    │
            │ (coordinator)  │
            └────────┬───────┘
                     │
         ┌───────────┼───────────┬──────────┐
         │           │           │          │
         ▼           ▼           ▼          ▼
    ┌────────┐  ┌────────┐  ┌────────┐  ┌────────┐
    │ 礼部子图│  │ 兵部子图│  │ 工部子图│  │ 户部子图│
    └────────┘  └────────┘  └────────┘  └────────┘
```

---

## 📚 文档索引

### 核心文档

| 文档 | 说明 | 适合人群 |
|------|------|---------|
| **[LANGGRAPH.md](LANGGRAPH.md)** | LangGraph 集成指南 | 开发者 |
| **[SKILL.md](SKILL.md)** | Skill 定义和使用 | 所有用户 |
| **[langgraph_workflow.py](langgraph_workflow.py)** | LangGraph 实现代码 | 开发者 |

### 文档

| 文档 | 说明 |
|------|------|
| **[PROTOCOL.md](PROTOCOL.md)** | 通信协议 |
| **[SECURITY.md](SECURITY.md)** | 安全规范 |

---

## 🚀 使用方式

### 运行 LangGraph 工作流

```bash
python langgraph_workflow.py "创建 Python Flask API 项目"
```

### 作为 Skill

```python
from edict.langgraph_workflow import run_langgraph_workflow

result = run_langgraph_workflow("帮我创建 Flask 项目")
```

---

## ✅ 完成状态

| 模块 | 状态 | 备注 |
|------|------|------|
| **三省主链** | ✅ 完成 | 中书省(架构设计)→门下省(审核)→尚书省(派发) |
| **驳回循环** | ✅ 完成 | 门下省→中书省 |
| **六部子图** | ✅ 完成 | 兵部(写代码)、工部(部署命令)、刑部(安全策略)等 |
| **工具挂载** | ✅ 完成 | 原生 Tool 调用: `read_file`, `write_file`, `execute_command` (对应角色解锁权限) |
| **持久化** | ✅ 完成 | SQLite + Checkpoint + `work/`实体目录资源映射 |
| **依赖管理** | ✅ 完成 | .venv + requirements.txt |

---

## ⚠️ 注意事项

1. **依赖安装**: 使用虚拟环境 `source .venv/bin/activate`
2. **首次运行**: 会自动创建 `tasks.db` SQLite 数据库

---

## 📖 参考资源

- **LangGraph 官方文档**: https://docs.langchain.com/oss/python/langgraph/
- **LangGraph 示例**: https://github.com/langchain-ai/langgraph/tree/main/examples
- **LangSmith 监控**: https://smith.langchain.com/
- **edict 主分支**: `<workspace>/skills/edict/`

## 下一步开发
- 添加一个“公堂”，每个agent都可以发言，将会同时发送给所有agent，用于开会讨论（记得写入各角色提示词）
- Web可视化监控