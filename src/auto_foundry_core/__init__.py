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
    FoundationTask,
    IdentityCandidate,
    IdentityDecision,
    IdentityEvidence,
    KnowledgeDelta,
    LEMRef,
    OntologyItem,
    OperationReceipt,
    OperationResultRef,
    OperationSpec,
    PreparedAssetDescriptor,
    PreparedAssetRef,
    RequirementPortfolioPlan,
    RequirementRecord,
    RunTelemetrySummary,
    TableRef,
    TelemetryEvent,
)
from .workspace import AllowedRootError, RunContext, require_allowed_roots
from .sources import discover, preview, register_source, read_rows
from .profiling import profile_rows, profile_source
from .normalization import normalize_rows, normalize_value, parse_date, parse_number
from .identity import apply_decision, generate_candidates, mapping_coverage
from .relationships import measure_relationship
from .populations import PopulationLedger
from .aggregation import aggregate_rows
from .artifacts import write_artifact, write_manifest
from .reproduction import compare_results, reproduce
from .cache import RunCache
from .telemetry import TelemetryRecorder
from .enterprise_model import LivingEnterpriseModel
from .catalog import capability_catalog, get_capability
from .runtime import CoreExecutionResult, CoreRuntime
from .workbench import (
    CatalogCounts,
    DataRoom,
    DataRoomCatalogEntry,
    DataRoomMember,
    DataRoomWorkbench,
    PreparedAsset,
)
from .prepared import PreparedAssetRegistry
from .analysis import (
    BoundAnalysisContext,
    CatalogSnapshot,
    ControlledScriptRunner,
    ScriptExecutionReceipt,
    ScriptRunReport,
    load_bound_analysis_context,
)
from .integration import (
    AcceptedAnalysisBundle,
    IntegrationRecord,
    IntegrationSession,
    IntegrationValidation,
)
from .lifecycle import (
    AgentInvocationReceipt,
    InvocationReceiptLedger,
    RunLifecycle,
    RunLifecycleSnapshot,
    classify_invocation_terminal_reason,
    classify_terminal_reason,
    recovery_classification,
)
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

__version__ = "0.3.0"

__all__ = [
    "AggregationSpec", "AllowedRootError", "CanonicalMapping",
    "CapabilityDescriptor", "CatalogCounts", "CoreExecutionResult", "CoreRuntime", "DataAssetRef", "DataRoom",
    "DataRoomCatalogEntry", "DataRoomMember", "DataRoomWorkbench", "DocumentRef", "FieldRef",
    "PreparedAsset", "PreparedAssetRegistry", "AcceptedSnapshot", "ArtifactProgress", "ExecutionAttempt", "ITEM_STATE_FIELDS",
    "ITEM_STATE_SCHEMA", "ItemWorkspace", "ProgressDecision",
    "FoundationTask", "IdentityCandidate", "IdentityDecision",
    "IdentityEvidence", "KnowledgeDelta", "LEMRef", "LivingEnterpriseModel",
    "OntologyItem", "OperationReceipt", "OperationResultRef", "OperationSpec",
    "PopulationLedger", "PreparedAssetDescriptor", "PreparedAssetRef",
    "RequirementPortfolioPlan", "RequirementRecord", "RunCache",
    "RunTelemetrySummary", "TableRef", "TelemetryEvent", "TelemetryRecorder",
    "RunContext", "aggregate_rows", "apply_decision", "capability_catalog",
    "compare_results", "discover", "generate_candidates", "get_capability",
    "mapping_coverage", "measure_relationship", "normalize_rows",
    "normalize_value", "parse_date", "parse_number", "preview", "profile_rows",
    "profile_source", "read_rows", "register_source", "reproduce",
    "write_artifact", "write_manifest", "require_allowed_roots",
    "DATA_ASSET_REFERENCE", "OPERATION_RESULT_REFERENCE", "REFERENCE_DISCRIMINATOR",
    "decode_explicit_reference", "decode_reference_value", "encode_explicit_reference",
    "encode_reference_value", "is_explicit_reference_mapping",
    "BoundAnalysisContext", "CatalogSnapshot", "ControlledScriptRunner",
    "ScriptExecutionReceipt", "ScriptRunReport", "load_bound_analysis_context",
    "AcceptedAnalysisBundle", "IntegrationRecord", "IntegrationSession", "IntegrationValidation",
    "AgentInvocationReceipt", "InvocationReceiptLedger", "RunLifecycle", "RunLifecycleSnapshot",
    "classify_invocation_terminal_reason", "classify_terminal_reason", "recovery_classification",
    "FREEZE_MARKER_FIELDS", "FreezeMarkers", "ProductContractError", "decode_freeze_markers",
    "validate_freeze_markers",
]
