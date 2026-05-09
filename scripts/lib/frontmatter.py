#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Tuple


_FM_START = re.compile(r"^\s*---\s*$")
_FM_KV = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*:\s*(.*?)\s*$")


def parse_frontmatter(md_text: str) -> Tuple[Dict[str, Any], str]:
    """
    极简 frontmatter 解析器：
    - 仅支持第一段 `---` ... `---`
    - 仅解析 key: value 的单行形式
    - 对 `[...]` 形式做简单列表解析
    返回：(meta, body)
    """
    lines = md_text.splitlines()
    if not lines or not _FM_START.match(lines[0]):
        return {}, md_text

    meta: Dict[str, Any] = {}
    i = 1
    while i < len(lines) and not _FM_START.match(lines[i]):
        line = lines[i]
        m = _FM_KV.match(line)
        if m:
            k, v = m.group(1), m.group(2)
            v = v.strip().strip('"').strip("'")
            # 简单列表解析
            if v.startswith("[") and v.endswith("]"):
                inner = v[1:-1].strip()
                if inner:
                    parts = [x.strip().strip('"').strip("'") for x in inner.split(",")]
                    meta[k] = [p for p in parts if p]
                else:
                    meta[k] = []
            else:
                meta[k] = v
        i += 1

    # 找到结束分隔线
    if i < len(lines) and _FM_START.match(lines[i]):
        body = "\n".join(lines[i + 1 :]).lstrip("\n")
        return meta, body
    return {}, md_text


def parse_frontmatter_file(path: Path) -> Tuple[Dict[str, Any], str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    return parse_frontmatter(text)

