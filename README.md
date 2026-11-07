# 简历初筛系统（Resume Screener）

> 新技术在数据回流和意图识别一次提效尝试

个人项目（网络空间安全本科毕业设计方向 / 求职作品集）。

一句话说清这套系统在试什么：**把「这个人对不对口」从关键词匹配，换成可判定的结构化意图；把 HR 每一次复核，变成下一轮的口径数据。**
模型只负责给标签和理由，判定权在规则层与 HR 手里——定位是**排除器，不是择优器**：成功标准不是「排得准」，而是「没把该留的人刷掉」。

---

## 一、两个提效点

### 1. 意图识别：把「对不对口」拆成可判定的结构

HR 的原话往往是「这人还行」「方向不太对」，落到系统里必须变成可判定的东西。这套系统把它拆成三层，每层都能指回原文：

| 层 | 判定什么 | 由谁判 | 输出 |
|---|---|---|---|
| 硬门槛 | 学历 / 专业 / 证书这类硬条件 | **规则层关键词预检** | `pass` / `fail` / `unknown`（unknown 一律不淘汰） |
| 方向匹配 | 能力画像与岗位方向是否对口 | 模型 | 对口 / 部分对口 / 方向不符 |
| 维度锚点 | 每项能力有多相关、多难 | 模型打标签，**规则层算分** | 相关性系数 × 难度系数 → 维度分 |

三条约束是这套设计的核心：

- **证据必须锚定**：每条能力项都带原文片段 + 字符级 span，四级降级（精确 → 空白/分隔符不敏感 → 模糊 → 失败），锚定失败会留痕，且**不允许作为自动淘汰的唯一依据**。
- **判定分不给模型**：模型只给「相关性强/中/弱/无关、难度高/中/低、维度」三组标签，分数由规则层算。同一份材料重复跑，单次标注极差 **10.0~16.6 分**；改成 3 次标注取中位数 + 维度多数投票后降到 **0.0**（`resume_a` 仍 6.6，但其处置恒为淘汰）。
- **综合达标，不是逐条否决**：本科 / 四级 / 竞赛是综合标准。早期模型会把写着「本科」「网络空间安全」的候选人判成「未提及学历」并据此自动淘汰，改成规则层预检后不再发生；同时加 `overrides` 豁免（竞赛维度 ≥24/30 → 不自动淘汰，转人工）。

### 2. 数据回流：把复核结论变成下一轮的线

系统不预设「60 分算过」。达标线与及格项来自 HR 给的样本，判完再把复核结论回流：

```
HR 给几份及格 / 不及格样本
      ↓  tools/calibrate.py            定达标线（合格组下限，两组必须分得开）
      ↓  tools/derive_basic_items.py   推导及格项
HR 在清单里勾 hr_tick (y/n)  →  回灌 basic_item_label
      ↓
每份简历的判定 + 复核结论（review_decision）→ 下一轮标定与回归的依据
```

几条口径是硬的：

- **回灌判据只有一处入口**：`auto_reject` + 复核为 `reject` + 复核人/时间非空，三者同时成立才置真；其余一律不置真，并强制 `calibration_eligible=false`。
- **抽检样本必须冻结**：回灌只认冻结的抽检样本文件，否则抽检结论没有抽样学意义。
- **自动校准经实测被否掉，不是没做**：四判据交集是**空集**（原始数见 `docs/口径定稿-决策记录-20260926.md`），结论是「当前口径下自动校准不可用，真实动线走人工锚点」。没有为了凑出一组好看参数去放宽判据。

---

## 二、为什么是这两处

HR 拿到 200 份简历要在一晚上筛完，实际做法是搜关键词，结果是**注水简历混进来、对口简历被漏掉**。两个卡点正好对应上面两个提效点：

- 靠关键词 → 判不准「对不对口」，也说不清「为什么不对口」（意图识别这一层要解决）
- 靠个人经验定线 → 换一个人、换一个岗位，口径就漂了（数据回流这一层要解决）

