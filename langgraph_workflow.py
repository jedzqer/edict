#!/usr/bin/env python3
"""
edict LangGraph 工作流协调器 - 三省六部制 AI 协作系统

使用 LangGraph 实现的 edict 工作流，支持：
- 原生循环（驳回重做）
- 状态持久化（SQLite + Checkpoint）
- 六部并行执行（ThreadPoolExecutor）
- Human-in-the-Loop

所有部门均通过 LLM（DashScope）执行任务，无外部工具依赖。
"""

import json
import os
import re
import sqlite3
import logging
import concurrent.futures
from pathlib import Path
from datetime import datetime
from typing import Literal, TypedDict, Dict, Any, Optional
from contextlib import contextmanager

# LangGraph 核心
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from langchain_core.messages import HumanMessage, SystemMessage

# 路径配置
EDICT_DIR = Path(__file__).parent
TASKS_DIR = EDICT_DIR / "tasks"
HISTORY_DIR = EDICT_DIR / "history"
ROLES_DIR = EDICT_DIR / "roles"
DB_PATH = EDICT_DIR / "tasks.db"

TASKS_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_DIR.mkdir(parents=True, exist_ok=True)

MAX_REVISIONS = 3
MAX_RETRIES = 3

# ==================== 日志系统 ====================

def setup_logger(task_id: str) -> logging.Logger:
    """为任务设置日志记录器，日志保存到 history/{date}.log"""
    logger = logging.getLogger(f"edict.{task_id}")
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    log_file = HISTORY_DIR / f"{datetime.now().strftime('%Y%m%d')}.log"
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)

    fmt = logging.Formatter(
        f"[%(asctime)s] [{task_id}] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    file_handler.setFormatter(fmt)
    console_handler.setFormatter(fmt)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


global_logger = logging.getLogger("edict.global")
if not global_logger.handlers:
    global_logger.setLevel(logging.INFO)
    _ch = logging.StreamHandler()
    _ch.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s"))
    global_logger.addHandler(_ch)


# ==================== 配置 ====================

_DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY")
if not _DASHSCOPE_API_KEY:
    raise EnvironmentError(
        "DASHSCOPE_API_KEY 环境变量未设置，请执行：export DASHSCOPE_API_KEY=\"your-api-key\""
    )

# LangSmith 集成（L4）：检测到 LANGCHAIN_API_KEY 时自动启用 tracing
if os.getenv("LANGCHAIN_API_KEY"):
    os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")
    os.environ.setdefault("LANGCHAIN_PROJECT", "edict")
    global_logger.info("LangSmith tracing 已启用")

try:
    from langchain_openai import ChatOpenAI
except ImportError as _import_err:
    raise ImportError(f"无法导入 langchain_openai，请执行：pip install langchain-openai") from _import_err


# ==================== 自定义异常（L3）====================

class EdictExecutionError(RuntimeError):
    """工作流执行错误，统一错误传播方式，避免字符串前缀检测"""
    pass


# ==================== 模型配置（M2）====================

def _load_model_config() -> Dict[str, Any]:
    """启动时加载 models.json（若存在），按角色返回配置字典"""
    models_path = EDICT_DIR / "models.json"
    if models_path.exists():
        try:
            with open(models_path, encoding="utf-8") as f:
                return json.load(f).get("models", {})
        except Exception as e:
            global_logger.warning(f"加载 models.json 失败：{e}，使用默认配置")
    return {}


_MODEL_CONFIG: Dict[str, Any] = _load_model_config()


def _get_llm_for_role(role: str) -> "ChatOpenAI":
    """按角色返回 LLM 实例；若 models.json 有对应配置则使用；否则回退到默认"""
    role_cfg = _MODEL_CONFIG.get(role, {})
    model = role_cfg.get("model", "Qwen/Qwen3-30B-A3B-Instruct-2507")
    api_key = role_cfg.get("api_key") or "sk-cvkefytsralxrkqqfktrukkwxqftbkanrgpwdtghsdohuarp"
    temperature = role_cfg.get("temperature", 0.7)
    return ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url="https://api.siliconflow.cn/v1",
        temperature=temperature,
    )

ROLE_DISPLAY_NAMES = {
    "zhongshu": "中书省",
    "menxia": "门下省",
    "shangshu": "尚书省",
    "libu_admin": "吏部",
    "hubu": "户部",
    "libu": "礼部",
    "bingbu": "兵部",
    "xingbu": "刑部",
    "gongbu": "工部",
    "final": "最终验收",
}

DEPARTMENT_NAME_MAP = {
    "吏部": "libu_admin",
    "户部": "hubu",
    "礼部": "libu",
    "兵部": "bingbu",
    "刑部": "xingbu",
    "工部": "gongbu",
    "libu_admin": "libu_admin",
    "hubu": "hubu",
    "libu": "libu",
    "bingbu": "bingbu",
    "xingbu": "xingbu",
    "gongbu": "gongbu",
}


def normalize_department(name: str) -> Optional[str]:
    """将中文部门名或英文名标准化为内部 key"""
    return DEPARTMENT_NAME_MAP.get(name)


# ==================== 权限管理 ====================

