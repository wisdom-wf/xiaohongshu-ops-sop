#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Check a local publish bundle before browser automation."""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from collections import Counter

from _common import SkillError, load_yaml, validate_config

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v"}
TITLE_NAMES = ("title.txt", "标题.txt")
BODY_NAMES = ("body.txt", "正文.txt")
COUNT_CLAIM_RE = re.compile(r"(\d+)\s*(笔|项|类|种|个|步|条|份|次|张|页|道|句|招|坑)")


def first_existing(root: pathlib.Path, names: tuple[str, ...], explicit: str | None) -> pathlib.Path | None:
    if explicit:
        p = root / explicit
        return p if p.exists() else None
    for name in names:
        p = root / name
        if p.exists():
            return p
    return None


def validate_visual_review(root: pathlib.Path, page_imgs: list[pathlib.Path], title: str, config: dict) -> list[dict]:
    """Validate the recorded image visual review against the current package.

    This is a process gate, not an automated aesthetic judgment. The review
    record must show that both the phone-scale preview and full contact sheet
    were inspected, and it must match the exact title, page list and page count.
    """
    issues: list[dict] = []

    def add(code: str, message: str) -> None:
        issues.append({"level": "error", "code": code, "message": message})

    gate = ((config.get("content", {}) or {}).get("image", {}) or {}).get("visual_gate", {}) or {}
    if gate.get("required") is not True:
        return issues

    review_name = str(gate.get("review_file", "visual-review.json"))
    review_path = root / review_name
    if not review_path.exists():
        add("visual_review_missing", f"{review_name} is required for image posts")
        return issues
    try:
        review = json.loads(review_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        add("visual_review_invalid", f"cannot read {review_name}: {exc}")
        return issues
    if not isinstance(review, dict):
        add("visual_review_invalid", f"{review_name} root must be an object")
        return issues

    compact_title = re.sub(r"\s+", "", title)
    review_title = re.sub(r"\s+", "", str(review.get("title", "")))
    if not compact_title or review_title != compact_title:
        add("visual_review_stale", "visual review title does not match title.txt")

    expected_images = sorted(str(path.relative_to(root)) for path in page_imgs)
    recorded_images = review.get("page_images")
    if not isinstance(recorded_images, list) or sorted(map(str, recorded_images)) != expected_images:
        add("visual_review_stale", "visual review page_images does not match the current image list")
    if review.get("page_count") != len(page_imgs):
        add("visual_review_stale", f"visual review page_count must be {len(page_imgs)}")

    preview_width = gate.get("preview_width_px", 270)
    try:
        if int(review.get("preview_width_px", 0)) < int(preview_width):
            add("visual_preview_width", f"preview_width_px must be at least {preview_width}")
    except (TypeError, ValueError):
        add("visual_preview_width", "preview_width_px must be an integer")
    if review.get("preview_reviewed") is not True:
        add("visual_preview_unreviewed", "the phone-scale cover preview was not reviewed")
    if review.get("contact_sheet_reviewed") is not True:
        add("visual_contact_sheet_unreviewed", "the full contact sheet was not reviewed")
    cover_promise = review.get("cover_promise_sentence")
    if not isinstance(cover_promise, str) or len(re.sub(r"\s+", "", cover_promise)) < 8:
        add("visual_cover_promise", "cover_promise_sentence must record one complete, readable sentence")

    first_read = review.get("first_read", {}) or {}
    for key in ("audience", "scenario", "action", "outcome"):
        if not isinstance(first_read.get(key), str) or not first_read.get(key, "").strip():
            add("visual_first_read", f"first_read.{key} must be filled after the 270px preview review")

    canvas = review.get("canvas", {}) or {}
    expected_canvas = gate.get("canvas", [1080, 1440])
    if not isinstance(expected_canvas, list) or len(expected_canvas) != 2:
        expected_canvas = [1080, 1440]
    try:
        if (int(canvas.get("width", 0)), int(canvas.get("height", 0))) != tuple(map(int, expected_canvas)):
            add("visual_canvas", f"canvas must be {expected_canvas[0]}x{expected_canvas[1]}")
    except (TypeError, ValueError):
        add("visual_canvas", "canvas width and height must be integers")

    typography = review.get("typography", {}) or {}
    thresholds = {
        "key_text_px": float(gate.get("min_key_text_px", 38)),
        "support_text_px": float(gate.get("min_support_text_px", 30)),
        "source_text_px": float(gate.get("min_source_text_px", 26)),
        "normal_contrast_ratio": float(gate.get("min_normal_contrast_ratio", 4.5)),
        "large_contrast_ratio": float(gate.get("min_large_contrast_ratio", 3.0)),
    }
    for key, minimum in thresholds.items():
        try:
            actual = float(typography.get(key, 0))
        except (TypeError, ValueError):
            actual = 0
        if actual < minimum:
            add("visual_typography", f"typography.{key} must be at least {minimum:g}; got {actual:g}")

    # Count promises such as 2笔 / 3项 / 5步 must name that many independent units.
    for token, unit in sorted(set(COUNT_CLAIM_RE.findall(title))):
        required = int(token)
        claims = review.get("numeric_claims", []) or []
        matches = [x for x in claims if isinstance(x, dict) and str(x.get("token")) == token]
        if not matches:
            add("visual_numeric_claim", f"numeric claim {token}{unit} is not recorded in visual review")
            continue
        units = matches[0].get("independent_units")
        if not isinstance(units, list) or len(units) < required:
            add(
                "visual_numeric_claim",
                f"numeric claim {token}{unit} needs {required} independent units; found {len(units) if isinstance(units, list) else 0}",
            )
        if matches[0].get("fulfilled") is not True:
            add("visual_numeric_claim", f"numeric claim {token}{unit} is not marked as independently fulfilled")

    checks = review.get("checks", {}) or {}
    for check_id in gate.get("required_checks", [f"V{i:02d}" for i in range(1, 11)]):
        if checks.get(check_id) is not True:
            add("visual_check_failed", f"{check_id} must pass before publishing")
    if not isinstance(review.get("reviewer_notes"), str) or not review.get("reviewer_notes", "").strip():
        add("visual_reviewer_notes", "reviewer_notes must describe what evidence was checked")
    if review.get("decision") != "pass":
        add("visual_decision", "visual review decision must be pass")
    return issues


def check_package(root: pathlib.Path, config: dict, kind: str, title_name: str | None, body_name: str | None) -> dict:
    issues: list[dict] = []
    def add(level: str, code: str, message: str) -> None:
        issues.append({"level": level, "code": code, "message": message})

    if not root.exists() or not root.is_dir():
        return {"ok": False, "package": str(root), "issues": [{"level": "error", "code": "missing_dir", "message": "package directory not found"}]}

    title_path = first_existing(root, TITLE_NAMES, title_name)
    body_path = first_existing(root, BODY_NAMES, body_name)
    title = title_path.read_text(encoding="utf-8").strip() if title_path else ""
    body = body_path.read_text(encoding="utf-8").strip() if body_path else ""
    if not title_path:
        add("error", "title_missing", "title.txt/标题.txt not found")
    if not body_path:
        add("error", "body_missing", "body.txt/正文.txt not found")

    max_title = int((config.get("content", {}).get("title", {}) or {}).get("max_chars", 20))
    if title:
        compact_title = re.sub(r"\s+", "", title)
        if len(compact_title) > max_title:
            add("error", "title_long", f"title is {len(compact_title)} chars; max {max_title}")
        for opening in (config.get("content", {}).get("title", {}) or {}).get("banned_openings", []) or []:
            if opening and opening in title:
                add("warning", "title_opening", f"title uses discouraged opening: {opening}")
    if not body:
        add("error", "body_empty", "body is empty")

    if kind == "image":
        imgs = sorted(p for p in root.rglob("*") if p.suffix.lower() in IMAGE_EXTS)
        # Ignore obvious preview/contact-sheet files for the page count.
        page_imgs = [p for p in imgs if not any(x in p.name.lower() for x in ("contact", "preview", "cover", "预览"))]
        target = (config.get("content", {}).get("image", {}) or {}).get("cards_target", [6, 8])
        hard_max = int((config.get("content", {}).get("image", {}) or {}).get("cards_hard_max", 10))
        if len(page_imgs) < int(target[0]):
            add("error", "images_few", f"found {len(page_imgs)} page images; target min {target[0]}")
        elif len(page_imgs) > hard_max:
            add("error", "images_many", f"found {len(page_imgs)} page images; hard max {hard_max}")
        elif len(page_imgs) > int(target[1]):
            add("warning", "images_over_target", f"found {len(page_imgs)} page images; target max {target[1]}")
        issues.extend(validate_visual_review(root, page_imgs, title, config))
    elif kind == "video":
        videos = [p for p in root.rglob("*") if p.suffix.lower() in VIDEO_EXTS]
        if not videos:
            add("error", "video_missing", "no .mp4/.mov video found")
        elif len(videos) > 1:
            add("warning", "video_multiple", f"found {len(videos)} video files; confirm the intended one")
    else:
        add("error", "kind", f"unsupported kind: {kind}")

    compliance = config.get("compliance", {}) or {}
    combined = f"{title}\n{body}"
    for term in compliance.get("banned_terms", []) or []:
        if term and term in combined:
            add("error", "banned_term", f"banned term found: {term}")
    for pattern in compliance.get("banned_patterns", []) or []:
        try:
            if re.search(pattern, combined, flags=re.I):
                add("error", "banned_pattern", f"forbidden pattern found: {pattern}")
        except re.error:
            if pattern in combined:
                add("error", "banned_pattern", f"forbidden text found: {pattern}")

    url_or_contact = [
        r"https?://", r"www\.", r"微信", r"\bVX\b|\bvx\b", r"\bQQ\b",
        r"加我", r"私信我", r"扫码", r"二维码", r"1[3-9]\d{9}",
    ]
    for pattern in url_or_contact:
        if re.search(pattern, combined, flags=re.I):
            add("error", "external_contact", f"external link/contact pattern found: {pattern}")

    # 配置了 off_platform_names 时：具体线上渠道 / 其他平台名称一律替换为中性表述（默认「官网入口」）。
    # title+body 之外，卡片文字在 cards.json、视频口播在 video_body.txt，也要一起扫。
    off_names = compliance.get("off_platform_names", []) or []
    replacement = compliance.get("off_platform_replacement", "官网入口")
    if off_names:
        for fname in ("title.txt", "body.txt", "cards.json", "video_body.txt", "script.txt"):
            f = root / fname
            if not f.is_file():
                continue
            txt = f.read_text(encoding="utf-8", errors="ignore")
            for pattern in off_names:
                try:
                    m = re.search(pattern, txt, flags=re.I)
                except re.error:
                    m = re.search(re.escape(pattern), txt)
                if m:
                    add("error", "off_platform_name",
                        f"{fname}: concrete outside-platform/channel name "
                        f"'{m.group(0)}' found; replace with {replacement}")
                    break

    tags = re.findall(r"#[^\s#]+", body)
    if not tags:
        add("warning", "tags_missing", "no hashtag found in body")
    elif len(tags) > 10:
        add("warning", "tags_many", f"{len(tags)} hashtags; confirm they are not keyword stuffing")

    title_numbers = sorted(set(re.findall(r"\d+(?:\.\d+)?%?", title)))
    body_numbers = set(re.findall(r"\d+(?:\.\d+)?%?", body))
    missing_numbers = [n for n in title_numbers if n not in body_numbers]
    if missing_numbers:
        add("warning", "title_body_numbers", f"numbers in title not found in body: {missing_numbers}")

    sources = [p for p in root.iterdir() if p.name.lower().startswith(("source", "来源", "政策", "policy"))]
    all_numbers = re.findall(r"\d+(?:\.\d+)?%?", body)
    if compliance.get("source_required_for_numbers", True) and all_numbers and not sources:
        add("warning", "sources_missing", "numbers found but no sources/policy file in package")

    # A rough title/body topic signal based on overlapping Chinese bigrams.
    def bigrams(text: str) -> set[str]:
        clean = re.sub(r"[^\u4e00-\u9fff]", "", text)
        stop = {"这个", "那个", "什么", "怎么", "就是", "不是", "可以", "自己", "很多"}
        return {clean[i:i + 2] for i in range(len(clean) - 1)} - stop

    title_terms = bigrams(title)
    body_terms = bigrams(body)
    overlap = title_terms & body_terms
    if title_terms and len(overlap) / len(title_terms) < 0.25:
        add("warning", "title_body_topic", "title and body share few topic words; verify the title promise is fulfilled")

    errors = [x for x in issues if x["level"] == "error"]
    warnings = [x for x in issues if x["level"] == "warning"]
    return {
        "ok": not errors,
        "package": str(root.resolve()),
        "kind": kind,
        "title": title,
        "title_length": len(re.sub(r"\s+", "", title)),
        "body_length": len(body),
        "tag_count": len(tags),
        "files": {
            "title": str(title_path) if title_path else None,
            "body": str(body_path) if body_path else None,
        },
        "errors": errors,
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Check a publish bundle for obvious blockers.")
    parser.add_argument("package")
    parser.add_argument("--config", default="xiaohongshu-ops.yaml")
    parser.add_argument("--kind", required=True, choices=("image", "video"))
    parser.add_argument("--title-file")
    parser.add_argument("--body-file")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    config = load_yaml(args.config)
    errors = [x for x in validate_config(config) if x["level"] == "error"]
    if errors:
        for err in errors:
            print(f"ERROR [{err['code']}] {err['message']}", file=sys.stderr)
        return 2

    try:
        report = check_package(pathlib.Path(args.package), config, args.kind, args.title_file, args.body_file)
    except SkillError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"package: {report['package']}")
        print(f"kind: {report['kind']} | title {report.get('title_length', 0)} chars | body {report.get('body_length', 0)} chars | tags {report.get('tag_count', 0)}")
        for item in report["errors"]:
            print(f"ERROR [{item['code']}] {item['message']}")
        for item in report["warnings"]:
            print(f"WARN  [{item['code']}] {item['message']}")
        print("PASS" if report["ok"] else "FAIL")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
