# edict-dev 安装指南

## 安装

推荐使用虚拟环境：

```bash
cd /path/to/edict
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

如果你不使用虚拟环境，至少也需要：

```bash
pip install -r requirements.txt
```

---

## 模型配置

当前工作流会读取仓库根目录的 `models.json`：

```bash
cp models.json.example models.json
```

然后为各角色填写：

- `model`
- `api_key`
- `temperature`

当前代码的几个重要事实：

- 主程序使用的是 `langchain_openai.ChatOpenAI`
- 主程序当前固定使用 `https://api.siliconflow.cn/v1`
- `models.json.example` 里的 `provider` 和 `endpoint` 目前主要是说明性字段，主流程不会读取它们

---

## 运行

```bash
cd /path/to/edict
source .venv/bin/activate
python langgraph_workflow.py "创建一个简单的 Flask API"
```

---

## 可选配置

如果你想开启 LangSmith tracing：

```bash
export LANGCHAIN_API_KEY="your-langsmith-key"
export LANGCHAIN_PROJECT="edict"
```

---

## 运行后会生成什么

通常会看到这些产物：

- `tasks.db`
- `tasks/{task_id}/session.log`
- `tasks/{task_id}/*.output`
- `tasks/{task_id}/result_langgraph.json`
- `history/YYYYMMDD.log`

---

## 常见问题

### 1. `No module named 'langchain_openai'`

原因：依赖未按 `requirements.txt` 正确安装，或当前虚拟环境未激活。

解决：

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 配了 `DASHSCOPE_API_KEY` 但还是调用失败

原因：当前主工作流不是靠 `DASHSCOPE_API_KEY` 读配置，而是主要读 `models.json`。

解决：

```bash
cp models.json.example models.json
```

然后检查每个角色的 `model` 和 `api_key`。

### 3. 改了 `models.json` 里的 `endpoint` 但没有生效

原因：当前代码把 `base_url` 固定在 `langgraph_workflow.py` 里，没有读取 `endpoint`。

解决：

- 如果你使用的是当前默认兼容接口，只需要正确填写 `model` 和 `api_key`
- 如果你要切换到其他 OpenAI 兼容服务，需要修改 [langgraph_workflow.py](langgraph_workflow.py)

---

## 说明

- `requirements.txt` 已包含当前主程序所需运行依赖
- 运行入口是 [langgraph_workflow.py](langgraph_workflow.py)
- 升级/申诉机制的说明在 [ESCALATION.md](ESCALATION.md)，但当前还没有完整自动化