**三档处置**：自动达标 / 转人工 / 自动淘汰，其中「转人工」是默认答案。触发转人工的条件包括：模型整体评估与规则分打架、置信度低于阈值、匹配度方向冲突、模型输出降级、证据没锚定成功。

---

## 三、架构

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
判定层            三段带（生效达标线 = 显式 auto_pass_line，缺省才回退 pass_line + band）
                  + 双通道（整体评估 × 规则分）+ 冲突检测
```

**数据契约先行**：三份 JSON Schema（简历解析 / 岗位配置 / 评估）+ 五段模型输出契约，全部由 Pydantic 模型导出，代码与 schema 漂移会被测试卡住。模型返回分四态：`ok` / `repaired` / `degraded` / `failed`，后两态强制转人工。

**三条线的口径**（最容易读错的地方）：`pass_line=60` 是兜底线、`auto_pass_line=70` 是**生效达标线（显式优先）**、`band_width=5` 是兜底带 ⇒ 实际三档 **≥70 / [55,70) / <55**。
**不得读成「线以下都淘汰」**：实测 310 份里分数 <55 的有 **179 份**，其中只有 **33 份**真正 `auto_reject`，其余 **146 份转人工**。

---

## 四、跑出来是什么样（实测，不是设计图）

### 行政岗 310 份全量重跑（提示词 p1.3）

| 项 | 数 |
|---|---|
| 自动达标 `auto_pass` | 74 |
| 转人工 `manual_review` | 203 |
| 自动淘汰 `auto_reject` | 33 |

### 任务 2｜M0 最小闭环（10 份简历 × 安全服务岗，2~3 并发）

| 验收项 | 结果 |
|---|---|
| 批量跑通 | 10/10 落库并出报告（4 份真实 + 6 份合成），75 条证据 span 中 71 条锚定成功（94.7%） |
| 中途 kill 能续跑 | kill 时有 2 行 `parsing`、1 行 `evaluating`；重跑同一条命令后全部 `evaluated`，**不重复计费**（已解析跳过解析、已有评分跳过评估） |
| 证据可点开 | 11 个 HTML（10 份单人报告 + 批次矩阵）；单人报告 12 个证据链接、7 处高亮锚点，点击跳原文并高亮（浏览器实测） |
| 处置分布 | 转人工 2 / 自动淘汰 8 |

> 顺带暴露一个真问题：同一份对口简历单跑 66.1「达标」、批量 63.9「边缘」，落在线两侧——这正是后来做规则分稳定性（3 次标注取中位数）的原因。

### 任务 3 / 4 / 5（巨石教育·少儿计算机竞赛老师岗）

| 项 | 实测 |
|---|---|
| 锚点标定 | 合格组下限 **41.1**（示例候选人 60.0 / resume_001 48.9 / resume_004 41.1），不合格组上限 35.2 → 分得开 → 达标线 41.1，`pass_line_source=anchor_calibrated`。**43.3 是 v5 旧值**，同一批锚点跨配置版本漂移 6.6 分 |
| 规则分稳定性 | 单次标注极差 10.0~16.6 → 3 次中位数 + 维度多数投票后 **0.0 / 0.0 / 6.6** |
| 时间线自洽 | 纯规则，实测抓到「2024/4 -- 2023/12」起止矛盾、同类型经历重叠、经历空档、未来日期；只提示不拦截 |

### 任务 1（4 份真实简历 × 安全服务岗）

8/8 schema 与代码一致 · 6/6 岗位配置过契约 · 4/4 简历解析双验通过 · 证据锚定 27/28（96.4%），span 逐条回原文核对 **0 处错位** · 降级四态离线自测 7/7。

### 测试

`pytest -q`：2026-09-25 实测 **203 collected = 203 executed，全绿（34.9s）**。判据是「collect 计数 = 执行数且全绿」，数值不钉死。

---

## 五、快速开始

```bash
git clone <repo> && cd resume-screener
python -m venv .venv                                   # 需 Python 3.11
.venv/Scripts/python -m pip install -r requirements-dev.txt   # 运行时 + 测试依赖
cp .env.example .env                                   # 填 SILICONFLOW_API_KEY 与 PG*（.env 已 gitignore）

