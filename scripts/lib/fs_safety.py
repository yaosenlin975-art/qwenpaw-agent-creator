#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def atomic_write_bytes(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + f".tmp.{os.getpid()}")
    with tmp.open("wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)


def backup_same_dir(target: Path) -> Optional[Path]:
    if not target.exists():
        return None
    bak = target.with_name(target.name + f".bak.{utc_ts()}")
    shutil.copy2(target, bak)
    return bak


def validate_json_obj(obj: Any) -> None:
    # 确保可序列化 + 可反序列化（避免 NaN 等）
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    json.loads(raw.decode("utf-8"))


def safe_write_json(target: Path, obj: Any, write: bool) -> dict:
    """
    写入 JSON（含校验 + 同目录备份 + 原子替换 + 写后再校验）。
    返回：{"path":..., "backup":...}
    """
    validate_json_obj(obj)
    if not write:
        return {"path": str(target), "backup": None, "written": False}

    bak = backup_same_dir(target)
    data = json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    atomic_write_bytes(target, data)

    # 写后再校验
    json.loads(target.read_text(encoding="utf-8"))
    return {"path": str(target), "backup": str(bak) if bak else None, "written": True}


def safe_mkdir(target: Path, write: bool) -> dict:
    if not write:
        return {"path": str(target), "created": False}
    target.mkdir(parents=True, exist_ok=True)
    return {"path": str(target), "created": True}


def safe_rename_dir_if_exists(path: Path, write: bool) -> dict:
    """
    若目录存在则重命名为 .bak.<ts>（用于 force 场景）
    """
    if not path.exists():
        return {"path": str(path), "renamed_to": None}
    renamed = path.with_name(path.name + f".bak.{utc_ts()}")
    if write:
        path.rename(renamed)
    return {"path": str(path), "renamed_to": str(renamed)}

