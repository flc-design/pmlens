# PM Lens Cheatsheet

> **42 MCP tools** for Claude Code + Codex CLI project management.
> Version 0.12.0 | Python 3.11+ | PyPI: `pmlens`

---

## Quick Start

```bash
pip install pm-server
pmlens install          # Register MCP server in Claude Code
```

Claude Code session:
```
> PM初期化して              → pm_init
> 現在の進捗は？            → pm_status
> 次にやるべきことは？      → pm_next
```

---

## Tool Reference

### Setup & Project

| Tool | Description | Key Params |
|------|-------------|------------|
| `pm_init` | Initialize .pm/ directory, auto-detect project info | `project_path?`, `project_name?` |
| `pm_status` | Project status: phases, tasks, blockers, velocity | `project_path?` |
| `pm_list` | List all registered projects | _(none)_ |
| `pm_discover` | Scan for projects and register them | `scan_path="."` |
| `pm_cleanup` | Health-check registry, remove invalid entries | _(none)_ |
| `pm_update_rules` | Update CLAUDE.md and/or AGENTS.md rules (multi-host, ADR-008) | `project_path?, target?, dry_run?` |
| `pm_update_claudemd` | Legacy alias: `pm_update_rules(target="claude-code")` (deprecated since v0.6.0) | `project_path?` |
| `pm_dashboard` | Generate HTML/text dashboard | `format="html"` |

### Tasks

| Tool | Description | Key Params |
|------|-------------|------------|
| `pm_add_task` | Create a task (ID auto-generated) | `title`, `phase`, `priority="P1"` |
| `pm_update_task` | Update task status/fields | `task_id`, `status?`, `priority?`, `notes?` |
| `pm_tasks` | List/filter tasks | `status?`, `phase?`, `priority?`, `tag?`, `parent_id?` |
| `pm_next` | Recommend next actionable tasks | `count=3` |
| `pm_blockers` | List blocked tasks | `project_path?` |
| `pm_add_issue` | Add child issue to a task | `parent_id`, `title`, `priority="P1"` |

### Memory

| Tool | Description | Key Params |
|------|-------------|------------|
| `pm_remember` | Save a memory (auto-links to active task) | `content`, `type="observation"`, `tags?` |
| `pm_recall` | Recall memories / last session | `query?`, `task_id?`, `limit=5` |
| `pm_memory_search` | Advanced search with filters | `query`, `type?`, `tags?`, `cross_project?` |
| `pm_memory_stats` | Memory DB statistics | `project_path?` |
| `pm_memory_cleanup` | Delete old memories / prune session summaries | `older_than_days?`, `keep_latest?`, `summaries_keep_latest?`, `dry_run=True` |
| `pm_session_summary` | Save/get/list session summaries | `action="save"`, `summary?` |

### Recording

| Tool | Description | Key Params |
|------|-------------|------------|
| `pm_log` | Add daily log entry (auto-links to active task) | `entry`, `category="progress"` |
| `pm_add_decision` | Record an ADR (ID auto-generated) | `title`, `context`, `decision` |
| `pm_velocity` | Velocity and trend analysis | `weeks=4` |
| `pm_risks` | Auto-detected + manual risks | `project_path?` |

### Knowledge Records

| Tool | Description | Key Params |
|------|-------------|------------|
| `pm_record` | Record structured knowledge | `category`, `title`, `findings?`, `confidence="medium"` |
| `pm_knowledge` | Query/update knowledge records | `action="list"`, `category?`, `status?`, `tag?` |

### Workflow

| Tool | Description | Key Params |
|------|-------------|------------|
| `pm_workflow_start` | Start a workflow from template | `feature`, `template="development"` |
| `pm_workflow_status` | Get workflow progress and guidance | `workflow_id?` (auto-detect) |
| `pm_workflow_advance` | Advance/loop/skip a step | `proceed=True`, `artifacts?`, `skip?` |
| `pm_workflow_abandon` | Abandon a workflow (preserves history) | `workflow_id?`, `notes?` |
| `pm_workflow_list` | List workflow instances | `status?` |
| `pm_workflow_templates` | List available templates | `project_path?` |

---

## Usage in Claude Code

Users give natural language instructions, and Claude automatically calls the right tools.
Composite instructions trigger multiple tools in sequence.

### Project Basics

```
User:   Initialize PM for this project
Claude: → pm_init → pm_status → pm_next
        Creates .pm/ directory, adds rules to CLAUDE.md, shows current state

User:   What's the current progress?
Claude: → pm_status
        Shows phase progress, task counts, and blockers

User:   What should I work on next?
Claude: → pm_next
        Recommends 3 tasks based on priority and dependencies
```

### Task Management

```
User:   Create a task "Add authentication", phase-1, P0
Claude: → pm_add_task(title="Add authentication", phase="phase-1", priority="P0")
        PROJ-001 created

User:   Starting work on PROJ-001
Claude: → pm_update_task(task_id="PROJ-001", status="in_progress")
        Status changed to in_progress

User:   Auth feature done. Tests pass
Claude: → pm_update_task(task_id="PROJ-001", status="done")
        → pm_log(entry="Auth feature implemented, tests passing")
        → pm_next()
        Task completed, log recorded, next tasks recommended (composite execution)
```

