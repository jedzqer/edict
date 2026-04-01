#!/usr/bin/env python3
"""
edict LangGraph 工作流协调器 - 三省六部制 AI 协作系统 (LangGraph 版本)

使用 LangGraph 重构的 edict 工作流，支持：
- 原生循环（驳回重做）
- 状态持久化（SQLite + Checkpoint）
- 子图嵌套（六部）
- Human-in-the-Loop

执行引擎：
- 中书省、门下省、尚书省、六部：LangGraph Node
- 兵部、工部：opencode（通过 tool 集成）
- 其他部门：nanobot agent（通过 tool 集成）
"""

import json
import os
import re
import sqlite3
import subprocess
import asyncio
import logging
from pathlib import Path
from datetime import datetime
from typing import Literal, Annotated, TypedDict, Dict, Any, Optional, List
from contextlib import contextmanager
import operator

# LangGraph 核心
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool

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
OPENCODE_TIMEOUT = 600
NANOBOT_TIMEOUT = 180

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

try:
    from langchain_dashscope import ChatDashScope
    llm = ChatDashScope(
        model="qwen-plus",
        dashscope_api_key=os.getenv("DASHSCOPE_API_KEY", "your-api-key")
    )
except Exception as e:
    print(f"⚠️  DashScope 初始化失败：{e}")
    print("请使用备用 LLM 或设置 DASHSCOPE_API_KEY 环境变量")
    llm = None

BINGBU_USE_OPENCODE = True
GONGBU_USE_OPENCODE = True

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
            final_output TEXT,
            created_at TEXT,
            updated_at TEXT,
            status TEXT DEFAULT 'running'
        )
    """)
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
             revision_count, ministry_results, final_output, created_at, updated_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            state.get("task_id", ""),
            state.get("user_input", ""),
            state.get("plan", ""),
            state.get("review_status", "pending"),
            state.get("review_feedback", ""),
            state.get("revision_count", 0),
            json.dumps(state.get("ministry_results", {}), ensure_ascii=False),
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
    """三省六部制状态（与传统版本兼容）"""
    task_id: str
    user_input: str
    plan: str
    review_status: Literal["pending", "approved", "rejected"]
    review_feedback: str
    revision_count: int
    ministry_results: Dict[str, str]
    final_output: str
    created_at: str
    updated_at: str


class MinistryState(TypedDict):
    """六部子图状态"""
    task: str
    project_dir: Optional[str]
    output: str
    context: str


# ==================== 工具定义 ====================

@tool
def run_opencode_task(task_description: str, project_dir: Optional[str] = None) -> str:
    """执行 opencode 代码任务（兵部/工部使用）"""
    env = os.environ.copy()
    env["PATH"] = f"/usr/sbin:/usr/bin:/sbin:/bin:{env.get('PATH', '')}"

    cmd = ["opencode", "run", "--format", "json"]
    if project_dir and Path(project_dir).exists():
        cmd.extend(["--dir", str(project_dir)])
    cmd.append(task_description)

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=OPENCODE_TIMEOUT, env=env
        )
        if result.returncode == 0:
            try:
                output_data = json.loads(result.stdout)
                return json.dumps(output_data, indent=2, ensure_ascii=False)
            except json.JSONDecodeError:
                return result.stdout
        else:
            return f"Error: {result.stderr}\nOutput: {result.stdout}"
    except subprocess.TimeoutExpired:
        return f"Error: 执行超时（{OPENCODE_TIMEOUT // 60} 分钟）"
    except Exception as e:
        return f"Error: {str(e)}"


@tool
def spawn_nanobot_agent(role: str, input_context: str, task_id: str) -> str:
    """调用 nanobot agent 执行角色任务"""
    task_dir = TASKS_DIR / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    output_file = task_dir / f"{role}.output"

    role_prompt_file = ROLES_DIR / f"{role}.md"
    if not role_prompt_file.exists():
        return f"Error: 角色提示词不存在：{role}"

    role_prompt = role_prompt_file.read_text(encoding="utf-8")

    agent_task = f"""{role_prompt}

---
## 任务上下文
{input_context}

---
## 输出要求
1. 将完整输出写入：{output_file}
2. 输出必须是有效的 JSON 格式
3. 完成后确保输出文件存在

