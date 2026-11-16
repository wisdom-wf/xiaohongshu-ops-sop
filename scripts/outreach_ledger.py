#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Record and inspect external-comment activity with hard rule enforcement."""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from _common import (
    SkillError,
    contains_question,
    in_windows,
    load_yaml,
    normalize_comment,
    opening_key,
    parse_windows,
    question_is_last_sentence,
    question_sentence_count,
    scheduling_windows,
    today_in_timezone,
    validate_config,
)


def ledger_path(config: dict, config_path: str, override: str | None) -> pathlib.Path:
    if override:
        return pathlib.Path(override).expanduser()
    value = (config.get("workspace") or {}).get("outreach_ledger", "data/outreach.jsonl")
    p = pathlib.Path(value).expanduser()
    return p if p.is_absolute() else pathlib.Path(config_path).expanduser().resolve().parent / p


def read_rows(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise SkillError(f"invalid JSONL at {path}:{line_no}: {exc}") from exc
    return rows


def parse_local_at(value: str | None, tz: str) -> datetime:
    zone = ZoneInfo(tz)
    if not value:
        return datetime.now(zone)
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=zone)
    return dt.astimezone(zone)


def row_at(row: dict, tz: str) -> datetime | None:
    value = row.get("at")
    if not value:
        return None
    try:
        return parse_local_at(str(value), tz)
    except (ValueError, SkillError):
        return None


def success_rows(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r.get("result") == "success"]


def has_question(row: dict) -> bool:
    if "has_question" in row:
        return bool(row.get("has_question"))
    return contains_question(str(row.get("text") or ""))


def question_limits(config: dict) -> dict[str, int]:
    tone = config["engagement"]["outreach"].get("tone", {}) or {}
    caps = tone.get("question_max", {}) or {}
    return {
        "total": int(caps.get("total", 5)),
        "same_topic": int(caps.get("same_topic", 3)),
        "viral": int(caps.get("viral", 2)),
    }


def window_segments(schedule: dict) -> list[tuple[int, int]]:
    """同日多段活跃窗口；兼容旧配置的单段 active_window。"""
    return parse_windows(scheduling_windows(schedule))


def in_active_window(at: datetime, schedule: dict) -> bool:
    """落在任一同日活跃窗口内即可（晚间高峰 + 深夜尾峰）。"""
    return in_windows(at.hour * 60 + at.minute, window_segments(schedule))


def window_tag(at: datetime, schedule: dict) -> str:
    """给成功记录打时段标签，用于复盘「晚间高峰」是否真的更有效。"""
    minute = at.hour * 60 + at.minute
    if in_windows(minute, parse_windows(schedule.get("peak_windows") or [])):
        return "peak"
    if in_active_window(at, schedule):
        return "regular"
    return "off_window"


