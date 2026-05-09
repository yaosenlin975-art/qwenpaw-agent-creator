# CoPaw Skill: Agent Creator (with Skill Auto-Assembly)

English | [中文](README.md)

This repository provides a custom **CoPaw (AgentScope)** skill: **copaw_agent_creator**.  
It creates a new CoPaw agent (workspace), registers it so it becomes visible in the CoPaw Console UI, and follows the required “skill auto-assembly” workflow:

1. Search the local skill pool (`~/.copaw/skill_pool`) and import matching skills into the new agent  
2. Search online skill marketplaces (prefer `npx clawhub search`) and import matching skills (dedupe vs step 1 / similar ones)  
3. If nothing is imported from 1+2, generate a minimal new skill for the agent (Skill-Creator style)

## IMPORTANT: Write-safety protocol

This skill creates directories and updates JSON configs, so it is **dry-run by default**.  
It will only write when you explicitly allow it AND you run with `--write`.

For every write, the agent must:

1) Ask for your permission every time  
2) Before overwriting any file:
- validate format (JSON must be parseable)
- create a same-directory backup: `<file>.bak.<timestamp>`
- prefer temp-file + atomic replace

The script enforces a hard guard: without `--write`, it does not write anything.

## Quick start

Dry-run:

```bash
python scripts/create_agent.py --spec-md ./agent_spec.md --dry-run
```

Write (only after explicit approval):

```bash
python scripts/create_agent.py --spec-md ./agent_spec.md --write
```

## Contents

- `SKILL.md`: Skill handbook for CoPaw agents (workflow, boundaries, permission prompts)
- `scripts/create_agent.py`: The main implementation (workspace + registry + skill assembly)
- `template/`: Built-in agent template (includes AGENTS.md, SOUL.md, RULES.md and more)

## Template Mechanism

When creating a new workspace, template priority:
1. `default` workspace (user-defined template)
2. `template/` directory (built-in template, new in v0.2.2)
3. Hardcoded minimal file set

**Built-in template features:**
- Complete set: AGENTS.md / SOUL.md / PROFILE.md / MEMORY.md / RULES.md / HEARTBEAT.md / BOOTSTRAP.md
- `RULES.md` (Agent dead rules) is auto-registered to global `system_prompt_files` → active immediately after creation
- No need to maintain a default workspace to get a standardized template

## Docker Environment Adaptation

When running CoPaw inside a Docker container, paths differ from the host:

| Item | Host Path | Docker Container Path |
|------|-----------|----------------------|
| Workspaces root | `~/.copaw/workspaces/` | `/app/working/workspaces/` |
| Skill pool | `~/.copaw/skill_pool/` | `/app/working/skill_pool/` |
| Config file | `~/.copaw/config.json` | `/app/working/config.json` |

Permissions: Running as root inside the container, no extra permissions needed.  
Network: Container has external network access for `npx clawhub` online search.  
Env vars: `HOME` should point to `/root`, `PATH` should include `/app/venv/bin`.

## License

Apache-2.0. See [LICENSE](LICENSE).