开始执行。
"""

    try:
        env = os.environ.copy()
        env["PATH"] = f"/usr/sbin:/usr/bin:/sbin:/bin:{env.get('PATH', '')}"

        result = subprocess.run(
            ["nanobot", "agent", "--message", agent_task, "--no-markdown"],
            capture_output=True,
            text=True,
            timeout=NANOBOT_TIMEOUT,
            env=env,
        )

        if result.returncode == 0:
            output_content = result.stdout
            output_file.write_text(output_content, encoding="utf-8")
            return output_content
        else:
            return f"Error: {result.stderr}\nOutput: {result.stdout}"
    except subprocess.TimeoutExpired:
        return f"Error: 执行超时（{NANOBOT_TIMEOUT} 秒）"
    except Exception as e:
        return f"Error: {str(e)}"


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
                        max_retries: int = MAX_RETRIES) -> str:
    """调用 LLM 带重试机制"""
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            messages = [
                SystemMessage(content=system_prompt),
                HumanMessage(content=user_content)
            ]
            response = llm.invoke(messages)
            return response.content
        except Exception as e:
            last_error = e
            global_logger.warning(f"LLM 调用失败（第 {attempt}/{max_retries} 次）：{e}")
            if attempt < max_retries:
                import time
                time.sleep(2 ** attempt)

    global_logger.error(f"LLM 调用失败，已重试 {max_retries} 次：{last_error}")
    return f"Error: LLM 调用失败 - {last_error}"


def load_role_prompt(role: str) -> str:
    """加载角色提示词文件"""
    role_file = ROLES_DIR / f"{role}.md"
    if not role_file.exists():
        return f"你是{role}，请根据任务上下文完成工作。"
    return role_file.read_text(encoding="utf-8")


def execute_ministry_async(ministry_key: str, task_desc: str,
                           state: EdictState, logger: logging.Logger) -> str:
    """同步执行六部任务（用于 asyncio.gather 包装）"""
    task_id = state["task_id"]
    ministry_name = ROLE_DISPLAY_NAMES.get(ministry_key, ministry_key)
    logger.info(f"  → {ministry_name} 开始执行：{task_desc[:80]}...")

    try:
        if ministry_key in ("bingbu", "gongbu"):
            role_prompt = load_role_prompt(ministry_key)
            full_task = f"{role_prompt}\n\n---\n## 任务\n{task_desc}\n\n用户原始请求：{state['user_input']}"
            result = run_opencode_task.invoke({
                "task_description": full_task,
                "project_dir": None
            })
        else:
            role_prompt = load_role_prompt(ministry_key)
            context = f"用户请求：{state['user_input']}\n\n计划：{state['plan']}\n\n派发任务：{task_desc}"
            result = spawn_nanobot_agent.invoke({
                "role": ministry_key,
                "input_context": context,
                "task_id": task_id
            })
    except Exception as e:
        logger.error(f"  ← {ministry_name} 执行异常：{e}")
        result = f"Error: {str(e)}"

    logger.info(f"  ← {ministry_name} 执行完成")
    return result


async def execute_ministry_coro(ministry_key: str, task_desc: str,
                                 state: EdictState, logger: logging.Logger) -> str:
    """异步包装六部任务执行"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, execute_ministry_async, ministry_key, task_desc, state, logger
    )


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

可用部门：吏部（组织治理）、户部（资源预算）、礼部（文档写作）、
兵部（代码实现）、刑部（安全审查）、工部（部署构建）

