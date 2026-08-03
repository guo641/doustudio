"""
任务分组测试 — 验证 start(prompts=...) 多 prompt 自动归组到同一 group_id

只测 VideoTaskService.start 的 prompt 处理逻辑,不 import peewee 模型,
所以兼容 Python 3.10。
"""

from __future__ import annotations

import pytest

from doupool.video.service import VideoTaskService


class _StubRepo:
    """最小 repository stub,只记录 create_video_task 调用"""

    def __init__(self):
        self.created: list[dict] = []

    def create_video_task(
        self,
        account_id,
        prompt,
        model,
        ratio,
        duration,
        *,
        mode="t2v",
        image_paths=None,
        group_id=None,
        group_index=0,
        callback_url=None,
    ):
        record = {
            "id": f"task-{len(self.created) + 1}",
            "account": account_id,
            "prompt": prompt,
            "model": model,
            "ratio": ratio,
            "duration": duration,
            "mode": mode,
            "group_id": group_id,
            "group_index": group_index,
        }
        self.created.append(record)
        # 返回一个带 group_id 属性的对象(模拟 peewee VideoTask)
        return _Task(**record)

    def reset_daily_quotas(self, *_args, **_kw): pass
    def choose_available_account(self, *_args, **_kw): return None
    def assign_video_task(self, *_args, **_kw): pass
    def update_video_task(self, *_args, **_kw): pass
    def get_video_task(self, *_args, **_kw):
        return _Task(id="x", account=None, prompt="x", model="m", ratio="r", duration=5)
    def mark_account_limited(self, *_args, **_kw): pass
    def increment_account_quota(self, *_args, **_kw): pass


class _Task:
    def __init__(self, **kw):
        self.__dict__.update(kw)
        self.image_paths = None


class _StubService(VideoTaskService):
    """绕过 __init__,屏蔽 _schedule 不真起协程"""

    def __init__(self, repo):
        self.repository = repo
        self.assets_dir = None

    def _schedule(self, task_id):  # type: ignore[override]
        pass


@pytest.fixture
def service():
    repo = _StubRepo()
    svc = _StubService(repo)
    return svc, repo


def test_single_prompt_no_group(service):
    svc, repo = service
    task = svc.start(prompt="一只猫", model="seedance_v2.0_mini", ratio="1:1", duration=5, account_id=None)
    assert len(repo.created) == 1
    assert repo.created[0]["group_id"] is None
    assert repo.created[0]["group_index"] == 0


def test_prompts_list_auto_grouped(service):
    svc, repo = service
    task = svc.start(
        prompt="",
        prompts=["场景1", "场景2", "场景3", "场景4"],
        model="seedance_v2.0_mini",
        ratio="16:9",
        duration=5,
        account_id=None,
    )
    assert len(repo.created) == 4
    # 4 个共享同一 group_id
    group_ids = {t["group_id"] for t in repo.created}
    assert len(group_ids) == 1
    # group_index 从 1 开始递增
    indices = [t["group_index"] for t in repo.created]
    assert indices == [1, 2, 3, 4]
    # 任务 ID 是 task-1 / task-2 / task-3 / task-4
    prompts = [t["prompt"] for t in repo.created]
    assert prompts == ["场景1", "场景2", "场景3", "场景4"]


def test_prompt_and_prompts_merged(service):
    """同时传 prompt 和 prompts → prompt 排第一"""
    svc, repo = service
    svc.start(
        prompt="主prompt",
        prompts=["次1", "次2"],
        model="seedance_v2.0_mini",
        ratio="1:1",
        duration=5,
        account_id=None,
    )
    prompts = [t["prompt"] for t in repo.created]
    assert prompts == ["主prompt", "次1", "次2"]
    assert {t["group_id"] for t in repo.created}.pop() is not None


def test_empty_prompts_raises_value_error(service):
    svc, _ = service
    with pytest.raises(ValueError, match="请输入画面描述"):
        svc.start(prompt="", prompts=[], model="seedance_v2.0_mini", ratio="1:1", duration=5, account_id=None)
    # 全空白也拒绝
    with pytest.raises(ValueError, match="请输入画面描述"):
        svc.start(prompt="   ", prompts=["  "], model="seedance_v2.0_mini", ratio="1:1", duration=5, account_id=None)


def test_blank_prompts_filtered(service):
    """prompts 列表里有空白 → 过滤掉"""
    svc, repo = service
    svc.start(
        prompt="",
        prompts=["有效", "  ", "", "另一个有效"],
        model="seedance_v2.0_mini",
        ratio="1:1",
        duration=5,
        account_id=None,
    )
    prompts = [t["prompt"] for t in repo.created]
    assert prompts == ["有效", "另一个有效"]
