# -*- coding: utf-8 -*-
"""报告渲染：单人报告（证据可点开原文）+ 批次矩阵。

证据可点开的实现：简历原文按 span 偏移切成片段，命中处包 <mark id="ev_0002">，
维度表/证据表里的引用写成 <a href="#ev_0002">，点一下滚动并高亮该段。
未锚定的证据渲染成灰色不可点（并在报告里明确标注），因为它的坐标不可信。
"""
from __future__ import annotations

import html as _html

from .contracts.evaluation import Evaluation
from .contracts.resume_parse import ResumeParse
from .text.anchor import highlight
from .text.fields import summarize as summarize_anomalies


def esc(x) -> str:
    return _html.escape(str(x if x is not None else ""))


VERDICT_COLOR = {"达标": "#1a7f37", "边缘": "#c98a00", "不达标": "#c0392b"}
DECISION_CN = {"auto_pass": "自动达标", "manual_review": "转人工复核", "auto_reject": "自动淘汰（可申诉）"}

STYLE = """<style>
 body { font-family: var(--app-font, "Segoe UI", "Microsoft YaHei", sans-serif); color: var(--foreground, #1a1a1a);
        margin: 0; padding: 6px 4px; font-size: 14px; line-height: 1.65; background: transparent; }
 h1 { font-size: 20px; margin: 0 0 4px; } h2 { font-size: 15px; margin: 22px 0 8px;
      border-bottom: 1px solid var(--border, #e5e5e5); padding-bottom: 4px; }
 .sub, .meta { color: var(--muted-foreground, #666); font-size: 12.5px; }
 table { border-collapse: collapse; width: 100%; margin: 6px 0 10px; }
 th, td { border: 1px solid var(--border, #e5e5e5); padding: 6px 8px; text-align: left; vertical-align: top; }
 th { background: var(--accent, #f3f3f3); font-weight: 600; }
 td.n, th.n { text-align: center; white-space: nowrap; }
 .card { border: 1px solid var(--border, #e5e5e5); border-radius: 6px; padding: 10px 12px; margin: 8px 0; }
 .big { font-size: 26px; font-weight: 700; }
 .pill { display: inline-block; border: 1px solid var(--border,#e5e5e5); border-radius: 10px;
         padding: 1px 8px; margin: 0 4px 4px 0; font-size: 12px; }
 mark { background: var(--accent, #ffe9a8); color: inherit; padding: 0 2px; border-radius: 2px; }
 mark:target, .un { outline: 2px solid #c98a00; }
 .un { color: var(--muted-foreground,#888); text-decoration: line-through; }
 a.ev { text-decoration: none; border-bottom: 1px dotted currentColor; }
 .text { white-space: pre-wrap; }
 ul { margin: 4px 0; padding-left: 20px; }
 .foot { color: var(--muted-foreground,#666); font-size: 12px; margin-top: 18px; }
</style>"""


def render_resume_text(parse: ResumeParse, cited_spans: list[tuple[int, int, str]]) -> str:
    """把原文按 span 高亮，每个高亮段带 id 锚点，供报告里的证据链接跳转（重叠区间自动合并）。"""
    merged: list[list] = []
    for s_, e_, sid in sorted(cited_spans):
        if merged and s_ <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e_)
            merged[-1][2].append(sid)
        else:
            merged.append([s_, e_, [sid]])
    out, prev = [], 0
    for s_, e_, ids in merged:
        out.append(esc(parse.text[prev:s_]))
        out.append(f'<mark id="{esc(ids[0])}">{esc(parse.text[s_:e_])}</mark>')
        prev = e_
    out.append(esc(parse.text[prev:]))
    return "".join(out)


