/**
 * Lettura dei dati del tracker: DynamoDB, CloudWatch e la loro decodifica.
 *
 * Non sa niente della pagina: riceve i client dell'SDK, restituisce strutture
 * JavaScript. Le credenziali non le vede: quelle stanno dentro i client.
 */

import * as k from "./constants.js";
import { sdk } from "./sdk.js";

// =============================================================================
// Decodifica e date
// =============================================================================

/** Da AttributeValue DynamoDB a tipo JavaScript. */
export function unwrap(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return value;
  const entries = Object.entries(value);
  if (entries.length !== 1) return value;
  const [kind, raw] = entries[0];
  switch (kind) {
    case "S": return raw;
    // in DynamoDB i numeri viaggiano come stringhe per non perdere precisione
    case "N": return Number(raw);
    case "BOOL": return Boolean(raw);
    case "NULL": return null;
    case "M": return Object.fromEntries(
      Object.entries(raw).map(([key, item]) => [key, unwrap(item)]));
    case "L": return raw.map(unwrap);
    case "SS": case "NS": case "BS": return [...raw];
    default: return raw;
  }
}

export function unwrapItem(item) {
  return Object.fromEntries(Object.entries(item).map(([key, value]) => [key, unwrap(value)]));
}

/**
 * ISO-8601 -> Date. `Instant` di Java può avere i nanosecondi, che `Date` non
 * digerisce: si tagliano ai millisecondi.
 */
