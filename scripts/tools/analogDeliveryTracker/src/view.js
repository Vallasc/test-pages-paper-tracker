/** Composizione dell'HTML. Nessuno stato: entrano dati, esce una stringa. */

import * as k from "./constants.js";
import * as diagram from "./diagram.js";
import { fmtTs, isoUtc, parseLogLine } from "./data.js";
import { escape } from "./html.js";

// =============================================================================
// Visualizzazione del JSON
// =============================================================================

function jsonScalar(value) {
  if (value === null || value === undefined) {
    return '<span class="text-fuchsia-700">null</span>';
  }
  if (typeof value === "boolean") {
    return `<span class="text-fuchsia-700">${value}</span>`;
  }
  if (typeof value === "number") {
    return `<span class="text-amber-700">${value}</span>`;
  }
  return `<span class="text-emerald-700">${escape(JSON.stringify(String(value)))}</span>`;
}

/** Albero JSON: tutti i rami nascono aperti, richiudibili uno per uno. */
export function jsonNode(value, label = "") {
  const prefix = label
    ? `<span class="text-sky-800">${escape(JSON.stringify(label))}</span>` +
      '<span class="text-slate-400">: </span>'
    : "";

  let rows;
  let summary;
  if (Array.isArray(value)) {
    if (!value.length) return `<div>${prefix}<span class="text-slate-400">[]</span></div>`;
    rows = value.map((item, index) => jsonNode(item, `[${index}]`)).join("");
    summary = `${prefix}<span class="text-slate-400">[ ${value.length} elementi ]</span>`;
  } else if (value !== null && typeof value === "object") {
    const entries = Object.entries(value);
    if (!entries.length) return `<div>${prefix}<span class="text-slate-400">{}</span></div>`;
    rows = entries.map(([key, item]) => jsonNode(item, key)).join("");
    summary = `${prefix}<span class="text-slate-400">{ ${entries.length} campi }</span>`;
  } else {
    return `<div class="break-all">${prefix}${jsonScalar(value)}</div>`;
  }

  return "<details open>" +
    `<summary class="cursor-pointer rounded hover:bg-slate-100">${summary}</summary>` +
    `<div class="ml-2 border-l border-slate-200 pl-3">${rows}</div>` +
    "</details>";
}

/** L'albero JSON come corpo del modale. */
export function jsonBody(payload) {
  return '<div class="jsn max-h-[70vh] overflow-auto rounded-md border border-slate-200 ' +
    'bg-slate-50 p-3 font-mono text-[13px] leading-relaxed text-slate-800">' +
    `${jsonNode(payload)}</div>`;
}

// =============================================================================
// Righe di log
// =============================================================================

const LEVEL_STYLE = {
  ERROR: "text-rose-700",
  WARN: "text-amber-700",
  INFO: "text-slate-700",
  DEBUG: "text-slate-400",
};

/**
 * Evidenzia «error» nel testo di una riga, comunque sia scritto.
 *
 * Si sostituisce sul testo già escapato: «error» non compare dentro nessuna
 * entità HTML, quindi non c'è modo di spezzarne una.
 */
export function markErrors(text) {
  return escape(text).replace(/error/gi, (found) =>
    `<span class="rounded bg-rose-100 px-0.5 font-semibold text-rose-700">${found}</span>`);
}

/** Le righe di log, già spacchettate dal JSON di logstash. */
export function renderLogs(events, minutes) {
  if (!events.length) {
    return '<p class="mt-1 text-xs text-slate-500">Nessun log: nessuna riga per ' +
      `questo filtro in ±${minutes} min.</p>`;
  }

  const rows = events.map((item) => {
    const { moment, body } = parseLogLine(item);
    const level = (body.level ?? "").toUpperCase();
    const logger = (body.logger_name ?? "").split(".").at(-1);
    return '<div class="border-b border-slate-100 py-1 last:border-0">' +
      `<span class="text-slate-400">${escape(fmtTs(moment))}</span> ` +
      `<span class="font-medium ${LEVEL_STYLE[level] ?? "text-slate-600"}">` +
      `${escape(level)}</span> <span class="text-sky-800">${escape(logger)}</span>` +
      // whitespace-pre-wrap: qui dentro ogni spazio in più si vedrebbe
      '<div class="whitespace-pre-wrap break-all text-slate-800">' +
      `${markErrors(String(body.message ?? item.message))}</div></div>`;
  });

  const capped = events.length >= k.LOG_MAX_EVENTS ? " (tetto raggiunto)" : "";
  return `<p class="mt-1 text-xs text-slate-500">${events.length} righe in ±${minutes} min${capped}</p>` +
    '<div class="mt-1 max-h-[70vh] overflow-auto rounded-md border border-slate-200 ' +
    `bg-slate-50 p-3 font-mono text-[12px] leading-relaxed">${rows.join("")}</div>`;
}

