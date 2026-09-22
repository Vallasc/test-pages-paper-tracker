"""Storico di una spedizione analogica a partire dallo IUN.

Legge le tabelle DynamoDB di pn-paper-tracker e costruisce, per ogni
destinatario della notifica, un'unica timeline che unisce gli eventi ricevuti
dal consolidatore, gli errori generati dalle validazioni, le tappe del flusso e
gli output prodotti in dry-run.

I trackingId non sono indicizzati per IUN, quindi vengono ricostruiti a partire
dal prodotto scelto (vedi ``constants.TRACKING_SCHEMES``). Le parti che
precedono il PCRETRY formano l'``attemptId``, partition key dell'indice
``attemptId-pcRetry-index``: una query restituisce tutti i PCRETRY di quel
tentativo, senza dover indovinare anche l'ultimo pezzo.
"""

import asyncio
import json
import re
from collections import Counter
from datetime import datetime, timezone
from html import escape

from pyscript import document, when, window

import awsdir

# boto3 va importato con lo shim di Pyodide già applicato: anticiparlo qui fa
# fallire subito il caricamento della pagina, invece che alla prima ricerca.
awsdir.import_boto3()

import constants as k


# =============================================================================
# Utilità
# =============================================================================
def el(element_id):
    return document.getElementById(element_id)


def unwrap(value):
    """Da AttributeValue DynamoDB a tipo Python."""
    if not isinstance(value, dict) or len(value) != 1:
        return value
    kind, raw = next(iter(value.items()))
    if kind == "S":
        return raw
    if kind == "N":
        return float(raw) if ("." in raw or "e" in raw.lower()) else int(raw)
    if kind == "BOOL":
        return bool(raw)
    if kind == "NULL":
        return None
    if kind == "M":
        return {key: unwrap(item) for key, item in raw.items()}
    if kind == "L":
        return [unwrap(item) for item in raw]
    if kind in ("SS", "NS", "BS"):
        return list(raw)
    return raw


def unwrap_item(item):
    return {key: unwrap(value) for key, value in item.items()}


_FRACTION_RE = re.compile(r"\.(\d{1,9})")


