from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
import json
import math
import os
import re
from typing import Any, Iterable

import pandas as pd

from auto_foundry_core.analysis import BoundAnalysisContext, load_selected_source_ids
from auto_foundry_core.analytics_toolkit import profile_data


OUTPUT_ROOT = Path(os.environ["AUTO_FOUNDRY_ANALYSIS_OUTPUT_ROOT"])
CONTEXT_PATH = os.environ["AUTO_FOUNDRY_ANALYSIS_CONTEXT"]
REQUIREMENT_ID = "REQ-002"
BATCH_SIZE = 250_000
PHASE = os.environ.get("AUTO_FOUNDRY_ANALYSIS_PHASE", "full")


def json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite analytical value")
        return value
    if isinstance(value, Decimal):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("non-finite decimal value")
        return result
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except (TypeError, ValueError):
            pass
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    if hasattr(value, "item"):
        return json_safe(value.item())
    return str(value)


def as_date(value: Any) -> str | None:
    if value is None:
        return None
    safe = json_safe(value)
    text = str(safe)
    return text[:10] if len(text) >= 10 else text


def as_content(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def canonical_json(value: Any) -> str:
    return json.dumps(json_safe(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def write_json(name: str, value: Any) -> None:
    target = OUTPUT_ROOT / name
    target.write_text(
        json.dumps(json_safe(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False),
        encoding="utf-8",
    )


def read_all(room: Any, source_id: str) -> list[dict[str, Any]]:
    return room.read_rows(source_id, limit=None)


def scan_selected(
    room: Any,
    source_id: str,
    wanted: set[str],
    fields: Iterable[str],
) -> tuple[list[dict[str, Any]], int]:
    # The public Parquet reader already streams Arrow record batches from one
    # verified temporary materialization.  One full call avoids repeatedly
    # rematerializing and rescanning the same selected member.  Smoke uses a
    # bounded prefix solely to exercise types and output contracts.
    rows = room.read_rows(source_id, limit=(5_000 if PHASE == "smoke" else None))
    total = len(rows)
    field_names = tuple(fields)
    selected = [
        {name: json_safe(row.get(name)) for name in field_names}
        for row in rows
        if str(row.get("SALESDOCUMENT") or "") in wanted
    ]
    return selected, total


bound = BoundAnalysisContext.load(path=CONTEXT_PATH)
selected_source_ids = load_selected_source_ids()
if len(selected_source_ids) != 5:
    raise ValueError("REQ-002 requires exactly five AO-selected sources")
(
    business_documents_id,
    sales_documents_id,
    sales_items_id,
    erp_transactions_id,
    supporting_documents_id,
) = selected_source_ids
room = bound.workbench.data_room

business_rows = read_all(room, business_documents_id)
supporting_rows = read_all(room, supporting_documents_id)
if len(business_rows) != 32:
    raise ValueError("business-document population drifted from the bound 32-row catalog")

business_frame = pd.DataFrame(business_rows)
supporting_frame = pd.DataFrame(supporting_rows)
business_period = {
    "field": "created_date",
    "minimum": as_date(business_frame["created_date"].min()),
    "maximum": as_date(business_frame["created_date"].max()),
}
supporting_date_values: list[str] = []
for field in ("order_date", "invoice_date", "issue_date", "ship_date", "inspection_date"):
    if field in supporting_frame:
        supporting_date_values.extend(
            as_date(value) for value in supporting_frame[field].dropna().tolist() if as_date(value) is not None
        )
supporting_period = {
    "basis": "observed operational document dates",
    "minimum": min(supporting_date_values) if supporting_date_values else None,
    "maximum": max(supporting_date_values) if supporting_date_values else None,
}

business_profile = profile_data(
    business_frame,
    requirement_id=REQUIREMENT_ID,
    columns=("document_type", "document_id", "created_date", "created_by", "title"),
    period=business_period,
    population={"kind": "business_document_rows", "row_count": len(business_rows)},
    max_frequency_values=10,
)
supporting_profile = profile_data(
    supporting_frame,
    requirement_id=REQUIREMENT_ID,
    columns=(
        "document_type",
        "document_id",
        "order_number",
        "customer_id",
        "currency",
        "invoice_number",
        "invoice_date",
        "due_date",
        "billed_amount",
        "total_amount",
    ),
    period=supporting_period,
    population={"kind": "supporting_document_rows", "row_count": len(supporting_rows)},
    max_frequency_values=10,
)
(OUTPUT_ROOT / "business_profile_artifact.json").write_text(business_profile.to_json(), encoding="utf-8")
(OUTPUT_ROOT / "supporting_profile_artifact.json").write_text(supporting_profile.to_json(), encoding="utf-8")

kpi_inventory: list[dict[str, Any]] = []
invoice_references: list[dict[str, Any]] = []
internal_checks: list[dict[str, Any]] = []
document_period_checks: list[dict[str, Any]] = []
document_id_dates: dict[str, set[str]] = defaultdict(set)
document_id_payloads: dict[str, set[str]] = defaultdict(set)
type_snapshots: dict[str, list[str]] = defaultdict(list)


def add_kpi(row: dict[str, Any], name: str, value: Any, unit: str, period: str | None, method: str) -> None:
    kpi_inventory.append(
        {
            "document_id": str(row["document_id"]),
            "document_type": str(row["document_type"]),
            "created_date": as_date(row.get("created_date")),
            "metric": name,
            "value": json_safe(value),
            "unit": unit,
            "period": period,
            "method": method,
        }
    )


for row in business_rows:
    document_id = str(row["document_id"])
    document_type = str(row["document_type"])
    created_date = as_date(row.get("created_date"))
    content = as_content(row.get("content"))
    document_id_dates[document_id].add(str(created_date))
    document_id_payloads[document_id].add(canonical_json(content))
    type_snapshots[document_type].append(canonical_json(content))
    stated_period = as_date(content.get("report_date")) or as_date(content.get("report_week")) or content.get("period") or as_date(content.get("meeting_date"))

    if document_type == "MEETING_AGENDA":
        agenda = " ".join(str(value) for value in (content.get("agenda_items") or []))
        actions = " ".join(str(value) for value in (content.get("action_items_from_last_week") or []))
        pipeline = re.search(r"\$([0-9.]+)M\b", agenda)
        at_risk = re.search(r"(\d+)\s+at-risk", agenda)
        forecast = re.search(r"forecasts\s*-\s*([0-9.]+)%", actions, re.IGNORECASE)
        if pipeline:
            add_kpi(row, "q4_pipeline_opportunity", float(pipeline.group(1)) * 1_000_000, "currency_unspecified", stated_period, "explicit agenda text")
        if at_risk:
            add_kpi(row, "at_risk_accounts", int(at_risk.group(1)), "accounts", stated_period, "explicit agenda text")
        if forecast:
            add_kpi(row, "q4_forecast_submission_completion", float(forecast.group(1)), "percent", stated_period, "explicit action-item text")
    elif document_type == "QUALITY_METRICS_REPORT":
        add_kpi(row, "customer_complaints", content.get("customer_complaints"), "complaints", stated_period, "structured document field")
        add_kpi(row, "rma_requests", content.get("rma_requests"), "requests", stated_period, "structured document field")
        total_units = 0.0
        total_defects = 0.0
        for plant in content.get("plant_metrics") or []:
            units = float(plant.get("units_produced") or 0)
            defects = float(plant.get("defects") or 0)
            stated_rate = float(plant.get("defect_rate") or 0)
            computed_rate = 100.0 * defects / units if units else None
            total_units += units
            total_defects += defects
            add_kpi(row, f"plant_{plant.get('plant')}_units_produced", units, "units", stated_period, "structured document field")
            add_kpi(row, f"plant_{plant.get('plant')}_defect_rate", stated_rate, "percent", stated_period, "structured document field")
            internal_checks.append(
                {
                    "document_id": document_id,
                    "check": "plant_defect_rate_recalculation",
                    "entity": str(plant.get("plant")),
                    "stated": stated_rate,
                    "computed": computed_rate,
                    "difference": None if computed_rate is None else stated_rate - computed_rate,
                    "consistent": computed_rate is not None and abs(stated_rate - computed_rate) <= 1e-9,
                }
            )
        weighted_rate = 100.0 * total_defects / total_units if total_units else None
        add_kpi(row, "all_plants_units_produced", total_units, "units", stated_period, "sum of structured plant rows")
        add_kpi(row, "all_plants_defects", total_defects, "defects", stated_period, "sum of structured plant rows")
        add_kpi(row, "all_plants_weighted_defect_rate", weighted_rate, "percent", stated_period, "defects divided by units produced")
    elif document_type == "OVERDUE_INVOICE_REPORT":
        invoices = content.get("invoices") or []
        stated_count = int(content.get("invoice_count") or 0)
        stated_total = float(content.get("total_overdue") or 0)
        computed_total = sum(float(invoice.get("NETAMOUNT") or 0) for invoice in invoices)
        add_kpi(row, "overdue_invoice_count", stated_count, "invoice_rows", stated_period, "structured document field")
        add_kpi(row, "total_overdue", stated_total, "currency_unspecified", stated_period, "structured document field")
        internal_checks.extend(
            (
                {
                    "document_id": document_id,
                    "check": "invoice_count_equals_embedded_rows",
                    "stated": stated_count,
                    "computed": len(invoices),
                    "difference": stated_count - len(invoices),
                    "consistent": stated_count == len(invoices),
                },
                {
                    "document_id": document_id,
                    "check": "total_overdue_equals_embedded_netamount_sum",
                    "stated": stated_total,
                    "computed": computed_total,
                    "difference": stated_total - computed_total,
                    "consistent": abs(stated_total - computed_total) <= 0.01,
                },
            )
        )
        for position, invoice in enumerate(invoices, start=1):
            invoice_references.append(
                {
                    "reference_id": f"{document_id}:{position:02d}",
                    "document_id": document_id,
                    "report_date": as_date(content.get("report_date")) or created_date,
                    "sales_document": str(invoice.get("SALESDOCUMENT") or ""),
                    "sold_to_party": str(invoice.get("SOLDTOPARTY") or ""),
                    "transaction_creation_date": as_date(invoice.get("CREATIONDATE")),
                    "net_amount": float(invoice.get("NETAMOUNT") or 0),
                }
            )
    elif document_type == "VENDOR_SCORECARD":
        total_spend = float(content.get("total_spend") or 0)
        cost_savings = float(content.get("cost_savings") or 0)
        add_kpi(row, "vendor_total_spend", total_spend, "currency_unspecified", stated_period, "structured document field")
        add_kpi(row, "vendor_cost_savings", cost_savings, "currency_unspecified", stated_period, "structured document field")
        add_kpi(row, "vendor_savings_rate", (100.0 * cost_savings / total_spend if total_spend else None), "percent", stated_period, "cost_savings divided by total_spend")
        for vendor in content.get("vendors") or []:
            for metric in ("on_time", "price_stability", "quality"):
                add_kpi(row, f"vendor_{vendor.get('name')}_{metric}", vendor.get(metric), "score_or_percent_unspecified", stated_period, "structured vendor row")

    period_consistent: bool | None = None
    reason = "no explicit period field"
    if isinstance(content.get("period"), str) and re.fullmatch(r"\d{4}-\d{2}", content["period"]):
        period_consistent = bool(created_date and created_date.startswith(content["period"]))
        reason = "created month compared with stated YYYY-MM period"
    elif content.get("report_date") is not None:
        period_consistent = as_date(content.get("report_date")) == created_date
        reason = "report_date compared with created_date"
    elif content.get("report_week") is not None:
        period_consistent = as_date(content.get("report_week")) == created_date
        reason = "report_week compared with created_date"
    elif content.get("meeting_date") is not None:
        period_consistent = as_date(content.get("meeting_date")) == created_date
        reason = "meeting_date compared with created_date"
    document_period_checks.append(
        {
            "document_id": document_id,
            "document_type": document_type,
            "created_date": created_date,
            "stated_period": json_safe(stated_period),
            "consistent": period_consistent,
            "method": reason,
        }
    )

reference_ids = {row["sales_document"] for row in invoice_references if row["sales_document"]}
sales_headers, sales_document_population = scan_selected(
    room,
    sales_documents_id,
    reference_ids,
    (
        "SALESDOCUMENT",
        "TRANSACTIONCURRENCY",
        "CREATIONDATE",
        "SALESDOCUMENTTYPE",
        "SALESORGANIZATION",
    ),
)
sales_item_rows, sales_item_population = scan_selected(
    room,
    sales_items_id,
    reference_ids,
    ("SALESDOCUMENT", "SALESDOCUMENTITEM", "SOLDTOPARTY", "BILLTOPARTY", "PAYERPARTY", "PRODUCT"),
)
erp_rows, erp_population = scan_selected(
    room,
    erp_transactions_id,
    reference_ids,
    (
        "SALESDOCUMENT",
        "SALESDOCUMENTITEM",
        "SOLDTOPARTY",
        "BILLTOPARTY",
        "PAYERPARTY",
        "CREATIONDATE",
        "TRANSACTIONCURRENCY",
    ),
)

headers_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
items_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
erp_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
supporting_by_order: dict[str, list[dict[str, Any]]] = defaultdict(list)
for row in sales_headers:
    headers_by_id[str(row["SALESDOCUMENT"])].append(row)
for row in sales_item_rows:
    items_by_id[str(row["SALESDOCUMENT"])].append(row)
for row in erp_rows:
    erp_by_id[str(row["SALESDOCUMENT"])].append(row)
for row in supporting_rows:
    order_number = str(row.get("order_number") or "")
    if order_number:
        supporting_by_order[order_number].append(row)

reconciled_references: list[dict[str, Any]] = []
currency_by_report: dict[str, Counter[str]] = defaultdict(Counter)
amount_by_report_currency: dict[str, defaultdict[str, float]] = defaultdict(lambda: defaultdict(float))
for reference in invoice_references:
    sales_document = reference["sales_document"]
    headers = headers_by_id.get(sales_document, [])
    items = items_by_id.get(sales_document, [])
    erp_matches = erp_by_id.get(sales_document, [])
    supporting = supporting_by_order.get(sales_document, [])
    header_dates = {as_date(row.get("CREATIONDATE")) for row in headers}
    header_currencies = {str(row.get("TRANSACTIONCURRENCY") or "") for row in headers if row.get("TRANSACTIONCURRENCY")}
    item_parties = {str(row.get("SOLDTOPARTY") or "") for row in items if row.get("SOLDTOPARTY")}
    erp_parties = {str(row.get("SOLDTOPARTY") or "") for row in erp_matches if row.get("SOLDTOPARTY")}
    erp_dates = {as_date(row.get("CREATIONDATE")) for row in erp_matches}
    erp_currencies = {str(row.get("TRANSACTIONCURRENCY") or "") for row in erp_matches if row.get("TRANSACTIONCURRENCY")}
    supporting_parties = {str(row.get("customer_id") or "") for row in supporting if row.get("customer_id")}
    explicit_invoice_numbers = {str(row.get("invoice_number") or "") for row in supporting if row.get("invoice_number")}
    currencies = sorted(header_currencies | erp_currencies)
    for currency in currencies:
        currency_by_report[reference["document_id"]][currency] += 1
        amount_by_report_currency[reference["document_id"]][currency] += float(reference["net_amount"])
    reconciled_references.append(
        {
            **reference,
            "sales_header_match_count": len(headers),
            "sales_item_match_count": len(items),
            "erp_transaction_match_count": len(erp_matches),
            "header_currencies": currencies,
            "creation_date_matches_header": reference["transaction_creation_date"] in header_dates,
            "creation_date_matches_erp": reference["transaction_creation_date"] in erp_dates,
            "sold_to_matches_sales_items": reference["sold_to_party"] in item_parties,
            "sold_to_matches_erp": reference["sold_to_party"] in erp_parties,
            "supporting_order_match_count": len(supporting),
            "sold_to_matches_supporting": (reference["sold_to_party"] in supporting_parties if supporting else None),
            "supporting_explicit_invoice_numbers": sorted(explicit_invoice_numbers),
            "amount_comparison_status": "unverifiable_primary_tables_have_no_comparable_amount_field",
        }
    )

unique_reference_count = len(reference_ids)
matched_header_ids = {value for value in reference_ids if headers_by_id.get(value)}
matched_item_ids = {value for value in reference_ids if items_by_id.get(value)}
matched_erp_ids = {value for value in reference_ids if erp_by_id.get(value)}
matched_supporting_ids = {value for value in reference_ids if supporting_by_order.get(value)}
reference_event_count = len(invoice_references)

duplicate_document_ids = [
    {
        "document_id": document_id,
        "created_dates": sorted(dates),
        "row_count": sum(1 for row in business_rows if str(row["document_id"]) == document_id),
        "distinct_payload_count": len(document_id_payloads[document_id]),
    }
    for document_id, dates in sorted(document_id_dates.items())
    if sum(1 for row in business_rows if str(row["document_id"]) == document_id) > 1
]
unchanged_type_snapshots = {
    document_type: {
        "row_count": len(payloads),
        "distinct_payload_count": len(set(payloads)),
    }
    for document_type, payloads in sorted(type_snapshots.items())
}
currency_summary = {
    document_id: {
        "reference_counts": dict(sorted(counter.items())),
        "amount_by_currency": {key: value for key, value in sorted(amount_by_report_currency[document_id].items())},
        "mixed_currency": len(counter) > 1,
    }
    for document_id, counter in sorted(currency_by_report.items())
}

direct_disagreements = {
    "internal_check_failures": sum(not bool(row["consistent"]) for row in internal_checks),
    "period_check_failures": sum(row["consistent"] is False for row in document_period_checks),
    "missing_sales_header_reference_events": sum(row["sales_header_match_count"] == 0 for row in reconciled_references),
    "non_unique_sales_header_reference_events": sum(row["sales_header_match_count"] != 1 for row in reconciled_references),
    "missing_sales_item_reference_events": sum(row["sales_item_match_count"] == 0 for row in reconciled_references),
    "missing_erp_reference_events": sum(row["erp_transaction_match_count"] == 0 for row in reconciled_references),
    "creation_date_mismatches_header": sum(not row["creation_date_matches_header"] for row in reconciled_references),
    "creation_date_mismatches_erp": sum(not row["creation_date_matches_erp"] for row in reconciled_references),
    "sold_to_mismatches_sales_items": sum(not row["sold_to_matches_sales_items"] for row in reconciled_references),
    "sold_to_mismatches_erp": sum(not row["sold_to_matches_erp"] for row in reconciled_references),
    "duplicate_document_ids": len(duplicate_document_ids),
    "mixed_currency_overdue_reports": sum(bool(row["mixed_currency"]) for row in currency_summary.values()),
}

relationships = (
    {
        "relationship_id": "REQ002-BUSINESS-INVOICE-REF-TO-SALES-HEADER",
        "source_id": business_documents_id,
        "target_id": sales_documents_id,
        "join_keys": [{"source_field": "content.invoices[].SALESDOCUMENT", "target_field": "SALESDOCUMENT"}],
        "grain": "distinct embedded overdue-report sales-document reference to sales-document header",
        "cardinality": "many_to_one across report events; one_to_one by distinct sales-document key",
        "matched_pairs": len(matched_header_ids),
        "source_population": unique_reference_count,
        "target_population": sales_document_population,
        "matched_source_count": len(matched_header_ids),
        "matched_target_count": len(matched_header_ids),
        "source_coverage": (len(matched_header_ids) / unique_reference_count if unique_reference_count else 0.0),
        "target_coverage": (len(matched_header_ids) / sales_document_population if sales_document_population else 0.0),
        "as_of": business_period["maximum"],
        "date_authority": "document report_date for reference event; sales_documents.CREATIONDATE for order creation",
        "limitations": ["The business report calls these invoice rows but supplies SALESDOCUMENT, not an explicit invoice number."],
    },
    {
        "relationship_id": "REQ002-BUSINESS-INVOICE-REF-TO-SUPPORTING-ORDER",
        "source_id": business_documents_id,
        "target_id": supporting_documents_id,
        "join_keys": [{"source_field": "content.invoices[].SALESDOCUMENT", "target_field": "order_number"}],
        "grain": "distinct embedded report reference to supporting operational order",
        "cardinality": "one_to_many where supporting documents exist",
        "matched_pairs": len(matched_supporting_ids),
        "source_population": unique_reference_count,
        "target_population": len({str(row.get('order_number') or '') for row in supporting_rows if row.get('order_number')}),
        "matched_source_count": len(matched_supporting_ids),
        "matched_target_count": len(matched_supporting_ids),
        "source_coverage": (len(matched_supporting_ids) / unique_reference_count if unique_reference_count else 0.0),
        "target_coverage": (
            len(matched_supporting_ids) / len({str(row.get('order_number') or '') for row in supporting_rows if row.get('order_number')})
            if any(row.get('order_number') for row in supporting_rows)
            else 0.0
        ),
        "as_of": supporting_period["maximum"],
        "date_authority": "supporting-document event dates",
        "limitations": ["No explicit invoice_number is present in the business overdue report, so an invoice-number join is unavailable."],
    },
)

result = {
    "requirement_id": REQUIREMENT_ID,
    "method": {
        "toolkit": {
            "name": "profile_data",
            "parameters": {
                "business_columns": ["document_type", "document_id", "created_date", "created_by", "title"],
                "supporting_columns": [
                    "document_type",
                    "document_id",
                    "order_number",
                    "customer_id",
                    "currency",
                    "invoice_number",
                    "invoice_date",
                    "due_date",
                    "billed_amount",
                    "total_amount",
                ],
                "max_frequency_values": 10,
            },
        },
        "custom": "deterministic nested-struct extraction, exact-key reconciliation, arithmetic and period validation",
        "batch_size": BATCH_SIZE,
        "selected_source_ids": list(selected_source_ids),
    },
    "scope": {
        "business_document_rows": len(business_rows),
        "business_document_period": business_period,
        "supporting_document_rows": len(supporting_rows),
        "supporting_document_period": supporting_period,
        "sales_document_population": sales_document_population,
        "sales_item_population": sales_item_population,
        "erp_transaction_population": erp_population,
        "invoice_reference_events": reference_event_count,
        "unique_sales_document_references": unique_reference_count,
    },
    "headline": {
        "document_type_counts": dict(sorted(Counter(str(row["document_type"]) for row in business_rows).items())),
        "kpi_observation_count": len(kpi_inventory),
        "matched_sales_header_unique_references": len(matched_header_ids),
        "matched_sales_item_unique_references": len(matched_item_ids),
        "matched_erp_unique_references": len(matched_erp_ids),
        "matched_supporting_order_unique_references": len(matched_supporting_ids),
        "direct_disagreements": direct_disagreements,
    },
    "kpi_inventory": kpi_inventory,
    "internal_checks": internal_checks,
    "document_period_checks": document_period_checks,
    "duplicate_document_ids": duplicate_document_ids,
    "unchanged_type_snapshots": unchanged_type_snapshots,
    "currency_summary": currency_summary,
    "reconciled_invoice_references": reconciled_references,
    "relationships": list(relationships),
    "limitations": [
        "The overdue-invoice reports expose SALESDOCUMENT, SOLDTOPARTY, CREATIONDATE, and NETAMOUNT but no explicit invoice number, due date, settlement status, or currency.",
        "The selected sales header/item/ERP tables contain no amount field comparable to NETAMOUNT, so cross-source amount agreement is not testable.",
        "A numerical total across mixed transaction currencies has no valid monetary unit unless an exchange-rate and conversion-date policy is supplied.",
        "Exact source-local keys support these joins; no canonical cross-system identity conclusion is inferred.",
        "Repeated synthetic weekly snapshots are descriptive test data and do not establish a real-world trend.",
    ],
}

write_json("profile_projection.json", {
    "business_profile": {
        "artifact_id": business_profile.artifact_id,
        "artifact_type": business_profile.artifact_type,
        "schema_version": business_profile.schema_version,
        "requirement_id": business_profile.requirement_id,
        "content_hash": business_profile.content_hash,
    },
    "supporting_profile": {
        "artifact_id": supporting_profile.artifact_id,
        "artifact_type": supporting_profile.artifact_type,
        "schema_version": supporting_profile.schema_version,
        "requirement_id": supporting_profile.requirement_id,
        "content_hash": supporting_profile.content_hash,
    },
})
write_json("reconciliation.json", result)
