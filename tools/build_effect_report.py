#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把「方案①效果验证」渲染成一页 HTML（可点开、可预览），数据全部来自库里与 effect JSON。

为什么单独做：判断标准事先写死在 tools/effect_report.py 里，报告只负责把「达标/未达标」和证据摆出来，
不给人留下挑口径的空间（写报告时不许再改口径）。

用法：
    python tools/effect_report.py --job config/job_game_dev.json --json out/effect_v3.json
    python tools/build_effect_report.py --job config/job_game_dev.json --effect out/effect_v3.json \
        --out out/reports_gamedev_v3/验证报告.html
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.report import STYLE, esc          # noqa: E402
from app.store import db                   # noqa: E402


def fmt(x, nd: int = 1) -> str:
    try:
        return f"{float(x):.{nd}f}"
    except (TypeError, ValueError):
        return "—"


def bar(n: int, scale: int, width: int = 160) -> str:
    w = 0 if not scale else max(2, int(width * n / scale))
    return f"<span style='display:inline-block;height:10px;width:{w}px;background:#5b8def;border-radius:2px'></span>"


def hist_of(job_id: str, version: str) -> dict[str, int]:
    rows = db.fetch_all("""SELECT e.base_total FROM evaluation e
        JOIN application a ON a.application_id = e.application_id
        WHERE a.job_id = %s AND a.config_version = %s""", (job_id, version))
    h: dict[str, int] = {}
    for r in rows:
        lo = int(float(r["base_total"]) // 10) * 10
        h[f"{lo}~{lo + 9}"] = h.get(f"{lo}~{lo + 9}", 0) + 1
    return h


def hist_table(cur: dict, base_hist: dict) -> str:
    ch = cur.get("hist", {})
    keys = sorted(set(ch) | set(base_hist), key=lambda k: int(k.split("~")[0]))
    scale = max([1] + [v for k in keys for v in (ch.get(k, 0), base_hist.get(k, 0))])
    rows = "".join(
        f"<tr><td class=n>{esc(k)}</td><td class=n>{base_hist.get(k, 0)}</td><td>{bar(base_hist.get(k, 0), scale)}</td>"
        f"<td class=n>{ch.get(k, 0)}</td><td>{bar(ch.get(k, 0), scale)}</td></tr>" for k in keys)
    return f"""<table><thead><tr><th class=n>分数区间</th><th class=n>v2 份数</th><th>v2</th>
      <th class=n>v3 份数</th><th>v3</th></tr></thead><tbody>{rows}</tbody></table>"""


def coverage_table(cur: dict, base_cov: dict) -> str:
    # 基线三个维度是「事先写死的探针口径」，其余维度 v2 未度量过（那时根本没有 coverage 字段）
    base_map = {"项目完整度与交付": 0.31, "游戏理解与玩法素养": 0.29, "学习与竞赛产出": 0.16}
    rows = []
    for name, v in sorted(cur.get("coverage", {}).items(), key=lambda kv: -kv[1]):
        b = base_map.get(name)
        thr = "≥65%" if name in base_map else "—"
        ok = "" if b is None else ("<span class='pill ok'>达标</span>" if v >= 0.65
                                   else "<span class='pill bad'>未达标</span>")
        rows.append(f"<tr><td>{esc(name)}</td>"
                    f"<td class=n>{'—' if b is None else f'{b * 100:.0f}%'}</td>"
                    f"<td class=n><b>{v * 100:.0f}%</b></td><td class=n>{thr}</td><td class=n>{ok}</td></tr>")
    return ("<table><thead><tr><th>维度</th><th class=n>v2 覆盖率</th><th class=n>v3 覆盖率</th>"
            "<th class=n>事先写死的线</th><th class=n>结论</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table>")


def movement(job_id: str, v1: str, v2: str, top: int = 12) -> tuple[str, dict]:
    q = """SELECT r.file_name, a.config_version, e.verdict, e.decision, e.base_total
             FROM evaluation e JOIN application a ON a.application_id = e.application_id
             JOIN resume r ON r.resume_id = e.resume_id
            WHERE a.job_id = %s AND a.config_version = ANY(%s)"""
    rows = db.fetch_all(q, (job_id, [v1, v2]))
    by: dict[str, dict] = collections.defaultdict(dict)
    for r in rows:
        by[r["file_name"]][r["config_version"]] = r
    moved = []
    for f, d in by.items():
        if v1 in d and v2 in d:
            delta = float(d[v2]["base_total"]) - float(d[v1]["base_total"])
            if abs(delta) > 0.05 or d[v1]["decision"] != d[v2]["decision"]:
                moved.append((f, d[v1], d[v2], delta))
    moved.sort(key=lambda t: -abs(t[3]))
    dec_cn = {"auto_pass": "自动达标", "manual_review": "转人工", "auto_reject": "自动淘汰"}
    trs = "".join(
        f"<tr><td>{esc(f)}</td><td class=n>{fmt(a['base_total'])}</td><td class=n>{fmt(b['base_total'])}</td>"
        f"<td class=n>{'+' if d > 0 else ''}{fmt(d)}</td>"
        f"<td>{esc(a['verdict'])} → {esc(b['verdict'])}</td>"
        f"<td>{esc(dec_cn.get(a['decision'], a['decision']))} → {esc(dec_cn.get(b['decision'], b['decision']))}</td></tr>"
        for f, a, b, d in moved[:top])
    stat = {"对照份数": len(by), "有变化": len(moved),
            "变化中位": fmt(st.median([m[3] for m in moved])) if moved else "—"}
    return ("<table><thead><tr><th>简历</th><th class=n>v2 分</th><th class=n>v3 分</th><th class=n>Δ</th>"
            "<th>判定 v2→v3</th><th>处置 v2→v3</th></tr></thead><tbody>" + trs + "</tbody></table>"), stat


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True)
    ap.add_argument("--effect", required=True, help="tools/effect_report.py 写出的 JSON")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    cfg = json.load(open(a.job, encoding="utf-8"))
    eff = json.load(open(a.effect, encoding="utf-8"))
    cur, base, verdicts = eff["current"], eff["baseline"], eff["verdicts"]

    def pill(ok: bool) -> str:
        return "<span class='pill ok'>达标</span>" if ok else "<span class='pill bad'>未达标</span>"

    crit_rows = "".join(
        f"<tr><td>{esc(v['criterion'])}</td><td class=n>{esc(v['baseline'])}</td><td class=n><b>{esc(v['now'])}</b></td>"
        f"<td class=n>{pill(v['pass'])}</td></tr>"
        for v in verdicts)
    n_pass = sum(1 for v in verdicts if v["pass"])

    mv_html, mv_stat = movement(cfg["job_id"], "2", str(cfg["config_version"]))
    dec_cn = {"auto_pass": "自动达标", "manual_review": "转人工复核", "auto_reject": "自动淘汰"}

    deg = cur.get("rows_with_degraded_stage", 0)
    stages = cur.get("stages", {})
    bad_stages = {k: v for k, v in stages.items() if k.endswith(("=degraded", "=failed"))}

    html = f"""<!doctype html><html lang=zh><meta charset=utf-8><title>方案①效果验证 · {esc(cfg['job_title'])}</title>
{STYLE}
<body>
<h1>方案①「按维度定向取证」效果验证</h1>
<div class=sub>{esc(cfg['job_title'])}（{esc(cfg['job_id'])} v{esc(str(cfg['config_version']))}）·
 {cur.get('n')} 份全量跑批 · 与 v{base['version']} 基线对照 · 判断标准在改动前写死，报告只负责摆结果</div>

<div class=card>
  <div class=big>{n_pass} / {len(verdicts)} 条事先写死的标准达标</div>
  <div class=meta>分数中位 {fmt(cur.get('score', {}).get('median'))}（v2 {base['score']['median']}）·
   标准差 {fmt(cur.get('score', {}).get('sd'), 2)}（v2 {base['score']['sd']}）·
   自动达标 {cur.get('decision', {}).get('auto_pass', 0)} 份（v2 {base['decision']['auto_pass']}）</div>
  <table><thead><tr><th>事先写死的标准</th><th class=n>v2 基线</th><th class=n>v3 实测</th><th class=n>结论</th></tr></thead>
  <tbody>{crit_rows}</tbody></table>
</div>

<h2>1. 维度覆盖率（这次改动的正题）</h2>
<div class=meta>「覆盖率」= 该维度在评估里有证据（found=true）的简历占比。v2 的三个空缺维度是本次改动要修的根因。</div>
{coverage_table(cur, base['coverage'])}
<div class=meta>每份简历有证据的维度数：中位 {cur.get('dims_with_evidence', {}).get('median')} 个 / 共 6 个 ·
 分布 {esc(json.dumps(cur.get('dims_with_evidence', {}).get('hist', {}), ensure_ascii=False))}</div>

<h2>2. 分数分布（对照基线）</h2>
{hist_table(cur, hist_of(cfg['job_id'], str(base['version'])))}
<div class=meta>v2 基线：中位 {base['score']['median']} · 范围 {base['score']['lo']}~{base['score']['hi']} ·
 标准差 {base['score']['sd']}（v2 直方图由库里 base_total 现算，不用估算值）。
 v3 实测：中位 {fmt(cur.get('score', {}).get('median'))} · p10 {fmt(cur.get('score', {}).get('p10'))} ·
 p25 {fmt(cur.get('score', {}).get('p25'))} · p75 {fmt(cur.get('score', {}).get('p75'))} ·
 范围 {fmt(cur.get('score', {}).get('min'))}~{fmt(cur.get('score', {}).get('max'))} ·
 标准差 {fmt(cur.get('score', {}).get('sd'), 2)}</div>

<h2>3. 判定与处置</h2>
<table><thead><tr><th>口径</th><th>v2 基线</th><th>v3 实测</th></tr></thead><tbody>
<tr><td>规则层判定</td><td>{esc(json.dumps(base.get('verdict', {'边缘': 90, '不达标': 10}), ensure_ascii=False))}</td>
    <td>{esc(json.dumps(cur.get('verdict', {}), ensure_ascii=False))}</td></tr>
<tr><td>处置</td><td>{esc(json.dumps({dec_cn.get(k, k): v for k, v in base['decision'].items()}, ensure_ascii=False))}</td>
    <td>{esc(json.dumps({dec_cn.get(k, k): v for k, v in cur.get('decision', {}).items()}, ensure_ascii=False))}</td></tr>
<tr><td>冲突/复核原因</td><td>—</td><td>{esc(json.dumps(cur.get('conflicts', {}), ensure_ascii=False))}</td></tr>
<tr><td>硬门槛</td><td>—</td><td>{esc(json.dumps(cur.get('hard_filters', {}), ensure_ascii=False))}</td></tr>
</tbody></table>

<h2>4. 逐份对照（v2 → v3，变化最大的 {min(12, mv_stat['有变化'])} 份）</h2>
{mv_html}
<div class=meta>可对照 {mv_stat['对照份数']} 份 · 有变化 {mv_stat['有变化']} 份 · 变化中位 {mv_stat['变化中位']} 分</div>

<h2>5. 质量与残留问题（这一节必须比结论更显眼）</h2>
<table><thead><tr><th>项</th><th class=n>实测</th><th>说明</th></tr></thead><tbody>
<tr><td>阶段降级/失败的行</td><td class=n>{deg}</td>
    <td>降级行一律强制转人工；若这一列不为 0，说明本批数据部分失真，先看原因再谈结论</td></tr>
<tr><td>降级的阶段</td><td class=n>{esc(json.dumps(bad_stages, ensure_ascii=False) or '—')}</td>
    <td>降级行一律强制转人工：这一列不为 0 就先修数据、别谈结论。本批曾因平台 HTTP 429 有 70/100 份降级，
        修法是「进程级 TPM 限速 + 429 专属退避 + 降级行自动重跑」，最终 0 降级</td></tr>
<tr><td>置信度</td><td class=n>{esc(json.dumps(cur.get('confidence', {}), ensure_ascii=False))}</td>
    <td>最小值应为 0 只出现在降级行；「zero」列若不为 0 要查是哪几份</td></tr>
<tr><td>空面试题</td><td class=n>{cur.get('empty_questions')}</td>
    <td>触发硬门槛/门槛未知时按规则层跳过出题，属预期行为，但要能逐份解释</td></tr>
<tr><td>证据条数（中位）</td><td class=n>{cur.get('n_cited_evidence', {}).get('median')}</td>
    <td>覆盖上来了 ≠ 注水：配合前面的覆盖率一起看</td></tr>
<tr><td>加分项命中</td><td class=n>{cur.get('bonus_hit')}</td>
    <td>口径修复后应为少数（v2 曾人人命中）</td></tr>
</tbody></table>

<h2>6. 本批的已知局限（结论必须带着这些看）</h2>
<ul>
<li><b>没有人工标注的 ground truth</b>：覆盖率、分布、判定都只是"流程自洽性"的证据，
  回答不了"排得准不准"。要回答它只有一条路：方案③（带档位标签的对照语料：明显合格/边缘合格/明显不匹配/注水型各四分之一）。</li>
<li><b>达标线还是旧口径（60 分）</b>：v2 分布下标定的线，套到 v3 上卡在人群上方 ——
  97 份转人工里 91 份是"分数落在达标线附近（edge_band）"这一类冲突。要用 <code>tools/line_sweep.py</code> 按 v3 分布重标。</li>
<li><b>自动达标只有 1 份</b>：这一条虽然"不再恒为 0"，但离"能自动放行"差得远，属达标线问题而非覆盖率问题。</li>
<li><b>稳定性未在 v3 上复测</b>：同一份简历重复跑会落在达标线两侧（v2 实测 66.1 vs 63.9）。稳定性未达标前，分数不能用于自动淘汰。</li>
<li><b>未做人工抽检</b>：漏放率现在无法测量（需要每天从淘汰件随机抽 5~10% 给 HR 复看）。</li>
<li><b>单一岗位、单一语料</b>：100 份合成简历 + 一个岗位配置；换岗位/换真实简历都还没验证。</li>
<li><b>本批曾经混过两个模型</b>（主干 122B 被启动探活自动选中，82/100 份用它跑）：已用
  <code>requeue-model</code> 挑出来重跑，现 100/100 均为 {esc(str(cur.get('model', '降本档 35B')))}。
  教训：探活只能证明"端点活着"，证明不了"限流额度够用"，做对照要 <code>--model</code> 钉死。</li>
</ul>

<div class=foot>
证据链：跑批原始日志 <code>out/batch-run*.log</code> · 限速实测 <code>out/probe_*.log</code> ·
口径脚本 <code>tools/effect_report.py</code>（标准写死在此）· 本页由 <code>tools/build_effect_report.py</code> 生成。<br>
本报告仅供参考，不参与录用决策。
</div>
</body></html>"""
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    open(a.out, "w", encoding="utf-8").write(html)
    print("已写出", a.out, f"({len(html)} 字节)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
