#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Score a batch of external comments for human tone, specificity, and safety."""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict

from _common import (
    SkillError,
    contains_question,
    count_sentences,
    load_yaml,
    longest_common_substring_len,
    opening_key,
    question_is_last_sentence,
    question_sentence_count,
    read_json,
    validate_config,
)

POSITIVE = [
    "赞", "欣赏", "喜欢", "共鸣", "同频", "说到心里", "讲得透", "讲透", "清楚",
    "实在", "有用", "值得", "收藏", "受用", "认同", "启发", "提醒", "打动",
    "角度", "切入点", "真诚", "有道理", "被.*吸引", "深有同感",
    # 含蓄夸（抓细节 → 含蓄夸眼光/功力 → 显判断力）。
    # 这类表达不含「赞/欣赏」等直白词，原始词表会误杀，故按实战话术扩充。
    "靠谱", "功力", "眼光", "讲究", "内行", "懂行", "老手", "常客", "少见", "难得",
    "不是随手", "不是随便", "不是打卡", "不是一般人", "不是谁都", "编不出来", "编不出",
    "一看就", "吃得出", "看得出来", "听得出来", "经得起", "花心思", "用心", "克制",
    "分寸", "点得准", "说得准", "写得出", "拍得出", "拿捏", "到位", "舒服",
    "真能上手", "能落地", "说到点上", "讲得清", "讲得明白", "说服", "服气",
    "清醒", "通透", "有价值", "有分量", "扎实", "硬核", "值得关注",
]
SAME_FREQUENCY = [
    "共鸣", "同频", "我也", "原来不只", "说到心里", "一样", "理解", "深有同感",
    "一直在关注", "有同感", "被.*吸引",
    # 含蓄版同频：显判断力本身即「我们看同一个东西」，不一定要写「我也」
    "不是随手", "不是随便", "不是打卡", "内行", "懂行", "常客", "经得起",
    "看得出来", "吃得出", "靠谱", "难得", "少见",
    "和我想的一样", "正好也是我", "我也这么看", "和我的判断一致", "这正是我认同的",
    "这份感受我懂", "说到我心里", "同感", "共情",
]
GENERIC_ONLY = [
    "说得好", "学习了", "谢谢分享", "受教了", "太棒了", "不错不错", "支持一下",
]
FORBIDDEN_DEFAULT = [
    r"https?://", r"www\.", r"微信", r"微\s*信", r"\bVX\b|\bvx\b", r"\bQQ\b",
    r"加我", r"私信我", r"关注我", r"互关", r"互赞", r"看我主页", r"主页有",
    r"扫码", r"二维码", r"联系方式", r"手机号", r"1[3-9]\d{9}",
]
STOP_NGRAMS = {
    "这个", "那个", "就是", "真的很", "其实", "很多", "一个", "自己", "我们", "你们",
    "他们", "因为", "所以", "但是", "不是", "可以", "什么", "怎么", "已经", "还是",
}


def text_of(item: dict) -> str:
    return str(item.get("text") or item.get("comment") or "").strip()


def context_text(item: dict) -> str:
    parts = [item.get("post_title"), item.get("post_text"), item.get("post_excerpt")]
    return "\n".join(str(x) for x in parts if x)


def has_pattern(text: str, pattern: str) -> bool:
    try:
        return bool(re.search(pattern, text, flags=re.I))
    except re.error:
        return pattern in text


