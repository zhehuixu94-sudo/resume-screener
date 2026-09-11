# 简历初筛系统（Resume Screener）

> 用 LLM 帮 HR 把一批简历判成「达标 / 转人工 / 淘汰」，**每个结论都能点开原文证据**。
> 定位是**排除器，不是择优器**：成功标准不是「排得准」，而是「没把该留的人刷掉」。

个人项目（网络空间安全本科毕业设计方向 / 求职作品集），当前处于**任务 1、任务 2（M0 闭环）已完成并实测验收的阶段**。

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
| 任务 2 M0 最小闭环 | ✅ 完成（CLI + Web UI + exe） | 落库（PG + pgvector）、状态机、幂等与断点续跑；FastAPI 接口 + Vue3 前端 + 打包成单文件 exe |
| 任务 3 锚点标定达标线 | ✅ 已实现并实跑 | `tools/calibrate.py`：合格组≥3、不合格组≥2、两组必须分得开；达标线 = 合格组下限 |
| 任务 4 规则分稳定性 | ✅ 已实现并实测 | `scoring.labeling_repeats`（多次标注取中位数 + 维度多数投票）+ `dimension_aggregation` |
| 任务 5 时间线 / 字段级校验 | 🚧 时间线已完成 | `app/text/timeline.py`：起止矛盾 / 同类型经历重叠 / 经历空档 / 未来日期；字段级异常仍是占位 |

### 任务 3 / 4 / 5 的实测结果（巨石教育·少儿计算机竞赛老师岗）

| 项 | 实测 |
|---|---|
| 硬门槛规则化（任务 3 的前置） | 模型会把写着"本科""网络空间安全"的候选人判成"未提及学历"并据此自动淘汰；改为**规则层关键词预检**（pass/fail/unknown）后不再发生，证据不足一律转人工 |
| 综合达标（口径修正） | 用户澄清：本科/四级/竞赛是**综合**标准，不是逐条否决。四级降为「英语能力」维度(5分)+加分项；学历/专业保留门槛但加 `overrides` 豁免（竞赛维度 ≥24/30 → 不自动淘汰、转人工）。回归测试覆盖"专科+ACM 金牌 → 转人工" |
| 锚点标定（任务 3） | 合格组下限 43.3（示例候选人 60.0 / resume_001 45.5 / resume_004 43.3），不合格组上限 35.2（候选人B）→ 分得开 → 达标线 43.3，`pass_line_source=anchor_calibrated` |
| 规则分稳定性（任务 4） | 同一份简历重复跑：单次标注 **极差 10.0~16.6**；`top2_mean` 单独用基本无效（14.4 不变、候选人B反而变差）→ 改为 **3 次标注取中位数 + 维度多数投票**：示例候选人 10.0 → **0.0**、resume_001 0.0、候选人B 11.0 → 6.6（其分数在 9.9~16.5 摆动，但处置恒为淘汰） |
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
表：`job_config`（配置版本化）· `resume`（简历实体）· `application`（应聘关系 + 状态机）·
`resume_parse`（解析结果）· `evidence_span`（证据 span 索引）· `evaluation`（评分行，带 model_id/prompt_hash/config_version）·
`audit_log`（append-only）· `resume_chunk`（检索层，v1 不写）。
DSN 走环境变量 `PG_DSN` / `PGHOST` `PGPORT` `PGUSER` `PGPASSWORD` `PGDATABASE`，仓库里没有任何凭据。

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
config/        6 个岗位配置（硬门槛 / 维度锚点 / 加分项关键词 / 达标线与标定）
tools/         导出 schema · 迁移配置 · 契约验收
tests/         契约、锚定、判定策略的回归测试
```

## 七、不提交什么

- `local/`（真实简历路径清单）、`out/`（报告与运行产物，含真实简历原文）、`.venv/`
- 任何密钥：代码只从环境变量 `SILICONFLOW_API_KEY` 或本机密钥文件读，仓库里没有 key

## 八、免责声明

本系统是**辅助工具**，输出的判定与分数**仅供参考，不参与录用决策**，结论必须由 HR 复核。
简历含个人信息：默认本地留存，对外发送前必须脱敏。

## 九、技术栈

Python 3.11 · Pydantic v2 · JSON Schema · psycopg 3 · PostgreSQL + pgvector（容器 pgvector/pgvector:pg18）·
硅基流动（LLM / Embedding / Reranker）· FastAPI + Vue3/Vite（构建 target Chrome 109，客户端层待做）
