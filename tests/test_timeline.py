# -*- coding: utf-8 -*-
"""时间线自洽校验的回归测试（任务 5）。真实案例来自实测简历里的「2024/4 -- 2023/12」。"""
from __future__ import annotations

import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.text.timeline import check_timeline, extract_spans, summarize  # noqa: E402


def _kinds(text, kind):
    return [i for i in check_timeline(text, today=date(2026, 9, 11)).issues if i.kind == kind]


def test_起止矛盾被抓到():
    text = "实习经历 2024/4 -- 2023/12 某公司 后端开发"
    assert _kinds(text, "reversed_range"), "2024/4 -- 2023/12 是实测出现过的起止矛盾"


def test_正常区间不误报():
    for text in ("实习经历 2023/12 -- 2024/4 某公司",
                 "教育经历 2021.9-2025.6 重庆文理学院 数字媒体技术",
                 "工作经历 2024.7 -- 至今 某公司",
                 "教育经历 2022.09-2026.06 示例大学 本科",
                 "实习经历 2024年7月 - 2024年9月 某公司"):
        r = check_timeline(text, today=date(2026, 9, 11))
        assert r.status == "ok", f"{text} 不该报问题：{summarize(r)}"


def test_日期解析不被年份吃掉():
    """2022.1-2023.6 这类写法，月份后不能把 '-20' 当成日。"""
    spans = extract_spans("工作经历 2022.1-2023.6 A公司 全职")
    assert len(spans) == 1
    assert spans[0].start == (2022, 1) and spans[0].end == (2023, 6), spans[0]


def test_同类型经历重叠():
    text = ("工作经历 2022.1-2024.6 A公司 全职\n"
            "工作经历 2023.5-2025.8 B公司 全职")
    hits = _kinds(text, "overlap")
    assert hits, "两份全职重叠 13 个月应当报重叠"
    assert "重叠" in hits[0].detail


def test_在校实习与教育重叠不算问题():
    text = ("教育经历 2021.9-2025.6 重庆文理学院 数字媒体技术\n"
            "实习经历 2024.7-2024.9 成都某公司")
    r = check_timeline(text, today=date(2026, 9, 11))
    assert not [i for i in r.issues if i.kind == "overlap"], "在校期间实习与学业重叠是正常的"


def test_经历空档():
    text = "工作经历 2020.1-2021.1 A公司\n工作经历 2023.6-2024.6 B公司"
    hits = _kinds(text, "gap")
    assert hits and "29" in hits[0].detail


def test_未来日期():
    text = "项目经历 2025.1 -- 2027.6 某项目"
    assert _kinds(text, "future_date")


def test_汇总文案():
    r = check_timeline("实习经历 2024/4 -- 2023/12 某公司", today=date(2026, 9, 11))
    assert "起止矛盾" in summarize(r)
    assert summarize(check_timeline("教育经历 2021.9-2025.6 大学", today=date(2026, 9, 11))) == "时间线自洽，未发现问题"
