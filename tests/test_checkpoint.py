"""Tests for the Checkpoint system: save/load, rotation, and clear."""

from __future__ import annotations


def test_checkpoint_roundtrip(tmp_path) -> None:
    from web_crawler.ai.checkpoint import Checkpoint, CheckpointStore

    store = CheckpointStore(tmp_path / "cps", keep=3)
    cp = Checkpoint(
        task_id="task-1",
        step=5,
        url="https://example.com/page",
        task="extract sign param",
        target_params_found={"sign": "abc123"},
        target_params=["sign"],
        hooks=["fetch_hook", "xhr_hook"],
        history=[{"step": 1, "action": "navigate"}],
        cumulative_summary="已注入 fetch_hook",
    )
    saved_path = store.save(cp)
    assert saved_path.exists()

    loaded = store.load_latest("task-1")
    assert loaded is not None
    assert loaded.step == 5
    assert loaded.url == "https://example.com/page"
    assert loaded.target_params_found == {"sign": "abc123"}
    assert loaded.hooks == ["fetch_hook", "xhr_hook"]


def test_checkpoint_rotation_keeps_recent(tmp_path) -> None:
    from web_crawler.ai.checkpoint import Checkpoint, CheckpointStore

    store = CheckpointStore(tmp_path / "cps", keep=2)
    for step in range(1, 5):
        store.save(Checkpoint(task_id="task-2", step=step, url="https://x.example"))
    files = store.list_checkpoints("task-2")
    assert len(files) == 2
    # 仅保留最新两个
    loaded = store.load_latest("task-2")
    assert loaded is not None
    assert loaded.step == 4


def test_checkpoint_load_at_specific_step(tmp_path) -> None:
    from web_crawler.ai.checkpoint import Checkpoint, CheckpointStore

    store = CheckpointStore(tmp_path / "cps")
    for step in [1, 5, 10]:
        store.save(Checkpoint(task_id="t3", step=step, url=f"https://x.example/{step}"))
    loaded = store.load_at("t3", 5)
    assert loaded is not None
    assert loaded.url == "https://x.example/5"
    # 加载不存在的 step
    assert store.load_at("t3", 99) is None


def test_checkpoint_manager_disabled_returns_none() -> None:
    from web_crawler.ai.checkpoint import CheckpointManager

    mgr = CheckpointManager(enable=False)
    mgr.task_id = "some-task"
    assert mgr.load_latest() is None


def test_checkpoint_clear(tmp_path) -> None:
    from web_crawler.ai.checkpoint import Checkpoint, CheckpointStore

    store = CheckpointStore(tmp_path / "cps")
    store.save(Checkpoint(task_id="clear-me", step=1, url="https://x.example"))
    assert store.load_latest("clear-me") is not None
    store.clear("clear-me")
    assert store.load_latest("clear-me") is None
