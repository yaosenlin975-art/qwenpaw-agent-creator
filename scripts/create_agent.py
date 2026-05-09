#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
创建 CoPaw 智能体（workspace）并注册到前端可见列表，同时按需求自动装配技能：
1) 本地 skill_pool 匹配并导入
2) 在线 clawhub 检索并导入（默认尝试 npx clawhub search；失败则跳过并提示）
3) 若 1+2 未导入任何技能，则生成一个最小新技能

写入安全：
- 默认只 dry-run，不写入
- 只有 --write 才会落盘
- JSON 写入：校验合法 JSON + 同目录备份 + 原子替换 + 写后再校验
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from lib.clawhub import clawhub_search_slugs, download_clawhub_zip, locate_skill_dir, unzip_to_dir
from lib.frontmatter import parse_frontmatter_file
from lib.fs_safety import safe_mkdir, safe_rename_dir_if_exists, safe_write_json, utc_ts
from lib.skill_pool import SkillInfo, find_best_local_skills, is_similar_skill, tokenize


def slugify_agent_id(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return "new_agent"
    s = s.lower()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^a-z0-9_-]", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "new_agent"


def guess_copaw_root() -> Path:
    return Path.home() / ".copaw"


def guess_secret_root() -> Path:
    return Path.home() / ".copaw.secret"


def detect_registry(copaw_root: Path, registry_override: str = "") -> Path:
    """
    兼容：如果存在 agents.json 则优先；否则使用 config.json。
    """
    if registry_override:
        return Path(registry_override).expanduser()
    agents_json = copaw_root / "agents.json"
    if agents_json.exists():
        return agents_json
    return copaw_root / "config.json"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def ensure_rules_activated(registry_path: Path, write: bool) -> Dict[str, Any]:
    """
    确保 RULES.md 在 config.json 的 system_prompt_files 中激活。
    RULES.md 是 Agent 的死规定文件，必须全局生效。
    """
    result: Dict[str, Any] = {"rules_in_config": False, "added": False, "skipped": False}

    if not registry_path.exists():
        result["skipped"] = True
        return result

    try:
        obj = load_json(registry_path)
    except Exception:
        result["skipped"] = True
        return result

    defaults = obj.get("defaults", {})
    spf: List[str] = defaults.get("system_prompt_files", [])

    result["rules_in_config"] = "RULES.md" in spf

    if "RULES.md" not in spf:
        spf.append("RULES.md")
        defaults["system_prompt_files"] = spf
        obj["defaults"] = defaults
        result["added"] = True
        if write:
            safe_write_json(registry_path, obj, write=write)
    else:
        result["skipped"] = True

    return result


def ensure_workspace_template(default_ws: Path, new_ws: Path, write: bool) -> Dict[str, Any]:
    """
    模板优先级：
    1. default workspace（用户自定义模板）
    2. repo 内置 template/ 目录（技能包自带模板）
    3. 硬编码最小文件集
    """
    actions: Dict[str, Any] = {"copy_from_default": False, "from_template": False, "created_minimal": False, "files": []}

    # 1. 优先复制 default workspace
    if default_ws.exists() and default_ws.is_dir():
        actions["copy_from_default"] = True
        if write:
            shutil.copytree(default_ws, new_ws, dirs_exist_ok=False)
        actions["files"] = [p.name for p in default_ws.iterdir()]
        return actions

    # 2. 尝试从 repo template/ 目录复制
    script_dir = Path(__file__).parent.resolve()
    repo_root = script_dir.parent
    template_dir = repo_root / "template"
    if template_dir.exists() and template_dir.is_dir():
        actions["from_template"] = True
        if write:
            shutil.copytree(template_dir, new_ws, dirs_exist_ok=False)
        actions["files"] = [p.name for p in template_dir.iterdir()]
        return actions

    # 3. 硬编码最小文件集
    actions["created_minimal"] = True
    safe_mkdir(new_ws, write=write)
    md_defaults = {
        "AGENTS.md": "# AGENTS\n\n这里写该智能体的工作规范与边界。\n",
        "MEMORY.md": "# MEMORY\n\n这里写长期记忆与经验教训（避免敏感信息）。\n",
        "PROFILE.md": "# PROFILE\n\n这里写身份与用户资料（按需更新）。\n",
        "SOUL.md": "# SOUL\n\n这里写人格、风格与原则。\n",
        "HEARTBEAT.md": "# Heartbeat checklist\n- （按需填写）\n",
        "BOOTSTRAP.md": "# BOOTSTRAP\n\n新会话启动时的引导。\n",
        "RULES.md": "# RULES.md\n\n## Agent 死规定\n\n1. 所有死规定统一写入 RULES.md\n2. 死规定冲突时必须协商\n3. 修改带格式文件必须 temp 校验 + 备份\n4. 需求确认后再执行\n",
    }
    if write:
        for fn, content in md_defaults.items():
            (new_ws / fn).write_text(content, encoding="utf-8")
        (new_ws / "chats.json").write_text('{\n  "chats": [],\n  "version": 1\n}\n', encoding="utf-8")
        (new_ws / "jobs.json").write_text("{}\n", encoding="utf-8")
    actions["files"] = sorted(md_defaults.keys())
    return actions