def lint_one(item: dict, config: dict, index: int) -> dict:
    outreach = config["engagement"]["outreach"]
    tone = outreach.get("tone", {}) or {}
    max_chars = int(tone.get("max_chars", 80))
    max_sentences = int(tone.get("max_sentences", 2))
    text = text_of(item)
    kind = item.get("kind", "unknown")
    style = item.get("style", "")
    checks: list[dict] = []

    def add(level: str, code: str, message: str) -> None:
        checks.append({"level": level, "code": code, "message": message})

    if not text:
        add("fail", "empty", "评论为空")
        return {"index": index, "kind": kind, "text": text, "status": "fail", "score": 0, "checks": checks}

    # 轻夸模式（可选）：刷到日常/生活向内容、没有具体细节可接时，
    # 允许只留一句 ≤15 字的短夸。跳过「具体细节/同频/赞扬词」硬校验，
    # 但导流词、问句、句数、长度仍然硬拦。
    lp = {}
    if style == "light_praise":
        lp = tone.get("light_praise", {}) or {}
        if not lp.get("enabled", True):
            add("fail", "style_disabled", "light_praise 风格未在配置中启用")
    if lp:
        max_chars = int(lp.get("max_chars", 15))
        max_sentences = int(lp.get("max_sentences", 1))
    else:
        max_chars = int(tone.get("max_chars", 80))
        max_sentences = int(tone.get("max_sentences", 2))

    patterns = list(FORBIDDEN_DEFAULT)
    patterns.extend(config.get("compliance", {}).get("banned_patterns", []) or [])
    for pat in patterns:
        if has_pattern(text, pat):
            add("fail", "forbidden", f"命中导流/联系方式模式：{pat}")
            break

    # 长度按汉字计数（默认硬上限 30 汉字，配置 tone.max_chars 可调）；
    # 标点、空格不计入上限。
    han_len = len(re.findall(r"[\u4e00-\u9fff]", text))
    if han_len > max_chars:
        add("fail", "too_long", f"{han_len} 个汉字，超过 {max_chars} 字上限")
    elif han_len > max_chars * 0.85 and not lp:
        add("warn", "near_limit", f"{han_len} 个汉字，接近长度上限")

    sentences = count_sentences(text)
    if sentences > max_sentences:
        add("fail", "too_many_sentences", f"{sentences} 句，超过 {max_sentences} 句")

    if not lp:
        if any(word in text for word in POSITIVE) or re.search(r"被.{0,8}吸引", text):
            pass
        else:
            add("fail" if tone.get("praise_required", True) else "warn", "no_positive", "缺少赞扬、欣赏或共鸣表达")

        if any(word in text for word in SAME_FREQUENCY) or re.search(r"被.{0,8}吸引", text):
            pass
        else:
            add(
                "fail" if tone.get("same_frequency_required", True) else "warn",
                "no_frequency",
                "缺少同频感；要自然表达‘我也关注/有共鸣/被吸引/和我想的一样’",
            )

        ctx = context_text(item)
        if ctx:
            lcs = longest_common_substring_len(text, ctx)
            if lcs < 3:
                add("fail", "weak_specificity", "没有提到原帖里的具体内容，容易像通用套话")
            else:
                add("pass", "specificity", f"提到原帖具体内容（重合 {lcs} 字）")
        else:
            add("fail", "no_context", "未提供原帖标题或正文，无法验证评论是否贴住具体内容")

        compact = re.sub(r"[\s，。！？!?、,.]+", "", text)
        if len(compact) <= 12 and any(g in text for g in GENERIC_ONLY):
            add("fail", "generic", "只有通用夸赞，缺少具体内容")
        elif any(g in text for g in GENERIC_ONLY) and len(compact) < 24:
            add("warn", "generic_hint", "通用夸赞偏多，建议补一个原帖细节")

    q = contains_question(text)
    q_sentences = question_sentence_count(text)
    if q_sentences > 1:
        add("fail", "multiple_questions", f"评论含 {q_sentences} 个问句；每条最多一个")
    if lp and q:
        add("fail", "light_praise_no_question", "轻夸模式不允许带问句")
    if q and not question_is_last_sentence(text):
        level = "fail" if tone.get("question_last_when_used", True) else "warn"
        add(level, "question_not_last", "问句必须放在最后一句，不能先追问再继续陈述")
    elif q and not lp:
        add("pass", "question_last", "问句位于最后一句")
    if not lp and style == "gentle_question" and not q:
        add("warn", "style_mismatch", "计划为轻问句，但文本没有问句")

    fail_count = sum(1 for c in checks if c["level"] == "fail")
    warn_count = sum(1 for c in checks if c["level"] == "warn")
    score = max(0, 100 - 45 * fail_count - 10 * warn_count)
    status = "fail" if fail_count else ("warn" if warn_count else "pass")
    return {
        "index": index,
        "kind": kind,
        "style": style,
        "text": text,
        "has_question": q,
        "status": status,
        "score": score,
        "checks": checks,
    }


