from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class WorkflowValidationError(ValueError):
    pass


ARCHITECTURE_REQUIRED_ARTIFACTS: tuple[str, ...] = (
    "verification_plan.json",
    "runner_metadata.json",
)


def _require_mapping(value: Any, *, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WorkflowValidationError(f"{field_name} must be a mapping")
    return value


def _require_list_of_strings(value: Any, *, field_name: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise WorkflowValidationError(f"{field_name} must be a list of strings")
    return list(value)


def _require_list_of_mappings(value: Any, *, field_name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise WorkflowValidationError(f"{field_name} must be a list of mappings")
    return [dict(item) for item in value]


@dataclass(frozen=True, slots=True)
class CommitteeRoleConfig:
    mode: str
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    output_schema: str = ""

    @classmethod
    def from_dict(cls, payload: Any, *, field_name: str) -> CommitteeRoleConfig:
        data = _require_mapping(payload, field_name=field_name)
        mode = str(data.get("mode", "")).strip()
        output_schema = str(data.get("output_schema", "")).strip()
        if not mode or not output_schema:
            raise WorkflowValidationError(f"{field_name} requires mode and output_schema")
        return cls(
            mode=mode,
            allowed_tools=_require_list_of_strings(
                data.get("allowed_tools", []), field_name=f"{field_name}.allowed_tools"
            ),
            disallowed_tools=_require_list_of_strings(
                data.get("disallowed_tools", []), field_name=f"{field_name}.disallowed_tools"
            ),
            output_schema=output_schema,
        )


@dataclass(frozen=True, slots=True)
class PlanningCommitteeConfig:
    roles: dict[str, CommitteeRoleConfig] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: Any) -> PlanningCommitteeConfig:
        data = _require_mapping(payload, field_name="planning.committee")
        raw_roles = _require_mapping(data.get("roles", {}), field_name="planning.committee.roles")
        roles = {
            name: CommitteeRoleConfig.from_dict(role_payload, field_name=f"planning.committee.roles.{name}")
            for name, role_payload in raw_roles.items()
        }
        if not roles:
            raise WorkflowValidationError("planning.committee.roles must not be empty")
        return cls(roles=roles)


@dataclass(frozen=True, slots=True)
class PlanningGates:
    require_out_of_scope: bool = True
    require_candidate_files: bool = True
    require_test_mapping: bool = True
    require_verification_profile: bool = True
    require_design_branches: bool = True

    @classmethod
    def from_dict(cls, payload: Any) -> PlanningGates:
        data = _require_mapping(payload, field_name="planning.gates")
        return cls(
            require_out_of_scope=bool(data.get("require_out_of_scope", True)),
            require_candidate_files=bool(data.get("require_candidate_files", True)),
            require_test_mapping=bool(data.get("require_test_mapping", True)),
            require_verification_profile=bool(data.get("require_verification_profile", True)),
            require_design_branches=bool(data.get("require_design_branches", True)),
        )


@dataclass(frozen=True, slots=True)
class PlanningConfig:
    provider: str
    enabled: bool = True
    mode: str = "committee"
    cwd_source: str = "plan_workspace"
    max_turns: int = 4
    timeout_seconds: int = 300
    settings_sources: list[str] = field(default_factory=lambda: ["project"])
    allowed_tools: list[str] = field(default_factory=list)
    skill_mode: str = ""
    committee: PlanningCommitteeConfig | None = None
    gates: PlanningGates = field(default_factory=PlanningGates)

    @classmethod
    def from_dict(cls, payload: Any) -> PlanningConfig:
        data = _require_mapping(payload, field_name="planning")
        provider = str(data.get("provider", "")).strip()
        if not provider:
            raise WorkflowValidationError("planning.provider is required")
        committee = None
        if "committee" in data:
            committee = PlanningCommitteeConfig.from_dict(data["committee"])
        gates = PlanningGates.from_dict(data.get("gates", {})) if "gates" in data else PlanningGates()
        return cls(
            provider=provider,
            enabled=bool(data.get("enabled", True)),
            mode=str(data.get("mode", "committee")).strip() or "committee",
            cwd_source=str(data.get("cwd_source", "plan_workspace")).strip() or "plan_workspace",
            max_turns=int(data.get("max_turns", 4)),
            timeout_seconds=int(data.get("timeout_seconds", 300)),
            settings_sources=_require_list_of_strings(
                data.get("settings_sources", ["project"]),
                field_name="planning.settings_sources",
            ),
            allowed_tools=_require_list_of_strings(
                data.get("allowed_tools", []),
                field_name="planning.allowed_tools",
            ),
            skill_mode=str(data.get("skill_mode", "")).strip(),
            committee=committee,
            gates=gates,
        )


@dataclass(frozen=True, slots=True)
class CandidateModeTriggers:
    rework_count_gte: int = 1
    planner_confidence_lt: float = 0.75
    require_clear_design_branches: bool = True

    @classmethod
    def from_dict(cls, payload: Any) -> CandidateModeTriggers:
        data = _require_mapping(payload, field_name="implementation.candidate_mode.triggers")
        return cls(
            rework_count_gte=int(data.get("rework_count_gte", 1)),
            planner_confidence_lt=float(data.get("planner_confidence_lt", 0.75)),
            require_clear_design_branches=bool(data.get("require_clear_design_branches", True)),
        )


@dataclass(frozen=True, slots=True)
class CandidateModeConfig:
    enabled: bool = True
    max_parallel_editors: int = 2
    triggers: CandidateModeTriggers = field(default_factory=CandidateModeTriggers)

    @classmethod
    def from_dict(cls, payload: Any) -> CandidateModeConfig:
        data = _require_mapping(payload, field_name="implementation.candidate_mode")
        max_parallel_editors = int(data.get("max_parallel_editors", 2))
        if max_parallel_editors > 2:
            raise WorkflowValidationError("phase1 candidate editors must be <= 2")
        triggers = (
            CandidateModeTriggers.from_dict(data.get("triggers", {})) if "triggers" in data else CandidateModeTriggers()
        )
        return cls(
            enabled=bool(data.get("enabled", True)),
            max_parallel_editors=max_parallel_editors,
            triggers=triggers,
        )


@dataclass(frozen=True, slots=True)
class ImplementationConfig:
    backend: str
    optional_backends: list[str] = field(default_factory=list)
    single_writer_default: bool = True
    candidate_mode: CandidateModeConfig = field(default_factory=CandidateModeConfig)
    push_only_winner: bool = True
    cleanup_loser_local_branches: bool = True

    @classmethod
    def from_dict(cls, payload: Any) -> ImplementationConfig:
        data = _require_mapping(payload, field_name="implementation")
        backend = str(data.get("backend", "")).strip()
        if not backend:
            raise WorkflowValidationError("implementation.backend is required")
        candidate_mode = (
            CandidateModeConfig.from_dict(data["candidate_mode"]) if "candidate_mode" in data else CandidateModeConfig()
        )
        push_policy = _require_mapping(data.get("push_policy", {}), field_name="implementation.push_policy")
        return cls(
            backend=backend,
            optional_backends=_require_list_of_strings(
                data.get("optional_backends", []),
                field_name="implementation.optional_backends",
            ),
            single_writer_default=bool(data.get("single_writer_default", True)),
            candidate_mode=candidate_mode,
            push_only_winner=bool(push_policy.get("push_only_winner", True)),
            cleanup_loser_local_branches=bool(push_policy.get("cleanup_loser_local_branches", True)),
        )


@dataclass(frozen=True, slots=True)
class ReviewThresholds:
    min_confidence_to_report: float = 0.80
    verifier_required: bool = True

    @classmethod
    def from_dict(cls, payload: Any) -> ReviewThresholds:
        data = _require_mapping(payload, field_name="review.thresholds")
        return cls(
            min_confidence_to_report=float(data.get("min_confidence_to_report", 0.80)),
            verifier_required=bool(data.get("verifier_required", True)),
        )


@dataclass(frozen=True, slots=True)
class ReviewConfig:
    enabled: bool = True
    provider: str = ""
    rules_file: str = "REVIEW.md"
    post_inline_to_github: bool = True
    roles: dict[str, dict[str, Any]] = field(default_factory=dict)
    thresholds: ReviewThresholds = field(default_factory=ReviewThresholds)

    @classmethod
    def from_dict(cls, payload: Any) -> ReviewConfig:
        data = _require_mapping(payload, field_name="review")
        raw_roles = data.get("roles", {})
        if not isinstance(raw_roles, dict):
            raise WorkflowValidationError("review.roles must be a mapping")
        roles: dict[str, dict[str, Any]] = {}
        for name, role_payload in raw_roles.items():
            role_data = _require_mapping(role_payload, field_name=f"review.roles.{name}")
            roles[name] = role_data
        return cls(
            enabled=bool(data.get("enabled", True)),
            provider=str(data.get("provider", "")).strip(),
            rules_file=str(data.get("rules_file", "REVIEW.md")).strip() or "REVIEW.md",
            post_inline_to_github=bool(data.get("post_inline_to_github", True)),
            roles=roles,
            thresholds=ReviewThresholds.from_dict(data.get("thresholds", {}))
            if "thresholds" in data
            else ReviewThresholds(),
        )


@dataclass(frozen=True, slots=True)
class VerificationCheck:
    name: str
    command: str
    category: str = ""

    @classmethod
    def from_dict(cls, payload: Any, *, field_name: str) -> VerificationCheck:
        data = _require_mapping(payload, field_name=field_name)
        name = str(data.get("name", "")).strip()
        command = str(data.get("command", "")).strip()
        if not name or not command:
            raise WorkflowValidationError(f"{field_name} requires name and command")
        return cls(
            name=name,
            command=command,
            category=str(data.get("category", "")).strip(),
        )


@dataclass(frozen=True, slots=True)
class VerificationConfig:
    required_artifacts: list[str] = field(default_factory=list)
    required_checks: list[VerificationCheck] = field(default_factory=list)
    advisory_checks: list[VerificationCheck] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: Any) -> VerificationConfig:
        data = _require_mapping(payload, field_name="verification")
        required_artifacts = _require_list_of_strings(
            data.get("required_artifacts", []),
            field_name="verification.required_artifacts",
        )
        missing_core_artifacts = [
            artifact for artifact in ARCHITECTURE_REQUIRED_ARTIFACTS if artifact not in required_artifacts
        ]
        if missing_core_artifacts:
            raise WorkflowValidationError(
                "verification.required_artifacts is missing architecture-required artifacts: "
                + ", ".join(missing_core_artifacts)
            )
        return cls(
            required_artifacts=required_artifacts,
            required_checks=[
                VerificationCheck.from_dict(item, field_name=f"verification.required_checks[{index}]")
                for index, item in enumerate(
                    _require_list_of_mappings(
                        data.get("required_checks", []),
                        field_name="verification.required_checks",
                    )
                )
            ],
            advisory_checks=[
                VerificationCheck.from_dict(item, field_name=f"verification.advisory_checks[{index}]")
                for index, item in enumerate(
                    _require_list_of_mappings(
                        data.get("advisory_checks", []),
                        field_name="verification.advisory_checks",
                    )
                )
            ],
        )


@dataclass(frozen=True, slots=True)
class EvalsConfig:
    enabled: bool = True
    strategy: str = ""
    fixtures_root: str = ""
    graders: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: Any) -> EvalsConfig:
        data = _require_mapping(payload, field_name="evals")
        raw_graders = data.get("graders", {})
        if not isinstance(raw_graders, dict):
            raise WorkflowValidationError("evals.graders must be a mapping")
        graders = {str(name): str(value) for name, value in raw_graders.items()}
        return cls(
            enabled=bool(data.get("enabled", True)),
            strategy=str(data.get("strategy", "")).strip(),
            fixtures_root=str(data.get("fixtures_root", "")).strip(),
            graders=graders,
        )


