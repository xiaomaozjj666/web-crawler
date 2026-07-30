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


# ---------------------------------------------------------------------------
# 以下为补充测试，覆盖 Checkpoint 数据类、Store 错误路径与 Manager 全部分支
# ---------------------------------------------------------------------------


def test_checkpoint_to_dict_and_to_json_roundtrip() -> None:
    """Checkpoint.to_dict / to_json 应保持字段一致。"""
    from web_crawler.ai.checkpoint import Checkpoint

    cp = Checkpoint(
        task_id="t-roundtrip",
        step=3,
        url="https://x.example/p",
        task="test task",
        target_params_found={"k": "v"},
        target_params=["k"],
        hooks=["h1"],
        history=[{"step": 1}],
        cumulative_summary="summary",
        metadata={"foo": "bar"},
    )
    d = cp.to_dict()
    assert d["task_id"] == "t-roundtrip"
    assert d["step"] == 3
    assert d["metadata"] == {"foo": "bar"}

    import json

    parsed = json.loads(cp.to_json())
    assert parsed["url"] == "https://x.example/p"
    assert parsed["hooks"] == ["h1"]


def test_checkpoint_from_dict_tolerates_missing_fields() -> None:
    """from_dict 缺字段时走默认值。"""
    from web_crawler.ai.checkpoint import Checkpoint

    cp = Checkpoint.from_dict({"task_id": "x"})
    assert cp.task_id == "x"
    assert cp.step == 0
    assert cp.url == ""
    assert cp.target_params_found == {}
    assert cp.target_params == []
    assert cp.hooks == []
    assert cp.history == []
    assert cp.cumulative_summary == ""
    assert cp.metadata == {}


def test_checkpoint_from_dict_handles_none_values() -> None:
    """from_dict 对 None 字段降级为默认值。"""
    from web_crawler.ai.checkpoint import Checkpoint

    cp = Checkpoint.from_dict(
        {
            "task_id": "t-none",
            "step": 2,
            "target_params_found": None,
            "target_params": None,
            "hooks": None,
            "history": None,
            "metadata": None,
        }
    )
    assert cp.target_params_found == {}
    assert cp.target_params == []
    assert cp.hooks == []
    assert cp.history == []
    assert cp.metadata == {}


def test_checkpoint_store_load_latest_returns_none_when_dir_missing(tmp_path) -> None:
    """任务目录不存在时 load_latest 返回 None。"""
    from web_crawler.ai.checkpoint import CheckpointStore

    store = CheckpointStore(tmp_path / "cps")
    assert store.load_latest("nonexistent-task") is None


def test_checkpoint_store_load_latest_returns_none_when_no_files(tmp_path) -> None:
    """任务目录存在但无 checkpoint 文件时返回 None。"""
    from web_crawler.ai.checkpoint import CheckpointStore

    store = CheckpointStore(tmp_path / "cps")
    # 手动创建空任务目录
    (tmp_path / "cps" / "empty-task").mkdir(parents=True)
    assert store.load_latest("empty-task") is None


def test_checkpoint_store_load_latest_returns_none_on_corrupt_json(tmp_path) -> None:
    """JSON 损坏时 load_latest 返回 None。"""
    from web_crawler.ai.checkpoint import CheckpointStore

    store = CheckpointStore(tmp_path / "cps")
    task_dir = tmp_path / "cps" / "bad-task"
    task_dir.mkdir(parents=True)
    (task_dir / "step-0001.json").write_text("not json", encoding="utf-8")
    assert store.load_latest("bad-task") is None


def test_checkpoint_store_load_at_returns_none_on_corrupt_json(tmp_path) -> None:
    """JSON 损坏时 load_at 返回 None。"""
    from web_crawler.ai.checkpoint import CheckpointStore

    store = CheckpointStore(tmp_path / "cps")
    task_dir = tmp_path / "cps" / "bad-at"
    task_dir.mkdir(parents=True)
    (task_dir / "step-0005.json").write_text("corrupt", encoding="utf-8")
    assert store.load_at("bad-at", 5) is None


