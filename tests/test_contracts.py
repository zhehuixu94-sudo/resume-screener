# -*- coding: utf-8 -*-
"""契约与锚定的回归测试（离线，不花 token）。

跑法：.venv\\Scripts\\python -m pytest tests -q
"""
from __future__ import annotations

import json
import os
import sys

import pytest
from pydantic import ValidationError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.contracts import Evaluation, JobConfig, SCHEMA_VERSION  # noqa: E402
from app.llm.selftest import run_scenarios  # noqa: E402
from app.text.anchor import anchor  # noqa: E402
from app.text.normalize import collapse_ws, normalize  # noqa: E402
from tools.validate_contracts import check_job_configs, check_schema_drift, load_schemas  # noqa: E402
from tools.migrate_job_config import migrate  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------- 契约导出
def test_schema_与代码一致():
    r = check_schema_drift()
    assert r["ok"], f"schema 漂移：{r['drifted']}"


def test_三件套与五段输出都有schema():
    sch = load_schemas()
    assert set(sch) == {"resume_parse", "job_config", "evaluation"}
    for name in sch:
        assert sch[name]["x-contract-version"] == SCHEMA_VERSION


# ---------------------------------------------------------------- 岗位配置
def test_所有岗位配置过契约():
    rows = check_job_configs(os.path.join(ROOT, "config"))
    assert len(rows) >= 6                                  # 原型的 6 个 + 后续新增岗位
    bad = [r for r in rows if not r.get("ok")]
    assert not bad, bad


def test_权重不足100被拒():
    cfg = json.load(open(os.path.join(ROOT, "config", "job_secops.json"), encoding="utf-8"))
    cfg["dimensions"][0]["weight"] = 10
    with pytest.raises(ValidationError):
        JobConfig.model_validate(cfg)


def test_已标定但来源没回填被拒():
    cfg = json.load(open(os.path.join(ROOT, "config", "job_secops.json"), encoding="utf-8"))
    cfg["calibration"] = {"status": "calibrated",
                          "anchors": [{"label": "合格", "resume_ref": f"a{i}"} for i in range(4)]}
    with pytest.raises(ValidationError):
        JobConfig.model_validate(cfg)


def test_合格锚点与不达标锚点相同被拒():
    cfg = json.load(open(os.path.join(ROOT, "config", "job_secops.json"), encoding="utf-8"))
    cfg["dimensions"][0]["anchors"]["不达标"] = cfg["dimensions"][0]["anchors"]["合格"]
    with pytest.raises(ValidationError):
        JobConfig.model_validate(cfg)


def test_锚点lint能挑出薄锚点():
    old = json.load(open(os.path.join(ROOT, "config", "job_hr_admin.json"), encoding="utf-8"))
    assert isinstance(JobConfig.model_validate(migrate(old)).anchor_lint(), list)


# ---------------------------------------------------------------- 文本与锚定
def test_空白折叠与偏移映射():
    raw = "项目经历\n  独立开发  扫描器\n"
    n = normalize(raw)
    assert n.text == "项目经历 独立开发 扫描器"
    i = n.text.find("独立开发")
    assert n.raw_slice(i, i + 4) == "独立开发"
    assert n.ops == ["drop_control", "collapse_ws", "strip"]


def test_锚定四级策略():
    raw = "教育背景\n示例大学 网络空间安全 本科 2022.09-2026.06\n技能：Python / Linux"
    n = normalize(raw)
    # 1) 精确
    r1 = anchor("示例大学 网络空间安全", n)
    assert r1.anchoring.method == "exact" and n.text[r1.span.start:r1.span.end] == "示例大学 网络空间安全"
    # 2) 空白不敏感（模型把换行处拼成空格/把分隔符换成 |）
    r2 = anchor("示例大学|网络空间安全", n)
    assert r2.anchoring.method == "ws_insensitive"
    # 3) 模糊（模型漏了两个字）
    r3 = anchor("示例大学 网络空间 本科", n)
    assert r3.anchoring.method in ("fuzzy", "ws_insensitive") and r3.span is not None
    # 4) 失败：片段不在文中
    r4 = anchor("清华大学 计算机博士", n)
    assert r4.anchoring.method == "none" and r4.span is None