def patch_agent_json(agent_json_path: Path, agent_id: str, name: str, description: str, ws_dir: Path, write: bool, model: str = "MiniMax-M2.7") -> Dict[str, Any]:
    """
    更新 agent.json 文件，包含正确的模型配置和渠道过滤设置
    """
    if agent_json_path.exists():
        obj = load_json(agent_json_path)
    else:
        obj = {}
    if not isinstance(obj, dict):
        obj = {}
    obj["id"] = agent_id
    obj["name"] = name
    obj["description"] = description
    obj["workspace_dir"] = str(ws_dir)
    
    # 添加正确的模型配置（与 default agent 一致）
    obj["active_model"] = {
        "provider_id": "minimax-custom",
        "model": model
    }
    
    # 设置渠道过滤配置：微信和企业微信渠道过滤工具使用和思考流程
    if "channels" not in obj:
        obj["channels"] = {}
    
    # 微信渠道过滤配置
    if "weixin" not in obj["channels"]:
        obj["channels"]["weixin"] = {}
    obj["channels"]["weixin"]["filter_tool_messages"] = True
    obj["channels"]["weixin"]["filter_thinking"] = True
    
    # 企业微信渠道过滤配置
    if "wecom" not in obj["channels"]:
        obj["channels"]["wecom"] = {}
    obj["channels"]["wecom"]["filter_tool_messages"] = True
    obj["channels"]["wecom"]["filter_thinking"] = True
    
    return safe_write_json(agent_json_path, obj, write=write)


def patch_workspace_skill_json(skill_json_path: Path, enabled_skills: List[Dict[str, Any]], write: bool) -> Dict[str, Any]:
    """
    尽量兼容 CoPaw workspace skill.json：
    - 保留原结构（若存在）
    - 确保 schema_version/version/skills 字段存在
    """
    if skill_json_path.exists():
        obj = load_json(skill_json_path)
    else:
        obj = {"schema_version": "workspace-skill-manifest.v1", "version": int(__import__("time").time() * 1000), "skills": {}}
    if not isinstance(obj, dict):
        obj = {"schema_version": "workspace-skill-manifest.v1", "version": int(__import__("time").time() * 1000), "skills": {}}
    obj.setdefault("schema_version", "workspace-skill-manifest.v1")
    obj.setdefault("version", int(__import__("time").time() * 1000))
    obj.setdefault("skills", {})
    skills_map = obj["skills"]
    if not isinstance(skills_map, dict):
        skills_map = {}
        obj["skills"] = skills_map

    for s in enabled_skills:
        name = s["name"]
        skills_map[name] = {
            "enabled": True,
            "channels": ["all"],
            "source": s.get("source", "custom"),
            "config": s.get("config", {}),
            "metadata": s.get("metadata", {"name": name, "description": s.get("description", "")}),
            "requirements": s.get("requirements", {"require_bins": [], "require_envs": []}),
            "updated_at": s.get("updated_at", ""),
        }

    obj["version"] = int(__import__("time").time() * 1000)
    return safe_write_json(skill_json_path, obj, write=write)


