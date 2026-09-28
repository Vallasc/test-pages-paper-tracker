"""Composizione dell'HTML. Nessuno stato: entrano dati, esce una stringa."""

import json
import re
from datetime import timedelta
from html import escape

import constants as k
import diagram
from data import fmt_ts, parse_log_line


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


def json_body(payload):
    """L'albero JSON come corpo del modale."""
    return ('<div class="jsn max-h-[70vh] overflow-auto rounded-md border border-slate-200 '
            'bg-slate-50 p-3 font-mono text-[13px] leading-relaxed text-slate-800">'
            f'{json_node(payload)}</div>')


# =============================================================================
# Righe di log
# =============================================================================

LEVEL_STYLE = {
    "ERROR": "text-rose-700",
    "WARN": "text-amber-700",
    "INFO": "text-slate-700",
    "DEBUG": "text-slate-400",
}


_ERROR_RE = re.compile("error", re.IGNORECASE)


def mark_errors(text):
    """Evidenzia «error» nel testo di una riga, comunque sia scritto.

    Si sostituisce sul testo già escapato: «error» non compare dentro nessuna
    entità HTML, quindi non c'è modo di spezzarne una.
    """
    return _ERROR_RE.sub(
        lambda found: '<span class="rounded bg-rose-100 px-0.5 font-semibold '
                      f'text-rose-700">{found.group(0)}</span>',
        escape(text))


def render_logs(events, minutes):
    """Le righe di log, già spacchettate dal JSON di logstash."""
    if not events:
        return ('<p class="mt-1 text-xs text-slate-500">Nessun log: nessuna riga per '
                f'questo filtro in ±{minutes} min.</p>')

    rows = []
    for item in events:
        moment, body = parse_log_line(item)
        level = (body.get("level") or "").upper()
        logger = (body.get("logger_name") or "").rsplit(".", 1)[-1]
        rows.append(
            '<div class="border-b border-slate-100 py-1 last:border-0">'
            f'<span class="text-slate-400">{escape(fmt_ts(moment))}</span> '
            f'<span class="font-medium {LEVEL_STYLE.get(level, "text-slate-600")}">'
            f'{escape(level)}</span> <span class="text-sky-800">{escape(logger)}</span>'
            f'<div class="whitespace-pre-wrap break-all text-slate-800">'
            f'{mark_errors(str(body.get("message") or item["message"]))}</div></div>')

    header = (f'<p class="mt-1 text-xs text-slate-500">{len(events)} righe in ±{minutes} min'
              + (" (tetto raggiunto)" if len(events) >= k.LOG_MAX_EVENTS else "") + "</p>")
    return (header + '<div class="mt-1 max-h-[70vh] overflow-auto rounded-md border border-slate-200 '
            'bg-slate-50 p-3 font-mono text-[12px] leading-relaxed">'
            + "".join(rows) + "</div>")


# =============================================================================
# Timeline
# =============================================================================

KIND_STYLE = {
    "event-ok":       ("bg-emerald-500", "border-emerald-200 bg-emerald-50"),
    # oggi nessun codice del catalogo arriva qui: i KO sono tutti FINAL_EVENT,
    # quindi verdi. Resta per un eventuale KO non finale, e comunque non rosso:
    # il rosso è degli errori veri, quelli di PaperTrackingsErrors.
    "event-ko":       ("bg-slate-700", "border-slate-300 bg-white"),
    "event-progress": ("bg-sky-500", "border-slate-200 bg-white"),
    "event-unknown":  ("bg-slate-400", "border-slate-200 bg-white"),
    "error":          ("bg-red-600", "border-red-300 bg-red-50"),
    "warning":        ("bg-amber-500", "border-amber-300 bg-amber-50"),
    # stessa cosa vista da due parti: output verso delivery-push, dalla tabella
    # di dry-run o dai log. A distinguerli basta il badge.
    "dryrun":         ("bg-violet-500", "border-violet-200 bg-violet-50"),
    "output":         ("bg-violet-500", "border-violet-200 bg-violet-50"),
    "ocr":            ("bg-indigo-500", "border-indigo-200 bg-indigo-50"),
    "milestone":      ("bg-slate-300", "border-slate-200 bg-slate-50"),
}

