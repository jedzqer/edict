# edict-dev 发布检查清单

## ✅ 隐私清理完成

### 已忽略的敏感文件 (.gitignore)
- [x] `models.json` - API 密钥配置（已删除，仅提供 models.json.example 模板）
- [x] `.venv/` - Python 虚拟环境
- [x] `__pycache__/` - Python 缓存
- [x] `tasks/` - 任务状态数据
- [x] `history/` - 日志文件
- [x] `*.db` - SQLite 数据库
- [x] `*.log` - 日志文件

### 已删除的开发过程文档
- [x] `OPENCODE_COMPLETION_REPORT.md` - 开发过程报告
- [x] `INTEGRATION_SUMMARY.md` - 整合报告
- [x] `langgraph-research.md` - 研究文档
- [x] `langgraph-quickstart.md` - 快速入门
- [x] `langgraph-architecture.md` - 架构图

### 已清理的隐私信息
- [x] 本地路径 `/home/ubuntu/...` → `<workspace>/...`
- [x] 具体日期 `2026-03-29` → `YYYY-MM-DD`
- [x] 任务 ID `task-20260329-094000` → `task-<timestamp>`
- [x] 维护者信息 → 已移除
- [x] 最后更新日期 → 已移除

### 保留的核心文件
- [x] `README.md` - 项目说明
- [x] `SKILL.md` - Skill 定义
- [x] `INSTALL.md` - 安装指南
- [x] `LANGGRAPH.md` - LangGraph 集成指南
- [x] `PROTOCOL.md` - 通信协议
- [x] `SECURITY.md` - 安全规范
- [x] `ESCALATION.md` - 升级机制
- [x] `langgraph_workflow.py` - 核心工作流
- [x] `nanobot_edict.py` - nanobot 集成
- [x] `requirements.txt` - 依赖列表
- [x] `roles/` - 角色提示词目录

## 📋 发布前检查

### 1. 确认 models.json 未提交
```bash
git status  # models.json 应显示为 ignored
```

### 2. 配置示例（用户需自行创建 models.json）
```json
{
  "models": {
    "zhongshu": {
      "provider": "your-provider",
      "endpoint": "https://api.example.com/v1",
      "model": "your-model",
      "api_key": "sk-your-api-key",
      "temperature": 0.7
    }
  }
}
```

### 3. 环境变量配置
```bash
export DASHSCOPE_API_KEY="sk-your-key"
```

## 🚀 发布命令

```bash
cd <workspace>/skills/edict-dev
git init
git add .
git commit -m "edict-dev: LangGraph 三省六部制 AI 协作系统"
git remote add origin <your-repo-url>
git push -u origin main
```

---

**状态**: ✅ 可安全发布
