# -*- coding: utf-8 -*-
"""TPM 限速器 + 429 处理：不打网络的单元测试。

为什么需要它：实测 6 worker 不限速跑 100 份，有 70 份的关键阶段被平台 429 打死
（`code 50602 TPM limit reached`），而原来的 6/12/18 秒退避全落在同一个 60 秒窗口里，
退了等于没退；这一批分数因此全部失真、白花钱。限速逻辑必须有测试，不能靠跑批去发现。
"""
from __future__ import annotations

import io
import os
import sys
import time
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.llm.client import RateLimited, SiliconFlowClient, TokenRateLimiter      # noqa: E402

OK_PAYLOAD = {"choices": [{"message": {"content": '{"ok": 1}'}, "finish_reason": "stop"}],
              "usage": {"total_tokens": 42}}
DETAIL_429 = ('{"code":50602,"message":"Request was rejected due to rate limiting. '
              'Details: TPM limit reached.","data":null}')


def http_err(code: int, body: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://x/v1/chat/completions", code, "err", None,
                                  io.BytesIO(body.encode()))


def make_client(**kw) -> SiliconFlowClient:
    """不读密钥文件、不打网络、不等退避的客户端。"""
    kw.setdefault("tpm", 0)
    kw.setdefault("backoff", 0.0)
    kw.setdefault("rate_limit_backoff", 0.0)
    return SiliconFlowClient(api_key="test-key", **kw)


def test_limiter_waits_when_window_is_full() -> None:
    lim = TokenRateLimiter(tpm=1000, window=0.6, reserve=1000, min_interval=0.0)
    t0 = time.time()
    lim.acquire()                                   # 第一发立刻拿到
    first = time.time() - t0
    lim.acquire()                                   # 配额已满 → 必须等到最老的登记滑出窗口
    second = time.time() - t0 - first
    assert first < 0.2, f"第一发不该被限速，等了 {first:.2f}s"
    assert second > 0.4, f"第二发应该等窗口滑过去，只等了 {second:.2f}s"
    assert lim.waits >= 1


def test_limiter_keeps_reservation_after_call() -> None:
    """回填真实用量时**预留不退**：平台按预留扣额度，退了就是超发。"""
    lim = TokenRateLimiter(tpm=100000, window=60.0, reserve=4000, min_interval=0.0)
    t = lim.acquire()
    assert lim.snapshot()["window_used"] == 4000, "没回填时应按预留量记账"
    lim.record(t, 42)
    assert lim.snapshot()["window_used"] == 4000, "实际用量小于预留时，仍按预留记账"
    t2 = lim.acquire()
    lim.record(t2, 9999)
    assert lim.snapshot()["window_used"] == 4000 + 9999, "实际用量大于预留时以实际为准"


def test_chat_reserves_prompt_plus_max_tokens() -> None:
    """配额要按「输入估算 + max_tokens」申请：实测平台就是这么扣的。"""
    c = make_client(tpm=100000, rate_limit_retries=1)
    c._post = lambda body: (OK_PAYLOAD, {})                  # type: ignore[method-assign]
    user = "啊" * 1300                                       # ≈1000 token 输入
    c.chat("系统", user, max_tokens=4000)
    used = c.limiter.snapshot()["window_used"]              # type: ignore[union-attr]
    assert used >= c.estimate_tokens("系统", user) + 4000, f"只占用了 {used}，预留按少了算"


def test_limiter_paces_by_second() -> None:
    """秒级 TPS 也要压：单请求 2500 token、30k TPM → 每秒只有 500 token，因此两发之间至少 5 秒。"""
    lim = TokenRateLimiter(tpm=30000, reserve=2500)
    assert abs(lim.min_interval - 5.0) < 0.01, f"最小间隔应由 TPM 推导，实际 {lim.min_interval}"
    lim.acquire()
    t0 = time.time()
    lim.acquire()
    assert time.time() - t0 > 4.0, "第二发必须按 TPS 摊平，不能立刻发"


def test_429_has_its_own_retry_budget_and_recovers() -> None:
    """429 不消耗普通重试额度：两次 429 之后成功，仍算成功。"""
    c = make_client(max_retries=3, rate_limit_retries=3)
    calls = {"n": 0}

    def fake_post(body):                                     # noqa: ANN001
        calls["n"] += 1
        if calls["n"] <= 2:
            raise http_err(429, DETAIL_429)
        return OK_PAYLOAD, {}

    c._post = fake_post                                      # type: ignore[method-assign]
    with mock.patch("app.llm.client.random.uniform", return_value=0.0):
        res = c.chat("sys", "user")
    assert res.text == '{"ok": 1}'
    assert c.rate_limit_hits == 2, "被 429 拒了多少次要能观测"
    assert res.attempts == 3, "两次 429 后被拒的那两次不该吃掉普通重试额度"


def test_429_exhausts_and_raises() -> None:
    c = make_client(max_retries=3, rate_limit_retries=2)
    c._post = lambda body: (_ for _ in ()).throw(http_err(429, DETAIL_429))   # type: ignore[method-assign]
    with mock.patch("app.llm.client.random.uniform", return_value=0.0):
        try:
            c.chat("sys", "user")
        except RuntimeError as e:
            assert "RateLimited" in str(e) or "429" in str(e)
        else:
            raise AssertionError("429 用完专属额度后必须抛错，不能静默返回")


def test_500_is_not_treated_as_rate_limit() -> None:
    """普通错误仍走原路：不吃 429 的专属额度。"""
    c = make_client(max_retries=2, rate_limit_retries=3)
    c._post = lambda body: (_ for _ in ()).throw(http_err(500, "boom"))       # type: ignore[method-assign]
    try:
        c.chat("sys", "user")
    except RuntimeError as e:
        assert "HTTP 500" in str(e)
    else:
        raise AssertionError("HTTP 500 应该照旧抛错")


def test_client_records_usage_into_limiter() -> None:
    c = make_client(tpm=100000, rate_limit_retries=1)
    assert isinstance(c.limiter, TokenRateLimiter)
    c._post = lambda body: (OK_PAYLOAD, {})                  # type: ignore[method-assign]
    c.chat("sys", "user")
    assert c.tokens == 42, "累计 token 要能观测（成本对账用）"
    assert c.limiter.snapshot()["window_used"] >= 4000, "窗口里至少要占住 max_tokens 的预留量"


def test_rate_limited_is_a_runtime_error() -> None:
    assert issubclass(RateLimited, RuntimeError)
