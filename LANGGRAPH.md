# LangGraph 集成指南

**目的**: 用 LangGraph 重构 edict 工作流，解决传统 Agent 框架的架构限制

---

## 📊 框架对比

| 特性 | CrewAI | LangGraph | edict 需求 |
|------|--------|-----------|-----------|
| **循环支持** | ❌ 单向流程 | ✅ 原生循环边 | ✅ 驳回重做 |
| **Agent 直连** | ❌ 需中转 | ✅ 任意节点连接 | ✅ 三省互连 |
| **多轮迭代** | ❌ 单轮 | ✅ 状态持久化 | ✅ 封驳重试 |
| **人工审核** | ❌ 无 | ✅ interrupt() | ✅ 门下省审核 |
| **层级结构** | ⚠️ 有限 | ✅ 子图嵌套 | ✅ 六部子图 |
| **状态管理** | ⚠️ 弱 | ✅ TypedDict+reducer | ✅ 任务状态 |
| **生产就绪** | ⚠️ 一般 | ✅ 持久化/监控 | ✅ 任务追踪 |

**结论**: LangGraph 完美匹配 edict 三省六部制架构 ✅

---

## 🏗️ 架构映射

### 现有 edict 架构

```
用户 → 太子 → 中书省 → 门下省 → 尚书省 → 六部 → 门下省验收
                    ↑封驳 ↓
```

### LangGraph 实现（`langgraph_workflow.py`）

```python
StateGraph(EdictState)
  ├─ Node: zhongshu_node (中书省)
  ├─ Node: menxia_node (门下省)
  ├─ Node: shangshu_node (尚书省) ← 通过 ThreadPoolExecutor 并行调度六部 LLM 调用
  ├─ Node: xingbu_node (刑部，按需触发)
  ├─ Node: hubu_node (户部，按需触发)
  ├─ Node: libu_admin_node (吏部，按需触发)
  ├─ Node: finalize_node (汇总)
  └─ Conditional Edge: menxia → zhongshu (封驳循环)
```

> ⚠️ **注意**：六部（礼部/兵部/工部）不作为独立图节点，而是在尚书省内部并行调度。
> 刑部/户部/吏部作为独立节点，根据关键词条件触发。

---

## 📦 安装依赖

```bash
# 基础安装
pip install langgraph langchain langchain-core

# DashScope (通义千问)
pip install langchain-dashscope

# 可选：LangSmith 监控
pip install langsmith
```

---

## 🎯 核心实现

### 1. 状态定义

```python
from typing import Literal, Annotated, TypedDict
import operator

class EdictState(TypedDict):
    """三省六部制状态"""
    task_id: str
    user_input: str
    plan: str
    review_status: Literal["pending", "approved", "rejected"]
    review_feedback: str
    revision_count: int
    ministry_results: dict
    final_output: str
```

### 2. 节点实现（中书省示例）

```python
def zhongshu_node(state: EdictState):
    """中书省：规划任务"""
    print("\n📜【中书省】正在规划任务...")
    
    system_prompt = """你是中书省令，负责规划任务。
请根据用户输入，生成详细的执行计划。"""
    
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=f"用户任务：{state['user_input']}")
    ]
    
    response = llm.invoke(messages)
    
    return {
        "plan": response.content,
        "revision_count": state.get("revision_count", 0) + 1
    }
```

### 3. 条件路由（门下省封驳）

```python
def after_review_route(state: EdictState) -> Literal["zhongshu_node", "shangshu_node", END]:
    """门下省审核后的路由决策"""
    max_revisions = 3
    
    if state["review_status"] == "rejected":
        if state.get("revision_count", 0) >= max_revisions:
            print(f"\n⚠️ 已达到最大修订次数 ({max_revisions})，任务终止")
            return END
        print(f"\n🔄 审核未通过，驳回重做（第 {state['revision_count']} 次修订）")
        return "zhongshu_node"  # 循环！
    
    elif state["review_status"] == "approved":
        print("\n✅ 审核通过，开始执行")
        return "shangshu_node"
    
    return END
```

### 4. 构建工作流

```python
from langgraph.graph import StateGraph, START, END

def create_edict_workflow():
    workflow = StateGraph(EdictState)
    
    # 添加节点
    workflow.add_node("zhongshu_node", zhongshu_node)
    workflow.add_node("menxia_node", menxia_node)
    workflow.add_node("shangshu_node", shangshu_node)
    
    # 添加边
    workflow.add_edge(START, "zhongshu_node")
    workflow.add_edge("zhongshu_node", "menxia_node")
    workflow.add_conditional_edges("menxia_node", after_review_route)
    workflow.add_edge("shangshu_node", END)
    
    return workflow.compile()
```

---

## 🔄 驳回重做机制

### `langgraph_workflow.py` 实现

### LangGraph 实现