def status_for(config: dict, rows: list[dict], date_str: str) -> dict:
    outreach = config["engagement"]["outreach"]
    by_kind = Counter()
    by_style = Counter()
    by_round = Counter()
    question_by_kind = Counter()
    question_count = 0
    day_successes = []
    tz = config["account"]["timezone"]
    for row in rows:
        if row.get("date") != date_str or row.get("result") != "success":
            continue
        day_successes.append(row)
        kind = row.get("kind", "unknown")
        by_kind[kind] += 1
        by_style[row.get("style", "unknown")] += 1
        if row.get("round_id"):
            by_round[str(row["round_id"])] += 1
        if has_question(row):
            question_count += 1
            question_by_kind[kind] += 1

    pools = outreach["pools"]
    qcaps = question_limits(config)
    total = sum(by_kind.values())
    schedule = outreach.get("scheduling") or {}
    active_windows = scheduling_windows(schedule)
    window_violations = []
    peak_hits = 0
    for row in day_successes:
        at = row_at(row, tz)
        if at is None:
            continue
        if not in_active_window(at, schedule):
            window_violations.append({
                "at": at.isoformat(),
                "kind": row.get("kind"),
                "author_name": row.get("author_name", ""),
            })
        elif (row.get("window_tag") or window_tag(at, schedule)) == "peak":
            peak_hits += 1

    points = sorted(
        at for at in (row_at(row, tz) for row in day_successes) if at is not None
    )
    rolling_60m_max = 0
    for point in points:
        rolling_60m_max = max(
            rolling_60m_max,
            sum(1 for other in points if point <= other < point + timedelta(hours=1)),
        )

    return {
        "date": date_str,
        "total": total,
        "daily_ceiling": outreach["daily_total"],
        "daily_ceiling_basis": outreach.get("daily_total_basis", "planned_ceiling"),
        "remaining_capacity": max(0, outreach["daily_total"] - total),
        "over_by": max(0, total - outreach["daily_total"]),
        "by_kind": {
            "same_topic": by_kind["same_topic"],
            "viral": by_kind["viral"],
        },
        "remaining_capacity_by_kind": {
            "same_topic": max(0, pools["same_topic"] - by_kind["same_topic"]),
            "viral": max(0, pools["viral"] - by_kind["viral"]),
        },
        "question_count": question_count,
        "question_ratio": question_count / total if total else 0,
        "question_by_kind": {
            "same_topic": question_by_kind["same_topic"],
            "viral": question_by_kind["viral"],
        },
        "remaining_questions": {
            "total": max(0, qcaps["total"] - question_count),
            "same_topic": max(0, qcaps["same_topic"] - question_by_kind["same_topic"]),
            "viral": max(0, qcaps["viral"] - question_by_kind["viral"]),
        },
        "by_style": dict(by_style),
        "by_round": dict(by_round),
        # active_window 保留为兼容字段；新执行者应读 active_windows 列表
        "active_window": ", ".join(active_windows),
        "active_windows": active_windows,
        "peak_windows": list(schedule.get("peak_windows") or []),
        "peak_share": schedule.get("peak_share"),
        "peak_hits": peak_hits,
        "quiet_window": schedule.get("quiet_window"),
        "window_violations": window_violations,
        "observed_max_per_rolling_60m": rolling_60m_max,
    }


def author_last_date(rows: list[dict], author_id: str) -> str | None:
    dates = [r.get("date") for r in rows if r.get("author_id") == author_id and r.get("result") == "success"]
    return max((d for d in dates if d), default=None)


