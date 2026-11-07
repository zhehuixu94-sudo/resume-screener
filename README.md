# 简历初筛系统（Resume Screener）

> 用 LLM 帮 HR 把一批简历判成「达标 / 转人工 / 淘汰」，**每个结论都能点开原文证据**。
> 定位是**排除器，不是择优器**：成功标准不是「排得准」，而是「没把该留的人刷掉」。

个人项目（网络空间安全本科毕业设计方向 / 求职作品集）。当前处于**「两段流程（定线 + 定及格项）+ 行政岗 310 份全量重跑」阶段**；口径与最新实测见第三节 `最新状态`。

---

## 一、它解决什么问题

HR 拿到 200 份简历要在一晚上筛完，实际做法是搜关键词，结果是**注水简历混进来、对口简历被漏掉**。本系统的取舍：

1. **不自动淘汰没把握的人**：判定分三档——自动达标 / 转人工 / 自动淘汰，其中"转人工"是默认答案。
   触发条件包括：LLM 整体评估与规则分打架、置信度低于阈值、匹配度方向冲突、模型输出降级、证据没锚定成功。
2. **每个结论必须能追溯**：每条能力项都带**原文片段 + 字符级 span 偏移**，报告里可点开高亮。
   锚定失败的证据会被显式标出，并且**不允许**作为自动淘汰的唯一依据。
