#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_guard.py — 付费生成预算闸门 / 台账 / 单价校准

背景：实战中一条 42 秒视频曾耗掉数十次付费调用和整个供应商额度。
规则详见 references/budget-gate.md（R1–R8）。

子命令：
  plan       报预算并做硬性校验（未通过 exit 1，不得开工）
  approve    拿到用户原话同意后核销（写 approval，4 小时有效）
  record     每次真实调用后记账
  calibrate  用平台积分差额校准单价
  status     查当前授权与本轮已用额度

零依赖，仅标准库。台账默认写在当前工作目录 data/ 下；
可用环境变量 XHS_OPS_WORKSPACE 指定账号工作区根目录。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta

ROOT = os.environ.get("XHS_OPS_WORKSPACE") or os.getcwd()
DATA = os.path.join(ROOT, "data")
BUDGET = os.path.join(DATA, "gen-budget.json")
APPROVAL = os.path.join(DATA, "gen-approval.json")
LEDGER = os.path.join(DATA, "gen-ledger.jsonl")
UNIT = os.path.join(DATA, "gen-unit-cost.json")

DEFAULT_BUDGET = {
    "max_image_calls": 12,
    "max_audio_calls": 12,
    "max_video_calls_hard": 8,
    "video_seconds_per_call": 10,     # 单段视频目标时长上限（秒）
    "require_publish_confirmed": True,  # 付费前必须确认这条要发
    "require_draft_reviewed": True,     # 付费视频前必须有零成本草稿且用户已看
    "approval_ttl_hours": 4,
    "allow_2k_video_without_reason": False,
    "allow_2k_image_without_reason": False,
}


def _load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default


def _save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def budget_cfg():
    cfg = dict(DEFAULT_BUDGET)
    cfg.update(_load_json(BUDGET, {}))
    return cfg


