from __future__ import annotations

import os

from doupool.main import DEFAULT_API_TOKEN, _resolve_api_token


def test_default_token_matches_yaonieyo_convention():
    """v0.2.9:默认 token 必须固定 = 'local-doubao-key'。让本机 Python 客户端
    / curl 集成能写死这个值,不用每次启服务去 stdout 抓随机串。"""
    assert DEFAULT_API_TOKEN == "local-doubao-key"


def test_default_token_when_env_unset(monkeypatch):
    """没设 DOUPOOL_API_TOKEN 时,回退到 DEFAULT_API_TOKEN。"""
    monkeypatch.delenv("DOUPOOL_API_TOKEN", raising=False)
    assert _resolve_api_token() == DEFAULT_API_TOKEN


def test_default_token_when_env_empty_string(monkeypatch):
    """空串也算未设(os.environ.get 仍返回空串),用 or 兜底成默认。
    这样部署脚本 export DOUPOOL_API_TOKEN='' 不会意外锁死服务。"""
    monkeypatch.setenv("DOUPOOL_API_TOKEN", "")
    assert _resolve_api_token() == DEFAULT_API_TOKEN


def test_env_override_wins(monkeypatch):
    """DOUPOOL_API_TOKEN 设了值就用它,生产 / 多用户场景换强 key 的唯一出口。"""
    monkeypatch.setenv("DOUPOOL_API_TOKEN", "production-strong-key-aabbccdd")
    assert _resolve_api_token() == "production-strong-key-aabbccdd"