请根据任务需要合理分配部门，不要遗漏必要的部门。"""

    user_content = f"用户任务：{state['user_input']}{revision_hint}"

    if llm:
        plan_text = call_llm_with_retry(system_prompt, user_content)
    else:
        plan_text = spawn_nanobot_agent.invoke({
            "role": "zhongshu",
            "input_context": user_content,
            "task_id": task_id
        })

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

    if llm:
        review_result = call_llm_with_retry(system_prompt, user_content)
    else:
        review_result = spawn_nanobot_agent.invoke({
            "role": "menxia",
            "input_context": user_content,
            "task_id": task_id
        })

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

        if llm:
            dispatch_result = call_llm_with_retry(system_prompt, user_content)
        else:
            dispatch_result = spawn_nanobot_agent.invoke({
                "role": "shangshu",
                "input_context": user_content,
                "task_id": task_id
            })

        plan_json = extract_json_from_output(dispatch_result)
        ministries = plan_json.get("dispatch_log", []) if plan_json else []

    # 如果没有解析到任何部门，使用默认
    if not ministries:
        logger.warning("未解析到任何部门任务，使用默认兵部任务")
        ministries = [{"name": "兵部", "task": state["user_input"]}]

    # 构建派发映射
    dispatch_map = {}
    for m in ministries:
        dept_name = m.get("name", "")
        dept_key = normalize_department(dept_name)
        if dept_key:
            dispatch_map[dept_key] = m.get("task", "")

    logger.info(f"派发任务到 {len(dispatch_map)} 个部门：{list(dispatch_map.keys())}")

    # 并行执行六部任务
    ministry_results = dict(state.get("ministry_results", {}))

    async def run_all_ministries():
        tasks = []
        for dept_key, task_desc in dispatch_map.items():
            tasks.append(
                execute_ministry_coro(dept_key, task_desc, state, logger)
            )
        return await asyncio.gather(*tasks, return_exceptions=True)

    results = asyncio.run(run_all_ministries())

    for (dept_key, _), result in zip(dispatch_map.items(), results):
        dept_name = ROLE_DISPLAY_NAMES.get(dept_key, dept_key)
        if isinstance(result, Exception):
            logger.error(f"{dept_name} 执行异常：{result}")
            ministry_results[dept_key] = f"Error: {result}"
        else:
            ministry_results[dept_key] = str(result)

    # 保存结果到文件
    task_dir = TASKS_DIR / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    for dept_key, result in ministry_results.items():
        output_file = task_dir / f"{dept_key}.output"
        output_file.write_text(result, encoding="utf-8")

    log_event_to_db(task_id, "node_complete", "shangshu",
                    f"六部执行完成，共 {len(ministry_results)} 个结果")

    return {
        "ministry_results": ministry_results,
        "updated_at": datetime.now().isoformat()
    }


def libu_admin_node(state: EdictState) -> EdictState:
    """
    吏部：组织治理、角色管理
    维护部门名册、能力标签、路由建议
    """
    task_id = state["task_id"]
    logger = setup_logger(task_id)
    logger.info("【吏部】开始组织治理")
    log_event_to_db(task_id, "node_start", "libu_admin", "吏部开始治理")

    role_prompt = load_role_prompt("libu_admin")
    context = f"""用户请求：{state['user_input']}

计划：{state['plan']}

当前涉及的部门：{list(state.get('ministry_results', {}).keys())}"""

    try:
        result = spawn_nanobot_agent.invoke({
            "role": "libu_admin",
            "input_context": f"{role_prompt}\n\n---\n## 任务上下文\n{context}",
            "task_id": task_id
        })
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
    logger.info("【户部】开始资源评估")
    log_event_to_db(task_id, "node_start", "hubu", "户部开始评估")

    role_prompt = load_role_prompt("hubu")
    context = f"""用户请求：{state['user_input']}

计划：{state['plan']}

请评估完成此任务所需的资源、预算和成本。"""

    try:
        result = spawn_nanobot_agent.invoke({
            "role": "hubu",
            "input_context": f"{role_prompt}\n\n---\n## 任务上下文\n{context}",
            "task_id": task_id
        })
    except Exception as e:
        logger.error(f"户部执行异常：{e}")
        result = f"Error: {str(e)}"

    ministry_results = dict(state.get("ministry_results", {}))
    ministry_results["hubu"] = result

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
    logger.info("【刑部】开始安全审查")
    log_event_to_db(task_id, "node_start", "xingbu", "刑部开始审查")

    role_prompt = load_role_prompt("xingbu")
    context = f"""用户请求：{state['user_input']}

计划：{state['plan']}

各部门执行结果摘要：
{json.dumps({k: v[:500] for k, v in state.get('ministry_results', {}).items()}, ensure_ascii=False, indent=2)}