#: i chip di stato si leggono a colpo d'occhio: KO rosso, riuscito verde, il
#: resto grigio. Vale per lo stato del tracking e per quello di business.
STATE_STYLE = {
    "KO": "bg-rose-100 text-rose-800 ring-1 ring-rose-200",
    "OK": "bg-emerald-100 text-emerald-800 ring-1 ring-emerald-200",
    "DONE": "bg-emerald-100 text-emerald-800 ring-1 ring-emerald-200",
}


def state_badge(label, value):
    return badge(f"{label} {value}", STATE_STYLE.get(value, "bg-slate-200 text-slate-700"))


def badge(text, classes="bg-slate-100 text-slate-600"):
    return (f'<span class="rounded px-2 py-0.5 text-[11px] font-medium {classes}">'
            f'{escape(str(text))}</span>')


#: tracciati a 24x24, disegnati con currentColor
ICONS = {
    "json": ("M8 4H7a2 2 0 0 0-2 2v3.5a2 2 0 0 1-2 2 2 2 0 0 1 2 2V17a2 2 0 0 0 2 2h1"
             "M16 4h1a2 2 0 0 1 2 2v3.5a2 2 0 0 0 2 2 2 2 0 0 0-2 2V17a2 2 0 0 1-2 2h-1"),
    "logs": "M4 6h16M4 10.5h10M4 15h16M4 19.5h8",
    "table": ("M4 6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z"
              "M4 10h16M10 10v10"),
    "copy": ("M9 9a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-8a2 2 0 0 1-2-2z"
             "M5 15H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v1"),
    "diagram": "M4 4h6v5H4zM14 15h6v5h-6zM7 9v4a2 2 0 0 0 2 2h5",
    "check": "M4 12.5l5 5L20 6.5",
}


def icon(name):
    return ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
            'stroke-linecap="round" stroke-linejoin="round" class="h-3.5 w-3.5 shrink-0" '
            f'aria-hidden="true"><path d="{ICONS[name]}"/></svg>')


def action(kind, label, attributes=""):
    """Un bottone del piede della card: icona più etichetta."""
    return (f'<button type="button" data-detail="{kind}" {attributes} '
            'class="inline-flex cursor-pointer items-center gap-1 rounded border '
            'border-slate-300 bg-white/70 '
            'px-2 py-1 text-[11px] font-medium text-slate-600 hover:border-slate-400 '
            f'hover:bg-white hover:text-slate-900">{icon(kind)}{escape(label)}</button>')


def console_link(href, kind, label):
    """Scorciatoia alla console, col bottone per copiarne l'URL accanto."""
    return ('<span class="inline-flex items-center overflow-hidden rounded border '
            'border-slate-300 bg-white">'
            f'<a href="{escape(href)}" target="_blank" rel="noopener" '
            'class="inline-flex items-center gap-1 px-2 py-1 text-[11px] font-medium '
            f'text-slate-600 hover:bg-slate-50 hover:text-slate-900">{icon(kind)}'
            f'{escape(label)}</a>'
            f'<button type="button" data-copy="{escape(href)}" title="Copia il link" '
            'class="cursor-pointer border-l border-slate-300 px-1.5 py-1 text-slate-400 '
            'hover:bg-slate-50 '
            f'hover:text-slate-900">{icon("copy")}</button></span>')


def log_action(item, moment):
    """Il bottone dei log esiste solo se c'è una finestra dove cercarli."""
    if not moment or not item.get("tracking_id"):
        return ""
    return action("logs", "Log",
                  f'data-logs="{escape(item["tracking_id"])}" data-at="{moment.isoformat()}" '
                  f'data-code="{escape(item.get("log_code") or "")}"')


