# -*- coding: utf-8 -*-
"""时间线自洽校验（任务 5，纯规则、不调 LLM）。

真实简历里出现过「2024/4 -- 2023/12」这种起止矛盾，模型看不出来，规则一眼就能抓。
四类问题：
  reversed_range  起止矛盾（结束早于开始）
  overlap         同类型经历时间重叠（两份「全职/实习」各重叠半年以上）
  gap             连续经历之间空档超过阈值（默认 12 个月）
  future_date     写的结束时间在未来（除「至今」）
顺带把解析不出来的时间写法记为 unparsable，供质量门禁参考。

设计取舍：只报警不拦截。时间写法千奇百怪（"大三下""暑期"），规则做硬判定会误伤；
所以这里只产出 issues，由报告呈现给 HR，判定层不因为时间线问题自动淘汰。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

from ..contracts.common import TimelineCheck, TimelineIssue

# 支持：2024.4 / 2024-04 / 2024/4 / 2024年4月 / 2024 ；也支持 2024.4.15
# 月份/日期后加 (?!\d)：否则 "2022.1-2023.6" 里的 "-20" 会被当成日，把年份吃掉
DATE = re.compile(r"(?P<y>20\d{2})\s*[.\-/年]?\s*(?P<m>1[0-2]|0?[1-9])?(?!\d)(?:\s*[.\-/月]\s*(?P<d>3[01]|[12]\d|0?[1-9])(?!\d))?")
SEP_RE = re.compile(r"\s*(?:--|—|–|~|-|到|至|～)\s*")
PRESENT_RE = re.compile(r"\s*(?:--|—|–|~|-|到|至|～)\s*(至今|现在|今|present)", re.I)

KIND_RULES = [
    ("实习", ("实习",)),
    ("工作", ("工作", "在职", "任职", "全职", "公司", "科技有限公司", "有限", "岗位")),
    ("教育", ("大学", "学院", "本科", "专科", "大专", "硕士", "研究生", "学历", "在读", "就读")),
    ("项目", ("项目", "课题", "比赛", "竞赛", "大赛")),
]
GAP_MONTHS = 12          # 空档阈值
OVERLAP_MONTHS = 6       # 重叠阈值


@dataclass
class Span:
    kind: str
    start: tuple[int, int]          # (year, month)
    end: tuple[int, int] | None     # None = 至今
    raw: str
    line: str
    pos: int
    reversed_: bool = False


def _to_month(y: str, m: str | None) -> tuple[int, int]:
    return int(y), int(m) if m else 1


def _index(pairs: list[tuple[int, int]]) -> list[int]:
    return [y * 12 + (m - 1) for y, m in pairs]


def _kind_of(line: str) -> str:
    for kind, kws in KIND_RULES:
        if any(k in line for k in kws):
            return kind
    return "其他"


def extract_spans(text: str) -> list[Span]:
    """逐行找日期，配对成区间。逐行做的好处：不会把不同条目的日期配到一起。"""
    spans: list[Span] = []
    for line_m in re.finditer(r"[^\n]+", text):
        line = line_m.group(0).strip()
        matches = list(DATE.finditer(line))
        if not matches:
            continue
        kind = _kind_of(line)
        i = 0
        while i < len(matches):
            m = matches[i]
            a = _to_month(m.group("y"), m.group("m"))
            # ① 后面紧跟分隔符 + 另一个日期 → 一个区间
            if i + 1 < len(matches) and SEP_RE.fullmatch(line[m.end():matches[i + 1].start()]):
                nxt = matches[i + 1]
                b = _to_month(nxt.group("y"), nxt.group("m"))
                raw = line[m.start():nxt.end()]
                spans.append(Span(kind=kind, start=a, end=b, raw=raw, line=line, pos=line_m.start() + m.start(),
                                  reversed_=_index([a])[0] > _index([b])[0]))
                i += 2
                continue
            # ② 分隔符 + 至今
            pm = PRESENT_RE.match(line[m.end():])
            if pm:
                spans.append(Span(kind=kind, start=a, end=None, raw=line[m.start():m.end() + pm.end()],
                                  line=line, pos=line_m.start() + m.start()))
                i += 1
                continue
            # ③ 单个日期（可能只是"获奖时间"这类，仅供参考，不参与区间校验）
            spans.append(Span(kind=kind, start=a, end=a, raw=line[m.start():m.end()],
                              line=line, pos=line_m.start() + m.start()))
            i += 1
    return spans


def check_timeline(text: str, *, today: date | None = None, gap_months: int = GAP_MONTHS,
                  overlap_months: int = OVERLAP_MONTHS) -> TimelineCheck:
    today = today or datetime.now().date()
    now_idx = today.year * 12 + (today.month - 1)
    spans = extract_spans(text)
    issues: list[TimelineIssue] = []

    for s in spans:
        if s.reversed_:
            issues.append(TimelineIssue(kind="reversed_range",
                                        detail=f"起止矛盾：{s.raw}（{s.line[:40]}）"))
        if s.end and _index([s.end])[0] > now_idx:
            issues.append(TimelineIssue(kind="future_date",
                                        detail=f"结束时间在未来：{s.raw}（{s.line[:40]}）"))

    # 同类型经历：重叠
    for kind in {s.kind for s in spans}:
        group = sorted([s for s in spans if s.kind == kind and s.end and not s.reversed_],
                       key=lambda x: _index([x.start])[0])
        for i in range(len(group) - 1):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                a_start, a_end = _index([a.start])[0], _index([a.end])[0]
                b_start, b_end = _index([b.start])[0], _index([b.end])[0]
                ov = min(a_end, b_end) - max(a_start, b_start)
                if ov >= overlap_months:
                    issues.append(TimelineIssue(
                        kind="overlap",
                        detail=f"{kind}经历时间重叠约 {ov} 个月：{a.raw} 与 {b.raw}"))

    # 连续经历之间的大空档（只对工作/实习/教育看）
    career = sorted([s for s in spans if s.kind in ("工作", "实习", "教育") and s.end and not s.reversed_],
                    key=lambda x: _index([x.start])[0])
    for i in range(len(career) - 1):
        end_i = _index([career[i].end])[0]
        start_j = _index([career[i + 1].start])[0]
        if start_j - end_i > gap_months:
            issues.append(TimelineIssue(
                kind="gap",
                detail=f"经历空档约 {start_j - end_i} 个月：{career[i].raw} 之后到 {career[i + 1].raw}"))

    return TimelineCheck(status="issues" if issues else "ok", issues=issues)


def summarize(check: TimelineCheck) -> str:
    if check.status == "not_checked":
        return "未校验"
    if not check.issues:
        return "时间线自洽，未发现问题"
    from collections import Counter
    c = Counter(i.kind for i in check.issues)
    names = {"reversed_range": "起止矛盾", "overlap": "经历重叠", "gap": "经历空档",
             "future_date": "未来日期", "unparsable": "无法解析"}
    return "、".join(f"{names.get(k, k)} {v} 处" for k, v in c.items())