export SILICONFLOW_API_KEY=sk-xxx                      # 密钥只从环境变量读

# 离线自检（不花 token）
.venv/Scripts/python -m pytest tests -q
.venv/Scripts/python tools/validate_contracts.py --no-live

# 带真实简历跑全量验收（会调用模型）
cp local/resumes.json.example local/resumes.json       # 填自己的简历路径（local/ 已 gitignore）
.venv/Scripts/python tools/validate_contracts.py
```

模型：默认 `Qwen/Qwen3.5-122B-A10B`（关思考模式），降本档 `Qwen/Qwen3.5-35B-A3B`，`--model` 可覆盖。平台为硅基流动（OpenAI 兼容 HTTP，无第三方 SDK 依赖）。

## 六、Web 界面与 exe

```bash
.venv/Scripts/python launcher.py --port 8765    # 开发态，自动开浏览器
build_exe.bat                                   # 打包成单文件 exe（约 45 MB，双击即用）
```

**exe 行为**：在 `127.0.0.1` 挑空闲端口起服务（**不监听局域网**）→ 自动开浏览器 → 首次运行把岗位配置释放到 exe 旁的 `config/`（改 JSON 即可加岗位，不用重新打包）→ 关窗口即停。

**界面三块**：① 上传与运行（拖拽 pdf/docx/txt → 入队 → 设并发 → 状态机进度条）；② 队列看板（自动达标 / 转人工 / 自动淘汰 三列，卡片显示分数、置信度、匹配度、证据覆盖率、冲突类型，点开是详情抽屉：硬门槛结论 + 维度分 + 内嵌报告，证据仍可跳原文）；③ 岗位配置（维度权重、硬门槛、豁免规则、标定状态）。

**部署前提**：一个可连的 PostgreSQL + 一个硅基流动 API key。两者缺一 exe 照样启动，界面明确显示「数据库不可用 / 未配置密钥」和排查提示，不黑框闪退。

## 七、批处理

```bash
.venv/Scripts/python tools/init_db.py --show                       # 建表（幂等）
.venv/Scripts/python -m app.batch enqueue --job config/job_secops.json --resumes "简历目录/"
.venv/Scripts/python -m app.batch run     --job config/job_secops.json --workers 3   # 重复执行即断点续跑
.venv/Scripts/python -m app.batch status  --job config/job_secops.json
.venv/Scripts/python -m app.batch report  --job config/job_secops.json --outdir out/reports
```

状态机：`pending_parse → parsing → parsed → evaluating → evaluated → reviewed`，失败分岔到 `parse_failed` / `eval_failed`，attempts 到上限后停住等人工。

- **幂等**：`resume` 按文件 sha256 去重，`resume_parse` / `evaluation` 唯一键 + `ON CONFLICT`，重复执行不会多出评分行。
- **并发安全**：`FOR UPDATE SKIP LOCKED` + `locked_at` 原子抢占；被 kill 留下的锁超过 `--lock-timeout` 视为原 worker 已死、可回收（kill 后立刻续跑用 `--lock-timeout 0`）。
- **不在挂掉的模型上白等**：开跑前 `probe()` 探活主干，失败自动切 `--fallback-model`（实测主干端点会整段时间无响应）。

**存储**：`pgvector/pgvector:pg18` 容器，宿主 127.0.0.1:5433，库 `resume_screener`。共 **14 张表**：`job_config` · `resume` · `application`（状态机）· `resume_parse` · `evidence_span` · `evaluation` · `audit_log`（append-only）· `resume_chunk`（检索层，v1 不写）· `review_decision` · `spot_check_batch` · `spot_check_item` · `calibration` · `basic_item_label` · `stability_check`。凭据只走环境变量。

**端口口径（已知坑）**：部署一律 **5433**（`run.bat`、`.env.example` 都是），而 `app/store/db.py` 的代码默认是 **5432**（给原生 PostgreSQL 用）。直接跑 `python tools/init_db.py` 且不设 `PGPORT` 会连 5432 报连不上——走 `run.bat` 或先 `. .env`。

## 八、目录结构

```
app/
  api.py       FastAPI 接口（上传/运行/看板/报告/健康检查）
  paths.py     路径解析（源码运行与 exe 两种形态）
  contracts/   三件套 + 五段模型输出契约（Pydantic）+ 清洗层
  text/        抽取 · 规范化与偏移映射 · 四级锚定 · 质量门禁
  llm/         硅基流动客户端（含探活）· 五段 prompt · 校验-重试-降级引擎
  pipeline/    解析 · 标注/判定/匹配度/面试题 + 规则层与三段带判定
  store/       Postgres schema + repository（状态机 / 幂等 / 审计）
  batch.py     批处理 CLI（入队 / 跑 / 续跑 / 出报告）
  report.py    单人报告（证据可点开）+ 批次矩阵
