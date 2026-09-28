/** Dalle righe delle tabelle alle voci di timeline, raggruppate per destinatario. */

import * as k from "./constants.js";
import { FAR_FUTURE, parseTs } from "./data.js";

export function entry(kind, statusAt, arrivalAt, title, subtitle, badges, payload,
                      eventId = null, logCode = null) {
  return {
    kind,
    statusAt,
    arrivalAt: arrivalAt || statusAt,
    title,
    subtitle,
    badges: badges.filter(Boolean),
    payload,
    eventId,
    retry: null,
    trackingId: null,
    // statusCode da cui partire per risalire al traceId nei log
    logCode,
  };
}

/** Quante volte compare ogni valore, nell'ordine in cui si presentano. */
function counted(values) {
  const totals = new Map();
  for (const value of values) totals.set(value, (totals.get(value) ?? 0) + 1);
  return totals;
}

/** Un chip per tipo di documento allegato, col conto se sono più d'uno. */
export function attachmentBadges(attachments) {
  const kinds = counted((attachments ?? []).map((item) => item.documentType ?? ""));
  return [...kinds].map(([kind, count]) =>
    `${count === 1 ? "Demat" : `${count} Demat`} ${kind}`.trim());
}

/**
 * Descrizione, badge e tipo di voce di uno statusCode del catalogo.
 *
 * Un FINAL_EVENT è verde anche con esito KO: la spedizione si è conclusa, e
 * un'irreperibilità o una mancata consegna sono esiti legittimi, non guasti.
 */
export function describeStatus(statusCode) {
  const known = k.STATUS_CODES[statusCode];
  if (!known) {
    return { description: "descrizione non disponibile nel catalogo", badges: [], kind: "event-unknown" };
  }
  const [description, eventType, product, outcome] = known;
  return {
    description,
    badges: [eventType, `prodotto ${product}`],
    kind: eventType === "FINAL_EVENT" ? "event-ok" : `event-${outcome.toLowerCase()}`,
  };
}

/** Voci di timeline generate da una singola riga PaperTrackings. */
export function entriesForTracking(tracking, errors, dryRuns, logOutputs, wantFlow) {
  const trackingId = tracking.trackingId ?? "";
  const result = [];

  const createdAt = parseTs(tracking.createdAt);
  const ocr = (tracking.validationConfig ?? {}).ocrEnabled;
  result.push(entry(
    "milestone", createdAt, createdAt,
    "Tracking creato", trackingId,
    [tracking.productType, tracking.unifiedDeliveryDriver,
     tracking.processingMode, ocr ? `OCR ${ocr}` : null],
    tracking));

  for (const event of tracking.events ?? []) {
    const statusCode = event.statusCode ?? "?";
    const { description, badges: base, kind } = describeStatus(statusCode);
    const ownDescription = event.statusDescription;
    let subtitle = description;
    if (ownDescription && ownDescription.trim() && ownDescription !== description) {
      subtitle = `${description} · ${ownDescription}`;
    }
    const badges = [...base];
    if (event.deliveryFailureCause) badges.push(`causa ${event.deliveryFailureCause}`);
    if (event.registeredLetterCode) badges.push(`codice oggetto ${event.registeredLetterCode}`);
    badges.push(...attachmentBadges(event.attachments));
    if (event.dryRun) badges.push("dry-run");
    result.push(entry(
      kind,
      parseTs(event.statusTimestamp),
      parseTs(event.createdAt) ?? parseTs(event.requestTimestamp),
      statusCode, subtitle, badges, event, event.id ?? null, statusCode));
  }

  for (const error of errors) {
    const category = error.errorCategory ?? error.category ?? "?";
    const details = error.details ?? {};
    const cause = details.cause;
    const message = details.message;
    const bits = [k.ERROR_CATEGORIES[category] ?? "categoria non nel catalogo"];
    if (cause) bits.push(`${cause}: ${k.ERROR_CAUSES[cause] ?? "causa non nel catalogo"}`);
    if (message) bits.push(message);
    const moment = parseTs(error.created);
    const severity = (error.type ?? "ERROR").toUpperCase();
    result.push(entry(
      severity === "ERROR" ? "error" : "warning",
      moment, moment, category, bits.join(" — "),
      [severity, error.flowThrow, error.eventThrow ? `evento ${error.eventThrow}` : null],
      error, null, error.eventThrow ?? null));
  }

  for (const output of dryRuns) {
    const statusCode = output.statusCode ?? "?";
    const { description } = describeStatus(statusCode);
    const moment = parseTs(output.statusDateTime) ?? parseTs(output.created);
    result.push(entry(
      "dryrun", moment, parseTs(output.created),
      `output → delivery-push: ${statusCode}`,
      output.statusDescription ?? description,
      ["dry-run", output.statusDetail,
       output.deliveryFailureCause ? `causa ${output.deliveryFailureCause}` : null,
       ...attachmentBadges(output.attachments)],
      output));
  }

  for (const { moment, payload: output } of logOutputs) {
    const code = output.statusDetail ?? output.statusCode ?? "?";
    const { description } = describeStatus(code);
    result.push(entry(
      "output",
      parseTs(output.statusDateTime),
      moment,                        // quando il tracker l'ha scritto davvero
      `output → delivery-push: ${code}`,
      output.statusDescription ?? description,
      ["dai log", output.statusCode,
       output.deliveryFailureCause ? `causa ${output.deliveryFailureCause}` : null,
       ...attachmentBadges(output.attachments)],
      output));
  }

  if (wantFlow) {
    const flow = tracking.validationFlow ?? {};
    for (const [field, label] of k.FLOW_STEPS) {
      const moment = parseTs(flow[field]);
      if (moment) result.push(entry("milestone", moment, moment, label, field, [], flow));
    }

    for (const request of flow.ocrRequests ?? []) {
      // il tipo di documento va in chip, dove si legge a colpo d'occhio come
      // tutto il resto; il sottotitolo tiene la uri, che dice *quale* documento
      const kind = request.documentType;
      const uri = request.uri ?? "";
      const asked = parseTs(request.requestTimestamp);
      if (asked) {
        result.push(entry("ocr", asked, asked, "Richiesta OCR", uri,
                          [kind, request.attachmentEventId], request));
      }
      const answered = parseTs(request.responseTimestamp);
      if (answered) {
        const status = request.responseStatus ?? "?";
        result.push(entry("ocr", answered, answered, `Risposta OCR: ${status}`, uri,
                          [kind, request.finalEventId], request));
      }
    }
  }

  for (const item of result) item.trackingId = trackingId;
  return result;
}

