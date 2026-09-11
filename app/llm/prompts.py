# -*- coding: utf-8 -*-
"""五段提示词（v1）。改 prompt 必须递增 PROMPT_VERSION —— 它和 prompt_hash 一起落库，分数才可复现。

与原型 score_resume.py 的差异：
- 输出字段与 schemas/llm/*.schema.json 一一对应（契约先行，prompt 跟着契约写）。
- 解析段明确要求"片段必须是原文连续片段"，并声明偏移由系统计算，禁止模型自报偏移。
- 判定段要求理由必须指名候选人条目代号，便于报告做证据引用。
"""
from __future__ import annotations

import hashlib

PROMPT_VERSION = "p1.2"

SYS_PARSE = "你是简历结构化解析器。只输出 JSON，不要任何解释文字。"
SYS_LABEL = "你是招聘评估助手。只输出 JSON，不要任何解释文字。"
SYS_COMPARE = "你是资深招聘评审。只输出 JSON，不要任何解释文字。"
SYS_FIT = "你是招聘匹配度分析员。只输出 JSON，不要任何解释文字。"
SYS_Q = "你是技术面试官。只输出 JSON，不要任何解释文字。"
SYS_COVER = "你是简历证据检索员。只输出 JSON，不要任何解释文字。"

TPL_PARSE = """【岗位】{job_title}
【岗位要求摘要】{jd}

【简历全文】
{resume}

任务：把简历切成"经历/项目/教育/技能"等块，逐块抽出**可评估的能力项**。
硬性要求：
1. evidence 必须是简历原文里的**连续片段**（≤60 字），不得改写、不得用 | 或 / 拼接不同位置的内容；
2. 只抽有证据支撑的内容，不脑补；每块至少 1 个能力项，全文 4~20 个能力项；
3. 不要输出字符偏移（系统会自己对片段做锚定），多余字段一律不要给。

输出 JSON（只含这两个键）：
{{"blocks":[{{"section":"项目经历","title":"块标题","period":"2024.03-2024.09",
  "items":[{{"capability":"能力项名称","evidence":"原文片段","note":"简述含金量"}}]}}],
 "parse_quality":{{"sections_found":["教育背景","项目经历"],"issues":[]}}}}"""

TPL_LABEL = """【岗位】{job_title}
【评估维度】（dimension 必须从这些 id 中选：{dim_ids}）
{dim_desc}

【待标注的能力项】
{items}

任务：为每个能力项标注 相关性、难度，选出它归属的维度，并给出理由与置信度。
- relevance：强(直接对口) / 中(沾边) / 弱(泛泛) / 无关(完全不对口)
- difficulty：高 / 中 / 低（该项经历本身的含金量，与岗位匹配无关）
- dimension：从上面 id 里选**最贴切的一个**；都不贴切时填第一个最接近的，理由里说明
- confidence：你对这条标注的把握，0~1 的小数

输出 JSON：
{{"items":[{{"idx":1,"block":"所属块标题","capability":"能力项名称","evidence":"原文片段",
  "relevance":"强","difficulty":"中","dimension":"维度id","reason":"为什么这么标","confidence":0.8}}]}}
要求：items 的条数与给你的一致，idx 必须对应，不许增删条目。"""

TPL_COVER = """【岗位】{job_title}
【岗位要求摘要】{jd}

【评估维度】（每个维度都必须给出结论，不许漏）
{dim_lines}

【简历全文】
{resume}

任务：**逐个维度**在这份简历里找支撑证据。每个维度都要有结论：
- 找到证据：found=true，items 里给 1~3 条，每条 = 能力项名称 + 简历原文里的**连续片段**（≤60 字，不得改写、不得拼接不同位置的句子）；
- 找不到：found=false，items 为空，note 里一句话说明"简历里没有哪方面的内容"（不要编造，也不要拿相近内容硬凑）。

判据提示：证据要能**直接支撑**该维度（例如"项目完整度"看的是有没有完整交付过东西，
"学习与竞赛"看的是竞赛/获奖/自学产出）。同一个片段不要重复塞给多个维度，
除非它确实同时支撑多个维度（那就分别列出，各自说明用途）。

输出 JSON：
{{"dimensions":[{{"id":"维度id","found":true,"note":"","items":[{{"capability":"能力项名称","evidence":"原文片段"}}]}}]}}
要求：dimensions 必须覆盖上面列出的**每一个** id，顺序与上面一致。"""