def render_report(parse: ResumeParse, ev: Evaluation, *, title: str = "简历初筛报告") -> str:
    cited = [(c.span.start, c.span.end, c.span_id) for c in ev.cited_evidence
             if c.span and c.anchored]
    span_index = {c.span_id: c for c in ev.cited_evidence}
    dicts = {d.id: d for d in ev.dimensions}

    def ev_link(span_id: str | None, short: int = 60) -> str:
        c = span_index.get(span_id or "")
        if not c:
            return "<span class=meta>无证据</span>"
        txt = c.text if len(c.text) <= short else c.text[:short] + "…"
        if not c.anchored:
            return f'<span class=un title="{esc("锚定失败：" + c.anchor_method)}">{esc(txt)}</span> <span class=meta>（未锚定，不可点）</span>'
        return f'<a class=ev href="#{esc(c.span_id)}">{esc(txt)}</a>'

    dim_rows = "".join(
        f"<tr><td>{esc(d.name)}<div class=meta>{esc(d.id)}</div></td><td class=n>{d.weight:.0f}</td>"
        f"<td class=n>{d.points:.1f}</td><td class=n>{d.rate*100:.0f}%</td><td class=n>{len(d.items)}</td>"
        f"<td>{ev_link(d.best_span_id)}</td></tr>" for d in ev.dimensions)

    item_rows = "".join(
        f"<tr><td>{esc(i.capability)}</td><td class=n>{esc(i.relevance)}</td><td class=n>{esc(i.difficulty)}</td>"
        f"<td class=n>{i.raw:.2f}</td><td>{ev_link(i.span_id, 80)}</td></tr>"
        for d in ev.dimensions for i in d.items)

    unmapped_rows = "".join(
        f"<tr><td>{esc(u.capability)}</td><td class=n>{esc(u.relevance)}</td><td class=n>{esc(u.difficulty)}</td>"
        f"<td>{ev_link(u.span_id, 80)}</td></tr>" for u in ev.unmapped_items)

    hf_rows = "".join(
        f"<tr><td>{esc(h.name)}<div class=meta>{esc(h.rule)}</div></td>"
        f"<td class=n>{'✅ 通过' if h.result == 'pass' else '❌ 触发' if h.result == 'fail' else '⚠️ 未知'}</td>"
        f"<td>{esc(h.reason)}</td></tr>" for h in ev.hard_filters)

    conf_rows = "".join(f"<tr><td>{esc(c.type)}</td><td>{esc(c.detail)}</td></tr>" for c in ev.conflicts) or \
        "<tr><td colspan=2 class=meta>无</td></tr>"

    q_cards = "".join(
        f"<div class=card><div><b>{esc(q.question)}</b></div>"
        f"<div class=meta><span class=pill>{esc(q.level)}</span><span class=pill>难度 {esc(q.difficulty)}</span>"
        f"针对：{esc(q.target)}</div>"
        f"<div class=meta>想验证：{esc(q.intent)}</div>"
        f"<div class=meta>注水信号：{esc(q.red_flag)}</div></div>" for q in ev.questions) or \
        f"<p class=meta>{esc(ev.questions_skipped_reason or '未出题')}</p>"

    fit = ev.fit
    fit_block = "<p class=meta>未取得匹配度结果</p>"
    if fit:
        def lst(items):
            return "".join(f"<li>{esc(i.item)} <span class=meta>—— {esc(i.why)}</span></li>" for i in items) or "<li class=meta>无</li>"
        fit_block = (f"<p>结论 <b>{esc(fit.verdict)}</b> ｜ 匹配率 <b>{esc(fit.fit_ratio)}</b></p>"
                     f"<p class=meta>{esc(fit.direction)}</p>"
                     f"<table><thead><tr><th>直接相关</th><th>沾边/可迁移</th><th>方向无关</th></tr></thead>"
                     f"<tbody><tr><td><ul>{lst(fit.matched)}</ul></td><td><ul>{lst(fit.partial)}</ul></td>"
                     f"<td><ul>{lst(fit.unmatched)}</ul></td></tr></tbody></table>")

    anomalies = parse.quality.field_anomalies or []
    anomaly_err, anomaly_warn = summarize_anomalies(anomalies)
    anomaly_block = ("<ul>" + "".join(
        f"<li>{'❌' if a.severity == 'error' else '⚠️'} {esc(a.field)}：{esc(a.detail)}</li>"
        for a in anomalies[:10]) + "</ul>") if anomalies else "<p class=meta>未发现字段级异常。</p>"

    tl = parse.quality.timeline
    if tl.status == "issues":
        timeline_block = ("<p class=meta>⚠️ 发现 <b>" + str(len(tl.issues)) + "</b> 处时间线问题（只提示，不参与自动淘汰）：</p><ul>"
                          + "".join("<li>" + esc(i.detail) + "</li>" for i in tl.issues[:8]) + "</ul>")
    else:
        timeline_block = "<p class=meta>时间线自洽，未发现问题。</p>"

    reason_html = "".join(f"<li>{esc(r)}</li>" for r in ev.reasons)
    stage_objs = {"label": ev.stages.label, "compare": ev.stages.compare, "fit": ev.stages.fit,
                  "questions": ev.stages.questions}
    stages = "".join(
        f"<tr><td>{esc(k)}</td><td>{esc(v)}</td>"
        f"<td class=n>{getattr(stage_objs.get(k), 'validation').attempts if stage_objs.get(k) else '-'}</td></tr>"
        for k, v in ev.stage_status.items())
    repairs = []
    for st in (ev.stages.label, ev.stages.compare, ev.stages.fit, ev.stages.questions):
        if st and st.validation.repairs:
            repairs += [f"{st.stage}: {r}" for r in st.validation.repairs[:4]]
    repairs_html = "".join(f"<li>{esc(r)}</li>" for r in repairs) or "<li class=meta>无（一次通过）</li>"

    return f"""<!doctype html><html lang=zh><meta charset=utf-8><title>{esc(title)}</title>{STYLE}
<body>
<h1>{esc(parse.source.file_name)}</h1>
<div class=sub>岗位：{esc(ev.job_id)}（配置 {esc(ev.job_config_version)}）｜ 评估 {esc(ev.eval_id)}｜ 模型 {esc(ev.model_id)}｜ prompt {esc(ev.prompt_version)}｜ {esc(ev.created_at)}</div>
<div class=card>
  <div class=big style="color:{VERDICT_COLOR.get(ev.verdict, '#555')}">{esc(ev.verdict)} · {esc(DECISION_CN.get(ev.decision, ev.decision))}</div>
  <div class=sub>规则总分 <b>{ev.base_total:.1f}</b> / 达标线 {ev.scoring_snapshot.pass_line:.0f} ±{ev.scoring_snapshot.band_width:.0f}
   ｜ 维度聚合 {esc(ev.scoring_snapshot.aggregation)} ｜ 置信度 {ev.confidence:.2f} ｜ 引用证据 {len(ev.cited_evidence)} 条
   ｜ 解析质量门禁 {'通过' if ev.quality.gate_passed else '未通过'} ｜ 证据覆盖率 {ev.quality.evidence_coverage*100:.0f}%
   ｜ 标注 {('单次' if ev.labeling.method == 'single' else f'{ev.labeling.repeats} 次取中位数·维度一致率 {ev.labeling.dimension_agreement_mean}')}</div>
  <ul>{reason_html}</ul>
</div>

<h2>解析质量与字段级异常</h2>
<p class=meta>质量门禁：{'通过' if ev.quality.gate_passed else '未通过' + str(ev.quality.issues)}
｜ 可点开的证据覆盖率 {ev.quality.evidence_coverage*100:.0f}%（{ev.quality.unanchored_items} 条未锚定）
｜ 字段级异常 {anomaly_err} 个严重 / {anomaly_warn} 个提醒</p>
{anomaly_block}

<h2>时间线自洽校验</h2>
{timeline_block}

<h2>硬性门槛</h2>
<table><thead><tr><th>门槛</th><th class=n>结果</th><th>依据</th></tr></thead><tbody>{hf_rows}</tbody></table>

<h2>维度得分</h2>
<table><thead><tr><th>维度</th><th class=n>权重</th><th class=n>得分</th><th class=n>得分率</th><th class=n>条目</th><th>最强证据（点开看原文）</th></tr></thead><tbody>{dim_rows}</tbody></table>

<h2>能力项明细（相关性 × 难度 = 解释标签）</h2>
<table><thead><tr><th>能力项</th><th class=n>相关性</th><th class=n>难度</th><th class=n>单项分</th><th>原文证据</th></tr></thead><tbody>{item_rows}</tbody></table>
{('<h2>未归类条目</h2><p class=meta>模型没给维度、也没命中维度关键词的条目——单列而不硬塞进权重最高的维度。</p><table><thead><tr><th>能力项</th><th class=n>相关性</th><th class=n>难度</th><th>原文证据</th></tr></thead><tbody>' + unmapped_rows + '</tbody></table>') if ev.unmapped_items else ''}

<h2>岗位匹配度</h2>{fit_block}

<h2>面试问题（低/中/高 三档）</h2>{q_cards}

<h2>冲突与转人工理由</h2>
<table><thead><tr><th>类型</th><th>明细</th></tr></thead><tbody>{conf_rows}</tbody></table>

<h2>阶段校验状态</h2>
<table><thead><tr><th>阶段</th><th>status</th><th class=n>调用次数</th></tr></thead><tbody>{stages}</tbody></table>
<p class=meta>自动修正记录（repaired）：</p><ul>{repairs_html}</ul>

<h2>简历原文（证据高亮）</h2>
<p class=meta>黄色 = 本报告引用的证据，点击上方任意证据链接可跳到对应位置；灰色带删除线的证据表示锚定失败（坐标不可信，不允许作为自动淘汰依据）。</p>
<div class="card text">{render_resume_text(parse, cited)}</div>

<div class=foot>{esc(ev.disclaimer)}<br>
模型 {esc(ev.model_id)} ｜ prompt {esc(ev.prompt_version)} ｜ 配置版本 {esc(ev.job_config_version)} ｜ 证据 {ev.quality.evidence_coverage*100:.0f}%（{ev.quality.unanchored_items} 条未锚定）</div>
</body></html>"""


