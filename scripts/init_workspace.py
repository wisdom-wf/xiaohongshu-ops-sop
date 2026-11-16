#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Create a local account workspace from the skill's example config."""
from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sys

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML is required: python3 -m pip install pyyaml") from exc

SKILL_DIR = pathlib.Path(__file__).resolve().parents[1]
TEMPLATE = SKILL_DIR / "assets" / "config.example.yaml"

LOCAL_README = """# 小红书运营工作区

这个目录由 `xiaohongshu-ops-sop` 初始化。

## 先完善配置并校验

1. 编辑 `xiaohongshu-ops.yaml`：替换所有 `__占位符__`（账号名、userId、定位、人群、支柱、地域词、搜索词、目标值）。
2. 校验：

```bash
python3 {skill}/scripts/doctor.py --config xiaohongshu-ops.yaml
```

依赖只需 Python 3.10+ 与 PyYAML；浏览器执行层用你环境里的任意浏览器适配器。

## 生成今日外联计划

```bash
python3 {skill}/scripts/plan_outreach.py --config xiaohongshu-ops.yaml --format md
```

## 审核一批评论

```bash
python3 {skill}/scripts/lint_comments.py --config xiaohongshu-ops.yaml --input comments.json
```

## 记录一条已发送外联

```bash
python3 {skill}/scripts/outreach_ledger.py record \
  --config xiaohongshu-ops.yaml \
  --round-id ROUND_ID \
  --kind same_topic \
  --author-id USER_ID \
  --author-name NICKNAME \
  --note-url NOTE_URL \
  --text '评论原文' \
  --result success
```

## 查看今日外联进度

```bash
python3 {skill}/scripts/outreach_ledger.py status --config xiaohongshu-ops.yaml
```

## 检查发布包

```bash
python3 {skill}/scripts/check_publish_package.py publish/my-note \
  --config xiaohongshu-ops.yaml --kind image
```

## 生成数据报告

```bash
python3 {skill}/scripts/analyze_metrics.py \
  --config xiaohongshu-ops.yaml \
  --input data/metrics.json \
  --markdown data/reports/analysis.md \
  --json-out data/reports/analysis.json
```

## 数据目录

- `data/reports/`：数据分析和日报
- `data/logs/`：执行日志
- `data/outreach.jsonl`：外联台账
- `drafts/`：草稿
- `publish/`：发布包
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialize an account workspace.")
    parser.add_argument("target_dir", nargs="?", default=".")
    parser.add_argument("--account-name", default="未命名账号")
    parser.add_argument("--force", action="store_true", help="overwrite an existing config")
    args = parser.parse_args()

    target = pathlib.Path(args.target_dir).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    config_path = target / "xiaohongshu-ops.yaml"
    if config_path.exists() and not args.force:
        print(f"SKIP config already exists: {config_path}")
    else:
        config = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
        config["account"]["name"] = args.account_name
        config_path.write_text(
            yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        print(f"CREATED {config_path}")

    for rel in ("data", "data/reports", "data/logs", "data/state", "drafts", "publish"):
        p = target / rel
        p.mkdir(parents=True, exist_ok=True)
    ledger = target / "data" / "outreach.jsonl"
    ledger.touch(exist_ok=True)

    readme = target / "XIAOHONGSHU_OPS_LOCAL.md"
    if not readme.exists() or args.force:
        readme.write_text(LOCAL_README.format(skill="xiaohongshu-ops-sop"), encoding="utf-8")
        print(f"CREATED {readme}")

    gitignore = target / ".gitignore"
    required = [
        ".env",
        "*.cookies",
        "data/logs/*.jsonl",
        "data/state/*.json",
        "screenshots/",
        "*.tmp",
    ]
    existing = gitignore.read_text(encoding="utf-8").splitlines() if gitignore.exists() else []
    merged = existing + [x for x in required if x not in existing]
    gitignore.write_text("\n".join(merged).rstrip() + "\n", encoding="utf-8")
    print(f"READY {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
