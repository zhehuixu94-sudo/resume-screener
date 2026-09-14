-- 简历初筛系统 · 存储层 v1（PostgreSQL 16 + pgvector）
-- 设计要点：
--   1. 简历是实体、应聘是关系：同一份简历投不同岗位 = 多条 application，解析结果只存一份。
--   2. 状态机挂在 application 上：待解析/解析失败/已入库/评估中/已评估/已复核 —— 断点续跑就查它。
--   3. 一切写入幂等：靠唯一键 + ON CONFLICT，重跑不会产生重复评分行。
--   4. 评分行带 model_id / prompt_version / prompt_hash / config_version，历史分数可复现。
--   5. 原文 span 落库，报告的"证据可点开高亮"依赖它；未锚定的证据 anchored=false，不许作自动处置依据。
--   6. audit_log 只增不改（权限层面保证）。

CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------- 岗位配置（版本化，不覆盖历史）
CREATE TABLE IF NOT EXISTS job_config (
    config_id      BIGSERIAL PRIMARY KEY,
    company_id     TEXT NOT NULL DEFAULT 'local',
    job_id         TEXT NOT NULL,
    config_version TEXT NOT NULL,
    config_hash    TEXT NOT NULL,
    job_title      TEXT NOT NULL,
    payload        JSONB NOT NULL,               -- 整份 JobConfig 契约对象
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (company_id, job_id, config_version)
);