TPL_COMPARE = """【岗位】{job_title}
【岗位要求】
{jd}

【硬性门槛】（被触发的门槛，veto_filter_ids 里必须填它的 id；一条都没触发就填 []）
{veto}

【锚点标尺 —— 拿这三档当尺子去量候选人】
合格档：{good}
边缘档：{border}
不达标档：{bad}

【候选人的能力项（已标注，方括号里是条目代号）】
{items}

任务：
1. 把候选人整体与上面三档锚点做**相对比较**，判定 达标 / 边缘 / 不达标；
2. 指出是否触发硬性门槛：**只有当候选人明确不满足**上面某条硬性门槛（例如门槛要求本科、简历写明是大专；
   门槛要求某技能、简历通篇没有）才写一句人话；候选人满足、或简历没写清楚，一律填 null。
   注意：「能力一般」「项目不够深」不等于门槛不满足；门槛最终由规则层按岗位配置的关键词预检判定，你不要替它下结论；
3. 说明"最像哪一档的哪些点"，理由里必须指名条目代号（如 [7]），strengths / gaps 也各指名条目代号。

输出 JSON：
{{"verdict":"达标|边缘|不达标","closest_anchor":"合格|边缘|不达标","confidence":0.0,
 "veto_triggered":null,"veto_filter_ids":[],
 "reason":"2~4 句","strengths":["[7] 带证据的优势"],"gaps":["[12] 带证据的差距"]}}"""

TPL_FIT = """【岗位】{job_title}
【岗位要求】
{jd}
【岗位关注的方向】{dims}

【候选人简历全文】
{resume}

任务：把候选人简历里出现的**全部技能 / 工具 / 方法 / 经历 / 证书**逐条与岗位要求比对，分成三类，
给出方向性结论。注意区分"方向不同"与"能力不行"——本任务只判方向。

- matched：与岗位要求直接相关
- partial：沾边或可迁移（学过、接触过，但不是岗位主线）
- unmatched：与岗位无关（含明显是别的行业的技能）

口径：
1. fit_ratio = (matched 条数 + 0.5 × partial 条数) / 全部条数，保留两位小数；
2. verdict：对口 / 部分对口 / 方向不符（几乎无 matched 且 unmatched 占绝大多数 → 方向不符）；
3. direction：一句话说清"候选人技能画像偏向什么方向"vs"本岗位要求什么方向"（≥15 字）。

输出 JSON：
{{"matched":[{{"item":"","why":""}}],"partial":[{{"item":"","why":""}}],"unmatched":[{{"item":"","why":""}}],
 "fit_ratio":0.0,"direction":"...","verdict":"对口|部分对口|方向不符"}}"""

TPL_Q = """【岗位】{job_title}
【岗位要求】
{jd}
【面试重点】{focus}

【候选人最值得验证的能力项（相关性强 / 难度高；方括号是条目代号）】
{items}

任务：出 3 道面试题，**难度必须形成「低 / 中 / 高」三档各 1 道**，分层考核（禁止三题同难度）：
  · 低（基础盘）：验证岗位最低要求的真实掌握，答不上来说明基础不扎实
  · 中（深度）：针对候选人自述最强的经历，追问实现细节、技术取舍与踩过的坑
  · 高（上限）：针对岗位的高难度要求或该经历的边界情况，考察是"用过"还是"真懂"
             （原理追问 / 极端场景 / 方案对比 / 失效条件）
要求：
1. 每题标明 level（基础盘 | 深度 | 上限）、difficulty（低 | 中 | 高，与 level 对应）、
   target（考的是哪项能力或哪条岗位要求）；
2. 高难度题若候选人经历不足以支撑，就从【岗位要求】里挑一条高难度要求出题；
3. 每题给出 red_flag（什么样的回答说明是注水）。

输出 JSON：
{{"questions":[{{"level":"基础盘","difficulty":"低","target":"...","question":"...","intent":"想验证什么","red_flag":"..."}}]}}"""


def prompt_hash(*parts: str) -> str:
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def hash_of(system: str, template: str) -> str:
    return prompt_hash(PROMPT_VERSION, system, template)


def render_items(labeled: list[dict], *, with_evidence: bool = True, limit: int = 40) -> str:
    import json

    brief = []
    for i in labeled[:limit]:
        row = {"idx": i.get("idx"), "capability": i.get("capability"), "relevance": i.get("relevance"),
               "difficulty": i.get("difficulty"), "dimension": i.get("dimension")}
        if with_evidence:
            row["evidence"] = i.get("evidence")
        brief.append(row)
    return json.dumps(brief, ensure_ascii=False, indent=1)
