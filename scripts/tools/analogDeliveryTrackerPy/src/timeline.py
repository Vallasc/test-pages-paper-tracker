"""Dalle righe delle tabelle alle voci di timeline, raggruppate per destinatario."""

from collections import Counter

import constants as k
from data import FAR_FUTURE, parse_ts


def entry(kind, status_at, arrival_at, title, subtitle, badges, payload,
          event_id=None, log_code=None):
    return {
        "kind": kind,
        "status_at": status_at,
        "arrival_at": arrival_at or status_at,
        "title": title,
        "subtitle": subtitle,
        "badges": [b for b in badges if b],
        "payload": payload,
        "event_id": event_id,
        "retry": None,
        "tracking_id": None,
        # statusCode da cui partire per risalire al traceId nei log
        "log_code": log_code,
    }


def attachment_badges(attachments):
    """Un chip per tipo di documento allegato, col conto se sono più d'uno."""
    kinds = Counter(item.get("documentType") or "" for item in attachments or [])
    return [f"{'Demat' if count == 1 else f'{count} Demat'} {kind}".strip()
            for kind, count in kinds.items()]


def describe_status(status_code):
    """Descrizione, badge e tipo di voce di uno statusCode del catalogo.

    Un FINAL_EVENT è verde anche con esito KO: la spedizione si è conclusa, e
    un'irreperibilità o una mancata consegna sono esiti legittimi, non guasti.
    """
    known = k.STATUS_CODES.get(status_code)
    if not known:
        return "descrizione non disponibile nel catalogo", [], "event-unknown"
    description, event_type, product, outcome, _ = known
    badges = [event_type, f"prodotto {product}"]
    kind = "event-ok" if event_type == "FINAL_EVENT" else f"event-{outcome.lower()}"
    return description, badges, kind


def entries_for_tracking(tracking, errors, dry_runs, log_outputs, want_flow):
    """Voci di timeline generate da una singola riga PaperTrackings."""
    tracking_id = tracking.get("trackingId", "")
    result = []

    created_at = parse_ts(tracking.get("createdAt"))
    ocr = (tracking.get("validationConfig") or {}).get("ocrEnabled")
    result.append(entry(
        "milestone", created_at, created_at,
        "Tracking creato", tracking_id,
        [tracking.get("productType"), tracking.get("unifiedDeliveryDriver"),
         tracking.get("processingMode"), f"OCR {ocr}" if ocr else None],
        tracking))

    for event in tracking.get("events") or []:
        status_code = event.get("statusCode") or "?"
        description, badges, kind = describe_status(status_code)
        own_description = event.get("statusDescription")
        subtitle = description
        if own_description and own_description.strip() and own_description != description:
            subtitle = f"{description} · {own_description}"
        badges = list(badges)
        if event.get("deliveryFailureCause"):
            badges.append(f"causa {event['deliveryFailureCause']}")
        if event.get("registeredLetterCode"):
            badges.append(f"codice oggetto {event['registeredLetterCode']}")
        badges.extend(attachment_badges(event.get("attachments")))
        if event.get("dryRun"):
            badges.append("dry-run")
        result.append(entry(
            kind,
            parse_ts(event.get("statusTimestamp")),
            parse_ts(event.get("createdAt")) or parse_ts(event.get("requestTimestamp")),
            status_code, subtitle, badges, event, event.get("id"),
            log_code=status_code))

    for error in errors:
        category = error.get("errorCategory") or error.get("category") or "?"
        details = error.get("details") or {}
        cause = details.get("cause")
        message = details.get("message")
        subtitle_bits = [k.ERROR_CATEGORIES.get(category, "categoria non nel catalogo")]
        if cause:
            subtitle_bits.append(f"{cause}: {k.ERROR_CAUSES.get(cause, 'causa non nel catalogo')}")
        if message:
            subtitle_bits.append(message)
        moment = parse_ts(error.get("created"))
        severity = (error.get("type") or "ERROR").upper()
        result.append(entry(
            "error" if severity == "ERROR" else "warning",
            moment, moment, category, " — ".join(subtitle_bits),
            [severity, error.get("flowThrow"),
             f"evento {error['eventThrow']}" if error.get("eventThrow") else None],
            error, log_code=error.get("eventThrow")))

    for output in dry_runs:
        status_code = output.get("statusCode") or "?"
        description, _, _ = describe_status(status_code)
        moment = parse_ts(output.get("statusDateTime")) or parse_ts(output.get("created"))
        result.append(entry(
            "dryrun", moment, parse_ts(output.get("created")),
            f"output → delivery-push: {status_code}",
            output.get("statusDescription") or description,
            ["dry-run", output.get("statusDetail"),
             f"causa {output['deliveryFailureCause']}" if output.get("deliveryFailureCause") else None]
            + attachment_badges(output.get("attachments")),
            output))

    for moment, output in log_outputs:
        code = output.get("statusDetail") or output.get("statusCode") or "?"
        description, _, _ = describe_status(code)
        result.append(entry(
            "output",
            parse_ts(output.get("statusDateTime")),
            moment,                     # quando il tracker l'ha scritto davvero
            f"output → delivery-push: {code}",
            output.get("statusDescription") or description,
            ["dai log", output.get("statusCode"),
             f"causa {output['deliveryFailureCause']}" if output.get("deliveryFailureCause") else None]
            + attachment_badges(output.get("attachments")),
            output))

    if want_flow:
        flow = tracking.get("validationFlow") or {}
        for field, label in k.FLOW_STEPS:
            moment = parse_ts(flow.get(field))
            if moment:
                result.append(entry("milestone", moment, moment, label, field, [], flow))

        for request in flow.get("ocrRequests") or []:
            asked = parse_ts(request.get("requestTimestamp"))
            if asked:
                result.append(entry(
                    "ocr", asked, asked, "Richiesta OCR",
                    request.get("documentType") or "",
                    [request.get("attachmentEventId")], request))
            answered = parse_ts(request.get("responseTimestamp"))
            if answered:
                status = request.get("responseStatus") or "?"
                result.append(entry(
                    "ocr", answered, answered, f"Risposta OCR: {status}",
                    request.get("documentType") or "",
                    [request.get("finalEventId")], request))

    for item in result:
        item["tracking_id"] = tracking_id
    return result