请对以上方案和执行结果进行安全与合规审查。"""

    try:
        result = spawn_nanobot_agent.invoke({
            "role": "xingbu",
            "input_context": f"{role_prompt}\n\n---\n## 任务上下文\n{context}",
            "task_id": task_id
        })
    except Exception as e:
        logger.error(f"刑部执行异常：{e}")
        result = f"Error: {str(e)}"

    ministry_results = dict(state.get("ministry_results", {}))
    ministry_results["xingbu"] = result

    log_event_to_db(task_id, "node_complete", "xingbu", "刑部审查完成")
    return {
        "ministry_results": ministry_results,
        "updated_at": datetime.now().isoformat()
    }


def liubu_node(state: EdictState) -> EdictState:
    """礼部执行节点：文档写作、翻译润色"""
    task_id = state["task_id"]
    logger = setup_logger(task_id)
    logger.info("【礼部】开始文事执行")
    log_event_to_db(task_id, "node_start", "libu", "礼部开始执行")

    role_prompt = load_role_prompt("libu")
    plan_json = extract_json_from_output(state["plan"])

    task_desc = "负责文事、文档编写、翻译润色工作"
    if plan_json and "ministries" in plan_json:
        for m in plan_json["ministries"]:
            if m.get("name") == "礼部":
                task_desc = m.get("task", task_desc)
                break

    context = f"""用户请求：{state['user_input']}

计划：{state['plan']}

派发任务：{task_desc}"""

    try:
        result = spawn_nanobot_agent.invoke({
            "role": "libu",
            "input_context": f"{role_prompt}\n\n---\n## 任务上下文\n{context}",
            "task_id": task_id
        })
    except Exception as e:
        logger.error(f"礼部执行异常：{e}")
        result = f"Error: {str(e)}"

    ministry_results = dict(state.get("ministry_results", {}))
    ministry_results["libu"] = result

    log_event_to_db(task_id, "node_complete", "libu", "礼部执行完成")
    return {
        "ministry_results": ministry_results,
        "updated_at": datetime.now().isoformat()
    }


def bingbu_node(state: EdictState) -> EdictState:
    """兵部执行节点：代码实现、技术调试（使用 opencode）"""
    task_id = state["task_id"]
    logger = setup_logger(task_id)
    logger.info("【兵部】开始技术执行")
    log_event_to_db(task_id, "node_start", "bingbu", "兵部开始执行")

    role_prompt = load_role_prompt("bingbu")
    plan_json = extract_json_from_output(state["plan"])

    task_desc = "负责代码开发、技术实现"
    if plan_json and "ministries" in plan_json:
        for m in plan_json["ministries"]:
            if m.get("name") == "兵部":
                task_desc = m.get("task", task_desc)
                break

    full_task = f"""{role_prompt}

---
## 代码开发任务

用户请求：{state['user_input']}

派发任务：{task_desc}

计划详情：{state['plan']}

⚠️ 重要要求：
1. 根据任务描述进行代码开发、调试或技术实现
2. 可以使用新建文件或修改现有文件
3. 修改后必须保存文件
4. 在输出中明确列出修改/创建的文件

🚨 错误报告要求：
1. 遇到任何错误必须立即停止并报告
2. 禁止跳过步骤或静默失败
3. 在输出中明确说明遇到的问题和错误信息"""

    try:
        result = run_opencode_task.invoke({
            "task_description": full_task,
            "project_dir": None
        })
    except Exception as e:
        logger.error(f"兵部执行异常：{e}")
        result = f"Error: {str(e)}"

    ministry_results = dict(state.get("ministry_results", {}))
    ministry_results["bingbu"] = result

    log_event_to_db(task_id, "node_complete", "bingbu", "兵部执行完成")
    return {
        "ministry_results": ministry_results,
        "updated_at": datetime.now().isoformat()
    }


def gongbu_node(state: EdictState) -> EdictState:
    """工部执行节点：部署构建、运维发布（使用 opencode）"""
    task_id = state["task_id"]
    logger = setup_logger(task_id)
    logger.info("【工部】开始工事执行")
    log_event_to_db(task_id, "node_start", "gongbu", "工部开始执行")

    role_prompt = load_role_prompt("gongbu")
    plan_json = extract_json_from_output(state["plan"])

    task_desc = "负责部署构建、运维发布"
    if plan_json and "ministries" in plan_json:
        for m in plan_json["ministries"]:
            if m.get("name") == "工部":
                task_desc = m.get("task", task_desc)
                break

    full_task = f"""{role_prompt}

