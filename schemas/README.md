# 数据契约三件套（v1.0.0）

> 任务 1 的产出。**Pydantic 模型是唯一真相来源**，本目录的 `*.schema.json` 全部由
> `python tools/export_schemas.py` 导出，**不要手改 JSON**。`--check` 模式用于提交前校验是否漂移（漂移即退出码 1）。

```
schemas/
├─ resume_parse.schema.json   ① 简历解析：块 / 能力项 / 证据 span 偏移 / 质量门禁 / 复现元数据
├─ job_config.schema.json     ② 岗位配置：硬门槛 + 维度锚点 + 加分项关键词 + 达标线与三段带 + 锚点标定
├─ evaluation.schema.json     ③ 评估：判定 / 理由 / 证据引用 / 置信度 / 各维度分数 / 硬门槛结果 / 匹配度
└─ llm/                       五段模型输出的独立 schema（可作 response_format.json_schema 的载荷）
   ├─ parse_output.schema.json      {} → blocks[] + parse_quality
   ├─ label_output.schema.json      idx/relevance/difficulty/dimension/reason/confidence
   ├─ compare_output.schema.json    verdict/closest_anchor/confidence/veto_triggered/veto_filter_ids/reason
   ├─ fit_output.schema.json        matched/partial/unmatched/fit_ratio/direction/verdict
   └─ questions_output.schema.json  level/difficulty/target/question/intent/red_flag
```

## 一、三个立场（决定了字段怎么切）

1. **偏移由代码锚定，模型只给原文片段。**
   模型自报字符偏移不可信，而"原文片段 → 偏移"可以由代码确定性还原
   （`exact` → `ws_insensitive` → `fuzzy` → `none`）。所以 `ResumeParse.evidence[].span` 是**计算出来的**，
   `anchoring.method` 把锚定质量暴露出来，而不是藏起来。
2. **判定是规则层的事，不是模型的事。**
   模型给"整体评估"（与 HR 锚点做相对比较），规则层给算术分，两路互相制衡；
   `Evaluation.verdict/decision` 只能由规则层产出，`llm_compare` 只作为留档。
3. **降级必须显式、必须可见、必须转人工。**
   四态 `ok / repaired / degraded / failed` 落在每一段的 `StageMeta.validation.status` 里。
   `degraded`、`failed` 或"引用里有未锚定证据"时，契约层直接禁止自动处置——这是"宁可多看、不可漏放"的代码落点。

## 二、谁填什么（职责边界）

| 字段 | 谁填 | 说明 |
|---|---|---|
| `ResumeParse.text` / `source` / `normalization` | 系统（抽取层） | 规范化只做折叠空白、去控制字符；**不做 NFKC**，否则高亮出来的就不是原文 |
| `ResumeParse.blocks/items` | 模型（parse 段） | 块 + 能力项 + capability/evidence/note |
| `ResumeParse.evidence[].span / raw_range / page / anchoring` | 系统（锚定层） | 契约不接受模型自报偏移；span 是规范化文本坐标，raw_range 指回抽取原文 |
| `ResumeParse.quality.gate_*` | 系统（门禁） | 字符密度 / 段落结构 / 乱码字符数 |
| `ResumeParse.quality.model_*` | 模型自报 | 识别到哪些段落、有无缺失 |
| `ResumeParse.quality.timeline` / `field_anomalies` | 系统（任务 5） | 当前 `not_checked` / 占位实现 |
| `ResumeParse.llm` | 系统 | model_id + prompt_version + prompt_hash + trace_id + tokens + 校验状态 |
| `JobConfig.*` | HR（配置层） | 达标线、锚点、权重、加分项关键词；`calibration` 是任务 3 的锚点标定落点 |
| `Evaluation.dimensions/ hard_filters / base_total` | 系统（规则层） | 单项分 = 相关性系数 × 难度系数；维度分聚合方式可配（best / top2_mean） |
| `Evaluation.verdict/decision/decision_reason/conflicts` | 系统（判定层） | 三段带（达标线 ± band）+ 双通道 + 冲突检测 |
| `Evaluation.llm_compare / questions` | 模型（compare / questions 段） | 留档，不直接决定处置 |
| `Evaluation.cited_evidence` | 系统 | 从解析结果拷一份自包含副本，报告不用回查就能高亮 |

## 三、校验的四个状态

| 状态 | 触发 | 后果 |
|---|---|---|
| `ok` | 一次就过 | — |
| `repaired` | 枚举同义归一、百分数折算、fit_ratio 重算、丢未知字段、剥代码围栏 | 修正明细进 `repairs` / `extra_keys_dropped`，报告可见 |
| `degraded` | 重试后仍不合契约 → 丢掉非法条目后勉强通过 | **强制转人工**，不许自动处置 |
| `failed` | 两次都不合契约且无可丢的条目 | 该段返回 None，上层按缺失处理 → **强制转人工** |

重试规则：解析失败 → 带**错误反馈**再调一次（把 Pydantic 的错误摘要回灌给模型），最多一次。

## 四、版本与复现

- `SCHEMA_VERSION` 在 `app/contracts/common.py`，出现在每个域对象的 `schema_version` 字段（`Literal` 常量，改版本号即强制升级）。
- 每次评分行都带 `model_id + prompt_version + prompt_hash + config_version`（`StageMeta` / `Evaluation`），历史分数才能复现。
- 提示词版本在 `app/llm/prompts.py::PROMPT_VERSION`；**改 prompt 必须递增**，否则同一 config_version 下的分数会不可比。

## 五、改契约的正确姿势

```bash
# 1. 改 app/contracts/**（Pydantic）
# 2. 重新导出 + 一致性检查
python tools/export_schemas.py && python tools/export_schemas.py --check
# 3. 跑离线自测（重试/降级四态）+ 契约验收
python -m app.llm.selftest
python tools/validate_contracts.py --no-live     # 再跑一次带真实简历的全量
```

改 `llm/*.schema.json` 对应模型时，同步改 `app/llm/prompts.py` 里那段 prompt——prompt 与契约必须一字不差地对得上。
