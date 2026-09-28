"""Lettura dei dati del tracker: DynamoDB, CloudWatch e la loro decodifica.

Non sa niente della pagina: riceve i client boto3, restituisce strutture Python.
"""

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone

import constants as k


# =============================================================================
# Decodifica e date
# =============================================================================

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

    Destinatari e tentativi sono numerati da 0 senza salti, quindi si sale
    finché si trova e ci si ferma al primo vuoto: una query per ogni
    ``(RECINDEX, ATTEMPT)``, più una che chiude ciascun livello.

    Il PCRETRY non si cerca: l'``attemptId`` è la partition key dell'indice, e
    una query restituisce già tutti i retry di quel tentativo.
    """
    found = []
    for rec in range(max_rec + 1):
        if not k.has_attempt(scheme):
            say(f"{scheme} · RECINDEX_{rec}")
            await asyncio.sleep(0)
            rows = query_trackings(ddb, k.build_attempt_id(scheme, iun, rec))
            if not rows:
                break
            found.extend(rows)
            continue

        found_for_rec = False
        for att in range(max_att + 1):
            say(f"{scheme} · RECINDEX_{rec} · ATTEMPT_{att}")
            await asyncio.sleep(0)
            rows = query_trackings(ddb, k.build_attempt_id(scheme, iun, rec, att))
            if not rows:
                break
            found.extend(rows)
            found_for_rec = True
        if not found_for_rec:
            break
    return found


async def find_by_iun(ddb, schemes, iun, max_rec, max_att, say):
    """I tracking dello IUN, ricostruendo i trackingId per ogni schema."""
    trackings = []
    for scheme in schemes:
        trackings.extend(await scan_scheme(ddb, scheme, iun, max_rec, max_att, say))
    return trackings


async def collect(ddb, trackings, want_dry_run, say):
    """Errori e output dry-run dei tracking dati."""
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

    return errors, dry_runs


# =============================================================================
# Log CloudWatch
# =============================================================================

def terms(*values):
    """Filtro CloudWatch: più termini fra virgolette valgono come AND."""
    return " ".join(f'"{value}"' for value in values if value)


def fetch_logs(logs, pattern, start, end, limit=None, max_pages=20):
    """Righe di log fra due istanti che corrispondono al filtro.

    Pagina finché c'è un ``nextToken``: ``filter_log_events`` restituisce pagine
    vuote mentre scandaglia il gruppo, e fermarsi alla prima direbbe «nessun
    log» pur avendone.
    """
    limit = limit or k.LOG_MAX_EVENTS
    events, token = [], None
    for _ in range(max_pages):
        kwargs = {
            "logGroupName": k.LOG_GROUP,
            "startTime": int(start.timestamp() * 1000),
            "endTime": int(end.timestamp() * 1000),
            "filterPattern": pattern,
            "limit": limit - len(events),
        }
        if token:
            kwargs["nextToken"] = token
        page = logs.filter_log_events(**kwargs)
        events.extend(page.get("events", []))
        token = page.get("nextToken")
        if not token or len(events) >= limit:
            break
    return events


def merge_windows(moments, minutes):
    """Gli intervalli ±minuti attorno agli istanti dati, fusi se si toccano.

    Gli eventi di una spedizione arrivano a grappoli: una query per grappolo
    invece di una per evento, senza allargarsi ai vuoti in mezzo.
    """
    window = timedelta(minutes=minutes)
    merged = []
    for moment in sorted(moments):
        start, end = moment - window, moment + window
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def cached_logs(cache, tracking_id, start, end):
    """Le righe già scaricate che cadono nella finestra, se ci sono."""
    events = cache.get(tracking_id)
    if events is None:
        return None
    first, last = start.timestamp() * 1000, end.timestamp() * 1000
    return [item for item in events if first <= item["timestamp"] <= last]


OUTPUT_MARKER = "Sending to output target for event: "

#: la riga che annuncia la lavorazione di un evento: è l'unica che porta sempre
#: il trace, sotto la chiave ``trace_id`` (altrove compare come ``traceId``).
HANDLING_MARKER = "Handling "

#: ripiego, quando la riga di lavorazione non c'è: la presa in carico dalla
#: coda. Il nome della coda fa parte del marcatore, perché nella stessa
#: finestra c'è anche l'hop a monte, "…_to_paper_channel".
INTAKE_MARKER = "Handle message from pn-external_channel_to_paper_tracker"


def intake_index(log_events):
    """Dove il tracker prende in carico l'evento, o ``None``.

    Prima di quella riga ci sono le righe dell'hop a monte, che non riguardano
    la lavorazione che stiamo guardando.
    """
    for index, item in enumerate(log_events):
        _, body = parse_log_line(item)
        if str(body.get("message") or "").startswith(INTAKE_MARKER):
            return index
    return None


def handling_trace(log_events, code):
    """Il trace della riga «Handling … and event: [CODE]», che lo porta sempre."""
    needle = f"and event: [{code}]"
    for item in log_events:
        _, body = parse_log_line(item)
        message = str(body.get("message") or "")
        if message.startswith(HANDLING_MARKER) and needle in message:
            return body.get("trace_id") or body.get("traceId")
    return None


def narrow_logs(logs, cache, tracking_id, code, start, end):
    """Le righe che nominano trackingId e codice.

    Se il prefetch ha già scaricato la finestra, si filtra quella in memoria:
    CloudWatch fa una sottostringa sul messaggio, che è quello che facciamo qui.
    """
    cached = cached_logs(cache, tracking_id, start, end)
    if cached is not None:
        return [item for item in cached if code in item["message"]]
    return fetch_logs(logs, terms(tracking_id, code), start, end)


def logs_for_entry(logs, cache, tracking_id, code, start, end):
    """``(righe, descrizione del filtro)`` per una voce di timeline.

    Senza codice si guarda tutto ciò che nomina il trackingId. Con un codice
    si cerca la riga che ne annuncia la lavorazione, se ne prende il trace e si
    riparte da quello: così escono anche le righe che il trackingId non lo
    scrivono, cioè quasi tutta la lavorazione.
    """
    if not code:
        cached = cached_logs(cache, tracking_id, start, end)
        if cached is not None:
            return cached, "filtro sul trackingId"
        return fetch_logs(logs, terms(tracking_id), start, end), "filtro sul trackingId"

    narrow = narrow_logs(logs, cache, tracking_id, code, start, end)
    trace = handling_trace(narrow, code)
    if trace:
        return fetch_logs(logs, terms(trace), start, end), f"trace {trace}"

    # nessuna riga di lavorazione: si tiene il primo passo, ma dalla presa in
    # carico in giù, così l'hop a monte resta comunque fuori
    intake = intake_index(narrow)
    if intake is None:
        return narrow, f"filtro su trackingId e {code}"
    return narrow[intake:], f"filtro su trackingId e {code}, dalla presa in carico"


def parse_log_line(item):
    """``(istante, corpo)`` di una riga: i log sono JSON di logstash."""
    try:
        body = json.loads(item["message"])
    except ValueError:
        body = {}
    moment = parse_ts(body.get("@timestamp")) or datetime.fromtimestamp(
        item["timestamp"] / 1000, timezone.utc)
    return moment, body


def parse_output_events(log_events):
    """Gli eventi spediti a delivery-push, con l'istante della riga che li porta.

    In dry-run finiscono in ``PaperTrackerDryRunOutputs``; fuori, la riga di log
    è l'unica traccia che resta, e il suo istante è il momento in cui il tracker
    li ha prodotti davvero — più preciso di qualunque data dentro il payload.
    """
    decoder = json.JSONDecoder()
    found = []
    for item in log_events:
        moment, body = parse_log_line(item)
        _, marker, payload = (body.get("message") or item["message"]).partition(OUTPUT_MARKER)
        if not marker:
            continue
        try:
            found.append((moment, decoder.raw_decode(payload.strip())[0]))
        except ValueError:
            continue
    return found


async def collect_log_outputs(logs, cache, trackings, minutes, say):
    """Per ogni tracking scarica i log attorno ai suoi eventi e ne cava gli output.

    Le righe restano in ``cache``: il modale di una voce le trova già pronte
    invece di richiedere le stesse a CloudWatch.
    """
    outputs = {}
    for tracking in trackings:
        tracking_id = tracking.get("trackingId")
        moments = [parse_ts(tracking.get("createdAt"))]
        moments += [parse_ts(event.get("statusTimestamp"))
                    for event in tracking.get("events") or []]
        moments = [moment for moment in moments if moment]
        if not moments:
            continue
        say(f"Log di {tracking_id}")
        await asyncio.sleep(0)
        events = []
        for start, end in merge_windows(moments, minutes):
            events.extend(fetch_logs(logs, terms(tracking_id), start, end))
        cache[tracking_id] = events
        outputs[tracking_id] = parse_output_events(events)
    return outputs