3. **不拍脑袋定达标线**：达标线由 HR 给的合格/不合格锚点样本推导（`pass_line ± band` 三段带），
   而不是全局写死一个 60 分。
   ⚠️ **三条线必须分清**（2026-09-26 补，代码依据 `app/contracts/job_config.py:174-181`）：`pass_line=60` + `band_width=5` 是**兜底三段带**，
   而配置里**显式写了 `auto_pass_line=70`，它优先** —— 该文件里两个函数决定了生效值：
   `pass_threshold() = auto_pass_line 显式存在 ? auto_pass_line : pass_line + band_width`（⇒ **生效 = 70**），
   `reject_threshold() = pass_line − band_width`（⇒ 55）。
   本岗位**实际生效的三档 = ≥70 自动达标 / [55,70) 转人工 / <55 进「可自动淘汰」判据**（还要再过双通道，见下条）。
   **不得读成「线以下都淘汰」**：实测 310 份里分数 <55 的有 **179 份**，其中只有 **33 份**真正 `auto_reject`，
   其余 **146 份转人工** —— 这是纪律「宁可多转人工、不接受不可逆的自动淘汰」的代码级实现。
   完整口径与原始数见 [`docs/口径定稿-决策记录-20260926.md`](docs/口径定稿-决策记录-20260926.md)。

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
判定层            三段带（**生效达标线 = 显式 auto_pass_line**，缺省才回退 pass_line + band）+ 双通道（整体评估 × 规则分）+ 冲突检测
```

**数据契约先行**：三份 JSON Schema（简历解析 / 岗位配置 / 评估）+ 五段模型输出契约，全部由 Pydantic 模型导出，
代码与 schema 漂移会被测试卡住。详见 [`schemas/README.md`](schemas/README.md)。

## 三、当前进度

| 任务 | 状态 | 说明 |
|---|---|---|
| 任务 1 数据契约三件套 | ✅ 完成并验收 | 8 个 JSON Schema（Pydantic 导出）、四级证据锚定、校验四态（ok/repaired/degraded/failed）、三段带双通道判定 |
| 任务 2 M0 最小闭环 | ✅ 完成（CLI + Web UI + exe） | 落库（PG + pgvector）、状态机、幂等与断点续跑；FastAPI 接口 + Vue3（全局构建，无打包步骤）+ 打包成单文件 exe |
| 任务 3 锚点标定达标线 | ✅ 已实现并实跑 | `tools/calibrate.py`：合格组≥3、不合格组≥2、两组必须分得开；达标线 = 合格组下限 |
| 任务 4 规则分稳定性 | ✅ 已实现并实测 | `scoring.labeling_repeats`（多次标注取中位数 + 维度多数投票）+ `dimension_aggregation` |
| 任务 5 时间线 / 字段级校验 | ✅ 已完成 | `app/text/timeline.py`：起止矛盾 / 同类型经历重叠 / 经历空档 / 未来日期；字段级异常见 `app/text/fields.py`（6 类检查，由 `app/text/quality.py` 调用，非占位） |

### 最新状态（2026-09-21 实测，2026-09-25 复核）

> 下面的任务表与实测是**历史节点**（任务 1~5 的验收记录）；系统此后进入“两段流程 + 全量重跑”阶段，现状见本节。

| 项 | 现状 |
|---|---|
| 口径 | 判定分 = 基础 60 + 加分 25（Σq/√n 批内分位）− 空话率^1.5 × 30；`pass_line=60` / `auto_pass_line=70` / 带 5，**设定值 = 生效值**（[55,70) 转人工）。**三条线别名**：`pass_line` 60（兜底线）· `auto_pass_line` 70（**生效达标线，显式优先**）· `band_width` 5（兜底带）→ 生效三档 **≥70 / [55,70) / <55**；不可读成「线以下都淘汰」（<55 的 179 份里只有 33 份自动淘汰） |
| 全量重跑 | 行政岗 310 份（提示词 p1.3）：`auto_pass` 74 / `manual_review` 203 / `auto_reject` 33 |
| 两段流程 | 第一段 `tools/calibrate.py` 定线、第二段 `tools/derive_basic_items.py` 定及格项（HR 勾 `hr_tick` 回灌 `basic_item_label`）；**等真实 HR 锚点**才能正式跑 |
| 测试 | `pytest -q`：**2026-09-25 实测 203 collected = 203 executed，全绿（34.9s）**，日志 `out/scratch/pytest-20260925.log`。判据是「collect 计数 = 执行数 且全绿」，**数值不钉死**（随代码变动；文档旧值 193 即由此而来） |
| **口径定稿（2026-09-26）** | **数值口径一字未动**；四判据交集 = **空集**（原始数见决策记录）⇒ 结论：**当前口径下自动校准不可用，真实动线走人工锚点**（与 09-18 §十一 同结论）。**不放宽任何判据**（理由：3.2 是同配置重复跑实测的分数极差上限、0.69 差 0.01 但单放任一条都凑不出可行点） |
| 未定稿（待锚点） | 加分项变体 γ / scale 与 \|ρ(条数)\|（见 `docs/定稿决策-20260918.md`）；`auto_pass_line` 是否由 70 下探到 ~62.3（=保留前 30%）**属待 HR 锚点决策**，见 `docs/口径定稿-决策记录-20260926.md` §4 |
| 模型路线 | 双路线：**Jev 线（外循环：问法 / 阈值 / 及格项）优先**，Laya 线（权重微调）兜底；生产走线上 API（目标机可能 Win7） |
| 文档入口 | 单轮记录见 `docs/README.md`；**口径定稿与依据 = `docs/口径定稿-决策记录-20260926.md`**；交接口径已并入该目录 |

### 任务 3 / 4 / 5 的实测结果（巨石教育·少儿计算机竞赛老师岗）

| 项 | 实测 |
|---|---|
| 硬门槛规则化（任务 3 的前置） | 模型会把写着"本科""网络空间安全"的候选人判成"未提及学历"并据此自动淘汰；改为**规则层关键词预检**（pass/fail/unknown）后不再发生，证据不足一律转人工 |
| 综合达标（口径修正） | 用户澄清：本科/四级/竞赛是**综合**标准，不是逐条否决。四级降为「英语能力」维度(5分)+加分项；学历/专业保留门槛但加 `overrides` 豁免（竞赛维度 ≥24/30 → 不自动淘汰、转人工）。回归测试覆盖"专科+ACM 金牌 → 转人工" |
| 锚点标定（任务 3） | 本表是**老师岗**（`config/job_jushi_teacher.json`，现 `config_version=8`）：合格组下限 **41.1**（zhehuixu94-sudo 60.0 / resume_001 48.9 / resume_004 41.1），不合格组上限 35.2（resume_a）→ 分得开 → 达标线 **41.1**（v7 值），`pass_line_source=anchor_calibrated`。**注**：43.3 是 v5 的旧值，同一批锚点跨配置版本漂移 6.6 分，见 `docs/标定接线修复-20260918.md` |
| 规则分稳定性（任务 4） | 同一份简历重复跑：单次标注 **极差 10.0~16.6**；`top2_mean` 单独用基本无效（14.4 不变、resume_a反而变差）→ 改为 **3 次标注取中位数 + 维度多数投票**：zhehuixu94-sudo 10.0 → **0.0**、resume_001 0.0、resume_a 11.0 → 6.6（其分数在 9.9~16.5 摆动，但处置恒为淘汰） |
| 时间线自洽（任务 5） | 纯规则，实测抓到样例里的「2024/4 -- 2023/12」起止矛盾、同类型经历重叠、经历空档、未来日期；只提示不拦截 |

### 任务 2 的实测结果（10 份简历 × 安全服务岗，2~3 并发）

| 验收项 | 结果 |
|---|---|
| 10 份批量跑通 | 10/10 落库并出报告（4 份真实简历 + 6 份合成简历），75 条证据 span 中 71 条锚定成功（94.7%） |
| 中途 kill 能续跑 | 实测 kill 进程时库里有 2 行 `parsing`、1 行 `evaluating`、0 份完成评估；重跑同一条命令后全部 `evaluated`，且**不重复计费**（已解析的简历跳过解析、已有评分的跳过评估）、失败次数不被误加 |
| 每份报告证据可点开 | 11 个 HTML（10 份单人报告 + 批次矩阵）；单人报告 12 个证据链接、7 处高亮锚点，点击链接跳到原文并高亮（已用浏览器实测跳转） |
| 处置分布 | 转人工 2 / 自动淘汰 8（对口简历 63.9 分落在线内转人工；6 份游戏开发简历方向不符） |

> 顺带暴露一个真实问题：同一份对口简历重复跑会落在达标线两侧（单跑 66.1「达标」vs 批量 63.9「边缘」）——
> 这正是任务 4（规则分稳定性）要解决的，在切 top2 平均并复测之前，不许进自动淘汰。

### 任务 1 的实测结果（4 份真实简历 × 安全服务岗）

- 8/8 schema 与代码一致；6/6 岗位配置过契约
- 4/4 简历跑通解析契约，Pydantic 与导出的 JSON Schema **双验通过**
- 证据锚定 27/28（96.4%），span 切片逐条回原文核对 **0 处错位**
- 完整评估样例：达标 / `auto_pass`，规则分 66.1（达标线 60 ± 5），冲突 0
- 降级四态离线自测 7/7 符合预期；`pytest` 20 passed

## 四、快速开始

```bash
git clone <repo> && cd resume-screener
python -m venv .venv                                   # 需 Python 3.11（本机 .venv 实测 3.11.16）
.venv/Scripts/python -m pip install -r requirements-dev.txt   # 运行时 + 测试依赖；只跑系统装 requirements.txt
cp .env.example .env                                   # 填 SILICONFLOW_API_KEY 与 PG*（.env 已 gitignore，绝不入库）

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