-- ---------------------------------------------------------------- 简历实体
CREATE TABLE IF NOT EXISTS resume (
    resume_id     TEXT PRIMARY KEY,              -- 文件 sha256 前 16 位
    company_id    TEXT NOT NULL DEFAULT 'local',
    file_name     TEXT NOT NULL,
    file_path     TEXT,                          -- 原始文件路径（断点续跑要重新读它；本地路径，交付环境由上传接口填）
    file_sha256   TEXT NOT NULL,
    file_format   TEXT NOT NULL,
    pages         INT  NOT NULL DEFAULT 1,
    char_count    INT  NOT NULL DEFAULT 0,
    l0_encrypted  BYTEA,                         -- L0 原始简历（应用层 AES 加密列，v1 允许为空）
    text_sha256   TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- 应聘关系（状态机）
-- state: pending_parse → parsing → parsed → evaluating → evaluated → reviewed
--        parse_failed（可重试，attempts 上限后转人工）
CREATE TABLE IF NOT EXISTS application (
    application_id BIGSERIAL PRIMARY KEY,
    company_id     TEXT NOT NULL DEFAULT 'local',
    resume_id      TEXT NOT NULL REFERENCES resume(resume_id) ON DELETE CASCADE,
    job_id         TEXT NOT NULL,
    config_version TEXT NOT NULL,
    state          TEXT NOT NULL DEFAULT 'pending_parse',
    attempts       INT  NOT NULL DEFAULT 0,
    last_error     TEXT NOT NULL DEFAULT '',
    worker         TEXT NOT NULL DEFAULT '',     -- 抢占锁：非空表示正在被某个 worker 处理
    locked_at      TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (company_id, resume_id, job_id, config_version)
);
CREATE INDEX IF NOT EXISTS idx_application_state ON application (company_id, state);

-- ---------------------------------------------------------------- 解析结果（一份简历一行，幂等覆盖）
CREATE TABLE IF NOT EXISTS resume_parse (
    parse_id         BIGSERIAL PRIMARY KEY,
    resume_id        TEXT NOT NULL REFERENCES resume(resume_id) ON DELETE CASCADE,
    payload          JSONB NOT NULL,             -- ResumeParse 契约对象（含 blocks/items/quality/llm 元数据）
    model_id         TEXT NOT NULL,
    prompt_version   TEXT NOT NULL,
    prompt_hash      TEXT NOT NULL,
    validation_state TEXT NOT NULL,              -- ok / repaired / degraded / failed
    gate_passed      BOOLEAN NOT NULL,
    evidence_total   INT NOT NULL DEFAULT 0,
    evidence_anchored INT NOT NULL DEFAULT 0,
    evidence_coverage REAL NOT NULL DEFAULT 0,
    text             TEXT NOT NULL,              -- 规范化文本（高亮坐标基准）
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (resume_id)
);

-- ---------------------------------------------------------------- 证据 span 索引（报告高亮/追溯依赖）
CREATE TABLE IF NOT EXISTS evidence_span (
    span_id        TEXT NOT NULL,                 -- 只在同一份简历内唯一（ev_0001…），所以主键是复合的
    resume_id      TEXT NOT NULL REFERENCES resume(resume_id) ON DELETE CASCADE,
    item_id        TEXT NOT NULL,
    block_title    TEXT NOT NULL DEFAULT '',
    text           TEXT NOT NULL,
    start_offset   INT,                          -- 规范化文本坐标；NULL = 锚定失败
    end_offset     INT,
    page           INT,
    method         TEXT NOT NULL,                -- exact / ws_insensitive / fuzzy / none
    similarity     REAL NOT NULL DEFAULT 0,
    anchored       BOOLEAN NOT NULL DEFAULT false,
    PRIMARY KEY (resume_id, span_id)
);
CREATE INDEX IF NOT EXISTS idx_evidence_resume ON evidence_span (resume_id);
CREATE INDEX IF NOT EXISTS idx_evidence_anchored ON evidence_span (resume_id, anchored);

-- ---------------------------------------------------------------- 评估结果（评分行，可复现）
CREATE TABLE IF NOT EXISTS evaluation (
    evaluation_id    BIGSERIAL PRIMARY KEY,
    application_id   BIGINT NOT NULL REFERENCES application(application_id) ON DELETE CASCADE,
    resume_id        TEXT NOT NULL REFERENCES resume(resume_id) ON DELETE CASCADE,
    job_id           TEXT NOT NULL,
    organization_id  TEXT NOT NULL DEFAULT 'local',   -- 多租户预留，不做 RLS
    verdict          TEXT NOT NULL,              -- 达标 / 边缘 / 不达标
    decision         TEXT NOT NULL,              -- auto_pass / manual_review / auto_reject
    base_total       REAL NOT NULL,
    confidence       REAL NOT NULL,
    config_version   TEXT NOT NULL,
    model_id         TEXT NOT NULL,
    prompt_version   TEXT NOT NULL,
    prompt_hash      TEXT NOT NULL,
    aggregation      TEXT NOT NULL,              -- best / top2_mean
    pass_line        REAL NOT NULL,
    payload          JSONB NOT NULL,             -- Evaluation 契约对象
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (application_id)
);
CREATE INDEX IF NOT EXISTS idx_evaluation_decision ON evaluation (job_id, decision);

-- ---------------------------------------------------------------- 审计日志（append-only）
CREATE TABLE IF NOT EXISTS audit_log (
    id         BIGSERIAL PRIMARY KEY,
    ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor      TEXT NOT NULL DEFAULT 'system',
    action     TEXT NOT NULL,
    entity     TEXT NOT NULL,
    entity_id  TEXT NOT NULL,
    detail     JSONB NOT NULL DEFAULT '{}'::jsonb,
    trace_ids  TEXT[] NOT NULL DEFAULT '{}'
);

-- ---------------------------------------------------------------- 检索层（v1 不写；启用锚点检索时用）
CREATE TABLE IF NOT EXISTS resume_chunk (
    chunk_id   BIGSERIAL PRIMARY KEY,
    resume_id  TEXT NOT NULL REFERENCES resume(resume_id) ON DELETE CASCADE,
    seq        INT  NOT NULL,
    content    TEXT NOT NULL,
    embedding  vector(1024),
    UNIQUE (resume_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_chunk_resume ON resume_chunk (resume_id);

-- 兼容早期建库：补列（幂等）
ALTER TABLE resume ADD COLUMN IF NOT EXISTS file_path TEXT;

-- 兼容早期建库：evidence_span 的主键从 (span_id) 改成 (resume_id, span_id)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint
                WHERE conname = 'evidence_span_pkey'
                  AND conrelid = 'evidence_span'::regclass
                  AND array_length(conkey, 1) = 1) THEN
        DELETE FROM evidence_span;                     -- span 可由解析结果重建，直接清掉再换主键
        ALTER TABLE evidence_span DROP CONSTRAINT evidence_span_pkey;
        ALTER TABLE evidence_span ADD PRIMARY KEY (resume_id, span_id);
    END IF;
END $$;

-- ---------------------------------------------------------------- 复核（HR 的人工动作，append-only）
-- 两个库就是这张表的两个影子：
--   自动淘汰库  = evaluation.decision = 'auto_reject' 的件（视图，不复制数据，避免双写漂移）
--   人工淘汰库  = 本表 action='reject' 且未被清理的件（cleared_at 为空），支持手动清理（软删除 + 审计）
CREATE TABLE IF NOT EXISTS review_decision (
    review_id      BIGSERIAL PRIMARY KEY,
    company_id     TEXT NOT NULL DEFAULT 'local',
    application_id BIGINT NOT NULL REFERENCES application(application_id) ON DELETE CASCADE,
    resume_id      TEXT NOT NULL,
    job_id         TEXT NOT NULL,
    config_version TEXT NOT NULL,
    action         TEXT NOT NULL,                 -- pass（该留）/ reject（该淘汰）
    source         TEXT NOT NULL DEFAULT 'manual',-- spot_check（抽检）/ manual（主动复核）
    note           TEXT NOT NULL DEFAULT '',
    actor          TEXT NOT NULL DEFAULT 'hr',
    archived_path  TEXT NOT NULL DEFAULT '',      -- 通过件的归档路径（复制到「简历/<日期>/」后的落点）
    cleared_at     TIMESTAMPTZ,                   -- 人工淘汰库被手动清理的时间（软删除，不物理删）
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_review_action ON review_decision (company_id, job_id, action);
CREATE INDEX IF NOT EXISTS idx_review_app ON review_decision (application_id);

-- ---------------------------------------------------------------- 抽检批次（按天一批，不累加）
-- 「不累加」的落地：每天一个批次，未复核的旧批次标记 expired 后不再展示；
-- 未复核的简历**不算被抽过**，之后的批次仍可能抽到它（否则这些件既没人看、又永不入库，漏放样本就丢了）。
CREATE TABLE IF NOT EXISTS spot_check_batch (
    batch_id       BIGSERIAL PRIMARY KEY,
    company_id     TEXT NOT NULL DEFAULT 'local',
    job_id         TEXT NOT NULL,
    config_version TEXT NOT NULL,
    check_date     DATE NOT NULL,
    drawn          INT NOT NULL DEFAULT 0,        -- 本批实际抽到几份
    want           INT NOT NULL DEFAULT 5,        -- 本批目标份数
    pool_size      INT NOT NULL DEFAULT 0,        -- 抽之前池子有多大（池子不足时如实记录）
    note           TEXT NOT NULL DEFAULT '',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (company_id, job_id, config_version, check_date)
);

CREATE TABLE IF NOT EXISTS spot_check_item (
    item_id        BIGSERIAL PRIMARY KEY,
    batch_id       BIGINT NOT NULL REFERENCES spot_check_batch(batch_id) ON DELETE CASCADE,
    application_id BIGINT NOT NULL REFERENCES application(application_id) ON DELETE CASCADE,
    resume_id      TEXT NOT NULL,
    job_id         TEXT NOT NULL,
    config_version TEXT NOT NULL,
    check_date     DATE NOT NULL,
    score          REAL,                          -- 抽中时的规则分（写清单时展示，便于 HR 对比）
    state          TEXT NOT NULL DEFAULT 'pending', -- pending / passed / rejected / expired
    reviewed_at    TIMESTAMPTZ,
    UNIQUE (batch_id, application_id)             -- 同一天不会重复抽同一份
);
CREATE INDEX IF NOT EXISTS idx_spot_state ON spot_check_item (job_id, state, check_date);

-- ---------------------------------------------------------------- 口径校准（全自动更新 + 可回滚）
-- 每次校准记一条：触发原因、用了哪些样本、改前改后的完整配置快照、是否被回滚。
-- 回滚 = 用 before_payload 覆盖回配置文件（并再记一条 calibration 行，留痕）。
CREATE TABLE IF NOT EXISTS calibration (
    calibration_id BIGSERIAL PRIMARY KEY,
    company_id     TEXT NOT NULL DEFAULT 'local',
    job_id         TEXT NOT NULL,
    trigger        TEXT NOT NULL,                 -- manual（用户主动）/ spot_check_error（当日抽检误差≥2）
    from_version   TEXT NOT NULL,
    to_version     TEXT NOT NULL,
    status         TEXT NOT NULL,                 -- applied / skipped（样本不足或两组分不开）
    reason         TEXT NOT NULL DEFAULT '',
    samples        JSONB NOT NULL DEFAULT '{}'::jsonb,   -- {pos:[...], neg:[...], 分数分布}
    changed        JSONB NOT NULL DEFAULT '{}'::jsonb,   -- {pass_line: {before, after}, ...}
    before_payload JSONB NOT NULL,                -- 改前整份 JobConfig（回滚依据）
    after_payload  JSONB NOT NULL,                -- 改后整份 JobConfig
    reverted_at    TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_calibration_job ON calibration (job_id, created_at DESC);

-- ---------------------------------------------------------------- 基本项勾选（多选校准的原始标注）
-- HR 在每份简历上勾「哪些是基本项」（只勾基本项，不勾优秀项）——这是给校准用的人工标注。
CREATE TABLE IF NOT EXISTS basic_item_label (
    label_id       BIGSERIAL PRIMARY KEY,
    company_id     TEXT NOT NULL DEFAULT 'local',
    application_id BIGINT NOT NULL REFERENCES application(application_id) ON DELETE CASCADE,
    job_id         TEXT NOT NULL,
    config_version TEXT NOT NULL,
    item_key       TEXT NOT NULL,                 -- 证据条目在评估里的唯一键（item_id 或 evc_xxx）
    dimension      TEXT NOT NULL DEFAULT '',      -- 勾中时的维度归属（沉淀岗位标准用）
    is_basic       BOOLEAN NOT NULL DEFAULT true, -- false = HR 明确把它排除出基本项
    actor          TEXT NOT NULL DEFAULT 'hr',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (application_id, item_key)
);
CREATE INDEX IF NOT EXISTS idx_basic_item_job ON basic_item_label (job_id, dimension);