// =============================================================================
// Timeline
// =============================================================================

const KIND_STYLE = {
  "event-ok": ["bg-emerald-500", "border-emerald-200 bg-emerald-50"],
  // oggi nessun codice del catalogo arriva qui: i KO sono tutti FINAL_EVENT,
  // quindi verdi. Resta per un eventuale KO non finale, e comunque non rosso:
  // il rosso è degli errori veri, quelli di PaperTrackingsErrors.
  "event-ko": ["bg-slate-700", "border-slate-300 bg-white"],
  "event-progress": ["bg-sky-500", "border-slate-200 bg-white"],
  "event-unknown": ["bg-slate-400", "border-slate-200 bg-white"],
  error: ["bg-red-600", "border-red-300 bg-red-50"],
  warning: ["bg-amber-500", "border-amber-300 bg-amber-50"],
  // stessa cosa vista da due parti: output verso delivery-push, dalla tabella
  // di dry-run o dai log. A distinguerli basta il badge.
  dryrun: ["bg-violet-500", "border-violet-200 bg-violet-50"],
  output: ["bg-violet-500", "border-violet-200 bg-violet-50"],
  ocr: ["bg-indigo-500", "border-indigo-200 bg-indigo-50"],
  milestone: ["bg-slate-300", "border-slate-200 bg-slate-50"],
};

/**
 * i chip di stato si leggono a colpo d'occhio: KO rosso, riuscito verde, il
 * resto grigio. Vale per lo stato del tracking e per quello di business.
 */
const STATE_STYLE = {
  KO: "bg-rose-100 text-rose-800 ring-1 ring-rose-200",
  OK: "bg-emerald-100 text-emerald-800 ring-1 ring-emerald-200",
  DONE: "bg-emerald-100 text-emerald-800 ring-1 ring-emerald-200",
};

function stateBadge(label, value) {
  return badge(`${label} ${value}`, STATE_STYLE[value] ?? "bg-slate-200 text-slate-700");
}

export function badge(text, classes = "bg-slate-100 text-slate-600") {
  return `<span class="rounded px-2 py-0.5 text-[11px] font-medium ${classes}">` +
    `${escape(text)}</span>`;
}

/** tracciati a 24x24, disegnati con currentColor */
const ICONS = {
  json: "M8 4H7a2 2 0 0 0-2 2v3.5a2 2 0 0 1-2 2 2 2 0 0 1 2 2V17a2 2 0 0 0 2 2h1" +
        "M16 4h1a2 2 0 0 1 2 2v3.5a2 2 0 0 0 2 2 2 2 0 0 0-2 2V17a2 2 0 0 1-2 2h-1",
  logs: "M4 6h16M4 10.5h10M4 15h16M4 19.5h8",
  table: "M4 6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z" +
         "M4 10h16M10 10v10",
  copy: "M9 9a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-8a2 2 0 0 1-2-2z" +
        "M5 15H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v1",
  diagram: "M4 4h6v5H4zM14 15h6v5h-6zM7 9v4a2 2 0 0 0 2 2h5",
  check: "M4 12.5l5 5L20 6.5",
};

export function icon(name) {
  return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" ' +
    'stroke-linecap="round" stroke-linejoin="round" class="h-3.5 w-3.5 shrink-0" ' +
    `aria-hidden="true"><path d="${ICONS[name]}"/></svg>`;
}

/** Un bottone del piede della card: icona più etichetta. */
export function action(kind, label, attributes = "") {
  return `<button type="button" data-detail="${kind}" ${attributes} ` +
    'class="inline-flex cursor-pointer items-center gap-1 rounded border ' +
    'border-slate-300 bg-white/70 ' +
    'px-2 py-1 text-[11px] font-medium text-slate-600 hover:border-slate-400 ' +
    `hover:bg-white hover:text-slate-900">${icon(kind)}${escape(label)}</button>`;
}

/** Scorciatoia alla console, col bottone per copiarne l'URL accanto. */
export function consoleLink(href, kind, label) {
  return '<span class="inline-flex items-center overflow-hidden rounded border ' +
    'border-slate-300 bg-white">' +
    `<a href="${escape(href)}" target="_blank" rel="noopener" ` +
    'class="inline-flex items-center gap-1 px-2 py-1 text-[11px] font-medium ' +
    `text-slate-600 hover:bg-slate-50 hover:text-slate-900">${icon(kind)}` +
    `${escape(label)}</a>` +
    `<button type="button" data-copy="${escape(href)}" title="Copia il link" ` +
    'class="cursor-pointer border-l border-slate-300 px-1.5 py-1 text-slate-400 ' +
    'hover:bg-slate-50 ' +
    `hover:text-slate-900">${icon("copy")}</button></span>`;
}