## 四点五、Web 界面与 exe（给 HR 用的形态）

```bash
# 开发态：起服务（自动打开浏览器）
.venv/Scripts/python launcher.py --port 8765

# 打包成单文件 exe（约 45 MB，双击即用，不需要装 Python）
build_exe.bat            # 或：
.venv/Scripts/python -m PyInstaller --onefile --name ResumeScreener \
  --add-data "web;web" --add-data "config;config" --add-data "schemas;schemas" \
  --hidden-import uvicorn.logging --hidden-import uvicorn.loops.auto \
  --hidden-import uvicorn.protocols.http.auto --hidden-import uvicorn.protocols.websockets.auto \
  --hidden-import uvicorn.lifespan.on --collect-submodules psycopg --collect-submodules pymupdf launcher.py
```

**exe 的行为**：在 `127.0.0.1` 挑一个空闲端口起服务（**不监听局域网**，简历不会外泄）→ 自动打开浏览器 →
第一次运行会把岗位配置释放到 exe 旁边的 `config/`（用户可直接改 JSON 加岗位，不用重新打包）→
控制台显示数据库连接状态与访问地址，关掉窗口即停止。

**界面三块**：① 上传与运行（拖拽上传 pdf/docx/txt → 入队 → 设并发开始评估 → 状态机进度条）；
② 队列看板（自动达标 / 转人工 / 自动淘汰 三列，卡片显示分数、置信度、匹配度、证据覆盖率、冲突类型，
点开是详情抽屉：硬门槛结论 + 维度分 + 内嵌的报告 iframe，证据依旧可点开跳原文）；
③ 岗位配置（维度权重、硬门槛、豁免规则、标定状态一眼可见）。

