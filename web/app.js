/* 前端逻辑：Vue3（全局构建，无打包），Chrome 109 兼容 */
(function () {
  const { createApp } = Vue;

  const STATE_CN = {
    pending_parse: '待解析', parsing: '解析中', parsed: '已解析', parse_failed: '解析失败',
    evaluating: '评估中', evaluated: '已评估', eval_failed: '评估失败', reviewed: '已复核',
  };
  const CONFLICT_CN = {
    veto_triggered: '触发硬门槛', veto_override: '门槛豁免（综合达标）',
    edge_band_low_reject: '边缘低分（按策略自动淘汰）',
    llm_vs_rule: '两路判据不一致', fit_vs_verdict: '匹配度冲突', low_confidence: '置信度偏低',
    degraded_validation: '模型输出降级', unanchored_evidence: '证据未锚定', edge_band: '落在线内',
  };
  const HF_CN = { pass: '通过', fail: '触发（不通过）', unknown: '证据不足 → 转人工' };
  const DECISION_CN = { auto_pass: '自动达标', manual_review: '转人工复核', auto_reject: '自动淘汰（可申诉）' };

  createApp({
    data() {
      return {
        tabs: [{ id: 'run', name: '上传与运行' }, { id: 'board', name: '队列看板' }, { id: 'config', name: '岗位配置' }],
        tab: 'run', jobs: [], jobId: '', job: {}, health: { ok: false, db: {}, runs: {} },
        progress: {}, runInfo: null, rows: [], q: '', detail: null,
        pending: [], dragging: false, msg: '', workers: 3, limit: null, running: false, timer: null,
      };
    },
    computed: {
      shortModel() { return (this.health.model || '').split('/').pop(); },
      columns() {
        return [
          { key: 'auto_pass', name: '自动达标', hint: '双通道一致且过线' },
          { key: 'manual_review', name: '转人工复核', hint: '拿不准的一律交人' },
          { key: 'auto_reject', name: '自动淘汰', hint: '可申诉' },
        ];
      },
    },
    methods: {
      async api(path, opts) {
        const res = await fetch(path, opts);
        if (!res.ok) throw new Error(res.status + ' ' + (await res.text()).slice(0, 200));
        return res.headers.get('content-type', '').includes('json') ? res.json() : res.text();
      },
      async loadAll() {
        try {
          this.health = await this.api('/api/health');
          this.jobs = await this.api('/api/jobs');
          if (!this.jobId && this.jobs.length) {
            // 默认选"已经有评估结果"的岗位，避免打开就是空看板
            const withData = this.jobs.filter(j => j.progress && j.progress.evaluated);
            withData.sort((a, b) => (b.progress.evaluated || 0) - (a.progress.evaluated || 0));
            this.jobId = (withData[0] || this.jobs[0]).job_id;
          }
          await this.onJobChange();
        } catch (e) { this.msg = '加载失败：' + e.message; }
      },
      async onJobChange() {
        if (!this.jobId) return;
        this.job = await this.api('/api/jobs/' + this.jobId);
        await this.loadRows();
        await this.poll();
        this.autoPoll();
      },
      async loadRows() {
        if (!this.jobId) return;
        const data = await this.api('/api/jobs/' + this.jobId + '/evaluations');
        this.rows = data.rows || [];
        this.progress = data.progress || {};
      },
      async poll() {
        if (!this.jobId) return;
        const d = await this.api('/api/jobs/' + this.jobId + '/run-status');
        this.runInfo = d.run || null;
        this.progress = d.progress || this.progress;
        this.running = !!this.runInfo && this.runInfo.status === 'running';
        return d;
      },
      autoPoll() {
        if (this.timer) clearInterval(this.timer);
        this.timer = setInterval(async () => {
          try {
            const d = await this.poll();
            if (!this.running) { await this.loadRows(); }
            if (d && d.run && d.run.status !== 'running') { await this.loadRows(); }
          } catch (e) { /* 静默：轮询失败不打扰 */ }
        }, 4000);
      },
      onPick(ev) { this.pending = this.pending.concat(Array.prototype.slice.call(ev.target.files)); ev.target.value = ''; },
      onDrop(ev) {
        this.dragging = false;
        this.pending = this.pending.concat(Array.prototype.slice.call(ev.dataTransfer.files));
      },
      async upload() {
        if (!this.pending.length) return;
        const fd = new FormData();
        this.pending.forEach(f => fd.append('files', f));
        this.msg = '上传中…';
        try {
          const r = await this.api('/api/jobs/' + this.jobId + '/upload', { method: 'POST', body: fd });
          this.msg = '已入队 ' + r.enqueued + ' 份' + (r.skipped.length ? '，跳过 ' + r.skipped.length + ' 个（格式不支持）' : '');
          this.pending = [];
          this.progress = r.progress;
          await this.loadRows();
        } catch (e) { this.msg = '上传失败：' + e.message; }
      },
      async startRun() {
        try {
          const r = await this.api('/api/jobs/' + this.jobId + '/run?workers=' + this.workers +
            (this.limit ? '&limit=' + this.limit : ''), { method: 'POST' });
          this.msg = r.started ? '已开始，正在后台评估…' : ('未启动：' + (r.why || ''));
          this.running = true;
          this.autoPoll();
        } catch (e) { this.msg = '启动失败：' + e.message; }
      },
      async retry() {
        try {
          const r = await this.api('/api/jobs/' + this.jobId + '/retry', { method: 'POST' });
          this.msg = '已重置 ' + r.reset + ' 行为可重跑';
          this.progress = r.progress;
        } catch (e) { this.msg = '重置失败：' + e.message; }
      },
      openDetail(r) { this.detail = r; },
      filtered(key) {
        const q = this.q.trim().toLowerCase();
        return this.rows.filter(r => (r.decision || 'pending') === key)
          .filter(r => !q || (r.file_name || '').toLowerCase().indexOf(q) >= 0)
          .sort((a, b) => (b.base_total || 0) - (a.base_total || 0));
      },
      pct(n) {
        const total = Object.keys(this.progress).reduce((s, k) => s + this.progress[k], 0);
        return total ? Math.max(1, Math.round(n * 100 / total)) : 0;
      },
      stateName(k) { return STATE_CN[k] || k; },
      stateClass(k) {
        if (k === 'evaluated' || k === 'reviewed') return 'ok';
        if (k === 'parse_failed' || k === 'eval_failed') return 'bad';
        if (k === 'evaluating' || k === 'parsing') return 'warn';
        return 'grey';
      },
      conflictName(c) { return CONFLICT_CN[c] || c; },
      hfName(v) { return HF_CN[v] || v; },
      decisionName(d) { return DECISION_CN[d] || d; },
    },
    mounted() { this.loadAll(); },
  }).mount('#app');
})();
