from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timedelta
import json
import math
import os
from pathlib import Path
import re

import pandas as pd

from auto_foundry_core import BoundAnalysisContext
from auto_foundry_core.analysis import load_selected_source_ids
from auto_foundry_core.analytics_toolkit import profile_data


REQUIREMENT_ID = "REQ-001"
COMMUNICATIONS = "all_communications.parquet"
ERP = "erp_transactions.parquet"
SUPPORTING = "supporting_documents.parquet"
REQUIRED_SOURCE_IDS = {COMMUNICATIONS, ERP, SUPPORTING, "README.md", "business_docs.md"}


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=False),
        encoding="utf-8",
    )


def clean_id(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return None
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text


def parse_date(value: object) -> pd.Timestamp | None:
    if value is None:
        return None
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return pd.Timestamp(parsed).normalize()


def finite_ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def in_window(value: pd.Timestamp, start: pd.Timestamp, end: pd.Timestamp) -> bool:
    return start <= value <= end


def period_bounds(as_of: pd.Timestamp, days: int) -> tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp, pd.Timestamp]:
    recent_start = as_of - timedelta(days=days - 1)
    prior_end = recent_start - timedelta(days=1)
    prior_start = prior_end - timedelta(days=days - 1)
    return prior_start, prior_end, recent_start, as_of


def period_label(value: pd.Timestamp, bounds: tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp, pd.Timestamp]) -> str | None:
    prior_start, prior_end, recent_start, recent_end = bounds
    if in_window(value, prior_start, prior_end):
        return "prior"
    if in_window(value, recent_start, recent_end):
        return "recent"
    return None


def artifact_projection(artifact: object) -> dict[str, object]:
    return {
        "artifact_id": artifact.artifact_id,
        "artifact_type": artifact.artifact_type,
        "schema_version": artifact.schema_version,
        "requirement_id": artifact.requirement_id,
        "content_hash": artifact.content_hash,
    }


output_root = Path(os.environ["AUTO_FOUNDRY_ANALYSIS_OUTPUT_ROOT"])
analysis_phase = os.environ.get("AUTO_FOUNDRY_ANALYSIS_PHASE", "full")
is_smoke = analysis_phase == "smoke"
bound = BoundAnalysisContext.load(path=os.environ["AUTO_FOUNDRY_ANALYSIS_CONTEXT"])
selected_source_ids = load_selected_source_ids()
if not REQUIRED_SOURCE_IDS.issubset(set(selected_source_ids)):
    raise ValueError("REQ-001 selected-source binding is incomplete")

# The toolkit is used first for its supported descriptive role. These bounded
# profiles validate types/missingness/frequencies; they are not used to infer
# the watchlist and are explicitly labelled as bounded samples.
communications_rows = bound.data_room.read_rows(COMMUNICATIONS, limit=None)
communications_profile_frame = pd.DataFrame(
    communications_rows,
    columns=["message_id", "timestamp", "customer_id", "customer_name", "from_role", "subject", "triggered_by"],
)
communications_profile = profile_data(
    communications_profile_frame,
    requirement_id=REQUIREMENT_ID,
    period="full selected communications source",
    population={"description": "all rows from the selected communications source", "row_count": len(communications_profile_frame)},
    max_frequency_values=20,
)
write_json(output_root / "profile_communications.json", json.loads(communications_profile.to_json()))

supporting_rows = bound.data_room.read_rows(SUPPORTING, limit=None)
supporting_profile_frame = pd.DataFrame(
    supporting_rows,
    columns=["document_type", "document_id", "order_number", "customer_id", "customer_name", "order_date", "expected_delivery", "actual_delivery", "due_date"],
)
supporting_profile = profile_data(
    supporting_profile_frame,
    requirement_id=REQUIREMENT_ID,
    period="full selected supporting-document source",
    population={"description": "all rows from the selected supporting-document source", "row_count": len(supporting_profile_frame)},
    max_frequency_values=20,
)
write_json(output_root / "profile_supporting.json", json.loads(supporting_profile.to_json()))

# The ERP source is read once through the public DataRoom API because its
# physical grain is 1.9M line items and offset paging would repeatedly rescan
# prior Parquet batches. The first deterministic slice supplies a structural
# profile; the custom pass keeps only distinct sales-document/customer/date
# state needed by this unsupported composite method.
ERP_SMOKE_LIMIT = 250_000
erp_rows_processed = 0
erp_profile = None
document_owner: dict[str, tuple[str, pd.Timestamp]] = {}
ambiguous_documents: set[str] = set()
erp_min_date: pd.Timestamp | None = None
erp_max_date: pd.Timestamp | None = None
erp_customer_ids_all: set[str] = set()