@dataclass(frozen=True, slots=True)
class TelemetryConfig:
    sink: str = "jsonl"
    otel_compatible_fields: bool = True

    @classmethod
    def from_dict(cls, payload: Any) -> TelemetryConfig:
        data = _require_mapping(payload, field_name="telemetry")
        sink = str(data.get("sink", "jsonl")).strip() or "jsonl"
        return cls(
            sink=sink,
            otel_compatible_fields=bool(data.get("otel_compatible_fields", True)),
        )


@dataclass(frozen=True, slots=True)
class IncidentBundleConfig:
    enabled: bool = True
    mount_readonly: bool = True
    freeze_on_human_review: bool = True
    cleanup_on_terminal: bool = True
    keep_provenance_after_cleanup: bool = True

    @classmethod
    def from_dict(cls, payload: Any) -> IncidentBundleConfig:
        data = _require_mapping(payload, field_name="debug.incident_bundle")
        return cls(
            enabled=bool(data.get("enabled", True)),
            mount_readonly=bool(data.get("mount_readonly", True)),
            freeze_on_human_review=bool(data.get("freeze_on_human_review", True)),
            cleanup_on_terminal=bool(data.get("cleanup_on_terminal", True)),
            keep_provenance_after_cleanup=bool(data.get("keep_provenance_after_cleanup", True)),
        )


