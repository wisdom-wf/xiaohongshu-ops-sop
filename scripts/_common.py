# -*- coding: utf-8 -*-
"""Shared helpers for the xiaohongshu-ops skill."""
from __future__ import annotations

import json
import math
import pathlib
import re
from datetime import date, datetime, time
from typing import Any, Iterable
from zoneinfo import ZoneInfo

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML is required: python3 -m pip install pyyaml") from exc


class SkillError(RuntimeError):
    """A user-facing configuration or input error."""


def load_yaml(path: str | pathlib.Path) -> dict[str, Any]:
    p = pathlib.Path(path).expanduser().resolve()
    if not p.exists():
        raise SkillError(f"config not found: {p}")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise SkillError(f"invalid YAML in {p}: {exc}") from exc
    if not isinstance(data, dict):
        raise SkillError("config root must be a mapping")
    return data


def read_json(path: str | pathlib.Path) -> Any:
    p = pathlib.Path(path).expanduser()
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SkillError(f"file not found: {p}") from exc
    except json.JSONDecodeError as exc:
        raise SkillError(f"invalid JSON in {p}: {exc}") from exc


def write_json(path: str | pathlib.Path, data: Any) -> pathlib.Path:
    p = pathlib.Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


def write_text(path: str | pathlib.Path, text: str) -> pathlib.Path:
    p = pathlib.Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def nested(data: dict[str, Any], dotted: str, default: Any = None) -> Any:
    cur: Any = data
    for key in dotted.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def parse_hhmm(value: str) -> time:
    if not isinstance(value, str):
        raise SkillError(f"time must be HH:MM, got {value!r}")
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", value)
    if not m:
        raise SkillError(f"time must be HH:MM, got {value!r}")
    hh, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise SkillError(f"invalid time: {value!r}")
    return time(hh, mm)


def _window_minute(part: str) -> int:
    """Minute offset for one window endpoint; '24:00' is a valid END marker."""
    if part.strip() == "24:00":
        return 24 * 60
    t = parse_hhmm(part)
    return t.hour * 60 + t.minute


def parse_window(value: str) -> tuple[int, int]:
    """Return start/end minute offsets for a same-day HH:MM-HH:MM window.

    En/em dashes are accepted for documents pasted from Chinese notes.
    '24:00' is accepted as an end marker (= 1440) so a window can cover the
    whole evening without stopping at 23:59.

    Overnight windows (e.g. 09:30-02:00) are still rejected: daily outreach
    quotas are keyed on the calendar date, so a wrapped window would split one
    evening session across two quota days. Use `active_windows` (several
    same-day segments) instead.
    """
    normalized = str(value).replace("–", "-").replace("—", "-").strip()
    parts = [x.strip() for x in normalized.split("-")]
    if len(parts) != 2:
        raise SkillError(f"window must be HH:MM-HH:MM, got {value!r}")
    a, b = _window_minute(parts[0]), _window_minute(parts[1])
    if parts[0].strip() == "24:00":
        raise SkillError(f"24:00 is only valid as a window end: {value!r}")
    if b <= a:
        raise SkillError(f"window must end after it starts (same-day): {value!r}")
    return a, b


def parse_windows(values: str | Iterable[str] | None) -> list[tuple[int, int]]:
    """Parse several same-day windows into sorted, non-wrapping segments."""
    if not values:
        return []
    items = [values] if isinstance(values, str) else list(values)
    return sorted(parse_window(str(v)) for v in items)


def scheduling_windows(schedule: dict) -> list[str]:
    """Active outreach windows, with the legacy single `active_window` fallback.

    `active_windows` (list) is preferred over the legacy `active_window` (string) so the
    evening traffic peak can span midnight without a wrapped window.
    """
    wins = schedule.get("active_windows")
    if isinstance(wins, (list, tuple)) and wins:
        return [str(w) for w in wins]
    legacy = schedule.get("active_window")
    if legacy:
        return [str(legacy)]
    # 仅当旧配置两个键都没写时才回落到保守的默认单窗口。
    return ["09:30-23:30"]


