# -*- coding: utf-8 -*-
"""字段级异常检测（任务 5 后半）：抽取出来的文本哪里坏了，报告要指出来。

为什么放在门禁之外：门禁只看"整份简历够不够干净"，而这里要回答"具体哪个字段不可信"——
HR 看到的往往是"姓名是乱码""职位名只剩半个字""电话只有 7 位"这类局部问题，
整份简历的字数、段落结构可能都正常。所以这一层是**逐字段的体检**，只提示不拦截。

六类检查（都可由人复现，不依赖模型）：
  1. 乱码行          替换符/方框字符所在的行
  2. 疑似职位名乱码  短行里非中文非 ASCII 字符占比过高（如 "PHP�开发工程师"）
  3. 联系方式可疑    电话位数不对、邮箱格式不合法
  4. 超长无标点行    一行几百字没有标点 → PDF 文字层破损的典型特征
  5. 重复行          同一行反复出现（页眉页脚、版式噪声）
  6. 疑似姓名乱码    姓名行里出现过半的字符或乱码
"""
from __future__ import annotations

import re

from ..contracts.common import FieldAnomaly

MOJIBAKE = re.compile(r"[\ufffd\u25a1\u25a0]")
PHONE = re.compile(r"(?<!\d)(1[3-9]\d{9}|0\d{2,3}-?\d{7,8})(?!\d)")
PHONE_LOOSE = re.compile(r"(?<!\d)\d{6,12}(?!\d)")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
NAME_HINT = re.compile(r"姓\s*名|姓名[：:]")
MAX_LINE = 200
DUP_THRESHOLD = 3


def _is_broken(line: str) -> bool:
    """非中文、非 ASCII 可打印字符占比过高 = 文字层坏了。"""
    if not line.strip():
        return False
    bad = sum(1 for ch in line if not (ch.isascii() and ch.isprintable()) and not ("\u4e00" <= ch <= "\u9fff"))
    return bad / max(1, len(line)) > 0.3


def detect_anomalies(text: str) -> list[FieldAnomaly]:
    out: list[FieldAnomaly] = []
    lines = text.split("\n")

    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            continue
        head = line[:40]

        if MOJIBAKE.search(line):
            out.append(FieldAnomaly(field=f"第{i + 1}行", detail=f"含乱码字符：{head}", severity="error"))
        elif len(line) <= 20 and _is_broken(line):
            out.append(FieldAnomaly(field=f"第{i + 1}行", detail=f"疑似字段损坏（字符集异常）：{head}", severity="error"))

        if len(line) > MAX_LINE and not re.search(r"[，。；：、,.!?;:]", line):
            out.append(FieldAnomaly(field=f"第{i + 1}行",
                                    detail=f"超长且无标点（{len(line)} 字），PDF 文字层可能断裂，证据坐标不可尽信",
                                    severity="warn"))

        if NAME_HINT.search(line):
            after = NAME_HINT.split(line, maxsplit=1)[-1].strip("：: ")
            # PDF 常把"姓名/性别/年龄/籍贯"挤在一行：只取姓名后的第一个词，遇到下一个字段名就截断
            candidate = re.split(r"[\s　]{2,}|性\s*别|年\s*龄|籍\s*贯|电\s*话|邮\s*箱|学\s*历", after, maxsplit=1)[0]
            candidate = re.sub(r"[\s　]+", "", candidate)
            if candidate and (_is_broken(candidate) or len(candidate) > 6):
                out.append(FieldAnomaly(field="姓名", detail=f"姓名字段可疑：{candidate[:20]}", severity="error"))

    # 联系方式
    phones = PHONE.findall(text)
    if not phones:
        loose = PHONE_LOOSE.findall(text)
        if loose:
            out.append(FieldAnomaly(field="联系电话",
                                    detail=f"未匹配到合法手机号/座机，但有 {len(loose)} 串 6~12 位数字（如 {loose[0]}），"
                                           f"可能位数缺失或格式被拆断", severity="warn"))
        else:
            out.append(FieldAnomaly(field="联系电话", detail="整份文本里没有可识别的联系电话", severity="warn"))
    emails = EMAIL.findall(text)
    for e in emails[:3]:
        if e.count(".") == 0 or len(e) > 40:
            out.append(FieldAnomaly(field="邮箱", detail=f"邮箱格式可疑：{e}", severity="warn"))

    # 重复行（页眉页脚/版式噪声）
    seen: dict[str, int] = {}
    for line in lines:
        s = line.strip()
        if 4 <= len(s) <= 40:
            seen[s] = seen.get(s, 0) + 1
    for line, n in sorted(seen.items(), key=lambda kv: -kv[1])[:3]:
        if n >= DUP_THRESHOLD:
            out.append(FieldAnomaly(field="重复行", detail=f"同一行出现 {n} 次：{line[:30]}", severity="warn"))

    return out


def summarize(anomalies: list[FieldAnomaly]) -> tuple[int, int]:
    """返回（error 数, warn 数）。"""
    errs = sum(1 for a in anomalies if a.severity == "error")
    return errs, len(anomalies) - errs