def test_过短片段不锚定():
    n = normalize("本科 江苏 计算机")
    assert anchor("本科", n).anchoring.method == "none"


def test_同片段多处出现记录alt_spans():
    n = normalize("Python 后端 Python 后端 Python 后端")
    r = anchor("Python 后端", n)
    assert r.anchoring.method == "exact" and len(r.alt_spans) == 2


# ---------------------------------------------------------------- 判定层
def _eval_base():
    return json.load(open(os.path.join(ROOT, "out", "_fixture_eval.json"), encoding="utf-8")) \
        if os.path.exists(os.path.join(ROOT, "out", "_fixture_eval.json")) else None


def _cfg():
    return JobConfig.model_validate(json.load(open(os.path.join(ROOT, "config", "job_secops.json"), encoding="utf-8")))


def test_判定_自动达标需要过线():
    from app.pipeline.rules import decide
    cfg = _cfg()
    out = decide(cfg, base_total=cfg.scoring.pass_line + 10, compare=_cmp("达标", 0.9), fit=None,
                 stage_status={"label": "ok", "compare": "ok", "fit": "ok"}, anchored_span_ids=set())
    assert (out.verdict, out.decision) == ("达标", "auto_pass")


def test_判定_两路打架转人工():
    from app.pipeline.rules import decide
    cfg = _cfg()
    out = decide(cfg, base_total=cfg.scoring.pass_line - 20, compare=_cmp("达标", 0.9), fit=None,
                 stage_status={"label": "ok", "compare": "ok", "fit": "ok"}, anchored_span_ids=set())
    assert out.decision == "manual_review" and "llm_vs_rule" in [c.type for c in out.conflicts]


def test_判定_降级一律转人工():
    from app.pipeline.rules import decide
    cfg = _cfg()
    out = decide(cfg, base_total=10, compare=_cmp("不达标", 0.9), fit=None,
                 stage_status={"label": "degraded", "compare": "ok", "fit": "ok"}, anchored_span_ids=set())
    assert out.decision == "manual_review" and "degraded_validation" in [c.type for c in out.conflicts]


def test_判定_方向不符不自动淘汰():
    from app.pipeline.rules import decide
    cfg = _cfg()
    fit = _fit("方向不符", 0.1)
    out = decide(cfg, base_total=cfg.scoring.pass_line - 1, compare=_cmp("不达标", 0.9), fit=fit,
                 stage_status={"label": "ok", "compare": "ok", "fit": "ok"}, anchored_span_ids=set())
    assert out.decision == "manual_review"


def test_判定_低置信度不自动淘汰():
    from app.pipeline.rules import decide
    cfg = _cfg()
    out = decide(cfg, base_total=10, compare=_cmp("不达标", 0.3), fit=None,
                 stage_status={"label": "ok", "compare": "ok", "fit": "ok"}, anchored_span_ids=set())
    assert out.decision == "manual_review" and "low_confidence" in [c.type for c in out.conflicts]


def test_判定_未锚定证据阻断自动淘汰():
    from app.pipeline.rules import decide, sync_decisions_with_evidence
    cfg = _cfg()
    out = decide(cfg, base_total=10, compare=_cmp("不达标", 0.9), fit=None,
                 stage_status={"label": "ok", "compare": "ok", "fit": "ok"}, anchored_span_ids=set())
    assert out.decision == "auto_reject"
    out = sync_decisions_with_evidence(out, {"ev_0001": False}, [])
    assert out.decision == "manual_review" and "unanchored_evidence" in [c.type for c in out.conflicts]


def _hard(cfg, **results):
    """构造规则层硬门槛结果。"""
    from app.contracts.evaluation import HardFilterResult
    return [HardFilterResult(id=f.id, name=f.name, rule=f.rule, veto=f.veto,
                             result=results.get(f.id, "pass"), reason="测试构造")
            for f in cfg.hard_filters]