def validate_success_record(
    config: dict,
    rows: list[dict],
    at: datetime,
    kind: str,
    text: str,
    round_id: str | None,
    author_id: str,
    note_url: str,
) -> list[str]:
    """Return all rule violations that must block a success record."""
    outreach = config["engagement"]["outreach"]
    schedule = outreach.get("scheduling", {}) or {}
    cooldown = outreach.get("cooldown", {}) or {}
    pools = outreach.get("pools", {}) or {}
    tz = config["account"]["timezone"]
    errors: list[str] = []

    def err(message: str) -> None:
        errors.append(message)

    if not str(round_id or "").strip():
        err("--round-id is required for a successful action")
    if not str(author_id or "").strip():
        err("--author-id is required for a successful action")
    if not str(note_url or "").strip():
        err("--note-url is required for a successful action")

    if not in_active_window(at, schedule):
        err(f"success time {at.isoformat()} is outside active windows {', '.join(scheduling_windows(schedule))}")

    date_str = at.date().isoformat()
    today = [
        r for r in success_rows(rows)
        if r.get("date") == date_str
    ]
    if len(today) >= int(outreach.get("daily_total", 30)):
        err(f"daily ceiling {outreach.get('daily_total', 30)} already reached")
    kind_today = sum(1 for r in today if r.get("kind") == kind)
    if kind_today >= int(pools.get(kind, 0)):
        err(f"{kind} pool ceiling {pools.get(kind, 0)} already reached")

    old_times = sorted(
        t for t in (row_at(r, tz) for r in success_rows(rows)) if t is not None and t <= at
    )
    if old_times:
        if (at - old_times[-1]).total_seconds() < int(schedule.get("min_gap_minutes", 10)) * 60:
            err(f"minimum gap is {schedule.get('min_gap_minutes', 10)} minutes from the previous successful comment")
        rolling_hour = sum(1 for t in old_times if at - timedelta(hours=1) < t <= at)
        cap = int(schedule.get("max_per_rolling_60m", 3))
        if rolling_hour >= cap:
            err(f"max {cap} successful comments per rolling 60-minute window would be exceeded")

    round_key = str(round_id or "").strip()
    if round_key:
        round_successes = sum(
            1 for r in success_rows(rows)
            if str(r.get("round_id") or "") == round_key
        )
        if round_successes >= int(schedule.get("max_success_per_round", 2)):
            err(f"round {round_key!r} already has {round_successes} successes; max is {schedule.get('max_success_per_round', 2)}")

    same_author_days = int(cooldown.get("same_author_days", 7))
    for old in success_rows(rows):
        if old.get("author_id") != author_id:
            continue
        old_at = row_at(old, tz)
        if old_at is not None and timedelta(0) <= at - old_at < timedelta(days=same_author_days):
            err(f"author {author_id} was contacted at {old_at.isoformat()}; cooldown is {same_author_days} days")
            break

    if note_url:
        for old in success_rows(rows):
            if old.get("note_url") == note_url:
                err(f"note {note_url} already recorded as successfully contacted")
                break

    new_text = normalize_comment(text)
    new_open = opening_key(text)
    phrase_days = int(cooldown.get("same_phrase_days", 7))
    opening_days = int(cooldown.get("same_opening_days", 7))
    for old in success_rows(rows):
        old_at = row_at(old, tz)
        if old_at is None or not (timedelta(0) <= at - old_at < timedelta(days=max(phrase_days, opening_days))):
            continue
        if phrase_days and new_text and normalize_comment(str(old.get("text") or "")) == new_text and at - old_at < timedelta(days=phrase_days):
            err(f"identical comment text used at {old_at.isoformat()}; cooldown is {phrase_days} days")
        old_open = opening_key(str(old.get("text") or ""))
        if opening_days and new_open and old_open == new_open and at - old_at < timedelta(days=opening_days):
            err(f"same comment opening {new_open!r} used at {old_at.isoformat()}; cooldown is {opening_days} days")

    has_q = contains_question(text)
    q_sentences = question_sentence_count(text)
    if q_sentences > 1:
        err(f"comment contains {q_sentences} question sentences; at most one is allowed")
    if has_q and not question_is_last_sentence(text):
        err("the question must be the final sentence")

    qcaps = question_limits(config)
    if has_q:
        q_total = sum(1 for r in today if has_question(r))
        q_kind = sum(1 for r in today if r.get("kind") == kind and has_question(r))
        if q_total >= qcaps["total"]:
            err(f"daily question ceiling {qcaps['total']} already reached")
        if q_kind >= qcaps.get(kind, 0):
            err(f"{kind} question ceiling {qcaps.get(kind, 0)} already reached")
        prior = [r for r in success_rows(rows) if (row_at(r, tz) or at) <= at]
        prior.sort(key=lambda r: row_at(r, tz) or at)
        if prior and has_question(prior[-1]):
            err("two question comments cannot be consecutive in the successful comment sequence")

    return errors


def initial_follow_state(config: dict, args, result: str) -> str:
    """留言成功后给这条记录打上关注回扫状态。

    pending = 已排进队列，下一轮开头去关注；
    self     = 评论发在自己的笔记上，不关注自己；
    n/a      = 非成功记录（跳过/失败/中止），没有可关注的对象。
    """
    if result != "success":
        return "n/a"
    if (config["engagement"]["outreach"].get("follow_back") or {}).get("skip_self", True):
        if str(args.author_id or "").strip() == str(
                (config.get("account") or {}).get("user_id") or "").strip():
            return "self"
    return "pending"


