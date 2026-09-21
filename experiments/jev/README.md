# Jev vs Laya 选型实验（2026-09-20 / 09-21）

**这是什么**：为「外循环（Jev 线）」vs「权重微调（Laya 线）」的路线决策做的一组离线对照实验。
结论已写进桌面《简历初筛_定稿交接-20260915.md》§十；**本节目录只保存可复现的脚本与原始结果**，不参与生产口径。

**语料**：`local/samples_all` 100 份合成简历（689–1262 字符，模板化；18 份共用同一句荣誉奖励）。
**真值**：8 个可字面验证的 noul + 3 个安全概念措辞变体，共 11 问/份。
**隐私**：脚本从不打印简历正文，只输出判定与聚合指标；结果 json 里只有文件名与分数。

## 脚本

| 脚本 | 模型 | 测什么 |
|---|---|---|
| `probe_resumes.py` | Laya（本地） | 100 份 × 4 问，第一版探针（指标退化，仅留档） |
| `probe2_literal.py` | Laya（本地） | 100 份 × 8+3 问，出 accuracy / ECE |
| `probe_jev.py` | Jev（云端） | **同一批语料、同一套问法、同一套真值**，与 Laya 直接可比 |
| `probe_sensitivity.py` | Jev（云端） | 3 概念 × 4 问法（宽泛/基线/严格/英文）= 12 问，**外循环的地基** |
| `probe3_wording.py` | Laya（本地） | 措辞/语言对照（20 份 × 3 变体），排除「是我措辞的问题」 |

## 怎么跑

```bash
# Jev（云端）—— key 走环境变量，绝不写进代码/仓库
export TYPESAFE_API_KEY=...            # 只为本机会话设置；本机存在 D:\Hermes\secrets\typesafe.env
python probe_jev.py
python probe_sensitivity.py

# Laya（本地，CPU 版 torch 即可）—— 需要先下载权重（见下）
pip install laya typesafe-sdk
python probe2_literal.py multilingual
python probe3_wording.py
```

可覆盖的环境变量：`SAMPLES_DIR`（默认 `local/samples_all`）、`PROBE_OUT`（默认本目录 `results/`）、`LAYA_MODEL_DIR`（默认本目录 `model/`）。
**权重不入库**：Laya 英文 843 MB + 多语言 645 MB，按 `hf-mirror` 下载到 `model/`（`model/` 已在 .gitignore）。
Jev 端点：`POST https://api.typesafe.ai/v1/systemone`，请求 `jev-latest`，定价 $0.042 / 百万 input token（output 免费）。

## 结果（原始数据在 `results/`）

| 指标 | Laya multilingual（零样本，CPU） | Jev `jev-latest`（云端） |
|---|---|---|
| 总准确率 | 0.406 | **0.984** |
| ECE | 0.507 | **0.034** |
| 判是率 vs 真值率 | 0.886 vs 0.383 | **0.403 vs 0.383** |
| 高置信（\|p\|≥0.9）错误率 | 57.1%（327 个决策） | **1.2%**（8 个） |
| 超过多数类基线的问题数 | 0 / 8 | **7 / 8** |
| 延迟（11 问/份） | 9627 ms | **385 ms** |
| 成本 | 0 | $0.0059 / 100 份 |

问法敏感度（Jev，同一批 100 份）：数据库/竞赛/学生干部三个概念的**分歧率 37–50%**；ECE 随问法从 0.064–0.122（基线）漂到 0.141–0.304（宽泛）。
Laya 对照组在三种措辞下几乎不变（98/100 判是）→ **对问法不敏感，外循环对它无效**。

## 三条必须带着说的前提（否则会读错）

1. **这是「未特化基座 vs 特化产品」，不是「开源 vs 闭源」**：Laya 官方定位是 "fast base to specialise"（零样本接近随机）。
2. **0.984 是合成语料上的乐观上界**：真实简历（OCR 错字、格式混乱、时间跳跃）未测。
3. **基线问法 acc=1.000 是自证**：本实验真值按字面定义、基线与字面同义 → 正确结论是「**问法改变的是"在测什么"，不是"测得准不准"**，"该测什么"只能由 HR 标注定义」。

## 两个已修正的真值 bug（保留以免重犯）

| 错误 | 后果 | 更正 |
|---|---|---|
| `java` 正则忽略大小写 | 88 份 **JavaScript** 被算成 Java，真值虚高到 95/100 | `\bJava\b(?!Script)` → **49/100**；Jev 准确率 0.560 → **0.980** |
| `database` 正则过宽 | 课程名「数据库原理」、微信「云开发数据库」被算成数据库技术 | 逐条看上下文后，Jev 的 28 个「假阴性」**全部是 Jev 对、正则错**（反向案例 0） |

**教训**：要判断模型对错，必须先有比它更可靠的 ground truth；没有独立验证能力时只能盲信模型。
