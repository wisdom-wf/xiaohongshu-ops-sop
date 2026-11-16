#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Turn a small metrics JSON file into a 14-dimension account review.

口径约定：

* 计数型字段（观看、赞、藏、评、分享、涨粉）只对**非空**求和，同时输出有效样本数，
  缺失一律保持 None，不再按 0 计入。
* 比率一律在「分子分母都有效」的完整子集上计算，并给出 valid_n / total_n；
  覆盖率 < 90% 视为「部分样本」，不参与红黄绿判定。
* 每个维度同时输出 `status`（green/amber/red/none，仅表示有目标时的达成状态）与
  `availability`（available/partial/descriptive/unavailable）。有数据但没有目标的维度
  是 `descriptive`，不是「未取到」。
* 账号级区间数据（近 7 日）与笔记样本累计数据口径不同，只在同周期内相除，
  绝不相加或跨周期算达成率。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import pathlib
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime

from _common import (
    SkillError,
    fmt_num,
    load_yaml,
    median,
    pct,
    read_json,
    safe_div,
    validate_config,
    weighted_average,
    write_json,
    write_text,
)

# 覆盖率低于该阈值即视为「部分样本」，不判红黄绿
COVERAGE_OK = 0.90
# 分组比较的最小样本量，低于此值只写「样本不足」
MIN_GROUP_N = 3

COUNT_FIELDS = ("views", "impressions", "likes", "comments", "collects", "shares", "follows")


