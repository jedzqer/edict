with open('langgraph_workflow.py', 'r') as f:
    content = f.read()

content = content.replace(
'''try:
    from langchain_dashscope import ChatDashScope
except ImportError as _import_err:
    raise ImportError(f"无法导入 langchain_dashscope，请执行：pip install langchain-dashscope") from _import_err''',
'''try:
    from langchain_openai import ChatOpenAI
except ImportError as _import_err:
    raise ImportError(f"无法导入 langchain_openai，请执行：pip install langchain-openai") from _import_err'''
)

content = content.replace(
'''def _get_llm_for_role(role: str) -> "ChatDashScope":
    """按角色返回 LLM 实例；若 models.json 有对应配置则使用；否则回退到默认"""
    role_cfg = _MODEL_CONFIG.get(role, {})
    model = role_cfg.get("model", "qwen-plus")
    api_key = role_cfg.get("api_key") or _DASHSCOPE_API_KEY
    temperature = role_cfg.get("temperature", 0.7)
    return ChatDashScope(
        model=model,
        dashscope_api_key=api_key,
        temperature=temperature,
    )''',
'''def _get_llm_for_role(role: str) -> "ChatOpenAI":
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
    )'''
)

with open('langgraph_workflow.py', 'w') as f:
    f.write(content)