def render_matrix(rows: list[dict], *, job_title: str) -> str:
    """批次矩阵：一行一份简历，按规则总分排序，可点进单人报告。"""
    def row_html(r):
        total = r.get("base_total")
        total_s = f"{float(total):.1f}" if total is not None else ""
        decision = r.get("decision") or "—"
        color = VERDICT_COLOR.get(r.get("verdict") or "", "#555")
        link = f'<a href="{esc(r["file_name"])}.html">报告</a>' if r.get("file_name") else "—"
        return (f"<tr><td>{esc(r['file_name'])}</td>"
                f"<td style='color:{color};font-weight:600'>{esc(r.get('verdict') or '—')}</td>"
                f"<td>{esc(DECISION_CN.get(decision, decision))}</td>"
                f"<td class=n>{total_s}</td>"
                f"<td class=n>{esc(r.get('confidence'))}</td>"
                f"<td class=n>{esc(r.get('attempts'))}</td>"
                f"<td class=meta>{esc((r.get('last_error') or '')[:60])}</td><td>{link}</td></tr>")

    counts: dict[str, int] = {}
    for r in rows:
        counts[r.get("decision") or r.get("state") or "未完成"] = counts.get(r.get("decision") or r.get("state") or "未完成", 0) + 1
    return f"""<!doctype html><html lang=zh><meta charset=utf-8><title>批次矩阵</title>{STYLE}
<body>
<h1>批次矩阵 · {esc(job_title)}</h1>
<div class=sub>共 {len(rows)} 份 ｜ 处置分布：{esc('、'.join(f'{DECISION_CN.get(k, k)} {v}' for k, v in counts.items()))}</div>
<table><thead><tr><th>简历</th><th>判定</th><th>处置</th><th class=n>规则分</th><th class=n>置信度</th>
<th class=n>尝试次数</th><th>最后错误</th><th>报告</th></tr></thead><tbody>{''.join(row_html(r) for r in rows)}</tbody></table>
<div class=foot>本矩阵由脚本生成，仅供参考，不参与录用决策。</div>
</body></html>"""