web/           Vue3（全局构建，无打包步骤，Chrome 109 兼容，离线可用）
launcher.py    exe 启动器（挑端口 → 起服务 → 开浏览器）
build_exe.bat  一键打包 exe
schemas/       Pydantic 导出的 JSON Schema（含 5 段模型输出契约）
config/        9 个岗位配置（硬门槛 / 维度锚点 / 加分项 / 达标线与标定）+ snapshots/（口径快照）
tools/         45 个脚本：导出 schema / 迁移配置 / 契约验收 / 标定 / 及格项推导 / 抽检 / 收口校验
tests/         26 个测试文件：契约、锚定、判定策略、抽检与校准
docs/          单轮记录与定稿决策（docs/README.md 是索引）
experiments/   Jev vs Laya 选型实验（探针脚本 + results/ 原始结果）
local/         真实简历清单与内部文档（gitignore，不入库）
out/           报告与运行产物、分析脚本（gitignore，不入库）
```

顶层文件：`requirements.txt` / `requirements-dev.txt` · `.env.example` · `run.bat`（Windows 一键：db → env → initdb → check）· `build_exe.bat` · `launcher.py` · `ResumeScreener.spec`

## 九、边界：这套系统不做什么

- **不自动淘汰没把握的人**：证据没锚定、模型降级、双通道冲突、置信度不足 → 全部转人工。系统里没有「一键淘汰」。
- **不自动定线**：四判据交集空集，自动校准在当前口径下不可用，真实动线依赖人工锚点（见 `docs/口径定稿-决策记录-20260926.md`）。
- **不做语义检索层**：代码里没有 Embedding / Reranker（`grep -rn 'embedding\|rerank' app/` 无命中）。简历是 1~2 页短文档，整份进上下文即可；`resume_chunk` 表 v1 不写。
- **不做择优**：它回答的是「该不该留在这里看」，不是「谁最优秀」。
- **不替代 HR**：输出仅供参考，不参与录用决策（见第十一节）。

## 十、技术栈

Python 3.11 · Pydantic v2 · JSON Schema · psycopg 3 · PostgreSQL + pgvector（容器 `pgvector/pgvector:pg18`）· 硅基流动（LLM，OpenAI 兼容 HTTP）· FastAPI + Vue3（**全局构建**：`web/vendor/vue.global.prod.js`，无 Vite、无 package.json、无打包步骤，Chrome 109 兼容）· PyInstaller（单文件 exe）

## 十一、免责声明与不提交什么

本系统是**辅助工具**，输出的判定与分数**仅供参考，不参与录用决策**，结论必须由 HR 复核。简历含个人信息：默认本地留存，对外发送前必须脱敏。

仓库不提交：`local/`（真实简历路径清单）、`out/`（报告与运行产物，含真实简历原文）、`.venv/`、任何密钥（代码只从环境变量 `SILICONFLOW_API_KEY` 或本机密钥文件读）。公开版本中样本姓名已统一替换为占位名。