def test_判定_规则层门槛不通过才自动淘汰():
    from app.pipeline.rules import decide
    cfg = _cfg()
    out = decide(cfg, base_total=10, compare=_cmp("不达标", 0.9), fit=None,
                 stage_status={"label": "ok", "compare": "ok", "fit": "ok"}, anchored_span_ids=set(),
                 hard_filters=_hard(cfg, edu="fail"))
    assert out.decision == "auto_reject"
    assert [h.result for h in out.hard_filters if h.id == "edu"] == ["fail"]


def test_判定_门槛证据不足转人工():
    from app.pipeline.rules import decide
    cfg = _cfg()
    out = decide(cfg, base_total=10, compare=_cmp("不达标", 0.9), fit=None,
                 stage_status={"label": "ok", "compare": "ok", "fit": "ok"}, anchored_span_ids=set(),
                 hard_filters=_hard(cfg, edu="unknown"))
    assert out.decision == "manual_review" and "veto_triggered" in [c.type for c in out.conflicts]


def test_判定_模型说触发但规则层判通过则转人工():
    """实测事故：模型把写了本科、网络空间安全的候选人判成『未提及学历』并自动淘汰。"""
    from app.pipeline.rules import decide
    cfg = _cfg()
    cmp = _cmp("不达标", 0.9)
    cmp.veto_triggered = "未明确提及学历为本科及以上"
    cmp.veto_filter_ids = ["edu"]
    out = decide(cfg, base_total=60, compare=cmp, fit=None,
                 stage_status={"label": "ok", "compare": "ok", "fit": "ok"}, anchored_span_ids=set(),
                 hard_filters=_hard(cfg))          # 规则层：三条全部 pass
    assert out.decision == "manual_review"
    assert "llm_vs_rule" in [c.type for c in out.conflicts]


def _parse_stub(text: str, evidences: list[tuple[str, str]] | None = None):
    """构造一份最小可用的 ResumeParse，用于硬门槛规则层测试。"""
    from app.contracts.common import (Anchoring, CharRange, EvidenceSpan, Normalization,
                                      Quality, QualityCheck, SourceInfo, StageMeta)
    from app.contracts.resume_parse import Block, ParsedItem, ParseStats, ResumeParse
    evs, items = [], []
    for i, (iid, ev_text) in enumerate((evidences or []), start=1):
        pos = text.find(ev_text)
        span = CharRange(start=pos, end=pos + len(ev_text)) if pos >= 0 else None
        evs.append(EvidenceSpan(span_id=f"ev_{i:04d}", item_id=iid, text=ev_text, span=span,
                                anchoring=Anchoring(method="exact" if span else "none", similarity=1.0 if span else 0.0)))
        items.append(ParsedItem(item_id=iid, block_id="b1", capability="测试项",
                                span_id=f"ev_{i:04d}" if span else None, anchored=bool(span)))
    if not items:
        items = [ParsedItem(item_id="b1.i1", block_id="b1", capability="测试项", anchored=False)]
    return ResumeParse(
        resume_id="test0000test0000",
        source=SourceInfo(file_name="t.txt", file_sha256="0" * 64, file_format="txt", extractor="builtin", pages=1),
        normalization=Normalization(ops=["strip"], raw_char_count=len(text), norm_char_count=len(text)),
        quality=Quality(gate_passed=True, checks=[QualityCheck(name="可读字符数", value=str(len(text)), ok=True)]),
        blocks=[Block(block_id="b1", section="测试", title="测试块", item_ids=[i.item_id for i in items])],
        items=items, evidence=evs,
        stats=ParseStats(blocks=1, items=len(items), evidence_total=len(evs),
                         evidence_anchored=sum(1 for e in evs if e.span),
                         evidence_unanchored=sum(1 for e in evs if not e.span),
                         evidence_coverage=1.0),
        text=text,
        llm=StageMeta(stage="parse", model_id="test", prompt_version="p-test", prompt_hash="h"))


