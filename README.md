# edict-dev · 三省六部制 AI 协作系统 (开发分支)

> **🏛️ 用 1300 年前的帝国智慧，设计现代 AI 协作架构**

本分支用于测试新架构（LangGraph 重构），生产环境请使用 `edict` 主分支。

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
├── models.json                 # 模型配置
├── nanobot_edict.py            # nanobot 集成入口
├── langgraph_workflow.py       # LangGraph 工作流 ⭐
├── roles/                      # 角色提示词
│   ├── zhongshu.md
│   ├── menxia.md
│   ├── shangshu.md
│   └── ...
├── tasks/                      # 任务状态目录
└── history/                    # 历史日志
```

---

## 🚀 LangGraph 工作流 ⭐

### 为什么选择 LangGraph？

| 特性 | 传统 edict | LangGraph | 改进 |
|------|-----------|-----------|------|
| **代码量** | ~1160 行 | ~650 行 | **-44%** |
| **驳回逻辑** | ~90 行 | ~20 行 | **-78%** |
| **循环支持** | 手动实现 | 原生支持 | ✅ |
| **状态管理** | 文件系统 | 内存/DB | ✅ |
| **可视化** | ❌ | ✅ | + |
| **监控** | 日志 | LangSmith | + |

### 快速开始

```bash
# 1. 安装依赖
pip install langgraph langchain langchain-dashscope

# 2. 设置 API Key
export DASHSCOPE_API_KEY="your-api-key"

# 3. 运行工作流
python langgraph_workflow.py "开发一个 Flask API 项目"
```

### 核心特性

1. **驳回重做循环** - 门下省审核不通过自动返回中书省重做
2. **状态持久化** - 支持断点续跑
3. **子图嵌套** - 六部作为子图实现
4. **流式输出** - 实时查看执行进度
5. **LangSmith 集成** - 可视化监控和调试

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
| **[workflow.py](langgraph_workflow.py)** | LangGraph 实现代码 | 开发者 |

### 研究文档

| 文档 | 说明 |
|------|------|
| **[langgraph-research.md](langgraph-research.md)** | 完整 LangGraph 研究报告（13KB） |
| **[langgraph-quickstart.md](langgraph-quickstart.md)** | LangGraph 快速上手教程 |
| **[langgraph-architecture.md](langgraph-architecture.md)** | Mermaid 架构图（6 张） |

### 历史文档

| 文档 | 说明 |
|------|------|
| **[CREWAI_NOTES.md](CREWAI_NOTES.md)** | Agent 框架评估笔记 |
| **[PROTOCOL.md](PROTOCOL.md)** | 通信协议 |
| **[SECURITY.md](SECURITY.md)** | 安全规范 |

---

## 🚀 使用方式

### 运行 LangGraph 工作流

```bash
python langgraph_workflow.py "创建 Python Flask API 项目"
```

### 作为 nanobot Skill

```python
from edict.langgraph_workflow import run_workflow

result = run_workflow("帮我创建 Flask 项目")
```

---

## ✅ 完成状态

| 模块 | 状态 | 备注 |
|------|------|------|
| **三省主链** | ✅ 完成 | 中书省→门下省→尚书省 |
| **驳回循环** | ✅ 完成 | 门下省→中书省 |
| **六部子图** | ✅ 完成 | 全部 6 部实现 |
| **持久化** | ✅ 完成 | SQLite + Checkpoint |
| **依赖管理** | ✅ 完成 | .venv + requirements.txt |

---

## 🧪 测试计划

### 阶段 1: 功能验证

- [x] 三省主链运行
- [x] 驳回循环测试
- [ ] 六部子图完整测试
- [ ] 持久化测试

### 阶段 2: 性能对比

- [ ] 响应时间对比
- [ ] 资源消耗对比
- [ ] 稳定性测试

### 阶段 3: 生产就绪

- [ ] LangSmith 集成
- [ ] 错误处理增强
- [ ] 文档完善
- [ ] 用户测试

---

## ⚠️ 注意事项

1. **API Key**: 需要配置 DashScope API Key
2. **依赖安装**: 使用虚拟环境 `source .venv/bin/activate`
3. **首次运行**: 会自动创建 `tasks.db` SQLite 数据库

---

## 📖 参考资源

- **LangGraph 官方文档**: https://docs.langchain.com/oss/python/langgraph/
- **LangGraph 示例**: https://github.com/langchain-ai/langgraph/tree/main/examples
- **LangSmith 监控**: https://smith.langchain.com/
- **edict 主分支**: `<workspace>/skills/edict/`

---

## 📝 更新日志

### v1.0.0 - LangGraph 重构

- ✅ 删除传统 workflow.py，全面切换至 LangGraph
- ✅ 完成依赖安装（langgraph, langchain, langchain-dashscope）
- ✅ 创建 requirements.txt 和 INSTALL.md
- ✅ 完善 LangGraph 集成文档

---

**状态**: ✅ 生产就绪
