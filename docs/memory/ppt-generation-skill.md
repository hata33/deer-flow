---
name: ppt-generation-skill
description: PPT 生成 skill 的执行流程、依赖库、路径问题及 Windows 兼容性
---

## Skill 位置

`skills/public/ppt-generation/`
- `SKILL.md` — 完整的生成指令（28K，含 8 种视觉风格）
- `scripts/generate.py` — 将 slide 图片合成 PPTX

## 执行流程

1. Agent 创建 JSON 演示计划 → `/mnt/user-data/workspace/`
2. 逐张生成 slide 图片（依赖 `skills/public/image-generation/`，顺序生成保持视觉一致性）
3. 调用 `generate.py` 将图片合成为 `.pptx` 文件

## 依赖库

```
Pillow (PIL) — 图片处理
python-pptx — PowerPoint 生成
```

这些库需要 bash 执行权限才能安装（`pip install`），参见 [[sandbox-system]]。

## 路径问题

Skill 的 SKILL.md 使用 Linux 虚拟路径（`/mnt/...`），在 LocalSandboxProvider 下会被翻译为 Windows 本地路径。如果沙箱层未正确翻译，agent 可能回退到 WSL 执行。

**排查步骤**：
1. 确认 `config.yaml` 中 `sandbox.use` 为 `LocalSandboxProvider`
2. 确认 `allow_host_bash: true`（否则 bash 命令被拦截）
3. 检查虚拟路径翻译日志

## generate.py 命令

```bash
python scripts/generate.py \
  --plan-file <plan.json 绝对路径> \
  --slide-images <img1.jpg> <img2.jpg> ... \
  --output-file <output.pptx>
```

支持 16:9 和 4:3 比例，自动将 PNG 转 JPEG 以提高 PPT 兼容性。

## Why: PPT 生成是用户首次接触 skill 执行问题的场景，记录其依赖和路径逻辑便于后续排查。
## How to apply: 当 skill 执行异常时，先检查沙箱配置 [[sandbox-system]]，再检查 image-generation skill 是否可用。