def search_and_import_additional_skills(agent_id: str, keywords: List[str], ws_dir: Path, write: bool) -> Dict[str, Any]:
    """
    使用 find-skills 技能为新的智能体查找更多合适职能的技能
    """
    import subprocess
    
    result = {
        "searched_keywords": keywords,
        "imported_skills": [],
        "failed_searches": []
    }
    
    if not write:
        return result
    
    skills_dir = ws_dir / "skills"
    safe_mkdir(skills_dir, write=write)
    
    # 使用 clawhub 搜索更多技能
    for keyword in keywords[:3]:  # 限制搜索前3个关键词
        try:
            # 使用 npx clawhub search 搜索技能
            cmd = ["npx", "clawhub", "search", keyword, "--limit", "2"]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            
            if proc.returncode == 0 and proc.stdout.strip():
                # 解析输出，尝试安装找到的技能
                lines = proc.stdout.strip().split("\n")
                for line in lines[:2]:  # 每个关键词最多安装2个技能
                    if "/" in line:
                        # 提取技能 slug
                        parts = line.strip().split()
                        if parts:
                            slug = parts[0].split("/")[-1] if "/" in parts[0] else parts[0]
                            try:
                                # 下载并安装技能
                                download_cmd = ["npx", "clawhub", "install", slug, "--dir", str(skills_dir)]
                                dl_proc = subprocess.run(download_cmd, capture_output=True, text=True, timeout=60)
                                if dl_proc.returncode == 0:
                                    result["imported_skills"].append(slug)
                            except Exception:
                                continue
        except Exception as e:
            result["failed_searches"].append({"keyword": keyword, "error": str(e)})
            continue
    
    return result


