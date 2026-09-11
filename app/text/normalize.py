# -*- coding: utf-8 -*-
"""文本规范化：规范化文本 + 偏移映射表。

规范化做的事只有三件（都可逆查）：折叠空白、去首尾空白、丢控制字符。
故意**不做** NFKC：全角括号/数字一旦被折叠，报告里的"原文"就不再是原文，
证据高亮的可信度会打折。字符集不动，只想把 PDF/DOCX 抽取出来的换行、制表符、
连续空格这类噪声统一掉，让模型给的片段能对上。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

WS = re.compile(r"\s+")
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass
class Normalized:
    text: str                       # 规范化后的文本（报告/高亮/模型输入的坐标基准）
    rmap: list[int]                 # rmap[i] = 规范化文本第 i 个字符在 raw 中的下标
    raw: str                        # 抽取原文
    ops: list[str]

    # ------------------------------------------------------------ 偏移换算
    def raw_range(self, start: int, end: int) -> tuple[int, int]:
        """规范化坐标 [start,end) → raw 坐标 [raw_start, raw_end)。"""
        if not self.rmap:
            return (0, 0)
        start = max(0, min(start, len(self.rmap) - 1))
        end = max(start + 1, min(end, len(self.rmap)))
        return (self.rmap[start], self.rmap[end - 1] + 1)

    def raw_slice(self, start: int, end: int) -> str:
        a, b = self.raw_range(start, end)
        return self.raw[a:b]

    # ------------------------------------------------------------ 哈希
    @property
    def raw_sha256(self) -> str:
        return hashlib.sha256(self.raw.encode("utf-8")).hexdigest()

    @property
    def norm_sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def map_sha256(self) -> str:
        return hashlib.sha256(json.dumps(self.rmap, separators=(",", ":")).encode()).hexdigest()[:32]


def collapse_ws(s: str) -> str:
    """与 normalize 保持一致的空折叠（用于模型给的证据片段）。"""
    return WS.sub(" ", s).strip()


def normalize(raw: str) -> Normalized:
    raw = CONTROL.sub("", raw)
    chars: list[str] = []
    rmap: list[int] = []
    pending_ws = False
    for i, ch in enumerate(raw):
        if ch.isspace():
            if chars:                       # 丢掉开头空白
                pending_ws = True
            continue
        if pending_ws:
            pending_ws = False
            chars.append(" ")
            rmap.append(prev_i)
        chars.append(ch)
        rmap.append(i)
        prev_i = i
    # 去掉可能的尾部空格（pending_ws 未落地，天然不产生）
    text = "".join(chars)
    return Normalized(text=text, rmap=rmap, raw=raw, ops=["drop_control", "collapse_ws", "strip"])


def page_offsets(norm: Normalized, page_texts: list[str]) -> list[int]:
    """各页在规范化文本中的起始偏移：按页文本长度比例累计（页文本与规范化文本近似同比）。"""
    total_raw = sum(len(p) for p in page_texts) or 1
    offsets, acc = [], 0.0
    for p in page_texts:
        offsets.append(min(len(norm.text), int(acc * len(norm.text) / total_raw)))
        acc += len(p)
    return offsets