def plan_hash(plan):
    return hashlib.sha1(
        json.dumps(plan, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]


# ---------------------------------------------------------------- 计划解析

def normalize(plan):
    """把 plan 展开成逐类计数。"""
    stats = {
        "image": {"calls": 0, "items": 0, "specs": []},
        "video": {"calls": 0, "items": 0, "seconds": 0.0, "specs": []},
        "audio": {"calls": 0, "items": 0, "specs": []},
    }
    for c in plan.get("calls", []):
        kind = c.get("kind")
        if kind not in stats:
            continue
        vendor = c.get("vendor", "?")
        if kind == "video":
            segs = c.get("segments") or []
            if not segs:
                segs = [c.get("duration") or 0]
            cnt = len(segs)
            stats[kind]["calls"] += cnt
            stats[kind]["items"] += cnt
            stats[kind]["seconds"] += float(sum(segs))
            stats[kind]["specs"].append(
                f"{vendor} {c.get('resolution','?')} ×{cnt} 段 / {sum(segs):.1f}s"
            )
        else:
            cnt = int(c["count"]) if c.get("count") is not None else 1
            if cnt <= 0:
                continue
            stats[kind]["calls"] += cnt
            stats[kind]["items"] += cnt
            spec = c.get("size") or c.get("quality") or c.get("model") or ""
            stats[kind]["specs"].append(f"{vendor} {spec} ×{cnt}")
    return stats


def video_cap(plan, cfg):
    target = float(plan.get("target_duration_s") or 0)
    if target <= 0:
        return 0
    derived = int(-(-target // cfg["video_seconds_per_call"])) + 1
    return min(derived, cfg["max_video_calls_hard"])


def check_plan(plan, cfg):
    """返回 (errors, warnings)。errors 非空即禁止开工。"""
    errors, warnings = [], []
    stats = normalize(plan)

    if cfg["require_publish_confirmed"] and not plan.get("note_publish_confirmed"):
        errors.append(
            "未确认『这条笔记要发』。未定稿前禁止任何付费生成——先做本地可看的版本。"
        )

    if stats["video"]["calls"] > 0:
        if cfg["require_draft_reviewed"] and not plan.get("draft_reviewed"):
            errors.append(
                "付费视频前必须有零成本草稿（静图 + Ken Burns）且用户已看过节奏（draft_reviewed=true）。"
            )
        cap = video_cap(plan, cfg)
        if cap and stats["video"]["calls"] > cap:
            errors.append(
                f"视频调用 {stats['video']['calls']} 次，超出上限 {cap} 次"
                f"（目标时长 {plan.get('target_duration_s')}s，单段上限 {cfg['video_seconds_per_call']}s，硬上限 {cfg['max_video_calls_hard']}）。"
            )
        if not plan.get("target_duration_s"):
            errors.append("视频计划缺少 target_duration_s，无法核算调用上限。")

    if stats["image"]["calls"] > cfg["max_image_calls"]:
        errors.append(
            f"图片调用 {stats['image']['calls']} 次，超出上限 {cfg['max_image_calls']}。"
        )
    if stats["audio"]["calls"] > cfg["max_audio_calls"]:
        errors.append(
            f"配音调用 {stats['audio']['calls']} 次，超出上限 {cfg['max_audio_calls']}"
            "（考虑整段生成或本地 TTS）。"
        )

    # 升档必须给理由
    for c in plan.get("calls", []):
        res = str(c.get("resolution") or "").upper()
        size = str(c.get("size") or "")
        if c.get("kind") == "video" and res in ("2K", "4K", "1080P", "1440P"):
            if not cfg["allow_2k_video_without_reason"] and not c.get("escalation_reason"):
                errors.append(
                    f"视频档位 {res} 属升档，必须在 calls 里写 escalation_reason。"
                    "默认 768P 即可，成片最终压到 1080×1920 分发。"
                )
        if c.get("kind") == "image" and ("2048" in size or "2K" in size.upper()):
            if not cfg["allow_2k_image_without_reason"] and not c.get("escalation_reason"):
                warnings.append(f"图片规格 {size} 偏高，确认是否必要（默认 1K 足够）。")

    if stats["video"]["calls"] or stats["image"]["calls"]:
        warnings.append(
            "开工前请把下方预算表原样贴给用户，取得『同意』原话后再 approve。"
        )
    return errors, warnings


def estimate(stats):
    unit = _load_json(UNIT, {})
    if not unit.get("credits_per_image_call") and not unit.get("credits_per_video_second"):
        return None
    total = 0.0
    total += stats["image"]["calls"] * float(unit.get("credits_per_image_call") or 0)
    total += stats["audio"]["calls"] * float(unit.get("credits_per_audio_call") or 0)
    total += stats["video"]["seconds"] * float(unit.get("credits_per_video_second") or 0)
    return total


def fmt_plan(plan, stats, errors, warnings):
    cid = plan_hash(plan)
    L = []
    L.append(f"计划：{plan.get('task','(未命名)')}   目标时长 {plan.get('target_duration_s','-')}s   #{cid}")
    L.append("-" * 66)
    L.append(f"{'类型':<6}{'次数':>6}{'明细'}")
    for k in ("image", "video", "audio"):
        s = stats[k]
        if s["calls"]:
            detail = "；".join(s["specs"])
            extra = f" 合计 {s['seconds']:.1f}s" if k == "video" else ""
            L.append(f"{k:<6}{s['calls']:>6}  {detail}{extra}")
    L.append("-" * 66)
    est = estimate(stats)
    if est is None:
        L.append("预估消耗：单价未校准（先跑 calibrate，或用平台报价手填到 data/gen-unit-cost.json）")
    else:
        L.append(f"预估消耗：≈ {est:.0f} 积分")
    L.append(f"零成本草稿已看：{'是' if plan.get('draft_reviewed') else '否'}"
             f"   发布意图已确认：{'是' if plan.get('note_publish_confirmed') else '否'}")
    if warnings:
        L.append("")
        for w in warnings:
            L.append(f"⚠  {w}")
    if errors:
        L.append("")
        L.append("✗ 禁止开工：")
        for e in errors:
            L.append(f"   · {e}")
    else:
        L.append("")
        L.append("✓ 校验通过，仍需用户书面同意后执行 approve")
    return "\n".join(L)


# ---------------------------------------------------------------- 子命令

def cmd_plan(args):
    cfg = budget_cfg()
    plan = _load_json(args.file, None)
    if not plan:
        print(f"读不到计划文件：{args.file}")
        return 2
    stats = normalize(plan)
    errors, warnings = check_plan(plan, cfg)
    print(fmt_plan(plan, stats, errors, warnings))
    return 1 if errors else 0


def cmd_approve(args):
    cfg = budget_cfg()
    plan = _load_json(args.file, None)
    if not plan:
        print(f"读不到计划文件：{args.file}")
        return 2
    stats = normalize(plan)
    errors, _ = check_plan(plan, cfg)
    if errors:
        print("✗ 校验未通过，拒绝核销：")
        for e in errors:
            print(f"   · {e}")
        return 1
    phrase = (args.confirm or "").strip()
    if len(phrase) < 4:
        print("✗ --confirm 需要引用户同意的原话（≥4 字），不接受空/占位。")
        return 1
    rec = {
        "plan_hash": plan_hash(plan),
        "task": plan.get("task"),
        "confirmed_phrase": phrase,
        "approved_at": datetime.now().isoformat(timespec="seconds"),
        "expires_at": (datetime.now() + timedelta(hours=cfg["approval_ttl_hours"])).isoformat(timespec="seconds"),
        "allowance": {k: stats[k]["calls"] for k in stats},
        "used": {"image": 0, "video": 0, "audio": 0},
        "video_seconds_allowance": stats["video"]["seconds"],
        "video_seconds_used": 0.0,
        "plan": plan,
    }
    _save_json(APPROVAL, rec)
    print(f"✓ 已核销 #{rec['plan_hash']}  有效至 {rec['expires_at']}")
    print(f"   额度：图 {stats['image']['calls']} / 视频 {stats['video']['calls']} / 配音 {stats['audio']['calls']}")
    return 0


def _approval_active(rec):
    if not rec:
        return False
    try:
        return datetime.now() < datetime.fromisoformat(rec["expires_at"])
    except Exception:
        return False


def cmd_record(args):
    rec = _load_json(APPROVAL, {})
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "vendor": args.vendor,
        "kind": args.kind,
        "model": args.model,
        "spec": args.spec,
        "seconds": float(args.seconds or 0),
        "result": args.result,
        "usable": bool(args.usable),
        "note": args.note or "",
        "plan_hash": rec.get("plan_hash") if _approval_active(rec) else None,
    }
    os.makedirs(DATA, exist_ok=True)
    with open(LEDGER, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"· 已记账 {entry['ts']} {args.vendor}/{args.kind} {args.spec} → {args.result}"
          f"{'（可用）' if args.usable else '（浪费）'}")

    if not _approval_active(rec):
        print("⚠  当前无有效授权记录，本次调用未挂在任何计划下——是否需要补报预算？")
        return 0

    used = rec.setdefault("used", {})
    used[args.kind] = used.get(args.kind, 0) + 1
    if args.kind == "video":
        rec["video_seconds_used"] = round(rec.get("video_seconds_used", 0.0) + entry["seconds"], 2)
    _save_json(APPROVAL, rec)

    over = []
    for k, allow in rec.get("allowance", {}).items():
        if used.get(k, 0) > allow:
            over.append(f"{k} 已用 {used[k]} / 授权 {allow}")
    if rec.get("video_seconds_used", 0) > rec.get("video_seconds_allowance", 0) + 0.5:
        over.append(
            f"视频秒数 {rec['video_seconds_used']:.1f}s 超出授权 {rec['video_seconds_allowance']:.1f}s"
        )
    if over:
        print("✗ 已超额，立即停止生成并重新报预算：")
        for o in over:
            print(f"   · {o}")
        return 1
    left = {k: rec["allowance"].get(k, 0) - used.get(k, 0) for k in rec["allowance"]}
    print("   剩余额度：" + " / ".join(f"{k} {v}" for k, v in left.items()))
    return 0


def cmd_calibrate(args):
    spent = float(args.before) - float(args.after)
    rows = []
    if os.path.exists(LEDGER):
        with open(LEDGER, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    since = args.since or ""
    rows = [r for r in rows if r.get("ts", "") >= since] if since else rows
    n_img = sum(1 for r in rows if r["kind"] == "image")
    n_aud = sum(1 for r in rows if r["kind"] == "audio")
    v_sec = sum(float(r.get("seconds") or 0) for r in rows if r["kind"] == "video")
    n_vid = sum(1 for r in rows if r["kind"] == "video")

    unit = {
        "calibrated_at": datetime.now().isoformat(timespec="seconds"),
        "sample_calls": {"image": n_img, "audio": n_aud, "video": n_vid},
        "sample_video_seconds": round(v_sec, 2),
        "measured_credits_spent": spent,
        "note": args.note or "",
    }
    # 只有当某类单独成批时才给单一单价，混合批次用加权提示，避免瞎猜
    kinds = [k for k, n in (("image", n_img), ("audio", n_aud), ("video", v_sec)) if n]
    if len(kinds) == 1:
        k = kinds[0]
        if k == "video":
            unit["credits_per_video_second"] = round(spent / v_sec, 2) if v_sec else None
        elif k == "image":
            unit["credits_per_image_call"] = round(spent / n_img, 2)
        else:
            unit["credits_per_audio_call"] = round(spent / n_aud, 2)
    else:
        unit["mixed_batch"] = True
        unit["hint"] = (
            "本轮混合了多种类型，无法拆分单价。下次请分别单测，"
            "或手工填入 credits_per_image_call / credits_per_video_second。"
        )
    _save_json(UNIT, unit)
    print(json.dumps(unit, ensure_ascii=False, indent=2))
    return 0


def cmd_status(args):
    cfg = budget_cfg()
    rec = _load_json(APPROVAL, {})
    if not rec:
        print("当前无授权记录。付费生成必须先 plan + approve。")
    else:
        active = _approval_active(rec)
        print(f"计划：{rec.get('task')}  #{rec.get('plan_hash')}  "
              f"{'有效' if active else '已过期'}")
        print(f"确认原话：{rec.get('confirmed_phrase')}")
        print(f"有效期：{rec.get('expires_at')}")
        used = rec.get("used", {})
        for k, allow in rec.get("allowance", {}).items():
            print(f"  {k:<6} 已用 {used.get(k,0):>2} / {allow}")
        print(f"  视频秒数 已用 {rec.get('video_seconds_used',0):.1f}s / {rec.get('video_seconds_allowance',0):.1f}s")
    unit = _load_json(UNIT, {})
    print("单价：" + (json.dumps(unit, ensure_ascii=False) if unit else "未校准"))
    if os.path.exists(LEDGER):
        n = sum(1 for _ in open(LEDGER, encoding="utf-8"))
        wasted = 0
        with open(LEDGER, encoding="utf-8") as f:
            for line in f:
                try:
                    if not json.loads(line).get("usable", True):
                        wasted += 1
                except Exception:
                    pass
        print(f"台账：{n} 条，其中浪费 {wasted} 次")
    return 0


def main():
    ap = argparse.ArgumentParser(description="付费生成预算闸门 / 台账 / 单价校准")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="报预算并校验")
    p.add_argument("--file", required=True)
    p.set_defaults(func=cmd_plan)

    a = sub.add_parser("approve", help="用户同意后核销")
    a.add_argument("--file", required=True)
    a.add_argument("--confirm", required=True, help="用户同意的原话")
    a.set_defaults(func=cmd_approve)

    r = sub.add_parser("record", help="记账")
    r.add_argument("--vendor", required=True)
    r.add_argument("--kind", required=True, choices=["image", "video", "audio"])
    r.add_argument("--model", default="")
    r.add_argument("--spec", default="")
    r.add_argument("--seconds", default="0")
    r.add_argument("--result", default="ok")
    r.add_argument("--usable", action="store_true")
    r.add_argument("--note", default="")
    r.set_defaults(func=cmd_record)

    c = sub.add_parser("calibrate", help="用积分差额校准单价")
    c.add_argument("--before", required=True)
    c.add_argument("--after", required=True)
    c.add_argument("--since", default="")
    c.add_argument("--note", default="")
    c.set_defaults(func=cmd_calibrate)

    s = sub.add_parser("status", help="查看授权与台账")
    s.set_defaults(func=cmd_status)

    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
