from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PlanTask:
    id: str
    summary: str
    files: list[str] = field(default_factory=list)
    done_when: str = ""


@dataclass(frozen=True, slots=True)
class DesignBranch:
    id: str
    summary: str
    pros: list[str] = field(default_factory=list)
    cons: list[str] = field(default_factory=list)
    recommended: bool = False


@dataclass(frozen=True, slots=True)
class RiskItem:
    risk: str
    mitigation: str


@dataclass(frozen=True, slots=True)
class TestMappingItem:
    criterion: str
    tests: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class PlanV2:
    goal: str
    acceptance_criteria: list[str]
    out_of_scope: list[str]
    constraints: list[str] = field(default_factory=list)
    candidate_files: list[str] = field(default_factory=list)
    tasks: list[PlanTask] = field(default_factory=list)
    design_branches: list[DesignBranch] = field(default_factory=list)
    risks: list[RiskItem] = field(default_factory=list)
    test_mapping: list[TestMappingItem] = field(default_factory=list)
    verification_profile: str = ""
    planner_confidence: float = 1.0


@dataclass(frozen=True, slots=True)
class RepoExplorerV1:
    candidate_files: list[str] = field(default_factory=list)
    similar_files: list[str] = field(default_factory=list)
    architectural_notes: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RiskTestPlanV1:
    risks: list[RiskItem] = field(default_factory=list)
    test_mapping: list[TestMappingItem] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ConstraintReportV1:
    out_of_scope: list[str] = field(default_factory=list)
    protected_paths: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ReviewFinding:
    id: str
    severity: str
    origin: str
    confidence: float
    file: str
    line_start: int
    line_end: int
    claim: str
    evidence: list[str] = field(default_factory=list)
    verifier_status: str = "unverified"
    suggested_fix: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewFindingsV1:
    findings: list[ReviewFinding] = field(default_factory=list)
