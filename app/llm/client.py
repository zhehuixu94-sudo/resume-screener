# -*- coding: utf-8 -*-
"""硅基流动（SiliconFlow）HTTP 客户端：只用标准库，避免为一个接口引第三方 SDK。

固化的调用约定（都是实测踩出来的）：
- 请求体固定 enable_thinking=false：思考模式开着时 content 为空、耗时翻倍；
  对不支持该参数的模型捕获 HTTPError 后摘掉参数重试一次。
- 单次超时 180s；外层 3 次重试 + 退避（实测偶发 URLError 10060）。
- content 为空则回退 reasoning_content。
- 响应头 x-siliconcloud-trace-id 落 StageMeta.trace_ids，用于对账与复现。
- **进程级 TPM 限速器**（TokenRateLimiter）：平台按「每分钟 token 数」限流（实测 6 worker 并发跑 100 份
  时有 70 份的关键阶段被 429 打死：`code 50602 TPM limit reached`）。重试退避只有 6/12/18 秒，
  全都落在同一个 60 秒窗口里，退了也白退。所以要在**发请求前**排队（滚动 60 秒窗口按实际用量控制），
  并把 429 单独当一类错误处理：长退避 + 不消耗普通重试额度。
"""
from __future__ import annotations

import json
import os
import random
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

API_URL = "https://api.siliconflow.cn/v1/chat/completions"
# 密钥来源优先级：环境变量 SILICONFLOW_API_KEY → 本机密钥文件（KEY_FILE，开发期用）。
ENV_KEY = "SILICONFLOW_API_KEY"
KEY_FILE = os.environ.get("SILICONFLOW_ENV_FILE", r"D:\Hermes\secrets\openclaw.env")
DEFAULT_MODEL = "Qwen/Qwen3.5-122B-A10B"          # 关思考后 8~10s 稳定返回合法 JSON
# 平台限额：文档口径 L0 档约 50k TPM/模型，但实测（35B-A3B、6 路并发持续 3 分钟）平台接受
# ~49k 实际 token/min、按「输入+max_tokens」预留口径约 157k/min 仍然全绿；
# 而主干 122B 在负载下约 20% 调用被 429 拒 —— 所以限速要配合「显式钉死模型」一起用。
# 默认按 120k 预留/分钟排程（约为实测上限的 3/4），每次跑批结束都会打印限速器快照与被拒次数。
DEFAULT_TPM = int(os.environ.get("RESUME_TPM") or 120000)


class RateLimited(RuntimeError):
    """平台按 TPM/RPM 拒绝的请求（HTTP 429）——单独一类，退避与重试额度都与普通错误不同。"""


# ---------------------------------------------------------------- TPM 限速器
class TokenRateLimiter:
    """滚动窗口 TPM 限速：发请求前排队，拿到配额才真正发。

    做法：每次 acquire 先在窗口里登记一个「预留量」，调用返回后用真实 usage 回填（拿不到就保守留预留量）。
    预留 + 已用超过配额就睡到最老的登记滑出窗口。min_interval 顺带压一下瞬时并发（RPS 也在限流范围内）。
    """

    def __init__(self, tpm: int, window: float = 60.0, reserve: int = 2500,
                 min_interval: float | None = None):
        self.tpm = tpm
        self.window = window
        self.reserve = reserve
        # 平台除了「每分钟 token」，还按秒级 TPS（=TPM/60）限流。实测只压 60 秒窗口不够：
        # 6 个 worker 同时发，每个请求 ~2.5k token，瞬时就是 1500+ token/s，照样 429。
        # 所以默认最小间隔 = 单次预留量 / 每秒配额（tpm/60），把发请求的速率摊平。
        self.min_interval = min_interval if min_interval is not None else \
            max(0.5, reserve / max(1.0, tpm / window))
        self._events: list[list[float]] = []          # [ts, tokens]
        self._lock = threading.Lock()                 # 保护 _events
        self._admit = threading.Lock()                # 串行化准入，避免同时放行
        self._last = 0.0
        self.waits = 0                                # 被限速等待的次数（观测用）
        self.waited_s = 0.0

    def _prune(self, now: float) -> None:
        self._events = [e for e in self._events if now - e[0] < self.window]

    def acquire(self, reserve: float | None = None) -> list[float]:
        """取一个发请求的名额。reserve 是这次请求要占的配额（默认用 self.reserve）。

        reserve 由调用方按「输入估算 token + max_tokens」给：实测平台按**预留量**而不是实际用量
        扣 TPM —— 12 发/分钟的 label 请求（输入 1.5k + max_tokens 4000）就触发 429，
        而同样真实用量、max_tokens 只有 64 的小请求跑到 22k token/min 全绿。
        """
        amount = float(reserve if reserve is not None else self.reserve)
        amount = min(max(amount, 1.0), max(1.0, self.tpm * 0.9))   # 单发不超过 90% 配额，否则会永远等下去
        with self._admit:
            while True:
                with self._lock:
                    now = time.time()
                    self._prune(now)
                    used = sum(e[1] for e in self._events)
                    quiet = now - self._last >= self.min_interval
                    if used + amount <= self.tpm and quiet:
                        ticket = [now, amount]
                        self._events.append(ticket)
                        self._last = now
                        return ticket
                    wait = max(self.min_interval - (now - self._last), 0.0)
                    if self._events and used + amount > self.tpm:
                        wait = max(wait, self._events[0][0] + self.window - now)
                    if wait > 0.05:
                        self.waits += 1
                        self.waited_s += wait
                time.sleep(min(max(wait, 0.05), 5.0))       # 最多睡 5 秒再复查（不长时间持锁）

    def record(self, ticket: list[float], tokens: int) -> None:
        """回填真实用量。但**预留下来的那部分不退**：平台按预留扣额度，退了就等于超发。"""
        with self._lock:
            ticket[1] = float(max(ticket[1], tokens))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            self._prune(now)
            return {"tpm": self.tpm, "window_used": int(sum(e[1] for e in self._events)),
                    "waits": self.waits, "waited_s": round(self.waited_s, 1)}

    @property
    def last_admit(self) -> float:
        return self._last