---
## 项目部署运维任务

用户请求：{state['user_input']}

派发任务：{task_desc}

计划详情：{state['plan']}

⚠️ 重要要求：
1. 执行实际的部署/构建/运维操作
2. 在输出中明确部署步骤和结果
3. 验证部署是否成功

🚨 错误报告要求：
1. 遇到任何错误必须立即停止并报告
2. 禁止跳过步骤或静默失败
3. 提供回滚方案"""

    try:
        result = run_opencode_task.invoke({
            "task_description": full_task,
            "project_dir": None
        })
    except Exception as e:
        logger.error(f"工部执行异常：{e}")
        result = f"Error: {str(e)}"

    ministry_results = dict(state.get("ministry_results", {}))
    ministry_results["gongbu"] = result

    log_event_to_db(task_id, "node_complete", "gongbu", "工部执行完成")
    return {
        "ministry_results": ministry_results,
        "updated_at": datetime.now().isoformat()
    }


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
        f"### {ROLE_DISPLAY_NAMES.get(m, m)}\n{result[:1000]}"
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

    if llm:
        final_report = call_llm_with_retry(system_prompt, user_content)
    else:
        final_report = f"""# 执行总结

## 任务概述
用户请求：{state['user_input']}

## 各部门执行情况
{ministry_reports}

## 最终结论
任务执行完成。

## 修订次数
共 {state.get('revision_count', 0)} 次规划修订。"""

    logger.info(f"最终报告生成完成，长度：{len(final_report)} 字符")
    log_event_to_db(task_id, "node_complete", "finalize", f"报告长度 {len(final_report)}")

    return {
        "final_output": final_report,
        "updated_at": datetime.now().isoformat()
    }


# ==================== 路由逻辑 ====================

def after_review_route(state: EdictState) -> Literal["zhongshu_node", "shangshu_node", "libu_admin_node", "hubu_node", "xingbu_node", "liubu_node", "bingbu_node", "gongbu_node", END]:
    """
    门下省审核后的路由决策

    - 如果驳回：返回中书省重做（循环）
    - 如果通过：前往六部执行（并行）
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


def should_run_governance(state: EdictState) -> Literal["libu_admin_node", "hubu_node", "xingbu_node", "finalize_node"]:
    """
    条件触发吏部/户部/刑部的路由决策

    触发规则：
    - 刑部：涉及生产环境、权限、敏感数据、删除操作等高风险关键词
    - 户部：涉及预算、成本、配额、资源限制等关键词
    - 吏部：涉及角色变更、能力登记、部门调整等关键词
    """
    combined_text = f"{state['user_input']}\n{state['plan']}\n{state.get('review_feedback', '')}"

    xingbu_keywords = [
        "生产", "prod", "权限", "敏感", "密码", "密钥", "token",
        "删除", "drop", "truncate", "rm -rf", "sudo", "root",
        "安全", "合规", "审计", "越权", "注入", "泄露", "高风险",
        "上线", "发布", "配置替换", "数据库迁移"
    ]
    if any(kw in combined_text for kw in xingbu_keywords):
        return "xingbu_node"

    hubu_keywords = [
        "预算", "成本", "配额", "资源", "限量", "多模型",
        "费用", "token 限制", "调用次数", "配额不足", "经济",
        "优化成本", "资源评估"
    ]
    if any(kw in combined_text for kw in hubu_keywords):
        return "hubu_node"

    libu_admin_keywords = [
        "角色变更", "能力登记", "部门调整", "名册", "替补",
        "路由", "可用性", "治理", "组织架构"
    ]
    if any(kw in combined_text for kw in libu_admin_keywords):
        return "libu_admin_node"

    return "finalize_node"