/** Il bottone dei log esiste solo se c'è una finestra dove cercarli. */
function logAction(item, moment) {
  if (!moment || !item.trackingId) return "";
  return action("logs", "Log",
    `data-logs="${escape(item.trackingId)}" data-at="${isoUtc(moment)}" ` +
    `data-code="${escape(item.logCode ?? "")}"`);
}

export function renderEntry(item, payloads) {
  const [dot, card] = KIND_STYLE[item.kind] ?? KIND_STYLE.milestone;
  // in testata l'arrivo sul tracker: è l'asse di ordinamento di default, ed è
  // anche l'istante attorno a cui ha senso cercare i log. Lo statusTimestamp,
  // che è la data dichiarata nell'evento, va nel piede.
  const moment = item.arrivalAt || item.statusAt;
  const statusAt = item.statusAt;
  const stamp = statusAt && Number(statusAt) !== Number(moment)
    ? `statusTimestamp ${escape(fmtTs(statusAt))}` : "";

  let retryBadge = "";
  if (item.retry) {
    const [occurrence, total] = item.retry;
    retryBadge = badge(
      occurrence > 1 ? `riconsegna coda ${occurrence} di ${total}`
                     : `riconsegnato dalla coda (${total} volte)`,
      "bg-amber-100 text-amber-800 ring-1 ring-amber-300");
  }

  // il payload resta in memoria: il modale lo ripesca per uid, invece di
  // portarselo dentro l'HTML della card
  const uid = String(payloads.size);
  payloads.set(uid, [item.title, item.payload]);
  const inline = retryBadge + item.badges.map((b) => badge(b)).join("");
  const chips = inline
    ? `<div class="mt-2 flex flex-wrap items-center gap-1.5">${inline}</div>` : "";

  return `
    <li class="relative">
      <span class="tl-dot absolute top-2 h-3 w-3 rounded-full ring-4 ring-white ${dot}"></span>
      <div class="rounded-lg border ${card} px-4 py-3 shadow-sm">
        <div class="flex flex-wrap items-baseline justify-between gap-2">
          <span class="font-mono text-sm font-semibold text-slate-900">${escape(item.title)}</span>
          <span class="font-mono text-xs text-slate-500"
                title="ricevuto dal tracker">${escape(fmtTs(moment))}</span>
        </div>
        <p class="mt-1 text-sm text-slate-700">${escape(item.subtitle ?? "")}</p>
        ${chips}
        <div class="mt-3 flex flex-wrap items-center justify-between gap-2
                    border-t border-slate-900/5 pt-2">
          <span class="font-mono text-[11px] text-slate-500">${stamp}</span>
          <span class="flex items-center gap-1">
            ${action("json", "JSON", `data-uid="${uid}"`)}
            ${logAction(item, moment)}
          </span>
        </div>
      </div>
    </li>`;
}

/** etichetta e chiave del conteggio, per tabella */
const TABLE_LINKS = [
  ["trackings", "PaperTrackings"],
  ["errors", "Errors"],
  ["dry_run", "DryRunOutputs"],
];

/**
 * Per ogni tracking l'intervallo che copre le sue voci, in millisecondi.
 *
 * Serve al link dei log: la console senza finestra apre sull'ultimo intervallo
 * di default, dove un evento di ieri non c'è.
 */
export function trackingSpans(entries, minutes) {
  const window = minutes * 60 * 1000;
  const spans = new Map();
  for (const item of entries) {
    for (const moment of [item.statusAt, item.arrivalAt]) {
      if (!moment) continue;
      const [first, last] = spans.get(item.trackingId) ?? [moment, moment];
      spans.set(item.trackingId,
                [moment < first ? moment : first, moment > last ? moment : last]);
    }
  }
  return Object.fromEntries([...spans].map(([trackingId, [first, last]]) =>
    [trackingId, [first.getTime() - window, last.getTime() + window]]));
}

/** I link di un tracking: query su ogni tabella che ha risposto, più i log. */
function trackingLinks(trackingId, tables, spans, region) {
  const found = tables[trackingId] ?? {};
  const links = TABLE_LINKS
    .filter(([name]) => found[name])
    .map(([name, label]) =>
      consoleLink(k.dynamoUrl(k.TABLES[name], region, trackingId), "table",
                  `${label} · ${found[name]}`));
  const [start, end] = spans[trackingId] ?? [null, null];
  links.push(consoleLink(k.logsUrl(trackingId, region, start, end), "logs", "Log"));
  return links.join("");
}

