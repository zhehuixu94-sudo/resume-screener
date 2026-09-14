# -*- coding: utf-8 -*-
"""跑批主循环（工作队列并发）的单元测试：不打网络、不连库。

为什么需要它：主循环原来没有测试，而它有两个很容易悄悄坏掉的性质——
  ① 每一行只能被处理一次（领活与补活的记账错了就会重复烧钱）；
  ② 并发不能被「最慢的那一行」拖住（原来是凑一批等一批，实测量到吞吐只有限速器允许值的 40%）。
真跑一次一百份要一小时，这种记账错误不该靠跑批去发现。
"""
from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.batch as batch                                        # noqa: E402


class FakeRepo:
    def __init__(self, n: int):
        self.rows = [{"application_id": i, "resume_id": f"r{i:03d}", "state": "pending_parse"}
                     for i in range(n)]
        self.claimed: list[str] = []
        self.peak_inflight = 0
        self._inflight = 0
        self._lock = threading.Lock()

    def claim(self, job, limit: int = 1, lock_timeout_min: int = 0):   # noqa: ANN001
        with self._lock:
            out = []
            for _ in range(limit):
                if not self.rows:
                    break
                r = self.rows.pop(0)
                out.append(r)
                self.claimed.append(r["resume_id"])
            return out

    def enter(self) -> None:
        with self._lock:
            self._inflight += 1
            self.peak_inflight = max(self.peak_inflight, self._inflight)

    def leave(self) -> None:
        with self._lock:
            self._inflight -= 1


def make_fake_process(repo: FakeRepo, slow_ids: set[str], slow_s: float = 0.30):
    def fake(row, job, r, client, *, model=None, force=False, force_eval=False):   # noqa: ANN001
        repo.enter()
        try:
            time.sleep(slow_s if row["resume_id"] in slow_ids else 0.02)
        finally:
            repo.leave()
        return {"application_id": row["application_id"], "resume_id": row["resume_id"],
                "state": "evaluated", "action": ""}
    return fake


def test_queue_processes_every_row_exactly_once(monkeypatch) -> None:
    repo = FakeRepo(12)
    monkeypatch.setattr(batch, "process_one", make_fake_process(repo, slow_ids={"r000", "r001"}))
    stats = batch.run(None, repo, None, workers=3, limit=None, model=None)          # type: ignore[arg-type]
    assert sorted(repo.claimed) == sorted(f"r{i:03d}" for i in range(12)), "每行必须恰好领一次"
    assert len(repo.claimed) == len(set(repo.claimed)), "不能有重复领取（重复领取=重复烧钱）"
    assert stats == {"evaluated": 12}, stats


def test_queue_respects_limit(monkeypatch) -> None:
    repo = FakeRepo(12)
    monkeypatch.setattr(batch, "process_one", make_fake_process(repo, slow_ids=set()))
    stats = batch.run(None, repo, None, workers=4, limit=5, model=None)              # type: ignore[arg-type]
    assert sum(stats.values()) == 5, f"limit=5 应该只处理 5 行，实际 {stats}"
    assert len(repo.claimed) == 5


def test_queue_stays_full_despite_slow_rows(monkeypatch) -> None:
    """慢行不能拖住整个并发：领活数应明显多于并发数（凑一批等一批时最多只领 workers 行）。"""
    repo = FakeRepo(10)
    monkeypatch.setattr(batch, "process_one", make_fake_process(repo, slow_ids={"r000"}, slow_s=0.4))
    batch.run(None, repo, None, workers=3, limit=None, model=None)                   # type: ignore[arg-type]
    assert repo.peak_inflight >= 3, f"并发没跑满，峰值只有 {repo.peak_inflight}"
