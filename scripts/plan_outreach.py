#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate a randomized two-pool outreach schedule from account config."""
from __future__ import annotations

import argparse
import json
import pathlib
import random
import secrets
import sys
from collections import Counter
from datetime import date

from _common import (
    SkillError,
    in_windows,
    iso_at,
    load_yaml,
    minutes_to_hhmm,
    parse_windows,
    scheduling_windows,
    today_in_timezone,
    validate_config,
    write_text,
)

NON_QUESTION_STYLES = [
    "specific_praise",
    "shared_resonance",
    "shared_identity",
    "detail_appreciation",
    "light_observation",
]
QUESTION_STYLE = "gentle_question"


def rolling_window_max(times: list[int], window_minutes: int = 60) -> int:
    """Return the maximum number of events in any rolling time window."""
    ordered = sorted(times)
    return max(
        (sum(1 for other in ordered if point <= other < point + window_minutes) for point in ordered),
        default=0,
    )


def interleave_pools(same: int, viral: int, rng: random.Random) -> list[str]:
    counts = {"same_topic": same, "viral": viral}
    out: list[str] = []
    last = None
    while counts["same_topic"] or counts["viral"]:
        available = [k for k, v in counts.items() if v > 0 and k != last]
        if not available:
            available = [k for k, v in counts.items() if v > 0]
        if len(available) > 1 and last is None and same == viral:
            rng.shuffle(available)
        # When one pool remains, continue with it.
        choice = available[0] if len(available) == 1 else rng.choice(available)
        out.append(choice)
        counts[choice] -= 1
        last = choice
    return out


def pick_times(start: int, end: int, total: int, min_gap: int, max_per_rolling_60m: int, rng: random.Random) -> list[int]:
    """Stratified random sample; retry until spacing and hourly caps hold."""
    if total <= 0:
        return []
    window = end - start
    if total > 1 and window < min_gap * (total - 1) + 1:
        raise SkillError("active window is too short for this target and minimum gap")

    slot = window / total
    for _ in range(10000):
        times: list[int] = []
        for i in range(total):
            lo = start + i * slot + slot * 0.18
            hi = start + (i + 1) * slot - slot * 0.18
            if hi < lo:
                lo = hi = start + (i + 0.5) * slot
            minute = int(round(rng.uniform(lo, hi)))
            minute = max(start, min(end - 1, minute))
            times.append(minute)
        if any(b - a < min_gap for a, b in zip(times, times[1:])):
            continue
        if rolling_window_max(times) > max_per_rolling_60m:
            continue
        return times

    # Fallback: minimum gaps plus a random extra anywhere, still validated.
    extra = max(0, window - min_gap * (total - 1) - 1)
    for _ in range(10000):
        weights = [rng.random() + 0.05 for _ in range(total + 1)]
        scale = extra / sum(weights)
        offsets = [int(w * scale) for w in weights]
        times = []
        cur = start + offsets[0]
        for i in range(total):
            times.append(cur)
            if i < total - 1:
                cur += min_gap + offsets[i + 1]
        if times[-1] >= end:
            continue
        if rolling_window_max(times) > max_per_rolling_60m:
            continue
        return times
    raise SkillError("could not generate a schedule satisfying min gap and hourly cap; relax the limits")