_LIMITER: TokenRateLimiter | None = None
_LIMITER_LOCK = threading.Lock()


def get_limiter(tpm: int | None = None) -> TokenRateLimiter | None:
    """进程级单例：同一进程里所有 worker 共用一份配额（这才是限流的正确粒度）。tpm<=0 关闭。"""
    global _LIMITER
    with _LIMITER_LOCK:
        if tpm and (tpm > 0) and (_LIMITER is None or _LIMITER.tpm != tpm):
            _LIMITER = TokenRateLimiter(tpm)
        return _LIMITER



@dataclass
class ChatResult:
    text: str
    usage: dict[str, Any] = field(default_factory=dict)
    elapsed_s: float = 0.0
    trace_id: str = ""
    model: str = ""
    attempts: int = 1
    finish_reason: str = ""
    raw_headers: dict[str, str] = field(default_factory=dict)


def load_api_key(key_file: str = KEY_FILE, name: str = ENV_KEY) -> str:
    """环境变量优先；没有才去读本机密钥文件（KEY_FILE / SILICONFLOW_ENV_FILE）。"""
    if os.environ.get(name):
        return os.environ[name].strip()
    if not os.path.exists(key_file):
        raise SystemExit(f"未设置环境变量 {name}，且密钥文件不存在：{key_file}")
    env = open(key_file, encoding="utf-8", errors="replace").read()
    m = re.search(rf"{name}\s*=\s*([^\s#]+)", env)
    if not m:
        raise SystemExit(f"未在 {key_file} 找到 {name}")
    return m.group(1).strip().strip('"')


