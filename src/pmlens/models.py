"""Pydantic v2 data models for PM Lens."""

import datetime as _dt
from enum import StrEnum

from pydantic import BaseModel, Field

# Alias to avoid field-name collisions (e.g. Decision.date vs date type)
_Date = _dt.date


# ─── Enums ───────────────────────────────────────────


class ProjectStatus(StrEnum):
    """Project lifecycle status."""

    DESIGN = "design"
    DEVELOPMENT = "development"
    TESTING = "testing"
    MAINTENANCE = "maintenance"
    ARCHIVED = "archived"


class TaskStatus(StrEnum):
    """Task workflow status."""

    TODO = "todo"
    IN_PROGRESS = "in_progress"
    REVIEW = "review"
    DONE = "done"
    BLOCKED = "blocked"


class Priority(StrEnum):
    """Task priority level."""

    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class SuggestedModel(StrEnum):
    """Recommended model for a task's implementation session (PMSERV-155 / ADR-041).

    ``any`` (the default) means "no preference" — it behaves like an unset
    field so existing tasks stay backward compatible and the prompt-pack diagram
    simply omits a model chip for them.
    """

    OPUS = "opus"
    SONNET = "sonnet"
    HAIKU = "haiku"
    ANY = "any"


class PhaseStatus(StrEnum):
    """Phase lifecycle status."""

    PLANNED = "planned"
    ACTIVE = "active"
    COMPLETED = "completed"


class RiskSeverity(StrEnum):
    """Risk severity level."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class RiskStatus(StrEnum):
    """Risk tracking status."""

    OPEN = "open"
    MITIGATED = "mitigated"
    CLOSED = "closed"


class DecisionStatus(StrEnum):
    """ADR decision status."""

    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    DEPRECATED = "deprecated"
    SUPERSEDED = "superseded"


class LogCategory(StrEnum):
    """Daily log entry category."""

    PROGRESS = "progress"
    DECISION = "decision"
    BLOCKER = "blocker"
    NOTE = "note"
    MILESTONE = "milestone"


class IssueSeverity(StrEnum):
    """Classifies a child issue's nature — gates auto-revert behavior.

    defect      = flaw found during review; parent reopens to 'review'
    enhancement = future improvement idea; parent stays 'done'
    """

    DEFECT = "defect"
    ENHANCEMENT = "enhancement"


class MemoryType(StrEnum):
    """Memory observation type."""

    OBSERVATION = "observation"
    INSIGHT = "insight"
    LESSON = "lesson"


class KnowledgeCategory(StrEnum):
    """Knowledge record category."""

    RESEARCH = "research"
    MARKET = "market"
    SPIKE = "spike"
    REQUIREMENT = "requirement"
    CONSTRAINT = "constraint"
    TRADEOFF = "tradeoff"
    RISK_ANALYSIS = "risk_analysis"
    SPEC = "spec"
    API_DESIGN = "api_design"


class KnowledgeStatus(StrEnum):
    """Knowledge record lifecycle status."""

    DRAFT = "draft"
    VALIDATED = "validated"
    SUPERSEDED = "superseded"


class ConfidenceLevel(StrEnum):
    """Confidence level for knowledge records."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class WorkflowStepStatus(StrEnum):
    """Workflow step lifecycle status."""

    PENDING = "pending"
    ACTIVE = "active"
    DONE = "done"
    SKIPPED = "skipped"


class WorkflowStatus(StrEnum):
    """Workflow instance lifecycle status."""

    ACTIVE = "active"
    COMPLETED = "completed"
    PAUSED = "paused"
    ABANDONED = "abandoned"


# ─── Exceptions ──────────────────────────────────────


class PmServerError(Exception):
    """Base exception for PM Lens."""


class ProjectNotFoundError(PmServerError):
    """No .pm/ directory found."""


class TaskNotFoundError(PmServerError):
    """Task ID does not exist."""


class DecisionNotFoundError(PmServerError):
    """Decision ID does not exist."""


class WorkflowNotFoundError(PmServerError):
    """Workflow ID does not exist."""