def render_entry(item, region, payloads):
    dot, card = KIND_STYLE.get(item["kind"], KIND_STYLE["milestone"])
    # in testata l'arrivo sul tracker: è l'asse di ordinamento di default, ed è
    # anche l'istante attorno a cui ha senso cercare i log. Lo statusTimestamp,
    # che è la data dichiarata nell'evento, va nel piede.
    moment = item["arrival_at"] or item["status_at"]
    status_at = item["status_at"]
    stamp = (f"statusTimestamp {escape(fmt_ts(status_at))}"
             if status_at and status_at != moment else "")

    retry_badge = ""
    if item["retry"]:
        occurrence, total = item["retry"]
        retry_badge = badge(
            f"riconsegna coda {occurrence} di {total}" if occurrence > 1
            else f"riconsegnato dalla coda ({total} volte)",
            "bg-amber-100 text-amber-800 ring-1 ring-amber-300")

    # il payload resta in memoria: il modale lo ripesca per uid, invece di
    # portarselo dentro l'HTML della card
    uid = str(len(payloads))
    payloads[uid] = (item["title"], item["payload"])
    inline = retry_badge + "".join(badge(b) for b in item["badges"])
    chips = (f'<div class="mt-2 flex flex-wrap items-center gap-1.5">{inline}</div>'
             if inline else "")

    return f"""
    <li class="relative">
      <span class="tl-dot absolute top-2 h-3 w-3 rounded-full ring-4 ring-white {dot}"></span>
      <div class="rounded-lg border {card} px-4 py-3 shadow-sm">
        <div class="flex flex-wrap items-baseline justify-between gap-2">
          <span class="font-mono text-sm font-semibold text-slate-900">{escape(item['title'])}</span>
          <span class="font-mono text-xs text-slate-500"
                title="ricevuto dal tracker">{escape(fmt_ts(moment))}</span>
        </div>
        <p class="mt-1 text-sm text-slate-700">{escape(item['subtitle'] or '')}</p>
        {chips}
        <div class="mt-3 flex flex-wrap items-center justify-between gap-2
                    border-t border-slate-900/5 pt-2">
          <span class="font-mono text-[11px] text-slate-500">{stamp}</span>
          <span class="flex items-center gap-1">
            {action('json', 'JSON', f'data-uid="{uid}"')}
            {log_action(item, moment)}
          </span>
        </div>
      </div>
    </li>"""


#: etichetta e chiave del conteggio, per tabella
TABLE_LINKS = (
    ("trackings", "PaperTrackings"),
    ("errors", "Errors"),
    ("dry_run", "DryRunOutputs"),
)


def tracking_spans(entries, minutes):
    """Per ogni tracking l'intervallo che copre le sue voci, in millisecondi.

    Serve al link dei log: la console senza finestra apre sull'ultimo intervallo
    di default, dove un evento di ieri non c'è.
    """
    window = timedelta(minutes=minutes)
    spans = {}
    for item in entries:
        for moment in (item["status_at"], item["arrival_at"]):
            if not moment:
                continue
            first, last = spans.get(item.get("tracking_id"), (moment, moment))
            spans[item.get("tracking_id")] = (min(first, moment), max(last, moment))
    return {tracking_id: (int((first - window).timestamp() * 1000),
                          int((last + window).timestamp() * 1000))
            for tracking_id, (first, last) in spans.items()}


def tracking_links(tracking_id, tables, spans, region):
    """I link di un tracking: query su ogni tabella che ha risposto, più i log."""
    found = tables.get(tracking_id) or {}
    links = [console_link(k.dynamo_url(k.TABLES[name], region, tracking_id), "table",
                          f"{label} · {found[name]}")
             for name, label in TABLE_LINKS if found.get(name)]
    start, end = spans.get(tracking_id, (None, None))
    links.append(console_link(k.logs_url(tracking_id, region, start, end), "logs", "Log"))
    return "".join(links)