### Issue Discovery

```
User:   JWT token expiry isn't handled — add an issue for that
Claude: → pm_add_issue(parent_id="PROJ-001", title="JWT token expiry not handled")
        PROJ-005 created, PROJ-001 automatically reverted to "review"

User:   Fixed PROJ-005
Claude: → pm_update_task(task_id="PROJ-005", status="done")
        All issues resolved → "Close PROJ-001 as done?" (auto-suggestion)
```

### 3-Layer Knowledge System

Three levels of knowledge capture, matched to formality:

```
User:   FastMCP v2 requires Python 3.11+. Note that down
Claude: → pm_remember(content="FastMCP v2 requires Python 3.11+", type="observation")
        [Layer 1: Memory] Quick casual note

User:   Record the JWT vs Session auth comparison results
Claude: → pm_record(category="tradeoff", title="JWT vs Session Auth",
            findings="JWT: stateless but larger payload...",
            conclusion="Use JWT for API, session for web",
            confidence="high")
        [Layer 2: Knowledge Record] Structured research findings

User:   We're going with JWT for the API. Record as ADR?
Claude: → pm_add_decision(title="Use JWT for API auth",
            context="Need stateless auth for microservices",
            decision="JWT with RS256, 15min expiry")
        [Layer 3: ADR] Formal architecture decision
```

### Knowledge Record Categories

| Category | Use Case | Example Prompt |
|----------|----------|----------------|
| `research` | General research findings | "Record the auth research results" |
| `market` | Market analysis, competitor research | "Record the competitor auth analysis" |
| `spike` | Technical spike / prototype results | "Record the FastMCP spike results" |
| `requirement` | Requirements definition | "Record the auth requirements" |
| `constraint` | Technical/business constraints | "Record the Python 3.11+ constraint" |
| `tradeoff` | Trade-off analysis (A vs B) | "Record the SQL vs NoSQL comparison" |
| `risk_analysis` | Risk assessment results | "Record the JWT vulnerability risks" |
| `spec` | Feature specification | "Record the auth API spec" |
| `api_design` | API design documentation | "Record the endpoint design" |

### Knowledge Record Updates

```
User:   KR-001 is verified now. Set it to validated, high confidence
Claude: → pm_knowledge(action="update", record_id="KR-001",
            new_status="validated", confidence="high")

User:   KR-001 is superseded by newer research
Claude: → pm_knowledge(action="update", record_id="KR-001",
            new_status="superseded")

User:   Show all research knowledge records
Claude: → pm_knowledge(action="list", category="research")
        Lists all records in the research category

User:   Give me a knowledge summary
Claude: → pm_knowledge(action="summary")
        Shows counts by category and status
```

---

## Workflow

### Built-in Templates

#### Discovery (5 steps, chains to Development)

```
research ──→ fact_check ──→ proposal ──→ cross_check ──→ confirm
   ↑              ↑             ↑
   └──── brainstorm loop ───────┘
         (proceed=false to loop)
```

- **research**: Investigate the topic (loop)
- **fact_check**: Verify findings (loop)
- **proposal**: Present to user (loop, gate: user_approval)
- **cross_check**: Independent validation
- **confirm**: Finalize direction, record ADR (gate: user_approval)

#### Development (9 steps)

```
decision → tasks → spec → plan → check → implement → test → quality → issues
                                   ↑                          ↑
                              gate: user_approval        gate: user_approval
```

- **decision**: Record ADR
- **tasks**: Break down into tasks
- **spec**: Write specification
- **plan**: Design implementation plan
- **check**: Cross-check (gate: user_approval)
- **implement**: Write code
- **test**: Write and run tests
- **quality**: Final review (gate: user_approval)
- **issues**: Register remaining issues (optional)

### Workflow Usage

```
User:   I want to start researching user authentication
Claude: → pm_workflow_start(feature="user authentication", template="discovery")
        WF-001 started. First step: "research" — shows guidance

User:   Research done. Move to next step
Claude: → pm_workflow_advance(artifacts=["KR-001"])
        research completed, next: fact_check

User:   I need to investigate more. Let's loop back
Claude: → pm_workflow_advance(proceed=False)
        Looped back to research (iteration 2)

User:   OK, enough research. Let's proceed through
Claude: → pm_workflow_advance() (repeated through steps)
        research → fact_check → proposal (asks for user approval at gate)

User:   Approved. Go ahead
Claude: → pm_workflow_advance()  # passes the gate
        → cross_check → confirm
        "Discovery complete. Start Development workflow?"

User:   Yes, start development
Claude: → pm_workflow_start(feature="user authentication", template="development")
        WF-002 started (Discovery → Development chain)

User:   What's the workflow progress?
Claude: → pm_workflow_status()
        Progress: 3/9, current step: plan, knowledge records: 5

User:   Skip this step
Claude: → pm_workflow_advance(skip=True)
        Step skipped, next step activated

User:   What workflow templates are available?
Claude: → pm_workflow_templates()
        brainstorming (8 steps, builtin), discovery (5 steps, builtin),
        development (9 steps, builtin), super-research (6 steps, builtin)
```