**部署前提**：需要一个能连上的 PostgreSQL（本项目开发用容器 `resume_pg`），以及一个硅基流动 API key
（环境变量 `SILICONFLOW_API_KEY`）。两者缺一 exe 照样启动，界面上会明确显示"数据库不可用/未配置密钥"和排查提示，
而不是黑框闪退。

## 五、批处理（M0 闭环）

```bash
# 1) 建表（幂等）
.venv/Scripts/python tools/init_db.py --show

# 2) 入队：把简历挂到某个岗位配置下（文件或目录都行）
.venv/Scripts/python -m app.batch enqueue --job config/job_secops.json --resumes "简历目录/"

# 3) 跑：workers 是并发 worker 数；重复执行即断点续跑
.venv/Scripts/python -m app.batch run --job config/job_secops.json --workers 3
.venv/Scripts/python -m app.batch status --job config/job_secops.json     # 看状态机分布
.venv/Scripts/python -m app.batch retry  --job config/job_secops.json     # 卡住/失败的行恢复成可重跑

# 4) 出报告：每份简历一个 HTML（证据可点开原文）+ 批次矩阵
.venv/Scripts/python -m app.batch report --job config/job_secops.json --outdir out/reports
```

**状态机**（挂在 `application` 上，进度即状态，断点续跑就查它）：

```
pending_parse → parsing → parsed → evaluating → evaluated → reviewed
                    ↓ 失败                ↓ 失败
              parse_failed          eval_failed        （attempts 到上限后停住等人工）
```

- **幂等**：`resume` 按文件 sha256 去重、`resume_parse`/`evaluation` 唯一键 + `ON CONFLICT`，重复执行不会多出评分行。
- **并发安全**：worker 用 `FOR UPDATE SKIP LOCKED` + `locked_at` 原子抢占；处理中不释放锁，
  被 kill 留下的锁超过 `--lock-timeout` 分钟视为原 worker 已死、可回收（kill 后立刻续跑用 `--lock-timeout 0`）。
- **不在挂掉的模型上白等**：开跑前 `probe()` 探活主干，失败自动切 `--fallback-model`（实测主干端点会整段时间无响应）。

**存储**：容器 `resume_pg`（`pgvector/pgvector:pg18`，主机 127.0.0.1:5433，库 `resume_screener`，用户 `resume_app`）。
表（`app/store/schema.sql` 共 **14 张**）：`job_config`（配置版本化）· `resume`（简历实体）· `application`（应聘关系 + 状态机）·
`resume_parse`（解析结果）· `evidence_span`（证据 span 索引）· `evaluation`（评分行，带 model_id/prompt_hash/config_version）·
`audit_log`（append-only）· `resume_chunk`（检索层，v1 不写）·
`review_decision` · `spot_check_batch` · `spot_check_item` · `calibration` · `basic_item_label` · `stability_check`（抽检闭环 / 校准 / 两段流程用）。
DSN 走环境变量 `PG_DSN` / `PGHOST` `PGPORT` `PGUSER` `PGPASSWORD` `PGDATABASE`，仓库里没有任何凭据。

