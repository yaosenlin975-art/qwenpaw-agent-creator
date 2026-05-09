#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import re
import shutil
import subprocess
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


CLAWZIP_ENDPOINT = "https://wry-manatee-359.convex.site/api/v1/download?slug={slug}"
URL_IN_TEXT_RE = re.compile(r"https?://clawhub\.ai/[A-Za-z0-9_-]+/([A-Za-z0-9_-]+)")


@dataclass
class OnlineSkillCandidate:
    slug: str
    source: str  # clawhub


def has_npx() -> bool:
    return shutil.which("npx") is not None


def clawhub_search_slugs(query: str, limit: int = 5, timeout_sec: int = 60) -> List[str]:
    """
    优先使用 `npx clawhub search "<query>"`。
    由于 clawhub CLI 输出格式可能变化，这里采用“从输出中提取 clawhub.ai URL”的稳健策略。
    """
    if not has_npx():
        return []
    try:
        proc = subprocess.run(
            ["npx", "clawhub", "search", query],
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except Exception:
        return []
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    slugs = []
    for m in URL_IN_TEXT_RE.finditer(out):
        slugs.append(m.group(1))
    # 去重保序
    seen = set()
    uniq = []
    for s in slugs:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq[:limit]


def download_clawhub_zip(slug: str, dest_zip: Path, timeout_sec: int = 120) -> None:
    url = CLAWZIP_ENDPOINT.format(slug=urllib.parse.quote(slug))
    req = urllib.request.Request(url, headers={"User-Agent": "copaw-agent-creator/0.1"})
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        data = resp.read()
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    dest_zip.write_bytes(data)


def unzip_to_dir(zip_path: Path, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(dest_dir)


def locate_skill_dir(extracted_root: Path) -> Optional[Path]:
    """
    clawhub 下载的 zip 里通常是单个 skill 目录（包含 SKILL.md）。
    这里做一个简单探测：找到包含 SKILL.md 的最近目录。
    """
    for p in extracted_root.rglob("SKILL.md"):
        return p.parent
    return None