while True:
    batch = bound.data_room.read_rows(ERP, limit=ERP_SMOKE_LIMIT if is_smoke else None)
    if not batch:
        raise ValueError("selected ERP source contains no usable rows")
    if erp_profile is None:
        profile_frame = pd.DataFrame(batch[:25_000], columns=["SALESDOCUMENT", "SOLDTOPARTY", "CREATIONDATE", "SALESDOCUMENTITEM", "SALESDOCUMENTTYPE"])
        erp_profile = profile_data(
            profile_frame,
            requirement_id=REQUIREMENT_ID,
            period="deterministic first 25,000 ERP line items; structural profile only",
            population={"description": "bounded structural sample, not used for watchlist estimates", "row_count": len(profile_frame)},
            max_frequency_values=20,
        )
        write_json(output_root / "profile_erp.json", json.loads(erp_profile.to_json()))
    for row in batch:
        document_id = clean_id(row.get("SALESDOCUMENT"))
        customer_id = clean_id(row.get("SOLDTOPARTY"))
        created = parse_date(row.get("CREATIONDATE"))
        if document_id is None or customer_id is None or created is None:
            continue
        erp_rows_processed += 1
        erp_customer_ids_all.add(customer_id)
        erp_min_date = created if erp_min_date is None else min(erp_min_date, created)
        erp_max_date = created if erp_max_date is None else max(erp_max_date, created)
        prior = document_owner.get(document_id)
        if prior is None:
            document_owner[document_id] = (customer_id, created)
        elif prior != (customer_id, created):
            ambiguous_documents.add(document_id)
    break

del batch

if erp_profile is None or erp_min_date is None or erp_max_date is None:
    raise ValueError("selected ERP source contains no usable order records")

profiles_projection = {
    "communications": artifact_projection(communications_profile),
    "erp": artifact_projection(erp_profile),
    "supporting": artifact_projection(supporting_profile),
}
write_json(output_root / "profiles_projection.json", profiles_projection)

complaint_re = re.compile(
    r"\bcomplain(?:t|ts|ed|ing)?\b|\bunacceptable\b|not what (?:we )?ordered|"
    r"do(?:es)? not (?:meet|match)|\bpoor quality\b|\bquality issue\b|\bdefect(?:ive|s)?\b|"
    r"\bdamag(?:e|ed)\b|\bwrong (?:item|items|product|products|order)\b|\bincorrect\b|"
    r"\breturn (?:this|these|the)\b|\bdissatisfied\b|\bdisappointed\b|\bescalat(?:e|ed|ion)\b|"
    r"\brefund\b|\bshortage\b|\bmissing (?:item|items|quantity|quantities)\b|\bovercharg(?:e|ed)\b",
    flags=re.IGNORECASE,
)
delay_re = re.compile(
    r"\blate (?:delivery|shipment|order|arrival)\b|\bdeliver(?:y|ies) (?:is |are )?late\b|"
    r"\bdelay(?:ed|s|ing)?\b|\bnot (?:yet )?(?:arrived|received|delivered)\b|"
    r"\bwhere is (?:our|the) (?:order|shipment|delivery)\b|\bmissed (?:the )?delivery\b|"
    r"\bbehind schedule\b|\bback[ -]?order(?:ed)?\b|\bexpedit(?:e|ed|ing)\b|\boverdue shipment\b",
    flags=re.IGNORECASE,
)
payment_re = re.compile(
    r"\bpayment reminder\b|\bpast due\b|\boverdue invoice\b|\bpayment (?:is )?overdue\b|"
    r"\boutstanding (?:balance|invoice|payment)\b|\breminder.{0,40}\binvoice\b|"
    r"\binvoice.{0,40}\boverdue\b|\bplease remit\b|\blate payment\b|\bunpaid invoice\b|"
    r"\bpayment due\b",
    flags=re.IGNORECASE,
)

