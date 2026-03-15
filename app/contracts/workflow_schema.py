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
class PlanningLegacyFallbackConfig:
    enabled: bool = True
    use_only_on_committee_failure: bool = True

    @classmethod
    def from_dict(cls, payload: Any) -> PlanningLegacyFallbackConfig:
        data = _require_mapping(payload, field_name="planning.legacy_fallback")
        return cls(
            enabled=bool(data.get("enabled", True)),
            use_only_on_committee_failure=bool(data.get("use_only_on_committee_failure", True)),
        )


@dataclass(frozen=True, slots=True)
class PlanningAutoselectCommitteeConfig:
    enabled: bool = True
    min_acceptance_criteria: int = 12
    min_acceptance_criteria_when_complex: int = 8
    min_summary_chars_when_complex: int = 2800
    min_repo_files: int = 120
    min_acceptance_criteria_with_large_repo: int = 6

    @classmethod
    def from_dict(cls, payload: Any) -> PlanningAutoselectCommitteeConfig:
        data = _require_mapping(payload, field_name="planning.autoselect_committee")
        return cls(
            enabled=bool(data.get("enabled", True)),
            min_acceptance_criteria=int(data.get("min_acceptance_criteria", 12)),
            min_acceptance_criteria_when_complex=int(data.get("min_acceptance_criteria_when_complex", 8)),
            min_summary_chars_when_complex=int(data.get("min_summary_chars_when_complex", 2800)),
            min_repo_files=int(data.get("min_repo_files", 120)),
            min_acceptance_criteria_with_large_repo=int(data.get("min_acceptance_criteria_with_large_repo", 6)),
        )


@dataclass(frozen=True, slots=True)
class PlanningConfig:
    provider: str
    enabled: bool = True
    mode: str = "auto"
    test_plan_max_parallelism: int = 3
    cwd_source: str = "plan_workspace"
    max_turns: int = 4
    timeout_seconds: int = 300
    settings_sources: list[str] = field(default_factory=lambda: ["project"])
    allowed_tools: list[str] = field(default_factory=list)
    skill_mode: str = ""
    legacy_fallback: PlanningLegacyFallbackConfig = field(default_factory=PlanningLegacyFallbackConfig)
    autoselect_committee: PlanningAutoselectCommitteeConfig = field(default_factory=PlanningAutoselectCommitteeConfig)
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
        legacy_fallback = (
            PlanningLegacyFallbackConfig.from_dict(data["legacy_fallback"])
            if "legacy_fallback" in data
            else PlanningLegacyFallbackConfig()
        )
        autoselect_committee = (
            PlanningAutoselectCommitteeConfig.from_dict(data["autoselect_committee"])
            if "autoselect_committee" in data
            else PlanningAutoselectCommitteeConfig()
        )
        return cls(
            provider=provider,
            enabled=bool(data.get("enabled", True)),
            mode=str(data.get("mode", "auto")).strip() or "auto",
            test_plan_max_parallelism=int(data.get("test_plan_max_parallelism", 3)),
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
            legacy_fallback=legacy_fallback,
            autoselect_committee=autoselect_committee,
            committee=committee,
            gates=gates,
        )


@dataclass(frozen=True, slots=True)
class CandidateModeTriggers:
    rework_count_gte: int = 1
    planner_confidence_lt: float = 0.75
    require_clear_design_branches: bool = False

    @classmethod
    def from_dict(cls, payload: Any) -> CandidateModeTriggers:
        data = _require_mapping(payload, field_name="implementation.candidate_mode.triggers")
        return cls(
            rework_count_gte=int(data.get("rework_count_gte", 1)),
            planner_confidence_lt=float(data.get("planner_confidence_lt", 0.75)),
            require_clear_design_branches=bool(data.get("require_clear_design_branches", False)),
        )


