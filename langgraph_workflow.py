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
import time
import threading
import concurrent.futures
from pathlib import Path
from datetime import datetime
from typing import Literal, TypedDict, Dict, Any, Optional
from contextlib import contextmanager

# LangGraph 核心
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from langchain_core.messages import HumanMessage, SystemMessage

import subprocess
from langchain_core.tools import tool


def snapshot_work_tree() -> Dict[str, Dict[str, Any]]:
    """返回 work/ 目录当前文件快照，用于部门间基于真实产物协作。"""
    snapshot: Dict[str, Dict[str, Any]] = {}
    for path in sorted(WORK_DIR.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(WORK_DIR).as_posix()
        stat = path.stat()
        snapshot[rel] = {
            "size": stat.st_size,
            "mtime": int(stat.st_mtime),
        }
    return snapshot


def diff_work_tree(before: Dict[str, Dict[str, Any]],
                   after: Dict[str, Dict[str, Any]]) -> Dict[str, list[str]]:
    """比较前后快照，提取新增、修改、删除文件。"""
    created = sorted([p for p in after if p not in before])
    deleted = sorted([p for p in before if p not in after])
    modified = sorted([
        p for p in after
        if p in before and after[p] != before[p]
    ])
    return {"created": created, "modified": modified, "deleted": deleted}


def format_work_tree(snapshot: Dict[str, Dict[str, Any]], limit: int = 40) -> str:
    """将 work/ 文件清单格式化为紧凑文本。"""
    if not snapshot:
        return "work/ 目录当前为空。"
    items = []
    for idx, (path, meta) in enumerate(snapshot.items()):
        if idx >= limit:
            items.append(f"... 共 {len(snapshot)} 个文件")
            break
        items.append(f"- {path} ({meta['size']} bytes)")
    return "\n".join(items)


def format_file_changes(changes: Dict[str, list[str]]) -> str:
    """将文件变更摘要格式化为文本。"""
    lines: list[str] = []
    for key, title in [("created", "新增"), ("modified", "修改"), ("deleted", "删除")]:
        files = changes.get(key, [])
        if files:
            lines.append(f"{title}：{', '.join(files)}")
    return "\n".join(lines) if lines else "无文件变更。"


def sample_work_file_contents(limit_files: int = 8, max_chars_per_file: int = 1200) -> str:
    """采样 work/ 中的关键文件内容，供终验节点基于真实产物判断。"""
    if not WORK_DIR.exists():
        return "work/ 目录不存在。"

    preferred_suffixes = [
        ".md", ".txt", ".py", ".html", ".css", ".js", ".json",
        ".yml", ".yaml", ".toml", ".ini", ".sh"
    ]
    preferred_paths: list[Path] = []
    fallback_paths: list[Path] = []

    for path in sorted(WORK_DIR.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(WORK_DIR).as_posix()
        if any(part.startswith(".") for part in path.relative_to(WORK_DIR).parts):
            continue
        if path.suffix.lower() in preferred_suffixes:
            preferred_paths.append(path)
        else:
            fallback_paths.append(path)

    selected = (preferred_paths + fallback_paths)[:limit_files]
    if not selected:
        return "work/ 目录当前无可读取文件。"

    samples: list[str] = []
    for path in selected:
        rel = path.relative_to(WORK_DIR).as_posix()
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            samples.append(f"## {rel}\n[读取失败] {e}")
            continue

        snippet = content[:max_chars_per_file]
        if len(content) > max_chars_per_file:
            snippet += "\n... [内容已截断]"
        samples.append(f"## {rel}\n{snippet}")

    return "\n\n".join(samples)


def extract_work_paths(text: str) -> list[str]:
    """从任务描述中提取 work/ 下的目标路径。"""
    if not text:
        return []
    matches = re.findall(r"work/[A-Za-z0-9_\-./<>一-龥]+", text)
    seen: set[str] = set()
    paths: list[str] = []
    for match in matches:
        cleaned = match.rstrip("，。；：,.;:）)]}>")
        if cleaned not in seen:
            seen.add(cleaned)
            paths.append(cleaned)
    return paths


def normalize_workspace_relpath(file_path: str) -> str:
    """将任务中出现的路径归一化为相对 work/ 根目录的安全路径。"""
    normalized = re.sub(r"^(?:\.?/)?work/", "", (file_path or "").strip())
    normalized = normalized.lstrip("/")
    normalized = re.sub(r"/{2,}", "/", normalized)
    if normalized in {"", ".", ".."}:
        raise ValueError("empty workspace path")
    if any(part == ".." for part in Path(normalized).parts):
        raise ValueError(f"unsafe workspace path: {file_path}")
    return normalized


def resolve_workspace_path(file_path: str) -> Path:
    """把任务路径映射到 work/ 目录下的真实路径。"""
    normalized = normalize_workspace_relpath(file_path)
    path = WORK_DIR / normalized
    resolved = path.resolve()
    if not str(resolved).startswith(str(WORK_DIR.resolve())):
        raise ValueError(f"workspace path escapes work/: {file_path}")
    return resolved


def rewrite_task_paths_for_workspace(text: str) -> str:
    """把任务中的 work/... 路径重写为相对 backend 根目录的路径，避免生成 work/work。"""
    if not text:
        return text

    seen: set[str] = set()

    def repl(match: re.Match[str]) -> str:
        raw = match.group(0)
        if raw in seen:
            return raw
        seen.add(raw)
        try:
            return normalize_workspace_relpath(raw)
        except ValueError:
            return raw

    return re.sub(r"work/[A-Za-z0-9_\-./<>一-龥]+", repl, text)


def is_rate_limit_error(text: str) -> bool:
    """粗略识别限流错误，便于退避和降级。"""
    lowered = (text or "").lower()
    markers = ["429", "rate limit", "rate_limit", "tpm limit", "too many requests"]
    return any(marker in lowered for marker in markers)


def repair_mirrored_workspace_paths() -> list[str]:
    """修复被错误写到 work/<abs-workspace-path>/... 下的镜像文件。"""
    moved: list[str] = []
    workspace_prefix = WORK_DIR.resolve().as_posix().lstrip("/") + "/"

    for path in sorted(WORK_DIR.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(WORK_DIR).as_posix()
        if not rel.startswith(workspace_prefix):
            continue

        suffix = rel[len(workspace_prefix):]
        if not suffix:
            continue

        target = resolve_workspace_path(suffix)
        if target == path:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        path.replace(target)
        moved.append(f"{rel} -> {suffix}")

    # 清理因镜像写入残留的空目录
    for path in sorted(WORK_DIR.rglob("*"), reverse=True):
        if path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass
    return moved


def infer_project_root(text: str) -> Optional[str]:
    """从计划或任务文本中推断统一项目根目录，如 work/my-app。"""
    paths = extract_work_paths(text)
    candidates = [p for p in paths if p.count("/") >= 1]
    for candidate in candidates:
        parts = candidate.split("/")
        if len(parts) >= 2 and parts[1] and "<项目名>" not in candidate:
            return "/".join(parts[:2])
    return None


def normalize_dispatch_department(task_desc: str, dept_key: str) -> str:
    """保守纠偏派工：仅在出现非常明确的前后端/文档/部署越权时才改派。"""
    text = task_desc.lower()
    task_paths = [path.lower() for path in extract_work_paths(task_desc)]

    # 这些部门不应被关键词纠偏，避免把资源治理/安全审计/组织治理误改派。
    if dept_key in {"hubu", "xingbu", "libu_admin"}:
        return dept_key

    # 若任务本身是审查、裁决、确认类工作，也不要靠关键词挪给礼部。
    if any(marker in text for marker in ["尚书省", "门下省", "中书省", "审查当前", "核验", "确认"]):
        return dept_key

    frontend_markers = [
        ".html", ".css", ".js", "前端", "页面", "样式", "组件", "交互", "浏览器", "静态资源",
        "canvas", "game.js", "index.html", "ui", "ux"
    ]
    backend_markers = [
        ".py", "后端", "flask", "fastapi", "api", "接口", "服务逻辑", "数据库访问", "数据持久化",
        "server.py", "app.py", "sqlite"
    ]
    doc_markers = [
        "readme", "文档", "接口文档", "部署文档", "用户手册", "说明文案", "操作说明", "使用说明"
    ]
    deploy_markers = [
        "deploy", "start.sh", "docker", "compose", "nginx", "启动脚本", "部署", "构建", "发布", "回滚", "运维"
    ]

    has_frontend = any(marker in text for marker in frontend_markers)
    has_backend = any(marker in text for marker in backend_markers)
    has_docs = any(marker in text for marker in doc_markers)
    has_deploy = any(marker in text for marker in deploy_markers)
    docs_only_paths = bool(task_paths) and all(
        "/docs/" in path or path.endswith("/readme.md") or path.endswith("readme.md")
        for path in task_paths
    )

    # 只修正明确的职责冲突，不根据模糊词跨部门改派。
    # 当前制度下：兵部与工部均为通用编码执行角色，不区分前后端；礼部负责文档；工部/兵部均可执行部署验证。
    if docs_only_paths:
        return "libu"
    if dept_key in {"bingbu", "gongbu"} and has_docs and not has_frontend and not has_backend and not has_deploy:
        return "libu"
    if dept_key == "libu" and (has_backend or has_frontend or has_deploy) and not has_docs:
        return "bingbu"
    if dept_key == "gongbu" and has_docs and not has_deploy and not has_frontend and not has_backend:
        return "libu"
    return dept_key


def normalize_stage_plan(stages: list[dict], logger: logging.Logger) -> list[dict]:
    """对尚书省派工结果做代码级纠偏，修正明显越权映射。"""
    normalized_stages: list[dict] = []
    for stage_info in stages:
        stage_copy = dict(stage_info)
        normalized_departments: list[dict] = []
        for dept in stage_info.get("departments", []):
            dept_copy = dict(dept)
            original_name = str(dept_copy.get("name", ""))
            original_key = normalize_department(original_name) or original_name
            task_desc = str(dept_copy.get("task", ""))
            normalized_key = normalize_dispatch_department(task_desc, original_key)
            if normalized_key != original_key:
                logger.warning(
                    "尚书省派工纠偏：%s -> %s，任务=%s",
                    original_key,
                    normalized_key,
                    task_desc[:120],
                )
                dept_copy["name"] = ROLE_DISPLAY_NAMES.get(normalized_key, normalized_key)
            normalized_departments.append(dept_copy)
        stage_copy["departments"] = normalized_departments
        normalized_stages.append(stage_copy)
    return normalized_stages


def extract_plan_contracts(plan_text: str) -> Dict[str, Any]:
    """从中书省规划中提取结构化契约，供尚书省派工时下发。"""
    plan_json = extract_json_from_output(plan_text) or {}
    architecture = plan_json.get("architecture", {}) if isinstance(plan_json, dict) else {}
    steps = {}
    for step in plan_json.get("steps", []) if isinstance(plan_json, dict) else []:
        if isinstance(step, dict) and step.get("step_id") is not None:
            steps[str(step["step_id"])] = step
    return {
        "project_root": architecture.get("project_root", ""),
        "interface_contracts": [
            contract for contract in architecture.get("interface_contracts", [])
            if isinstance(contract, dict)
        ],
        "steps": steps,
    }


def format_dispatch_contracts(
    task_desc: str,
    dept_key: str,
    dispatch_info: Dict[str, Any],
    plan_contracts: Dict[str, Any],
) -> str:
    """把关键文件路径、验收标准、接口契约追加到部门任务中，减少自由发挥。"""
    lines = [task_desc.strip()]
    project_root = plan_contracts.get("project_root")
    step_id = dispatch_info.get("derived_from_step")
    step = plan_contracts.get("steps", {}).get(str(step_id)) if step_id is not None else None
    constraints = [str(item) for item in dispatch_info.get("constraints", []) if str(item).strip()]
    task_paths = extract_work_paths(task_desc)

    lines.append("\n【结构化派工约束】")
    lines.append(f"- 指派部门：{ROLE_DISPLAY_NAMES.get(dept_key, dept_key)}")
    if project_root:
        lines.append(f"- 统一项目根目录：{project_root}")
    if task_paths:
        lines.append(f"- 当前任务中出现的目标路径：{', '.join(task_paths)}")
    if step:
        lines.append(f"- 来源步骤：step {step.get('step_id')} - {step.get('description', '')}")
        expected_output = str(step.get("expected_output", "")).strip()
        if expected_output:
            lines.append(f"- 该步骤预期交付物：{expected_output}")
        acceptance = str(step.get("acceptance_criteria", "")).strip()
        if acceptance:
            lines.append(f"- 该步骤验收标准：{acceptance}")
    if constraints:
        lines.append(f"- 尚书省附加约束：{'；'.join(constraints)}")

    contracts = plan_contracts.get("interface_contracts", [])
    if contracts:
        lines.append("- 共享接口契约：")
        for contract in contracts:
            name = contract.get("name", "未命名契约")
            producer = contract.get("producer", "未知产生方")
            consumer = contract.get("consumer", "未知使用方")
            details = contract.get("details", "")
            lines.append(f"  * {name} | {producer} -> {consumer} | {details}")

    lines.append("执行要求：优先遵守以上路径、接口、验收标准；若与自由文本冲突，以结构化约束为准。")
    return "\n".join(lines)


def _stage_department_keys(stage_info: Dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for dept in stage_info.get("departments", []):
        dept_name = str(dept.get("name", ""))
        dept_key = normalize_department(dept_name)
        if dept_key:
            keys.add(dept_key)
    return keys


def _steps_are_dependent(step_a: Any, step_b: Any, plan_contracts: Dict[str, Any]) -> bool:
    steps = plan_contracts.get("steps", {})
    a = steps.get(str(step_a)) if step_a is not None else None
    b = steps.get(str(step_b)) if step_b is not None else None
    deps_a = {str(dep) for dep in a.get("dependencies", [])} if isinstance(a, dict) else set()
    deps_b = {str(dep) for dep in b.get("dependencies", [])} if isinstance(b, dict) else set()
    if step_a is None or step_b is None:
        return False
    return str(step_a) in deps_b or str(step_b) in deps_a


def allow_parallel_frontend_backend(
    stages: list[dict],
    plan_contracts: Dict[str, Any],
    logger: logging.Logger,
) -> list[dict]:
    """在模块边界已明确时，允许兵部与工部并发开发，联调再后置。"""
    if not plan_contracts.get("interface_contracts"):
        return stages

    optimized: list[dict] = []
    idx = 0
    while idx < len(stages):
        current = dict(stages[idx])
        if idx + 1 >= len(stages):
            optimized.append(current)
            break

        nxt = dict(stages[idx + 1])
        current_keys = _stage_department_keys(current)
        next_keys = _stage_department_keys(nxt)

        can_merge = (
            current_keys == {"bingbu"} and next_keys == {"gongbu"}
            or current_keys == {"gongbu"} and next_keys == {"bingbu"}
        )

        if not can_merge:
            optimized.append(current)
            idx += 1
            continue

        current_step = current.get("departments", [{}])[0].get("derived_from_step")
        next_step = nxt.get("departments", [{}])[0].get("derived_from_step")
        if _steps_are_dependent(current_step, next_step, plan_contracts):
            optimized.append(current)
            idx += 1
            continue

        merged = dict(current)
        merged["description"] = (
            f"{current.get('description', '')} + {nxt.get('description', '')}"
        ).strip(" +")
        merged["departments"] = list(current.get("departments", [])) + list(nxt.get("departments", []))
        optimized.append(merged)
        logger.info(
            "尚书省阶段优化：检测到模块边界已明确，合并相邻阶段以并发执行兵部/工部开发。"
        )
        idx += 2

    for order, stage in enumerate(optimized, start=1):
        stage["stage"] = order
    return optimized


@tool
def read_file(file_path: str) -> str:
    """Read a file from the work directory."""
    try:
        path = resolve_workspace_path(file_path)
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        return f"Error reading {file_path}: {e}"

@tool
def write_file(file_path: str, content: str) -> str:
    """Write text content to a file in the work directory."""
    try:
        normalized = normalize_workspace_relpath(file_path)
        path = resolve_workspace_path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        return f"Successfully wrote to {normalized}"
    except Exception as e:
        return f"Error writing {file_path}: {e}"

@tool
def execute_command(command: str) -> str:
    """Execute a shell command in the work directory."""
    try:
        result = subprocess.run(command, shell=True, cwd=WORK_DIR, capture_output=True, text=True, timeout=30)
        return f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    except Exception as e:
        return f"Error executing command: {e}"


def _build_deepagents_subagents(ministry_key: str) -> list[dict]:
    """为执行部门提供可委派的子代理，增强任务拆解与上下文隔离。"""
    subagents = [
        {
            "name": "artifact_writer",
            "description": "负责把需求落到 work/ 中的实际文件，适合撰写、修改、整理交付物。",
            "system_prompt": (
                "你是文件落地产出子代理。优先直接修改或创建 work/ 内文件，"
                "不要只给建议；完成后汇报改了哪些文件、为什么这样改。"
            ),
        },
        {
            "name": "artifact_reviewer",
            "description": "负责审查 work/ 中已有文件是否满足当前任务与验收标准。",
            "system_prompt": (
                "你是交付物审查子代理。检查 work/ 中真实文件与任务要求是否一致，"
                "指出缺口、风险和需要补齐的内容；不要编造未存在的文件。"
            ),
        },
    ]

    if ministry_key in {"bingbu", "gongbu"}:
        subagents.append(
            {
                "name": "command_executor",
                "description": "负责在 work/ 目录内运行必要命令，验证实现、构建或调试问题。",
                "system_prompt": (
                    "你是命令执行子代理。仅在有明确必要时才运行命令；"
                    "优先使用最小命令验证，汇报执行结果、失败原因和后续建议。"
                ),
            }
        )

    return subagents


def _create_ministry_agent(ministry_key: str, llm: "ChatOpenAI", role_prompt: str, tools: list[Any]):
    """按部门能力选择 agent 实现；执行部门优先使用 deepagents。"""
    if ministry_key in {"libu", "bingbu", "gongbu"}:
        from deepagents import create_deep_agent
        from deepagents.backends.filesystem import FilesystemBackend
        from deepagents.backends.local_shell import LocalShellBackend

        backend = FilesystemBackend(root_dir=WORK_DIR, virtual_mode=False)
        if ministry_key in {"bingbu", "gongbu"}:
            backend = LocalShellBackend(
                root_dir=WORK_DIR,
                virtual_mode=False,
                timeout=30,
                inherit_env=True,
            )

        deep_prompt = (
            f"{role_prompt}\n\n"
            "你运行在三省六部制工作流的执行部门中。\n"
            "硬约束：\n"
            "1. 真实产物优先，结果应尽量落到 work/ 目录，而不是停留在文字说明。\n"
            "2. 如任务较复杂，主动使用 write_todos 拆解，再视情况使用 task 委派子代理。\n"
            "3. 当前 backend 的工作根目录就是 work/；优先使用相对路径如 `climb-game/README.md`。\n"
            "4. 严禁把绝对路径或 `work/...` 再拼成 `work/home/...`、`work/work/...` 这类镜像路径。\n"
            "5. 若需要执行命令，只做与当前任务直接相关的最小验证。\n"
        )
        return create_deep_agent(
            model=llm,
            tools=[],
            system_prompt=deep_prompt,
            backend=backend,
            subagents=_build_deepagents_subagents(ministry_key),
        )

    # 非执行部门保持轻量 agent，避免把审批/治理节点变成通用 coding agent。
    try:
        from langchain.agents import create_agent
        return create_agent(llm, tools=tools, system_prompt=role_prompt)
    except ImportError:
        from langgraph.prebuilt import create_react_agent
        return create_react_agent(llm, tools=tools, prompt=role_prompt)


def execute_ministry(ministry_key: str, task_desc: str, state: "EdictState",
                     logger: logging.Logger, task_dir: Optional[Path] = None) -> dict:
    ministry_name = ROLE_DISPLAY_NAMES.get(ministry_key, ministry_key)
    logger.info(f"  → {ministry_name} 开始执行：{task_desc[:80]}...")

    output_file = (task_dir / f"{ministry_key}.output") if task_dir else None

    try:
        role_prompt = load_role_prompt(ministry_key)
        pre_repaired_paths = repair_mirrored_workspace_paths()
        if pre_repaired_paths:
            logger.warning(f"{ministry_name} 执行前修复镜像路径：{pre_repaired_paths}")
        before_snapshot = snapshot_work_tree()
        prior_results = state.get("ministry_results", {})
        prior_artifacts = []
        for dept_key, result in prior_results.items():
            if not isinstance(result, dict):
                continue
            changes = result.get("file_changes", {})
            if any(changes.get(k) for k in ["created", "modified", "deleted"]):
                prior_artifacts.append(
                    f"- {ROLE_DISPLAY_NAMES.get(dept_key, dept_key)}\n"
                    f"{format_file_changes(changes)}"
                )
        backend_task_desc = rewrite_task_paths_for_workspace(task_desc)
        context = (
            f"用户请求：{state['user_input']}\n\n"
            f"工作流指定你的任务是：{task_desc}\n\n"
            f"当前 backend 根目录就是 work/。\n"
            f"路径换算规则：任务中若写 `work/...`，你实际操作时必须写成相对路径 `...`，"
            f"按 backend 根目录重写后的任务路径：{backend_task_desc}\n\n"
            f"当前 work/ 文件清单：\n{format_work_tree(before_snapshot)}\n\n"
            f"前序部门已落盘的文件变更：\n"
            f"{chr(10).join(prior_artifacts) if prior_artifacts else '暂无前序文件产物。'}\n\n"
            f"说明：你可以使用工具在 {WORK_DIR} 目录下读写文件。"
            "若任务涉及实现、文档或部署，请优先基于已有文件协作，并把结果真实写入 work/，不要只做文字说明。"
        )

        # Select tools based on role
        tools = [read_file]
        if ministry_key in ["bingbu", "gongbu", "libu"]:
            tools.append(write_file)
        if ministry_key in ["bingbu", "gongbu"]:
            tools.append(execute_command)

        llm = _get_llm_for_role(ministry_key)
        agent = _create_ministry_agent(ministry_key, llm, role_prompt, tools)

        def _extract_agent_output(res: Any) -> str:
            """兼容多种 agent 返回结构，避免 KeyError('output')。"""
            if res is None:
                return ""
            if isinstance(res, str):
                return res
            if isinstance(res, dict):
                messages = res.get("messages")
                if isinstance(messages, list) and messages:
                    last = messages[-1]
                    content = getattr(last, "content", "")
                    if isinstance(content, list):
                        return "\n".join(str(item) for item in content)
                    if content:
                        return str(content)
                for key in ["output", "result", "final_output", "text", "content"]:
                    value = res.get(key)
                    if value:
                        return str(value)
                return json.dumps(res, ensure_ascii=False, default=str)
            return str(res)

        def _stream_to_file(agent_obj) -> str:
            """流式执行并实时写入输出文件，返回最终输出文本"""
            if output_file is None:
                # 无文件目标，直接 invoke
                try:
                    res = agent_obj.invoke({"messages": [("user", context)]})
                    return _extract_agent_output(res)
                except Exception:
                    res = agent_obj.invoke({"input": context})
                    return _extract_agent_output(res)

            output_file.parent.mkdir(parents=True, exist_ok=True)
            parts: list[str] = []
            with open(str(output_file), "w", encoding="utf-8") as f:
                header = (
                    f"# {ministry_name} 执行日志\n"
                    f"任务：{task_desc}\n"
                    f"开始时间：{datetime.now().isoformat()}\n"
                    f"执行前文件清单：\n{format_work_tree(before_snapshot)}\n"
                    f"{'='*60}\n\n"
                )
                f.write(header)
                f.flush()
                logger.info(f"{ministry_name} 输出文件：{output_file}")

                try:
                    for chunk in agent_obj.stream({"messages": [("user", context)]}):
                        for _node, node_data in chunk.items():
                            msgs = node_data.get("messages", []) if isinstance(node_data, dict) else []
                            for msg in msgs:
                                content = getattr(msg, "content", "") or ""
                                tool_calls = getattr(msg, "tool_calls", []) or []
                                ts = datetime.now().strftime("%H:%M:%S")
                                msg_type = type(msg).__name__

                                if tool_calls:
                                    for tc in tool_calls:
                                        line = (
                                            f"[{ts}][工具调用] {tc.get('name', '')}"
                                            f"({json.dumps(tc.get('args', {}), ensure_ascii=False)})\n"
                                        )
                                        f.write(line)
                                        f.flush()
                                        logger.debug(f"{ministry_name} 工具调用: {tc.get('name', '')}")

                                if content and content.strip():
                                    f.write(f"[{ts}][{msg_type}] {content}\n")
                                    f.flush()
                                    parts.append(content)
                except Exception as stream_err:
                    logger.warning(f"{ministry_name} 流式输出失败，回退到 invoke：{stream_err}")
                    parts = []
                    try:
                        res = agent_obj.invoke({"messages": [("user", context)]})
                        fallback_output = _extract_agent_output(res)
                    except Exception:
                        res = agent_obj.invoke({"input": context})
                        fallback_output = _extract_agent_output(res)
                    f.write(fallback_output)
                    f.flush()
                    parts = [fallback_output]

                f.write(f"\n{'='*60}\n完成时间：{datetime.now().isoformat()}\n")
                f.flush()

            # 若无文本部分（如全为工具调用），读回文件中的完整内容作为输出
            if not parts:
                try:
                    with open(str(output_file), "r", encoding="utf-8") as rf:
                        return rf.read()
                except Exception:
                    return ""
            return parts[-1]

        output = _stream_to_file(agent)
        repaired_paths = repair_mirrored_workspace_paths()
        if repaired_paths:
            logger.warning(f"{ministry_name} 执行后修复镜像路径：{repaired_paths}")
        after_snapshot = snapshot_work_tree()
        file_changes = diff_work_tree(before_snapshot, after_snapshot)
        artifact_status = "success"
        if ministry_key in {"bingbu", "gongbu", "libu"} and not any(file_changes.values()):
            artifact_status = "partial"
            output = (
                f"{output}\n\n[系统提示] 该部门具备写文件权限，但本轮未检测到 work/ 文件变更。"
                " 当前结果更接近文本说明，而非文件级协作产物。"
            ).strip()

        if output_file:
            with open(str(output_file), "a", encoding="utf-8") as f:
                if repaired_paths:
                    f.write("\n## 镜像路径修复\n")
                    f.write("\n".join(f"- {item}" for item in repaired_paths))
                    f.write("\n")
                f.write("\n## 文件变更\n")
                f.write(format_file_changes(file_changes))
                f.write("\n\n## 执行后文件清单\n")
                f.write(format_work_tree(after_snapshot))
                f.write("\n")

        logger.info(f"  ← {ministry_name} 执行完成")
        return {
            "output": output,
            "status": artifact_status,
            "timestamp": datetime.now().isoformat(),
            "file_changes": file_changes,
            "work_snapshot": after_snapshot,
        }
    except Exception as e:
        logger.error(f"  ← {ministry_name} 执行异常：{e}")
        if output_file:
            with open(str(output_file), "a", encoding="utf-8") as f:
                f.write(f"\n[ERROR] {datetime.now().isoformat()} {e}\n")
        return {
            "output": str(e),
            "status": "error",
            "timestamp": datetime.now().isoformat(),
            "file_changes": {"created": [], "modified": [], "deleted": []},
            "work_snapshot": snapshot_work_tree(),
        }


# 路径配置
EDICT_DIR = Path(__file__).parent
TASKS_DIR = EDICT_DIR / "tasks"
HISTORY_DIR = EDICT_DIR / "history"
ROLES_DIR = EDICT_DIR / "roles"
DB_PATH = EDICT_DIR / "tasks.db"
WORK_DIR = EDICT_DIR / "work"

TASKS_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_DIR.mkdir(parents=True, exist_ok=True)
WORK_DIR.mkdir(parents=True, exist_ok=True)

MAX_REVISIONS = 3
MAX_RETRIES = 3
MAX_SHANGSHU_REWORK_ROUNDS = 2
MAX_DELIVERY_REVISIONS = 2

# ==================== 日志系统 ====================

_logger_lock = threading.Lock()


def setup_logger(task_id: str) -> logging.Logger:
    """
    为任务设置日志记录器。
    - history/{date}.log  : 跨任务汇总（带 task_id 标签，便于全局检索）
    - tasks/{task_id}/session.log : 单任务完整会话记录（仅当前任务）
    使用锁保证多线程安全（六部并行时不重复添加 handler）。
    """
    logger = logging.getLogger(f"edict.{task_id}")
    with _logger_lock:
        if logger.handlers:
            return logger

        logger.setLevel(logging.DEBUG)
        logger.propagate = False

        fmt_history = logging.Formatter(
            f"[%(asctime)s] [{task_id}] [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        fmt_session = logging.Formatter(
            "[%(asctime)s] [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )

        # history/{date}.log — 跨任务汇总
        log_file = HISTORY_DIR / f"{datetime.now().strftime('%Y%m%d')}.log"
        fh = logging.FileHandler(str(log_file), encoding="utf-8", mode="a")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt_history)
        logger.addHandler(fh)

        # tasks/{task_id}/session.log — 单任务会话
        task_dir = TASKS_DIR / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        session_log = task_dir / "session.log"
        sh = logging.FileHandler(str(session_log), encoding="utf-8", mode="a")
        sh.setLevel(logging.DEBUG)
        sh.setFormatter(fmt_session)
        logger.addHandler(sh)

        # 控制台
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        ch.setFormatter(fmt_history)
        logger.addHandler(ch)

    return logger


global_logger = logging.getLogger("edict.global")
if not global_logger.handlers:
    global_logger.setLevel(logging.INFO)
    _ch = logging.StreamHandler()
    _ch.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s"))
    global_logger.addHandler(_ch)


def write_node_output(task_id: str, filename: str, content: str) -> None:
    """将节点输出实时写入 tasks/{task_id}/{filename}"""
    task_dir = TASKS_DIR / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    output_path = task_dir / filename
    with open(str(output_path), "w", encoding="utf-8") as f:
        f.write(content)
        f.flush()


# ==================== 配置 ====================

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
    "bingbu":     {"通用编码执行"},
    "xingbu":     {"风险审查"},
    "gongbu":     {"通用编码执行"},
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
    dispatch_revision_count: int
    delivery_review_status: Literal["pending", "approved", "rejected"]
    delivery_review_feedback: str




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
            err_text = str(e)
            global_logger.warning(f"LLM 调用失败（第 {attempt}/{max_retries} 次，角色={role}）：{e}")
            if attempt < max_retries:
                delay = 2 ** attempt
                if is_rate_limit_error(err_text):
                    delay = max(delay, 8 * attempt)
                time.sleep(delay)

    global_logger.error(f"LLM 调用失败，已重试 {max_retries} 次（角色={role}）：{last_error}")
    raise EdictExecutionError(f"LLM 调用失败（角色={role}）：{last_error}") from last_error


def load_role_prompt(role: str) -> str:
    """加载角色提示词文件"""
    role_file = ROLES_DIR / f"{role}.md"
    if not role_file.exists():
        return f"你是{role}，请根据任务上下文完成工作。"
    return role_file.read_text(encoding="utf-8")


WRITE_REQUIRED_DEPARTMENTS = {"bingbu", "libu", "gongbu"}


def has_file_changes(result: Optional[Dict[str, Any]]) -> bool:
    """判断部门结果是否包含真实文件变更。"""
    if not isinstance(result, dict):
        return False
    changes = result.get("file_changes", {})
    return any(changes.get(key) for key in ["created", "modified", "deleted"])


def should_rework_department(dept_key: str, result: Optional[Dict[str, Any]]) -> bool:
    """判断某部门是否应被尚书省勒令返工。"""
    if not isinstance(result, dict):
        return True
    status = result.get("status", "")
    if status == "error":
        return True
    if dept_key in WRITE_REQUIRED_DEPARTMENTS and not has_file_changes(result):
        return True
    return False


def rework_needs_serial_execution(results: Dict[str, Dict[str, Any]], targets: Dict[str, str]) -> bool:
    """若返工目标里已有明显限流错误，则改为串行返工，降低再次撞上 TPM 的概率。"""
    for dept_key in targets:
        result = results.get(dept_key)
        if isinstance(result, dict) and is_rate_limit_error(str(result.get("output", ""))):
            return True
    return False


def build_rework_task(
    dept_key: str,
    original_task: str,
    result: Optional[Dict[str, Any]],
    delivery_feedback: str = "",
) -> str:
    """生成尚书省返工指令。"""
    reasons: list[str] = []
    expected_paths = extract_work_paths(original_task)
    project_root = infer_project_root(original_task)
    if not isinstance(result, dict):
        reasons.append("系统未拿到有效执行结果。")
    else:
        if result.get("status") == "error":
            reasons.append(f"上轮执行异常：{result.get('output', '')[:240]}")
        if dept_key in WRITE_REQUIRED_DEPARTMENTS and not has_file_changes(result):
            reasons.append("上轮未在 work/ 目录产生任何真实文件变更。")
        if result.get("status") == "partial":
            reasons.append("上轮结果仅为部分完成，未达到文件级交付要求。")
    if delivery_feedback:
        reasons.append(f"门下省/上级复核意见：{delivery_feedback[:400]}")

    rework_rules = (
        "\n\n【尚书省复核结论】\n"
        + "\n".join(f"- {reason}" for reason in reasons)
        + "\n\n【返工要求】\n"
        + "- 本轮必须把结果真实写入 work/，不能只做文字说明。\n"
        + "- 不得先写探测脚本、目录巡检脚本或与目标无关的辅助文件代替正式交付。\n"
        + (
            f"- 本轮统一项目根目录按 `{project_root}` 执行，不得再写回 work/ 根目录。\n"
            if project_root else ""
        )
        + (
            "- 本轮必须至少补齐这些目标文件："
            + ", ".join(f"`{path}`" for path in expected_paths[:8])
            + "。\n"
            if expected_paths else
            "- 若你具备 write_file 权限，则至少产出与任务直接相关的目标文件。\n"
        )
        + "- 若仍无法完成，必须明确说明阻塞原因，并指出缺失的具体文件或前置产物。"
    )
    return f"{original_task}{rework_rules}"


# execute_ministry moved above

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

    system_prompt = """你是中书省令，负责项目结构设计。
请根据用户输入，生成项目的详细结构设计和实施步骤，不需要分配部门，部门分配将由尚书省完成。

强制要求：
1. 先确定统一项目根目录，必须写成 `work/<项目名>`，不得默认把产物散落到 `work/` 根目录
2. `project_root` 必须是具体路径，例如 `work/climb-game`
3. 所有交付物路径、目录结构、预期文件，都必须尽量写成 `work/<项目名>/...`
4. 前端、后端、文档、部署等产物路径要分开写清，便于尚书省后续派工

计划必须使用以下 JSON 格式输出：
```json
{
    "task_name": "任务名称",
    "description": "任务描述",
    "project_root": "work/<项目名>",
    "project_structure": {
        "root": "work/<项目名>",
        "directories": ["work/<项目名>/frontend", "work/<项目名>/backend"],
        "key_files": ["work/<项目名>/README.md", "work/<项目名>/backend/app.py"]
    },
    "implementation_steps": [
        {
            "step": "步骤1名称",
            "goal": "该步骤的目标",
            "expected_outputs": ["work/<项目名>/..."],
            "notes": "依赖、接口或验收要点"
        }
    ],
    "timeline": "预期时间线",
    "resources": ["所需资源1", "所需资源2"]
}
```"""

    user_content = f"用户任务：{state['user_input']}{revision_hint}"

    plan_text = call_llm_with_retry(system_prompt, user_content, role="zhongshu")

    logger.info(f"中书省规划完成，计划长度：{len(plan_text)} 字符")
    log_event_to_db(task_id, "node_complete", "zhongshu", f"规划完成，长度 {len(plan_text)}")

    # 实时写入计划文件
    revision = state.get("revision_count", 0)
    fname = f"zhongshu_plan_r{revision}.output" if revision > 0 else "zhongshu_plan.output"
    write_node_output(
        task_id, fname,
        f"# 中书省 规划（第{revision+1}轮）\n"
        f"时间：{datetime.now().isoformat()}\n"
        f"{'='*60}\n\n{plan_text}\n"
    )

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

    system_prompt = f"""你是门下省侍中，负责审核中书省的项目结构设计计划。

审核标准：
1. 可行性：计划是否可执行
2. 资源充足性：资源是否足够
3. 风险控制：是否有风险预案
4. 结构设计：项目的实施步骤和整体架构是否合理清晰

当前是第 {state.get("revision_count", 0)} 次修订，最大允许 {MAX_REVISIONS} 次。

如果通过，返回 JSON：
{{"decision": "通过", "comments": "审核意见"}}

如果不通过，返回 JSON：
{{"decision": "封驳", "issues": ["问题1", "问题2"], "suggestions": "修改建议"}}"""

    user_content = f"请审核以下计划:\n{state['plan']}"

    review_result = call_llm_with_retry(system_prompt, user_content, role="menxia")

    logger.info(f"门下省审核结果：{review_result[:200]}...")
    revision = state.get("revision_count", 0)
    write_node_output(
        task_id, f"menxia_review_r{revision}.output",
        f"# 门下省 审核（第{revision}轮）\n时间：{datetime.now().isoformat()}\n{'='*60}\n\n{review_result}\n"
    )

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
        f"- {ROLE_DISPLAY_NAMES.get(k, k)}: 状态={v.get('status', '?')}; "
        f"文件={format_file_changes(v.get('file_changes', {}))}; "
        f"摘要={v.get('output', '')[:300]}"
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
    由 LLM 决定调用哪些部门、分几个阶段执行，阶段间串行（后阶段可看到前阶段输出），
    同一阶段内并行。
    """
    task_id = state["task_id"]
    logger = setup_logger(task_id)
    logger.info("【尚书省】开始协调六部执行")
    log_event_to_db(task_id, "node_start", "shangshu", "尚书省开始协调")

    system_prompt = """你是尚书省尚书令，负责协调六部执行任务，并在执行后按文件产物进行阶段验收。

    根据计划内容，决定：
    1. 需要调用哪些部门（只调用确实需要的部门，不必所有部门都派活）
    2. 各部门的执行顺序：有依赖关系的放入不同阶段（stage），同一阶段内并行执行
    3. 每个任务必须尽量指向 work/<项目名>/ 下的具体交付物，避免空泛描述
    4. 尚书省必须先看真实文件产物再放行；若有写权限的部门未写文件，应由原部门返工，不得直接带过

    执行规则：
    - 若某部门的输出会影响另一个部门的工作（如户部的资源建议要传递给兵部），则户部放早期阶段，兵部放后续阶段
    - 派工前必须先从计划中确认统一项目根目录，并在任务描述里显式写出 `work/<项目名>/...` 目标路径
    - 兵部与工部均为通用编码与指令执行角色，不区分前端/后端，均可使用 write_file、read_file、execute_command 工具
    - 简单任务（单一功能点、改动集中）：派发给兵部或工部中的一个部门独立完成，无需并发
    - 复杂任务（涉及多个模块、有明确可拆分边界）：并发派发至兵部与工部，尚书省必须为二者明确划分各自负责的模块边界，写入任务描述
    - 兵部与工部并发时，联调、启动验证、端到端测试可放到后续阶段
    - 礼部负责 README、说明文档、接口文档与文案；不要把文档任务派给兵部或工部
    - 吏部只负责组织治理，不承担虚拟环境创建、依赖安装、测试执行、部署验证
- README、说明文档、接口文档只能交给礼部，不要派给兵部或工部
- 若任务不涉及某部门，直接不分配，不要编造无意义的任务
- 如当前已经存在执行结果或门下省/上级给出“存疑”意见，本轮应优先安排补齐缺失产物的返工任务，而不是重新发散规划

可用部门：吏部(libu_admin)、户部(hubu)、礼部(libu)、兵部(bingbu)、刑部(xingbu)、工部(gongbu)

输出必须是 JSON 格式：
```json
{
    "stages": [
        {
            "stage": 1,
            "description": "第一阶段概述（如：资源评估）",
            "departments": [
                {"name": "户部", "task": "评估本项目所需云资源和成本，重点说明数据库和服务器选型建议"}
            ]
        },
        {
            "stage": 2,
            "description": "第二阶段概述（如：代码开发，依赖第一阶段的资源建议）",
            "departments": [
                {"name": "兵部", "task": "根据户部的资源建议，实现xxx功能……"},
                {"name": "礼部", "task": "编写用户文档"}
            ]
        }
    ]
}
```"""

    user_content = (
        f"用户任务：\n{state['user_input']}\n\n"
        f"中书省计划：\n{state['plan']}\n\n"
        f"门下省审核意见：\n{state['review_feedback']}\n\n"
        f"门下省终验/尚书省返工意见：\n{state.get('delivery_review_feedback', '无')}\n\n"
        f"现有部门执行结果：\n"
        + (
            "\n".join(
                f"- {ROLE_DISPLAY_NAMES.get(k, k)}：状态={v.get('status', '?')}；"
                f"文件={format_file_changes(v.get('file_changes', {}))}；"
                f"摘要={v.get('output', '')[:240]}"
                for k, v in state.get("ministry_results", {}).items()
                if isinstance(v, dict)
            ) or "暂无"
        )
    )

    plan_contracts = extract_plan_contracts(state.get("plan", ""))
    dispatch_result = call_llm_with_retry(system_prompt, user_content, role="shangshu")
    logger.info(f"尚书省分配方案：{dispatch_result[:200]}...")

    plan_json = extract_json_from_output(dispatch_result)
    stages = plan_json.get("stages", []) if plan_json else []

    # 回退：若解析失败则单阶段执行兵部
    if not stages:
        logger.warning("未解析到分阶段方案，回退到单阶段兵部任务")
        stages = [{"stage": 1, "description": "默认执行", "departments": [{"name": "兵部", "task": state["user_input"]}]}]
    else:
        stages = normalize_stage_plan(stages, logger)
        stages = allow_parallel_frontend_backend(stages, plan_contracts, logger)
    ministry_results: Dict[str, Dict[str, Any]] = dict(state.get("ministry_results", {}))
    all_dispatch_tasks: Dict[str, str] = {}
    _validate_task_id(task_id)
    task_dir = TASKS_DIR / task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    # 按阶段串行，阶段内并行
    for stage_info in sorted(stages, key=lambda s: s.get("stage", 0)):
        stage_num = stage_info.get("stage", "?")
        stage_desc = stage_info.get("description", "")
        departments = stage_info.get("departments", [])

        # 将前序阶段产出注入到本阶段每个部门的任务描述末尾
        prior_outputs = ""
        if ministry_results:
            prior_outputs = "\n\n【前序部门产出（供参考）】\n" + "\n".join(
                f"- {ROLE_DISPLAY_NAMES.get(k, k)}：状态={v.get('status', '?')}\n"
                f"  文件变更：{format_file_changes(v.get('file_changes', {}))}\n"
                f"  文本摘要：{v.get('output', '')[:400]}"
                for k, v in ministry_results.items()
            )

        # 构建本阶段派发映射
        stage_dispatch: Dict[str, str] = {}
        for m in departments:
            dept_name = m.get("name", "")
            dept_key = normalize_department(dept_name)
            if not dept_key:
                logger.warning(f"未知部门名：{dept_name}，跳过")
                continue
            if not validate_flow("shangshu", dept_key, logger):
                continue
            task_desc = format_dispatch_contracts(
                m.get("task", ""),
                dept_key,
                m,
                plan_contracts,
            ) + prior_outputs
            stage_dispatch[dept_key] = task_desc
            all_dispatch_tasks[dept_key] = task_desc

        if not stage_dispatch:
            logger.warning(f"阶段 {stage_num} 无有效部门，跳过")
            continue

        logger.info(f"阶段 {stage_num}（{stage_desc}）：并行执行 {list(stage_dispatch.keys())}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(stage_dispatch)) as executor:
            state_for_stage = dict(state)
            state_for_stage["ministry_results"] = dict(ministry_results)
            future_map = {
                executor.submit(
                    execute_ministry,
                    dept_key,
                    task_desc,
                    state_for_stage,
                    logger,
                    task_dir,
                ): dept_key
                for dept_key, task_desc in stage_dispatch.items()
            }
            for future in concurrent.futures.as_completed(future_map):
                dept_key = future_map[future]
                dept_name = ROLE_DISPLAY_NAMES.get(dept_key, dept_key)
                try:
                    ministry_results[dept_key] = future.result()
                    result_status = ministry_results[dept_key].get("status", "?")
                    if result_status in {"error", "partial"}:
                        logger.warning(f"  ! {dept_name} 阶段 {stage_num} 结果={result_status}")
                    else:
                        logger.info(f"  ✓ {dept_name} 完成（阶段 {stage_num}）")
                except Exception as exc:
                    logger.error(f"  ✗ {dept_name} 执行异常：{exc}")
                    ministry_results[dept_key] = {
                        "output": str(exc), "status": "error",
                        "timestamp": datetime.now().isoformat()
                    }

    rework_summaries: list[str] = []
    delivery_feedback = state.get("delivery_review_feedback", "")
    for rework_round in range(1, MAX_SHANGSHU_REWORK_ROUNDS + 1):
        rework_targets = {
            dept_key: all_dispatch_tasks[dept_key]
            for dept_key in all_dispatch_tasks
            if should_rework_department(dept_key, ministry_results.get(dept_key))
        }
        if not rework_targets:
            break

        logger.warning(
            f"尚书省文件复核未通过，第 {rework_round} 轮勒令返工：{list(rework_targets.keys())}"
        )
        log_event_to_db(
            task_id,
            "dispatch_rework",
            "shangshu",
            f"第 {rework_round} 轮返工：{','.join(rework_targets.keys())}",
        )

        rework_summaries.append(
            f"第 {rework_round} 轮返工：{', '.join(ROLE_DISPLAY_NAMES.get(k, k) for k in rework_targets)}"
        )

        serial_rework = rework_needs_serial_execution(ministry_results, rework_targets)
        max_workers = 1 if serial_rework else len(rework_targets)
        if serial_rework:
            logger.warning("检测到返工目标包含限流错误，本轮改为串行返工并增加退避。")
            time.sleep(6 * rework_round)

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            state_for_rework = dict(state)
            state_for_rework["ministry_results"] = dict(ministry_results)
            future_map = {
                executor.submit(
                    execute_ministry,
                    dept_key,
                    build_rework_task(
                        dept_key,
                        original_task,
                        ministry_results.get(dept_key),
                        delivery_feedback=delivery_feedback,
                    ),
                    state_for_rework,
                    logger,
                    task_dir,
                ): dept_key
                for dept_key, original_task in rework_targets.items()
            }
            for future in concurrent.futures.as_completed(future_map):
                dept_key = future_map[future]
                dept_name = ROLE_DISPLAY_NAMES.get(dept_key, dept_key)
                try:
                    ministry_results[dept_key] = future.result()
                    result_status = ministry_results[dept_key].get("status", "?")
                    logger.info(f"  ↺ {dept_name} 返工完成，结果={result_status}")
                except Exception as exc:
                    logger.error(f"  ↺ {dept_name} 返工异常：{exc}")
                    ministry_results[dept_key] = {
                        "output": str(exc),
                        "status": "error",
                        "timestamp": datetime.now().isoformat(),
                        "file_changes": {"created": [], "modified": [], "deleted": []},
                    }

    # M1: 用 LLM 语义分析决定需要哪些治理部门
    governance_plan = _plan_governance_with_llm(state, ministry_results, logger)
    logger.info(f"治理计划：{governance_plan}")

    # 写入尚书省派发日志
    write_node_output(
        task_id, "shangshu_dispatch.output",
        f"# 尚书省 派发日志\n时间：{datetime.now().isoformat()}\n{'='*60}\n\n"
        f"## 派发结果\n{dispatch_result}\n\n"
        f"## 尚书省复核与返工\n{chr(10).join(rework_summaries) if rework_summaries else '无需返工'}\n\n"
        f"## 治理计划\n{json.dumps(governance_plan, ensure_ascii=False, indent=2)}\n"
    )

    log_event_to_db(task_id, "node_complete", "shangshu",
                    f"六部执行完成，共 {len(ministry_results)} 个结果")

    return {
        "ministry_results": ministry_results,
        "governance_plan": governance_plan,
        "dispatch_revision_count": state.get("dispatch_revision_count", 0) + 1,
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
        result_text = call_llm_with_retry(role_prompt, context, role="libu_admin")
        ministry_result: Dict[str, Any] = {
            "output": result_text, "status": "success", "timestamp": datetime.now().isoformat()
        }
    except EdictExecutionError as e:
        logger.error(f"吏部执行异常：{e}")
        ministry_result = {"output": str(e), "status": "error", "timestamp": datetime.now().isoformat()}
        result_text = str(e)

    write_node_output(
        task_id, "libu_admin.output",
        f"# 吏部 执行日志\n时间：{datetime.now().isoformat()}\n{'='*60}\n\n{result_text}\n"
    )

    ministry_results = dict(state.get("ministry_results", {}))
    ministry_results["libu_admin"] = ministry_result

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

    write_node_output(
        task_id, "hubu.output",
        f"# 户部 资源评估\n时间：{datetime.now().isoformat()}\n{'='*60}\n\n{ministry_result.get('output', '')}\n"
    )

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

    write_node_output(
        task_id, "xingbu.output",
        f"# 刑部 安全审查\n时间：{datetime.now().isoformat()}\n风险级别：{new_priority}\n{'='*60}\n\n{ministry_result.get('output', '')}\n"
    )

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
        f"文件变更={format_file_changes(v.get('file_changes', {}))}; "
        f"{v.get('output', '')[:400] if isinstance(v, dict) else str(v)[:400]}"
        for k, v in state.get("ministry_results", {}).items()
    )
    current_workspace = format_work_tree(snapshot_work_tree())
    workspace_samples = sample_work_file_contents()

    system_prompt = """你是门下省侍中，负责对六部执行成果进行最终质量验收。

请检查：
1. 各部门执行结果是否完整、无明显错误
2. 整体方案是否满足用户原始需求
3. 是否存在安全或合规遗漏
4. 对于代码、文档、部署类任务，必须优先依据真实文件产物判断，不能仅依据部门自述
5. 若已提供 work/ 关键文件内容摘录，必须优先阅读这些摘录，再结合文件清单与部门结果作出判断

输出 JSON：
{"verdict": "通过" 或 "存疑", "summary": "验收意见", "issues": ["问题1（如无则为空列表）"]}"""

    user_content = (
        f"用户原始任务：{state['user_input']}\n\n"
        f"计划：{state['plan'][:500]}\n\n"
        f"各部门执行结果：\n{results_summary}\n\n"
        f"当前 work/ 文件清单：\n{current_workspace}\n\n"
        f"当前 work/ 关键文件内容摘录：\n{workspace_samples}"
    )

    try:
        review = call_llm_with_retry(system_prompt, user_content, role="menxia")
    except EdictExecutionError as e:
        logger.warning(f"门下省终验调用失败，跳过：{e}")
        review = '{"verdict": "通过", "summary": "终验跳过（LLM 调用失败）", "issues": []}'

    parsed_review = extract_json_from_output(review) or {}
    verdict = str(parsed_review.get("verdict", "")).strip()
    if any(keyword in verdict for keyword in ["存疑", "不通过", "驳回"]):
        delivery_review_status = "rejected"
    else:
        delivery_review_status = "approved"

    feedback_text = review
    log_event_to_db(task_id, "node_complete", "menxia_final", f"终验完成：{review[:200]}")
    logger.info(f"门下省终验结果：{review[:200]}")

    write_node_output(
        task_id, "menxia_final.output",
        f"# 门下省·终验 执行日志\n时间：{datetime.now().isoformat()}\n{'='*60}\n\n"
        f"## work/ 文件清单\n{current_workspace}\n\n"
        f"## work/ 关键文件内容摘录\n{workspace_samples}\n\n"
        f"## 终验结论\n{review}\n"
    )

    return {
        "delivery_review_status": delivery_review_status,
        "delivery_review_feedback": feedback_text,
        "updated_at": datetime.now().isoformat(),
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
        f"### {ROLE_DISPLAY_NAMES.get(m, m)}\n"
        f"状态：{result.get('status', '?') if isinstance(result, dict) else '?'}\n"
        f"文件变更：{format_file_changes(result.get('file_changes', {})) if isinstance(result, dict) else '未知'}\n"
        f"{result.get('output', '') if isinstance(result, dict) else str(result)[:1000]}"
        for m, result in state.get("ministry_results", {}).items()
    ])
    workspace_summary = format_work_tree(snapshot_work_tree())

    system_prompt = """你是太子太傅，负责汇总六部工作成果。
请根据以下各部门的报告，生成最终的执行总结。

要求：
- 明确区分“真实已落盘文件”和“仅文字声称完成”的内容
- 如果 work/ 中缺少关键代码或文档文件，不得写成“已完成开发”

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
{ministry_reports}

当前 work/ 文件清单:
{workspace_summary}"""

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

    write_node_output(task_id, "finalize.output", final_report)

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


def after_final_review_route(state: EdictState) -> Literal["shangshu_node", "finalize_node"]:
    """门下省终验后的路由：存疑则回尚书省整改，通过则汇总。"""
    task_id = state.get("task_id", "unknown")
    logger = setup_logger(task_id)
    delivery_status = state.get("delivery_review_status", "approved")
    dispatch_round = state.get("dispatch_revision_count", 0)

    if delivery_status == "rejected":
        if dispatch_round >= MAX_DELIVERY_REVISIONS:
            logger.warning(
                f"门下省终验仍存疑，但尚书省整改已达上限 ({MAX_DELIVERY_REVISIONS})，转入最终汇总"
            )
            log_event_to_db(
                task_id,
                "routing",
                "menxia_final",
                f"终验存疑但整改达上限 {MAX_DELIVERY_REVISIONS}，转入汇总",
            )
            return "finalize_node"
        logger.warning("门下省终验存疑，回流尚书省继续整改")
        log_event_to_db(task_id, "routing", "menxia_final", "终验存疑，回流尚书省整改")
        return "shangshu_node"

    logger.info("门下省终验通过，进入最终汇总")
    log_event_to_db(task_id, "routing", "menxia_final", "终验通过，进入汇总")
    return "finalize_node"


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

    workflow.add_conditional_edges(
        "menxia_final_node",
        after_final_review_route,
        {
            "shangshu_node": "shangshu_node",
            "finalize_node": "finalize_node",
        }
    )
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
        "dispatch_revision_count": 0,
        "delivery_review_status": "pending",
        "delivery_review_feedback": "",
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