```python
# 通过循环边自动实现
workflow.add_conditional_edges("menxia_node", after_review_route)
# menxia_node → zhongshu_node (封驳时)
# menxia_node → shangshu_node (通过时)
# menxia_node → END (超过重试次数)
```

**优势**: 
- 代码量减少 60%+
- 状态自动管理
- 可视化图结构
- 支持断点续跑

---

## 🗂️ 六部并行执行

六部（礼部/兵部/工部）由尚书省通过 `concurrent.futures.ThreadPoolExecutor` 并行调度，
每个部门调用 LLM 执行对应角色提示词，结果汇聚到 `ministry_results`。

```python
with ThreadPoolExecutor(max_workers=len(dispatch_map)) as executor:
    future_map = {
        executor.submit(execute_ministry, dept_key, task_desc, state, logger): dept_key
        for dept_key, task_desc in dispatch_map.items()
    }
    for future in as_completed(future_map):
        ministry_results[future_map[future]] = future.result()
```

> 刑部/户部/吏部作为独立 LangGraph 节点，根据任务关键词条件触发（见 `should_run_governance`）。

---

## 👤 Human-in-the-Loop（人工审核）

```python
from langgraph.graph import interrupt

def human_review_node(state: EdictState):
    """人工审核节点（可选，用于关键任务）"""
    print(f"当前计划：{state['plan']}")
    
    # 暂停，等待人工输入
    decision = interrupt("请输入审核结果 (approve/reject): ")
    
    if decision == "approve":
        return {"review_status": "approved"}
    else:
        feedback = interrupt("请输入修改意见：")
        return {
            "review_status": "rejected",
            "review_feedback": feedback
        }

# 添加到工作流
workflow.add_node("human_review", human_review_node)
workflow.add_edge("menxia_node", "human_review")
```

---

## 💾 持久化与断点续跑

```python
from langgraph.checkpoint import MemorySaver

# 创建持久化
memory = MemorySaver()
app = workflow.compile(checkpointer=memory)

# 运行（带 thread_id）
config = {"configurable": {"thread_id": "task-<timestamp>"}}
result = app.invoke(initial_state, config)

# 稍后恢复（从断点继续）
result = app.invoke(None, config)  # None 表示从上次状态继续
```

---

## 📊 流式输出

```python
# 流式获取每个节点的输出
for event in app.stream(initial_state):
    for node_name, output in event.items():
        print(f"[{node_name}] 完成")
        print(output[:200])
```

---

## 🐛 迁移注意事项

### 1. 状态管理差异

**现有 edict**: 文件系统存储 (`tasks/{task_id}/state.json`)  
**LangGraph**: 内存/数据库存储 (MemorySaver/SQLite)

**迁移方案**: 保留文件系统用于审计，LangGraph 用于执行

### 2. 执行引擎

**LangGraph 版本**：所有节点（三省 + 六部）均通过 LLM（DashScope `qwen-plus`）执行，
不依赖任何外部工具（已移除 opencode / nanobot）。

### 3. 错误处理

**现有 edict**: 超时重试 (`max_retries`)  
**LangGraph**: 内置重试机制 + 循环边

---

## 🚀 渐进式迁移方案

### 阶段 1: 并行运行（推荐）

```
已完成迁移，当前状态：
- 主工作流文件：`langgraph_workflow.py`
- 所有部门通过 LLM 执行，无外部工具依赖
- 接下来可集成 LangSmith 监控（见 BACKLOG L4）

---

## 📈 性能对比

| 指标 | 现有 edict | LangGraph | 改进 |
|------|-----------|-----------|------|
| **代码量** | ~1160 行 | ~650 行 | -44% |
| **驳回逻辑** | ~90 行 | ~20 行 | -78% |
| **状态管理** | ~200 行 | ~50 行 | -75% |
| **可视化** | ❌ | ✅ | + |
| **持久化** | 文件系统 | 内存/DB | + |
| **调试** | 日志 | LangSmith | + |

---

## 📖 参考资源

- **工作流代码**: [`langgraph_workflow.py`](langgraph_workflow.py)
- **已知问题**: [`BACKLOG.md`](BACKLOG.md)
- **官方文档**: https://docs.langchain.com/oss/python/langgraph/
- **示例库**: https://github.com/langchain-ai/langgraph/tree/main/examples

---

## ✅ 下一步行动

1. [x] 三省主链运行
2. [x] 驳回循环
3. [x] 六部并行执行（ThreadPoolExecutor）
4. [x] 越权防护（LEGAL_FLOWS + ROLE_CAPABILITIES）
5. [ ] 集成 LangSmith 监控（见 BACKLOG L4）
6. [ ] 全局超时（见 BACKLOG M3）
7. [ ] 门下省对六部结果的二次验收（见 BACKLOG L1）