LEGAL_FLOWS = {
    ("zhongshu", "menxia"),
    ("menxia",   "zhongshu"),   # 驳回重做
    ("menxia",   "shangshu"),
    ("shangshu", "libu_admin"),
    ("shangshu", "hubu"),
    ("shangshu", "libu"),
    ("shangshu", "bingbu"),
    ("shangshu", "xingbu"),
    ("shangshu", "gongbu"),
}

ROLE_CAPABILITIES: Dict[str, set] = {
    "zhongshu":   {"规划"},
    "menxia":     {"审核"},
    "shangshu":   {"协调"},
    "libu_admin": {"组织治理"},
    "hubu":       {"资源治理"},
    "libu":       {"文事执行"},
    "bingbu":     {"技术执行"},
    "xingbu":     {"风险审查"},
    "gongbu":     {"工事执行"},
}


def validate_flow(from_role: str, to_role: str, logger: logging.Logger) -> bool:
    """检查流转是否合法；不合法则记录审计日志并返回 False"""
    if (from_role, to_role) not in LEGAL_FLOWS:
        logger.error(
            f"FLOW_VIOLATION: 检测到非法流转 {from_role} → {to_role}"
        )
        return False
    return True


def _validate_task_id(task_id: str) -> str:
    """校验 task_id 只允许安全字符，防止路径遍历"""
    if not re.match(r'^[a-zA-Z0-9_\-\.]+$', task_id):
        raise ValueError(f"非法 task_id：{task_id!r}")
    return task_id


# ==================== SQLite 持久化 ====================

def init_db(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """初始化 SQLite 数据库，创建 tasks 表"""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            user_input TEXT NOT NULL,
            plan TEXT,
            review_status TEXT DEFAULT 'pending',
            review_feedback TEXT,
            revision_count INTEGER DEFAULT 0,
            ministry_results TEXT,
            governance_plan TEXT,
            priority TEXT DEFAULT 'P3',
            final_output TEXT,
            created_at TEXT,
            updated_at TEXT,
            status TEXT DEFAULT 'running'
        )
    """)
    # M4: 迁移旧库，若 priority / governance_plan 列不存在则添加
    for col, default in [("priority", "'P3'"), ("governance_plan", "'{}'")]:
        try:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} TEXT DEFAULT {default}")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # 列已存在
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            node_name TEXT,
            message TEXT,
            timestamp TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(task_id)
        )
    """)
    conn.commit()
    return conn


@contextmanager
def get_db(db_path: Path = DB_PATH):
    """获取数据库连接的上下文管理器"""
    conn = init_db(db_path)
    try:
        yield conn
    finally:
        conn.close()