def test_checkpoint_store_load_at_returns_none_when_file_missing(tmp_path) -> None:
    """load_at 对不存在文件返回 None。"""
    from web_crawler.ai.checkpoint import CheckpointStore

    store = CheckpointStore(tmp_path / "cps")
    assert store.load_at("missing", 1) is None


def test_checkpoint_store_list_checkpoints_empty_when_dir_missing(tmp_path) -> None:
    """list_checkpoints 在目录不存在时返回空列表。"""
    from web_crawler.ai.checkpoint import CheckpointStore

    store = CheckpointStore(tmp_path / "cps")
    assert store.list_checkpoints("no-such-task") == []


def test_checkpoint_store_clear_when_not_exists(tmp_path) -> None:
    """clear 对不存在的任务目录不抛异常。"""
    from web_crawler.ai.checkpoint import CheckpointStore

    store = CheckpointStore(tmp_path / "cps")
    # 目录不存在，clear 应直接 return 不抛
    store.clear("never-existed")


def test_checkpoint_store_clear_removes_directory(tmp_path) -> None:
    """clear 后任务目录应被删除。"""
    from web_crawler.ai.checkpoint import Checkpoint, CheckpointStore

    store = CheckpointStore(tmp_path / "cps")
    store.save(Checkpoint(task_id="to-remove", step=1, url="https://x"))
    task_dir = tmp_path / "cps" / "to-remove"
    assert task_dir.is_dir()
    store.clear("to-remove")
    assert not task_dir.exists()