@dataclass(frozen=True, slots=True)
class WorkflowConfig:
    planning: PlanningConfig | None = None
    implementation: ImplementationConfig | None = None
    review: ReviewConfig | None = None
    verification: VerificationConfig | None = None
    incident_bundle: IncidentBundleConfig | None = None
    evals: EvalsConfig | None = None
    telemetry: TelemetryConfig | None = None

    @classmethod
    def from_dict(cls, payload: Any) -> WorkflowConfig:
        data = _require_mapping(payload, field_name="workflow")
        planning = PlanningConfig.from_dict(data["planning"]) if "planning" in data else None
        implementation = ImplementationConfig.from_dict(data["implementation"]) if "implementation" in data else None
        review = ReviewConfig.from_dict(data["review"]) if "review" in data else None
        verification = VerificationConfig.from_dict(data["verification"]) if "verification" in data else None
        incident_bundle = None
        if isinstance(data.get("debug"), dict) and "incident_bundle" in data["debug"]:
            incident_bundle = IncidentBundleConfig.from_dict(data["debug"]["incident_bundle"])
        evals = EvalsConfig.from_dict(data["evals"]) if "evals" in data else None
        telemetry = TelemetryConfig.from_dict(data["telemetry"]) if "telemetry" in data else None
        return cls(
            planning=planning,
            implementation=implementation,
            review=review,
            verification=verification,
            incident_bundle=incident_bundle,
            evals=evals,
            telemetry=telemetry,
        )
