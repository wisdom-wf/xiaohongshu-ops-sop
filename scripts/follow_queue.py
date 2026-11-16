#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""关注回扫队列 —— 上一轮留言过的博主，下一轮开头先去关注。

流程位置（每轮外联）：
    ① gate     → 本轮能不能关注、能关注几个
    ② pending  → 取待关注博主（按留言时间降序，先补最近一轮）
    ③ 【人工/浏览器执行】回到那条笔记 → 进作者主页 → 点关注
    ④ record   → 回写关注结果（done / failed / skipped）

关注是对外可见动作，比留言更敏感，因此：
  - 失败不重试（failed 直接出队，不重复打扰）；
  - 出现「操作频繁 / 验证码 / 登录失效」记 skipped 并停止当天全部关注；
  - 每日上限同时受 daily_max 与「滚动 24h 成功留言数」双重约束，不能关注得比留言还多。
    （晚间高峰允许跨零点，若按自然日留言数计会把昨晚的回扫永久挡住。）

用法：
    python3 <skill-dir>/scripts/follow_queue.py gate    --config xiaohongshu-ops.yaml
    python3 <skill-dir>/scripts/follow_queue.py pending --config xiaohongshu-ops.yaml --limit 2
    python3 <skill-dir>/scripts/follow_queue.py record  --config xiaohongshu-ops.yaml \
        --author-id <id> --result done --note ""
    python3 <skill-dir>/scripts/follow_queue.py stats   --config xiaohongshu-ops.yaml
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from _common import (
    SkillError,
    load_yaml,
    scheduling_windows,
    today_in_timezone,
    validate_config,
)
from outreach_ledger import (
    in_active_window,
    ledger_path,
    read_rows,
    row_at,
    success_rows,
)

# 出现这些信号 = 账号风险，当天停止一切关注
RISK_SIGNALS = ("操作频繁", "验证码", "登录失效", "风险提示", "被限制", "异常")


def follow_cfg(config: dict) -> dict:
    return ((config.get("engagement") or {}).get("outreach") or {}).get(
        "follow_back") or {}