def render_tracking(tracking, entries, tables, spans, region, payloads):
    """Un blocco per tracking: la sua identità in testa, poi le sue voci.

    Le voci di tentativi e retry diversi non si mescolano più in un'unica
    scaletta: ognuno ha il suo riquadro, nell'ordine in cui sono stati tentati.
    """
    tracking_id = tracking.get("trackingId", "")
    _, attempt, pc_retry = k.parse_tracking_id(tracking_id)
    title = (f"ATTEMPT_{attempt} · PCRETRY_{pc_retry}" if attempt is not None
             else f"PCRETRY_{pc_retry}")
    return f"""
    <section class="border-t border-slate-200 px-5 pb-5">
      <div class="sticky top-[var(--page-header)] z-10 -mx-5 mb-4 flex flex-wrap
                  items-start justify-between gap-2 border-b border-slate-100
                  bg-white px-5 py-3">
        <div class="min-w-0">
          <h3 class="font-mono text-sm font-semibold text-slate-900">{escape(title)}</h3>
          <p class="mt-0.5 break-all font-mono text-[11px] text-slate-400">
            {escape(tracking_id)}</p>
        </div>
        <span class="flex flex-wrap items-center gap-1.5">
          {diagram.action(tracking, action)}{tracking_links(tracking_id, tables, spans, region)}
        </span>
      </div>
      <ol class="tl ml-2 space-y-4 pl-6">
        {''.join(render_entry(item, region, payloads) for item in entries)}
      </ol>
    </section>"""


def render_recipient(bucket, region, payloads):
    last = bucket["trackings"][-1][2] if bucket["trackings"] else {}
    paper_status = last.get("paperStatus") or {}
    # non chiamarlo "state": il modulo ne ha già uno, e qui lo maschererebbe
    tracking_state = last.get("state") or "—"
    header_badges = "".join([
        state_badge("stato", tracking_state),
        state_badge("business", last.get("businessState") or "—"),
        badge(f"prodotto {last.get('productType') or '—'}"),
        badge("bonarie", "bg-violet-100 text-violet-800 ring-1 ring-violet-300")
        if any(k.is_bonaria(tracking.get("trackingId"))
               for _, _, tracking in bucket["trackings"]) else "",
        badge(f"{len(bucket['trackings'])} tracking"),
        badge(f"esito finale {paper_status.get('finalStatusCode')}", "bg-blue-100 text-blue-800")
        if paper_status.get("finalStatusCode") else "",
    ])

    spans = tracking_spans(bucket["entries"], k.LOG_WINDOW_MINUTES)
    # le voci restano ordinate come sono: qui si smistano solo nel loro blocco
    by_tracking = {}
    for item in bucket["entries"]:
        by_tracking.setdefault(item.get("tracking_id"), []).append(item)
    blocks = "".join(
        render_tracking(tracking, by_tracking.get(tracking.get("trackingId", ""), []),
                        bucket.get("tables", {}), spans, region, payloads)
        for _, _, tracking in bucket["trackings"])

    return f"""
    <article class="mb-6 rounded-xl border border-slate-200 bg-white shadow-sm">
      <header class="rounded-t-xl bg-slate-50 px-5 py-4">
        <h2 class="text-base font-semibold text-slate-900">
          Destinatario RECINDEX_{bucket['recindex']}
        </h2>
        <div class="mt-2 flex flex-wrap gap-1.5">{header_badges}</div>
      </header>
      {blocks}
    </article>"""


def render(needle, recipients, region, payloads):
    if not recipients:
        return ('<div class="rounded-xl border border-amber-300 bg-amber-50 p-5 text-sm '
                f'text-amber-900">Nessun tracking trovato per <b>{escape(needle)}</b>. '
                "Controlla il profilo AWS e, se cerchi per IUN, il prodotto selezionato.</div>")

    total_entries = sum(len(bucket["entries"]) for bucket in recipients)
    total_trackings = sum(len(bucket["trackings"]) for bucket in recipients)
    summary = f"""
    <div class="mb-6 rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
      <h2 class="font-mono text-sm font-semibold text-slate-900">{escape(needle)}</h2>
      <div class="mt-2 flex flex-wrap gap-1.5">
        {badge(f"{len(recipients)} destinatari")}
        {badge(f"{total_trackings} tracking")}
        {badge(f"{total_entries} voci in timeline")}
      </div>
    </div>"""
    return summary + "".join(render_recipient(bucket, region, payloads)
                             for bucket in recipients)
