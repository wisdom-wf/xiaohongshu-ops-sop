# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from _common import (  # noqa: E402
    contains_question,
    in_windows,
    load_yaml,
    parse_windows,
    scheduling_windows,
    validate_config,
)
from analyze_metrics import build_report, to_markdown  # noqa: E402
from check_publish_package import check_package  # noqa: E402
from lint_comments import lint_batch  # noqa: E402
from outreach_ledger import status_for, validate_success_record, window_tag  # noqa: E402
from plan_outreach import make_plan, rolling_window_max  # noqa: E402


def write_visual_review(root: pathlib.Path, title: str, page_images: list[str], **overrides) -> None:
    review = {
        "title": title,
        "page_count": len(page_images),
        "page_images": page_images,
        "canvas": {"width": 1080, "height": 1440},
        "preview_width_px": 270,
        "preview_reviewed": True,
        "contact_sheet_reviewed": True,
        "cover_promise_sentence": "孩子体检报告这6项异常别拖到明年",
        "first_read": {
            "audience": "普通家庭",
            "scenario": "遇到急事",
            "action": "去乡镇民政窗口申请临时救助",
            "outcome": "两类情形能当场分清",
        },
        "typography": {
            "key_text_px": 44,
            "support_text_px": 34,
            "source_text_px": 28,
            "normal_contrast_ratio": 7.0,
            "large_contrast_ratio": 4.5,
        },
        "numeric_claims": [
            {
                "token": "6",
                "unit": "项",
                "independent_units": ["项目1", "项目2", "项目3", "项目4", "项目5", "项目6"],
                "fulfilled": True,
            }
        ],
        "checks": {f"V{i:02d}": True for i in range(1, 11)},
        "reviewer_notes": "已检查 270px 首图和 6 页联系表，正文、出处和数字承诺均可读。",
        "decision": "pass",
    }
    review.update(overrides)
    (root / "visual-review.json").write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")


class XiaohongshuOpsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_yaml(ROOT / "assets" / "config.example.yaml")

    def test_example_config_is_valid(self):
        errors = [x for x in validate_config(self.config) if x["level"] == "error"]
        self.assertEqual(errors, [])

    def test_outreach_plan_is_15_plus_15_and_spaced(self):
        plan = make_plan(self.config, "2026-10-22", 20261022)
        self.assertEqual(plan["total"], 30)
        self.assertEqual(plan["pools"], {"same_topic": 15, "viral": 15})
        self.assertEqual(len(plan["items"]), 30)
        minutes = [int(x["time"][:2]) * 60 + int(x["time"][3:]) for x in plan["items"]]
        self.assertEqual(minutes, sorted(minutes))
        self.assertTrue(all(b - a >= 10 for a, b in zip(minutes, minutes[1:])))
        self.assertLessEqual(rolling_window_max(minutes), 3)
        self.assertEqual(plan["spacing"]["observed_max_per_rolling_60m"], rolling_window_max(minutes))
        kinds = [x["kind"] for x in plan["items"]]
        self.assertTrue(all(a != b for a, b in zip(kinds, kinds[1:])))
        self.assertLessEqual(plan["question_ratio"], 0.20)
        question_flags = [x["needs_question"] for x in plan["items"]]
        self.assertTrue(all(not (a and b) for a, b in zip(question_flags, question_flags[1:])))
        self.assertEqual(sum(1 for x in plan["items"] if x["kind"] == "same_topic" and x["needs_question"]), 3)
        self.assertEqual(sum(1 for x in plan["items"] if x["kind"] == "viral" and x["needs_question"]), 2)
        schedule = self.config["engagement"]["outreach"]["scheduling"]
        active = parse_windows(scheduling_windows(schedule))
        peaks = parse_windows(schedule.get("peak_windows") or [])
        quiet = parse_windows(schedule.get("quiet_window") or [])
        minutes = [int(x["time"][:2]) * 60 + int(x["time"][3:]) for x in plan["items"]]
        # 每个计划位都在活跃窗口内（含晚间高峰与深夜尾峰），且不落静默窗口
        self.assertTrue(all(in_windows(m, active) for m in minutes), minutes)
        self.assertTrue(all(not in_windows(m, quiet) for m in minutes), minutes)
        # 晚间高峰优先：计划位占比必须达标（可执行检查，防止规则只写在文档里）
        peak_hits = sum(1 for m in minutes if in_windows(m, peaks))
        self.assertGreaterEqual(peak_hits / plan["total"], 0.55, peak_hits)
        self.assertEqual(plan["spacing"]["peak_hits"], peak_hits)

    def test_daily_mix_ceiling_rejects_extra_or_over_max_composition(self):
        config = deepcopy(self.config)
        config["content"]["daily_mix_ceiling"] = {"video": 1, "image": 1, "other": 1}
        errors = [x for x in validate_config(config) if x["level"] == "error"]
        self.assertTrue(any(x["code"] == "daily_mix" for x in errors), errors)

        config = deepcopy(self.config)
        config["content"]["daily_mix_ceiling"] = {"video": 3, "image": 0}
        errors = [x for x in validate_config(config) if x["level"] == "error"]
        self.assertTrue(any(x["code"] == "daily_mix" for x in errors), errors)

    def test_automation_must_stay_manual_only_and_explicitly_approved(self):
        config = deepcopy(self.config)
        del config["automation"]
        errors = [x for x in validate_config(config) if x["level"] == "error"]
        self.assertTrue(any(x["code"] == "required" for x in errors), errors)

        config = deepcopy(self.config)
        config["automation"]["mode"] = "scheduled"
        errors = [x for x in validate_config(config) if x["level"] == "error"]
        self.assertTrue(any(x["code"] == "automation_mode" for x in errors), errors)

    def test_tab_lifecycle_is_required_and_cannot_leave_residue(self):
        config = deepcopy(self.config)
        del config["browser"]["tab_lifecycle"]
        errors = [x for x in validate_config(config) if x["level"] == "error"]
        self.assertTrue(any(x["code"] == "required" for x in errors), errors)

        config = deepcopy(self.config)
        config["browser"]["tab_lifecycle"]["max_residual_task_tabs"] = 3
        errors = [x for x in validate_config(config) if x["level"] == "error"]
        self.assertTrue(any(x["code"] == "tab_lifecycle" for x in errors), errors)

    def test_fixed_comment_polling_schedule_is_rejected(self):
        config = deepcopy(self.config)
        config["engagement"]["own_comments"]["regular_checks"] = ["12:30", "18:30", "22:30"]
        errors = [x for x in validate_config(config) if x["level"] == "error"]
        self.assertTrue(any(x["code"] == "own_comment_schedule" for x in errors), errors)

    def test_comments_pass_when_specific_and_human(self):
        comments = [
            {
                "kind": "same_topic",
                "style": "specific_praise",
                "post_text": "职工医保和居民医保的差别，不只看每个月交多少钱，还要看退休年限和大病保障。",
                "text": "你这个角度很实在，尤其是退休年限那段，我也一直关注。",
            },
            {
                "kind": "viral",
                "style": "shared_resonance",
                "post_text": "真正拉开差距的，不是某一次选择，而是长期把一件小事做稳。",
                "text": "看到这段很有共鸣，长期把一件小事做稳比一时冲劲重要。",
            },
        ]
        report = lint_batch(comments, self.config)
        self.assertTrue(report["summary"]["ok"], report)
        self.assertEqual(report["summary"]["fail"], 0)
        self.assertEqual(report["summary"]["question_count"], 0)

    def test_comments_fail_without_context_specificity_or_same_frequency(self):
        comments = [
            {
                "kind": "same_topic",
                "post_text": "退休年限和大病保障都要一起看。",
                "text": "写得很实在，也说到我心里了。",
            },
            {
                "kind": "viral",
                "post_text": "真正拉开差距的是长期把一件小事做稳。",
                "text": "长期把一件小事做稳确实重要。",
            },
            {
                "kind": "viral",
                "text": "长期把一件小事做稳确实重要，我也很有共鸣。",
            },
        ]
        report = lint_batch(comments, self.config)
        codes = {c["code"] for r in report["results"] for c in r["checks"] if c["level"] == "fail"}
        self.assertIn("weak_specificity", codes)
        self.assertIn("no_frequency", codes)
        self.assertIn("no_context", codes)
        self.assertFalse(report["summary"]["ok"])

    def test_question_detection_ignores_non_question_me(self):
        self.assertFalse(contains_question("这么实在的内容，我也是同感。"))
        self.assertTrue(contains_question("这个细节你当时怎么处理的？"))

    def test_question_must_be_last_and_at_most_one(self):
        comments = [
            {
                "kind": "same_topic",
                "post_text": "退休年限和大病保障都要一起看。",
                "text": "退休年限这段讲得实在，你当时怎么核对？我也一直在关注。",
            },
            {
                "kind": "viral",
                "post_text": "真正拉开差距的是长期把一件小事做稳。",
                "text": "长期把一件小事做稳这段很有共鸣，你从哪年开始的？后来坚持了多久？",
            },
        ]
        report = lint_batch(comments, self.config)
        codes = {c["code"] for r in report["results"] for c in r["checks"] if c["level"] == "fail"}
        self.assertIn("question_not_last", codes)
        self.assertIn("multiple_questions", codes)
        self.assertFalse(report["summary"]["ok"])

    def test_consecutive_questions_fail_batch(self):
        comments = [
            {
                "kind": "same_topic",
                "post_text": "退休年限和大病保障都要一起看。",
                "text": "退休年限这段讲得实在，我也一直在关注，你当时怎么核对？",
            },
            {
                "kind": "viral",
                "post_text": "真正拉开差距的是长期把一件小事做稳。",
                "text": "长期把一件小事做稳这段很有共鸣，你从哪年开始的？",
            },
        ]
        report = lint_batch(comments, self.config)
        self.assertTrue(any(x["code"] == "consecutive_questions" for x in report["aggregate_issues"]), report)

    def test_comments_fail_for_link_and_repeated_generic_opening(self):
        comments = [
            {"kind": "viral", "text": "说得好，看我主页"},
            {"kind": "viral", "text": "说得好，学习了"},
        ]
        report = lint_batch(comments, self.config)
        self.assertFalse(report["summary"]["ok"])
        self.assertGreaterEqual(report["summary"]["fail"], 1)

    def test_publish_package_checks_images_and_title(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "title.txt").write_text("孩子体检报告，这6项异常别拖到明年", encoding="utf-8")
            (root / "body.txt").write_text(
                "孩子体检报告别只看结论。这6项异常别拖到明年，先找医生复核。\n\n#儿童体检 #体检报告",
                encoding="utf-8",
            )
            (root / "sources.md").write_text("来源核验", encoding="utf-8")
            for i in range(6):
                (root / "images").mkdir(exist_ok=True)
                (root / "images" / f"0{i}.png").write_bytes(b"png")
            write_visual_review(
                root,
                "孩子体检报告，这6项异常别拖到明年",
                [f"images/0{i}.png" for i in range(6)],
            )
            report = check_package(root, self.config, "image", None, None)
            self.assertTrue(report["ok"], report)

    def test_publish_package_requires_visual_review(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            title = "孩子体检报告，这6项异常别拖到明年"
            (root / "title.txt").write_text(title, encoding="utf-8")
            (root / "body.txt").write_text(
                "孩子体检报告别只看结论。这6项异常别拖到明年，先找医生复核。\n\n#儿童体检 #体检报告",
                encoding="utf-8",
            )
            (root / "sources.md").write_text("来源核验", encoding="utf-8")
            (root / "images").mkdir()
            for i in range(6):
                (root / "images" / f"0{i}.png").write_bytes(b"png")
            report = check_package(root, self.config, "image", None, None)
            self.assertFalse(report["ok"])
            self.assertTrue(any(x["code"] == "visual_review_missing" for x in report["errors"]), report)

    def test_visual_review_blocks_phone_scale_or_numeric_promise_failures(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            title = "急事家庭，这2笔救助先申请"
            (root / "title.txt").write_text(title, encoding="utf-8")
            (root / "body.txt").write_text(
                "急事家庭先看两类情形。这2笔救助的入口相同，条件和程序不一样。\n\n#临时救助 #民政",
                encoding="utf-8",
            )
            (root / "sources.md").write_text("来源核验", encoding="utf-8")
            (root / "images").mkdir()
            images = []
            for i in range(6):
                path = root / "images" / f"0{i}.png"
                path.write_bytes(b"png")
                images.append(f"images/0{i}.png")
            write_visual_review(
                root,
                title,
                images,
                numeric_claims=[
                    {
                        "token": "2",
                        "unit": "笔",
                        "independent_units": ["急难型与支出型合并为同一入口"],
                        "fulfilled": True,
                    }
                ],
            )
            report = check_package(root, self.config, "image", None, None)
            self.assertFalse(report["ok"])
            self.assertTrue(any(x["code"] == "visual_numeric_claim" for x in report["errors"]), report)

    def test_metrics_report_has_14_dimensions(self):
        data = json.loads((ROOT / "assets" / "templates" / "metrics-input.example.json").read_text(encoding="utf-8"))
        report = build_report(data, self.config)
        self.assertEqual(len(report["dimensions"]), 14)
        self.assertTrue(report["actions"])
        md = to_markdown(report)
        self.assertIn("14 维诊断", md)
        self.assertIn("观看 Top 10", md)

    def test_ledger_status_counts_success_only(self):
        rows = [
            {"date": "2026-10-22", "result": "success", "kind": "same_topic", "style": "specific_praise", "has_question": False},
            {"date": "2026-10-22", "result": "success", "kind": "viral", "style": "shared_resonance", "has_question": False},
            {"date": "2026-10-22", "result": "failed", "kind": "viral", "style": "gentle_question", "has_question": True},
        ]
        status = status_for(self.config, rows, "2026-10-22")
        self.assertEqual(status["total"], 2)
        self.assertEqual(status["by_kind"]["same_topic"], 1)
        self.assertEqual(status["by_kind"]["viral"], 1)
        self.assertEqual(status["daily_ceiling_basis"], "planned_ceiling")
        self.assertEqual(status["remaining_capacity"], 28)

    def test_ledger_blocks_outside_window_gap_and_round_overflow(self):
        tz = ZoneInfo("Asia/Shanghai")
        at = datetime(2026, 10, 25, 8, 0, tzinfo=tz)
        errors = validate_success_record(
            self.config, [], at, "same_topic", "具体内容很实在，我也同频。", "r1", "u1", "https://example/1"
        )
        self.assertTrue(any("outside" in x for x in errors), errors)

        rows = [{
            "at": "2026-10-25T09:50:00+08:00",
            "date": "2026-10-25",
            "round_id": "r1",
            "kind": "same_topic",
            "author_id": "u-old",
            "note_url": "https://example/old",
            "text": "旧评论内容实在，我也同频。",
            "has_question": False,
            "result": "success",
        }]
        errors = validate_success_record(
            self.config,
            rows,
            datetime(2026, 10, 25, 9, 55, tzinfo=tz),
            "viral",
            "新的具体内容很实在，我也同频。",
            "r1",
            "u2",
            "https://example/2",
        )
        self.assertTrue(any("minimum gap" in x for x in errors), errors)

        rows = [
            {
                "at": "2026-10-25T10:00:00+08:00",
                "date": "2026-10-25",
                "round_id": "round-full",
                "kind": "same_topic",
                "author_id": "u1",
                "note_url": "https://example/1",
                "text": "第一条具体内容，我也同频。",
                "has_question": False,
                "result": "success",
            },
            {
                "at": "2026-10-25T10:20:00+08:00",
                "date": "2026-10-25",
                "round_id": "round-full",
                "kind": "viral",
                "author_id": "u2",
                "note_url": "https://example/2",
                "text": "第二条具体内容，我也同频。",
                "has_question": False,
                "result": "success",
            },
        ]
        errors = validate_success_record(
            self.config,
            rows,
            datetime(2026, 10, 25, 10, 40, tzinfo=tz),
            "viral",
            "第三条具体内容，我也同频。",
            "round-full",
            "u3",
            "https://example/3",
        )
        self.assertTrue(any("max is 2" in x for x in errors), errors)

    def test_evening_peak_and_night_tail_are_allowed(self):
        """晚间与跨零点尾峰不判窗口外；静默段仍硬拦。"""
        tz = ZoneInfo("Asia/Shanghai")
        schedule = self.config["engagement"]["outreach"]["scheduling"]
        for hh, mm, allowed in [
            (0, 30, True), (1, 45, True), (9, 45, True), (18, 20, True), (23, 45, True),
            (2, 30, False), (8, 0, False), (9, 0, False),
        ]:
            at = datetime(2026, 11, 12, hh, mm, tzinfo=tz)
            errors = validate_success_record(
                self.config, [], at, "same_topic", "这段讲得实在，我也同频。",
                "r-night", "u-night", "https://example/night",
            )
            outside = [x for x in errors if "outside" in x]
            self.assertEqual(not outside, allowed, (hh, mm, errors))
        self.assertEqual(window_tag(datetime(2026, 11, 12, 0, 30, tzinfo=tz), schedule), "peak")
        self.assertEqual(window_tag(datetime(2026, 11, 12, 19, 30, tzinfo=tz), schedule), "peak")
        self.assertEqual(window_tag(datetime(2026, 11, 12, 11, 0, tzinfo=tz), schedule), "regular")
        self.assertEqual(window_tag(datetime(2026, 11, 12, 8, 0, tzinfo=tz), schedule), "off_window")

    def test_ledger_blocks_question_position_and_multiple_questions(self):
        tz = ZoneInfo("Asia/Shanghai")
        at = datetime(2026, 10, 25, 10, 0, tzinfo=tz)
        errors = validate_success_record(
            self.config,
            [],
            at,
            "same_topic",
            "退休年限这段讲得实在，你当时怎么核对？我也一直在关注。",
            "r-position",
            "u-position",
            "https://example/position",
        )
        self.assertTrue(any("final sentence" in x for x in errors), errors)

        errors = validate_success_record(
            self.config,
            [],
            at,
            "same_topic",
            "退休年限这段讲得实在，你当时怎么核对？后来补缴了吗？",
            "r-multiple",
            "u-multiple",
            "https://example/multiple",
        )
        self.assertTrue(any("at most one" in x for x in errors), errors)

    def test_ledger_blocks_question_cap_and_repeated_opening(self):
        tz = ZoneInfo("Asia/Shanghai")
        rows = []
        for i in range(6):
            rows.append({
                "at": f"2026-10-25T10:{i * 10:02d}:00+08:00",
                "date": "2026-10-25",
                "round_id": f"r{i}",
                "kind": "same_topic",
                "author_id": f"u{i}",
                "note_url": f"https://example/{i}",
                "text": ("社保这段讲得实在，我也同频。" if i % 2 == 0 else f"补充一个细节{i}，我也认同。"),
                "has_question": i % 2 == 0,
                "result": "success",
            })
        errors = validate_success_record(
            self.config,
            rows,
            datetime(2026, 10, 25, 11, 10, tzinfo=tz),
            "same_topic",
            "养老这段讲得实在，我也一直在关注，你当时怎么核对的？",
            "r-new-q",
            "u-new",
            "https://example/new-q",
        )
        self.assertTrue(any("question ceiling" in x for x in errors), errors)

        rows = [{
            "at": "2026-10-25T10:00:00+08:00",
            "date": "2026-10-25",
            "round_id": "r-old",
            "kind": "viral",
            "author_id": "u-old",
            "note_url": "https://example/old",
            "text": "看到这段确实很有共鸣，我也一直在关注。",
            "result": "success",
        }]
        errors = validate_success_record(
            self.config,
            rows,
            datetime(2026, 10, 25, 10, 20, tzinfo=tz),
            "viral",
            "看到这段确实很实在，我也认同。",
            "r-new",
            "u-new",
            "https://example/new",
        )
        self.assertTrue(any("same comment opening" in x for x in errors), errors)


if __name__ == "__main__":
    unittest.main()