def parse_ts(value):
    """ISO-8601 -> datetime aware. ``Instant`` può avere i nanosecondi."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    text = _FRACTION_RE.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), text, count=1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def fmt_ts(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if moment else "—"


FAR_FUTURE = datetime.max.replace(tzinfo=timezone.utc)


# =============================================================================
# Visualizzazione del JSON
# =============================================================================
def json_scalar(value):
    if value is None:
        return '<span class="text-fuchsia-700">null</span>'
    if isinstance(value, bool):
        return f'<span class="text-fuchsia-700">{str(value).lower()}</span>'
    if isinstance(value, (int, float)):
        return f'<span class="text-amber-700">{value}</span>'
    if not isinstance(value, str):
        value = str(value)
    return f'<span class="text-emerald-700">{escape(json.dumps(value, ensure_ascii=False))}</span>'


def json_node(value, depth=0, label=""):
    """Albero JSON: tutti i rami nascono aperti, richiudibili uno per uno."""
    prefix = (f'<span class="text-sky-800">{escape(json.dumps(label, ensure_ascii=False))}</span>'
              f'<span class="text-slate-400">: </span>') if label else ""

    if isinstance(value, dict):
        if not value:
            return f'<div>{prefix}<span class="text-slate-400">{{}}</span></div>'
        rows = "".join(json_node(item, depth + 1, key) for key, item in value.items())
        summary = f'{prefix}<span class="text-slate-400">{{ {len(value)} campi }}</span>'
    elif isinstance(value, list):
        if not value:
            return f'<div>{prefix}<span class="text-slate-400">[]</span></div>'
        rows = "".join(json_node(item, depth + 1, f"[{index}]")
                       for index, item in enumerate(value))
        summary = f'{prefix}<span class="text-slate-400">[ {len(value)} elementi ]</span>'
    else:
        return f'<div class="break-all">{prefix}{json_scalar(value)}</div>'

    return (f'<details open>'
            f'<summary class="cursor-pointer rounded hover:bg-slate-100">{summary}</summary>'
            f'<div class="ml-2 border-l border-slate-200 pl-3">{rows}</div>'
            f'</details>')


def json_viewer(payload, title="Dettaglio JSON"):
    return (f'<details class="mt-2">'
            f'<summary class="cursor-pointer text-xs font-medium text-brand hover:underline">'
            f'{escape(title)}</summary>'
            f'<div class="jsn mt-2 max-h-[32rem] overflow-auto rounded-md border border-slate-200 '
            f'bg-slate-50 p-3 font-mono text-[13px] leading-relaxed text-slate-800">'
            f'{json_node(payload)}</div></details>')


# =============================================================================
# Accesso a DynamoDB
# =============================================================================
def query_all(ddb, **kwargs):
    """Query paginata, già deserializzata."""
    items, start_key = [], None
    while True:
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        response = ddb.query(**kwargs)
        items.extend(response.get("Items", []))
        start_key = response.get("LastEvaluatedKey")
        if not start_key:
            break
    return [unwrap_item(item) for item in items]


def query_trackings(ddb, attempt_id):
    return query_all(
        ddb,
        TableName=k.TABLES["trackings"],
        IndexName=k.ATTEMPT_INDEX,
        KeyConditionExpression="attemptId = :a",
        ExpressionAttributeValues={":a": {"S": attempt_id}},
    )


def query_by_tracking_id(ddb, table, tracking_id):
    return query_all(
        ddb,
        TableName=table,
        KeyConditionExpression="trackingId = :t",
        ExpressionAttributeValues={":t": {"S": tracking_id}},
    )


async def scan_scheme(ddb, scheme, iun, max_rec, max_att, say):
    """Tutti i tracking dello IUN per uno schema di trackingId.

    Si ferma al primo RECINDEX senza risultati: i destinatari sono numerati in
    modo contiguo a partire da 0. Sui tentativi tollera un buco, perché un
    ATTEMPT può essere stato saltato senza che sia finita la spedizione.
    """
    found = []
    for rec in range(max_rec + 1):
        if not k.has_attempt(scheme):
            attempt_id = k.build_attempt_id(scheme, iun, rec)
            say(f"{scheme} · RECINDEX_{rec}")
            await asyncio.sleep(0)
            rows = query_trackings(ddb, attempt_id)
            if not rows:
                break
            found.extend(rows)
            continue

        found_for_rec, misses = False, 0
        for att in range(max_att + 1):
            say(f"{scheme} · RECINDEX_{rec} · ATTEMPT_{att}")
            await asyncio.sleep(0)
            rows = query_trackings(ddb, k.build_attempt_id(scheme, iun, rec, att))
            if rows:
                found.extend(rows)
                found_for_rec = True
                misses = 0
            else:
                misses += 1
                if misses >= 2:
                    break
        if not found_for_rec:
            break
    return found


async def collect(ddb, schemes, iun, max_rec, max_att, want_dry_run, say):
    """Tracking, errori e output dry-run che compongono lo storico dello IUN."""
    trackings = []
    for scheme in schemes:
        trackings.extend(await scan_scheme(ddb, scheme, iun, max_rec, max_att, say))

    errors, dry_runs = {}, {}
    for tracking in trackings:
        tracking_id = tracking.get("trackingId")
        say(f"Errori di {tracking_id}")
        await asyncio.sleep(0)
        errors[tracking_id] = query_by_tracking_id(ddb, k.TABLES["errors"], tracking_id)
        if want_dry_run:
            say(f"Output dry-run di {tracking_id}")
            await asyncio.sleep(0)
            dry_runs[tracking_id] = query_by_tracking_id(ddb, k.TABLES["dry_run"], tracking_id)

    return trackings, errors, dry_runs


# =============================================================================
# Costruzione della timeline
# =============================================================================
def entry(kind, status_at, arrival_at, title, subtitle, badges, payload, origin, event_id=None):
    return {
        "kind": kind,
        "status_at": status_at,
        "arrival_at": arrival_at or status_at,
        "title": title,
        "subtitle": subtitle,
        "badges": [b for b in badges if b],
        "payload": payload,
        "origin": origin,
        "event_id": event_id,
        "retry": None,
    }


def describe_status(status_code):
    known = k.STATUS_CODES.get(status_code)
    if not known:
        return "descrizione non disponibile nel catalogo", [], "unknown"
    description, event_type, product, outcome, is_final_demat = known
    badges = [event_type, f"prodotto {product}"]
    if is_final_demat:
        badges.append("demat finale")
    return description, badges, outcome


def entries_for_tracking(tracking, errors, dry_runs, want_flow):
    """Voci di timeline generate da una singola riga PaperTrackings."""
    tracking_id = tracking.get("trackingId", "")
    _, attempt, pc_retry = k.parse_tracking_id(tracking_id)
    origin = (f"ATTEMPT_{attempt} · PCRETRY_{pc_retry}" if attempt is not None
              else f"PCRETRY_{pc_retry}")
    result = []

    created_at = parse_ts(tracking.get("createdAt"))
    result.append(entry(
        "milestone", created_at, created_at,
        "Tracking creato", tracking_id,
        [tracking.get("productType"), tracking.get("unifiedDeliveryDriver"),
         tracking.get("processingMode")],
        tracking, origin))

    for event in tracking.get("events") or []:
        status_code = event.get("statusCode") or "?"
        description, badges, outcome = describe_status(status_code)
        own_description = event.get("statusDescription")
        subtitle = description
        if own_description and own_description.strip() and own_description != description:
            subtitle = f"{description} · dal consolidatore: {own_description}"
        badges = list(badges)
        if event.get("deliveryFailureCause"):
            badges.append(f"causa {event['deliveryFailureCause']}")
        if event.get("registeredLetterCode"):
            badges.append(f"codice oggetto {event['registeredLetterCode']}")
        attachments = event.get("attachments") or []
        if attachments:
            badges.append(f"{len(attachments)} allegat{'o' if len(attachments) == 1 else 'i'}")
        if event.get("dryRun"):
            badges.append("dry-run")
        result.append(entry(
            f"event-{outcome.lower()}",
            parse_ts(event.get("statusTimestamp")),
            parse_ts(event.get("createdAt")) or parse_ts(event.get("requestTimestamp")),
            status_code, subtitle, badges, event, origin, event.get("id")))

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
            error, origin))

    for output in dry_runs:
        status_code = output.get("statusCode") or "?"
        description, _, _ = describe_status(status_code)
        moment = parse_ts(output.get("statusDateTime")) or parse_ts(output.get("created"))
        result.append(entry(
            "dryrun", moment, parse_ts(output.get("created")),
            f"output → delivery-push: {status_code}",
            output.get("statusDescription") or description,
            ["dry-run", output.get("statusDetail"),
             f"causa {output['deliveryFailureCause']}" if output.get("deliveryFailureCause") else None],
            output, origin))

    if want_flow:
        flow = tracking.get("validationFlow") or {}
        for field, label in k.FLOW_STEPS:
            moment = parse_ts(flow.get(field))
            if moment:
                result.append(entry("milestone", moment, moment, label, field, [], flow, origin))

        for request in flow.get("ocrRequests") or []:
            asked = parse_ts(request.get("requestTimestamp"))
            if asked:
                result.append(entry(
                    "ocr", asked, asked, "Richiesta OCR",
                    request.get("documentType") or "",
                    [request.get("attachmentEventId")], request, origin))
            answered = parse_ts(request.get("responseTimestamp"))
            if answered:
                status = request.get("responseStatus") or "?"
                result.append(entry(
                    "ocr", answered, answered, f"Risposta OCR: {status}",
                    request.get("documentType") or "",
                    [request.get("finalEventId")], request, origin))

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


def build_recipients(trackings, errors, dry_runs, want_flow, order_by):
    """Raggruppa per destinatario e ordina le voci cronologicamente."""
    grouped = {}
    for tracking in trackings:
        rec, attempt, pc_retry = k.parse_tracking_id(tracking.get("trackingId", ""))
        bucket = grouped.setdefault(rec, {"recindex": rec, "trackings": [], "entries": []})
        bucket["trackings"].append((attempt or 0, pc_retry or 0, tracking))
        bucket["entries"].extend(entries_for_tracking(
            tracking,
            errors.get(tracking.get("trackingId")) or [],
            dry_runs.get(tracking.get("trackingId")) or [],
            want_flow))

    field = "arrival_at" if order_by == "arrival" else "status_at"
    for bucket in grouped.values():
        bucket["trackings"].sort(key=lambda row: (row[0], row[1]))
        bucket["entries"].sort(key=lambda item: item[field] or item["arrival_at"] or FAR_FUTURE)
        mark_queue_retries(bucket["entries"])
    return [grouped[rec] for rec in sorted(grouped, key=lambda r: (r is None, r))]


# =============================================================================
# Rendering
# =============================================================================
KIND_STYLE = {
    "event-ok":       ("bg-emerald-500", "border-emerald-200 bg-emerald-50"),
    "event-ko":       ("bg-rose-500", "border-rose-200 bg-rose-50"),
    "event-progress": ("bg-sky-500", "border-slate-200 bg-white"),
    "event-unknown":  ("bg-slate-400", "border-slate-200 bg-white"),
    "error":          ("bg-red-600", "border-red-300 bg-red-50"),
    "warning":        ("bg-amber-500", "border-amber-300 bg-amber-50"),
    "dryrun":         ("bg-violet-500", "border-violet-200 bg-violet-50"),
    "ocr":            ("bg-indigo-500", "border-indigo-200 bg-indigo-50"),
    "milestone":      ("bg-slate-300", "border-slate-200 bg-slate-50"),
}

STATE_STYLE = {
    "DONE": "bg-emerald-100 text-emerald-800",
    "KO": "bg-rose-100 text-rose-800",
}


def badge(text, classes="bg-slate-100 text-slate-600"):
    return (f'<span class="rounded px-2 py-0.5 text-[11px] font-medium {classes}">'
            f'{escape(str(text))}</span>')


def render_entry(item):
    dot, card = KIND_STYLE.get(item["kind"], KIND_STYLE["milestone"])
    moment = item["status_at"] or item["arrival_at"]
    arrival = item["arrival_at"]
    # L'arrivo sul tracker resta come tooltip: serve solo quando si ordina per arrivo.
    tooltip = (f' title="sul tracker {escape(fmt_ts(arrival))}"'
               if arrival and item["status_at"] and arrival != item["status_at"] else "")

    retry_badge = ""
    if item["retry"]:
        occurrence, total = item["retry"]
        retry_badge = badge(
            f"riconsegna coda {occurrence} di {total}" if occurrence > 1
            else f"riconsegnato dalla coda ({total} volte)",
            "bg-amber-100 text-amber-800 ring-1 ring-amber-300")

    return f"""
    <li class="relative">
      <span class="tl-dot absolute top-2 h-3 w-3 rounded-full ring-4 ring-white {dot}"></span>
      <div class="rounded-lg border {card} px-4 py-3 shadow-sm">
        <div class="flex flex-wrap items-baseline justify-between gap-2">
          <span class="font-mono text-sm font-semibold text-slate-900">{escape(item['title'])}</span>
          <span class="font-mono text-xs text-slate-500"{tooltip}>{escape(fmt_ts(moment))}</span>
        </div>
        <p class="mt-1 text-sm text-slate-700">{escape(item['subtitle'] or '')}</p>
        <div class="mt-2 flex flex-wrap items-center gap-1.5">
          {badge(item['origin'], 'bg-slate-200 text-slate-700')}
          {retry_badge}
          {''.join(badge(b) for b in item['badges'])}
        </div>
        {json_viewer(item['payload'])}
      </div>
    </li>"""


def render_recipient(bucket):
    last = bucket["trackings"][-1][2] if bucket["trackings"] else {}
    paper_status = last.get("paperStatus") or {}
    state = last.get("state") or "—"
    header_badges = "".join([
        badge(f"stato {state}", STATE_STYLE.get(state, "bg-slate-200 text-slate-700")),
        badge(f"business {last.get('businessState') or '—'}"),
        badge(f"prodotto {last.get('productType') or '—'}"),
        badge(f"{len(bucket['trackings'])} tracking"),
        badge(f"esito finale {paper_status.get('finalStatusCode')}", "bg-blue-100 text-blue-800")
        if paper_status.get("finalStatusCode") else "",
        badge(f"raccomandata {paper_status.get('registeredLetterCode')}")
        if paper_status.get("registeredLetterCode") else "",
    ])

    rows = "".join(
        f'<li class="font-mono text-xs text-slate-500">{escape(tracking.get("trackingId", ""))}</li>'
        for _, _, tracking in bucket["trackings"])

    return f"""
    <article class="mb-6 overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
      <header class="border-b border-slate-200 bg-slate-50 px-5 py-4">
        <h2 class="text-base font-semibold text-slate-900">
          Destinatario RECINDEX_{bucket['recindex']}
        </h2>
        <div class="mt-2 flex flex-wrap gap-1.5">{header_badges}</div>
        <details class="mt-2">
          <summary class="cursor-pointer text-xs font-medium text-brand hover:underline">
            trackingId coinvolti ({len(bucket['trackings'])})
          </summary>
          <ul class="mt-1 space-y-0.5">{rows}</ul>
        </details>
      </header>
      <div class="px-5 py-6">
        <ol class="tl ml-2 space-y-4 pl-6">
          {''.join(render_entry(item) for item in bucket['entries'])}
        </ol>
      </div>
    </article>"""


def render(iun, recipients):
    if not recipients:
        return ('<div class="rounded-xl border border-amber-300 bg-amber-50 p-5 text-sm '
                f'text-amber-900">Nessun tracking trovato per lo IUN <b>{escape(iun)}</b>. '
                "Controlla il prodotto selezionato e il profilo AWS.</div>")

    total_entries = sum(len(bucket["entries"]) for bucket in recipients)
    total_trackings = sum(len(bucket["trackings"]) for bucket in recipients)
    summary = f"""
    <div class="mb-6 rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
      <h2 class="font-mono text-sm font-semibold text-slate-900">{escape(iun)}</h2>
      <div class="mt-2 flex flex-wrap gap-1.5">
        {badge(f"{len(recipients)} destinatari")}
        {badge(f"{total_trackings} tracking")}
        {badge(f"{total_entries} voci in timeline")}
      </div>
    </div>"""
    return summary + "".join(render_recipient(bucket) for bucket in recipients)


# =============================================================================
# Profili e connessione
# =============================================================================
state = {"aws": None, "ddb": None}


def say(element_id, message, tone="text-slate-500"):
    node = el(element_id)
    node.className = f"mt-3 text-sm {tone}"
    node.innerText = message


def read_setting(key):
    """localStorage non è disponibile ovunque (file://, modalità restrittive)."""
    try:
        return window.localStorage.getItem(key)
    except Exception:
        return None


def write_setting(key, value):
    try:
        window.localStorage.setItem(key, value)
    except Exception:
        pass


def remembered_profile():
    return read_setting(k.LAST_PROFILE_KEY)


def profile_usage():
    """``{profilo: quante volte è stato usato}``, per ordinare la tendina."""
    raw = read_setting(k.PROFILE_USAGE_KEY)
    try:
        counts = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return counts if isinstance(counts, dict) else {}


def remember_profile(name):
    write_setting(k.LAST_PROFILE_KEY, name)
    counts = profile_usage()
    counts[name] = int(counts.get(name, 0)) + 1
    write_setting(k.PROFILE_USAGE_KEY, json.dumps(counts))


def profile_rank(name, last_used, counts):
    """Ultimo usato in cima, poi i più usati, poi in ordine alfabetico."""
    return (name != last_used, -int(counts.get(name, 0)), name)


def set_connection(status, text, detail=""):
    """status: ``off`` | ``ok`` | ``warn`` | ``ko``."""
    palette = {
        "off": ("bg-slate-400", "border-slate-200 bg-slate-50 text-slate-600"),
        "ok": ("bg-emerald-500", "border-emerald-200 bg-emerald-50 text-emerald-800"),
        "warn": ("bg-amber-500", "border-amber-200 bg-amber-50 text-amber-800"),
        "ko": ("bg-rose-500", "border-rose-200 bg-rose-50 text-rose-800"),
    }
    dot, pill = palette[status]
    el("conn-dot").className = f"h-2 w-2 rounded-full {dot}"
    el("conn-pill").className = ("inline-flex items-center gap-2 rounded-full border px-3 py-1.5 "
                                 f"text-xs font-medium {pill}")
    el("conn-pill").title = detail or text
    el("conn-text").innerText = text


def connect():
    """Prende la credenziale attiva del profilo scelto e prepara il client."""
    aws, profile = state["aws"], el("sel-profile").value
    state["ddb"] = None
    el("btn-search").disabled = True
    try:
        source = aws.best_source(profile)
        region = aws.region_for(profile) or k.DEFAULT_REGION
        state["ddb"] = aws.client("dynamodb", profile, source=source, region=region)
    except awsdir.AwsDirError as exc:
        set_connection("ko", "Credenziali non valide", str(exc))
        say("search-msg", str(exc), "text-rose-600")
        return

    remember_profile(profile)
    el("btn-search").disabled = False
    account = source.account or "account ignoto"
    remaining = source.expires_in
    if remaining is None:
        window_text = "senza scadenza"
    else:
        minutes = int(remaining.total_seconds() // 60)
        window_text = f"{minutes} min" if minutes < 60 else f"{minutes // 60}h {minutes % 60:02d}m"
    set_connection("ok", f"{region} · {account}",
                   f"{profile} — {source.describe()} — scadenza: {window_text}")
    say("search-msg", "")


def fill_profiles():
    aws = state["aws"]
    last_used, counts = remembered_profile(), profile_usage()
    names = sorted(aws.profile_names(),
                   key=lambda name: profile_rank(name, last_used, counts))
    select = el("sel-profile")
    select.innerHTML = "".join(
        f'<option value="{escape(name)}"{" selected" if name == names[0] else ""}>'
        f'{escape(name)}</option>' for name in names)
    select.disabled = False
    connect()


def selected_schemes():
    """Schemi di trackingId del prodotto scelto; in dubbio, li prova tutti."""
    return k.PRODUCT_SCHEMES.get(el("sel-product").value) or k.PRODUCT_SCHEMES["AUTO"]


def update_hint(event=None):
    """Mostra la forma del trackingId che verrà interrogata."""
    iun = el("inp-iun").value.strip() or "<IUN>"
    el("tracking-hint").innerText = "  •  ".join(
        k.build_attempt_id(scheme, iun, 0, 0) + ".PCRETRY_0" for scheme in selected_schemes())


# =============================================================================
# Eventi
# =============================================================================
@when("click", "#btn-pick")
async def on_pick(event):
    try:
        state["aws"] = await awsdir.AwsDirectory.pick()
    except Exception as exc:
        set_connection("ko", "Cartella non letta", str(exc))
        return
    fill_profiles()


@when("change", "#sel-profile")
def on_profile_change(event):
    connect()


@when("change", "#sel-product")
def on_product_change(event):
    update_hint()


@when("input", "#inp-iun")
def on_iun_input(event):
    update_hint()


@when("click", "#btn-search")
async def on_search(event):
    iun = el("inp-iun").value.strip()
    if not iun:
        say("search-msg", "Inserisci uno IUN.", "text-rose-600")
        return
    if not state["ddb"]:
        say("search-msg", "Nessuna connessione AWS attiva.", "text-rose-600")
        return

    schemes = selected_schemes()
    el("btn-search").disabled = True
    el("results").innerHTML = ""
    try:
        say("search-msg", "Avvio…")
        await asyncio.sleep(0)
        trackings, errors, dry_runs = await collect(
            state["ddb"], schemes, iun,
            int(el("inp-max-rec").value or k.MAX_RECINDEX),
            int(el("inp-max-att").value or k.MAX_ATTEMPT),
            el("chk-dryrun").checked,
            lambda text: say("search-msg", text))

        recipients = build_recipients(
            trackings, errors, dry_runs, el("chk-flow").checked, el("sel-order").value)
        el("results").innerHTML = render(iun, recipients)
        say("search-msg",
            f"Trovati {len(trackings)} tracking su {len(recipients)} destinatari.",
            "text-emerald-700")
    except Exception as exc:
        say("search-msg", f"{type(exc).__name__}: {exc}", "text-rose-600")
    finally:
        el("btn-search").disabled = False


# =============================================================================
# Avvio
# =============================================================================
el("sel-product").innerHTML = "".join(
    f'<option value="{value}">{escape(label)}</option>' for value, label, _ in k.PRODUCTS)
el("inp-max-rec").value = str(k.MAX_RECINDEX)
el("inp-max-att").value = str(k.MAX_ATTEMPT)
el("btn-pick").disabled = False
update_hint()
set_connection("off", "Non connesso", "Scegli la cartella ~/.aws")