def cmd_record(args, config: dict, path: pathlib.Path) -> int:
    tz = config["account"]["timezone"]
    at = parse_local_at(args.at, tz)
    rows = read_rows(path)
    if args.result == "success":
        errors = validate_success_record(
            config,
            rows,
            at,
            args.kind,
            args.text,
            args.round_id,
            args.author_id,
            args.note_url,
        )
        if errors:
            for error in errors:
                print(f"ERROR {error}", file=sys.stderr)
            return 2

    row = {
        "at": at.isoformat(),
        "date": at.date().isoformat(),
        "round_id": args.round_id or "",
        "kind": args.kind,
        "style": args.style,
        "author_id": args.author_id,
        "author_name": args.author_name,
        "note_url": args.note_url,
        "text": args.text,
        "has_question": contains_question(args.text),
        "result": args.result,
        "note": args.note,
        # 标记 peak / regular / off_window，供高峰时段效果复盘
        "window_tag": window_tag(at, (config["engagement"]["outreach"].get("scheduling") or {})),
        # 关注回扫：留言成功即排进「下一轮开头去关注」的队列
        "follow": initial_follow_state(config, args, args.result),
        "follow_at": "",
        "follow_note": "",
    }
    report = status_for(config, rows + [row], at.date().isoformat())
    if args.dry_run:
        print(json.dumps({"validated": args.result != "success" or report["total"] >= 0, "record": row, "today": report}, ensure_ascii=False, indent=2))
        return 0

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps({"recorded": row, "today": report}, ensure_ascii=False, indent=2))
    return 0


def cmd_status(args, config: dict, path: pathlib.Path) -> int:
    rows = read_rows(path)
    date_str = args.date or today_in_timezone(config["account"]["timezone"]).isoformat()
    report = status_for(config, rows, date_str)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_check_author(args, config: dict, path: pathlib.Path) -> int:
    rows = read_rows(path)
    cooldown = int((config["engagement"]["outreach"].get("cooldown") or {}).get("same_author_days", 7))
    last = author_last_date(rows, args.author_id)
    today = today_in_timezone(config["account"]["timezone"])
    allowed = True
    days_since = None
    if last:
        days_since = (today - datetime.fromisoformat(last).date()).days
        allowed = days_since >= cooldown
    print(json.dumps({
        "author_id": args.author_id,
        "last_success_date": last,
        "days_since": days_since,
        "cooldown_days": cooldown,
        "allowed": allowed,
    }, ensure_ascii=False, indent=2))
    return 0 if allowed else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Record and inspect outreach comments.")
    parser.add_argument("--config", default="xiaohongshu-ops.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    rec = sub.add_parser("record")
    rec.add_argument("--config", default=argparse.SUPPRESS)
    rec.add_argument("--file")
    rec.add_argument("--round-id", required=False, help="Execution round ID; required for success and max 2 successes per round")
    rec.add_argument("--kind", required=True, choices=("same_topic", "viral", "dm_thanks"))
    rec.add_argument("--style", default="")
    rec.add_argument("--author-id", required=True)
    rec.add_argument("--author-name", default="")
    rec.add_argument("--note-url", default="")
    rec.add_argument("--text", required=True)
    rec.add_argument("--result", default="success", choices=("success", "failed", "skipped"))
    rec.add_argument("--at", help="ISO datetime; default now in account timezone")
    rec.add_argument("--note", default="")
    rec.add_argument("--dry-run", action="store_true")

    st = sub.add_parser("status")
    st.add_argument("--config", default=argparse.SUPPRESS)
    st.add_argument("--file")
    st.add_argument("--date", help="YYYY-MM-DD; default today")

    ca = sub.add_parser("check-author")
    ca.add_argument("--config", default=argparse.SUPPRESS)
    ca.add_argument("--file")
    ca.add_argument("--author-id", required=True)

    args = parser.parse_args()
    config = load_yaml(args.config)
    errors = [x for x in validate_config(config) if x["level"] == "error"]
    if errors:
        for err_item in errors:
            print(f"ERROR [{err_item['code']}] {err_item['message']}", file=sys.stderr)
        return 2
    path = ledger_path(config, args.config, getattr(args, "file", None))
    try:
        if args.command == "record":
            return cmd_record(args, config, path)
        if args.command == "status":
            return cmd_status(args, config, path)
        return cmd_check_author(args, config, path)
    except SkillError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