def add_multi_agent_collaboration_skill(ws_dir: Path, skill_pool_dir: Path, write: bool) -> Dict[str, Any]:
    """
    添加多智能体协作技能到智能体工作区
    """
    import shutil
    
    result = {
        "skill_added": False,
        "source": None,
        "skill_name": "multi_agent_collaboration"
    }
    
    if not write:
        return result
    
    skills_dir = ws_dir / "skills"
    safe_mkdir(skills_dir, write=write)
    
    # 检查是否已存在
    target_skill_dir = skills_dir / "multi_agent_collaboration"
    if target_skill_dir.exists():
        result["skill_added"] = True
        result["source"] = "already_exists"
        return result
    
    # 首先检查本地技能池
    local_skill_paths = [
        skill_pool_dir / "multi_agent_collaboration",
        Path("/app/working/skill_pool/multi_agent_collaboration"),
        Path("/app/working/workspaces/default/skills/multi_agent_collaboration")
    ]
    
    for local_path in local_skill_paths:
        if local_path.exists() and (local_path / "SKILL.md").exists():
            try:
                shutil.copytree(local_path, target_skill_dir)
                result["skill_added"] = True
                result["source"] = str(local_path)
                return result
            except Exception:
                continue
    
    # 如果本地没有，尝试从 clawhub 下载
    try:
        import subprocess
        cmd = ["npx", "clawhub", "install", "multi_agent_collaboration", "--dir", str(skills_dir)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode == 0:
            result["skill_added"] = True
            result["source"] = "clawhub"
    except Exception:
        pass
    
    return result


def test_agent_communication(agent_id: str, write: bool) -> Dict[str, Any]:
    """
    测试与新建智能体的通信，确保智能体创建正常
    """
    import subprocess
    
    result = {
        "test_sent": False,
        "response_received": False,
        "test_message": f"Hello {agent_id}, this is a test message to verify your setup.",
        "response": None
    }
    
    if not write:
        return result
    
    try:
        # 使用 copaw agents chat 命令发送测试消息
        cmd = ["copaw", "agents", "chat", agent_id, "--message", result["test_message"], "--timeout", "30"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        
        result["test_sent"] = True
        
        if proc.returncode == 0:
            result["response_received"] = True
            result["response"] = proc.stdout.strip()[:500]  # 限制响应长度
        else:
            result["response"] = f"Command failed: {proc.stderr[:200] if proc.stderr else 'Unknown error'}"
    except subprocess.TimeoutExpired:
        result["response"] = "Test timed out"
    except Exception as e:
        result["response"] = f"Test failed: {str(e)}"
    
    return result


def update_registry(registry_path: Path, agent_id: str, ws_dir: Path, write: bool, set_active: bool) -> Dict[str, Any]:
    """
    config.json (v1.0.x) 的 agents 字段：
      agents.profiles[agent_id] = {id, workspace_dir, enabled:true}
      agents.agent_order append
      agents.active_agent 不修改（除非 set_active=True）
    """
    obj = load_json(registry_path)
    if not isinstance(obj, dict):
        raise ValueError("registry 不是 JSON object")

    # config.json：顶层有 agents 字段
    if "agents" in obj and isinstance(obj["agents"], dict):
        agents = obj["agents"]
        profiles = agents.setdefault("profiles", {})
        if not isinstance(profiles, dict):
            profiles = {}
            agents["profiles"] = profiles
        profiles[agent_id] = {"id": agent_id, "workspace_dir": str(ws_dir), "enabled": True}

        order = agents.setdefault("agent_order", [])
        if not isinstance(order, list):
            order = []
            agents["agent_order"] = order
        if agent_id not in order:
            order.append(agent_id)

        if set_active:
            agents["active_agent"] = agent_id
    else:
        # agents.json 形态未知：保守处理
        # 最小：假设顶层就是 profiles + agent_order
        profiles = obj.setdefault("profiles", {})
        if not isinstance(profiles, dict):
            profiles = {}
            obj["profiles"] = profiles
        profiles[agent_id] = {"id": agent_id, "workspace_dir": str(ws_dir), "enabled": True}
        order = obj.setdefault("agent_order", [])
        if not isinstance(order, list):
            order = []
            obj["agent_order"] = order
        if agent_id not in order:
            order.append(agent_id)
        if set_active:
            obj["active_agent"] = agent_id

    return safe_write_json(registry_path, obj, write=write)


def copy_skill_dir(src: Path, dst: Path, write: bool) -> None:
    if not write:
        return
    shutil.copytree(src, dst, dirs_exist_ok=False)


def generate_minimal_skill(skill_dir: Path, skill_name: str, agent_goal: str, write: bool) -> Dict[str, Any]:
    """
    生成一个最小可用 skill（SKILL.md），遵循 skill-creator 的“简洁 + 渐进披露”原则。
    """
    safe_mkdir(skill_dir, write=write)
    skill_md = skill_dir / "SKILL.md"
    content = f"""---\nname: {skill_name}\ndescription: \"为该智能体提供最小可用的工作流指导：{agent_goal}\"\nmetadata: {{ \"copaw\": {{ \"emoji\": \"🛠️\" }}, \"skill_version\": \"0.1.0\" }}\n---\n\n# {skill_name}\n\n## 目的\n\n该技能是为新创建的智能体自动生成的“最小可用技能”，用于指导它完成：\n\n- {agent_goal}\n\n## 触发\n\n当用户明确要求该智能体执行上述目标或相关任务时使用。\n\n## 工作流（保持精简）\n\n1. 用 3-5 句话复述任务目标与输出物\n2. 列出你需要的输入（文件/链接/偏好）并向用户确认\n3. 给出执行步骤（可复用的 checklist）\n4. 产出结果并附验证方法\n\n## 边界\n\n- 默认不写入任何文件；若需要写入，必须先征得用户同意并做备份与校验。\n"""
    if write:
        skill_md.write_text(content, encoding="utf-8")
    return {"generated": True, "path": str(skill_dir)}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Create CoPaw agent workspace + register + skill assembly (dry-run by default)")
    ap.add_argument("--spec-md", default="", help="agent 规格文件（markdown，可选 YAML frontmatter）")
    ap.add_argument("--id", dest="agent_id", default="", help="agent id（不填则从 name 推导）")
    ap.add_argument("--name", default="", help="agent name")
    ap.add_argument("--description", default="", help="agent description")
    ap.add_argument("--keywords", default="", help="逗号分隔关键词（可选）")

    ap.add_argument("--copaw-dir", default=str(guess_copaw_root()), help="~/.copaw 路径")
    ap.add_argument("--registry", default="", help="注册表文件路径（可选；不填则自动探测 agents.json/config.json）")
    ap.add_argument("--dry-run", action="store_true", help="只输出计划，不写入（默认推荐）")
    ap.add_argument("--write", action="store_true", help="允许写入（必须先征得用户同意）")
    ap.add_argument("--force", action="store_true", help="若 workspace 已存在则备份后重建（需要 --write）")

    ap.add_argument("--max-local", type=int, default=5, help="本地技能池导入数量上限")
    ap.add_argument("--max-online", type=int, default=3, help="在线检索导入数量上限")
    ap.add_argument("--no-online", action="store_true", help="禁用在线检索（只用本地技能池）")
    ap.add_argument("--set-active", action="store_true", help="创建后将 active_agent 切到新 agent（默认不启用）")

    args = ap.parse_args(argv)
    write = bool(args.write)
    dry_run = bool(args.dry_run) or not write

    copaw_root = Path(args.copaw_dir).expanduser()
    skill_pool_dir = copaw_root / "skill_pool"
    workspaces_root = copaw_root / "workspaces"
    default_ws = workspaces_root / "default"

    # 解析 spec
    meta: Dict[str, Any] = {}
    body = ""
    if args.spec_md:
        meta, body = parse_frontmatter_file(Path(args.spec_md).expanduser())

    name = args.name or str(meta.get("name", "")).strip()
    description = args.description or str(meta.get("description", "")).strip()
    if not name:
        name = "New Agent"
    if not description:
        description = body.strip().splitlines()[0] if body.strip() else "new copaw agent"

    agent_id = args.agent_id or str(meta.get("id", "")).strip()
    if not agent_id:
        agent_id = slugify_agent_id(name)

    # keywords
    kws: List[str] = []
    if isinstance(meta.get("keywords"), list):
        kws = [str(x) for x in meta.get("keywords", [])]
    if args.keywords:
        kws.extend([x.strip() for x in args.keywords.split(",") if x.strip()])
    if not kws:
        # 从 name/description/body 抽取关键词
        kws = tokenize(" ".join([name, description, body]))[:12]

    registry_path = detect_registry(copaw_root, args.registry)
    new_ws = workspaces_root / agent_id
    new_ws_skills = new_ws / "skills"

    plan: Dict[str, Any] = {
        "mode": "dry-run" if dry_run else "write",
        "agent": {"id": agent_id, "name": name, "description": description},
        "paths": {
            "copaw_root": str(copaw_root),
            "registry": str(registry_path),
            "workspace": str(new_ws),
        },
        "keywords": kws,
        "steps": {},
    }

    # Step 1: local skill pool
    local_ranked = find_best_local_skills(skill_pool_dir, kws, top_n=args.max_local)
    imported: List[Dict[str, Any]] = []
    imported_names: List[str] = []
    local_import_candidates = [s for (s, sc) in local_ranked]
    plan["steps"]["local_skill_pool"] = [{"name": s.name, "score": sc, "path": str(s.path)} for (s, sc) in local_ranked]

    # Step 2: online
    online_slugs: List[str] = []
    if not args.no_online:
        # 用前 3 个关键词拼 query（降低噪声）
        q = " ".join(kws[:3]) if kws else name
        online_slugs = clawhub_search_slugs(q, limit=args.max_online)
    plan["steps"]["online_search"] = {"slugs": online_slugs, "disabled": bool(args.no_online)}

    # Step 3 decision will be after imports

    # dry-run 输出计划并退出
    if dry_run:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        print(
            "\n下一步：请把以上 dry-run 计划发给用户确认，并明确询问是否允许写入。"
            "\n用户允许后再用 --write 执行。"
        )
        return 0

    # write mode
    if not write:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        print("\n未提供 --write，拒绝写入。")
        return 2

    # workspace 处理
    if new_ws.exists():
        if not args.force:
            raise SystemExit(f"workspace 已存在：{new_ws}。如需覆盖请使用 --force（会先备份目录）")
        plan["steps"]["workspace_backup"] = safe_rename_dir_if_exists(new_ws, write=write)

    safe_mkdir(workspaces_root, write=write)
    # 优先复制 default workspace 模板
    plan["steps"]["workspace_template"] = ensure_workspace_template(default_ws, new_ws, write=write)

    # 修正 agent.json/skill.json
    agent_json_path = new_ws / "agent.json"
    skill_json_path = new_ws / "skill.json"
    plan["steps"]["patch_agent_json"] = patch_agent_json(agent_json_path, agent_id, name, description, new_ws, write=write)

    # skills dir
    safe_mkdir(new_ws_skills, write=write)

    # 导入本地技能池技能
    enabled_skill_entries: List[Dict[str, Any]] = []
    for s, sc in local_ranked:
        if not s.path or not s.path.exists():
            continue
        dst = new_ws_skills / s.name
        if dst.exists():
            continue
        copy_skill_dir(s.path, dst, write=write)
        enabled_skill_entries.append(
            {
                "name": s.name,
                "description": s.description,
                "source": s.source or "builtin",
                "metadata": {
                    "name": s.name,
                    "description": s.description,
                    "version_text": s.version_text,
                    "signature": s.signature,
                    "source": s.source or "builtin",
                },
                "requirements": {"require_bins": [], "require_envs": []},
                "updated_at": "",
            }
        )
        imported_names.append(s.name)

    # 在线导入（clawhub zip）
    online_imported = []
    tmp_root = copaw_root / ".tmp_agent_creator" / f"{agent_id}.{utc_ts()}"
    tmp_root.mkdir(parents=True, exist_ok=True)
    for slug in online_slugs:
        # 去重：同名/相似就跳过
        if any(is_similar_skill(slug, n) for n in imported_names):
            continue
        try:
            z = tmp_root / f"{slug}.zip"
            download_clawhub_zip(slug, z)
            ex = tmp_root / slug
            unzip_to_dir(z, ex)
            skill_dir = locate_skill_dir(ex)
            if not skill_dir:
                continue
            # 目录名用 slug
            dst = new_ws_skills / slug
            if dst.exists():
                continue
            copy_skill_dir(skill_dir, dst, write=write)
            enabled_skill_entries.append(
                {
                    "name": slug,
                    "description": f"imported from clawhub slug={slug}",
                    "source": "custom",
                    "metadata": {"name": slug, "description": f"imported from clawhub slug={slug}", "source": "custom"},
                    "requirements": {"require_bins": [], "require_envs": []},
                    "updated_at": "",
                }
            )
            imported_names.append(slug)
            online_imported.append(slug)
        except Exception:
            continue
    plan["steps"]["online_imported"] = online_imported

    # 若没有导入任何技能：生成最小技能
    generated_skill = None
    if not imported_names:
        gen_name = f"{agent_id}_core"
        gen_dir = new_ws_skills / gen_name
        generated_skill = generate_minimal_skill(gen_dir, gen_name, agent_goal=description, write=write)
        enabled_skill_entries.append(
            {
                "name": gen_name,
                "description": f"auto-generated core skill for {agent_id}",
                "source": "custom",
                "metadata": {"name": gen_name, "description": f"auto-generated core skill for {agent_id}", "source": "custom"},
                "requirements": {"require_bins": [], "require_envs": []},
                "updated_at": "",
            }
        )
        plan["steps"]["generated_skill"] = generated_skill

    plan["steps"]["patch_workspace_skill_json"] = patch_workspace_skill_json(skill_json_path, enabled_skill_entries, write=write)
    
    # Step 4: 使用 find-skills 技能查找更多合适技能
    additional_skills_result = search_and_import_additional_skills(agent_id, kws, new_ws, write=write)
    plan["steps"]["additional_skills_search"] = additional_skills_result
    
    # 如果通过 find-skills 找到了新技能，添加到 skill.json
    if write and additional_skills_result.get("imported_skills"):
        for skill_name in additional_skills_result["imported_skills"]:
            enabled_skill_entries.append({
                "name": skill_name,
                "description": f"imported via find-skills for {agent_id}",
                "source": "clawhub",
                "metadata": {"name": skill_name, "description": f"imported via find-skills", "source": "clawhub"},
                "requirements": {"require_bins": [], "require_envs": []},
                "updated_at": "",
            })
        # 重新更新 skill.json
        patch_workspace_skill_json(skill_json_path, enabled_skill_entries, write=write)
    
    # Step 5: 为智能体设置模型配置（已在 patch_agent_json 中完成）
    # 模型配置已添加到 agent.json 中

    # Step 5b: 确保 RULES.md 在全局 system_prompt_files 中激活
    rules_activation_result = ensure_rules_activated(registry_path, write=write)
    plan["steps"]["rules_activation"] = rules_activation_result

    # Step 6: 添加多智能体协作技能
    multi_agent_result = add_multi_agent_collaboration_skill(new_ws, skill_pool_dir, write=write)
    plan["steps"]["multi_agent_skill"] = multi_agent_result
    
    # 如果成功添加了多智能体协作技能，更新 skill.json
    if write and multi_agent_result.get("skill_added"):
        enabled_skill_entries.append({
            "name": "multi_agent_collaboration",
            "description": "Multi-agent collaboration skill for CoPaw",
            "source": "builtin",
            "metadata": {"name": "multi_agent_collaboration", "description": "Multi-agent collaboration skill", "source": "builtin"},
            "requirements": {"require_bins": [], "require_envs": []},
            "updated_at": "",
        })
        patch_workspace_skill_json(skill_json_path, enabled_skill_entries, write=write)
    
    # Step 7: 测试智能体通信（收尾操作）
    test_result = test_agent_communication(agent_id, write=write)
    plan["steps"]["communication_test"] = test_result
    
    plan["steps"]["update_registry"] = update_registry(registry_path, agent_id, new_ws, write=write, set_active=bool(args.set_active))

    print(json.dumps(plan, ensure_ascii=False, indent=2))
    print("\n完成：已创建 workspace、装配技能并更新注册表。你可运行 `copaw agents list` 验证。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