**端口口径（已知坑，务必区分「代码默认」与「部署值」）**：部署一律用 **5433** —— 容器 `pgvector/pgvector:pg18` 把宿主 5433 映射到容器内 5432，
`run.bat`（`PGPORT_HOST=5433`，未设时自动导出 `PGPORT`）与 `.env.example` 都是 5433；
而 `app/store/db.py` 的代码默认值是 **5432**（给本机原生 PostgreSQL 用）。**直接跑 `python tools/init_db.py` 且不设 `PGPORT`/`PG_DSN` 会连 5432 而报连不上** —— 走 `run.bat` 或先 `. .env` 即不会踩。

## 六、目录结构

```
app/
  api.py       FastAPI 接口（上传/运行/看板/报告/健康检查）
  paths.py     路径解析（源码运行与 exe 两种形态）
  contracts/   三件套 + 五段模型输出契约（Pydantic）+ 清洗层（枚举归一/垃圾值剔除/未知字段留痕）
  text/        抽取 · 规范化与偏移映射 · 四级锚定 · 质量门禁
  llm/         硅基流动客户端（含探活）· 五段 prompt · 校验-重试-降级引擎 · 清洗器
  pipeline/    解析段 · 标注/判定/匹配度/面试题 + 规则层与三段带判定
  store/       Postgres schema + repository（状态机 / 幂等 / 审计日志）
  batch.py     批处理 CLI（入队 / 跑 / 续跑 / 出报告）
  report.py    单人报告（证据可点开）+ 批次矩阵
web/           Vue3 前端（全局构建，无打包步骤，Chrome 109 兼容；vue 已本地内置，离线可用）
launcher.py    exe 启动器（挑端口 → 起服务 → 开浏览器）
build_exe.bat  一键打包 exe
schemas/       由 Pydantic 导出的 JSON Schema（含 5 段模型输出契约）
config/        9 个岗位配置（硬门槛 / 维度锚点 / 加分项关键词 / 达标线与标定）+ snapshots/（口径快照：内嵌配置逐字 + 代码指纹 + 批次指纹）；当前只有 hr_admin 套用最新口径
tools/         45 个脚本：导出 schema / 迁移配置 / 契约验收 / 标定 / 及格项推导 / 抽检 / 收口校验 / 沙箱守卫
tests/         契约、锚定、判定策略、抽检与校准的回归测试（26 个测试文件）
docs/          单轮记录与定稿决策（docs/README.md 是索引）
experiments/   Jev vs Laya 选型实验（探针脚本 + results/ 原始结果）
local/         真实简历清单与内部文档（gitignore，不入库）
out/           报告与运行产物、分析脚本（gitignore，不入库）

顶层文件：requirements.txt / requirements-dev.txt（运行时 / 运行时+测试依赖）· .env.example（环境变量样例）·
run.bat（Windows 一键：db → env → initdb → check，每步可单独重跑）· build_exe.bat · launcher.py · ResumeScreener.spec
```

## 七、不提交什么

- `local/`（真实简历路径清单）、`out/`（报告与运行产物，含真实简历原文）、`.venv/`
- 任何密钥：代码只从环境变量 `SILICONFLOW_API_KEY` 或本机密钥文件读，仓库里没有 key

## 八、免责声明

本系统是**辅助工具**，输出的判定与分数**仅供参考，不参与录用决策**，结论必须由 HR 复核。
简历含个人信息：默认本地留存，对外发送前必须脱敏。

## 九、技术栈

Python 3.11 · Pydantic v2 · JSON Schema · psycopg 3 · PostgreSQL + pgvector（容器 pgvector/pgvector:pg18）·
硅基流动（LLM，OpenAI 兼容 HTTP）· FastAPI + Vue3（**全局构建**：`web/vendor/vue.global.prod.js`，无 Vite、无 package.json、无打包步骤，Chrome 109 兼容）· PyInstaller（单文件 exe）

> 说明：**当前代码里没有 Embedding / Reranker**（`grep -rn 'embedding\|rerank' app/` 无命中）；简历是 1~2 页短文档，整份进上下文即可，检索层（`resume_chunk` 表）v1 不写。此前 README 的「Embedding / Reranker」「Vue3/Vite」两处与技术栈实况不符，已于 2026-09-25 修正。