communications_min_date: pd.Timestamp | None = None
communications_max_date: pd.Timestamp | None = None
communication_customer_ids_all: set[str] = set()
customer_names: dict[str, Counter[str]] = defaultdict(Counter)
communication_events: list[dict[str, object]] = []
seen_communication_events: set[tuple[str, ...]] = set()
matched_subjects: dict[str, Counter[str]] = {"complaint": Counter(), "delay": Counter(), "payment_reminder": Counter()}

for row in communications_rows:
    customer_id = clean_id(row.get("customer_id"))
    timestamp = parse_date(row.get("timestamp"))
    if customer_id is None or timestamp is None:
        continue
    subject = str(row.get("subject") or "").strip()
    body = str(row.get("body") or "").strip()
    triggered_by = str(row.get("triggered_by") or "").strip()
    event_key = (
        str(row.get("message_id") or ""),
        timestamp.date().isoformat(),
        customer_id,
        str(row.get("from") or ""),
        str(row.get("to") or ""),
        subject,
        body,
    )
    if event_key in seen_communication_events:
        continue
    seen_communication_events.add(event_key)
    communication_customer_ids_all.add(customer_id)
    name = str(row.get("customer_name") or "").strip()
    if name:
        customer_names[customer_id][name] += 1
    communications_min_date = timestamp if communications_min_date is None else min(communications_min_date, timestamp)
    communications_max_date = timestamp if communications_max_date is None else max(communications_max_date, timestamp)
    text = " ".join((subject, body, triggered_by))
    categories: list[str] = []
    if complaint_re.search(text):
        categories.append("complaint")
        matched_subjects["complaint"][subject or "(no subject)"] += 1
    if delay_re.search(text):
        categories.append("delay_communication")
        matched_subjects["delay"][subject or "(no subject)"] += 1
    if payment_re.search(text):
        categories.append("payment_reminder")
        matched_subjects["payment_reminder"][subject or "(no subject)"] += 1
    communication_events.append({"customer_id": customer_id, "date": timestamp, "categories": tuple(categories)})

if communications_min_date is None or communications_max_date is None:
    raise ValueError("selected communications source contains no usable dated customer records")

late_shipments: list[tuple[str, pd.Timestamp, str]] = []
supporting_customer_ids_all: set[str] = set()
for row in supporting_rows:
    customer_id = clean_id(row.get("customer_id"))
    if customer_id is None:
        continue
    supporting_customer_ids_all.add(customer_id)
    name = str(row.get("customer_name") or "").strip()
    if name:
        customer_names[customer_id][name] += 1
    if str(row.get("document_type") or "") != "SHIPPING_NOTICE":
        continue
    expected = parse_date(row.get("expected_delivery"))
    actual = parse_date(row.get("actual_delivery"))
    if expected is None or actual is None or actual <= expected:
        continue
    shipment_id = clean_id(row.get("shipment_number")) or clean_id(row.get("document_id"))
    if shipment_id is not None:
        late_shipments.append((customer_id, actual, shipment_id))

common_as_of = min(erp_max_date, communications_max_date)