@dataclass(frozen=True, slots=True)
class ProtectedConfigAllowlistSource:
    issue_body_section: str = "保護設定変更許可リスト"
    artifacts: list[str] = field(default_factory=lambda: ["protected_config_allowlist.json"])

    @classmethod
    def from_dict(cls, payload: Any) -> ProtectedConfigAllowlistSource:
        data = _require_mapping(payload, field_name="protected_config.allowlist_source")
        priority = _require_list_of_mappings(
            data.get("priority", []),
            field_name="protected_config.allowlist_source.priority",
        )
        issue_body_section = "保護設定変更許可リスト"
        artifacts: list[str] = []
        for item in priority:
            section = str(item.get("issue_body_section", "")).strip()
            artifact = str(item.get("artifact", "")).strip()
            if section and issue_body_section == "保護設定変更許可リスト":
                issue_body_section = section
            if artifact:
                artifacts.append(artifact)
        if not artifacts:
            artifacts = ["protected_config_allowlist.json"]
        return cls(issue_body_section=issue_body_section, artifacts=artifacts)


@dataclass(frozen=True, slots=True)
class ProtectedConfigConfig:
    default_policy: str = "deny"
    allow_label: str = "allow-protected-config"
    protected_paths: list[str] = field(default_factory=list)
    allowlist_source: ProtectedConfigAllowlistSource = field(default_factory=ProtectedConfigAllowlistSource)

    @classmethod
    def from_dict(cls, payload: Any) -> ProtectedConfigConfig:
        data = _require_mapping(payload, field_name="protected_config")
        return cls(
            default_policy=str(data.get("default", "deny")).strip() or "deny",
            allow_label=str(data.get("allow_label", "allow-protected-config")).strip() or "allow-protected-config",
            protected_paths=_require_list_of_strings(
                data.get("protected_paths", []),
                field_name="protected_config.protected_paths",
            ),
            allowlist_source=ProtectedConfigAllowlistSource.from_dict(data.get("allowlist_source", {}))
            if "allowlist_source" in data
            else ProtectedConfigAllowlistSource(),
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
class ReplanningConfig:
    enabled: bool = True
    auto_replan_on_reject_reasons: list[str] = field(default_factory=lambda: ["plan_misalignment", "scope_drift"])
    max_replans_per_issue: int = 2
    emit_replan_reason_artifact: bool = True
    create_new_attempt_on_replan: bool = True

    @classmethod
    def from_dict(cls, payload: Any) -> ReplanningConfig:
        data = _require_mapping(payload, field_name="replanning")
        max_replans_per_issue = int(data.get("max_replans_per_issue", 2))
        if max_replans_per_issue < 0:
            raise WorkflowValidationError("replanning.max_replans_per_issue must be >= 0")
        return cls(
            enabled=bool(data.get("enabled", True)),
            auto_replan_on_reject_reasons=_require_list_of_strings(
                data.get("auto_replan_on_reject_reasons", ["plan_misalignment", "scope_drift"]),
                field_name="replanning.auto_replan_on_reject_reasons",
            ),
            max_replans_per_issue=max_replans_per_issue,
            emit_replan_reason_artifact=bool(data.get("emit_replan_reason_artifact", True)),
            create_new_attempt_on_replan=bool(data.get("create_new_attempt_on_replan", True)),
        )


@dataclass(frozen=True, slots=True)
class CompactionPolicyConfig:
    turn_count_gte: int = 12
    steer_count_gte: int = 2
    repair_cycles_gte: int = 3

    @classmethod
    def from_dict(cls, payload: Any) -> CompactionPolicyConfig:
        data = _require_mapping(payload, field_name="codex.compaction_policy")
        return cls(
            turn_count_gte=int(data.get("turn_count_gte", 12)),
            steer_count_gte=int(data.get("steer_count_gte", 2)),
            repair_cycles_gte=int(data.get("repair_cycles_gte", 3)),
        )


@dataclass(frozen=True, slots=True)
class CodexConfig:
    command: str = "codex app-server"
    model: str = "gpt-5.4"
    reasoning_effort: str = "medium"
    summary: str = "concise"
    approval_policy: str = "never"
    thread_sandbox: str = "workspace-write"
    writable_roots: list[str] = field(default_factory=list)
    network_access: bool = False
    turn_timeout_ms: int = 3_600_000
    read_timeout_ms: int = 5_000
    allow_turn_steer: bool = False
    allow_thread_resume_same_run_only: bool = True
    compaction_policy: CompactionPolicyConfig = field(default_factory=CompactionPolicyConfig)
    service_name: str = "dev-bot"

    @classmethod
    def from_dict(cls, payload: Any) -> CodexConfig:
        data = _require_mapping(payload, field_name="codex")
        return cls(
            command=str(data.get("command", "codex app-server")).strip() or "codex app-server",
            model=str(data.get("model", "gpt-5.4")).strip() or "gpt-5.4",
            reasoning_effort=str(data.get("reasoning_effort", "medium")).strip() or "medium",
            summary=str(data.get("summary", "concise")).strip() or "concise",
            approval_policy=str(data.get("approval_policy", "never")).strip() or "never",
            thread_sandbox=str(data.get("thread_sandbox", "workspace-write")).strip() or "workspace-write",
            writable_roots=_require_list_of_strings(
                data.get("writable_roots", []),
                field_name="codex.writable_roots",
            ),
            network_access=bool(data.get("network_access", False)),
            turn_timeout_ms=int(data.get("turn_timeout_ms", 3_600_000)),
            read_timeout_ms=int(data.get("read_timeout_ms", 5_000)),
            allow_turn_steer=bool(data.get("allow_turn_steer", False)),
            allow_thread_resume_same_run_only=bool(data.get("allow_thread_resume_same_run_only", True)),
            compaction_policy=CompactionPolicyConfig.from_dict(data["compaction_policy"])
            if "compaction_policy" in data
            else CompactionPolicyConfig(),
            service_name=str(data.get("service_name", "dev-bot")).strip() or "dev-bot",
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
    codex: CodexConfig | None = None
    implementation: ImplementationConfig | None = None
    replanning: ReplanningConfig | None = None
    protected_config: ProtectedConfigConfig | None = None
    review: ReviewConfig | None = None
    verification: VerificationConfig | None = None
    incident_bundle: IncidentBundleConfig | None = None
    evals: EvalsConfig | None = None
    telemetry: TelemetryConfig | None = None

    @classmethod
    def from_dict(cls, payload: Any) -> WorkflowConfig:
        data = _require_mapping(payload, field_name="workflow")
        planning = PlanningConfig.from_dict(data["planning"]) if "planning" in data else None
        codex = CodexConfig.from_dict(data["codex"]) if "codex" in data else None
        implementation = ImplementationConfig.from_dict(data["implementation"]) if "implementation" in data else None
        replanning = ReplanningConfig.from_dict(data["replanning"]) if "replanning" in data else None
        protected_config = (
            ProtectedConfigConfig.from_dict(data["protected_config"]) if "protected_config" in data else None
        )
        review = ReviewConfig.from_dict(data["review"]) if "review" in data else None
        verification = VerificationConfig.from_dict(data["verification"]) if "verification" in data else None
        incident_bundle = None
        if isinstance(data.get("debug"), dict) and "incident_bundle" in data["debug"]:
            incident_bundle = IncidentBundleConfig.from_dict(data["debug"]["incident_bundle"])
        evals = EvalsConfig.from_dict(data["evals"]) if "evals" in data else None
        telemetry = TelemetryConfig.from_dict(data["telemetry"]) if "telemetry" in data else None
        return cls(
            planning=planning,
            codex=codex,
            implementation=implementation,
            replanning=replanning,
            protected_config=protected_config,
            review=review,
            verification=verification,
            incident_bundle=incident_bundle,
            evals=evals,
            telemetry=telemetry,
        )