def atomic_write(path: pathlib.Path, rows: list[dict]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8")
    os.replace(tmp, path)


def is_pending(row: dict, self_id: str) -> bool:
    """待关注判定。旧记录没有 follow 字段，按 pending 处理以便兼容。"""
    if row.get("result") != "success":
        return False
    if self_id and str(row.get("author_id") or "") == self_id:
        return False
    state = row.get("follow")
    if state is None:
        return True
    return state == "pending"


def today_stats(config: dict, rows: list[dict], date_str: str) -> dict:
    tz = config["account"]["timezone"]
    followed = 0
    for r in rows:
        if r.get("date") != date_str:
            continue
        if r.get("follow") == "done" and r.get("follow_at", "").startswith(date_str):
            followed += 1
    commented = sum(1 for r in success_rows(rows) if r.get("date") == date_str)
    return {"date": date_str, "followed_today": followed,
            "comments_today": commented, "tz": tz}


def cmd_gate(args, config: dict, path: pathlib.Path) -> int:
    tz = config["account"]["timezone"]
    fc = follow_cfg(config)
    now = datetime.now(ZoneInfo(tz))
    date_str = now.date().isoformat()
    rows = read_rows(path)
    st = today_stats(config, rows, date_str)

    reasons: list[str] = []
    if not fc.get("enabled", False):
        reasons.append("follow_back.enabled=false")
    schedule = config["engagement"]["outreach"].get("scheduling") or {}
    windows = scheduling_windows(schedule)
    if not in_active_window(now, schedule):
        reasons.append(f"当前不在活跃窗口 {', '.join(windows)}（静默段 {schedule.get('quiet_window') or '-'}）")

    daily_max = int(fc.get("daily_max", 10))
    # 跨零点晚间场不能按「今日留言数」卡，否则 23:50 留言、00:10 回扫
    # 会因新自然日 comments_today=0 被永久挡住。改按【滚动 24h 成功留言数】，
    # 与 follow_back.max_age_hours 的回扫窗口对齐，安全边界不放松（仍 ≤ daily_max）。
    lookback_hours = int(fc.get("max_age_hours", 24))
    cutoff = now - timedelta(hours=lookback_hours)
    recent_comments = 0
    for r in success_rows(rows):
        t = row_at(r, tz)
        if t is not None and cutoff <= t <= now:
            recent_comments += 1
    allowed_by_comments = recent_comments
    # 近 24h 无成功留言时，队列本就为空，语义是「没有可回扫对象」，
    # 不是「关注已达上限」/账号限流。提示必须写清语义，避免执行者把
    # gate=false 误读成「主任务受阻」而整轮止损。
    # 判定逻辑与风控边界（关注数 ≤ 近 24h 留言数 ≤ daily_max）完全不变。
    if allowed_by_comments == 0:
        reasons.append(
            "近24h无成功留言 → 关注回扫队列为空（非上限触顶、非限流）；"
            "gate 只约束『关注回扫』，不阻断留言 / 回复 / 首评，按要点①直接进留言环节")
    elif st["followed_today"] >= min(daily_max, allowed_by_comments):
        reasons.append(
            f"关注已达上限（已关注 {st['followed_today']}，"
            f"上限 min(daily_max={daily_max}, 近{lookback_hours}h留言={allowed_by_comments}）")

    # 相邻关注间隔
    gap = int(fc.get("min_gap_minutes", 10))
    last_at = None
    for r in rows:
        if r.get("follow") == "done" and r.get("follow_at"):
            t = row_at({"at": r["follow_at"]}, tz)
            if t and (last_at is None or t > last_at):
                last_at = t
    wait_minutes = 0
    if last_at is not None:
        elapsed = (now - last_at).total_seconds() / 60
        if elapsed < gap:
            wait_minutes = int(gap - elapsed) + 1
            reasons.append(f"距上次关注仅 {int(elapsed)} 分钟，需间隔 {gap} 分钟")

    queue = build_queue(config, rows, now, limit=None)
    per_round = int(fc.get("max_per_round", 2))
    budget = min(per_round,
                 max(0, min(daily_max, allowed_by_comments) - st["followed_today"]))

    out = {
        "ok": not reasons,
        "block_reasons": reasons,
        # 显式声明 gate 的作用域，避免执行者把「关注回扫不可做」放大成「整轮外联不可做」。
        "scope": "follow_back_only",
        "blocks_only": ["follow_back"],
        "does_not_block": ["outreach_comments", "reply", "backfill"],
        "now": now.isoformat(),
        "active_window": ", ".join(windows),
        "active_windows": windows,
        "peak_windows": list(schedule.get("peak_windows") or []),
        "quiet_window": schedule.get("quiet_window"),
        "followed_today": st["followed_today"],
        "comments_today": st["comments_today"],
        "recent_comments_24h": recent_comments,
        "comment_cap_basis": f"rolling_{lookback_hours}h_success",
        "daily_max": daily_max,
        "max_per_round": per_round,
        "pending_count": len(queue),
        "budget_this_round": budget if not reasons else 0,
        "wait_minutes": wait_minutes,
        "queue_preview": [
            {"author_id": q["author_id"], "author_name": q["author_name"],
             "note_url": q["note_url"], "commented_at": q["at"],
             "age_hours": q["age_hours"]}
            for q in queue[:budget]
        ],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    # gate 是【只读探测】命令，不是断言门禁：ok=false 时也返回 0，
    # 避免用 && 串联多个只读检查时被它掐断后续检查。
    # 是否可做关注回扫一律以 JSON 的 ok / budget_this_round 为准，进程退出码恒为 0。
    return 0


def build_queue(config: dict, rows: list[dict], now: datetime,
                limit: int | None) -> list[dict]:
    tz = config["account"]["timezone"]
    fc = follow_cfg(config)
    self_id = str((config.get("account") or {}).get("user_id") or "")
    max_age = int(fc.get("max_age_hours", 72))

    out = []
    for r in rows:
        if not is_pending(r, self_id):
            continue
        at = row_at(r, tz)
        if at is None:
            continue
        age_hours = (now - at).total_seconds() / 3600
        if age_hours > max_age:
            continue          # 留言太久远，回扫关注的价值下降，直接出队
        item = dict(r)
        item["age_hours"] = round(age_hours, 1)
        out.append(item)

    # 最近的留言先关注 —— 用户要的是「下一轮开头先去**上一轮**留言的位置」，
    # 不是翻旧账。队列按留言时间降序。
    out.sort(key=lambda r: str(r.get("at") or ""), reverse=True)
    return out[:limit] if limit else out


def cmd_pending(args, config: dict, path: pathlib.Path) -> int:
    tz = config["account"]["timezone"]
    now = datetime.now(ZoneInfo(tz))
    rows = read_rows(path)
    queue = build_queue(config, rows, now, limit=args.limit)
    print(json.dumps({
        "count": len(queue),
        "items": [
            {
                "author_id": q.get("author_id"),
                "author_name": q.get("author_name"),
                "note_url": q.get("note_url"),
                "commented_at": q.get("at"),
                "age_hours": q["age_hours"],
                "kind": q.get("kind"),
                # 回扫动作：回到这条笔记 → 进作者主页 → 关注
                "action": "打开 note_url → 点作者昵称进主页 → 点关注",
                "profile_url": f"https://www.xiaohongshu.com/user/profile/{q.get('author_id')}",
            }
            for q in queue
        ],
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_record(args, config: dict, path: pathlib.Path) -> int:
    tz = config["account"]["timezone"]
    now = datetime.now(ZoneInfo(tz))
    if args.result not in ("done", "failed", "skipped"):
        print("ERROR --result must be one of done|failed|skipped", file=sys.stderr)
        return 2
    rows = read_rows(path)
    hit = False
    for r in reversed(rows):
        if str(r.get("author_id") or "") != str(args.author_id or ""):
            continue
        # 老记录没有 follow 字段，按 pending 兼容；显式标过其它状态的才跳过
        state = r.get("follow")
        if state is not None and state != "pending":
            continue
        r["follow"] = args.result
        r["follow_at"] = now.isoformat()
        r["follow_note"] = args.note or ""
        hit = True
        break
    if not hit:
        print(json.dumps({"ok": False,
                          "error": f"没有找到 author_id={args.author_id} 的待关注记录"},
                         ensure_ascii=False))
        return 1

    risk = any(sig in (args.note or "") for sig in RISK_SIGNALS)
    if args.dry_run:
        print(json.dumps({"dry_run": True, "row": r, "risk_signal": risk,
                          "stop_today": risk}, ensure_ascii=False, indent=2))
        return 0

    atomic_write(path, rows)
    print(json.dumps({
        "ok": True,
        "recorded": {"author_id": args.author_id, "follow": args.result,
                     "follow_at": r["follow_at"], "note": args.note or ""},
        "risk_signal": risk,
        # 命中风控信号 → 当天停止一切关注，不只是这一条
        "stop_today": risk and str(follow_cfg(config).get(
            "on_risk_signal", "stop")) == "stop",
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_backfill(args, config: dict, path: pathlib.Path) -> int:
    """把超出回扫窗口的历史 pending 归档为 expired。

    老台账没有 follow 字段，按兼容规则会被当成 pending，一开跑就涌进几十个
    待关注——一天关注几十个人必然触发风控。这里按时间窗做一次性归档。
    """
    tz = config["account"]["timezone"]
    now = datetime.now(ZoneInfo(tz))
    hours = args.hours if args.hours is not None else int(
        follow_cfg(config).get("max_age_hours", 72))
    rows = read_rows(path)
    self_id = str((config.get("account") or {}).get("user_id") or "")
    changed = []
    for r in rows:
        if r.get("result") != "success" or not is_pending(r, self_id):
            continue
        at = row_at(r, tz)
        if at is None:
            continue
        age = (now - at).total_seconds() / 3600
        if age <= hours:
            continue
        changed.append({
            "author_name": r.get("author_name"),
            "at": r.get("at"),
            "age_hours": round(age, 1),
        })
        if not args.dry_run:
            r["follow"] = "expired"
            r["follow_at"] = now.isoformat()
            r["follow_note"] = f"超出回扫窗口 {hours}h，初始化归档"

    if not args.dry_run and changed:
        atomic_write(path, rows)

    print(json.dumps({
        "dry_run": bool(args.dry_run),
        "window_hours": hours,
        "changed_count": len(changed),
        "changed": changed[:20],
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_stats(args, config: dict, path: pathlib.Path) -> int:
    tz = config["account"]["timezone"]
    date_str = args.date or today_in_timezone(tz).isoformat()
    rows = read_rows(path)
    st = today_stats(config, rows, date_str)
    now = datetime.now(ZoneInfo(tz))
    pending = build_queue(config, rows, now, limit=None)
    by_state: dict[str, int] = {}
    for r in rows:
        # 非成功记录本来就没有可关注对象，显示 n/a 而不是 unknown
        key = str(r.get("follow")
                  or ("n/a" if r.get("result") != "success" else "legacy"))
        by_state[key] = by_state.get(key, 0) + 1
    print(json.dumps({
        "date": date_str,
        "followed_today": st["followed_today"],
        "comments_today": st["comments_today"],
        "daily_max": int(follow_cfg(config).get("daily_max", 10)),
        "pending_total": len(pending),
        "pending_overdue": sum(
            1 for p in pending
            if p["age_hours"] > int(follow_cfg(config).get("max_age_hours", 72))),
        "by_state": by_state,
    }, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Follow-back queue for outreach.")
    parser.add_argument("--config", default="xiaohongshu-ops.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("gate", "pending", "record", "stats", "backfill"):
        sp = sub.add_parser(name)
        sp.add_argument("--config", default=argparse.SUPPRESS)
        sp.add_argument("--file")

    bf = sub.choices["backfill"]
    bf.add_argument("--hours", type=int, default=None,
                    help="留言超过 N 小时就不再回扫关注；默认取 follow_back.max_age_hours")
    bf.add_argument("--dry-run", action="store_true")

    sub.choices["pending"].add_argument("--limit", type=int, default=2)
    rec = sub.choices["record"]
    rec.add_argument("--author-id", required=True)
    rec.add_argument("--result", required=True, choices=("done", "failed", "skipped"))
    rec.add_argument("--note", default="")
    rec.add_argument("--dry-run", action="store_true")
    sub.choices["stats"].add_argument("--date")

    args = parser.parse_args()
    config = load_yaml(args.config)
    errors = [x for x in validate_config(config) if x["level"] == "error"]
    if errors:
        for e in errors:
            print(f"ERROR [{e['code']}] {e['message']}", file=sys.stderr)
        return 2
    path = ledger_path(config, args.config, getattr(args, "file", None))
    try:
        if args.command == "gate":
            return cmd_gate(args, config, path)
        if args.command == "pending":
            return cmd_pending(args, config, path)
        if args.command == "record":
            return cmd_record(args, config, path)
        if args.command == "backfill":
            return cmd_backfill(args, config, path)
        return cmd_stats(args, config, path)
    except SkillError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
