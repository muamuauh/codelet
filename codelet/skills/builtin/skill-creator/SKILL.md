---
name: skill-creator
description: Design, author, validate, and hot-install new codelet Skills from a user's description
category: meta
triggers: [create skill, new skill, write skill, build skill, 创建skill, 新建技能]
allowed_tools: [read_file, write_file, edit_file, glob, grep, skill, install_skill]
---

# Skill Creator Procedure

Use this skill whenever the user asks you to create, design, or package a new Skill for `codelet`.

## 1. Clarify Intent & Placement

Infer as much as possible from the user's description; only ask follow-up questions if truly ambiguous:
- **Name**: short kebab-case identifier (e.g. `sql-migration-review`, `release-notes`).
- **Scope & Classification**:
  - `project` (default): `.codelet/skills/<name>.md` or `.codelet/skills/<name>/SKILL.md` — active in this workspace (`Standalone · Project`).
  - `global`: `~/.codelet/skills/<name>.md` or `~/.codelet/skills/<name>/SKILL.md` — available across all workspaces (`Standalone · Global`).
  - `plugin` (attached): add `plugin: <plugin_name>` in frontmatter, or place under `.codelet/plugins/<pack>/skills/<name>/SKILL.md` to create a hot-pluggable Skill Pack plugin.
- **Category** (optional UI tag): e.g. `review`, `testing`, `git`, `docs`, `ops`, `research`, `meta`.
- **Layout**:
  - Single markdown file `<name>.md` for straightforward checklists and workflows.
  - Directory bundle `<name>/SKILL.md` + `references/` or `scripts/` when the skill needs templates, schemas, or helper scripts.

## 2. Author High-Signal Frontmatter & Body

Follow the exact `codelet` skill format:

```markdown
---
name: <kebab-case-name>
description: <One crisp sentence (<=80 chars) stating WHEN and WHY to trigger this skill>
category: <optional-tag>
triggers: [<3-6 short keywords>]
allowed_tools: [<optional hint list, e.g. read_file, grep, bash>]
---

# <Clear Title>

## When to Use
- Concrete trigger scenarios and boundaries.

## Procedure
1. **Step 1** — specific tool calls, file patterns, or checks (never vague advice).
2. **Step 2** — concrete verification or transformation steps.

## Output Format
- Specify the exact structure expected back to the user.

## Guardrails / Stop Conditions
- What must NOT be done without confirmation (destructive edits, secrets, large scope).
```

Authoring standards:
- Keep the `description` crisp and trigger-oriented (it is the only line loaded into the cold system prompt index).
- Write concrete, imperative steps; avoid generic filler prose.
- Match the user's preferred language unless they ask for another language.

## 3. Write & Hot-Reload

- Create the skill file(s) with `write_file`.
- `codelet` automatically hot-reloads skills whenever files under `.codelet/skills/`, `~/.codelet/skills/`, or `.codelet/plugins/*/skills/` are written — no restart needed.

## 4. Verify & Report

1. Call `skill(name="<kebab-case-name>")` to verify the YAML frontmatter parsed cleanly and the body is live in the skill index.
2. Report to the user:
   - Exact file path written;
   - Scope & classification (`Standalone · Project` / `Standalone · Global` / `Plugin`);
   - How to trigger or toggle it in the sidebar.
