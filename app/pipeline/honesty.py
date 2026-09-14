# -*- coding: utf-8 -*-
"""证据具体性 / 空话检测（离线可算，0 API 成本）。

为什么需要它：分数公式改不动「注水型简历排在踏实候选前面」这个倒挂——因为注水型的证据
**条数、相关度、模型给的难度三项全都更高**（术语多、模型看不出虚实）。唯一能治的是不看
「有多少条/多相关」，只看**每条证据说不说得清**：有没有数字、时间、机构、可核实的产出。

实测（300 份行政岗对照语料、3435 条证据片段）：
    档位        具体性S   含数字比例   空话率
    明显合格     0.407     0.553      0.107
    边缘合格     0.107     0.042      0.158
    注水型       0.074     0.000      0.787   ← 一个数字都没有
用「空话率」作扣分（上限 30）后四档排正：88.1 / 58.4 / 45.2（倒挂 −8.1 → +13.3）。

关键：汇总一律用**均值**，不能用求和/条数——注水型条目更多，求和等于奖励注水。
"""
from __future__ import annotations

import re
import statistics as st

# —— 四要素：数字 +2、时间 +1、机构名 +1、明确产出/结果 +2（每片段上限 6，归一 0~1）——
NUM = re.compile(r"\d|[一二三四五六七八九十百千万]{1,3}(余|多)?"
                 r"(人|名|场|次|份|项|个|天|月|年|篇|条|台|封|页|小时|周|元|万|人次)")
DATE = re.compile(r"(19|20)\d{2}\s*[.\-/年]|至今|个月|半年|每周|每天|每日|日均|每月|每季度|寒暑假|期间|实习期")
ORG = re.compile(r"(有限公司|公司|集团|银行|医院|事务所|大学|学院|学校|中学|小学|中心|协会|学生会|"
                 r"研究生会|社团|党支部|团委|局|研究院|部委)")
RESULT = re.compile(r"(完成|落地|上线|通过|获得|荣获|评为|获奖|提升|增长|下降|减少|节省|零差错|达成|超额|"
                    r"签约|签订|办理|录入|归档|报销|接待|统筹|组织|策划|整理出|编制|核对|统计出|服务|覆盖|支持)")

# —— 空话词：听起来有能力、但本身不提供可核实信息 ——
FLUFF_DEFAULT = re.compile(
    r"(能力强|较强|良好|优秀|熟悉|熟练|有耐心|责任心|团队协作|沟通协调|沟通表达|抗压|细致|认真|"
    r"积极主动|乐于助人|学习能力|适应能力|办公软件|综合素质|上进心)")

MIN_SPAN_CHARS = 6          # 太短的片段（栏目标题等）不参与统计


def span_concreteness(text: str) -> float:
    """单条证据的"说得清"程度，0~1。"""
    t = (text or "").strip()
    if len(t) < MIN_SPAN_CHARS:
        return 0.0
    v = 2 * bool(NUM.search(t)) + bool(DATE.search(t)) + bool(ORG.search(t)) + 2 * bool(RESULT.search(t))
    return min(v, 6.0) / 6.0


def is_fluff(text: str, fluff: re.Pattern = FLUFF_DEFAULT) -> bool:
    """空话片段 = 命中空话词 **且** 不含任何数字（有数字就得算它给了实据）。"""
    t = (text or "").strip()
    return bool(fluff.search(t)) and not NUM.search(t)


def resume_signals(spans: list[str], fluff: re.Pattern = FLUFF_DEFAULT) -> dict:
    """把一份简历的全部证据片段汇总成信号（全部用均值/比例，不用求和）。"""
    sp = [t.strip() for t in spans if len((t or "").strip()) >= MIN_SPAN_CHARS]
    if not sp:
        return {"n_spans": 0, "concreteness": 0.0, "fluff_rate": 1.0, "number_rate": 0.0}
    return {
        "n_spans": len(sp),
        "concreteness": st.mean([span_concreteness(t) for t in sp]),
        "fluff_rate": sum(1 for t in sp if is_fluff(t, fluff)) / len(sp),
        "number_rate": sum(1 for t in sp if NUM.search(t)) / len(sp),
    }


def honesty_penalty(signals: dict, *, cap: float = 30.0, signal: str = "fluff") -> float:
    """注水扣分（正数=要扣掉的分）。

    signal="fluff"（默认，实测最有效）：扣分 = cap × 空话率
    signal="concreteness"：扣分 = cap × (1 − 具体性)
    signal="sqrt_fluff"：扣分 = cap × √空话率（对中度空话更宽容）
    """
    if signal == "fluff":
        f = signals.get("fluff_rate", 1.0)
    elif signal == "concreteness":
        f = 1.0 - signals.get("concreteness", 0.0)
    elif signal == "sqrt_fluff":
        f = signals.get("fluff_rate", 1.0) ** 0.5
    else:
        raise ValueError(f"未知的注水信号：{signal}")
    return max(0.0, min(1.0, f)) * cap


def is_high_confidence_fluff(signals: dict, *, min_spans: int = 8, fluff_at: float = 0.8,
                             number_at: float = 0.0) -> bool:
    """高置信注水：可以直接淘汰的那一档（比"扣分"更狠，必须苛刻，否则误杀）。

    实测注水型：空话率 0.787、含数字比例 0.000。但浅层规则**只要塞进一个数字就能绕过**，
    所以这里要求同时满足：样本足够多 + 空话率极高 + 一个数字都没有。
    """
    return (signals.get("n_spans", 0) >= min_spans
            and signals.get("fluff_rate", 0.0) >= fluff_at
            and signals.get("number_rate", 0.0) <= number_at)
