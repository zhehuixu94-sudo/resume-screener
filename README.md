# 简历初筛系统（Resume Screener）

> 用 LLM 帮 HR 把一批简历判成「达标 / 转人工 / 淘汰」，**每个结论都能点开原文证据**。
> 定位是**排除器，不是择优器**：成功标准不是「排得准」，而是「没把该留的人刷掉」。

个人项目（网络空间安全本科毕业设计方向 / 求职作品集），当前处于**任务 1 已完成、任务 2 在做的阶段**。

---

## 一、它解决什么问题

HR 拿到 200 份简历要在一晚上筛完，实际做法是搜关键词，结果是**注水简历混进来、对口简历被漏掉**。本系统的取舍：

1. **不自动淘汰没把握的人**：判定分三档——自动达标 / 转人工 / 自动淘汰，其中"转人工"是默认答案。
   触发条件包括：LLM 整体评估与规则分打架、置信度低于阈值、匹配度方向冲突、模型输出降级、证据没锚定成功。
2. **每个结论必须能追溯**：每条能力项都带**原文片段 + 字符级 span 偏移**，报告里可点开高亮。
   锚定失败的证据会被显式标出，并且**不允许**作为自动淘汰的唯一依据。
3. **不拍脑袋定达标线**：达标线由 HR 给的合格/不合格锚点样本推导（`pass_line ± band` 三段带），
   而不是全局写死一个 60 分。

## 二、架构

```
文本抽取          PyMuPDF / python-docx（段落 + 表格）
   ↓
规范化 + 偏移映射  折叠空白、去控制字符（不做 NFKC，否则高亮出的就不是原文）
   ↓
解析质量门禁       字符密度 / 段落结构 / 乱码字符数 → 烂文本不进 LLM
   ↓
① 解析  blocks + 能力项，每项给原文片段
   ↓
证据锚定          exact → 空白/分隔符不敏感 → 模糊 → 失败（四级，逐级降级并留痕）
   ↓
② 标注            相关性（强/中/弱/无关）× 难度（高/中/低）× 维度 + 置信度
③ 整体判定        与 HR 锚点做「相对比较」，而不是打绝对分
④ 匹配度          技能画像 vs 岗位方向（对口 / 部分对口 / 方向不符）
⑤ 面试题          低/中/高 三档，附「注水信号」
   ↓
规则层（不加 LLM）  单项分 = 相关性系数 × 难度系数 → 维度分 → 加权总分
   ↓
判定层            三段带（达标线 ± band）+ 双通道（整体评估 × 规则分）+ 冲突检测
```

**数据契约先行**：三份 JSON Schema（简历解析 / 岗位配置 / 评估）+ 五段模型输出契约，全部由 Pydantic 模型导出，
代码与 schema 漂移会被测试卡住。详见 [`schemas/README.md`](schemas/README.md)。

## 三、当前进度

| 任务 | 状态 | 说明 |
|---|---|---|
| 任务 1 数据契约三件套 | ✅ 完成并验收 | 8 个 JSON Schema（Pydantic 导出）、四级证据锚定、校验四态（ok/repaired/degraded/failed）、三段带双通道判定 |
| 任务 2 M0 最小闭环 | 🚧 进行中 | 单份已能端到端跑通；批处理、状态机与落库待补 |
| 任务 3 锚点标定达标线 | ⏳ 契约已留位 | `JobConfig.calibration` / `scoring.pass_line_source` |
| 任务 4 规则分稳定性 | ⏳ 契约已留开关 | `scoring.dimension_aggregation = best / top2_mean`（默认仍是 best） |
| 任务 5 时间线 / 字段级校验 | ⏳ 契约已留位 | `quality.timeline`、`quality.field_anomalies` |

### 任务 1 的实测结果（4 份真实简历 × 安全服务岗）

- 8/8 schema 与代码一致；6/6 岗位配置过契约
- 4/4 简历跑通解析契约，Pydantic 与导出的 JSON Schema **双验通过**
- 证据锚定 27/28（96.4%），span 切片逐条回原文核对 **0 处错位**
- 完整评估样例：达标 / `auto_pass`，规则分 66.1（达标线 60 ± 5），冲突 0
- 降级四态离线自测 7/7 符合预期；`pytest` 20 passed

## 四、快速开始

```bash
git clone <repo> && cd resume-screener
uv venv --python 3.11 .venv          # 或 python -m venv .venv
uv pip install --python .venv/Scripts/python.exe pydantic jsonschema pymupdf python-docx pytest

export SILICONFLOW_API_KEY=sk-xxx    # 密钥只从环境变量读；开发期也可指向本机密钥文件

# 离线自检（不花 token）：契约一致性 + 岗位配置校验 + 重试/降级四态
.venv/Scripts/python -m pytest tests -q
.venv/Scripts/python tools/validate_contracts.py --no-live

# 带真实简历跑全量验收（会调用模型）
cp local/resumes.json.example local/resumes.json   # 填自己的简历路径（local/ 已 gitignore）
.venv/Scripts/python tools/validate_contracts.py
```

模型：默认 `Qwen/Qwen3.5-122B-A10B`（关思考模式），降本档 `Qwen/Qwen3.5-35B-A3B`，`--model` 可覆盖。
平台为硅基流动（OpenAI 兼容 HTTP，无第三方 SDK 依赖）。

## 五、目录结构

```
app/
  contracts/   三件套 + 五段模型输出契约（Pydantic）+ 清洗层（枚举归一/垃圾值剔除/未知字段留痕）
  text/        抽取 · 规范化与偏移映射 · 四级锚定 · 质量门禁
  llm/         硅基流动客户端 · 五段 prompt · 校验-重试-降级引擎 · 清洗器
  pipeline/    解析段 · 标注/判定/匹配度/面试题 + 规则层与三段带判定
schemas/       由 Pydantic 导出的 JSON Schema（含 5 段模型输出契约）
config/        6 个岗位配置（硬门槛 / 维度锚点 / 加分项关键词 / 达标线与标定）
tools/         导出 schema · 迁移配置 · 契约验收
tests/         契约、锚定、判定策略的回归测试
```

## 六、不提交什么

- `local/`（真实简历路径清单）、`out/`（报告与运行产物，含真实简历原文）、`.venv/`
- 任何密钥：代码只从环境变量 `SILICONFLOW_API_KEY` 或本机密钥文件读，仓库里没有 key

## 七、免责声明

本系统是**辅助工具**，输出的判定与分数**仅供参考，不参与录用决策**，结论必须由 HR 复核。
简历含个人信息：默认本地留存，对外发送前必须脱敏。

## 八、技术栈

Python 3.11 · Pydantic v2 · JSON Schema · FastAPI（任务 2 起）· PostgreSQL 16 + pgvector（任务 2 起）·
Vue3 + Vite（构建 target Chrome 109，任务 2 起）· 硅基流动（LLM / Embedding / Reranker）