def num(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def as_number(value):
    """严格取值：空值返回 None，不把缺失变成 0。"""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_cn_number(text) -> tuple[float | None, bool]:
    """把「3.4万」「1,234」「56%」这类展示文本解析成数值。

    返回 (value, estimated)。带「万」「w」的原文是四舍五入到一位小数的展示值，
    解析结果只能当约数用，所以 estimated=True。
    """
    if text is None:
        return None, False
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return float(text), False
    raw = str(text).strip().replace(",", "")
    if not raw:
        return None, False
    m = re.match(r"^(-?\d+(?:\.\d+)?)\s*(万|w|W|亿)?\s*%?$", raw)
    if not m:
        return None, False
    value = float(m.group(1))
    unit = m.group(2)
    if unit in ("万", "w", "W"):
        return value * 10000, True
    if unit == "亿":
        return value * 100000000, True
    return value, False


def _strict_values(notes: list[dict], key: str) -> list[float]:
    return [v for v in (as_number(n.get(key)) for n in notes) if v is not None]


def _text_values(notes: list[dict], key: str) -> list[str]:
    """文本 / 列表型字段（钩子、标签）的有效样本：非空才算有值。"""
    out: list[str] = []
    for n in notes:
        v = n.get(key)
        if isinstance(v, str) and v.strip() and v.strip() not in ("未标注", "无", "None"):
            out.append(v.strip())
        elif isinstance(v, (list, tuple)) and len(v):
            out.append(",".join(str(x) for x in v))
    return out


def rate_block(notes: list[dict], num_key: str, scale: float = 1.0,
               den_key: str = "views") -> dict:
    """在「分子分母都有效」的子集上算比率，缺失不补零。"""
    pairs: list[tuple[float, float]] = []
    for n in notes:
        a, b = as_number(n.get(num_key)), as_number(n.get(den_key))
        if a is None or b is None:
            continue
        pairs.append((a, b))
    total = len(notes)
    valid = len(pairs)
    den = sum(b for _, b in pairs)
    value = (sum(a for a, _ in pairs) * scale / den) if valid and den else None
    coverage = (valid / total) if total else None
    return {
        "value": value,
        "valid_n": valid,
        "total_n": total,
        "denominator": den if valid else None,
        "coverage": coverage,
        "partial": bool(total and valid / total < COVERAGE_OK),
    }


def weighted_rate(notes: list[dict], value_key: str, weight_key: str = "views") -> dict:
    pairs = [(as_number(n.get(value_key)), as_number(n.get(weight_key))) for n in notes]
    pairs = [(v, w) for v, w in pairs if v is not None and w is not None]
    total = len(notes)
    valid = len(pairs)
    value = weighted_average(pairs) if valid else None
    coverage = (valid / total) if total else None
    return {
        "value": value,
        "valid_n": valid,
        "total_n": total,
        "coverage": coverage,
        "partial": bool(total and valid / total < COVERAGE_OK),
    }


def summarize(notes: list[dict]) -> dict:
    total = len(notes)
    sums: dict[str, float | None] = {}
    valid_n: dict[str, int] = {}
    coverage: dict[str, float | None] = {}
    for key in COUNT_FIELDS:
        vals = _strict_values(notes, key)
        sums[key] = sum(vals) if vals else None
        valid_n[key] = len(vals)
        coverage[key] = (len(vals) / total) if total else None

    watch = weighted_rate(notes, "avg_watch_s")
    completion = weighted_rate(notes, "completion_rate")
    ctr = weighted_rate(notes, "cover_ctr", "impressions")

    rates = {
        "like_rate": rate_block(notes, "likes"),
        "comment_rate": rate_block(notes, "comments"),
        "collect_rate": rate_block(notes, "collects"),
        "share_rate": rate_block(notes, "shares"),
        "follows_per_100_views": rate_block(notes, "follows", scale=100.0),
    }
    views_vals = _strict_values(notes, "views")
    summary = {
        "notes": total,
        "impressions": sums["impressions"],
        "views": sums["views"],
        "views_median": median(views_vals),
        "cover_ctr": ctr["value"],
        "completion_rate": completion["value"],
        "avg_watch_s": watch["value"],
        "likes": sums["likes"],
        "comments": sums["comments"],
        "collects": sums["collects"],
        "shares": sums["shares"],
        "follows": sums["follows"],
        # 兼容旧键：值仍为比率，口径已改为「完整子集」
        "like_rate": rates["like_rate"]["value"],
        "comment_rate": rates["comment_rate"]["value"],
        "collect_rate": rates["collect_rate"]["value"],
        "share_rate": rates["share_rate"]["value"],
        "follows_per_100_views": rates["follows_per_100_views"]["value"],
        "comments_per_note": safe_div(sums["comments"], total),
        # 透明化：每个字段的有效样本数与覆盖率
        "valid_n": valid_n,
        "coverage": coverage,
        "rates": rates,
        "metric_meta": {
            "cover_ctr": ctr,
            "completion_rate": completion,
            "avg_watch_s": watch,
        },
    }
    return summary


def group_summaries(notes: list[dict], field: str) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for n in notes:
        value = n.get(field)
        if value in (None, ""):
            value = "未标注"
        groups[str(value)].append(n)
    rows = []
    for key, subset in groups.items():
        s = summarize(subset)
        s["group"] = key
        s["small_sample"] = len(subset) < MIN_GROUP_N
        rows.append(s)
    # 按中位数排序：总观看由样本量决定，容易把「发得多」说成「最好」
    return sorted(rows, key=lambda x: (x["views_median"] or 0, x["notes"]), reverse=True)


def status_from_target(value, target, higher_is_better=True, tolerance=0.7):
    if value is None or target is None:
        return "none"
    try:
        v, t = float(value), float(target)
    except (TypeError, ValueError):
        return "none"
    ratio = (v / t) if t else None
    if higher_is_better:
        if v >= t:
            return "green"
        if ratio is not None and ratio >= tolerance:
            return "amber"
        return "red"
    if v <= t:
        return "green"
    if ratio is not None and ratio <= 1.25:
        return "amber"
    return "red"


def best_hour(notes: list[dict]) -> tuple[str, float | None, int]:
    buckets: dict[str, list[dict]] = defaultdict(list)
    for n in notes:
        value = n.get("published_at")
        if not value:
            continue
        try:
            hour = datetime.fromisoformat(str(value)).hour
        except ValueError:
            continue
        buckets[f"{hour:02d}:00"].append(n)
    if not buckets:
        return "未取到", None, 0
    best = max(buckets.items(), key=lambda kv: median(_strict_values(kv[1], "views")) or 0)
    return best[0], median(_strict_values(best[1], "views")), len(best[1])


def _prov(path: str) -> str:
    """相对项目根的可读路径，写进报告便于溯源。"""
    try:
        return os.path.relpath(path, os.getcwd())
    except ValueError:
        return path


def load_account_overview(search_dirs: list[str], date: str | None = None) -> dict | None:
    """读最新的账号总览 JSON（近 7 日区间数据）。

    只读本地已采集文件，不访问任何平台接口。找不到就返回 None，
    调用方据此把账号级维度标成 unavailable，而不是填 0。
    """
    candidates: list[str] = []
    for d in search_dirs:
        pattern = os.path.join(d, f"account_overview_{date}.json") if date else os.path.join(d, "account_overview_*.json")
        candidates.extend(glob.glob(pattern))
    if not candidates:
        return None
    path = sorted(candidates)[-1]
    try:
        raw = read_json(path)
    except (SkillError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    views_value, estimated = parse_cn_number(raw.get("period_views"))
    meta = (raw.get("_meta") or {}) if isinstance(raw.get("_meta"), dict) else {}
    as_of = meta.get("fetch_time") or raw.get("fetch_time")
    if as_of:
        # 源文件里出现过「00:1x」这类半成品时间，按落盘时间校正
        if re.search(r"[xX]", str(as_of)) or not str(as_of).strip():
            try:
                as_of = datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M")
            except OSError:
                as_of = None
    period = raw.get("period")
    ends = re.findall(r"\d{4}-\d{2}-\d{2}", str(period or ""))
    follows, lost = as_number(raw.get("period_follows")), as_number(raw.get("period_follows_lost"))
    net = None
    if follows is not None or lost is not None:
        net = (follows or 0) - (lost or 0)
    return {
        "source_file": _prov(path),
        "as_of": as_of,
        "data_date": raw.get("date"),
        "period": period,
        "period_start": ends[0] if ends else None,
        "period_end": ends[-1] if len(ends) > 1 else (ends[0] if ends else None),
        "followers_total": as_number(raw.get("followers_total")),
        "period_views_raw": raw.get("period_views"),
        "period_views": views_value,
        "period_views_estimated": estimated,
        "period_likes": as_number(raw.get("period_likes")),
        "period_collects": as_number(raw.get("period_collects")),
        "period_comments": as_number(raw.get("period_comments")),
        "period_shares": as_number(raw.get("period_shares")),
        "period_follows": follows,
        "period_follows_lost": lost,
        "period_net_follows": net,
        "period_profile_visits": as_number(raw.get("period_profile_visits")),
        "fans_gender": raw.get("fans_gender") or {},
        "fans_source": raw.get("fans_source") or {},
        "period_completion_rate": as_number(raw.get("period_completion_rate")),
        "period_cover_ctr": as_number(raw.get("period_cover_ctr")),
    }


def _n_label(block: dict) -> str:
    """有效样本标注：67/69（97%）。"""
    total = block.get("total_n")
    valid = block.get("valid_n")
    if not total:
        return ""
    cov = block.get("coverage")
    suffix = f"，{cov * 100:.0f}%" if isinstance(cov, (int, float)) else ""
    return f"{valid}/{total} 条有效{suffix}"


def build_report(data: dict, config: dict, account_overview: dict | None = None) -> dict:
    notes = data.get("notes") or []
    if not isinstance(notes, list) or not all(isinstance(n, dict) for n in notes):
        raise SkillError("input.notes must be an array of objects")
    base_account = data.get("account") or {}
    targets = (config.get("analytics") or {}).get("targets", {}) or {}
    overall = summarize(notes)
    by_format = group_summaries(notes, "format")
    by_topic = group_summaries(notes, "topic")
    by_formula = group_summaries(notes, "title_formula")
    by_hook = group_summaries(notes, "hook_type")
    top_notes = sorted(notes, key=lambda n: as_number(n.get("views")) or 0, reverse=True)[:10]
    best_time, best_time_median, best_time_n = best_hour(notes)

    # 账号级（近 7 日区间）与笔记级（累计）分开保存，不合并成一个数
    acc = dict(account_overview or {})
    account = {
        "name": base_account.get("name") or acc.get("name"),
        "account_level": {k: v for k, v in acc.items() if v not in (None, {}, "")},
        "note_level": {
            "notes": overall["notes"],
            "views": overall["views"],
            "follows": overall["follows"],
            "follows_valid_n": overall["valid_n"]["follows"],
        },
    }
    if acc:
        account.update(acc)
    else:
        account.update({
            "followers_total": as_number(base_account.get("followers")),
            "period_follows": None,
            "period_profile_visits": as_number(base_account.get("profile_visits_7d")),
        })

    followers = as_number(base_account.get("followers")) or as_number(account.get("followers_total"))
    followers_prev = as_number(base_account.get("followers_previous"))
    follower_delta = None
    if followers is not None and followers_prev is not None:
        follower_delta = num(followers) - num(followers_prev)
    profile_visits = as_number(account.get("period_profile_visits")) or as_number(base_account.get("profile_visits_7d"))

    daily_views_target = targets.get("daily_views")
    daily_follows_target = targets.get("daily_follows")
    profile_target = targets.get("daily_profile_visits")
    completion_target = targets.get("video_completion_rate")
    collect_target = targets.get("collect_rate")
    share_target = targets.get("share_rate")
    comment_target = targets.get("comments_per_note")
    follow_per100_target = targets.get("follows_per_100_views")
    video_ctr_target = targets.get("video_cover_ctr")
    image_ctr_target = targets.get("image_cover_ctr")

    video_summary = next((x for x in by_format if x["group"] == "video"), None)
    image_summary = next((x for x in by_format if x["group"] == "image"), None)

    dims: list[dict] = []

    def add(name, text, availability, status="none", n=None, note=None):
        dims.append({
            "name": name,
            "status": status,            # green / amber / red / none（仅有目标时判色）
            "availability": availability,  # available / partial / descriptive / unavailable
            "text": text,
            "n": n or {},
            "note": note,
            "tag": status if status != "none" else availability,  # 兼容旧消费方
        })

    period_label = account.get("period") or (f"{account.get('period_start')} ~ {account.get('period_end')}"
                                             if account.get("period_start") else "区间未标注")
    period_views_raw = account.get("period_views_raw")
    period_views = as_number(account.get("period_views"))
    period_follows = as_number(account.get("period_follows"))
    period_lost = as_number(account.get("period_follows_lost"))
    period_net = as_number(account.get("period_net_follows"))
    account_views_text = f"{period_views_raw}（原文）" if period_views_raw else "未取到"

    # ---------- 1 账号增长 ----------
    note_follows_n = overall["valid_n"]["follows"]
    growth_text = f"账号级（{period_label}）：粉丝 {fmt_num(account.get('followers_total'), 0)}，净增 {('%+d' % period_net) if period_net is not None else '未取到'}（新增 {fmt_num(period_follows, 0)} / 掉粉 {fmt_num(period_lost, 0)}）；单篇归因：{note_follows_n}/{overall['notes']} 条笔记有涨粉字段，合计 {fmt_num(overall['follows'], 0)}"
    if profile_visits is not None and period_views:
        growth_text += f"；主页访客 {fmt_num(profile_visits, 0)}，访客/观看 {pct(profile_visits / period_views)}"
    elif profile_visits is not None:
        growth_text += f"；主页访客 {fmt_num(profile_visits, 0)}（观看未取到，不算访客率）"
    add(
        "1 账号增长",
        growth_text,
        "available" if account.get("followers_total") is not None else "unavailable",
        status="none",
        n={"note_follows": note_follows_n, "notes": overall["notes"]},
        note=("账号级为近 7 日区间，笔记级为样本累计，两者不相加；日涨粉目标为单日口径，不在此判色。"
              + ("访客率的分母取自后台原文展示值，属四舍五入约数。" if account.get("period_views_estimated") else "")),
    )

    # ---------- 2 流量规模 ----------
    top_views = as_number(top_notes[0].get("views")) if top_notes else None
    traffic_text = (f"账号级（{period_label}）观看 {account_views_text}；"
                    f"样本累计 {fmt_num(overall['views'], 0)}（{overall['notes']} 条逐篇累加）；"
                    f"单条最高 {fmt_num(top_views, 0)}；观看中位数 {fmt_num(overall['views_median'], 0)}")
    if overall["impressions"] is not None:
        traffic_text += f"；曝光合计 {fmt_num(overall['impressions'], 0)}（{_n_label(overall['metric_meta']['cover_ctr'])}）"
    add(
        "2 流量规模",
        traffic_text,
        "available" if overall["views"] is not None else "unavailable",
        status="none",
        n={"notes": overall["notes"]},
        note=f"日目标 {fmt_num(daily_views_target, 0)} 是单日口径，只与当日增量比；样本累计观看不与日目标算达成率。缺曝光的 {overall['notes'] - overall['valid_n']['impressions']} 条按缺失处理，不补零。",
    )

    # ---------- 3 平均观看时长（原「完播效率」） ----------
    # 平均观看时长只对视频笔记有意义：图文的 avg_watch_s 会把加权值拉偏
    watch_scope = video_summary or overall
    watch = watch_scope["metric_meta"]["avg_watch_s"]
    completion = watch_scope["metric_meta"]["completion_rate"]
    scope_label = "视频" if video_summary else "全部"
    watch_text = f"平均观看时长 {fmt_num(watch['value'], 1)} 秒（{scope_label} {_n_label(watch)}，按观看加权）"
    if completion["value"] is None:
        watch_text += f"；完播率 {completion['valid_n']}/{completion['total_n']} 未取得"
    else:
        watch_text += f"；完播率 {pct(completion['value'])}（{_n_label(completion)}）"
    duration_n = overall["valid_n"].get("duration_s", 0)
    watch_text += f"；视频时长 {duration_n}/{overall['notes']} 未取得，暂不能算「观看时长占片长比」"
    add(
        "3 平均观看时长",
        watch_text,
        "descriptive" if watch["value"] is not None else "unavailable",
        status="none",
        n={"avg_watch": watch["valid_n"], "notes": overall["notes"]},
        note="后台未提供逐篇完播字段，故不叫「完播效率」；平均观看时长不得除以素材时长后命名为完播率。",
    )

    # ---------- 4 封面效率 ----------
    def _pct_vs_target(value, target):
        """与目标的差在 0.2 个百分点内时给两位小数，避免四舍五入后「看着达标却判预警」。"""
        if value is None:
            return "未取到"
        if target:
            try:
                gap = abs(float(value) - float(target))
            except (TypeError, ValueError):
                gap = 1
            return pct(value, 2 if gap < 0.02 else 1)
        return pct(value)

    cover_parts = []
    cover_status = "none"
    cover_partial = False
    for label, summary, target in (("视频", video_summary, video_ctr_target),
                                   ("图文", image_summary, image_ctr_target)):
        if not summary:
            continue
        meta = summary["metric_meta"]["cover_ctr"]
        tgt_txt = f"，目标 {pct(target)}" if target else ""
        cover_parts.append(f"{label} {_pct_vs_target(meta['value'], target)}（{_n_label(meta)}{tgt_txt}）")
        if meta["partial"]:
            cover_partial = True
        if target is not None and meta["value"] is not None and not meta["partial"]:
            st = status_from_target(meta["value"], target)
            order = {"none": 0, "green": 1, "amber": 2, "red": 3}
            if order[st] > order[cover_status]:
                cover_status = st
    if not cover_parts:
        cover_parts.append("封面点击率未取到")
    add(
        "4 封面效率",
        "；".join(cover_parts),
        "partial" if cover_partial else ("available" if cover_parts else "unavailable"),
        status="none" if cover_partial else cover_status,
        n={"notes": overall["notes"], "ctr_valid": overall["valid_n"]["impressions"]},
        note="只在平台原始 CTR 或同笔记同一时点的观看/曝光上计算；缺曝光的笔记不按 CTR=0 处理；账号级 7 日 CTR 不与样本混用。",
    )

    # ---------- 5 互动质量 ----------
    r = overall["rates"]
    like_b, comment_b, collect_b, share_b = r["like_rate"], r["comment_rate"], r["collect_rate"], r["share_rate"]
    engagement_text = (f"点赞率 {pct(like_b['value'])}（{_n_label(like_b)}）；"
                       f"评论 {fmt_num(overall['comments_per_note'], 1)}/条；"
                       f"收藏率 {pct(collect_b['value'])}（{_n_label(collect_b)}）；"
                       f"分享率 {pct(share_b['value'])}（{_n_label(share_b)}）")
    engagement_partial = any(b["partial"] for b in (collect_b, share_b))
    engagement_status = "none"
    if not engagement_partial:
        cands = []
        if collect_target:
            cands.append(status_from_target(collect_b["value"], collect_target))
        if share_target:
            cands.append(status_from_target(share_b["value"], share_target))
        for s in cands:
            if {"none": 0, "green": 1, "amber": 2, "red": 3}[s] > {"none": 0, "green": 1, "amber": 2, "red": 3}[engagement_status]:
                engagement_status = s
    add(
        "5 互动质量",
        engagement_text,
        "partial" if engagement_partial else "available",
        status=engagement_status,
        n={"notes": overall["notes"], "collects": collect_b["valid_n"], "shares": share_b["valid_n"]},
        note="各率在「分子分母都有效」的子集上计算；分享覆盖不足 90%，标为部分样本，不判绿。",
    )

    # ---------- 6 关注转化 ----------
    acc_follow_per100 = None
    if period_net is not None and period_views:
        acc_follow_per100 = period_net * 100 / period_views
    note_b = r["follows_per_100_views"]
    convert_text = (f"账号级（{period_label}）：每百观看涨粉 "
                    f"{fmt_num(acc_follow_per100, 2)}（净增 {fmt_num(period_net, 0)} / 观看 {account_views_text}）；"
                    f"逐篇级：{note_b['valid_n']}/{note_b['total_n']} 条有涨粉，"
                    f"每百观看涨粉 {fmt_num(note_b['value'], 2)}（部分样本）")
    convert_status = "none"
    if acc_follow_per100 is not None and follow_per100_target:
        convert_status = status_from_target(acc_follow_per100, follow_per100_target)
    add(
        "6 关注转化",
        convert_text,
        "available" if acc_follow_per100 is not None else "partial",
        status=convert_status,
        n={"note_follows": note_b["valid_n"], "notes": note_b["total_n"]},
        note=("账号级用同周期净增粉/同周期观看；逐篇级只对有涨粉字段的笔记计算并标注 n；两个口径不合并不相加。"
              + ("账号级观看为后台原文（如「3.4万」）的约值，比率保留两位小数仅供量级参考。" if account.get("period_views_estimated") else "")),
    )

    # ---------- 7 形式对比（描述项） ----------
    def group_line(x: dict) -> str:
        flag = "样本不足" if x["small_sample"] else ""
        ctr_meta = x["metric_meta"]["cover_ctr"]
        fol = x["rates"]["follows_per_100_views"]
        bits = [f"{x['notes']} 条", f"观看中位 {fmt_num(x['views_median'], 0)}",
                f"CTR {pct(ctr_meta['value'])}（{ctr_meta['valid_n']}/{x['notes']}）",
                f"每百观看涨粉 {fmt_num(fol['value'], 2)}（{fol['valid_n']}/{x['notes']}）"]
        return f"{'图文' if x['group'] == 'image' else ('视频' if x['group'] == 'video' else x['group'])}：" + "、".join(bits) + (f"〔{flag}〕" if flag else "")

    add(
        "7 形式对比",
        "；".join(group_line(x) for x in by_format) or "未取到形式数据",
        "descriptive" if by_format else "unavailable",
        status="none",
        n={"groups": len(by_format), "notes": overall["notes"]},
        note="描述项，不参与红黄绿评分；分组内互动率按各自有效覆盖率展示，任意组不足 3 条只写样本不足。",
    )

    # ---------- 8 主题对比（描述项） ----------
    topic_bits = []
    for x in by_topic[:5]:
        ctr_meta = x["metric_meta"]["cover_ctr"]
        topic_bits.append(
            f"{x['group']} {x['notes']} 条、观看中位 {fmt_num(x['views_median'], 0)}、"
            f"CTR {pct(ctr_meta['value'])}（{ctr_meta['valid_n']}/{x['notes']}）"
            + ("〔样本不足〕" if x["small_sample"] else "")
        )
    add(
        "8 主题对比",
        "按观看中位数排序：" + "；".join(topic_bits) if topic_bits else "未取到主题数据",
        "descriptive" if by_topic else "unavailable",
        status="none",
        n={"groups": len(by_topic), "notes": overall["notes"]},
        note="按中位数排序而非总观看（总量由发帖量决定）；主题为人工归类，不同批次的归类规则需一致才可横比。",
    )

    # ---------- 9 视频时长分布 ----------
    add(
        "9 视频时长分布",
        f"视频时长 {duration_n}/{overall['notes']} 未取得，暂无法做时长分布；钩子类型 {len(_text_values(notes, 'hook_type'))}/{overall['notes']} 未取得。补采路径：用 ffprobe 读本地发布包 MP4 时长（不依赖平台），钩子需人工标注后才能分组。",
        "unavailable",
        status="none",
        n={"duration": duration_n, "notes": overall["notes"]},
        note=("原「时长与钩子」拆成这一维；钩子效果在拿到留存/完播前不单列。"
              "补采建议：统一发布包命名后用 ffprobe 读本地 MP4 时长回填；钩子需人工标注后才能分组。"),
    )

    # ---------- 10 粉丝来源与受众画像 ----------
    fans_source = account.get("fans_source") or {}
    fans_gender = account.get("fans_gender") or {}
    source_bits = []
    if isinstance(fans_source, dict) and fans_source:
        ordered = sorted(fans_source.items(), key=lambda kv: parse_cn_number(kv[1])[0] or 0, reverse=True)
        source_bits.append("粉丝来源 " + " / ".join(f"{k} {v}" for k, v in ordered))
    if isinstance(fans_gender, dict) and fans_gender:
        zh = {"male": "男", "female": "女", "unknown": "未知"}
        ordered_gender = sorted(fans_gender.items(), key=lambda kv: parse_cn_number(kv[1])[0] or 0, reverse=True)
        source_bits.append("性别 " + " / ".join(f"{zh.get(k, k)} {v}" for k, v in ordered_gender))
    audience_text = (f"账号级（{period_label}）：" + "；".join(source_bits)) if source_bits else "粉丝来源与性别未取到"
    add(
        "10 粉丝来源与受众画像",
        audience_text,
        "available" if source_bits else "unavailable",
        status="none",
        n={"sources": len(fans_source)},
        note="这是账号级粉丝来源，不是单篇笔记流量来源；单篇推荐/搜索/主页来源需后台逐篇字段，当前不可得。",
    )

    # ---------- 11 标题公式（描述项） ----------
    formula_bits = []
    for x in by_formula:
        ctr_meta = x["metric_meta"]["cover_ctr"]
        formula_bits.append(
            f"{x['group']} {x['notes']} 条、观看中位 {fmt_num(x['views_median'], 0)}、"
            f"CTR {pct(ctr_meta['value'])}（{ctr_meta['valid_n']}/{x['notes']}）"
            + ("〔样本不足〕" if x["small_sample"] else "")
        )
    add(
        "11 标题公式",
        ("按等级：" + "；".join(formula_bits)) if formula_bits else "未取到标题等级",
        "descriptive" if by_formula else "unavailable",
        status="none",
        n={"groups": len(by_formula), "notes": overall["notes"]},
        note="标题等级须在发布前锁定并记录规则版本；事后按结果回标会污染分组，故只作分布描述，不选「最佳公式」。",
    )

    # ---------- 12 发布时段观察（描述项） ----------
    add(
        "12 发布时段观察",
        f"观看中位数最高的发布小时：{best_time}（{fmt_num(best_time_median, 0)}，{best_time_n} 条）"
        + ("〔样本不足〕" if best_time_n and best_time_n < MIN_GROUP_N else "")
        + f"；标签逐篇清单未采集（tags {len(_text_values(notes, 'tags'))}/{overall['notes']}），故不做标签效果比较。",
        "descriptive" if best_time != "未取到" else "unavailable",
        status="none",
        n={"groups": best_time_n},
        note="所有已发布笔记均纳入总体；发布小时仅作描述，不代表因果。分组样本不足 3 条时标注样本不足。",
    )

    # ---------- 13 开头类型实验记录 ----------
    hook_n = len(_text_values(notes, "hook_type"))
    add(
        "13 开头类型实验记录",
        f"开头类型 {hook_n}/{overall['notes']} 未标注，当前无法分组。若要做，需在发布包里发布前填写 opening_type，并配套留存/完播指标才能谈效果；否则只保留分布。",
        "unavailable",
        status="none",
        n={"hook": hook_n, "notes": overall["notes"]},
        note="连续 30 天无稳定标签与指标则删除该维度。",
    )

    # ---------- 14 分享 / 转发价值 ----------
    shares_sum = overall["shares"]
    share_text = f"分享率 {pct(share_b['value'])}（{_n_label(share_b)}）；分享合计 {fmt_num(shares_sum, 0)}"
    if share_b["partial"]:
        share_text += "；覆盖率不足 90%，标为部分样本，不判达标"
    add(
        "14 分享/转发价值",
        share_text,
        "partial" if share_b["partial"] else ("available" if share_b["value"] is not None else "unavailable"),
        status="none" if share_b["partial"] else status_from_target(share_b["value"], share_target),
        n={"shares": share_b["valid_n"], "notes": share_b["total_n"]},
        note="与第 5 维分工：第 5 维看整体互动质量，本维只看转发价值；覆盖不足时状态为部分样本。",
    )

    # ---------- 动作 ----------
    actions = []
    if (watch["value"] is not None and completion_target is not None
            and completion["value"] is not None and completion["value"] < completion_target):
        actions.append("先修前 3 秒和段落顺序：首句留缺口，中段按决策或代价排序，不按时间流水账。")
    if overall["comments_per_note"] is not None and comment_target is not None and overall["comments_per_note"] < comment_target:
        actions.append("自家内容可补一个低门槛表达入口：立场句后只问省份、资格、金额、材料或参保类型中的一个；问题可选，不套给外联评论。")
    if acc_follow_per100 is not None and follow_per100_target is not None and acc_follow_per100 < follow_per100_target:
        actions.append("检查主页承接和关注理由：置顶、简介、前 6 条内容与笔记结尾要指向同一条主线。")
    # 分享率覆盖不足时不给动作建议，避免基于部分样本下指令
    if (not share_b["partial"] and share_b["value"] is not None
            and share_target is not None and share_b["value"] < share_target):
        actions.append("把转发对象写具体：转给正在经历该场景的那个人，不要只写「建议收藏」。")
    if not actions and overall["notes"]:
        actions.append("保留当前结构，只测一个变量；不要同时改标题、封面、开头和发布时间。")
    if not notes:
        actions.append("先补齐至少 1 条笔记数据，不基于空数据做策略判断。")

    availability = Counter(d["availability"] for d in dims)
    coverage_rows = []
    for key, label in (("views", "观看"), ("impressions", "曝光"), ("likes", "点赞"),
                       ("comments", "评论"), ("collects", "收藏"), ("shares", "分享"),
                       ("follows", "单篇涨粉")):
        valid = overall["valid_n"][key]
        coverage_rows.append({
            "field": key, "label": label, "valid_n": valid,
            "total_n": overall["notes"], "coverage": overall["coverage"][key],
        })
    for extra_key, label in (("avg_watch_s", "平均观看时长"), ("duration_s", "视频时长"),
                             ("completion_rate", "完播率"), ("hook_type", "开头类型"),
                             ("tags", "标签")):
        valid = (len(_text_values(notes, extra_key))
                 if extra_key in ("hook_type", "tags")
                 else len(_strict_values(notes, extra_key)))
        coverage_rows.append({
            "field": extra_key, "label": label, "valid_n": valid,
            "total_n": overall["notes"],
            "coverage": (valid / overall["notes"]) if overall["notes"] else None,
        })

    cohort = (config.get("analytics") or {}).get("cohort", "all_published")
    caveats = [
        f"统计口径：{cohort}；所有已发布笔记纳入总量和分组，不按发布方式、排期状态、发布时间或特殊时长排除。",
        "自动报告仅描述数据，不作因果结论；格式、主题、时长和发布时间分组只作观察，组内少于 3 条标注样本不足。",
        "缺失或无效字段保持未取到，不补零、不估算；发布窗口合规另按实际发布时间核对，不影响该笔记纳入分析。",
        "本报告使用统一全量口径；人工复核可补充背景说明，但不得另设排除样本。",
        "状态与可得性分离：status 只表示有目标时的达成情况，availability 表示数据可得程度；描述项不参与红黄绿评分。",
    ]
    if share_b["partial"]:
        caveats.append(f"分享率仅 {share_b['valid_n']}/{share_b['total_n']} 条笔记有分享数，属部分样本，不据此下转发动作。")
    if not acc:
        caveats.append("未读取到账号总览文件，第 1、6、10 维退化为笔记级口径。")

    return {
        "title": f"小红书数据分析 · {data.get('date', '未标日期')}",
        "account": account,
        "scope": data.get("scope", f"{len(notes)} 条样本"),
        "overall": overall,
        "groups": {
            "format": by_format,
            "topic": by_topic,
            "title_formula": by_formula,
            "hook_type": by_hook,
        },
        "top_notes": top_notes,
        "dimensions": dims,
        "availability_counts": dict(availability),
        "coverage": coverage_rows,
        "actions": actions,
        "missing": data.get("missing", []),
        "analysis_caveats": caveats,
    }


AVAILABILITY_LABEL = {
    "available": "可计算",
    "partial": "部分样本",
    "descriptive": "描述线索",
    "unavailable": "无数据",
}
STATUS_LABEL = {"green": "达标", "amber": "预警", "red": "风险", "none": "—"}


def to_markdown(report: dict) -> str:
    o = report["overall"]
    acc = report.get("account") or {}
    lines = [
        f"# {report['title']}",
        "",
        "> **全量口径**：所有已发布笔记共同纳入统计；分组结果只作描述，不作因果判断。",
        "> 缺失数据不补零；小样本比较标注“样本不足”。",
        "",
        f"> 口径：{report['scope']}",
    ]
    if acc.get("period"):
        lines.append(f"> 账号级区间：{acc['period']}（来源 {acc.get('source_file', '—')}，采集 {acc.get('as_of', '—')}）")
    lines.extend([
        "",
        "## 一、关键结论",
        "",
        f"- 样本观看合计 **{fmt_num(o['views'], 0)}**（累计口径，不与日目标比），观看中位数 **{fmt_num(o['views_median'], 0)}**。",
        f"- 封面点击率 **{pct(o['cover_ctr'])}**（{o['valid_n']['impressions']}/{o['notes']} 条有效），收藏率 **{pct(o['collect_rate'])}**（{o['valid_n']['collects']}/{o['notes']}），分享率 **{pct(o['share_rate'])}**（{o['valid_n']['shares']}/{o['notes']}）。",
        f"- 每百观看涨粉 **{fmt_num(o['follows_per_100_views'], 2)}**（{o['valid_n']['follows']}/{o['notes']} 条有效），评论/条 **{fmt_num(o['comments_per_note'], 1)}**。",
        "",
        "## 二、14 维诊断",
        "",
        "| 维度 | 可得性 | 状态 | 现状 |",
        "|---|---|---|---|",
    ])
    for d in report["dimensions"]:
        lines.append(
            f"| {d['name']} | {AVAILABILITY_LABEL.get(d['availability'], d['availability'])} | "
            f"{STATUS_LABEL.get(d['status'], d['status'])} | {d['text']} |"
        )
    counts = report.get("availability_counts") or {}
    if counts:
        lines.extend([
            "",
            "可得性计数：" + "、".join(f"{AVAILABILITY_LABEL.get(k, k)} {v}" for k, v in sorted(counts.items())),
        ])
    lines.extend(["", "## 三、字段覆盖率", "", "| 字段 | 有效样本 | 覆盖率 |", "|---|---:|---:|"])
    for row in report.get("coverage", []):
        cov = row.get("coverage")
        lines.append(f"| {row['label']} | {row['valid_n']}/{row['total_n']} | {pct(cov)} |")
    lines.extend(["", "## 四、形式对比", "", "| 形式 | 条数 | 观看中位 | 封面点击率 | 收藏率 | 分享率 | 每百观看涨粉 |", "|---|---:|---:|---:|---:|---:|---:|"])
    for x in report["groups"]["format"]:
        lines.append(
            f"| {x['group']} | {x['notes']} | {fmt_num(x['views_median'], 0)} | {pct(x['cover_ctr'])} | {pct(x['collect_rate'])} | {pct(x['share_rate'])} | {fmt_num(x['follows_per_100_views'], 2)} |"
        )
    lines.extend(["", "## 五、观看 Top 10", "", "| # | 标题 | 形式 | 观看 | 评论 | 涨粉 |", "|---:|---|---|---:|---:|---:|"])
    for i, n in enumerate(report["top_notes"], 1):
        lines.append(f"| {i} | {n.get('title', '未命名')} | {n.get('format', '')} | {fmt_num(as_number(n.get('views')), 0)} | {fmt_num(as_number(n.get('comments')), 0)} | {fmt_num(as_number(n.get('follows')), 0)} |")
    lines.extend(["", "## 六、下一步动作", ""])
    for i, action in enumerate(report["actions"], 1):
        lines.append(f"{i}. {action}")
    if report.get("analysis_caveats"):
        lines.extend(["", "## 七、口径与边界", ""])
        for x in report["analysis_caveats"]:
            lines.append(f"- {x}")
    if report.get("missing"):
        lines.extend(["", "## 八、缺失数据", ""])
        for x in report["missing"]:
            lines.append(f"- {x}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze exported Xiaohongshu metrics.")
    parser.add_argument("--config", default="xiaohongshu-ops.yaml")
    parser.add_argument("--input", required=True)
    parser.add_argument("--markdown", help="write Markdown report")
    parser.add_argument("--json-out", help="write machine-readable report")
    parser.add_argument("--account-overview", help="账号总览 JSON 路径（默认自动找最新的）")
    parser.add_argument("--no-account-overview", action="store_true",
                        help="不合并账号总览，只用笔记级数据")
    parser.add_argument("--json", action="store_true", help="print JSON instead of Markdown")
    args = parser.parse_args()
    config = load_yaml(args.config)
    errors = [x for x in validate_config(config) if x["level"] == "error"]
    if errors:
        for err in errors:
            print(f"ERROR [{err['code']}] {err['message']}", file=sys.stderr)
        return 2
    try:
        data = read_json(args.input)
        if not isinstance(data, dict):
            raise SkillError("input root must be an object")
        account = None
        if not args.no_account_overview:
            if args.account_overview:
                loaded = load_account_overview([os.path.dirname(args.account_overview) or "."])
                if loaded:
                    account = loaded
                elif os.path.isfile(args.account_overview):
                    account = load_account_overview([os.path.dirname(args.account_overview)])
            else:
                # 默认在工作区的 data/reports 与 data 下找 account_overview_<date>.json
                roots = [os.getcwd(), str(pathlib.Path(args.config).expanduser().resolve().parent)]
                search = [os.path.join(r, sub) for r in dict.fromkeys(roots) for sub in ("data/reports", "data")]
                account = load_account_overview(search, data.get("date"))
        report = build_report(data, config, account)
    except SkillError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
    if args.json_out:
        write_json(args.json_out, report)
    if args.markdown:
        write_text(args.markdown, to_markdown(report))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(to_markdown(report), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
