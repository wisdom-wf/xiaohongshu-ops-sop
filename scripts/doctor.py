#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validate the account config and local workspace health.

零第三方依赖（除 PyYAML）。浏览器适配器不绑定具体工具：
本脚本只校验配置、Python 环境与工作区目录；适配器健康由执行层自行确认。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import shutil
import sys

from _common import SkillError, load_yaml, validate_config

PLACEHOLDER_RE = re.compile(r"__.+?__")


def find_placeholders(config: dict) -> list[str]:
    """列出配置里尚未替换的 __占位符__ 字段路径。"""
    found: list[str] = []

    def walk(node, path: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else str(k))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
        elif isinstance(node, str) and PLACEHOLDER_RE.search(node):
            found.append(path)

    walk(config, "")
    return found


def build_report(config_path: pathlib.Path) -> dict:
    issues: list[dict[str, str]] = []
    config = None
    try:
        config = load_yaml(config_path)
        issues.extend(validate_config(config))
    except SkillError as exc:
        issues.append({"level": "error", "code": "config", "message": str(exc)})

    deps = {
        "python": sys.version.split()[0],
        "pyyaml": True,
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "node": shutil.which("node") is not None,
    }

    if config:
        placeholders = find_placeholders(config)
        if placeholders:
            issues.append({
                "level": "warning",
                "code": "placeholder",
                "message": "config still has __placeholders__ to fill: " + ", ".join(placeholders),
            })
        workspace = config.get("workspace") or {}
        for key in ("reports_dir", "logs_dir", "outreach_ledger"):
            value = workspace.get(key)
            if value:
                p = pathlib.Path(value)
                if not p.is_absolute():
                    p = config_path.parent / p
                if not p.exists():
                    issues.append({
                        "level": "warning",
                        "code": "workspace",
                        "message": f"workspace path not created yet: {p} (run init_workspace.py)",
                    })

    errors = [x for x in issues if x["level"] == "error"]
    warnings = [x for x in issues if x["level"] == "warning"]
    return {
        "ok": not errors,
        "config": str(config_path.resolve()),
        "automation": (config.get("automation", {}) if config else {}),
        "dependencies": deps,
        "errors": errors,
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Check config and workspace health.")
    parser.add_argument("--config", default="xiaohongshu-ops.yaml")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    report = build_report(pathlib.Path(args.config).expanduser())
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("xiaohongshu-ops-sop doctor")
        print(f"config: {report['config']}")
        automation = report.get("automation") or {}
        if automation:
            print(f"automation: {automation.get('mode', 'manual_only')} | create/resume requires explicit approval")
        print(f"python: {report['dependencies']['python']}")
        print(f"ffmpeg: {'ok' if report['dependencies']['ffmpeg'] else 'missing (only needed for video production)'}")
        print(f"node: {'ok' if report['dependencies']['node'] else 'missing (only needed by some browser adapters)'}")
        for item in report["errors"]:
            print(f"ERROR [{item['code']}] {item['message']}")
        for item in report["warnings"]:
            print(f"WARN  [{item['code']}] {item['message']}")
        print("PASS" if report["ok"] else "FAIL")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())