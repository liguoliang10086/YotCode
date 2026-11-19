---
name: release
description: 发布流程清单——跑测试、更新 CHANGELOG、打 tag。用户提到"发版""打 tag""release"时使用。
allowed-tools: Bash, Read, Edit
---

# 发布流程

按顺序执行，**不要跳步，也不要自己发挥**。

## 1. 确认工作区干净

```bash
git status
```

如果有未提交的改动，先停下来问用户要不要一起带上。

## 2. 跑测试

```bash
python -m pytest -q
```

测试不通过就**立刻停止**，把失败用例和原因报告给用户，不要继续往下走。

## 3. 更新 CHANGELOG

读一下 `CHANGELOG.md`，在顶部补上这次版本的条目：

- 新增了什么
- 修复了什么
- 破坏性变更（如果有）

## 4. 打 tag

确认 CHANGELOG 写好后：

```bash
git tag -a v<版本号> -m "release v<版本号>"
git push origin v<版本号>
```

版本号问用户要，或者从 `pyproject.toml` 里读。