def lint_batch(comments: list[dict], config: dict) -> dict:
    results = [lint_one(item, config, i) for i, item in enumerate(comments, 1)]

    # Repeated openings and exact duplicates are batch-level failures.
    opening_counts = Counter(opening_key(r["text"]) for r in results if r["text"])
    text_counts = Counter(r["text"] for r in results if r["text"])
    for r in results:
        op = opening_key(r["text"])
        if op and opening_counts[op] > 1:
            r["checks"].append({"level": "fail", "code": "repeated_opening", "message": f"开头重复：{op}"})
        if r["text"] and text_counts[r["text"]] > 1:
            r["checks"].append({"level": "fail", "code": "duplicate", "message": "本批次出现完全重复评论"})
        fail_count = sum(1 for c in r["checks"] if c["level"] == "fail")
        warn_count = sum(1 for c in r["checks"] if c["level"] == "warn")
        r["score"] = max(0, 100 - 45 * fail_count - 10 * warn_count)
        r["status"] = "fail" if fail_count else ("warn" if warn_count else "pass")

    ratios = {}
    for kind in ("same_topic", "viral"):
        subset = [r for r in results if r["kind"] == kind]
        if subset:
            ratios[kind] = sum(1 for r in subset if r["has_question"]) / len(subset)
    total_ratio = (sum(1 for r in results if r["has_question"]) / len(results)) if results else 0

    tone = config["engagement"]["outreach"].get("tone", {}) or {}
    targets = tone.get("question_ratio_target", {}) or {}
    question_max = tone.get("question_max", {}) or {}
    aggregate_issues = []
    for kind, ratio in ratios.items():
        max_ratio = float(targets.get(kind, 0.25))
        subset_size = sum(1 for r in results if r["kind"] == kind)
        scaled = max(1, int(round(subset_size * max_ratio))) if subset_size else 0
        configured = int(question_max.get(kind, subset_size)) if question_max else subset_size
        allowed = min(scaled, configured)
        question_count = sum(1 for r in results if r["kind"] == kind and r["has_question"])
        if question_count > allowed:
            aggregate_issues.append({
                "level": "fail",
                "code": "question_ratio",
                "message": f"{kind} 问句 {question_count}/{subset_size}，超过本批次上限 {allowed} 条（目标比例 {max_ratio:.1%}）",
            })
    total_max = float(tone.get("question_ratio_total_max", 0.167))
    total_scaled = max(1, int(round(len(results) * total_max))) if results else 0
    total_configured = int(question_max.get("total", len(results))) if question_max else len(results)
    total_allowed = min(total_scaled, total_configured)
    total_questions = sum(1 for r in results if r["has_question"])
    if total_questions > total_allowed:
        aggregate_issues.append({
            "level": "fail",
            "code": "question_ratio_total",
            "message": f"总问句 {total_questions}/{len(results)}，超过本批次上限 {total_allowed} 条（目标比例 {total_max:.1%}）",
        })

    for a, b in zip(results, results[1:]):
        if a["has_question"] and b["has_question"]:
            aggregate_issues.append({
                "level": "fail",
                "code": "consecutive_questions",
                "message": f"第 {a['index']} 和 {b['index']} 条连续使用问句；问句之间至少隔一条陈述型评论",
            })

    # 轻夸模式批次约束：连续两条不得都是轻夸；每日轻夸有上限。
    lp_cfg = config["engagement"]["outreach"].get("tone", {}).get("light_praise", {}) or {}
    if lp_cfg.get("enabled", False):
        lp_flags = [bool(r.get("style") == "light_praise") for r in results]
        for i, (a, b) in enumerate(zip(lp_flags, lp_flags[1:]), 1):
            if a and b:
                aggregate_issues.append({
                    "level": "fail",
                    "code": "consecutive_light_praise",
                    "message": f"第 {i} 和 {i + 1} 条连续使用轻夸；轻夸之间至少隔一条正评",
                })
        lp_max = int(lp_cfg.get("max_per_day", 10))
        lp_count = sum(lp_flags)
        if lp_count > lp_max:
            aggregate_issues.append({
                "level": "fail",
                "code": "light_praise_daily",
                "message": f"轻夸 {lp_count} 条，超过每日上限 {lp_max} 条",
            })

    daily_total = int(config["engagement"]["outreach"].get("daily_total", 30))
    pool_limits = config["engagement"]["outreach"].get("pools", {}) or {}
    if len(results) > daily_total:
        aggregate_issues.append({
            "level": "fail",
            "code": "daily_total",
            "message": f"本批次 {len(results)} 条，超过每日计划上限 {daily_total} 条",
        })
    for kind in ("same_topic", "viral"):
        kind_count = sum(1 for r in results if r["kind"] == kind)
        kind_cap = int(pool_limits.get(kind, kind_count))
        if kind_count > kind_cap:
            aggregate_issues.append({
                "level": "fail",
                "code": "pool_limit",
                "message": f"{kind} 本批次 {kind_count} 条，超过池上限 {kind_cap} 条",
            })

    counts = Counter(r["status"] for r in results)
    ok = counts["fail"] == 0 and not any(x["level"] == "fail" for x in aggregate_issues)
    return {
        "summary": {
            "total": len(results),
            "pass": counts["pass"],
            "warn": counts["warn"],
            "fail": counts["fail"],
            "question_count": sum(1 for r in results if r["has_question"]),
            "question_ratio": total_ratio,
            "ratios_by_kind": ratios,
            "ok": ok,
        },
        "aggregate_issues": aggregate_issues,
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Lint external comments before posting.")
    parser.add_argument("--config", default="xiaohongshu-ops.yaml")
    parser.add_argument("--input", required=True, help="JSON array, JSON object with comments[], or - for stdin")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true", help="warnings also fail the command")
    args = parser.parse_args()

    config = load_yaml(args.config)
    errors = [x for x in validate_config(config) if x["level"] == "error"]
    if errors:
        for err in errors:
            print(f"ERROR [{err['code']}] {err['message']}", file=sys.stderr)
        return 2

    try:
        raw = json.load(sys.stdin) if args.input == "-" else read_json(args.input)
        comments = raw.get("comments") if isinstance(raw, dict) else raw
        if not isinstance(comments, list) or not all(isinstance(x, dict) for x in comments):
            raise SkillError("input must be a JSON array or an object with comments[]")
        report = lint_batch(comments, config)
    except SkillError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        s = report["summary"]
        print(f"comments: {s['total']} | pass {s['pass']} | warn {s['warn']} | fail {s['fail']}")
        print(f"questions: {s['question_count']} ({s['question_ratio']:.1%})")
        for issue in report["aggregate_issues"]:
            print(f"{issue['level'].upper()} [{issue['code']}] {issue['message']}")
        for r in report["results"]:
            print(f"\n#{r['index']} [{r['status']}] {r['score']} {r['kind']} {r.get('style', '')}")
            print(f"  {r['text']}")
            for c in r["checks"]:
                if c["level"] != "pass":
                    print(f"  {c['level'].upper()} [{c['code']}] {c['message']}")

    has_fail = not report["summary"]["ok"] or bool(report["aggregate_issues"])
    if args.strict:
        has_fail = has_fail or report["summary"]["warn"] > 0
    return 1 if has_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
