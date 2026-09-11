# -*- coding: utf-8 -*-
"""解析质量门禁：烂文本不许进 LLM。

三层信号：
1. 机器门禁（可读字符数 / 段落结构 / 乱码字符数）——原型已验证有效，
2. 模型自报（识别到哪些段落、有无缺失）——进质量区，供人工排查，
3. 字段级与时间线校验——占位（任务 5 落地：起止矛盾、经历重叠、异常空档）。
"""
from __future__ import annotations

import re

from ..contracts.common import FieldAnomaly, Quality, QualityCheck, TimelineCheck

MIN_CHARS = 300
MIN_SECTIONS = 3
MAX_MOJIBAKE = 5
SECTION_HINTS = ("教育", "实习", "项目", "技能", "经历", "实践", "获奖")


def gate(text: str, pages: int) -> Quality:
    checks: list[QualityCheck] = []
    issues: list[str] = []

    n = len(text.strip())
    density = n / max(pages, 1)
    checks.append(QualityCheck(name="可读字符数", value=f"{n} 字 / {pages} 页（{density:.0f} 字/页）",
                               ok=n >= MIN_CHARS, hint="字符过少，可能是扫描件或图片型简历"))

    sections = [s for s in SECTION_HINTS if s in text]
    checks.append(QualityCheck(name="段落结构", value="命中段落标题：" + ("、".join(sections) or "无"),
                               ok=len(sections) >= MIN_SECTIONS, hint="结构信号不足，解析结果可能不完整"))

    bad = len(re.findall(r"[\ufffd\u25a1]", text))
    checks.append(QualityCheck(name="乱码字符", value=f"{bad} 个", ok=bad < MAX_MOJIBAKE,
                               hint="存在乱码，需重新抽取或转 OCR"))

    for c in checks:
        if not c.ok:
            issues.append(f"{c.name}：{c.hint}")

    return Quality(gate_passed=not issues, checks=checks, issues=issues,
                   field_anomalies=[], timeline=TimelineCheck(status="not_checked"))


def field_anomalies(text: str) -> list[FieldAnomaly]:
    """字段级异常检测：转交 app/text/fields.py（六类检查，逐字段体检，只提示不拦截）。"""
    from .fields import detect_anomalies

    return detect_anomalies(text)