/**
 * Un blocco per tracking: la sua identità in testa, poi le sue voci.
 *
 * Le voci di tentativi e retry diversi non si mescolano in un'unica scaletta:
 * ognuno ha il suo riquadro, nell'ordine in cui sono stati tentati.
 */
function renderTracking(tracking, entries, tables, spans, region, payloads) {
  const trackingId = tracking.trackingId ?? "";
  const { attempt, pcretry } = k.parseTrackingId(trackingId);
  const title = attempt !== null
    ? `ATTEMPT_${attempt} · PCRETRY_${pcretry}` : `PCRETRY_${pcretry}`;
  return `
    <section class="border-t border-slate-200 px-5 pb-5">
      <div class="sticky top-[var(--page-header)] z-10 -mx-5 mb-4 flex flex-wrap
                  items-start justify-between gap-2 border-b border-slate-100
                  bg-white px-5 py-3">
        <div class="min-w-0">
          <h3 class="font-mono text-sm font-semibold text-slate-900">${escape(title)}</h3>
          <p class="mt-0.5 break-all font-mono text-[11px] text-slate-400">
            ${escape(trackingId)}</p>
        </div>
        <span class="flex flex-wrap items-center gap-1.5">
          ${diagram.action(tracking, action)}${trackingLinks(trackingId, tables, spans, region)}
        </span>
      </div>
      <ol class="tl ml-2 space-y-4 pl-6">
        ${entries.map((item) => renderEntry(item, payloads)).join("")}
      </ol>
    </section>`;
}

function renderRecipient(bucket, region, payloads) {
  const last = bucket.trackings.length ? bucket.trackings.at(-1)[2] : {};
  const paperStatus = last.paperStatus ?? {};
  // non chiamarlo "state": il modulo ne ha già uno, e qui lo maschererebbe
  const trackingState = last.state ?? "—";
  const headerBadges = [
    stateBadge("stato", trackingState),
    stateBadge("business", last.businessState ?? "—"),
    badge(`prodotto ${last.productType ?? "—"}`),
    bucket.trackings.some(([, , tracking]) => k.isBonaria(tracking.trackingId))
      ? badge("bonarie", "bg-violet-100 text-violet-800 ring-1 ring-violet-300") : "",
    badge(`${bucket.trackings.length} tracking`),
    paperStatus.finalStatusCode
      ? badge(`esito finale ${paperStatus.finalStatusCode}`, "bg-blue-100 text-blue-800") : "",
  ].join("");

  const spans = trackingSpans(bucket.entries, k.LOG_WINDOW_MINUTES);
  // le voci restano ordinate come sono: qui si smistano solo nel loro blocco
  const byTracking = new Map();
  for (const item of bucket.entries) {
    if (!byTracking.has(item.trackingId)) byTracking.set(item.trackingId, []);
    byTracking.get(item.trackingId).push(item);
  }
  const blocks = bucket.trackings.map(([, , tracking]) =>
    renderTracking(tracking, byTracking.get(tracking.trackingId ?? "") ?? [],
                   bucket.tables ?? {}, spans, region, payloads)).join("");

  return `
    <article class="mb-6 rounded-xl border border-slate-200 bg-white shadow-sm">
      <header class="rounded-t-xl bg-slate-50 px-5 py-4">
        <h2 class="text-base font-semibold text-slate-900">
          Destinatario RECINDEX_${bucket.recindex}
        </h2>
        <div class="mt-2 flex flex-wrap gap-1.5">${headerBadges}</div>
      </header>
      ${blocks}
    </article>`;
}

export function render(needle, recipients, region, payloads) {
  if (!recipients.length) {
    return '<div class="rounded-xl border border-amber-300 bg-amber-50 p-5 text-sm ' +
      `text-amber-900">Nessun tracking trovato per <b>${escape(needle)}</b>. ` +
      "Controlla il profilo AWS e, se cerchi per IUN, il prodotto selezionato.</div>";
  }

  const totalEntries = recipients.reduce((sum, bucket) => sum + bucket.entries.length, 0);
  const totalTrackings = recipients.reduce((sum, bucket) => sum + bucket.trackings.length, 0);
  const summary = `
    <div class="mb-6 rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
      <h2 class="font-mono text-sm font-semibold text-slate-900">${escape(needle)}</h2>
      <div class="mt-2 flex flex-wrap gap-1.5">
        ${badge(`${recipients.length} destinatari`)}
        ${badge(`${totalTrackings} tracking`)}
        ${badge(`${totalEntries} voci in timeline`)}
      </div>
    </div>`;
  return summary + recipients.map((bucket) => renderRecipient(bucket, region, payloads)).join("");
}
