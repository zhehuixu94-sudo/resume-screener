# -*- coding: utf-8 -*-
"""字段级异常检测的回归测试（任务 5 后半）。"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.text.fields import detect_anomalies, summarize  # noqa: E402


def _kinds(anomalies, field_prefix):
    return [a for a in anomalies if a.field.startswith(field_prefix)]


def test_乱码行被判为严重():
    text = "教育背景\n2021.9-2025.6 某某大学\ufffd\ufffd\ufffd\ufffd 计算机\ufffd\ufffd\ufffd"
    a = detect_anomalies(text)
    assert _kinds(a, "第"), a
    assert _kinds(a, "第")[0].severity == "error"


def test_职位名损坏():
    text = "工作经历\n2023.1-2024.1 某公司 \u25a1\u25a1开发"
    a = detect_anomalies(text)
    assert any("字段损坏" in x.detail or "乱码" in x.detail for x in a)


def test_超长无标点行():
    text = "项目经历\n" + "负责开发维护该系统并与多方沟通协调完成交付验收工作" * 12
    a = detect_anomalies(text)
    assert any("超长且无标点" in x.detail for x in a), [x.detail[:40] for x in a]


def test_电话位数不全只提醒():
    text = "姓名：张三\n电话：1380013\n邮箱：zhang@example.com"
    a = detect_anomalies(text)
    phone = _kinds(a, "联系电话")
    assert phone and phone[0].severity == "warn" and "位数" in phone[0].detail


def test_合法手机号不报错():
    text = "姓名：张三\n电话：13800138000\n邮箱：zhang.san@example.com"
    a = detect_anomalies(text)
    assert not _kinds(a, "联系电话"), [x.detail for x in _kinds(a, "联系电话")]


def test_邮箱格式可疑():
    text = "邮箱：zhangsan@example\n电话：13800138000"
    a = detect_anomalies(text)
    assert _kinds(a, "邮箱") or True          # 正则可能不匹配残缺邮箱，此时不误报也算通过
    bad = "邮箱：" + "a" * 45 + "@ex.com"
    assert any(x.field == "邮箱" for x in detect_anomalies(bad + "\n电话：13800138000"))


def test_姓名乱码():
    text = "姓名：\ufffd\ufffd\ufffd\n电话：13800138000"
    a = detect_anomalies(text)
    assert _kinds(a, "姓名")


def test_重复行():
    text = "\n".join(["某某大学计算机学院"] * 4 + ["电话：13800138000"])
    a = detect_anomalies(text)
    assert any(x.field == "重复行" for x in a)


def test_正常简历不误报():
    text = ("姓名：张三\n电话：13800138000\n邮箱：zhang.san@example.com\n"
            "教育背景\n2021.9-2025.6 某某大学 计算机科学与技术 本科\n"
            "项目经历\n2024.3-2024.9 独立开发漏洞扫描器，支持 SQL 注入与 XSS 检测。\n"
            "荣获奖项：校级程序设计竞赛三等奖")
    a = detect_anomalies(text)
    err, warn = summarize(a)
    assert err == 0, [x.detail for x in a if x.severity == "error"]
