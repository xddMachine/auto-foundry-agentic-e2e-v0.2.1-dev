#!/usr/bin/env python3
"""Render a reviewed widget fixture as a deterministic offline dashboard.

The renderer is deliberately a presentation helper.  It reads values and
labels already present in the reviewed fixture and never queries a source or
derives an analytical metric.  All visual forms are small HTML/CSS forms so a
generated page works without JavaScript, a CDN, or an internet connection.

Example::

    python3 dashboard_renderer.py \
      --input reviewed_widgets.json \
      --output dashboard.html \
      --manifest-output dashboard_manifest.json

The fixture contract is documented in ``skills/auto-foundry-agentic-e2e/README.md``.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

try:
    from auto_foundry_core.product_contracts import validate_product_manifest
    from auto_foundry_core.workspace import AllowedRootError, RunContext
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    # The skill is installable independently of the source checkout.  When a
    # developer invokes this script directly from the repository, make the
    # committed local core importable without changing the caller's paths.
    _SRC = Path(__file__).resolve().parents[3] / "src"
    if str(_SRC) not in sys.path:
        sys.path.insert(0, str(_SRC))
    from auto_foundry_core.product_contracts import validate_product_manifest
    from auto_foundry_core.workspace import AllowedRootError, RunContext


SUPPORTED_WIDGETS = {
    "kpi",
    "bar",
    "line",
    "stacked_composition",
    "heatmap",
    "scatter",
    "donut",
    "table",
}


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _slug(value: Any) -> str:
    raw = _text(value, "trace")
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip("-")
    return slug or "trace"


def _escape(value: Any, default: str = "") -> str:
    return html.escape(_text(value, default), quote=True)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _trace_records(widget: Mapping[str, Any]) -> list[dict[str, str]]:
    """Normalize trace references without changing their reviewed meaning."""

    raw = widget.get("trace_refs")
    if not _as_list(raw):
        raw = widget.get("trace_ref")
    if not _as_list(raw):
        raw = widget.get("evidence_refs")
    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in _as_list(raw):
        if isinstance(value, Mapping):
            ref = _text(value.get("id") or value.get("trace_id") or value.get("anchor") or value.get("href") or value.get("ref") or value.get("path"))
            label = _text(value.get("label") or value.get("title") or ref)
            href = _text(value.get("href"))
        else:
            ref, label, href = _text(value), _text(value), ""
        if not ref and not href:
            continue
        if href.startswith("#"):
            anchor = href[1:]
            if not anchor:
                raise ValueError("trace reference cannot use an empty fragment")
        else:
            # A reviewed evidence reference is represented by a stable local
            # anchor.  The file/path remains visible as the link label.
            anchor = "trace-" + _slug(ref or href)
        if not anchor.startswith("trace-"):
            anchor = "trace-" + _slug(anchor)
        if anchor not in seen:
            records.append({"anchor": anchor, "label": label or ref or href, "ref": ref or href})
            seen.add(anchor)
    if not records:
        widget_id = _text(widget.get("id") or widget.get("title"), "widget")
        raise ValueError(f"widget {widget_id} has no usable evidence or trace provenance")
    return records


def _reference_values(value: Any) -> list[str]:
    """Return non-empty reviewed references without inventing placeholders."""

    values: list[str] = []
    for item in _as_list(value):
        if isinstance(item, Mapping):
            candidate = item.get("id") or item.get("trace_id") or item.get("anchor") or item.get("href") or item.get("ref") or item.get("path")
        else:
            candidate = item
        if candidate is None or not _text(candidate).strip():
            continue
        values.append(_text(candidate).strip())
    return values


def _widget_trace_refs(widget: Mapping[str, Any]) -> list[str]:
    refs = _reference_values(widget.get("trace_refs"))
    if not refs:
        refs = _reference_values(widget.get("trace_ref"))
    return refs


def _validate_widget_provenance(widget: Mapping[str, Any]) -> None:
    widget_id = _text(widget.get("id"), "widget")
    reviewed_item_ref = widget.get("reviewed_item_ref")
    reviewed_output_ref = widget.get("reviewed_output_ref")
    if not _reference_values(reviewed_item_ref):
        raise ValueError(f"widget {widget_id} requires reviewed_item_ref")
    if not _reference_values(reviewed_output_ref):
        raise ValueError(f"widget {widget_id} requires reviewed_output_ref")
    evidence_refs = _reference_values(widget.get("evidence_refs"))
    trace_refs = _reference_values(widget.get("trace_refs"))
    if not trace_refs:
        trace_refs = _reference_values(widget.get("trace_ref"))
    if not evidence_refs and not trace_refs:
        raise ValueError(f"widget {widget_id} requires evidence_refs or trace_refs")


def _display_value(value: Any) -> str:
    """Display a supplied value verbatim; no number parsing or formatting."""

    return _text(value, "—")


def _meta_lines(widget: Mapping[str, Any]) -> str:
    fields = (
        ("Period", widget.get("period")),
        ("Population", widget.get("population")),
        ("Unit", widget.get("unit")),
        ("Proxy / limit", widget.get("proxy_or_limit") or widget.get("limit")),
    )
    rows = [
        f'<dt>{html.escape(label)}</dt><dd>{_escape(value)}</dd>'
        for label, value in fields
        if value is not None and _text(value) != ""
    ]
    assumptions = _as_list(widget.get("assumptions"))
    limitations = _as_list(widget.get("limitations"))
    if assumptions:
        rows.append(f'<dt>Assumptions</dt><dd>{_escape("; ".join(_text(v) for v in assumptions))}</dd>')
    if limitations:
        rows.append(f'<dt>Limitations</dt><dd>{_escape("; ".join(_text(v) for v in limitations))}</dd>')
    return f'<dl class="widget-meta">{"".join(rows)}</dl>' if rows else ""


def _trace_links(records: Iterable[Mapping[str, str]]) -> str:
    links = [
        f'<a class="trace-link" href="#{_escape(record["anchor"])}">{_escape(record["label"])}</a>'
        for record in records
    ]
    return '<div class="trace-links"><span>Trace:</span> ' + ", ".join(links) + "</div>"


def _rows(value: Any) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    for row in _as_list(value):
        if isinstance(row, Mapping):
            result.append(row)
        else:
            result.append({"label": row, "value": row})
    return result


def _render_kpi(widget: Mapping[str, Any]) -> str:
    value = widget.get("value")
    if value is None:
        value = widget.get("display_value")
    return f'<div class="kpi-value">{_escape(_display_value(value))}</div>'


def _render_bar(widget: Mapping[str, Any]) -> str:
    rows = _rows(widget.get("bars") or widget.get("values") or widget.get("data"))
    rendered = []
    for row in rows:
        supplied_size = row.get("size") or row.get("width") or row.get("share") or row.get("percent")
        style = f' style="--bar-size:{_escape(supplied_size)}"' if supplied_size is not None else ""
        rendered.append(
            '<div class="viz-row"><span class="viz-label">{label}</span>'
            '<span class="viz-track"><span class="viz-bar"{style}></span></span>'
            '<span class="viz-value">{value}</span></div>'.format(
                label=_escape(row.get("label") or row.get("name")),
                style=style,
                value=_escape(_display_value(row.get("display_value", row.get("value")))),
            )
        )
    return '<div class="viz viz-bar-list">' + "".join(rendered) + "</div>"


def _render_line(widget: Mapping[str, Any]) -> str:
    rows = _rows(widget.get("points") or widget.get("series") or widget.get("values") or widget.get("data"))
    rendered = []
    for row in rows:
        label = row.get("label") or row.get("x") or row.get("period") or row.get("name")
        value = row.get("display_value", row.get("y", row.get("value")))
        rendered.append(f'<li><span>{_escape(label)}</span><strong>{_escape(_display_value(value))}</strong></li>')
    return '<ol class="viz viz-line-list">' + "".join(rendered) + "</ol>"


def _render_stacked(widget: Mapping[str, Any]) -> str:
    rows = _rows(widget.get("segments") or widget.get("values") or widget.get("data"))
    rendered = []
    for row in rows:
        supplied_size = row.get("size") or row.get("share") or row.get("percent")
        style = f' style="--segment-size:{_escape(supplied_size)}"' if supplied_size is not None else ""
        rendered.append(
            '<span class="stack-segment"{style} title="{label}">{label}: {value}</span>'.format(
                style=style,
                label=_escape(row.get("label") or row.get("name")),
                value=_escape(_display_value(row.get("display_value", row.get("value")))),
            )
        )
    return '<div class="viz viz-stacked">' + "".join(rendered) + '</div>'


def _render_heatmap(widget: Mapping[str, Any]) -> str:
    rows = _rows(widget.get("cells") or widget.get("values") or widget.get("data"))
    rendered = []
    for row in rows:
        intensity = row.get("intensity") or row.get("level") or row.get("class")
        classes = "heat-cell" + (" " + _slug(intensity) if intensity else "")
        rendered.append(
            f'<span class="{_escape(classes)}" title="{_escape(row.get("label") or row.get("name"))}">'
            f'{_escape(_display_value(row.get("display_value", row.get("value"))))}</span>'
        )
    return '<div class="viz viz-heatmap">' + "".join(rendered) + "</div>"


def _render_scatter(widget: Mapping[str, Any]) -> str:
    rows = _rows(widget.get("points") or widget.get("data"))
    rendered = []
    for row in rows:
        rendered.append(
            '<li><span class="scatter-dot" aria-hidden="true"></span>'
            '<span>{label}</span><span>x={x}; y={y}</span></li>'.format(
                label=_escape(row.get("label") or row.get("name")),
                x=_escape(_display_value(row.get("x"))),
                y=_escape(_display_value(row.get("y"))),
            )
        )
    return '<ul class="viz viz-scatter">' + "".join(rendered) + "</ul>"


def _render_donut(widget: Mapping[str, Any]) -> str:
    rows = _rows(widget.get("categories") or widget.get("values") or widget.get("data"))
    rendered = []
    for row in rows:
        supplied_size = row.get("share") or row.get("percent") or row.get("size")
        rendered.append(
            '<li><span class="donut-key" aria-hidden="true"></span>{label}: {value}</li>'.format(
                label=_escape(row.get("label") or row.get("name")),
                value=_escape(_display_value(row.get("display_value", row.get("value", supplied_size)))),
            )
        )
    return '<ul class="viz viz-donut" data-supplied="true">' + "".join(rendered) + "</ul>"


def _render_table(widget: Mapping[str, Any]) -> str:
    rows = _rows(widget.get("rows") or widget.get("data"))
    columns = _as_list(widget.get("columns"))
    if not columns and rows:
        columns = list(rows[0].keys())
    columns = [_text(column) for column in columns]
    head = "".join(f"<th>{_escape(column)}</th>" for column in columns)
    body = []
    for row in rows:
        body.append("<tr>" + "".join(f"<td>{_escape(row.get(column))}</td>" for column in columns) + "</tr>")
    return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


def _render_visual(widget: Mapping[str, Any]) -> str:
    kind = _text(widget.get("type") or widget.get("kind"), "kpi").lower()
    if kind == "kpi":
        return _render_kpi(widget)
    if kind == "bar":
        return _render_bar(widget)
    if kind == "line":
        return _render_line(widget)
    if kind == "stacked_composition":
        return _render_stacked(widget)
    if kind == "heatmap":
        return _render_heatmap(widget)
    if kind == "scatter":
        return _render_scatter(widget)
    if kind == "donut":
        return _render_donut(widget)
    if kind == "table":
        return _render_table(widget)
    raise ValueError(f"unsupported widget type: {kind}")


def _ordered_positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} requires a positive integer order")
    return value


def _normalize_domains(fixture: Mapping[str, Any], widgets: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    supplied = fixture.get("domains")
    if not isinstance(supplied, list) or not supplied:
        raise ValueError("fixture requires non-empty ordered domains metadata")
    widget_ids = {_text(widget.get("id")) for widget in widgets}
    seen_widget_ids: dict[str, tuple[str, str]] = {}
    domains: list[dict[str, Any]] = []
    domain_ids: set[str] = set()
    domain_orders: list[int] = []
    for domain in supplied:
        if not isinstance(domain, Mapping):
            raise ValueError("each domain must be an object")
        domain_id = _text(domain.get("id"))
        if not domain_id or domain_id in domain_ids:
            raise ValueError("domains require unique non-empty ids")
        if "decision_flows" in domain:
            raise ValueError(f"domain {domain_id or '<unknown>'} must use singular decision_flow")
        domain_ids.add(domain_id)
        domain_order = _ordered_positive_int(domain.get("order"), f"domain {domain_id}")
        domain_orders.append(domain_order)
        title = _text(domain.get("title"))
        if not title:
            raise ValueError(f"domain {domain_id} requires a title")
        decisions = domain.get("decision_flow")
        if not isinstance(decisions, list) or not decisions:
            raise ValueError(f"domain {domain_id} requires non-empty decision_flow metadata")
        flow_ids: set[str] = set()
        flow_orders: list[int] = []
        normalized_decisions: list[dict[str, Any]] = []
        for flow in decisions:
            if not isinstance(flow, Mapping):
                raise ValueError(f"domain {domain_id} has invalid decision_flow entry")
            if "widgets" in flow:
                raise ValueError(f"decision flow {domain_id} requires widget_ids")
            flow_id = _text(flow.get("id"))
            if not flow_id or flow_id in flow_ids:
                raise ValueError(f"domain {domain_id} requires unique decision-flow ids")
            flow_ids.add(flow_id)
            flow_order = _ordered_positive_int(flow.get("order"), f"decision flow {domain_id}/{flow_id}")
            flow_orders.append(flow_order)
            flow_title = _text(flow.get("title"))
            if not flow_title:
                raise ValueError(f"decision flow {domain_id}/{flow_id} requires a title")
            assigned = flow.get("widget_ids")
            if not isinstance(assigned, list) or not assigned or any(not isinstance(item, str) or not item.strip() for item in assigned):
                raise ValueError(f"decision flow {domain_id}/{flow_id} requires non-empty widget_ids")
            for widget_id in assigned:
                if widget_id not in widget_ids:
                    raise ValueError(f"decision flow {domain_id}/{flow_id} references unknown widget {widget_id}")
                if widget_id in seen_widget_ids:
                    raise ValueError(f"widget {widget_id} is assigned more than once")
                seen_widget_ids[widget_id] = (domain_id, flow_id)
            normalized_decisions.append({**flow, "id": flow_id, "order": flow_order, "title": flow_title, "widget_ids": list(assigned)})
        if sorted(flow_orders) != list(range(1, len(flow_orders) + 1)):
            raise ValueError(f"domain {domain_id} decision-flow orders must be contiguous from 1")
        domains.append({**domain, "id": domain_id, "order": domain_order, "title": title, "decision_flow": sorted(normalized_decisions, key=lambda flow: flow["order"])})
    if sorted(domain_orders) != list(range(1, len(domain_orders) + 1)):
        raise ValueError("domain orders must be contiguous from 1")
    if set(seen_widget_ids) != widget_ids:
        missing = sorted(widget_ids - set(seen_widget_ids))
        raise ValueError(f"widgets require valid domain/decision-flow assignment: {missing}")
    for widget in widgets:
        widget_id = _text(widget.get("id"))
        domain_id, flow_id = seen_widget_ids[widget_id]
        if "domain_id" in widget and _text(widget.get("domain_id")) != domain_id:
            raise ValueError(f"widget {widget_id} has unknown or mismatched domain assignment")
        if "decision_flow_id" in widget and _text(widget.get("decision_flow_id")) != flow_id:
            raise ValueError(f"widget {widget_id} has unknown or mismatched decision-flow assignment")
    return sorted(domains, key=lambda domain: domain["order"])


def _ordered_widgets(widgets: list[Mapping[str, Any]], domains: list[Mapping[str, Any]]) -> list[tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]]:
    by_id = {_text(widget.get("id")): widget for widget in widgets}
    emitted: set[str] = set()
    output: list[tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]] = []
    for domain in domains:
        for flow in _as_list(domain.get("decision_flow")):
            if not isinstance(flow, Mapping):
                continue
            ids = flow.get("widget_ids")
            for widget_id in ids:
                key = _text(widget_id.get("id") if isinstance(widget_id, Mapping) else widget_id)
                widget = by_id.get(key)
                if widget is not None and key not in emitted:
                    output.append((domain, flow, widget))
                    emitted.add(key)
    if len(emitted) != len(widgets):
        raise ValueError("every widget requires a valid ordered domain/decision-flow assignment")
    return output


def _validate_links(document: str) -> None:
    anchors = set(re.findall(r'\bid=["\']([^"\']+)["\']', document))
    links = re.findall(r'href=["\']([^"\']+)["\']', document)
    for href in links:
        if href.startswith("#"):
            if href[1:] not in anchors:
                raise ValueError(f"broken internal dashboard link: {href}")
        elif href.startswith(("http://", "https://", "//")):
            raise ValueError(f"offline dashboard cannot contain external link: {href}")


def render_dashboard(fixture: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """Return ``(html, manifest)`` for a reviewed fixture."""

    freeze_markers = validate_product_manifest(fixture)
    raw_widgets = fixture.get("widgets") or fixture.get("items")
    if not isinstance(raw_widgets, list) or not raw_widgets:
        raise ValueError("fixture must contain a non-empty widgets list")
    widgets: list[Mapping[str, Any]] = []
    for widget in raw_widgets:
        if not isinstance(widget, Mapping):
            raise ValueError("each widget must be an object")
        kind = _text(widget.get("type") or widget.get("kind"), "kpi").lower()
        if kind not in SUPPORTED_WIDGETS:
            raise ValueError(f"unsupported widget type: {kind}")
        if not widget.get("id"):
            raise ValueError("every widget requires a stable id")
        _validate_widget_provenance(widget)
        widgets.append(widget)
    domains = _normalize_domains(fixture, widgets)
    ordered = _ordered_widgets(widgets, domains)

    trace_records: list[dict[str, str]] = []
    trace_seen: set[str] = set()
    cards: list[str] = []
    sections: list[str] = []
    manifest_items: list[dict[str, Any]] = []
    for domain, flow, widget in ordered:
        widget_id = _slug(widget.get("id"))
        kind = _text(widget.get("type") or widget.get("kind"), "kpi").lower()
        records = _trace_records(widget)
        for record in records:
            if record["anchor"] not in trace_seen:
                trace_records.append(record)
                trace_seen.add(record["anchor"])
        trace = _trace_links(records)
        title = _text(widget.get("title") or widget.get("label"), widget_id)
        content = _render_visual(widget)
        meta = _meta_lines(widget)
        review_status = widget.get("review_status") or fixture.get("review_status")
        review = f'<p class="review-status">Review: {_escape(review_status)}</p>' if review_status else ""
        block = (
            f'<article class="widget widget-{_escape(kind)}" id="widget-{_escape(widget_id)}">'
            f'<h3>{_escape(title)}</h3>{content}{meta}{review}{trace}</article>'
        )
        if kind == "kpi":
            cards.append(block)
        else:
            sections.append(block)
        manifest_items.append(
            {
                "element_id": f"widget-{widget_id}",
                "kind": "kpi" if kind == "kpi" else kind,
                "title": title,
                "reviewed_item_ref": _text(widget.get("reviewed_item_ref")),
                "reviewed_output_ref": _text(widget.get("reviewed_output_ref")),
                "evidence_refs": _reference_values(widget.get("evidence_refs")),
                "trace_refs": _widget_trace_refs(widget),
                "trace_anchors": [record["anchor"] for record in records],
                "period": _text(widget.get("period")),
                "population": _text(widget.get("population")),
                "unit": _text(widget.get("unit")),
                "proxy_or_limit": _text(widget.get("proxy_or_limit") or widget.get("limit")),
            }
        )

    domain_blocks = []
    for domain in domains:
        domain_id = _slug(domain.get("id") or domain.get("title"))
        flow_blocks = []
        for flow in _as_list(domain.get("decision_flow")):
            if not isinstance(flow, Mapping):
                continue
            wanted = {_text(v.get("id") if isinstance(v, Mapping) else v) for v in _as_list(flow.get("widget_ids"))}
            # Use the already ordered blocks by their explicit IDs.  The
            # marker keeps domain/decision-flow order from the reviewed input.
            selected = []
            for _, _, widget in ordered:
                if _text(widget.get("id")) in wanted or (_text(widget.get("domain_id")) == _text(domain.get("id")) and not wanted):
                    selected.append(f'<a class="widget-jump" href="#widget-{_escape(_slug(widget.get("id")))}">{_escape(widget.get("title") or widget.get("id"))}</a>')
            flow_blocks.append(
                f'<div class="decision-flow"><h3>{_escape(flow.get("title") or flow.get("id"), "Decision view")}</h3>'
                f'<div class="flow-links">{"".join(selected)}</div></div>'
            )
        domain_blocks.append(
            f'<section class="domain" id="domain-{_escape(domain_id)}"><h2>{_escape(domain.get("title") or domain_id)}</h2>{"".join(flow_blocks)}</section>'
        )

    limitations = _as_list(fixture.get("limitations"))
    limitations_block = "".join(f"<li>{_escape(value)}</li>" for value in limitations)
    if not limitations_block:
        limitations_block = "<li>Only reviewed values supplied in the fixture are shown; no new analytics were calculated.</li>"
    trace_block = "".join(
        f'<li id="{_escape(record["anchor"])}"><strong>{_escape(record["label"])}</strong>'
        f'<span class="trace-ref">{_escape(record["ref"])}</span></li>'
        for record in trace_records
    )
    css = """
    :root{color-scheme:light;--ink:#18222d;--muted:#536170;--line:#d9e1e8;--accent:#146c94;--panel:#fff;--soft:#f2f6f8}
    *{box-sizing:border-box}body{margin:0;background:var(--soft);color:var(--ink);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
    main{max-width:1180px;margin:0 auto;padding:24px}header{background:var(--panel);border:1px solid var(--line);padding:20px;border-radius:12px}
    h1,h2,h3{margin:0 0 8px}h1{font-size:1.8rem}h2{margin-top:26px;font-size:1.25rem}h3{font-size:1rem}.eyebrow{color:var(--muted);text-transform:uppercase;letter-spacing:.08em;font-size:.72rem}
    .kpi-grid,.widget-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px;margin-top:14px}.widget{background:var(--panel);border:1px solid var(--line);padding:16px;border-radius:10px;min-width:0}
    .kpi-value{font-size:2rem;font-weight:700;color:var(--accent);margin:10px 0}.widget-meta{display:grid;grid-template-columns:max-content 1fr;gap:2px 10px;color:var(--muted);font-size:.82rem}.widget-meta dt{font-weight:600}.widget-meta dd{margin:0}
    .trace-links{margin-top:12px;border-top:1px solid var(--line);padding-top:8px;font-size:.78rem}.trace-links span{color:var(--muted)}.trace-link,.widget-jump{color:var(--accent);margin-right:8px}
    .review-status{font-size:.78rem;color:#7c4d00;background:#fff6df;border-radius:5px;padding:5px 7px;display:inline-block}.viz{margin:10px 0}.viz-row{display:grid;grid-template-columns:minmax(80px,1fr) 2fr max-content;gap:7px;align-items:center;margin:6px 0}.viz-track{height:9px;border-radius:9px;background:#e4ebef;overflow:hidden}.viz-bar{display:block;height:100%;width:var(--bar-size,0%);background:var(--accent)}.viz-value{font-variant-numeric:tabular-nums}.viz-line-list,.viz-scatter,.viz-donut{padding-left:20px}.viz-line-list li,.viz-scatter li,.viz-donut li{display:flex;gap:12px;justify-content:space-between;border-bottom:1px solid var(--line);padding:5px 0}.viz-stacked{display:flex;min-height:28px;border-radius:5px;overflow:hidden;background:#e4ebef}.stack-segment{width:var(--segment-size,auto);padding:5px 8px;border-right:1px solid var(--panel);background:#77b6c9;white-space:nowrap}.stack-segment:nth-child(2n){background:#e2a65e}.viz-heatmap{display:grid;grid-template-columns:repeat(auto-fit,minmax(64px,1fr));gap:4px}.heat-cell{padding:15px 5px;background:#d9edf2;text-align:center;border-radius:3px}.heat-cell.high,.heat-cell.critical{background:#dc8c78;color:#fff}.heat-cell.medium{background:#f2ce8f}.scatter-dot,.donut-key{display:inline-block;width:11px;height:11px;border-radius:50%;background:var(--accent);flex:none}.table-wrap{overflow:auto}.table-wrap table{border-collapse:collapse;width:100%;font-size:.84rem}.table-wrap th,.table-wrap td{padding:6px;border:1px solid var(--line);text-align:left}.table-wrap th{background:#edf3f6}.limitations{margin-top:24px;background:#fff8e7;border:1px solid #eed9a8;padding:14px;border-radius:10px}.trace-panel{margin-top:24px;background:var(--panel);border:1px solid var(--line);padding:16px;border-radius:10px}.trace-panel ul{padding-left:20px}.trace-ref{color:var(--muted);font-size:.8rem;margin-left:8px}.decision-flow{margin:8px 0 10px}.flow-links{display:flex;flex-wrap:wrap;gap:5px}.widget-jump{border:1px solid #b7d3df;border-radius:99px;padding:3px 9px;text-decoration:none;font-size:.8rem}
    @media(max-width:640px){main{padding:12px}.viz-row{grid-template-columns:1fr max-content}.viz-track{grid-column:1/-1}.viz-value{grid-column:2;grid-row:1}}
    """
    title = _text(fixture.get("title"), "Reviewed offline dashboard")
    subtitle = _text(fixture.get("subtitle"), "Static presentation of already-reviewed widget specifications")
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{_escape(title)}</title><style>{css}</style></head>
<body><main><header><p class="eyebrow">Offline · reviewed outputs only</p><h1>{_escape(title)}</h1><p>{_escape(subtitle)}</p><p>No external assets, JavaScript, CDN, source reads, or new calculations are used by this renderer.</p></header>
<section aria-labelledby="kpi-heading"><h2 id="kpi-heading">Key indicators</h2><div class="kpi-grid">{"".join(cards) or '<p>No KPI cards were supplied.</p>'}</div></section>
<section aria-labelledby="flow-heading"><h2 id="flow-heading">Decision flow</h2>{"".join(domain_blocks)}</section>
<section aria-labelledby="detail-heading"><h2 id="detail-heading">Reviewed visual details</h2><div class="widget-grid">{"".join(sections) or '<p>No non-KPI widgets were supplied.</p>'}</div></section>
<section class="limitations" aria-labelledby="limits-heading"><h2 id="limits-heading">Assumptions and limitations</h2><ul>{limitations_block}</ul></section>
<section class="trace-panel" aria-labelledby="trace-heading"><h2 id="trace-heading">Audit and trace references</h2><ul>{trace_block}</ul></section>
</main></body></html>"""
    _validate_links(document)
    manifest = {
        "product_type": "offline_static_dashboard",
        "source_status": "reviewed_outputs_only",
        "new_analytics": False,
        "organization": "business_domain_and_decision_flow",
        "assets_local": True,
        "internal_links_checked": True,
        "freeze_markers": freeze_markers.to_dict(),
        "run_id": _text(fixture.get("run_id")),
        "skill_version": _text(fixture.get("skill_version"), "0.2.7"),
        "domain_order": [domain["id"] for domain in domains],
        "decision_flow_order": [
            {"domain_id": domain["id"], "flow_id": flow["id"]}
            for domain in domains
            for flow in domain["decision_flow"]
        ],
        "items": manifest_items,
        "limitations": [_text(value) for value in limitations],
    }
    return document, manifest


def render_fixture(
    context: RunContext,
    fixture_path: str | Path,
    output_path: str | Path,
    manifest_path: str | Path | None = None,
) -> dict[str, Any]:
    """Render one reviewed fixture through one bounded :class:`RunContext`.

    ``fixture_path`` is run-relative (the reviewed fixture is already a
    system-owned run artifact); output and manifest paths are products-relative.
    Every path is resolved before the fixture is probed/read or any directory
    is created.  The pure :func:`render_dashboard` function remains available
    for callers that already hold a validated in-memory fixture.
    """

    if not isinstance(context, RunContext):
        raise TypeError("render_fixture requires one RunContext")
    # Resolve all paths up front.  In particular, do not read the fixture
    # before a malicious output/manifest path has been rejected.
    resolved_fixture = context.resolve_run_path(fixture_path)
    resolved_product_root = context.resolve_product_path("")
    if resolved_product_root != context.run_root / "products":
        raise AllowedRootError("products root must be the current run's products directory")
    resolved_output = context.resolve_product_path(output_path)
    resolved_manifest = context.resolve_product_path(manifest_path) if manifest_path is not None else None
    if not resolved_fixture.is_file():
        raise FileNotFoundError(resolved_fixture)
    fixture = json.loads(resolved_fixture.read_text(encoding="utf-8"))
    if not isinstance(fixture, Mapping):
        raise ValueError("dashboard fixture root must be a JSON object")
    document, manifest = render_dashboard(fixture)
    resolved_output.parent.mkdir(parents=True, exist_ok=True)
    resolved_output.write_text(document.rstrip() + "\n", encoding="utf-8")
    if resolved_manifest is not None:
        resolved_manifest.parent.mkdir(parents=True, exist_ok=True)
        resolved_manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True, help="current run root")
    parser.add_argument("--run-id", required=True, help="simple current run identifier")
    parser.add_argument("--input", "--fixture", dest="fixture_path", required=True, help="run-relative reviewed widget fixture JSON")
    parser.add_argument("--output", required=True, help="products-relative offline HTML output")
    parser.add_argument("--manifest-output", help="optional products-relative dashboard manifest JSON")
    args = parser.parse_args(argv)
    try:
        context = RunContext(run_id=args.run_id, run_root=args.run_root)
        manifest = render_fixture(context, args.fixture_path, args.output, args.manifest_output)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"dashboard renderer: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"output": str(context.resolve_product_path(args.output)), "internal_links_checked": manifest["internal_links_checked"], "widget_count": len(manifest["items"])}, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