def in_windows(minute: int, segments: list[tuple[int, int]]) -> bool:
    """Whether a minute-of-day offset falls inside any half-open segment."""
    return any(a <= minute < b for a, b in segments)


def minutes_to_hhmm(minutes: int) -> str:
    if not 0 <= minutes < 24 * 60:
        raise SkillError(f"minute offset outside one day: {minutes}")
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def today_in_timezone(tz_name: str) -> date:
    try:
        return datetime.now(ZoneInfo(tz_name)).date()
    except Exception as exc:
        raise SkillError(f"invalid timezone: {tz_name!r}") from exc


def iso_at(date_str: str, hhmm: str, tz_name: str) -> str:
    try:
        tz = ZoneInfo(tz_name)
        local = datetime.fromisoformat(f"{date_str}T{hhmm}:00").replace(tzinfo=tz)
    except Exception as exc:
        raise SkillError(f"invalid date/time/timezone: {date_str} {hhmm} {tz_name}") from exc
    return local.isoformat()


def safe_div(numerator: float | int | None, denominator: float | int | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    try:
        return float(numerator) / float(denominator)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def pct(value: float | int | None, digits: int = 1) -> str:
    if value is None:
        return "未取到"
    return f"{float(value) * 100:.{digits}f}%"


def fmt_num(value: float | int | None, digits: int = 2) -> str:
    if value is None:
        return "未取到"
    if isinstance(value, int):
        return f"{value:,}"
    return f"{value:,.{digits}f}"


def median(values: Iterable[float | int | None]) -> float | None:
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    if n % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2


def weighted_average(values: list[tuple[float | int | None, float | int | None]]) -> float | None:
    total_weight = 0.0
    total = 0.0
    for value, weight in values:
        if value is None or weight is None:
            continue
        try:
            w = float(weight)
            v = float(value)
        except (TypeError, ValueError):
            continue
        if w <= 0:
            continue
        total += v * w
        total_weight += w
    return total / total_weight if total_weight else None


def contains_question(text: str) -> bool:
    """Detect a real question without treating words such as 这么/那么 as questions."""
    return bool(
        re.search(
            r"[?？]|(?:吗|呢)(?:[，。！!、,.；;]|$)|"
            r"(?:为什么|什么|怎么|如何|哪(?:个|里|种|些|年|家|位)|多少|"
            r"多久|多长时间|"
            r"能不能|可不可以|是不是|是否|有没有|会不会)",
            text or "",
        )
    )


def sentence_parts(text: str) -> list[str]:
    """Split prose into non-empty sentence-like units for question checks."""
    return [
        p.strip()
        for p in re.findall(r"[^。！？!?]+[。！？!?]?", (text or "").strip())
        if p.strip()
    ]


def question_sentence_count(text: str) -> int:
    """Count sentence-like units containing at least one question."""
    return sum(1 for part in sentence_parts(text) if contains_question(part))


def question_is_last_sentence(text: str) -> bool:
    """Return true when the only/allowed question appears in the final sentence."""
    parts = sentence_parts(text)
    return bool(parts and contains_question(parts[-1]))


def count_sentences(text: str) -> int:
    parts = sentence_parts(text)
    return max(1, len(parts))


def normalize_comment(text: str) -> str:
    """Normalize spacing and punctuation for duplicate checks, not semantic rewriting."""
    return re.sub(r"[\s，。！？!?、,.；;：:（）()【】\[\]“”\"']+", "", text or "")


def opening_key(text: str, n: int = 5) -> str:
    return normalize_comment(text)[:n]


def longest_common_substring_len(a: str, b: str) -> int:
    a = re.sub(r"\s+", "", a or "")
    b = re.sub(r"\s+", "", b or "")
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for ch in a:
        cur = [0]
        for j, other in enumerate(b, 1):
            n = prev[j - 1] + 1 if ch == other else 0
            cur.append(n)
            if n > best:
                best = n
        prev = cur
    return best


def validate_config(config: dict[str, Any]) -> list[dict[str, str]]:
    """Return issues as {level, code, message}. level is error/warning."""
    issues: list[dict[str, str]] = []

    def issue(level: str, code: str, message: str) -> None:
        issues.append({"level": level, "code": code, "message": message})

    if config.get("schema_version") != 1:
        issue("error", "schema_version", "schema_version must be 1")

    required = [
        "account.name",
        "account.timezone",
        "automation.mode",
        "content.daily_max_notes",
        "content.daily_mix_ceiling",
        "content.image.visual_gate",
        "publishing",
        "analytics",
        "engagement.own_comments",
        "engagement.outreach",
        "compliance",
        "workspace",
        "browser.tab_lifecycle",
    ]
    for key in required:
        if nested(config, key) in (None, "", {}):
            issue("error", "required", f"missing required field: {key}")

    cohort = nested(config, "analytics.cohort")
    if cohort not in (None, "all_published"):
        issue("error", "analytics_cohort", "analytics.cohort must be all_published; exclusions are not supported")

    automation = config.get("automation", {}) or {}
    if automation.get("mode") != "manual_only":
        issue("error", "automation_mode", "automation.mode must remain manual_only in this skill")
    if automation.get("create_or_resume_requires_explicit_approval") is not True:
        issue("error", "automation_approval", "automation create/resume must require explicit user approval")

    tz = nested(config, "account.timezone")
    if tz:
        try:
            ZoneInfo(tz)
        except Exception:
            issue("error", "timezone", f"invalid timezone: {tz}")

    daily_max = nested(config, "content.daily_max_notes")
    mix = nested(config, "content.daily_mix_ceiling", {}) or {}
    if not isinstance(mix, dict) or set(mix) != {"video", "image"}:
        issue("error", "daily_mix", "daily_mix_ceiling must contain exactly video and image")
    else:
        try:
            video_ceiling = int(mix["video"])
            image_ceiling = int(mix["image"])
            total_mix = video_ceiling + image_ceiling
            if video_ceiling < 0 or image_ceiling < 0:
                issue("error", "daily_mix", "daily_mix_ceiling values must be non-negative")
            if isinstance(daily_max, (int, float)) and total_mix > daily_max:
                issue("error", "daily_mix", "daily_mix_ceiling exceeds daily_max_notes")
        except (TypeError, ValueError):
            issue("error", "daily_mix", "daily_mix_ceiling values must be integers")

    ratios = [float(x.get("ratio", 0)) for x in nested(config, "content.pillars", []) or [] if isinstance(x, dict)]
    if ratios and not math.isclose(sum(ratios), 1.0, abs_tol=0.011):
        issue("warning", "pillar_ratio", f"content pillar ratios sum to {sum(ratios):.3f}, expected 1.0")

    visual_gate = nested(config, "content.image.visual_gate", {}) or {}
    if visual_gate.get("required") is not True:
        issue("error", "visual_gate", "content.image.visual_gate.required must be true")
    if not isinstance(visual_gate.get("review_file"), str) or not visual_gate.get("review_file", "").strip():
        issue("error", "visual_gate", "visual gate review_file must be a non-empty filename")
    try:
        if int(visual_gate.get("preview_width_px", 0)) < 270:
            issue("error", "visual_gate", "visual gate preview_width_px must be at least 270")
    except (TypeError, ValueError):
        issue("error", "visual_gate", "visual gate preview_width_px must be an integer")
    required_checks = visual_gate.get("required_checks")
    expected_checks = [f"V{i:02d}" for i in range(1, 11)]
    if required_checks != expected_checks:
        issue("error", "visual_gate", "visual gate required_checks must contain V01 through V10 in order")

    for kind in ("image_windows", "video_windows"):
        for window in nested(config, f"publishing.{kind}", []) or []:
            try:
                parse_window(window)
            except SkillError as exc:
                issue("error", "publish_window", str(exc))

    tab_lifecycle = nested(config, "browser.tab_lifecycle", {}) or {}
    for key in ("required", "close_on_success", "close_on_error_or_stop", "preserve_unowned_tabs"):
        if tab_lifecycle.get(key) is not True:
            issue("error", "tab_lifecycle", f"browser.tab_lifecycle.{key} must be true")
    if tab_lifecycle.get("max_residual_task_tabs") != 0:
        issue("error", "tab_lifecycle", "browser.tab_lifecycle.max_residual_task_tabs must be 0")
    try:
        if int(tab_lifecycle.get("max_candidate_tabs", 99)) > 5:
            issue("error", "tab_lifecycle", "browser.tab_lifecycle.max_candidate_tabs must be at most 5")
    except (TypeError, ValueError):
        issue("error", "tab_lifecycle", "browser.tab_lifecycle.max_candidate_tabs must be an integer")

    outreach = nested(config, "engagement.outreach", {}) or {}
    total = outreach.get("daily_total")
    pools = outreach.get("pools", {}) or {}
    same = pools.get("same_topic")
    viral = pools.get("viral")
    if not all(isinstance(x, int) and x >= 0 for x in (total, same, viral)):
        issue("error", "outreach_total", "outreach daily_total and pool targets must be non-negative integers")
    elif same + viral != total:
        issue("error", "outreach_total", f"same_topic({same}) + viral({viral}) != daily_total({total})")

    schedule = outreach.get("scheduling", {}) or {}
    try:
        # 窗口为同日多段（active_windows），兼容旧的单段 active_window。
        # 单段跨日窗口仍被 parse_window 拒绝，避免每日配额归属出现歧义。
        segments = parse_windows(scheduling_windows(schedule))
        for (a1, b1), (a2, b2) in zip(segments, segments[1:]):
            if a2 < b1:
                issue("error", "outreach_window", f"active windows overlap at {minutes_to_hhmm(a2)}")
        span = sum(b - a for a, b in segments)
        gap = int(schedule.get("min_gap_minutes", 0))
        if gap <= 0:
            issue("error", "outreach_gap", "min_gap_minutes must be positive")
        elif isinstance(total, int) and total > 1 and span < gap * (total - 1) + 1:
            issue("error", "outreach_window", "active windows are too short for the daily target and minimum gap")
        for peak in (schedule.get("peak_windows") or []):
            pa, pb = parse_window(peak)
            if not any(a <= pa and pb <= b for a, b in segments):
                issue("error", "outreach_peak_window", f"peak window {peak!r} is not inside an active window")
        peak_share = schedule.get("peak_share")
        if peak_share is not None and not 0 <= float(peak_share) <= 1:
            issue("error", "outreach_peak_share", "peak_share must be between 0 and 1")
        quiet = schedule.get("quiet_window")
        if quiet:
            qa, qb = parse_window(quiet)
            if any(qa < b and a < qb for a, b in segments):
                issue("error", "outreach_quiet_window", "quiet_window overlaps an active window")
    except (SkillError, TypeError, ValueError) as exc:
        issue("error", "outreach_window", str(exc))
    if int(schedule.get("max_per_rolling_60m", 0) or 0) <= 0:
        issue("error", "outreach_rolling_hour", "max_per_rolling_60m must be positive")
    max_round = schedule.get("max_success_per_round")
    if max_round is not None:
        try:
            if int(max_round) <= 0:
                issue("error", "outreach_round", "max_success_per_round must be positive")
            elif int(max_round) > int(schedule.get("max_per_rolling_60m", 0) or 0):
                issue("error", "outreach_round", "max_success_per_round cannot exceed max_per_rolling_60m")
        except (TypeError, ValueError):
            issue("error", "outreach_round", "max_success_per_round must be an integer")
    batch_open = schedule.get("batch_open_candidates")
    if batch_open is not None:
        if not isinstance(batch_open, list) or len(batch_open) != 2:
            issue("error", "batch_open", "batch_open_candidates must contain [min, max]")
        else:
            try:
                lo, hi = int(batch_open[0]), int(batch_open[1])
                if lo <= 0 or hi < lo:
                    issue("error", "batch_open", "batch_open_candidates must be positive and ordered")
            except (TypeError, ValueError):
                issue("error", "batch_open", "batch_open_candidates must be integers")
    if schedule.get("off_window_policy") not in (None, "skip_no_backfill"):
        issue("error", "outreach_window_policy", "off_window_policy must be skip_no_backfill")

    tone = outreach.get("tone", {}) or {}
    question_max = tone.get("question_max", {}) or {}
    if question_max:
        try:
            q_total = int(question_max.get("total", 5))
            q_same = int(question_max.get("same_topic", 3))
            q_viral = int(question_max.get("viral", 2))
            if min(q_total, q_same, q_viral) < 0:
                issue("error", "question_max", "question_max values must be non-negative")
            if q_total > total:
                issue("error", "question_max", "question_max.total cannot exceed daily_total")
            if q_same > int(pools.get("same_topic", 0) or 0):
                issue("error", "question_max", "question_max.same_topic exceeds the same-topic pool ceiling")
            if q_viral > int(pools.get("viral", 0) or 0):
                issue("error", "question_max", "question_max.viral exceeds the viral pool ceiling")
            if q_total > q_same + q_viral:
                issue("error", "question_max", "question_max.total cannot exceed the sum of pool caps")
        except (TypeError, ValueError):
            issue("error", "question_max", "question_max values must be integers")
    for name, value in (tone.get("question_ratio_target", {}) or {}).items():
        try:
            f = float(value)
            if not 0 <= f <= 0.5:
                issue("error", "question_ratio", f"{name} question ratio should be between 0 and 0.5")
        except (TypeError, ValueError):
            issue("error", "question_ratio", f"invalid question ratio for {name}")
    if "question_ratio_total_max" in tone:
        try:
            total_ratio = float(tone["question_ratio_total_max"])
            if not 0 <= total_ratio <= 0.5:
                issue("error", "question_ratio_total", "question_ratio_total_max should be between 0 and 0.5")
        except (TypeError, ValueError):
            issue("error", "question_ratio_total", "invalid question_ratio_total_max")
    for key in ("max_chars", "max_sentences"):
        if int(tone.get(key, 0) or 0) <= 0:
            issue("error", "tone_limit", f"tone.{key} must be positive")
    recommended = tone.get("recommended_chars")
    if recommended is not None:
        if not isinstance(recommended, list) or len(recommended) != 2:
            issue("error", "comment_length", "recommended_chars must contain [min, max]")
        else:
            try:
                lo, hi = int(recommended[0]), int(recommended[1])
                hard_max = int(tone.get("max_chars", 0) or 0)
                if lo <= 0 or hi < lo or hi > hard_max:
                    issue("error", "comment_length", "recommended_chars must be positive, ordered, and within max_chars")
            except (TypeError, ValueError):
                issue("error", "comment_length", "recommended_chars must be integers")

    own = nested(config, "engagement.own_comments", {}) or {}
    if own.get("regular_checks"):
        issue("error", "own_comment_schedule", "regular_checks must be empty; do not recreate fixed polling schedules")
    first_per_run = own.get("max_first_comments_per_run")
    if first_per_run is not None:
        try:
            if int(first_per_run) <= 0:
                issue("error", "own_comment_limit", "max_first_comments_per_run must be positive")
            if isinstance(daily_max, (int, float)) and int(first_per_run) > int(daily_max):
                issue("error", "own_comment_limit", "max_first_comments_per_run cannot exceed daily_max_notes; no historical zero-comment backfill")
        except (TypeError, ValueError):
            issue("error", "own_comment_limit", "max_first_comments_per_run must be an integer")

    direct_messages = nested(config, "engagement.direct_messages", {}) or {}
    if direct_messages.get("mode") != "read_only" or direct_messages.get("auto_reply") is not False:
        issue("error", "direct_messages", "direct messages must default to read_only with auto_reply=false")

    for name in ("video_cover_ctr", "image_cover_ctr", "video_completion_rate", "collect_rate", "share_rate"):
        value = nested(config, f"analytics.targets.{name}")
        if value is not None:
            try:
                if not 0 <= float(value) <= 1:
                    issue("warning", "analytics_target", f"{name} should be a 0-1 rate")
            except (TypeError, ValueError):
                issue("error", "analytics_target", f"{name} is not numeric")

    return issues