class KnowledgeNotFoundError(PmServerError):
    """Knowledge record ID does not exist."""


# ─── Data Models ─────────────────────────────────────


class Phase(BaseModel):
    """Project phase definition."""

    id: str
    name: str
    status: PhaseStatus = PhaseStatus.PLANNED
    target_date: _Date | None = None


class ProjectHealth(BaseModel):
    """Computed project health metrics."""

    velocity: float | None = None
    blockers: int = 0
    overdue: int = 0


class Task(BaseModel):
    """A single task within a project."""

    id: str
    title: str
    phase: str
    status: TaskStatus = TaskStatus.TODO
    priority: Priority = Priority.P1
    assignee: str = "claude-code"
    estimate_hours: float | None = None
    actual_hours: float | None = None
    depends_on: list[str] = Field(default_factory=list)
    blocked_by: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    created: _Date = Field(default_factory=_dt.date.today)
    updated: _Date = Field(default_factory=_dt.date.today)
    description: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    notes: str = ""
    parent_id: str | None = None
    severity: IssueSeverity | None = None
    # Prompt Pack v2 (PMSERV-155 / ADR-041). All optional & defaulted so
    # existing tasks load unchanged; a prompt pack reads these when present.
    suggested_model: SuggestedModel = SuggestedModel.ANY
    after_recommended: list[str] = Field(default_factory=list)  # soft deps (≠ blocked_by)
    track: str = ""  # lane key for prompt-pack group_by="track"


class Consequences(BaseModel):
    """ADR consequences structure."""

    positive: list[str] = Field(default_factory=list)
    negative: list[str] = Field(default_factory=list)
    mitigations: list[str] = Field(default_factory=list)


class Decision(BaseModel):
    """Architecture Decision Record (ADR)."""

    id: str
    title: str
    date: _Date = Field(default_factory=_dt.date.today)
    status: DecisionStatus = DecisionStatus.ACCEPTED
    context: str = ""
    decision: str = ""
    consequences: Consequences = Field(default_factory=Consequences)


class Milestone(BaseModel):
    """Project milestone."""

    id: str
    name: str
    target_date: _Date | None = None
    status: PhaseStatus = PhaseStatus.PLANNED
    deliverables: list[str] = Field(default_factory=list)


class Risk(BaseModel):
    """Risk or issue tracking entry."""

    id: str
    title: str
    severity: RiskSeverity = RiskSeverity.MEDIUM
    status: RiskStatus = RiskStatus.OPEN
    description: str = ""
    mitigation: str = ""
    related_tasks: list[str] = Field(default_factory=list)
    created: _Date = Field(default_factory=_dt.date.today)


class DailyLogEntry(BaseModel):
    """Single entry in a daily log."""

    time: str  # HH:MM format
    category: LogCategory = LogCategory.PROGRESS
    entry: str = ""


class DailyLog(BaseModel):
    """A day's log entries."""

    date: _Date
    entries: list[DailyLogEntry] = Field(default_factory=list)


class Project(BaseModel):
    """Root model for project.yaml."""

    name: str
    display_name: str = ""
    version: str = "0.1.0"
    status: ProjectStatus = ProjectStatus.DEVELOPMENT
    started: _Date = Field(default_factory=_dt.date.today)
    owner: str = ""
    repository: str | None = None
    description: str = ""
    phases: list[Phase] = Field(default_factory=list)
    health: ProjectHealth = Field(default_factory=ProjectHealth)
    pm_schema: int = 1
    # Prompt Pack v2 (PMSERV-155 / ADR-041): project-wide discipline text and
    # verification commands injected into generated session prompts. Optional &
    # defaulted (v1 read verify_commands from raw YAML; now formalized as fields).
    discipline: str = ""
    verify_commands: list[str] = Field(default_factory=list)


class RegistryEntry(BaseModel):
    """Single project entry in the global registry."""

    path: str
    name: str
    registered: _Date = Field(default_factory=_dt.date.today)


class Registry(BaseModel):
    """Root model for ~/.pm/registry.yaml."""

    projects: list[RegistryEntry] = Field(default_factory=list)


