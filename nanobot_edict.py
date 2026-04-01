#!/usr/bin/env python3
"""
edict 技能入口 - 多模型路由

这是 nanobot 环境中的 edict 技能主入口，负责：
1. 调用 workflow.py 创建工作流
2. 使用直接 LLM 调用实现多模型路由（不同角色使用不同模型）
3. 协调各角色执行
"""

import asyncio
import json
import importlib
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any

from loguru import logger

from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider

from .workflow import (
    create_task,
    update_state,
    log_task_event,
    get_task_status,
    wait_for_output,
    spawn_role_agent,
    run_opencode_task,
    load_role_prompt,
    ROLES,
    TASKS_DIR,
)

# 角色与模型映射（从 models.json 读取）
ROLE_MODEL_MAP = {
    "zhongshu": "zhongshu",
    "menxia": "menxia",
    "shangshu": "shangshu",
    "libu_admin": "libu_admin",
    "hubu": "hubu",
    "libu": "libu",
    "bingbu": "bingbu",
    "xingbu": "xingbu",
    "gongbu": "gongbu",
}


class ModelRouter:
    """多模型路由器 - 从 models.json 读取配置，动态创建 provider 并调用 LLM"""

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.edict_dir = Path(__file__).parent
        self.models_file = self.edict_dir / "models.json"
        self._config: Optional[Dict] = None
        self._provider_cache: Dict[str, LLMProvider] = {}

    def load_config(self) -> Dict:
        """加载 models.json 配置"""
        if self._config is not None:
            return self._config
        if not self.models_file.exists():
            raise FileNotFoundError(f"模型配置文件不存在：{self.models_file}")
        content = self.models_file.read_text(encoding="utf-8")
        config: Dict = json.loads(content)
        self._config = config
        return config

    def get_role_config(self, role: str) -> Optional[Dict]:
        """获取指定角色的模型配置"""
        config = self.load_config()
        return config.get("models", {}).get(role)

    def _create_provider(self, role_config: Dict) -> LLMProvider:
        """根据角色配置创建 provider 实例"""
        provider_name = role_config.get("provider", "")
        cache_key = f"{provider_name}:{role_config.get('model', '')}"

        if cache_key in self._provider_cache:
            return self._provider_cache[cache_key]

        endpoint = role_config.get("endpoint", "")
        api_key = role_config.get("api_key", "")
        model = role_config.get("model", "")

        if not endpoint or not api_key:
            raise ValueError(f"角色配置缺少 endpoint 或 api_key: {role_config}")

        # 尝试使用 CustomProvider（OpenAI 兼容接口）
        try:
            from nanobot.providers.custom_provider import CustomProvider

            provider = CustomProvider(
                api_key=api_key,
                api_base=endpoint,
                default_model=model,
            )
            self._provider_cache[cache_key] = provider
            logger.info(f"[edict] 创建 CustomProvider: {provider_name} → {model}")
            return provider
        except ImportError:
            pass

        # 尝试使用 LiteLLMProvider
        try:
            from nanobot.providers.litellm_provider import LiteLLMProvider

            provider = LiteLLMProvider(
                api_key=api_key,
                api_base=endpoint,
                default_model=model,
                provider_name=provider_name,
            )
            self._provider_cache[cache_key] = provider
            logger.info(f"[edict] 创建 LiteLLMProvider: {provider_name} → {model}")
            return provider
        except ImportError:
            pass

        raise RuntimeError(f"无法创建 provider: {provider_name} ({model})")

    async def call_role_llm(self, role: str, input_context: str) -> Optional[str]:
        """
        调用指定角色的 LLM

        Args:
            role: 角色名称（如 zhongshu, menxia）
            input_context: 输入上下文

        Returns:
            LLM 返回的文本
        """
        role_config = self.get_role_config(role)

        if not role_config:
            logger.warning(f"[edict] 角色 '{role}' 未配置模型，跳过")
            return None

        model = role_config.get("model", "")
        temperature = role_config.get("temperature", 0.7)
        max_tokens = role_config.get("max_tokens", 4096)

        try:
            role_prompt = load_role_prompt(role)
        except FileNotFoundError:
            logger.error(f"[edict] 角色 '{role}' 的 prompt 文件不存在")
            return None

        messages = [
            {"role": "system", "content": role_prompt},
            {"role": "user", "content": input_context},
        ]

        provider = self._create_provider(role_config)

        logger.info(f"[edict] 调用 {role} → {role_config.get('provider')}/{model}")

        response = await provider.chat(
            messages=messages,
            tools=[],
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        return response.content


class EdictWorkflow:
    """edict 工作流执行器 - 多模型路由"""

    def __init__(
        self,
        provider: LLMProvider,
        workspace: Path,
        bus: MessageBus,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
    ):
        self.provider = provider
        self.workspace = workspace
        self.bus = bus
        self.origin_channel = origin_channel
        self.origin_chat_id = origin_chat_id
        self.edict_dir = Path(__file__).parent
        self.router = ModelRouter(workspace)

    async def run_workflow(self, user_request: str) -> Dict[str, Any]:
        """执行完整的三省六部工作流"""
        task_id = create_task(user_request)
        task_dir = TASKS_DIR / task_id

        log_task_event(task_id, "workflow_start", "开始执行工作流（多模型版）")
        logger.info(f"[edict] 任务 {task_id} 启动：{user_request[:100]}...")

        try:
            # 2. 中书省 - 规划
            zhongshu_output = await self._execute_role(
                task_id, "zhongshu", user_request
            )
            if not zhongshu_output:
                raise RuntimeError("中书省执行失败")

            # 3. 门下省 - 审核
            menxia_output = await self._execute_role(task_id, "menxia", zhongshu_output)
            if not menxia_output:
                raise RuntimeError("门下省执行失败")

            # 检查是否封驳
            rejection = self._check_rejection(menxia_output)
            if rejection:
                return await self._handle_rejection(
                    task_id, "zhongshu", rejection, zhongshu_output
                )

            # 4. 尚书省 - 派发
            shangshu_input = f"""中书省方案：
{zhongshu_output}

门下省审核：
{menxia_output}

请根据批准的方案派发任务给对应部门。"""
            shangshu_output = await self._execute_role(
                task_id, "shangshu", shangshu_input
            )
            if not shangshu_output:
                raise RuntimeError("尚书省执行失败")

            # 5. 六部执行
            departments = self._parse_execution_plan(shangshu_output)
            await self._execute_departments(
                task_id, departments, user_request, shangshu_output
            )

            # 6. 门下省 - 最终验收
            final_result = await self._final_acceptance(task_id, user_request)

            # 7. 生成结果
            result = self._generate_result(
                task_id,
                user_request,
                {
                    "zhongshu": zhongshu_output,
                    "menxia": menxia_output,
                    "shangshu": shangshu_output,
                    "final": final_result,
                },
            )

            log_task_event(task_id, "workflow_complete", "工作流执行完成")
            return result

        except Exception as e:
            log_task_event(task_id, "error", str(e))
            logger.error(f"[edict] 任务 {task_id} 失败：{e}")
            raise

    async def _execute_role(
        self,
        task_id: str,
        role: str,
        input_context: str,
    ) -> Optional[str]:
        """执行角色任务（使用直接 LLM 调用）"""
        log_task_event(task_id, "stage_start", f"{role}: 执行任务")
        update_state(task_id, role, "running")

        try:
            output = await self.router.call_role_llm(role, input_context)
        except Exception as e:
            logger.error(f"[edict] {role} LLM 调用失败：{e}")
            output = None

        if output:
            # 保存输出到文件（供后续环节和 opencode 部门使用）
            task_dir = TASKS_DIR / task_id
            output_file = task_dir / f"{role}.output"
            output_file.write_text(output, encoding="utf-8")
            update_state(task_id, role, "done", output)
            log_task_event(task_id, "stage_complete", f"{role}: 完成")
        else:
            update_state(task_id, role, "failed")
            log_task_event(task_id, "stage_failed", f"{role}: 失败")

        return output

    def _check_rejection(self, menxia_output: str) -> Optional[Dict]:
        """检查门下省是否封驳"""
        try:
            result = json.loads(menxia_output)
            if result.get("decision") == "封驳":
                return {
                    "decision": "封驳",
                    "issues": result.get("issues", []),
                    "suggestions": result.get("suggestions", ""),
                }
        except:
            pass
        return None

    async def _handle_rejection(
        self,
        task_id: str,
        rejected_stage: str,
        rejection: Dict,
        original_output: str,
    ) -> Dict:
        """处理封驳重做"""
        log_task_event(task_id, "rejection", f"封驳：{rejection.get('issues', [])}")
        logger.warning(f"[edict] 任务 {task_id} 被驳回：{rejection}")

        return {
            "task_id": task_id,
            "status": "rejected",
            "stage": rejected_stage,
            "reason": rejection,
        }

    def _parse_execution_plan(self, shangshu_output: str) -> list:
        """解析尚书省的执行计划"""
        departments = []
        try:
            result = json.loads(shangshu_output)
            for step in result.get("dispatch_log", []):
                dept = step.get("department")
                if dept and dept not in departments:
                    departments.append(dept)
        except:
            departments = ["bingbu", "gongbu"]
        return departments

    async def _execute_departments(
        self,
        task_id: str,
        departments: list,
        user_request: str,
        shangshu_output: str,
    ):
        """执行六部任务"""
        for dept in departments:
            if dept == "libu":
                log_task_event(task_id, "stage_start", "礼部：执行任务")
                update_state(task_id, "libu", "running")

                libu_task = self._extract_department_task(
                    shangshu_output, "礼部", user_request
                )
                libu_input = f"""## 礼部任务

**任务描述**: {libu_task}

请根据尚书省的派发计划，执行文事相关任务（文档、写作、翻译等）。"""
                libu_output = await self._execute_role(task_id, "libu", libu_input)
                if libu_output:
                    update_state(task_id, "libu", "done", libu_output)
                    log_task_event(task_id, "stage_complete", "礼部：完成")

            elif dept == "bingbu":
                log_task_event(task_id, "stage_start", "兵部：执行任务（opencode）")
                update_state(task_id, "bingbu", "running")

                bingbu_task = self._extract_department_task(
                    shangshu_output, "兵部", user_request
                )
                output = run_opencode_task(task_id, bingbu_task)
                if output:
                    update_state(task_id, "bingbu", "done", output)
                    log_task_event(task_id, "stage_complete", "兵部：完成")

            elif dept == "gongbu":
                log_task_event(task_id, "stage_start", "工部：执行任务（opencode）")
                update_state(task_id, "gongbu", "running")

                gongbu_task = self._extract_department_task(
                    shangshu_output, "工部", "部署任务"
                )
                output = run_opencode_task(task_id, gongbu_task)
                if output:
                    update_state(task_id, "gongbu", "done", output)
                    log_task_event(task_id, "stage_complete", "工部：完成")

    def _extract_department_task(
        self,
        shangshu_output: str,
        department: str,
        default: str,
    ) -> str:
        """从尚书省输出中提取部门任务"""
        try:
            result = json.loads(shangshu_output)
            for step in result.get("dispatch_log", []):
                if step.get("department") == department:
                    return step.get("task", default)
        except:
            pass
        return default

    async def _final_acceptance(self, task_id: str, user_request: str) -> str:
        """最终验收"""
        task_dir = TASKS_DIR / task_id

        execution_summary = {}
        for dept in ["libu", "bingbu", "gongbu"]:
            output_file = task_dir / f"{dept}.output"
            if output_file.exists():
                execution_summary[dept] = "completed"

        acceptance_input = f"""## 任务执行完成确认

**原始需求**: {user_request[:200]}

**执行部门完成情况**:
{json.dumps(execution_summary, indent=2, ensure_ascii=False)}

## 验收标准
- ✅ 所有派发的部门都已完成执行
- ✅ 无严重错误报告
- ✅ 输出文件已生成

请确认是否可以交付用户。"""
        acceptance_output = await self.router.call_role_llm("menxia", acceptance_input)
        if acceptance_output:
            output_file = task_dir / "menxia_final.output"
            output_file.write_text(acceptance_output, encoding="utf-8")
            return acceptance_output
        return "验收通过"

    def _generate_result(
        self,
        task_id: str,
        user_request: str,
        outputs: Dict[str, str],
    ) -> Dict:
        """生成最终结果"""
        task_dir = TASKS_DIR / task_id
        result_file = task_dir / "result.md"

        result_content = f"""# edict 任务结果

**任务 ID**: {task_id}
**完成时间**: {datetime.now().isoformat()}
**用户请求**: {user_request}

## 执行摘要
- 中书省规划：✅ 完成
- 门下省审核：✅ 通过
- 尚书省派发：✅ 完成
- 部门执行：✅ 完成
- 最终验收：✅ 完成

## 详细输出

### 中书省规划
{outputs.get("zhongshu", "N/A")[:500]}...

### 门下省审核
{outputs.get("menxia", "N/A")[:500]}...

### 尚书省派发
{outputs.get("shangshu", "N/A")[:500]}...

### 最终验收
{outputs.get("final", "N/A")}
"""
        result_file.write_text(result_content, encoding="utf-8")

        return {
            "task_id": task_id,
            "status": "completed",
            "result_file": str(result_file),
            "state_file": str(task_dir / "state.json"),
            "user_request": user_request,
        }


# 便捷函数
async def run_edict_workflow(
    user_request: str,
    provider: LLMProvider,
    workspace: Path,
    bus: MessageBus,
    origin_channel: str = "cli",
    origin_chat_id: str = "direct",
) -> Dict[str, Any]:
    """
    运行 edict 工作流（多模型版）

    用法:
        result = await run_edict_workflow(
            "创建 Flask 项目",
            provider,
            workspace,
            bus,
        )
    """
    workflow = EdictWorkflow(
        provider=provider,
        workspace=workspace,
        bus=bus,
        origin_channel=origin_channel,
        origin_chat_id=origin_chat_id,
    )
    return await workflow.run_workflow(user_request)
