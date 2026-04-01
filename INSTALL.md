# edict-dev 安装指南

## 📦 依赖安装

### 方式一：使用虚拟环境（推荐）

```bash
# 进入目录
cd <workspace>/skills/edict-dev

# 创建虚拟环境
python3 -m venv .venv

# 激活虚拟环境
source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt
```

### 方式二：直接安装到系统（不推荐）

```bash
pip install langgraph langchain langchain-dashscope==0.1.8
```

---

## ✅ 已安装包版本

| 包名 | 版本 | 用途 |
|------|------|------|
| **langgraph** | 1.1.4 | 图工作流引擎 |
| **langchain** | 0.3.28 | LangChain 框架 |
| **langchain-core** | 0.3.83 | LangChain 核心 |
| **langchain-dashscope** | 0.1.8 | DashScope 集成 |
| **dashscope** | 1.25.15 | 阿里云通义千问 SDK |

---

## 🔑 配置 API Key

使用前需要设置 DashScope API Key：

```bash
# 临时设置（当前终端会话）
export DASHSCOPE_API_KEY="sk-xxxxxxxxxxxxxxxx"

# 永久设置（添加到 ~/.bashrc 或 ~/.zshrc）
echo 'export DASHSCOPE_API_KEY="sk-xxxxxxxxxxxxxxxx"' >> ~/.bashrc
source ~/.bashrc
```

---

## 🚀 运行测试

```bash
# 激活虚拟环境
cd <workspace>/skills/edict-dev
source .venv/bin/activate

# 运行工作流
python langgraph_workflow.py "创建一个简单的 Flask API"
```

---

## ⚠️ 注意事项

### 兼容性问题

`langchain-dashscope==0.1.8` 需要 `langchain-core<1.0.0`，安装时请注意版本：

```bash
# ✅ 正确的版本组合
pip install "langchain-core<1.0.0" "langchain<1.0.0" "langchain-dashscope==0.1.8"

# ❌ 错误的版本组合（会导致导入错误）
pip install "langchain-core>=1.0.0" "langchain-dashscope==0.1.8"
```

### 警告信息

运行时可能会出现以下警告（可以忽略）：

```
LangChainDeprecationWarning: As of langchain-core 0.3.0, LangChain uses pydantic v2 internally.
The langchain_core.pydantic_v1 module was a compatibility shim for pydantic v1...
```

这是 langchain-dashscope 使用旧版 pydantic v1 兼容性模块导致的，不影响功能。

---

## 📁 虚拟环境管理

```bash
# 激活虚拟环境
source .venv/bin/activate

# 退出虚拟环境
deactivate

# 查看已安装包
.venv/bin/pip list

# 更新依赖
.venv/bin/pip install --upgrade -r requirements.txt
```

---

## 🔧 故障排查

### 问题 1：导入错误 `No module named 'langchain_core.pydantic_v1'`

**原因**: langchain-core 版本过高

**解决**:
```bash
pip install "langchain-core<1.0.0" "langchain<1.0.0"
```

### 问题 2：`DASHSCOPE_API_KEY` 未设置

**原因**: 未配置 API Key

**解决**:
```bash
export DASHSCOPE_API_KEY="sk-xxxxxxxxxxxxxxxx"
```

### 问题 3：opencode 未找到

**原因**: opencode 未安装或未在 PATH 中

**解决**:
```bash
# 检查 opencode 是否可用
which opencode

# 如果未安装，参考 opencode skill 文档安装
```

---

## 📖 参考文档

- [LangGraph 官方文档](https://docs.langchain.com/oss/python/langgraph/)
- [LangChain 文档](https://python.langchain.com/)
- [DashScope 文档](https://help.aliyun.com/zh/dashscope/)
- [edict-dev README](README.md)
- [LANGGRAPH.md](LANGGRAPH.md)

---