# ─── Knowledge Records ─────────────────────────────


class KnowledgeRecord(BaseModel):
    """A structured knowledge record.

    Sits between casual Memory (observation/insight/lesson) and formal ADR.
    Used for research findings, requirements, trade-off analyses, specs, etc.
    Stored in .pm/knowledge.yaml.
    """

    id: str
    category: KnowledgeCategory
    title: str
    status: KnowledgeStatus = KnowledgeStatus.DRAFT
    confidence: ConfidenceLevel = ConfidenceLevel.MEDIUM
    findings: str = ""
    conclusion: str = ""
    sources: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    task_id: str | None = None
    workflow_id: str | None = None
    created: _Date = Field(default_factory=_dt.date.today)
    updated: _Date = Field(default_factory=_dt.date.today)


# ─── Memory Layer Models ──────────────────────────


class Memory(BaseModel):
    """A single memory entry stored in SQLite."""

    id: int | None = None
    session_id: str
    type: MemoryType = MemoryType.OBSERVATION
    content: str
    task_id: str | None = None
    decision_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    created_at: str = ""
    project: str = ""


class SessionSummary(BaseModel):
    """Session summary for cross-session continuity.

    `created_at` is the original creation timestamp (preserved across re-saves).
    `updated_at` is the latest save timestamp (refreshed by save_session_summary
    when the same session_id is saved again). Use updated_at for ambiguity
    detection windows so still-active sessions are captured even when their
    initial summary was created long ago.

    `branch` is the git branch the summary was recorded on (detected from
    `.git/HEAD` as text on the save path; "" when not in a git repo, on a
    detached HEAD, or in a worktree whose `.git` is a file). It powers
    branch-aware recall via `pm_recall(track=...)` (PMSERV-124 / ADR-028).
    """

    id: int | None = None
    session_id: str
    summary: str
    goals: str = ""
    tasks_done: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    pending: list[str] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    project: str = ""
    branch: str = ""


# ─── Workflow Models ────────────────────────────────


class WorkflowStep(BaseModel):
    """A single step in a workflow.

    Used both in templates (definition) and instances (runtime state).
    Template fields define the step's behavior and hints.
    Runtime fields (status, artifacts, iteration, notes) track execution state.
    """

    id: str
    name: str
    description: str = ""
    status: WorkflowStepStatus = WorkflowStepStatus.PENDING

    # Step behavior
    loop: bool = False
    loop_group: str | None = None
    gate: str | None = None  # e.g. "user_approval"
    optional: bool = False

    # Hints for Claude — which tool/skill/agent to use
    tool_hint: str | None = None
    skill_hint: str | None = None
    agent_hint: str | None = None

    # Knowledge integration (Phase 6)
    required_artifacts: list[str] = Field(default_factory=list)
    produces: list[str] = Field(default_factory=list)
    consumes: list[str] = Field(default_factory=list)

    # Runtime state
    artifacts: list[str] = Field(default_factory=list)
    iteration: int = 0
    notes: str = ""


class WorkflowTemplate(BaseModel):
    """Blueprint for creating workflow instances.

    Templates define the steps and their configuration.
    Stored as YAML in templates/workflows/ (built-in) or .pm/workflow_templates/ (custom).
    """

    name: str
    description: str = ""
    chain_to: str | None = None
    steps: list[WorkflowStep] = Field(default_factory=list)


class Workflow(BaseModel):
    """A workflow instance created from a template.

    Tracks the execution state of a feature development workflow.
    Each step progresses through pending → active → done/skipped.
    Supports loops (brainstorming), gates (user approval), and chaining.
    """

    id: str
    name: str
    feature: str
    template: str
    steps: list[WorkflowStep] = Field(default_factory=list)
    current_step_index: int = 0
    status: WorkflowStatus = WorkflowStatus.ACTIVE
    chain_to: str | None = None
    created: _Date = Field(default_factory=_dt.date.today)
    updated: _Date = Field(default_factory=_dt.date.today)