def compute_window(days: int) -> dict[str, object]:
    bounds = period_bounds(common_as_of, days)
    prior_start, prior_end, recent_start, recent_end = bounds
    orders: dict[str, dict[str, set[str]]] = defaultdict(lambda: {"prior": set(), "recent": set()})
    for document_id, (customer_id, created) in document_owner.items():
        if document_id in ambiguous_documents:
            continue
        label = period_label(created, bounds)
        if label is not None:
            orders[customer_id][label].add(document_id)

    communication_counts: dict[str, dict[str, object]] = defaultdict(
        lambda: {
            "prior_adverse": set(), "recent_adverse": set(),
            "prior_complaint": 0, "recent_complaint": 0,
            "prior_delay_communication": 0, "recent_delay_communication": 0,
            "prior_payment_reminder": 0, "recent_payment_reminder": 0,
        }
    )
    for index, event in enumerate(communication_events):
        label = period_label(event["date"], bounds)
        categories = event["categories"]
        if label is None or not categories:
            continue
        customer_id = str(event["customer_id"])
        communication_counts[customer_id][f"{label}_adverse"].add(index)
        for category in categories:
            communication_counts[customer_id][f"{label}_{category}"] += 1

    shipment_counts: dict[str, dict[str, set[str]]] = defaultdict(lambda: {"prior": set(), "recent": set()})
    for customer_id, actual, shipment_id in late_shipments:
        label = period_label(actual, bounds)
        if label is not None:
            shipment_counts[customer_id][label].add(shipment_id)

    population = set(orders) | set(communication_counts) | set(shipment_counts)
    rows: list[dict[str, object]] = []
    eligible_population = 0
    decline_population = 0
    adverse_growth_population = 0
    for customer_id in population:
        prior_orders = len(orders[customer_id]["prior"])
        recent_orders = len(orders[customer_id]["recent"])
        prior_adverse = len(communication_counts[customer_id]["prior_adverse"])
        recent_adverse = len(communication_counts[customer_id]["recent_adverse"])
        prior_late_shipments = len(shipment_counts[customer_id]["prior"])
        recent_late_shipments = len(shipment_counts[customer_id]["recent"])
        if prior_orders > 0:
            eligible_population += 1
        order_decline = prior_orders > 0 and recent_orders < prior_orders
        if order_decline:
            decline_population += 1
        component_growth = {
            "complaints": int(communication_counts[customer_id]["recent_complaint"]) - int(communication_counts[customer_id]["prior_complaint"]),
            "delay_communications": int(communication_counts[customer_id]["recent_delay_communication"]) - int(communication_counts[customer_id]["prior_delay_communication"]),
            "payment_reminders": int(communication_counts[customer_id]["recent_payment_reminder"]) - int(communication_counts[customer_id]["prior_payment_reminder"]),
            "late_shipments": recent_late_shipments - prior_late_shipments,
        }
        growing_components = sorted(name for name, change in component_growth.items() if change > 0)
        adverse_growth = bool(growing_components)
        if adverse_growth:
            adverse_growth_population += 1
        if not (order_decline and adverse_growth):
            continue
        decline_pct = round((prior_orders - recent_orders) / prior_orders * 100.0, 1)
        positive_growth_points = sum(max(0, change) for change in component_growth.values())
        tier = "high" if decline_pct >= 25.0 and positive_growth_points >= 3 else "moderate"
        name = customer_names[customer_id].most_common(1)[0][0] if customer_names[customer_id] else None
        rows.append(
            {
                "customer_id": customer_id,
                "customer_name": name,
                "priority_tier": tier,
                "prior_orders": prior_orders,
                "recent_orders": recent_orders,
                "order_change": recent_orders - prior_orders,
                "order_decline_pct": decline_pct,
                "prior_adverse_communications": prior_adverse,
                "recent_adverse_communications": recent_adverse,
                "adverse_communication_change": recent_adverse - prior_adverse,
                "prior_complaints": int(communication_counts[customer_id]["prior_complaint"]),
                "recent_complaints": int(communication_counts[customer_id]["recent_complaint"]),
                "prior_delay_communications": int(communication_counts[customer_id]["prior_delay_communication"]),
                "recent_delay_communications": int(communication_counts[customer_id]["recent_delay_communication"]),
                "prior_payment_reminders": int(communication_counts[customer_id]["prior_payment_reminder"]),
                "recent_payment_reminders": int(communication_counts[customer_id]["recent_payment_reminder"]),
                "prior_late_shipments": prior_late_shipments,
                "recent_late_shipments": recent_late_shipments,
                "growing_components": growing_components,
                "positive_growth_points": positive_growth_points,
                "low_base_flag": prior_orders < 3,
            }
        )
    rows.sort(
        key=lambda value: (
            0 if value["priority_tier"] == "high" else 1,
            -float(value["order_decline_pct"]),
            -int(value["positive_growth_points"]),
            str(value["customer_id"]),
        )
    )
    return {
        "window_days": days,
        "prior_period": {"start": prior_start.date().isoformat(), "end": prior_end.date().isoformat()},
        "recent_period": {"start": recent_start.date().isoformat(), "end": recent_end.date().isoformat()},
        "eligible_customer_count_with_prior_orders": eligible_population,
        "order_decline_customer_count": decline_population,
        "adverse_component_growth_customer_count": adverse_growth_population,
        "watchlist_count": len(rows),
        "high_priority_count": sum(row["priority_tier"] == "high" for row in rows),
        "watchlist": rows,
    }


main = compute_window(180)
sensitivity = compute_window(90)
main_ids = {row["customer_id"] for row in main["watchlist"]}
sensitivity_ids = {row["customer_id"] for row in sensitivity["watchlist"]}
stable_ids = sorted(main_ids & sensitivity_ids)

communication_overlap = communication_customer_ids_all & erp_customer_ids_all
supporting_overlap = supporting_customer_ids_all & erp_customer_ids_all

