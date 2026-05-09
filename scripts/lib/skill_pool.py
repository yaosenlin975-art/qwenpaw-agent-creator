#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


WORD_RE = re.compile(r"[A-Za-z0-9_\u4e00-\u9fff]+")


def tokenize(s: str) -> List[str]:
    return [w.lower() for w in WORD_RE.findall(s or "") if w.strip()]


@dataclass
class SkillInfo:
    name: str
    description: str
    source: str
    signature: str = ""
    version_text: str = ""
    path: Optional[Path] = None


def load_skill_pool_manifest(skill_pool_dir: Path) -> Dict[str, SkillInfo]:
    manifest = skill_pool_dir / "skill.json"
    if not manifest.exists():
        return {}
    obj = json.loads(manifest.read_text(encoding="utf-8"))
    skills = obj.get("skills", {}) if isinstance(obj, dict) else {}
    out: Dict[str, SkillInfo] = {}
    for name, meta in skills.items():
        if not isinstance(meta, dict):
            continue
        out[name] = SkillInfo(
            name=name,
            description=str(meta.get("description", "")),
            source=str(meta.get("source", "builtin")),
            signature=str(meta.get("signature", "")),
            version_text=str(meta.get("version_text", "")),
            path=skill_pool_dir / name,
        )
    return out


def score_skill(skill: SkillInfo, keywords: List[str]) -> float:
    """
    非 ML 的轻量打分：关键词覆盖 + 名称加权。
    """
    if not keywords:
        return 0.0
    blob = " ".join([skill.name, skill.description])
    toks = tokenize(blob)
    if not toks:
        return 0.0
    tokset = set(toks)
    hits = 0
    for kw in keywords:
        if kw.lower() in tokset:
            hits += 1
    base = hits / max(len(keywords), 1)
    # 名称命中加权
    if any(kw.lower() in skill.name.lower() for kw in keywords):
        base += 0.2
    return min(base, 1.0)


def find_best_local_skills(skill_pool_dir: Path, keywords: List[str], top_n: int = 5) -> List[Tuple[SkillInfo, float]]:
    skills = load_skill_pool_manifest(skill_pool_dir)
    ranked: List[Tuple[SkillInfo, float]] = []
    for s in skills.values():
        ranked.append((s, score_skill(s, keywords)))
    ranked.sort(key=lambda x: x[1], reverse=True)
    return [(s, sc) for (s, sc) in ranked if sc > 0][:top_n]


def is_similar_skill(name_a: str, name_b: str) -> bool:
    """
    粗略“相似”判断：同名/包含关系/去掉下划线和短横后相同。
    """
    if name_a == name_b:
        return True
    na = re.sub(r"[-_]", "", name_a.lower())
    nb = re.sub(r"[-_]", "", name_b.lower())
    if na == nb:
        return True
    if na in nb or nb in na:
        return True
    return False