def test_checkpoint_store_clear_swallows_unlink_oserror(tmp_path, monkeypatch) -> None:
    """unlink 抛 OSError 时被吞掉，rmdir 仍尝试。"""
    from web_crawler.ai.checkpoint import Checkpoint, CheckpointStore

    store = CheckpointStore(tmp_path / "cps")
    store.save(Checkpoint(task_id="oserror-task", step=1, url="https://x"))
    task_dir = tmp_path / "cps" / "oserror-task"

    # 让 Path.unlink 抛 OSError
    import pathlib

    original_unlink = pathlib.Path.unlink

    def raising_unlink(self: pathlib.Path, *args: object, **kwargs: object) -> None:
        if self.parent == task_dir:
            raise OSError("simulated")
        original_unlink(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(pathlib.Path, "unlink", raising_unlink)
    # 不应抛异常
    store.clear("oserror-task")


def test_checkpoint_store_clear_swallows_rmdir_oserror(tmp_path, monkeypatch) -> None:
    """rmdir 抛 OSError 时被吞掉。"""
    from web_crawler.ai.checkpoint import Checkpoint, CheckpointStore

    store = CheckpointStore(tmp_path / "cps")
    store.save(Checkpoint(task_id="rmdir-task", step=1, url="https://x"))

    import pathlib

    original_rmdir = pathlib.Path.rmdir

    def raising_rmdir(self: pathlib.Path, *args: object, **kwargs: object) -> None:
        if self.name == "rmdir-task":
            raise OSError("simulated rmdir failure")
        original_rmdir(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(pathlib.Path, "rmdir", raising_rmdir)
    # 不应抛
    store.clear("rmdir-task")


def test_checkpoint_store_rotate_keeps_limit(tmp_path) -> None:
    """_rotate 应保留最近 N 个文件，删除更早的。"""
    from web_crawler.ai.checkpoint import Checkpoint, CheckpointStore

    store = CheckpointStore(tmp_path / "cps", keep=2)
    for step in range(1, 6):
        store.save(Checkpoint(task_id="rot", step=step, url=f"https://x/{step}"))
    files = store.list_checkpoints("rot")
    assert len(files) == 2
    # 仅保留 step 5 与 step 4
    names = sorted(f.name for f in files)
    assert names == ["step-0004.json", "step-0005.json"]


def test_checkpoint_store_rotate_no_op_when_under_limit(tmp_path) -> None:
    """文件数 ≤ keep 时 _rotate 不删除。"""
    from web_crawler.ai.checkpoint import Checkpoint, CheckpointStore

    store = CheckpointStore(tmp_path / "cps", keep=10)
    store.save(Checkpoint(task_id="under", step=1, url="https://x"))
    files = store.list_checkpoints("under")
    assert len(files) == 1


def test_checkpoint_store_safe_id_escapes_special_chars(tmp_path) -> None:
    """_task_dir 应将特殊字符替换为下划线。"""
    from web_crawler.ai.checkpoint import CheckpointStore

    store = CheckpointStore(tmp_path / "cps")
    task_dir = store._task_dir("task/with..special chars!")
    # 所有非字母数字/-_ 都被替换
    assert "/" not in task_dir.name
    assert " " not in task_dir.name
    assert "!" not in task_dir.name


def test_checkpoint_manager_save_returns_none_when_disabled() -> None:
    """enable=False 时 save 返回 None。"""
    from web_crawler.ai.checkpoint import Checkpoint, CheckpointManager

    mgr = CheckpointManager(enable=False)
    cp = Checkpoint(task_id="", step=1, url="https://x", task="t")
    assert mgr.save(cp) is None


def test_checkpoint_manager_save_skips_non_interval_steps(tmp_path) -> None:
    """save_interval=3 时仅 step 3/6/9 保存。"""
    from web_crawler.ai.checkpoint import Checkpoint, CheckpointManager, CheckpointStore

    mgr = CheckpointManager(
        task_id="interval-task",
        store=CheckpointStore(tmp_path / "cps"),
        save_interval=3,
    )
    cp1 = Checkpoint(task_id="", step=1, url="https://x", task="t")
    assert mgr.save(cp1) is None
    cp2 = Checkpoint(task_id="", step=2, url="https://x", task="t")
    assert mgr.save(cp2) is None
    cp3 = Checkpoint(task_id="", step=3, url="https://x", task="t")
    saved = mgr.save(cp3)
    assert saved is not None
    assert saved.exists()


def test_checkpoint_manager_save_step_zero_always_saves(tmp_path) -> None:
    """step=0 时即使不满足 save_interval 也保存（初始 checkpoint）。"""
    from web_crawler.ai.checkpoint import Checkpoint, CheckpointManager, CheckpointStore

    mgr = CheckpointManager(
        task_id="zero-task",
        store=CheckpointStore(tmp_path / "cps"),
        save_interval=5,
    )
    cp = Checkpoint(task_id="", step=0, url="https://x", task="t")
    saved = mgr.save(cp)
    assert saved is not None
    assert saved.exists()


def test_checkpoint_manager_load_latest_returns_none_when_no_task_id() -> None:
    """task_id 为空时 load_latest 直接返回 None。"""
    from web_crawler.ai.checkpoint import CheckpointManager

    mgr = CheckpointManager(enable=True)
    mgr.task_id = ""
    assert mgr.load_latest() is None


def test_checkpoint_manager_clear_noop_when_no_task_id() -> None:
    """task_id 为空时 clear 不抛异常。"""
    from web_crawler.ai.checkpoint import CheckpointManager

    mgr = CheckpointManager(enable=True)
    mgr.task_id = ""
    mgr.clear()  # 不应抛


def test_checkpoint_manager_clear_noop_when_disabled() -> None:
    """enable=False 时 clear 不抛异常。"""
    from web_crawler.ai.checkpoint import CheckpointManager

    mgr = CheckpointManager(enable=False)
    mgr.task_id = "some-task"
    mgr.clear()  # 不应抛


def test_checkpoint_manager_ensure_task_id_auto_generates() -> None:
    """task_id 为空时 _ensure_task_id 自动生成 md5 哈希。"""
    from web_crawler.ai.checkpoint import CheckpointManager

    mgr = CheckpointManager(enable=True)
    mgr.task_id = ""
    generated = mgr._ensure_task_id(url="https://x", task="t")
    assert generated
    assert len(generated) == 12
    # 二次调用应返回相同值（已缓存）
    assert mgr._ensure_task_id() == generated


def test_checkpoint_manager_save_uses_existing_task_id() -> None:
    """已有 task_id 时 save 不重新生成。"""
    from web_crawler.ai.checkpoint import Checkpoint, CheckpointManager

    mgr = CheckpointManager(task_id="predefined-id")
    cp = Checkpoint(task_id="", step=1, url="https://x", task="t")
    mgr.save(cp)
    assert cp.task_id == "predefined-id"


def test_checkpoint_manager_load_latest_roundtrip(tmp_path) -> None:
    """save → load_latest 完整往返。"""
    from web_crawler.ai.checkpoint import Checkpoint, CheckpointManager, CheckpointStore

    mgr = CheckpointManager(
        task_id="roundtrip-task",
        store=CheckpointStore(tmp_path / "cps"),
    )
    cp = Checkpoint(
        task_id="",
        step=4,
        url="https://x/y",
        task="do something",
        target_params_found={"a": "1"},
        target_params=["a"],
        hooks=["h"],
        history=[{"step": 1}],
        cumulative_summary="sum",
        metadata={"k": "v"},
    )
    mgr.save(cp)
    loaded = mgr.load_latest()
    assert loaded is not None
    assert loaded.step == 4
    assert loaded.url == "https://x/y"
    assert loaded.target_params_found == {"a": "1"}
    assert loaded.metadata == {"k": "v"}


def test_checkpoint_manager_clear_removes_files(tmp_path) -> None:
    """clear 后 load_latest 返回 None。"""
    from web_crawler.ai.checkpoint import Checkpoint, CheckpointManager, CheckpointStore

    mgr = CheckpointManager(
        task_id="clear-task",
        store=CheckpointStore(tmp_path / "cps"),
    )
    mgr.save(Checkpoint(task_id="", step=1, url="https://x", task="t"))
    assert mgr.load_latest() is not None
    mgr.clear()
    assert mgr.load_latest() is None


def test_checkpoint_manager_build_checkpoint_constructs_object() -> None:
    """build_checkpoint 便捷方法应正确构造 Checkpoint。"""
    from web_crawler.ai.checkpoint import CheckpointManager

    mgr = CheckpointManager(task_id="build-task")
    cp = mgr.build_checkpoint(
        step=7,
        url="https://x.example/p",
        task="build test",
        target_params_found={"k": "v"},
        target_params=["k"],
        hooks=["h1", "h2"],
        history=[{"step": 1}, {"step": 2}],
        cumulative_summary="cumulative",
        metadata={"m": 1},
    )
    assert cp.task_id == "build-task"
    assert cp.step == 7
    assert cp.url == "https://x.example/p"
    assert cp.task == "build test"
    assert cp.target_params_found == {"k": "v"}
    assert cp.target_params == ["k"]
    assert cp.hooks == ["h1", "h2"]
    assert cp.history == [{"step": 1}, {"step": 2}]
    assert cp.cumulative_summary == "cumulative"
    assert cp.metadata == {"m": 1}


def test_checkpoint_manager_build_checkpoint_with_none_optionals() -> None:
    """build_checkpoint 对 None 可选字段降级为空集合。"""
    from web_crawler.ai.checkpoint import CheckpointManager

    mgr = CheckpointManager(task_id="build-none")
    cp = mgr.build_checkpoint(
        step=1,
        url="https://x",
        task="t",
        target_params_found={},
        target_params=None,
        hooks=None,
        history=[],
        metadata=None,
    )
    assert cp.target_params == []
    assert cp.hooks == []
    assert cp.metadata == {}


def test_checkpoint_default_keep_at_least_one() -> None:
    """keep<1 时被钳制为 1。"""
    from web_crawler.ai.checkpoint import CheckpointStore

    store = CheckpointStore(keep=0)
    assert store.keep == 1


def test_checkpoint_store_save_creates_parent_dir(tmp_path) -> None:
    """save 应自动创建多层父目录。"""
    from web_crawler.ai.checkpoint import Checkpoint, CheckpointStore

    store = CheckpointStore(tmp_path / "deep" / "nested" / "cps")
    cp = Checkpoint(task_id="deep-task", step=1, url="https://x")
    saved = store.save(cp)
    assert saved.exists()
    assert saved.parent.is_dir()