def subtract_segments(segments: list[tuple[int, int]],
                      cuts: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """从活跃窗口里扣掉高峰段，得到常规段；均为同日半开区间。"""
    out: list[tuple[int, int]] = []
    for a, b in sorted(segments):
        pieces = [(a, b)]
        for c, d in sorted(cuts):
            nxt = []
            for x, y in pieces:
                if d <= x or c >= y:
                    nxt.append((x, y))
                    continue
                if c > x:
                    nxt.append((x, min(c, y)))
                if d < y:
                    nxt.append((max(d, x), y))
            pieces = nxt
        out.extend(p for p in pieces if p[1] > p[0])
    return sorted(out)


def _sample_in_segments(segments: list[tuple[int, int]], count: int,
                        rng: random.Random) -> list[int]:
    """把多段窗口首尾相接成虚拟轴，分层随机取 count 点，再映射回真实分钟。"""
    if count <= 0 or not segments:
        return []
    bounds = []
    acc = 0
    for a, b in sorted(segments):
        length = b - a
        bounds.append((acc, acc + length, a, length))
        acc += length
    slot = acc / count
    times = []
    for i in range(count):
        lo = i * slot + slot * 0.18
        hi = (i + 1) * slot - slot * 0.18
        v = rng.uniform(lo, hi) if hi > lo else (i + 0.5) * slot
        v = max(0.0, min(float(acc - 1), v))
        real = bounds[-1][2] + bounds[-1][3] - 1
        for va, vb, start, length in bounds:
            if va <= v < vb:
                # 夹在段内，不能四舍五入到段尾（段尾属于静默窗口）
                offset = max(0, min(length - 1, int(round(v - va))))
                real = start + offset
                break
        times.append(real)
    return times


def _valid_times(times: list[int], min_gap: int, cap: int) -> bool:
    if any(b - a < min_gap for a, b in zip(times, times[1:])):
        return False
    return rolling_window_max(times) <= cap


def pick_times_multi(segments: list[tuple[int, int]],
                     peak_segments: list[tuple[int, int]],
                     total: int, min_gap: int, cap: int,
                     peak_share: float, rng: random.Random) -> list[int]:
    """多段窗口出计划：高峰段优先占 peak_share，其余落常规段。

    高峰占比拿不到时逐步降档，再兜底到全窗口均匀铺；
    任何情况下都不破坏最小间隔和滚动 60 分钟限流。
    """
    if total <= 0:
        return []
    regular = subtract_segments(segments, peak_segments)
    share = max(0.0, min(1.0, float(peak_share)))
    target = int(round(total * share))
    attempts = list(range(target, -1, -2))
    for peak_n in attempts:
        regular_n = total - peak_n
        if (peak_n and not peak_segments) or (regular_n and not regular):
            continue
        for _ in range(1200):
            times = sorted(
                _sample_in_segments(peak_segments, peak_n, rng)
                + _sample_in_segments(regular, regular_n, rng)
            )
            if _valid_times(times, min_gap, cap):
                return times
    # 兜底：不区分高峰，全活跃段均匀铺
    for _ in range(3000):
        times = sorted(_sample_in_segments(segments, total, rng))
        if _valid_times(times, min_gap, cap):
            return times
    raise SkillError("could not generate a schedule satisfying multi-window spacing and hourly cap")


def question_targets(outreach: dict, same: int, viral: int) -> dict[str, int]:
    """Return the planned question count by pool, bounded by explicit daily caps."""
    tone = outreach.get("tone", {}) or {}
    ratios = tone.get("question_ratio_target", {}) or {}
    caps = tone.get("question_max", {}) or {}
    counts = {
        "same_topic": min(same, max(0, int(round(same * float(ratios.get("same_topic", 0)))))),
        "viral": min(viral, max(0, int(round(viral * float(ratios.get("viral", 0)))))),
    }
    if caps:
        counts["same_topic"] = min(counts["same_topic"], int(caps.get("same_topic", counts["same_topic"])))
        counts["viral"] = min(counts["viral"], int(caps.get("viral", counts["viral"])))
        total_cap = int(caps.get("total", counts["same_topic"] + counts["viral"]))
        while counts["same_topic"] + counts["viral"] > total_cap:
            if counts["viral"] >= counts["same_topic"] and counts["viral"] > 0:
                counts["viral"] -= 1
            elif counts["same_topic"] > 0:
                counts["same_topic"] -= 1
            else:
                break
    return counts


def assign_styles(kinds: list[str], targets: dict[str, int], rng: random.Random) -> list[str]:
    styles = [""] * len(kinds)
    pool_indices = {
        kind: [i for i, k in enumerate(kinds) if k == kind]
        for kind in ("same_topic", "viral")
    }

    # Spread each pool's questions across its own share of the day. Retry only
    # the placement (not the counts) until no two global comments are questions.
    for _ in range(10000):
        question_indices: set[int] = set()
        for kind in ("same_topic", "viral"):
            indices = pool_indices[kind]
            q_count = min(len(indices), max(0, int(targets.get(kind, 0))))
            if not q_count:
                continue
            step = len(indices) / q_count
            for q in range(q_count):
                lo = int(q * step)
                hi = max(lo + 1, int((q + 1) * step))
                candidate_pool = [x for x in indices[lo:hi] if x not in question_indices]
                if not candidate_pool:
                    candidate_pool = [x for x in indices if x not in question_indices]
                if candidate_pool:
                    question_indices.add(rng.choice(candidate_pool))
        if len(question_indices) == sum(targets.values()) and all(
            not (i in question_indices and i + 1 in question_indices)
            for i in range(len(kinds) - 1)
        ):
            break
    else:
        raise SkillError("could not spread question-style comments without placing two back-to-back")

    for kind in ("same_topic", "viral"):
        indices = pool_indices[kind]
        non_question_indices = [i for i in indices if i not in question_indices]
        cycle = NON_QUESTION_STYLES[:]
        rng.shuffle(cycle)
        pool_styles = []
        while len(pool_styles) < len(non_question_indices):
            pool_styles.extend(cycle)
            rng.shuffle(cycle)
        for idx, style in zip(non_question_indices, pool_styles):
            styles[idx] = style
    for idx in question_indices:
        styles[idx] = QUESTION_STYLE
    return styles


def make_plan(config: dict, date_str: str, seed: int) -> dict:
    rng = random.Random(seed)
    outreach = config["engagement"]["outreach"]
    pools = outreach["pools"]
    same = int(pools["same_topic"])
    viral = int(pools["viral"])
    total = int(outreach["daily_total"])
    schedule = outreach["scheduling"]
    segments = parse_windows(scheduling_windows(schedule))
    peak_segments = parse_windows(schedule.get("peak_windows") or [])
    times = pick_times_multi(
        segments,
        peak_segments,
        total,
        int(schedule.get("min_gap_minutes", 10)),
        int(schedule.get("max_per_rolling_60m", 3)),
        float(schedule.get("peak_share", 0) or 0),
        rng,
    )
    kinds = interleave_pools(same, viral, rng) if schedule.get("alternate_pools", True) else (
        ["same_topic"] * same + ["viral"] * viral
    )
    times.sort()

    q_targets = question_targets(outreach, same, viral)
    styles = assign_styles(kinds, q_targets, rng)

    search_queries = list((outreach.get("same_topic") or {}).get("search_queries", []) or [])
    rng.shuffle(search_queries)
    tz = config["account"]["timezone"]

    items = []
    for i, (kind, minute, style) in enumerate(zip(kinds, times, styles), 1):
        hhmm = minutes_to_hhmm(minute)
        item = {
            "index": i,
            "kind": kind,
            "style": style,
            "needs_question": style == QUESTION_STYLE,
            "time": hhmm,
            "scheduled_at": iso_at(date_str, hhmm, tz),
        }
        if kind == "same_topic":
            item["search_query"] = search_queries[(i - 1) % len(search_queries)] if search_queries else ""
            item["selection_brief"] = (outreach.get("same_topic") or {}).get("selection_note", "")
        else:
            item["selection_brief"] = (outreach.get("viral") or {}).get("selection_note", "")
        item["comment_rule"] = (
            "必须写出原帖具体细节、具体赞赏/欣赏和自然同频感；问句只能放在最后，且最多一个。"
            if style == QUESTION_STYLE
            else "必须写出原帖具体细节、具体赞赏/欣赏和自然同频感；本条不问问题，不导流。"
        )
        items.append(item)

    by_style = Counter(x["style"] for x in items)
    q_count = sum(1 for x in items if x["needs_question"])
    observed_rolling_max = rolling_window_max(times)
    return {
        "date": date_str,
        "timezone": tz,
        "seed": seed,
        "total": total,
        "total_basis": "planned_ceiling",
        "pools": {"same_topic": same, "viral": viral},
        "question_limits": {
            "total": int(((outreach.get("tone") or {}).get("question_max") or {}).get("total", q_count)),
            "same_topic": int(((outreach.get("tone") or {}).get("question_max") or {}).get("same_topic", q_targets["same_topic"])),
            "viral": int(((outreach.get("tone") or {}).get("question_max") or {}).get("viral", q_targets["viral"])),
        },
        "spacing": {
            # active_window 保留兼容字段，值为多段窗口的逗号串
            "active_window": ", ".join(scheduling_windows(schedule)),
            "active_windows": scheduling_windows(schedule),
            "peak_windows": list(schedule.get("peak_windows") or []),
            "peak_share": float(schedule.get("peak_share", 0) or 0),
            "quiet_window": schedule.get("quiet_window"),
            "peak_hits": sum(1 for m in times if peak_segments and in_windows(m, peak_segments)),
            "min_gap_minutes": int(schedule.get("min_gap_minutes", 10)),
            "max_per_rolling_60m": int(schedule.get("max_per_rolling_60m", 3)),
            "max_success_per_round": int(schedule.get("max_success_per_round", 2)),
            "batch_open_candidates": schedule.get("batch_open_candidates", [3, 5]),
            "observed_max_per_rolling_60m": observed_rolling_max,
        },
        "question_ratio": (q_count / total) if total else 0,
        "question_count": q_count,
        "styles": dict(by_style),
        "items": items,
    }


def to_markdown(plan: dict) -> str:
    lines = [
        f"# 外联计划 · {plan['date']}",
        "",
        f"- 时区：{plan['timezone']}",
        f"- 计划上限：{plan['total']} 条（同主题最多 {plan['pools']['same_topic']} + 爆款最多 {plan['pools']['viral']}）",
        f"- 活跃窗口：{plan['spacing']['active_window']}",
        f"- 晚间高峰：{', '.join(plan['spacing']['peak_windows']) or '未设置'}"
        f"（计划位 {plan['spacing']['peak_hits']}/{plan['total']}，目标占比 {plan['spacing']['peak_share']:.0%}）",
        f"- 静默窗口：{plan['spacing']['quiet_window'] or '未设置'}（不发起任何外联动作）",
        f"- 最小间隔：{plan['spacing']['min_gap_minutes']} 分钟",
        f"- 滚动 60 分钟峰值：{plan['spacing']['observed_max_per_rolling_60m']} / {plan['spacing']['max_per_rolling_60m']}",
        f"- 单轮最多成功：{plan['spacing']['max_success_per_round']} 条",
        f"- 问句：计划 {plan['question_count']} 条（{plan['question_ratio']:.1%}）；上限 总 {plan['question_limits']['total']} / 同主题 {plan['question_limits']['same_topic']} / 爆款 {plan['question_limits']['viral']}",
        f"- 随机种子：`{plan['seed']}`",
        "",
        "| # | 时间 | 类型 | 风格 | 是否问句 | 选题/选帖提示 |",
        "|---:|---|---|---|---|---|",
    ]
    for item in plan["items"]:
        hint = item.get("search_query") or "高流量、近期、评论区仍活跃"
        kind = "同主题" if item["kind"] == "same_topic" else "爆款"
        q = "是" if item["needs_question"] else "否"
        lines.append(f"| {item['index']} | {item['time']} | {kind} | {item['style']} | {q} | {hint} |")
    lines.extend([
        "",
        "## 执行纪律",
        "",
        f"- {plan['total']} 条是计划上限，不是必须完成的配额；没有合格候选、时间不够或遇风控时宁可少做。",
        f"- 高峰段（{', '.join(plan['spacing']['peak_windows']) or '未设置'}）优先执行；静默窗口（{plan['spacing']['quiet_window'] or '未设置'}）不操作。",
        "- 运营日按自然日归属；晚间场允许跨零点连续执行，深夜尾峰计入新一天的配额，不算窗口外补量。",
        "- 静默窗口内只允许记 skipped；任何时段都不得为凑计划上限补量。",
        f"- 单轮最多成功 {plan['spacing']['max_success_per_round']} 条，建议同主题 1 条 + 爆款 1 条，逐条发送并回读。",
        "- 先选帖，再写评论；评论必须提到原帖里的具体内容。",
        f"- 每条都要有具体赞赏/欣赏和同频/被吸引感；问句上限 总 {plan['question_limits']['total']} / 同主题 {plan['question_limits']['same_topic']} / 爆款 {plan['question_limits']['viral']}，不连续提问。",
        "- 同主题帖至少有 1 条真实评论；0 评论“荒地帖”不用于外联。",
        "- 发出后回读成功再记账；失败不重复硬发。",
        "- 遇验证码、操作频繁、登录异常立即停止。",
    ])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan a randomized 15+15 outreach day.")
    parser.add_argument("--config", default="xiaohongshu-ops.yaml")
    parser.add_argument("--date", help="YYYY-MM-DD; default is today in account timezone")
    parser.add_argument("--seed", type=int, help="random seed for reproducible plans")
    parser.add_argument("--output", help="write plan to this path")
    parser.add_argument("--format", choices=("json", "md"), default="json")
    args = parser.parse_args()

    config = load_yaml(args.config)
    issues = validate_config(config)
    errors = [x for x in issues if x["level"] == "error"]
    if errors:
        for err in errors:
            print(f"ERROR [{err['code']}] {err['message']}", file=sys.stderr)
        return 2

    tz = config["account"]["timezone"]
    date_str = args.date or today_in_timezone(tz).isoformat()
    try:
        date.fromisoformat(date_str)
    except ValueError:
        print(f"invalid --date: {date_str}", file=sys.stderr)
        return 2
    seed = args.seed if args.seed is not None else secrets.randbits(32)
    try:
        plan = make_plan(config, date_str, seed)
    except SkillError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2

    rendered = to_markdown(plan) if args.format == "md" else json.dumps(plan, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        write_text(args.output, rendered)
        print(args.output)
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