def mark_queue_retries(entries):
    """L'id di un evento è il messageId SQS: se si ripete, la coda ha riconsegnato."""
    totals = Counter(item["event_id"] for item in entries if item["event_id"])
    seen = Counter()
    for item in entries:
        event_id = item["event_id"]
        if event_id and totals[event_id] > 1:
            seen[event_id] += 1
            item["retry"] = (seen[event_id], totals[event_id])


def build_recipients(trackings, errors, dry_runs, log_outputs, want_flow, order_by):
    """Raggruppa per destinatario e ordina le voci cronologicamente."""
    grouped = {}
    for tracking in trackings:
        rec, attempt, pc_retry = k.parse_tracking_id(tracking.get("trackingId", ""))
        bucket = grouped.setdefault(
            rec, {"recindex": rec, "trackings": [], "entries": [], "tables": {}})
        bucket["trackings"].append((attempt or 0, pc_retry or 0, tracking))
        # i link a DynamoDB escono solo per le tabelle che hanno davvero risposto
        tracking_id = tracking.get("trackingId")
        bucket["tables"][tracking_id] = {
            "trackings": 1,
            "errors": len(errors.get(tracking_id) or []),
            "dry_run": len(dry_runs.get(tracking_id) or []),
        }
        bucket["entries"].extend(entries_for_tracking(
            tracking,
            errors.get(tracking.get("trackingId")) or [],
            dry_runs.get(tracking.get("trackingId")) or [],
            log_outputs.get(tracking.get("trackingId")) or [],
            want_flow))

    field = "status_at" if order_by == "status" else "arrival_at"
    other = "arrival_at" if field == "status_at" else "status_at"
    for bucket in grouped.values():
        bucket["trackings"].sort(key=lambda row: (row[0], row[1]))
        bucket["entries"].sort(key=lambda item: item[field] or item[other] or FAR_FUTURE)
        mark_queue_retries(bucket["entries"])
    return [grouped[rec] for rec in sorted(grouped, key=lambda r: (r is None, r))]
