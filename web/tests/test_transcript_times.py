#!/usr/bin/env python3
"""transcript 时刻读取器直测 —— 真 JSONL 解析，临时目录 + CLAUDE_CONFIG_DIR。

读取器是事件时刻透传的对齐表源（SDK 的 SessionMessage 丢了 timestamp，
时刻只能自读 transcript 找回）：session_id → {uuid: epoch 秒}。缺文件/
坏行/行缺 uuid 或 timestamp 降级为跳过或空 map，不抛——簿记增强不反过来
阻断恢复。纯 assert，无 pytest。

运行：python web/tests/test_transcript_times.py
"""
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from web.sdk import _sanitize_project_dir_name, transcript_times  # noqa: E402


@contextmanager
def claude_config():
    """临时 CLAUDE_CONFIG_DIR（读取器与 CLI 的同一约定）；用毕还原。"""
    old = os.environ.get("CLAUDE_CONFIG_DIR")
    cfg = tempfile.mkdtemp()
    os.environ["CLAUDE_CONFIG_DIR"] = cfg
    try:
        yield Path(cfg)
    finally:
        if old is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old


def fresh_project_root():
    """临时项目根（真实目录，git worktree 探测在此安静失败只剩它自己）。"""
    return str(Path(tempfile.mkdtemp()).resolve())


def transcript_file(cfg, project_root, session_id):
    """临时布局里的 transcript 路径（目录名按 CLI 约定派生）。"""
    d = cfg / "projects" / _sanitize_project_dir_name(project_root)
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{session_id}.jsonl"


def row(**fields):
    """transcript 行 JSON；uuid/timestamp 给不给由用例自己定。"""
    return json.dumps(fields)


def test_extracts_uuid_to_epoch_per_line():
    sid = "11111111-2222-3333-4444-555555555555"
    root = fresh_project_root()
    with claude_config() as cfg:
        transcript_file(cfg, root, sid).write_text("\n".join([
            # 无 uuid 的元数据行（queue-operation 等）天然不进对齐表
            row(type="queue-operation", timestamp="2026-09-04T01:00:00.000Z"),
            row(type="user", uuid="u1", timestamp="2026-09-04T01:02:03.400Z",
                message={"role": "user", "content": "部署 nginx"}),
            row(type="assistant", uuid="u2", timestamp="2026-09-04T01:05:06.789Z",
                message={"role": "assistant"}),
            # 快照行：uuid 与 timestamp 都缺
            row(type="file-history-snapshot"),
        ]) + "\n", encoding="utf-8")
        assert transcript_times(sid, root) == {
            "u1": datetime.fromisoformat("2026-09-04T01:02:03.400+00:00").timestamp(),
            "u2": datetime.fromisoformat("2026-09-04T01:05:06.789+00:00").timestamp(),
        }


def test_skips_lines_missing_uuid_or_timestamp():
    sid = "11111111-2222-3333-4444-555555555555"
    root = fresh_project_root()
    with claude_config() as cfg:
        transcript_file(cfg, root, sid).write_text("\n".join([
            row(type="user", uuid="no-ts"),  # 有 uuid 无 timestamp
            row(type="assistant", timestamp="2026-09-04T01:02:03.400Z"),  # 反之
        ]) + "\n", encoding="utf-8")
        assert transcript_times(sid, root) == {}


def test_skips_broken_lines_and_unparseable_timestamps():
    sid = "11111111-2222-3333-4444-555555555555"
    root = fresh_project_root()
    with claude_config() as cfg:
        transcript_file(cfg, root, sid).write_text("\n".join([
            "not-json{",
            "",
            "null",
            row(type="user", uuid="u-bad-ts", timestamp="not-a-date"),
            row(type="user", uuid="u1", timestamp="2026-09-04T01:02:03.400Z"),
        ]) + "\n", encoding="utf-8")
        assert transcript_times(sid, root) == {
            "u1": datetime.fromisoformat("2026-09-04T01:02:03.400+00:00").timestamp(),
        }


def test_missing_file_returns_empty_map():
    root = fresh_project_root()
    with claude_config():
        assert transcript_times("99999999-8888-7777-6666-555555555555", root) == {}


def main():
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_")]
    for fn in tests:
        fn()
        print(f"ok {fn.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    main()