class SiliconFlowClient:
    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL,
                 timeout: int = 180, max_retries: int = 3, backoff: float = 6.0,
                 tpm: int = DEFAULT_TPM, rate_limit_retries: int = 3,
                 rate_limit_backoff: float = 25.0):
        self.api_key = api_key or load_api_key()
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff = backoff
        self.tpm = tpm
        self.rate_limit_retries = rate_limit_retries        # 429 单独的重试额度（不占普通额度）
        self.rate_limit_backoff = rate_limit_backoff
        self.calls = 0                      # 累计调用次数（成本观测用）
        self.rate_limit_hits = 0            # 被 429 拒了几次（观测用）
        self.tokens = 0                     # 累计 token（成本观测用）
        self.limiter = get_limiter(tpm) if tpm and tpm > 0 else None

    # ------------------------------------------------------------ 原始 HTTP
    def _post(self, body: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        req = urllib.request.Request(API_URL, data=json.dumps(body).encode(), method="POST")
        req.add_header("Authorization", f"Bearer {self.api_key}")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            payload = json.loads(r.read().decode())
            headers = {k.lower(): v for k, v in r.headers.items()}
        return payload, headers

    # ------------------------------------------------------------ 探活
    @staticmethod
    def estimate_tokens(*texts: str) -> int:
        """粗估输入 token 数（不引 tokenizer：中英混排按 1 token ≈ 1.3 字符，宁可高估）。

        高估的代价只是慢一点；低估的代价是被平台 429 打死一整批（实测 70/100 份因此降级）。
        """
        chars = sum(len(t or "") for t in texts)
        return int(chars / 1.3) + 32

    def probe(self, model: str | None = None, timeout: int = 20) -> bool:
        """极小请求探活：能拿到 content 就算可用。批处理开跑前用它挑模型。"""
        try:
            body = {"model": model or self.model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 4, "temperature": 0.0, "enable_thinking": False}
            req = urllib.request.Request(API_URL, data=json.dumps(body).encode(), method="POST")
            req.add_header("Authorization", f"Bearer {self.api_key}")
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = json.loads(r.read().decode())
            return bool(payload.get("choices"))
        except Exception:                                     # noqa: BLE001
            return False

    # ------------------------------------------------------------ chat
    def chat(self, system: str, user: str, *, model: str | None = None, max_tokens: int = 4000,
             temperature: float = 0.0, json_mode: bool = True,
             json_schema: dict[str, Any] | None = None) -> ChatResult:
        body: dict[str, Any] = {
            "model": model or self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "enable_thinking": False,
        }
        if json_schema is not None:
            body["response_format"] = {"type": "json_schema", "json_schema": json_schema}
        elif json_mode:
            body["response_format"] = {"type": "json_object"}

        last_err: Exception | None = None
        attempt = 0
        rl_used = 0
        budget = self.max_retries + self.rate_limit_retries
        # 平台按「输入 + max_tokens」预留量扣 TPM：配额要按预留量申请，不能按实际用量估。
        reserve = self.estimate_tokens(system, user) + max_tokens
        while budget > 0:
            budget -= 1
            attempt += 1
            ticket = self.limiter.acquire(reserve) if self.limiter else None
            t0 = time.time()
            try:
                self.calls += 1
                try:
                    payload, headers = self._post(body)
                except urllib.error.HTTPError as e:
                    detail = e.read().decode(errors="replace")
                    if "enable_thinking" in detail and "enable_thinking" in body:
                        body.pop("enable_thinking", None)      # 某些模型不认这个参数
                        payload, headers = self._post(body)
                    elif "json_schema" in detail and "json_schema" in str(body.get("response_format")):
                        body["response_format"] = {"type": "json_object"}   # 不支持 json_schema 就退回 json_object
                        payload, headers = self._post(body)
                    elif e.code == 429 or "rate limiting" in detail:
                        # 平台限流：单独一类。靠进程级 TPM 限速器本不该常见；真被拒了就长退避等窗口滑过去
                        raise RateLimited(f"HTTP {e.code}: {detail[:200]}") from e
                    else:
                        raise RuntimeError(f"HTTP {e.code}: {detail[:300]}") from e
                dt = time.time() - t0
                msg = payload["choices"][0]["message"]
                text = (msg.get("content") or "").strip()
                if not text:                                    # 推理模型偶尔只给 reasoning_content
                    text = (msg.get("reasoning_content") or "").strip()
                if not text:
                    raise RuntimeError("content 与 reasoning_content 都为空")
                usage = payload.get("usage", {}) or {}
                if self.limiter and ticket is not None:          # 用真实用量回填，比预留量准
                    self.limiter.record(ticket, int(usage.get("total_tokens") or 0))
                self.tokens += int(usage.get("total_tokens") or 0)
                return ChatResult(text=text, usage=usage, elapsed_s=dt,
                                  trace_id=headers.get("x-siliconcloud-trace-id", ""),
                                  model=body["model"], attempts=attempt,
                                  finish_reason=payload["choices"][0].get("finish_reason", ""),
                                  raw_headers=headers)
            except Exception as e:                              # noqa: BLE001 网络层什么都可能抛
                last_err = e
                if isinstance(e, RateLimited) and rl_used < self.rate_limit_retries:
                    rl_used += 1
                    self.rate_limit_hits += 1
                    budget += 1                                  # 429 不消耗普通重试额度
                    wait = min(self.rate_limit_backoff * rl_used, 60.0) + random.uniform(1.0, 4.0)
                    print(f"  ⏳ [{time.strftime('%H:%M:%S')}] 平台限流（第 {rl_used}/"
                          f"{self.rate_limit_retries} 次），等 {wait:.0f}s 再试", flush=True)
                    time.sleep(wait)
                elif budget > 0:
                    time.sleep(self.backoff * attempt)
        raise RuntimeError(f"调用失败（{self.max_retries} 次重试后）：{type(last_err).__name__}: {last_err}")



# ---------------------------------------------------------------- JSON 解析
def parse_json(text: str) -> Any:
    """剥代码围栏 + 截取最外层 {} / [] 后再解析。失败抛 ValueError（由上层决定重试还是降级）。"""
    t = text.strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    try:
        return json.loads(t)
    except Exception:
        m = re.search(r"[\[{].*[\]}]", t, re.S)
        if m:
            return json.loads(m.group(0))
        raise ValueError(f"不是合法 JSON：{text[:200]}")
