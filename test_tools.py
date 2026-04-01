import json
import os
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool

@tool
def create_file(path: str, content: str) -> str:
    """Create a file with the given content."""
    return f"Created {path}"

llm = ChatOpenAI(
    model="Qwen/Qwen3-30B-A3B-Instruct-2507",
    api_key=os.environ.get("DASHSCOPE_API_KEY", "sk-cvkefytsralxrkqqfktrukkwxqftbkanrgpwdtghsdohuarp"),
    base_url="https://api.siliconflow.cn/v1"
)
llm_with_tools = llm.bind_tools([create_file])
try:
    res = llm_with_tools.invoke("Please create a file named test.txt with content 'hello'")
    print(res.tool_calls)
except Exception as e:
    print("Error:", e)
