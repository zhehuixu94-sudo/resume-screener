# -*- coding: utf-8 -*-
"""硅基流动（SiliconFlow）HTTP 客户端：只用标准库，避免为一个接口引第三方 SDK。

固化的调用约定（都是实测踩出来的）：
- 请求体固定 enable_thinking=false：思考模式开着时 content 为空、耗时翻倍；
  对不支持该参数的模型捕获 HTTPError 后摘掉参数重试一次。
- 单次超时 180s；外层 3 次重试 + 退避（实测偶发 URLError 10060）。
- content 为空则回退 reasoning_content。
- 响应头 x-siliconcloud-trace-id 落 StageMeta.trace_ids，用于对账与复现。
"""
from __future__ import annotations

import json
import os
import re
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
                 timeout: int = 180, max_retries: int = 3, backoff: float = 6.0):
        self.api_key = api_key or load_api_key()
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff = backoff
        self.calls = 0                      # 累计调用次数（成本观测用）

    # ------------------------------------------------------------ 原始 HTTP
    def _post(self, body: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        req = urllib.request.Request(API_URL, data=json.dumps(body).encode(), method="POST")
        req.add_header("Authorization", f"Bearer {self.api_key}")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            payload = json.loads(r.read().decode())
            headers = {k.lower(): v for k, v in r.headers.items()}
        return payload, headers

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
        for attempt in range(1, self.max_retries + 1):
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
                    else:
                        raise RuntimeError(f"HTTP {e.code}: {detail[:300]}") from e
                dt = time.time() - t0
                msg = payload["choices"][0]["message"]
                text = (msg.get("content") or "").strip()
                if not text:                                    # 推理模型偶尔只给 reasoning_content
                    text = (msg.get("reasoning_content") or "").strip()
                if not text:
                    raise RuntimeError("content 与 reasoning_content 都为空")
                return ChatResult(text=text, usage=payload.get("usage", {}), elapsed_s=dt,
                                  trace_id=headers.get("x-siliconcloud-trace-id", ""),
                                  model=body["model"], attempts=attempt,
                                  finish_reason=payload["choices"][0].get("finish_reason", ""),
                                  raw_headers=headers)
            except Exception as e:                              # noqa: BLE001 网络层什么都可能抛
                last_err = e
                if attempt < self.max_retries:
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
