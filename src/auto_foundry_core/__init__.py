"""Small, source-agnostic deterministic analytics core.

The public API deliberately consists of typed contracts and a handful of
deterministic operations.  Optional readers (Excel and Parquet) are imported
only when used, so the package remains installable in a clean offline Python
environment.
"""

from .contracts import (
    AggregationSpec,
    CanonicalMapping,
    CapabilityDescriptor,
    DataAssetRef,
    DocumentRef,
    FieldRef,
    IncidentRecord,
    IdentityCandidate,
    IdentityDecision,
    IdentityEvidence,
    ImplementationTransition,
    KnowledgeDelta,
    LEMRef,
    OntologyItem,
    OperationReceipt,
    OperationResultRef,
    OperationSpec,
    PhaseTimingRecord,
    PreparedAssetDescriptor,
    PreparedAssetRef,
    RequirementAnalysisPlan,
    RequirementAnalysisTask,
    RequirementRecord,
    RunTelemetrySummary,
    TableRef,
    TelemetryEvent,
)
from .workspace import AllowedRootError, RunContext, require_allowed_roots
from .sources import discover, preview, register_source, read_rows
from .profiling import profile_rows, profile_source
from .normalization import normalize_rows, normalize_value, observation_as_of, parse_date, parse_number
from .identity import apply_decision, generate_candidates, mapping_coverage
from .mapping_view import IdentityMappingView, MappingCompletenessAdvisory
from .semantic_advisory import SemanticPromotionSuggestion, suggest_semantic_promotions
from .relationships import measure_relationship
from .populations import PopulationLedger
from .aggregation import aggregate_rows
from .artifacts import write_artifact, write_manifest
from .reproduction import compare_results, reproduce
from .cache import RunCache
from .telemetry import TelemetryRecorder
from .enterprise_model import LivingEnterpriseModel
from .lem_projection import LEMCommittedBinding, LEMProjection, LivingEnterpriseModelProjector
from .entity_resolution import (
    EntityResolutionResult,
    IdentityDomainScope,
    IdentityDomainRequest,
    EntityResolutionWorkspace,
    IdentityDomainReservation,
    ResolutionCapacity,
    ResolutionCommit,
    StaleIdentityScopeError,
    WorkerLease,
    replay_ready_commits,
)
from .catalog import capability_catalog, get_capability
from .runtime import CoreExecutionResult, CoreRuntime
from .autopilot import ActionDispatcher, AutopilotTick, LocalRunAutopilot
from .coordinator import (
    CONTROL_PLANE_DIRNAME,
    COORDINATOR_EVENTS_FILENAME,
    COORDINATOR_LOCK_FILENAME,
    COORDINATOR_SPEC_FILENAME,
    COORDINATOR_STATE_FILENAME,
    CommandRoleAdapter,
    CodexExecConfig,
    CoordinatorConflictError,
    CoordinatorError,
    CoordinatorIntegrityError,
    CoordinatorPublicationError,
    CoordinatorRunSpec,
    CoordinatorStatus,
    CodexRoleAdapter,
    MappingRoleAdapter,
    PlannerActionProvider,
    RequirementPlannerProvider,
    RoleAdapter,
    RoleExecution,
    RoleRunner,
    RunCoordinator,
    build_role_prompt,
    start_coordinator,
)
from .workbench import (
    CatalogCounts,
    DataRoom,
    DataRoomCatalogEntry,
    DataRoomMember,
    DataRoomWorkbench,
    PreparedAsset,
)
from .prepared import PreparedAssetRegistry
from .semantic_store import (
    ContextPayloadRef,
    SemanticSelectionRef,
    SemanticSnapshotRef,
    SemanticSnapshotStore,
    canonical_context_payload,
)
from .analysis import (
    ANALYSIS_SOURCE_MAP_ENV,
    BoundAnalysisContext,
    CatalogSnapshot,
    ControlledScriptRunner,
    ScriptExecutionReceipt,
    ScriptRunReport,
    ScriptValidationResult,
    load_bound_analysis_context,
    load_selected_source_ids,
)
from .analyst_workspace import (
    AnalystAnswer,
    AnalystBrief,
    AnalystSource,
    AnalystWorkspace,
    AnalyticalRelationshipEvidence,
    BusinessReviewAdapter,
    DataInsufficiencyConclusion,
    EvidenceNote,
    IdentityDomainProposal,
    ReviewFinding,
    SpecialistMemo,
    SpecialistTask,
)
from .requirement_planning import (
    PLANNER_INCIDENT_FILENAME,
    SUPERVISOR_PLAN_FILENAME,
    PlannerAction,
    RequirementExecutionGroup,
    RequirementExecutionPlan,
    RequirementRunSnapshot,
    RequirementSupervisorWorkspace,
    compact_catalog_payload,
)
from .integration import (
    AcceptedAnalysisBundle,
    CurrentObservationFact,
    collision_safe_record_id,
    deterministic_record_id,
    IntegrationRecord,
    IntegrationSession,
    IntegrationValidation,
    make_record_id,
    validate_record_id,
)
from .integration_review import (
    FidelityFinding,
    FidelityPacket,
    FidelityRepairAuthorization,
    FidelityRepairProgress,
    FidelityResult,
    FidelityReviewResult,
    IntegrationFidelityPacket,
    write_packet,
    write_result,
)
from .reporting import (
    RunReportFinalizer,
    RunReportProjector,
    finalize_run_report,
    project_run_report,
)
from .lifecycle import (
    ACTIVE_GENERATION_POINTER_FILENAME,
    AgentInvocationReceipt,
    GENERATION_DIRECTORY,
    GENERATION_MANIFEST_FILENAME,
    GENERATION_PLAN_FILENAME,
    GENERATION_STATE_FILENAME,
    InvocationReceiptLedger,
    RunLifecycle,
    RunGenerationSnapshot,
    RunLifecycleSnapshot,
    classify_invocation_terminal_reason,
    classify_terminal_reason,
    recovery_classification,
)
from .run_extension import GenerationManifest, RequirementRunExtension, RequirementRunGeneration, RunExtension
from .product_contracts import (
    FREEZE_MARKER_FIELDS,
    FreezeMarkers,
    ProductContractError,
    decode_freeze_markers,
    validate_freeze_markers,
)
from .durable import (
    AcceptedSnapshot,
    ArtifactProgress,
    ExecutionAttempt,
    ITEM_STATE_FIELDS,
    ITEM_STATE_SCHEMA,
    ItemWorkspace,
    ProgressDecision,
)
from .references import (
    DATA_ASSET_REFERENCE,
    OPERATION_RESULT_REFERENCE,
    REFERENCE_DISCRIMINATOR,
    decode_explicit_reference,
    decode_reference_value,
    encode_explicit_reference,
    encode_reference_value,
    is_explicit_reference_mapping,
)