### Custom Templates

Place YAML files in `.pm/workflow_templates/` to override or add templates:

```yaml
# .pm/workflow_templates/my-workflow.yaml
name: My Custom Workflow
description: Custom workflow for my team
chain_to: development  # optional

steps:
  - id: research
    name: Research
    tool_hint: pm_record
    loop: true
    loop_group: investigate

  - id: review
    name: Review
    gate: user_approval

  - id: implement
    name: Implement
    skill_hint: Use plan mode
    optional: false
```

---

## Session Lifecycle

### Session Start (auto-executed by CLAUDE.md rules)

```
Claude: → pm_status()       Check current state
        → pm_next()         Show recommended tasks
        → pm_recall()       Restore last session context
        → Warn if blockers or overdue items exist
```

### During Work

```
User:   Working on PROJ-003
Claude: → pm_update_task(task_id="PROJ-003", status="in_progress")

User:   (implements code...)
Claude: Auto-saves important findings → pm_remember()
        Records knowledge when needed → pm_record()
        Advances workflow steps       → pm_workflow_advance()
```

### Session End

```
User:   Let's wrap up for today
Claude: → pm_update_task() to check in-progress task states
        → pm_log(entry="Session accomplishments summary")
        → pm_session_summary(action="save", summary="...")
        → Suggest committing uncommitted changes
```

### After Context Compaction

```
(Claude Code automatically compresses context)
Claude: → pm_recall()           Restore latest memories and session summary
        → pm_workflow_status()   Check current workflow position
        Continues work seamlessly
```

### Restart on source edit (editable installs only)

If you installed PM Lens with `pip install -e .` and edited the source
mid-session, restart your MCP host (Claude Code or Codex CLI) to reload
the package — Python caches modules in long-running processes and
**lazy-imported** modules (e.g. `pmlens.rules`) will hit the stale
cache when first imported, even if other modules are fresh on disk.

`pm_status()` now exposes a fingerprint to make this easy to spot:

```python
pm_status()["diagnostics"]["utils_fingerprint"]
# → {
#     "loaded":  "a1b2c3d4",     # what the running process loaded
#     "current": "a1b2c3d4",     # what is on disk right now
#     "stale":   False,           # True ⇒ restart the MCP host
#     "path":    "/.../utils.py",
#   }
```

If `stale: true`, the loaded code differs from the file on disk — restart
the server. Wheel installs (`pip install pm-server` from PyPI) are
unaffected because the source is immutable until the next `pip install -U`.
See PMSERV-060 for the originating incident.

---

## CLI Commands

```bash
pmlens install              # Register MCP server
pmlens uninstall            # Remove MCP server
pmlens serve                # Start MCP server (stdio)
pmlens status               # Show project status
pmlens discover [path]      # Find and register projects
pmlens update-rules         # Update CLAUDE.md / AGENTS.md rules (multi-host)
pmlens update-rules -t auto --dry-run  # Preview detected hosts
pmlens update-rules --all   # Apply to every registered project
pmlens update-claudemd      # Legacy: equivalent to update-rules -t claude-code
pm-server hook post-tool-use   # PostToolUse hook handler
```

---

## Data Storage

```
.pm/
├── project.yaml        # Project metadata
├── tasks.yaml          # All tasks
├── decisions.yaml      # ADRs
├── knowledge.yaml      # Knowledge records
├── workflows.yaml      # Workflow instances
├── risks.yaml          # Manual risks
├── milestones.yaml     # Milestones
├── memory.db           # SQLite + FTS5 memory
├── daily/              # Daily logs
│   └── 2026-04-16.yaml
└── workflow_templates/ # Custom templates (optional)
    └── my-workflow.yaml

~/.pm/
├── registry.yaml       # Global project registry
└── memory.db           # Cross-project memory index
```

---

## Enum Reference

| Type | Values |
|------|--------|
| TaskStatus | `todo`, `in_progress`, `review`, `done`, `blocked` |
| Priority | `P0` (critical), `P1` (important), `P2` (nice-to-have), `P3` (someday) |
| DecisionStatus | `proposed`, `accepted`, `deprecated`, `superseded` |
| LogCategory | `progress`, `decision`, `blocker`, `note`, `milestone` |
| MemoryType | `observation`, `insight`, `lesson` |
| KnowledgeCategory | `research`, `market`, `spike`, `requirement`, `constraint`, `tradeoff`, `risk_analysis`, `spec`, `api_design` |
| KnowledgeStatus | `draft`, `validated`, `superseded` |
| ConfidenceLevel | `high`, `medium`, `low` |
| WorkflowStepStatus | `pending`, `active`, `done`, `skipped` |
| WorkflowStatus | `active`, `completed`, `paused`, `abandoned` |