/** L'id di un evento è il messageId SQS: se si ripete, la coda ha riconsegnato. */
export function markQueueRetries(entries) {
  const totals = counted(entries.map((item) => item.eventId).filter(Boolean));
  const seen = new Map();
  for (const item of entries) {
    const eventId = item.eventId;
    if (eventId && totals.get(eventId) > 1) {
      seen.set(eventId, (seen.get(eventId) ?? 0) + 1);
      item.retry = [seen.get(eventId), totals.get(eventId)];
    }
  }
}

/** Raggruppa per destinatario e ordina le voci cronologicamente. */
export function buildRecipients(trackings, errors, dryRuns, logOutputs, wantFlow, orderBy) {
  const grouped = new Map();
  for (const tracking of trackings) {
    const { recindex, attempt, pcretry } = k.parseTrackingId(tracking.trackingId ?? "");
    if (!grouped.has(recindex)) {
      grouped.set(recindex, { recindex, trackings: [], entries: [], tables: {} });
    }
    const bucket = grouped.get(recindex);
    bucket.trackings.push([attempt ?? 0, pcretry ?? 0, tracking]);
    // i link a DynamoDB escono solo per le tabelle che hanno davvero risposto
    const trackingId = tracking.trackingId;
    bucket.tables[trackingId] = {
      trackings: 1,
      errors: (errors[trackingId] ?? []).length,
      dry_run: (dryRuns[trackingId] ?? []).length,
    };
    bucket.entries.push(...entriesForTracking(
      tracking,
      errors[trackingId] ?? [],
      dryRuns[trackingId] ?? [],
      logOutputs[trackingId] ?? [],
      wantFlow));
  }

  const field = orderBy === "status" ? "statusAt" : "arrivalAt";
  const other = field === "statusAt" ? "arrivalAt" : "statusAt";
  for (const bucket of grouped.values()) {
    bucket.trackings.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    const when = (item) => item[field] ?? item[other] ?? FAR_FUTURE;
    bucket.entries.sort((a, b) => when(a) - when(b));
    markQueueRetries(bucket.entries);
  }
  // i destinatari in ordine di RECINDEX; chi non si è lasciato leggere va in fondo
  return [...grouped.values()].sort((a, b) =>
    (a.recindex === null) - (b.recindex === null) || a.recindex - b.recindex);
}