def test_硬门槛预检_本科与对口专业不被冤枉():
    """实测事故回归：简历里写着本科 + 网络空间安全，模型却说『未提及学历』并据此淘汰。"""
    import json as _json
    from app.contracts import JobConfig
    from app.pipeline.hard_filters import check_hard_filters, veto_summary
    cfg = JobConfig.model_validate(_json.load(open(os.path.join(ROOT, "config", "job_jushi_teacher.json"),
                                                  encoding="utf-8")))
    text = "教育经历 示例大学 网络空间安全 本科 2022.9-2026.6 竞赛：CTF 市级三等奖"
    parse = _parse_stub(text, [("b1.i1", "示例大学 网络空间安全 本科"), ("b1.i2", "CTF 市级三等奖")])
    res = check_hard_filters(cfg, parse, None)
    fails, unknown, passes = veto_summary(res)
    assert "edu" in passes and "major" in passes, [(r.id, r.result, r.reason) for r in res]
    assert not fails
    assert "cet4" not in [f.id for f in cfg.hard_filters], "四级已降为综合评分项，不再一票否决"
    edu = next(r for r in res if r.id == "edu")
    assert edu.evidence_span_ids, "命中关键词时要带上证据 span，报告才点得开"
    # 抽取的证据没覆盖到关键词时：如实说明，不编 span
    parse2 = _parse_stub("本人本科毕业，另有专科经历", [("b1.i1", "做过一个管理系统")])
    res2 = check_hard_filters(cfg, parse2, None)
    edu2 = next(r for r in res2 if r.id == "edu")
    assert edu2.result == "pass" and edu2.evidence_span_ids == []
    assert "无法高亮" in edu2.reason


def test_硬门槛预检_大专与不对口专业判不通过():
    import json as _json
    from app.contracts import JobConfig
    from app.pipeline.hard_filters import check_hard_filters, veto_summary
    cfg = JobConfig.model_validate(_json.load(open(os.path.join(ROOT, "config", "job_jushi_teacher.json"),
                                                  encoding="utf-8")))
    parse = _parse_stub("教育经历 某某学院 工程管理 大专", [("b1.i1", "工程管理")])
    fails, unknown, _passes = veto_summary(check_hard_filters(cfg, parse, None))
    assert "edu" in fails and "major" in fails


def test_英语能力作为维度覆盖六级与四级():
    """四级从否决项改为『英语能力』维度后，六级/雅思等更高证明同样算合格证据。"""
    import json as _json
    from app.contracts import JobConfig
    cfg = JobConfig.model_validate(_json.load(open(os.path.join(ROOT, "config", "job_jushi_teacher.json"),
                                                  encoding="utf-8")))
    eng = next(d for d in cfg.dimensions if d.id == "english")
    for kw in ("四级", "CET-4", "六级", "CET-6", "雅思", "托福"):
        assert kw in eng.keywords, f"英语维度关键词缺 {kw}"
    assert eng.anchors.合格 and "四级" in eng.anchors.合格


def _cmp(verdict: str, conf: float):
    from app.contracts.llm_outputs import LlmCompareOutput
    return LlmCompareOutput(verdict=verdict, closest_anchor="合格" if verdict == "达标" else "不达标",
                            confidence=conf, reason="测试用理由，需要够长才能过契约校验。")


def _fit(verdict: str, ratio: float):
    from app.contracts.llm_outputs import LlmFitOutput
    return LlmFitOutput(matched=[], partial=[], unmatched=[{"item": "AutoCAD", "why": "无关"}],
                        fit_ratio=ratio, direction="候选人偏建筑方向，岗位要求网络安全方向", verdict=verdict)


# ---------------------------------------------------------------- 重试/降级四态
def test_重试降级四态自测全通过():
    bad = [s.name for s in run_scenarios() if not s.result.get("pass")]
    assert not bad, f"以下场景不符合预期：{bad}"