__version__ = "0.8.0"

__all__ = [
    "CONTROL_PLANE_DIRNAME", "COORDINATOR_EVENTS_FILENAME", "COORDINATOR_LOCK_FILENAME", "COORDINATOR_SPEC_FILENAME", "COORDINATOR_STATE_FILENAME",
    "CommandRoleAdapter", "CodexExecConfig", "CodexRoleAdapter", "CoordinatorConflictError",
    "CoordinatorError", "CoordinatorIntegrityError", "CoordinatorPublicationError", "CoordinatorRunSpec",
    "CoordinatorStatus", "MappingRoleAdapter", "PlannerActionProvider", "RequirementPlannerProvider", "RoleAdapter", "RoleExecution", "RoleRunner", "RunCoordinator", "build_role_prompt", "start_coordinator",
    "AggregationSpec", "AllowedRootError", "CanonicalMapping",
    "ActionDispatcher", "AutopilotTick", "LocalRunAutopilot",
    "CapabilityDescriptor", "CatalogCounts", "CoreExecutionResult", "CoreRuntime", "DataAssetRef", "DataRoom",
    "DataRoomCatalogEntry", "DataRoomMember", "DataRoomWorkbench", "DocumentRef", "FieldRef",
    "PreparedAsset", "PreparedAssetRegistry", "AcceptedSnapshot", "ArtifactProgress", "ExecutionAttempt", "ITEM_STATE_FIELDS",
    "ITEM_STATE_SCHEMA", "ItemWorkspace", "ProgressDecision",
    "IdentityCandidate", "IdentityDecision",
    "IdentityMappingView", "MappingCompletenessAdvisory",
    "SemanticPromotionSuggestion", "suggest_semantic_promotions",
    "IdentityEvidence", "IncidentRecord", "ImplementationTransition", "KnowledgeDelta", "LEMRef", "LivingEnterpriseModel",
    "LEMCommittedBinding", "LEMProjection", "LivingEnterpriseModelProjector",
    "EntityResolutionResult", "IdentityDomainScope", "IdentityDomainRequest", "EntityResolutionWorkspace", "IdentityDomainReservation",
    "ResolutionCapacity", "ResolutionCommit", "StaleIdentityScopeError", "WorkerLease", "replay_ready_commits",
    "OntologyItem", "OperationReceipt", "OperationResultRef", "OperationSpec",
    "PhaseTimingRecord",
    "PopulationLedger", "PreparedAssetDescriptor", "PreparedAssetRef",
    "RequirementAnalysisPlan", "RequirementAnalysisTask", "RequirementRecord", "RunCache",
    "PLANNER_INCIDENT_FILENAME", "SUPERVISOR_PLAN_FILENAME", "PlannerAction",
    "RequirementExecutionGroup", "RequirementExecutionPlan", "RequirementRunSnapshot",
    "RequirementSupervisorWorkspace", "compact_catalog_payload",
    "RunTelemetrySummary", "TableRef", "TelemetryEvent", "TelemetryRecorder",
    "RunContext", "aggregate_rows", "apply_decision", "capability_catalog",
    "compare_results", "discover", "generate_candidates", "get_capability",
    "mapping_coverage", "measure_relationship", "normalize_rows",
    "normalize_value", "observation_as_of", "parse_date", "parse_number", "preview", "profile_rows",
    "profile_source", "read_rows", "register_source", "reproduce",
    "write_artifact", "write_manifest", "require_allowed_roots",
    "DATA_ASSET_REFERENCE", "OPERATION_RESULT_REFERENCE", "REFERENCE_DISCRIMINATOR",
    "decode_explicit_reference", "decode_reference_value", "encode_explicit_reference",
    "encode_reference_value", "is_explicit_reference_mapping",
    "ANALYSIS_SOURCE_MAP_ENV", "BoundAnalysisContext", "CatalogSnapshot", "ContextPayloadRef", "ControlledScriptRunner",
    "canonical_context_payload",
    "ScriptExecutionReceipt", "ScriptRunReport", "ScriptValidationResult", "load_bound_analysis_context", "load_selected_source_ids",
    "SemanticSelectionRef", "SemanticSnapshotRef", "SemanticSnapshotStore",
    "AnalystAnswer", "AnalystBrief", "AnalystSource", "AnalystWorkspace",
    "BusinessReviewAdapter", "DataInsufficiencyConclusion", "EvidenceNote", "IdentityDomainProposal", "AnalyticalRelationshipEvidence", "ReviewFinding", "SpecialistMemo", "SpecialistTask",
    "AcceptedAnalysisBundle", "CurrentObservationFact", "IntegrationRecord", "IntegrationSession", "IntegrationValidation",
    "IntegrationFidelityPacket", "FidelityPacket", "FidelityFinding", "FidelityResult", "FidelityReviewResult", "FidelityRepairAuthorization", "FidelityRepairProgress", "deterministic_record_id",
    "collision_safe_record_id", "make_record_id", "validate_record_id", "write_packet", "write_result",
    "RunReportFinalizer", "RunReportProjector", "finalize_run_report", "project_run_report",
    "ACTIVE_GENERATION_POINTER_FILENAME", "GENERATION_DIRECTORY", "GENERATION_MANIFEST_FILENAME",
    "GENERATION_PLAN_FILENAME", "GENERATION_STATE_FILENAME", "AgentInvocationReceipt", "InvocationReceiptLedger",
    "RunGenerationSnapshot", "RunLifecycle", "RunLifecycleSnapshot", "GenerationManifest", "RequirementRunGeneration",
    "RequirementRunExtension", "RunExtension",
    "classify_invocation_terminal_reason", "classify_terminal_reason", "recovery_classification",
    "FREEZE_MARKER_FIELDS", "FreezeMarkers", "ProductContractError", "decode_freeze_markers",
    "validate_freeze_markers",
]