analysis = {
    "schema_version": "req001_watchlist_analysis.v1",
    "requirement_id": REQUIREMENT_ID,
    "method": {
        "type": "descriptive two-signal watchlist",
        "order_grain": "distinct SALESDOCUMENT per SOLDTOPARTY per period",
        "main_window_days": 180,
        "sensitivity_window_days": 90,
        "as_of": common_as_of.date().isoformat(),
        "adverse_rule": "At least one of complaint communications, delay communications, payment-reminder communications, or observed late shipments increases from prior to recent equal-duration window.",
        "watchlist_rule": "prior_orders > 0 AND recent_orders < prior_orders AND at least one adverse component increases",
        "ranking": "high tier first, then larger order-decline percentage, then more positive adverse growth points; no predictive probability",
        "communication_classification": "transparent case-insensitive regular-expression rules over subject, body, and triggered_by; communications may receive multiple category flags",
    },
    "coverage": {
        "erp_line_rows_processed": erp_rows_processed,
        "distinct_erp_sales_documents": len(document_owner),
        "ambiguous_sales_documents_excluded": len(ambiguous_documents),
        "deduplicated_communication_events": len(communication_events),
        "communication_customer_count": len(communication_customer_ids_all),
        "erp_customer_count": len(erp_customer_ids_all),
        "exact_customer_id_overlap_count": len(communication_overlap),
        "communication_to_erp_customer_coverage": finite_ratio(len(communication_overlap), len(communication_customer_ids_all)),
        "erp_to_communication_customer_coverage": finite_ratio(len(communication_overlap), len(erp_customer_ids_all)),
        "supporting_customer_count": len(supporting_customer_ids_all),
        "supporting_to_erp_customer_coverage": finite_ratio(len(supporting_overlap), len(supporting_customer_ids_all)),
    },
    "source_periods": {
        "erp": {"min": erp_min_date.date().isoformat(), "max": erp_max_date.date().isoformat()},
        "communications": {"min": communications_min_date.date().isoformat(), "max": communications_max_date.date().isoformat()},
    },
    "matched_subject_examples": {
        category: [{"subject": subject, "event_count": count} for subject, count in values.most_common(10)]
        for category, values in matched_subjects.items()
    },
    "main_180_day_result": main,
    "sensitivity_90_day_result": {
        **{key: value for key, value in sensitivity.items() if key != "watchlist"},
        "watchlist_customer_ids": sorted(sensitivity_ids),
    },
    "sensitivity_overlap": {
        "main_watchlist_count": len(main_ids),
        "sensitivity_watchlist_count": len(sensitivity_ids),
        "intersection_count": len(stable_ids),
        "main_retained_in_90_day_share": finite_ratio(len(stable_ids), len(main_ids)),
        "stable_customer_ids": stable_ids,
    },
    "limitations": [
        "This is a descriptive watchlist, not a trained or validated churn model and not causal evidence.",
        "Communication categories are transparent keyword rules, not human-labelled outcomes; false positives and false negatives remain possible.",
        "A reused message_id is not treated as a unique event by itself; only exact event duplicates are removed because the dataset reuses template identifiers across dates and recipients.",
        "Supporting-document coverage is partial, so an absent late-shipment record is not interpreted as proof of on-time delivery.",
        "Order counts treat every distinct sales document as activity and do not infer cancellation or commercial value from fields not used here.",
        "The 90-day sensitivity result is a stability diagnostic, not an alternative probability estimate.",
    ],
}

write_json(output_root / "watchlist_analysis.json", analysis)
watchlist_projection = {
    "schema_version": analysis["schema_version"],
    "requirement_id": REQUIREMENT_ID,
    "method": analysis["method"],
    "coverage": analysis["coverage"],
    "source_periods": analysis["source_periods"],
    "main_180_day_result": main,
    "sensitivity_overlap": analysis["sensitivity_overlap"],
    "limitations": analysis["limitations"],
}
write_json(output_root / "watchlist_projection.json", watchlist_projection)

print(
    json.dumps(
        {
            "main_watchlist_count": main["watchlist_count"],
            "high_priority_count": main["high_priority_count"],
            "communication_to_erp_customer_coverage": analysis["coverage"]["communication_to_erp_customer_coverage"],
            "sensitivity_intersection_count": len(stable_ids),
        },
        sort_keys=True,
        allow_nan=False,
    )
)