export function parseTs(value) {
  if (!value || typeof value !== "string") return null;
  const text = value.trim().replace(/\.(\d{1,9})/, (_, digits) => `.${digits.slice(0, 3)}`);
  const parsed = new Date(/[Zz]|[+-]\d{2}:?\d{2}$/.test(text) ? text : `${text}Z`);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

/**
 * Come `datetime.isoformat()`: offset esplicito e frazione a sei cifre.
 *
 * Serve nei `data-at`, che `tracker` rilegge per calcolare la finestra dei log.
 * `toISOString()` scriverebbe `Z` e tre decimali: qui l'offset è esplicito e la
 * frazione ha sei cifre, come la scrive un `Instant`.
 */
export function isoUtc(moment) {
  const text = moment.toISOString();
  return text.endsWith(".000Z")
    ? `${text.slice(0, -5)}+00:00`
    : `${text.slice(0, -1)}000+00:00`;
}

export function fmtTs(moment) {
  if (!moment) return "—";
  return `${moment.toISOString().slice(0, 19).replace("T", " ")} UTC`;
}

/** più avanti di qualunque data vera: serve a mandare in coda chi non ne ha */
export const FAR_FUTURE = new Date(8640000000000000);

// =============================================================================
// Accesso a DynamoDB
// =============================================================================

/** Query paginata, già deserializzata. */
async function queryAll(ddb, input) {
  const { dynamodb } = await sdk();
  const items = [];
  let startKey;
  do {
    const answer = await ddb.send(
      new dynamodb.QueryCommand({ ...input, ExclusiveStartKey: startKey }));
    items.push(...(answer.Items ?? []));
    startKey = answer.LastEvaluatedKey;
  } while (startKey);
  return items.map(unwrapItem);
}

function queryTrackings(ddb, attemptId) {
  return queryAll(ddb, {
    TableName: k.TABLES.trackings,
    IndexName: k.ATTEMPT_INDEX,
    KeyConditionExpression: "attemptId = :a",
    ExpressionAttributeValues: { ":a": { S: attemptId } },
  });
}

export function queryByTrackingId(ddb, table, trackingId) {
  return queryAll(ddb, {
    TableName: table,
    KeyConditionExpression: "trackingId = :t",
    ExpressionAttributeValues: { ":t": { S: trackingId } },
  });
}

/**
 * Tutti i tracking dello IUN per uno schema di trackingId.
 *
 * Destinatari e tentativi sono numerati da 0 senza salti, quindi si sale finché
 * si trova e ci si ferma al primo vuoto: una query per ogni
 * `(RECINDEX, ATTEMPT)`, più una che chiude ciascun livello.
 *
 * Il PCRETRY non si cerca: l'`attemptId` è la partition key dell'indice, e una
 * query restituisce già tutti i retry di quel tentativo.
 */
export async function scanScheme(ddb, scheme, iun, maxRec, maxAtt, say) {
  const found = [];
  for (let rec = 0; rec <= maxRec; rec += 1) {
    if (!k.hasAttempt(scheme)) {
      say(`${scheme} · RECINDEX_${rec}`);
      const rows = await queryTrackings(ddb, k.buildAttemptId(scheme, iun, rec));
      if (!rows.length) break;
      found.push(...rows);
      continue;
    }

    let foundForRec = false;
    for (let att = 0; att <= maxAtt; att += 1) {
      say(`${scheme} · RECINDEX_${rec} · ATTEMPT_${att}`);
      const rows = await queryTrackings(ddb, k.buildAttemptId(scheme, iun, rec, att));
      if (!rows.length) break;
      found.push(...rows);
      foundForRec = true;
    }
    if (!foundForRec) break;
  }
  return found;
}

/** I tracking dello IUN, ricostruendo i trackingId per ogni schema. */
export async function findByIun(ddb, schemes, iun, maxRec, maxAtt, say) {
  const trackings = [];
  for (const scheme of schemes) {
    trackings.push(...await scanScheme(ddb, scheme, iun, maxRec, maxAtt, say));
  }
  return trackings;
}

/** Errori e output dry-run dei tracking dati. */
export async function collect(ddb, trackings, wantDryRun, say) {
  const errors = {};
  const dryRuns = {};
  for (const tracking of trackings) {
    const trackingId = tracking.trackingId;
    say(`Errori di ${trackingId}`);
    errors[trackingId] = await queryByTrackingId(ddb, k.TABLES.errors, trackingId);
    if (wantDryRun) {
      say(`Output dry-run di ${trackingId}`);
      dryRuns[trackingId] = await queryByTrackingId(ddb, k.TABLES.dry_run, trackingId);
    }
  }
  return { errors, dryRuns };
}

// =============================================================================
// Log CloudWatch
// =============================================================================

/** Filtro CloudWatch: più termini fra virgolette valgono come AND. */
export function terms(...values) {
  return values.filter(Boolean).map((value) => `"${value}"`).join(" ");
}

/**
 * Righe di log fra due istanti che corrispondono al filtro.
 *
 * Pagina finché c'è un `nextToken`: `filterLogEvents` restituisce pagine vuote
 * mentre scandaglia il gruppo, e fermarsi alla prima direbbe «nessun log» pur
 * avendone.
 */
export async function fetchLogs(logs, pattern, start, end, limit = null, maxPages = 20) {
  const { cloudwatch } = await sdk();
  const cap = limit ?? k.LOG_MAX_EVENTS;
  const events = [];
  let token;
  for (let page = 0; page < maxPages; page += 1) {
    const answer = await logs.send(new cloudwatch.FilterLogEventsCommand({
      logGroupName: k.LOG_GROUP,
      startTime: start.getTime(),
      endTime: end.getTime(),
      filterPattern: pattern,
      limit: cap - events.length,
      nextToken: token,
    }));
    events.push(...(answer.events ?? []));
    token = answer.nextToken;
    if (!token || events.length >= cap) break;
  }
  return events;
}

/**
 * Gli intervalli ±minuti attorno agli istanti dati, fusi se si toccano.
 *
 * Gli eventi di una spedizione arrivano a grappoli: una query per grappolo
 * invece di una per evento, senza allargarsi ai vuoti in mezzo.
 */
export function mergeWindows(moments, minutes) {
  const window = minutes * 60 * 1000;
  const merged = [];
  for (const moment of [...moments].sort((a, b) => a - b)) {
    const start = new Date(moment.getTime() - window);
    const end = new Date(moment.getTime() + window);
    const last = merged.at(-1);
    if (last && start <= last[1]) {
      last[1] = end > last[1] ? end : last[1];
    } else {
      merged.push([start, end]);
    }
  }
  return merged;
}

/** Le righe già scaricate che cadono nella finestra, se ci sono. */
export function cachedLogs(cache, trackingId, start, end) {
  const events = cache[trackingId];
  if (events === undefined) return null;
  return events.filter((item) =>
    item.timestamp >= start.getTime() && item.timestamp <= end.getTime());
}

const OUTPUT_MARKER = "Sending to output target for event: ";

/**
 * la riga che annuncia la lavorazione di un evento: è l'unica che porta sempre
 * il trace, sotto la chiave `trace_id` (altrove compare come `traceId`).
 */
const HANDLING_MARKER = "Handling ";

/**
 * ripiego, quando la riga di lavorazione non c'è: la presa in carico dalla
 * coda. Il nome della coda fa parte del marcatore, perché nella stessa finestra
 * c'è anche l'hop a monte, "…_to_paper_channel".
 */
const INTAKE_MARKER = "Handle message from pn-external_channel_to_paper_tracker";

/**
 * Dove il tracker prende in carico l'evento, o `null`.
 *
 * Prima di quella riga ci sono le righe dell'hop a monte, che non riguardano la
 * lavorazione che stiamo guardando.
 */
export function intakeIndex(logEvents) {
  for (const [index, item] of logEvents.entries()) {
    const { body } = parseLogLine(item);
    if (String(body.message ?? "").startsWith(INTAKE_MARKER)) return index;
  }
  return null;
}

/** Il trace della riga «Handling … and event: [CODE]», che lo porta sempre. */
export function handlingTrace(logEvents, code) {
  const needle = `and event: [${code}]`;
  for (const item of logEvents) {
    const { body } = parseLogLine(item);
    const message = String(body.message ?? "");
    if (message.startsWith(HANDLING_MARKER) && message.includes(needle)) {
      return body.trace_id ?? body.traceId ?? null;
    }
  }
  return null;
}

/**
 * Le righe che nominano trackingId e codice.
 *
 * Se il prefetch ha già scaricato la finestra, si filtra quella in memoria:
 * CloudWatch fa una sottostringa sul messaggio, che è quello che facciamo qui.
 */
async function narrowLogs(logs, cache, trackingId, code, start, end) {
  const cached = cachedLogs(cache, trackingId, start, end);
  if (cached !== null) return cached.filter((item) => item.message.includes(code));
  return fetchLogs(logs, terms(trackingId, code), start, end);
}

/**
 * `{events, filter}` per una voce di timeline.
 *
 * Senza codice si guarda tutto ciò che nomina il trackingId. Con un codice si
 * cerca la riga che ne annuncia la lavorazione, se ne prende il trace e si
 * riparte da quello: così escono anche le righe che il trackingId non lo
 * scrivono, cioè quasi tutta la lavorazione.
 */
export async function logsForEntry(logs, cache, trackingId, code, start, end) {
  if (!code) {
    const cached = cachedLogs(cache, trackingId, start, end);
    if (cached !== null) return { events: cached, filter: "filtro sul trackingId" };
    return {
      events: await fetchLogs(logs, terms(trackingId), start, end),
      filter: "filtro sul trackingId",
    };
  }

  const narrow = await narrowLogs(logs, cache, trackingId, code, start, end);
  const trace = handlingTrace(narrow, code);
  if (trace) {
    return {
      events: await fetchLogs(logs, terms(trace), start, end),
      filter: `trace ${trace}`,
    };
  }

  // nessuna riga di lavorazione: si tiene il primo passo, ma dalla presa in
  // carico in giù, così l'hop a monte resta comunque fuori
  const intake = intakeIndex(narrow);
  if (intake === null) {
    return { events: narrow, filter: `filtro su trackingId e ${code}` };
  }
  return {
    events: narrow.slice(intake),
    filter: `filtro su trackingId e ${code}, dalla presa in carico`,
  };
}

/** `{moment, body}` di una riga: i log sono JSON di logstash. */
export function parseLogLine(item) {
  let body;
  try {
    body = JSON.parse(item.message);
  } catch {
    body = {};
  }
  const moment = parseTs(body["@timestamp"]) ?? new Date(item.timestamp);
  return { moment, body };
}

/**
 * Gli eventi spediti a delivery-push, con l'istante della riga che li porta.
 *
 * In dry-run finiscono in `PaperTrackerDryRunOutputs`; fuori, la riga di log è
 * l'unica traccia che resta, e il suo istante è il momento in cui il tracker li
 * ha prodotti davvero — più preciso di qualunque data dentro il payload.
 */
export function parseOutputEvents(logEvents) {
  const found = [];
  for (const item of logEvents) {
    const { moment, body } = parseLogLine(item);
    const text = body.message ?? item.message;
    const at = text.indexOf(OUTPUT_MARKER);
    if (at < 0) continue;
    const payload = decodeLeadingJson(text.slice(at + OUTPUT_MARKER.length).trim());
    if (payload !== undefined) found.push({ moment, payload });
  }
  return found;
}

/**
 * Il primo oggetto JSON di una stringa, ignorando quel che segue.
 *
 * `JSON.parse` vuole che la stringa finisca lì, mentre la riga di log continua
 * con altro testo: il confine va trovato contando le graffe fuori dalle
 * stringhe.
 */
function decodeLeadingJson(text) {
  if (text[0] !== "{") return undefined;
  let depth = 0;
  let inString = false;
  let escaped = false;
  for (let at = 0; at < text.length; at += 1) {
    const character = text[at];
    if (escaped) { escaped = false; continue; }
    if (character === "\\") { escaped = true; continue; }
    if (character === '"') { inString = !inString; continue; }
    if (inString) continue;
    if (character === "{") depth += 1;
    else if (character === "}") {
      depth -= 1;
      if (depth === 0) {
        try {
          return JSON.parse(text.slice(0, at + 1));
        } catch {
          return undefined;
        }
      }
    }
  }
  return undefined;
}

/**
 * Per ogni tracking scarica i log attorno ai suoi eventi e ne cava gli output.
 *
 * Le righe restano in `cache`: il modale di una voce le trova già pronte invece
 * di richiedere le stesse a CloudWatch.
 */
export async function collectLogOutputs(logs, cache, trackings, minutes, say) {
  const outputs = {};
  for (const tracking of trackings) {
    const trackingId = tracking.trackingId;
    const moments = [parseTs(tracking.createdAt),
                     ...(tracking.events ?? []).map((event) => parseTs(event.statusTimestamp))]
      .filter(Boolean);
    if (!moments.length) continue;
    say(`Log di ${trackingId}`);
    const events = [];
    for (const [start, end] of mergeWindows(moments, minutes)) {
      events.push(...await fetchLogs(logs, terms(trackingId), start, end));
    }
    cache[trackingId] = events;
    outputs[trackingId] = parseOutputEvents(events);
  }
  return outputs;
}
