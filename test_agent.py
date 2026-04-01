import os
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent
from langchain_core.messages import SystemMessage

@tool
def create_file(path: str, content: str) -> str:
    """Create a file with the given content."""
    with open(path, 'w') as f:
        f.write(content)
    return f"Success: created {path}"

llm = ChatOpenAI(
    model="Qwen/Qwen3-30B-A3B-Instruct-2507",
    api_key=os.environ.get("DASHSCOPE_API_KEY", "sk-cvkefytsralxrkqqfktrukkwxqftbkanrgpwdtghsdohuarp"),
    base_url="https://api.siliconflow.cn/v1"
)

agent = create_react_agent(llm, tools=[create_file], state_modifier=SystemMessage(content="You are a helpful assistant with tools."))
try:
    result = agent.invoke({"messages": [("user", "Please create a file named testing_qwen.txt with the content 'qwen is smart'")]})
    print(result["messages"][-1].content)
except Exception as e:
    print("Error:", e)