def after_governance_route(state: EdictState) -> Literal["libu_admin_node", "hubu_node", "xingbu_node", "finalize_node"]:
    """治理节点后的路由，检查是否还有其他治理节点需要执行"""
    combined_text = f"{state['user_input']}\n{state['plan']}"
    results = state.get("ministry_results", {})

    xingbu_keywords = [
        "生产", "prod", "权限", "敏感", "密码", "密钥", "token",
        "删除", "drop", "安全", "合规", "审计", "上线", "发布"
    ]
    if "xingbu" not in results and any(kw in combined_text for kw in xingbu_keywords):
        return "xingbu_node"

    hubu_keywords = ["预算", "成本", "配额", "资源", "费用", "经济"]
    if "hubu" not in results and any(kw in combined_text for kw in hubu_keywords):
        return "hubu_node"

    libu_admin_keywords = ["角色变更", "能力登记", "部门调整", "治理", "组织架构"]
    if "libu_admin" not in results and any(kw in combined_text for kw in libu_admin_keywords):
        return "libu_admin_node"

    return "finalize_node"


# ==================== 构建工作流 ====================

def create_edict_workflow():
    """创建三省六部制工作流图"""
    workflow = StateGraph(EdictState)

    # 添加节点
    workflow.add_node("zhongshu_node", zhongshu_node)
    workflow.add_node("menxia_node", menxia_node)
    workflow.add_node("shangshu_node", shangshu_node)
    workflow.add_node("libu_admin_node", libu_admin_node)
    workflow.add_node("hubu_node", hubu_node)
    workflow.add_node("liubu_node", liubu_node)
    workflow.add_node("bingbu_node", bingbu_node)
    workflow.add_node("xingbu_node", xingbu_node)
    workflow.add_node("gongbu_node", gongbu_node)
    workflow.add_node("finalize_node", finalize_node)

    # 添加边
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

    # 尚书省 → 六部并行
    workflow.add_edge("shangshu_node", "liubu_node")
    workflow.add_edge("shangshu_node", "bingbu_node")
    workflow.add_edge("shangshu_node", "gongbu_node")

    # 六部 → 条件治理
    workflow.add_edge("liubu_node", "xingbu_node")
    workflow.add_edge("bingbu_node", "xingbu_node")
    workflow.add_edge("gongbu_node", "xingbu_node")

    # 治理节点 → 汇总
    workflow.add_edge("xingbu_node", "finalize_node")
    workflow.add_edge("libu_admin_node", "finalize_node")
    workflow.add_edge("hubu_node", "finalize_node")

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
        "final_output": "",
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat()
    }

    # 保存到数据库
    save_state_to_db(initial_state)
    log_event_to_db(task_id, "workflow_start", None, f"工作流启动：{user_input[:100]}")

    config = {"configurable": {"thread_id": task_id}}

    print("\n" + "🐈" * 25)
    print("nanobot 三省六部制系统 (LangGraph 版本) 启动")
    print("🐈" * 25)
    print(f"任务 ID: {task_id}")
    print(f"用户输入：{user_input}")
    print("\n🚀 开始执行任务...\n")

    try:
        for event in app.stream(initial_state, config):
            for node_name, output in event.items():
                display_name = ROLE_DISPLAY_NAMES.get(node_name, node_name)
                print(f"\n[节点完成] {display_name}")
                logger.info(f"节点完成：{display_name}")

                # 每次节点更新后保存状态到 DB
                save_state_to_db(output)

        # 获取最终状态
        final_state = app.get_state(config)

        print("\n" + "🐈" * 25)
        print("任务完成!")
        print("🐈" * 25)

        if final_state and final_state.value:
            final_output = final_state.value.get("final_output", "")
            print(f"\n最终输出:\n{final_output[:1000]}...")

            # 保存结果
            task_dir = TASKS_DIR / task_id
            task_dir.mkdir(parents=True, exist_ok=True)

            result_file = task_dir / "result_langgraph.json"
            result_file.write_text(
                json.dumps(final_state.value, indent=2, ensure_ascii=False),
                encoding="utf-8"
            )

            # 更新数据库状态
            final_state.value["status"] = "completed"
            save_state_to_db(final_state.value)
            log_event_to_db(task_id, "workflow_complete", None, "工作流执行完成")

            print(f"\n结果已保存到：{result_file}")
            logger.info(f"结果已保存到：{result_file}")

            return final_state.value
        else:
            logger.error("最终状态为空")
            return None

    except Exception as e:
        logger.error(f"执行失败：{e}", exc_info=True)
        log_event_to_db(task_id, "workflow_error", None, str(e))

        # 更新数据库状态为失败
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