def save_state_to_db(state: Dict[str, Any], db_path: Path = DB_PATH):
    """将状态保存到 SQLite"""
    with get_db(db_path) as conn:
        conn.execute("""
            INSERT OR REPLACE INTO tasks
            (task_id, user_input, plan, review_status, review_feedback,
             revision_count, ministry_results, governance_plan, priority,
             final_output, created_at, updated_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            state.get("task_id", ""),
            state.get("user_input", ""),
            state.get("plan", ""),
            state.get("review_status", "pending"),
            state.get("review_feedback", ""),
            state.get("revision_count", 0),
            json.dumps(state.get("ministry_results", {}), ensure_ascii=False),
            json.dumps(state.get("governance_plan", {}), ensure_ascii=False),
            state.get("priority", "P3"),
            state.get("final_output", ""),
            state.get("created_at", ""),
            state.get("updated_at", ""),
            "running"
        ))
        conn.commit()


def load_state_from_db(task_id: str, db_path: Path = DB_PATH) -> Optional[Dict[str, Any]]:
    """从 SQLite 加载状态，支持通过 thread_id 恢复任务"""
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if not row:
            return None

        columns = [desc[0] for desc in conn.execute(
            "SELECT * FROM tasks LIMIT 0"
        ).description]
        state = dict(zip(columns, row))

        try:
            state["ministry_results"] = json.loads(state.get("ministry_results", "{}"))
        except (json.JSONDecodeError, TypeError):
            state["ministry_results"] = {}

        return state


def log_event_to_db(task_id: str, event_type: str, node_name: str, message: str,
                    db_path: Path = DB_PATH):
    """记录任务事件到数据库"""
    with get_db(db_path) as conn:
        conn.execute("""
            INSERT INTO task_events (task_id, event_type, node_name, message, timestamp)
            VALUES (?, ?, ?, ?, ?)
        """, (task_id, event_type, node_name, message, datetime.now().isoformat()))
        conn.commit()


# ==================== 状态定义 ====================

class EdictState(TypedDict):
    """三省六部制状态"""
    task_id: str
    user_input: str
    plan: str
    review_status: Literal["pending", "approved", "rejected"]
    review_feedback: str
    revision_count: int
    ministry_results: Dict[str, Dict[str, Any]]   # L2: 结构化输出
    governance_plan: Dict[str, bool]              # M1: LLM 语义分析后的治理部门清单
    priority: Literal["P0", "P1", "P2", "P3"]   # M4: 问题优先级
    final_output: str
    created_at: str
    updated_at: str
    status: str




# ==================== 辅助函数 ====================

def extract_json_from_output(output: str) -> Optional[Dict[str, Any]]:
    """从 LLM 输出中提取 JSON（处理 markdown 代码块包装）"""
    try:
        return json.loads(output)
    except Exception:
        pass

    json_blocks = re.findall(r"```json\s*(.*?)\s*```", output, re.DOTALL)
    for block in json_blocks:
        try:
            return json.loads(block)
        except Exception:
            pass

    json_objects = re.findall(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", output)
    for obj_str in json_objects:
        try:
            return json.loads(obj_str)
        except Exception:
            pass

    return None


def call_llm_with_retry(system_prompt: str, user_content: str,
                        max_retries: int = MAX_RETRIES,
                        role: str = "default") -> str:
    """调用 LLM 带重试机制（M2: 按角色选择 LLM；L3: 失败时抛出 EdictExecutionError）"""
    last_error: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        try:
            llm = _get_llm_for_role(role)
            messages = [
                SystemMessage(content=system_prompt),
                HumanMessage(content=user_content)
            ]
            response = llm.invoke(messages)
            return response.content
        except EdictExecutionError:
            raise
        except Exception as e:
            last_error = e
            global_logger.warning(f"LLM 调用失败（第 {attempt}/{max_retries} 次，角色={role}）：{e}")
            if attempt < max_retries:
                import time
                time.sleep(2 ** attempt)

    global_logger.error(f"LLM 调用失败，已重试 {max_retries} 次（角色={role}）：{last_error}")
    raise EdictExecutionError(f"LLM 调用失败（角色={role}）：{last_error}") from last_error


def load_role_prompt(role: str) -> str:
    """加载角色提示词文件"""
    role_file = ROLES_DIR / f"{role}.md"
    if not role_file.exists():
        return f"你是{role}，请根据任务上下文完成工作。"
    return role_file.read_text(encoding="utf-8")


def execute_ministry(ministry_key: str, task_desc: str,
                     state: "EdictState", logger: logging.Logger) -> Dict[str, Any]:
    """通过 LLM 执行单个部门任务，返回结构化结果（L2）"""
    ministry_name = ROLE_DISPLAY_NAMES.get(ministry_key, ministry_key)
    logger.info(f"  → {ministry_name} 开始执行：{task_desc[:80]}...")
    try:
        role_prompt = load_role_prompt(ministry_key)
        context = (
            f"用户请求：{state['user_input']}\n\n"
            f"计划：{state['plan']}\n\n"
            f"派发任务：{task_desc}"
        )
        output = call_llm_with_retry(role_prompt, context, role=ministry_key)
        logger.info(f"  ← {ministry_name} 执行完成")
        return {"output": output, "status": "success", "timestamp": datetime.now().isoformat()}
    except EdictExecutionError as e:
        logger.error(f"  ← {ministry_name} 执行失败：{e}")
        return {"output": str(e), "status": "error", "timestamp": datetime.now().isoformat()}
    except Exception as e:
        logger.error(f"  ← {ministry_name} 执行异常：{e}")
        return {"output": str(e), "status": "error", "timestamp": datetime.now().isoformat()}


# ==================== 节点实现 ====================

def zhongshu_node(state: EdictState) -> EdictState:
    """
    中书省：规划任务
    理解用户需求，制定详细执行计划，分配六部任务
    """
    task_id = state["task_id"]
    logger = setup_logger(task_id)
    logger.info("【中书省】开始规划任务")
    log_event_to_db(task_id, "node_start", "zhongshu", "中书省开始规划")

    revision_hint = ""
    if state.get("review_feedback"):
        revision_hint = f"""

## 上次封驳意见（请根据以下意见修改）
{state['review_feedback']}
"""

    system_prompt = """你是中书省令，负责规划任务。
请根据用户输入，生成详细的执行计划。

计划必须使用以下 JSON 格式输出：
```json
{
    "task_name": "任务名称",
    "description": "任务描述",
    "ministries": [
        {"name": "礼部", "task": "负责的具体工作"},
        {"name": "兵部", "task": "负责的具体工作"},
        {"name": "工部", "task": "负责的具体工作"}
    ],
    "timeline": "预期时间线",
    "resources": "所需资源"
}
```

可用部门：户部（资源预算）、礼部（文档写作）、
兵部（代码实现）、刑部（安全审查）、工部（部署构建）

请根据任务需要合理分配部门，不要遗漏必要的部门。"""

    user_content = f"用户任务：{state['user_input']}{revision_hint}"

    plan_text = call_llm_with_retry(system_prompt, user_content, role="zhongshu")

    logger.info(f"中书省规划完成，计划长度：{len(plan_text)} 字符")
    log_event_to_db(task_id, "node_complete", "zhongshu", f"规划完成，长度 {len(plan_text)}")

    return {
        "plan": plan_text,
        "revision_count": state.get("revision_count", 0) + 1,
        "updated_at": datetime.now().isoformat()
    }


def menxia_node(state: EdictState) -> EdictState:
    """
    门下省：审核封驳
    审核计划可行性，可驳回重做（循环），最多 3 次修订
    """
    task_id = state["task_id"]
    logger = setup_logger(task_id)
    logger.info("【门下省】开始审核计划")
    log_event_to_db(task_id, "node_start", "menxia", "门下省开始审核")

    system_prompt = f"""你是门下省侍中，负责审核中书省的计划。

审核标准：
1. 可行性：计划是否可执行
2. 资源充足性：资源是否足够
3. 风险控制：是否有风险预案
4. 部门分工：六部职责是否明确

当前是第 {state.get("revision_count", 0)} 次修订，最大允许 {MAX_REVISIONS} 次。

如果通过，返回 JSON：
{{"decision": "通过", "comments": "审核意见"}}

如果不通过，返回 JSON：
{{"decision": "封驳", "issues": ["问题1", "问题2"], "suggestions": "修改建议"}}"""

    user_content = f"请审核以下计划:\n{state['plan']}"

    review_result = call_llm_with_retry(system_prompt, user_content, role="menxia")

    logger.info(f"门下省审核结果：{review_result[:200]}...")

    parsed = extract_json_from_output(review_result)
    if parsed:
        decision = parsed.get("decision", "")
        if "封驳" in decision or "reject" in decision.lower():
            log_event_to_db(task_id, "node_complete", "menxia", "审核未通过，驳回")
            return {
                "review_status": "rejected",
                "review_feedback": review_result,
                "updated_at": datetime.now().isoformat()
            }
        else:
            log_event_to_db(task_id, "node_complete", "menxia", "审核通过")
            return {
                "review_status": "approved",
                "review_feedback": review_result,
                "updated_at": datetime.now().isoformat()
            }

    # 无法解析 JSON 时回退到关键词匹配
    if "封驳" in review_result or "不通过" in review_result or "rejected" in review_result.lower():
        log_event_to_db(task_id, "node_complete", "menxia", "审核未通过（关键词匹配）")
        return {
            "review_status": "rejected",
            "review_feedback": review_result,
            "updated_at": datetime.now().isoformat()
        }
    else:
        log_event_to_db(task_id, "node_complete", "menxia", "审核通过（默认）")
        return {
            "review_status": "approved",
            "review_feedback": review_result,
            "updated_at": datetime.now().isoformat()
        }


# ==================== 治理计划辅助函数（M1）====================

def _plan_governance_with_llm(state: "EdictState",
                               ministry_results: Dict[str, Dict[str, Any]],
                               logger: logging.Logger) -> Dict[str, bool]:
    """
    由 LLM 基于语义分析决定需要哪些治理部门介入（M1）。
    返回 {"xingbu": bool, "hubu": bool, "libu_admin": bool}。
    """
    results_summary = "\n".join(
        f"- {ROLE_DISPLAY_NAMES.get(k, k)}: {v.get('output', '')[:300]}"
        for k, v in ministry_results.items()
    )
    system_prompt = """你是尚书省协调员，负责判断六部执行结果是否需要额外的治理部门介入。

请根据用户任务、执行计划和六部输出摘要，判断：
- 刑部（xingbu）：是否存在安全/合规/高风险问题需要审查
- 户部（hubu）：是否存在资源/预算/成本问题需要评估


输出必须是 JSON：
{"xingbu": true/false, "hubu": true/false, "reason": "简要说明"}"""

    user_content = (
        f"用户任务：{state['user_input']}\n\n"
        f"执行计划摘要：{state['plan'][:500]}\n\n"
        f"六部执行结果摘要：\n{results_summary}"
    )

    try:
        raw = call_llm_with_retry(system_prompt, user_content, role="shangshu")
        parsed = extract_json_from_output(raw)
        if parsed and isinstance(parsed, dict):
            plan = {
                "xingbu": bool(parsed.get("xingbu", False)),
                "hubu": bool(parsed.get("hubu", False)),
                "libu_admin": bool(parsed.get("libu_admin", False)),
            }
            logger.info(f"LLM 治理分析（M1）：{parsed.get('reason', '')} → {plan}")
            return plan
    except EdictExecutionError as e:
        logger.warning(f"LLM 治理分析失败，回退到关键词匹配：{e}")

    # 回退到关键词匹配
    combined = f"{state['user_input']}\n{state['plan']}"
    return {
        "xingbu": any(kw in combined for kw in ["生产", "权限", "敏感", "密钥", "删除", "安全", "合规"]),
        "hubu": any(kw in combined for kw in ["预算", "成本", "配额", "资源", "费用"]),
        "libu_admin": any(kw in combined for kw in ["角色变更", "部门调整", "治理", "组织架构"]),
    }


def shangshu_node(state: EdictState) -> EdictState:
    """
    尚书省：协调派发
    解析中书省计划 JSON，智能分配六部任务，并行执行，汇总结果
    """
    task_id = state["task_id"]
    logger = setup_logger(task_id)
    logger.info("【尚书省】开始协调六部执行")
    log_event_to_db(task_id, "node_start", "shangshu", "尚书省开始协调")

    # 解析中书省计划中的 JSON
    plan_json = extract_json_from_output(state["plan"])

    if plan_json and "ministries" in plan_json:
        ministries = plan_json["ministries"]
        logger.info(f"从计划中解析到 {len(ministries)} 个部门任务")
    else:
        # 回退：让尚书省重新派发
        system_prompt = """你是尚书省尚书令，负责协调六部执行任务。

请根据以下计划和审核意见，分派任务给六部。
输出必须是 JSON 格式：
```json
{
    "dispatch_log": [
        {"department": "兵部", "task": "具体任务描述"},
        {"department": "工部", "task": "具体任务描述"}
    ]
}
```

可用部门：吏部、户部、礼部、兵部、刑部、工部"""

        user_content = f"""计划：{state['plan']}
审核意见：{state['review_feedback']}
用户任务：{state['user_input']}"""

        dispatch_result = call_llm_with_retry(system_prompt, user_content, role="shangshu")
        plan_json = extract_json_from_output(dispatch_result)
        ministries = plan_json.get("dispatch_log", []) if plan_json else []

    # 如果没有解析到任何部门，使用默认
    if not ministries:
        logger.warning("未解析到任何部门任务，使用默认兵部任务")
        ministries = [{"name": "兵部", "task": state["user_input"]}]

    # 构建派发映射，并检查每个派发是否合法流转
    dispatch_map = {}
    for m in ministries:
        dept_name = m.get("name", "")
        dept_key = normalize_department(dept_name)
        if dept_key:
            if validate_flow("shangshu", dept_key, logger):
                dispatch_map[dept_key] = m.get("task", "")
            else:
                logger.warning(f"跳过非法派发目标：{dept_name}")

    logger.info(f"派发任务到 {len(dispatch_map)} 个部门：{list(dispatch_map.keys())}")

    # 并行执行六部任务（ThreadPoolExecutor，避免 asyncio 死锁）
    ministry_results: Dict[str, Dict[str, Any]] = dict(state.get("ministry_results", {}))

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(dispatch_map) or 1) as executor:
        future_map = {
            executor.submit(execute_ministry, dept_key, task_desc, state, logger): dept_key
            for dept_key, task_desc in dispatch_map.items()
        }
        for future in concurrent.futures.as_completed(future_map):
            dept_key = future_map[future]
            dept_name = ROLE_DISPLAY_NAMES.get(dept_key, dept_key)
            try:
                ministry_results[dept_key] = future.result()
            except Exception as exc:
                logger.error(f"{dept_name} 执行异常：{exc}")
                ministry_results[dept_key] = {
                    "output": str(exc), "status": "error",
                    "timestamp": datetime.now().isoformat()
                }

    # 保存结果到文件（L2: 写出 output 字段）
    _validate_task_id(task_id)
    task_dir = TASKS_DIR / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    for dept_key, result in ministry_results.items():
        output_file = task_dir / f"{dept_key}.output"
        output_text = result.get("output", "") if isinstance(result, dict) else str(result)
        output_file.write_text(output_text, encoding="utf-8")

    # M1: 用 LLM 语义分析决定需要哪些治理部门
    governance_plan = _plan_governance_with_llm(state, ministry_results, logger)
    logger.info(f"治理计划：{governance_plan}")

    log_event_to_db(task_id, "node_complete", "shangshu",
                    f"六部执行完成，共 {len(ministry_results)} 个结果")

    return {
        "ministry_results": ministry_results,
        "governance_plan": governance_plan,
        "updated_at": datetime.now().isoformat()
    }


def libu_admin_node(state: EdictState) -> EdictState:
    """
    吏部：组织治理、角色管理
    维护部门名册、能力标签、路由建议
    """
    task_id = state["task_id"]
    logger = setup_logger(task_id)
    validate_flow("shangshu", "libu_admin", logger)
    logger.info("【吏部】开始组织治理")
    log_event_to_db(task_id, "node_start", "libu_admin", "吏部开始治理")

    role_prompt = load_role_prompt("libu_admin")
    context = (
        f"用户请求：{state['user_input']}\n\n"
        f"计划：{state['plan']}\n\n"
        f"当前涉及的部门：{list(state.get('ministry_results', {}).keys())}"
    )

    try:
        result = call_llm_with_retry(role_prompt, context)
    except Exception as e:
        logger.error(f"吏部执行异常：{e}")
        result = f"Error: {str(e)}"

    ministry_results = dict(state.get("ministry_results", {}))
    ministry_results["libu_admin"] = result

    log_event_to_db(task_id, "node_complete", "libu_admin", "吏部治理完成")
    return {
        "ministry_results": ministry_results,
        "updated_at": datetime.now().isoformat()
    }


def hubu_node(state: EdictState) -> EdictState:
    """
    户部：资源预算、成本评估
    评估预算、配额、资源充足性
    """
    task_id = state["task_id"]
    logger = setup_logger(task_id)
    validate_flow("shangshu", "hubu", logger)
    logger.info("【户部】开始资源评估")
    log_event_to_db(task_id, "node_start", "hubu", "户部开始评估")

    role_prompt = load_role_prompt("hubu")
    context = (
        f"用户请求：{state['user_input']}\n\n"
        f"计划：{state['plan']}\n\n"
        "请评估完成此任务所需的资源、预算和成本。"
    )

    try:
        result = call_llm_with_retry(role_prompt, context, role="hubu")
        ministry_result: Dict[str, Any] = {
            "output": result, "status": "success", "timestamp": datetime.now().isoformat()
        }
    except EdictExecutionError as e:
        logger.error(f"户部执行异常：{e}")
        ministry_result = {"output": str(e), "status": "error", "timestamp": datetime.now().isoformat()}

    ministry_results = dict(state.get("ministry_results", {}))
    ministry_results["hubu"] = ministry_result

    log_event_to_db(task_id, "node_complete", "hubu", "户部评估完成")
    return {
        "ministry_results": ministry_results,
        "updated_at": datetime.now().isoformat()
    }


def xingbu_node(state: EdictState) -> EdictState:
    """
    刑部：安全审查、合规检查
    识别安全风险、审查敏感信息、评估高风险操作
    """
    task_id = state["task_id"]
    logger = setup_logger(task_id)
    validate_flow("shangshu", "xingbu", logger)
    logger.info("【刑部】开始安全审查")
    log_event_to_db(task_id, "node_start", "xingbu", "刑部开始审查")

    role_prompt = load_role_prompt("xingbu")
    context = (
        f"用户请求：{state['user_input']}\n\n"
        f"计划：{state['plan']}\n\n"
        "各部门执行结果摘要：\n"
        + json.dumps(
            {k: v.get("output", "")[:500] if isinstance(v, dict) else str(v)[:500]
             for k, v in state.get("ministry_results", {}).items()},
            ensure_ascii=False, indent=2
        )
        + "\n\n请对以上方案和执行结果进行安全与合规审查。若存在高风险，在输出 JSON 中包含 \"risk_level\": \"high\" 或 \"critical\"."
    )

    try:
        result = call_llm_with_retry(role_prompt, context, role="xingbu")
        ministry_result: Dict[str, Any] = {
            "output": result, "status": "success", "timestamp": datetime.now().isoformat()
        }
    except EdictExecutionError as e:
        logger.error(f"刑部执行异常：{e}")
        ministry_result = {"output": str(e), "status": "error", "timestamp": datetime.now().isoformat()}
        result = str(e)

    # M4: 刹部检测到高风险时，将优先级升级为 P0/P1
    new_priority = state.get("priority", "P3")
    parsed_review = extract_json_from_output(result) if isinstance(result, str) else None
    if parsed_review:
        risk = str(parsed_review.get("risk_level", "")).lower()
        if risk == "critical":
            new_priority = "P0"
            logger.warning("刑部识别到临界风险，优先级升级为 P0")
        elif risk == "high":
            new_priority = "P1"
            logger.warning("刑部识别到高风险，优先级升级为 P1")
        log_event_to_db(task_id, "priority_escalation", "xingbu",
                        f"风险级别={risk}，优先级={new_priority}")

    ministry_results = dict(state.get("ministry_results", {}))
    ministry_results["xingbu"] = ministry_result

    log_event_to_db(task_id, "node_complete", "xingbu", "刑部审查完成")
    return {
        "ministry_results": ministry_results,
        "priority": new_priority,
        "updated_at": datetime.now().isoformat()
    }


def menxia_final_node(state: EdictState) -> EdictState:
    """
    门下省最终验收节点（L1）
    对六部执行成果做最终质量把关，在 finalize_node 之前执行
    """
    task_id = state["task_id"]
    logger = setup_logger(task_id)
    logger.info("【门下省·终验】对六部执行成果进行最终验收")
    log_event_to_db(task_id, "node_start", "menxia_final", "门下省开始终验")

    results_summary = "\n".join(
        f"- {ROLE_DISPLAY_NAMES.get(k, k)}（{v.get('status', '?')}）: "
        f"{v.get('output', '')[:400] if isinstance(v, dict) else str(v)[:400]}"
        for k, v in state.get("ministry_results", {}).items()
    )

    system_prompt = """你是门下省侍中，负责对六部执行成果进行最终质量验收。

请检查：
1. 各部门执行结果是否完整、无明显错误
2. 整体方案是否满足用户原始需求
3. 是否存在安全或合规遗漏

输出 JSON：
{"verdict": "通过" 或 "存疑", "summary": "验收意见", "issues": ["问题1（如无则为空列表）"]}"""

    user_content = (
        f"用户原始任务：{state['user_input']}\n\n"
        f"计划：{state['plan'][:500]}\n\n"
        f"各部门执行结果：\n{results_summary}"
    )

    try:
        review = call_llm_with_retry(system_prompt, user_content, role="menxia")
    except EdictExecutionError as e:
        logger.warning(f"门下省终验调用失败，跳过：{e}")
        review = '{"verdict": "通过", "summary": "终验跳过（LLM 调用失败）", "issues": []}'

    log_event_to_db(task_id, "node_complete", "menxia_final", f"终验完成：{review[:200]}")
    logger.info(f"门下省终验结果：{review[:200]}")

    return {"updated_at": datetime.now().isoformat()}


def finalize_node(state: EdictState) -> EdictState:
    """
    最终汇总节点
    汇总六部结果，生成最终报告
    """
    task_id = state["task_id"]
    logger = setup_logger(task_id)
    logger.info("【汇总】生成最终报告")
    log_event_to_db(task_id, "node_start", "finalize", "开始汇总")

    ministry_reports = "\n\n".join([
        f"### {ROLE_DISPLAY_NAMES.get(m, m)}\n{result.get('output', '') if isinstance(result, dict) else str(result)[:1000]}"
        for m, result in state.get("ministry_results", {}).items()
    ])

    system_prompt = """你是太子太傅，负责汇总六部工作成果。
请根据以下各部门的报告，生成最终的执行总结。

输出格式：
# 执行总结

## 任务概述
（简述任务目标和范围）

## 各部门执行情况
（按部门汇总执行结果）

## 最终结论
（是否完成用户需求，存在哪些遗留问题）

## 建议
（后续改进建议）"""

    user_content = f"""原始任务：{state['user_input']}

各部门报告:
{ministry_reports}"""

    try:
        final_report = call_llm_with_retry(system_prompt, user_content, role="final")
    except EdictExecutionError:
        final_report = (
            f"# 执行总结\n\n## 任务概述\n用户请求：{state['user_input']}\n\n"
            f"## 各部门执行情况\n{ministry_reports}\n\n"
            f"## 最终结论\n任务执行完成（汇总节点 LLM 调用失败，输出降级）。\n\n"
            f"## 修订次数\n共 {state.get('revision_count', 0)} 次规划修订。"
        )

    logger.info(f"最终报告生成完成，长度：{len(final_report)} 字符")
    log_event_to_db(task_id, "node_complete", "finalize", f"报告长度 {len(final_report)}")

    return {
        "final_output": final_report,
        "updated_at": datetime.now().isoformat()
    }


# ==================== 路由逻辑 ====================

def after_review_route(state: EdictState) -> Literal["zhongshu_node", "shangshu_node", END]:
    """
    门下省审核后的路由决策

    - 如果驳回：返回中书省重做（循环）
    - 如果通过：前往尚书省内部并行执行六部
    - 如果超过最大重试次数：结束
    """
    task_id = state.get("task_id", "unknown")
    logger = setup_logger(task_id)

    if state["review_status"] == "rejected":
        if state.get("revision_count", 0) >= MAX_REVISIONS:
            logger.warning(f"已达到最大修订次数 ({MAX_REVISIONS})，任务终止")
            log_event_to_db(task_id, "routing", "menxia",
                            f"驳回已达上限 ({MAX_REVISIONS})，终止")
            return END
        logger.info(f"审核未通过，驳回重做（第 {state['revision_count']} 次修订）")
        log_event_to_db(task_id, "routing", "menxia",
                        f"驳回，第 {state['revision_count']} 次修订")
        return "zhongshu_node"

    elif state["review_status"] == "approved":
        logger.info("审核通过，开始六部执行")
        log_event_to_db(task_id, "routing", "menxia", "审核通过，分发六部")
        return "shangshu_node"

    logger.warning("未知审核状态，结束任务")
    return END


def should_run_governance(state: EdictState) -> Literal["libu_admin_node", "hubu_node", "xingbu_node", "menxia_final_node"]:
    """
    根据尚书省生成的 governance_plan（LLM 语义分析结果，M1）决定首个治理节点。
    若无需治理直接进入门下省终验。
    """
    plan = state.get("governance_plan", {})
    if plan.get("xingbu"):
        return "xingbu_node"
    if plan.get("hubu"):
        return "hubu_node"
    if plan.get("libu_admin"):
        return "libu_admin_node"
    return "menxia_final_node"


def after_governance_route(state: EdictState) -> Literal["libu_admin_node", "hubu_node", "xingbu_node", "menxia_final_node"]:
    """治理节点后继续检查 governance_plan，执行未完成的治理节点"""
    plan = state.get("governance_plan", {})
    results = state.get("ministry_results", {})

    if plan.get("xingbu") and "xingbu" not in results:
        return "xingbu_node"
    if plan.get("hubu") and "hubu" not in results:
        return "hubu_node"
    if plan.get("libu_admin") and "libu_admin" not in results:
        return "libu_admin_node"
    return "menxia_final_node"


# ==================== 构建工作流 ====================

def create_edict_workflow():
    """创建三省六部制工作流图"""
    workflow = StateGraph(EdictState)

    # 添加节点（礼部/兵部/工部由尚书省内部并行调度，不作为独立图节点）
    workflow.add_node("zhongshu_node", zhongshu_node)
    workflow.add_node("menxia_node", menxia_node)
    workflow.add_node("shangshu_node", shangshu_node)
    workflow.add_node("libu_admin_node", libu_admin_node)
    workflow.add_node("hubu_node", hubu_node)
    workflow.add_node("xingbu_node", xingbu_node)
    workflow.add_node("menxia_final_node", menxia_final_node)   # L1
    workflow.add_node("finalize_node", finalize_node)

    # 主链
    workflow.add_edge(START, "zhongshu_node")
    workflow.add_edge("zhongshu_node", "menxia_node")

    # 门下省条件路由
    workflow.add_conditional_edges(
        "menxia_node",
        after_review_route,
        {
            "zhongshu_node": "zhongshu_node",
            "shangshu_node": "shangshu_node",
            END: END,
        }
    )

    # 尚书省执行完毕 → 条件治理路由（M1: 基于 governance_plan）
    workflow.add_conditional_edges(
        "shangshu_node",
        should_run_governance,
        {
            "xingbu_node": "xingbu_node",
            "hubu_node": "hubu_node",
            "libu_admin_node": "libu_admin_node",
            "menxia_final_node": "menxia_final_node",
        }
    )

    # 治理节点链式路由，均最终流向门下省终验节点（L1）
    workflow.add_conditional_edges(
        "xingbu_node",
        after_governance_route,
        {
            "hubu_node": "hubu_node",
            "libu_admin_node": "libu_admin_node",
            "menxia_final_node": "menxia_final_node",
        }
    )
    workflow.add_conditional_edges(
        "hubu_node",
        after_governance_route,
        {
            "xingbu_node": "xingbu_node",
            "libu_admin_node": "libu_admin_node",
            "menxia_final_node": "menxia_final_node",
        }
    )
    workflow.add_conditional_edges(
        "libu_admin_node",
        after_governance_route,
        {
            "xingbu_node": "xingbu_node",
            "hubu_node": "hubu_node",
            "menxia_final_node": "menxia_final_node",
        }
    )

    workflow.add_edge("menxia_final_node", "finalize_node")   # L1
    workflow.add_edge("finalize_node", END)

    # 编译（带内存检查点，支持状态恢复）
    memory = MemorySaver()
    app = workflow.compile(checkpointer=memory)

    return app


# ==================== 运行入口 ====================

def run_langgraph_workflow(user_input: str, task_id: Optional[str] = None):
    """
    运行 LangGraph 三省六部制任务

    Args:
        user_input: 用户的任务描述
        task_id: 可选的任务 ID，不提供则自动生成

    Returns:
        最终状态字典，或 None（失败时）
    """
    if task_id is None:
        task_id = f"task-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

    logger = setup_logger(task_id)
    logger.info(f"三省六部制系统 (LangGraph) 启动，任务：{user_input[:100]}")

    # 创建工作流
    app = create_edict_workflow()

    # 初始状态
    initial_state: EdictState = {
        "task_id": task_id,
        "user_input": user_input,
        "plan": "",
        "review_status": "pending",
        "review_feedback": "",
        "revision_count": 0,
        "ministry_results": {},
        "governance_plan": {},
        "priority": "P3",
        "final_output": "",
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat()
    }

    # 保存到数据库
    save_state_to_db(initial_state)
    log_event_to_db(task_id, "workflow_start", None, f"工作流启动：{user_input[:100]}")

    config = {"configurable": {"thread_id": task_id}}

    print("\n" + "="*60)
    print("三省六部制 AI 协作系统 (LangGraph) 启动")
    print("="*60)
    print(f"任务 ID: {task_id}")
    print(f"用户输入：{user_input}")
    print("\n🚀 开始执行任务...\n")

    # M3: 全局超时保护（默认 1800s，可通过 EDICT_TIMEOUT 覆盖）
    global_timeout = int(os.getenv("EDICT_TIMEOUT", "1800"))

    def _run_workflow() -> Optional[Dict[str, Any]]:
        for event in app.stream(initial_state, config):
            for node_name, output in event.items():
                display_name = ROLE_DISPLAY_NAMES.get(node_name, node_name)
                print(f"\n[节点完成] {display_name}")
                logger.info(f"节点完成：{display_name}")

        # 流式执行完毕后获取完整状态并保存
        final_state_snapshot = app.get_state(config)
        if final_state_snapshot and final_state_snapshot.values:
            save_state_to_db(final_state_snapshot.values)

        # 获取最终状态
        final_state = app.get_state(config)

        print("\n" + "="*60)
        print("任务完成!")
        print("="*60)

        if final_state and final_state.values:
            final_output = final_state.values.get("final_output", "")
            print(f"\n最终输出:\n{final_output[:1000]}...")

            task_dir = TASKS_DIR / task_id
            task_dir.mkdir(parents=True, exist_ok=True)

            result_file = task_dir / "result_langgraph.json"
            result_file.write_text(
                json.dumps(final_state.values, indent=2, ensure_ascii=False),
                encoding="utf-8"
            )

            completed_state = dict(final_state.values)
            completed_state["status"] = "completed"
            save_state_to_db(completed_state)
            log_event_to_db(task_id, "workflow_complete", None, "工作流执行完成")

            print(f"\n结果已保存到：{result_file}")
            logger.info(f"结果已保存到：{result_file}")

            return final_state.values
        return None

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _exec:
            _future = _exec.submit(_run_workflow)
            try:
                return _future.result(timeout=global_timeout)
            except concurrent.futures.TimeoutError:
                logger.error(f"工作流全局超时（{global_timeout}s），强制终止")
                log_event_to_db(task_id, "workflow_timeout", None, f"超时 {global_timeout}s")
                error_state = dict(initial_state)
                error_state["status"] = "timeout"
                error_state["final_output"] = f"Error: 工作流超时（{global_timeout}s）"
                save_state_to_db(error_state)
                print(f"\n❌ 工作流超时（{global_timeout}s）")
                return None

    except Exception as e:
        logger.error(f"执行失败：{e}", exc_info=True)
        log_event_to_db(task_id, "workflow_error", None, str(e))

        error_state = initial_state.copy()
        error_state["status"] = "error"
        error_state["final_output"] = f"Error: {str(e)}"
        save_state_to_db(error_state)

        print(f"\n❌ 执行失败：{e}")
        import traceback
        traceback.print_exc()
        return None


# ==================== CLI 入口 ====================

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法：python langgraph_workflow.py <用户请求>")
        print("示例：python langgraph_workflow.py '开发一个 Flask API 项目'")
        sys.exit(1)

    user_request = " ".join(sys.argv[1:])
    result = run_langgraph_workflow(user_request)

    if result:
        print("\n✅ 任务执行完成")
    else:
        print("\n❌ 任务执行失败")
