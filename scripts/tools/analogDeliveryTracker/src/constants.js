/**
 * Costanti e cataloghi dell'Analog Delivery Tracker.
 *
 * Qui sta tutto ciò che dipende dalla configurazione degli ambienti PN o dagli
 * enum del microservizio: se cambiano quelli, si tocca solo questo file. I
 * cataloghi stanno a parte, in `catalogs.js`, perché sono dati e basta.
 */

export { STATUS_CODES, ERROR_CATEGORIES, ERROR_CAUSES, FLOW_STEPS } from "./catalogs.js";

// =============================================================================
// AWS
// =============================================================================
/** usata quando il profilo non dichiara una region propria */
export const DEFAULT_REGION = "eu-south-1";

/** prefisso delle tabelle DynamoDB (`${ProjectName}` in storage.yml) */
const TABLE_PREFIX = "pn";

export const TABLES = {
  trackings: `${TABLE_PREFIX}-PaperTrackings`,
  errors: `${TABLE_PREFIX}-PaperTrackingsErrors`,
  dry_run: `${TABLE_PREFIX}-PaperTrackerDryRunOutputs`,
};

/** indice su cui si interroga l'`attemptId` */
export const ATTEMPT_INDEX = "attemptId-pcRetry-index";

/**
 * log del microservizio. La query si fa sempre, quanto indietro sia l'evento:
 * oltre la retention del gruppo non fallisce, torna vuota.
 */
export const LOG_GROUP = "/aws/ecs/pn-paper-tracker";

/**
 * Diagrammi di flusso per prodotto, in percorso relativo alla pagina: titolo e
 * file, perché l'890 ne ha due — il flusso principale e il dettaglio di cosa
 * succede in giacenza, che nel primo è un solo riquadro. Manca un prodotto? Il
 * bottone non compare, senza bisogno di altro.
 */
export const DIAGRAMS = {
  AR: [["", "diagrams/AR.mermaid"]],
  890: [["Consegna", "diagrams/890.mermaid"],
        ["Giacenza", "diagrams/890_giacenza.mermaid"]],
  RIR: [["", "diagrams/RIR.mermaid"]],
  RS: [["", "diagrams/RS.mermaid"]],
  RIS: [["", "diagrams/RIS.mermaid"]],
};

/** quanto guardare prima e dopo l'evento, e il tetto alle righe riportate */
export const LOG_WINDOW_MINUTES = 5;
export const LOG_MAX_EVENTS = 200;

// =============================================================================
// Link alla console
// =============================================================================
// Nell'URL va solo la region: l'account è quello con cui sei già entrato in
// console, che non è detto sia quello del profilo scelto qui.

/**
 * L'escape dei frammenti della console: percent-encoding, con `$` per `%`.
 *
 * I nomi (log group, filtri) sono codificati due volte, i separatori una.
 */
function fragment(value, passes = 2) {
  let encoded = String(value);
  for (let i = 0; i < passes; i += 1) encoded = encodeURIComponent(encoded);
  return encoded.replaceAll("%", "$");
}

function consoleHost(region) {
  return `https://${region}.console.aws.amazon.com`;
}

/**
 * L'esploratore di item, in query sulla partition key.
 *
 * Query e non item singolo: `trackingId` è chiave intera solo in
 * PaperTrackings, mentre Errors e DryRunOutputs hanno anche `created`, e per
 * quelle la risposta è più di un record.
 *
 * La forma del frammento non è documentata da AWS: è quella che la console
 * produce da sé. Se cambia si finisce sulla tabella, non su un errore, e il
 * link resta copiabile.
 */
export function dynamoUrl(table, region, key = null) {
  const home = `${consoleHost(region)}/dynamodbv2/home?region=${region}`;
  const params = [`table=${encodeURIComponent(table)}`, "maximize=true"];
  if (key) params.push("operation=QUERY", `pk=${encodeURIComponent(key)}`);
  return `${home}#item-explorer?${params.join("&")}`;
}

/**
 * Il log group filtrato, e se si sa quando, già sulla finestra giusta.
 *
 * `start` e `end` sono millisecondi epoch. Senza, la console apre
 * sull'intervallo di default e per un evento di ieri non trova niente: il link
 * sembra rotto anche quando i log ci sono.
 */
export function logsUrl(filterPattern, region, start = null, end = null) {
  const equals = fragment("=", 1);
  const ampersand = fragment("&", 1);
  const query = [];
  if (start != null && end != null) {
    query.push(`start${equals}${Math.trunc(start)}`, `end${equals}${Math.trunc(end)}`);
  }
  query.push(`filterPattern${equals}${fragment(`"${filterPattern}"`)}`);
  return `${consoleHost(region)}/cloudwatch/home?region=${region}` +
         `#logsV2:log-groups/log-group/${fragment(LOG_GROUP)}` +
         `/log-events${fragment("?", 1)}${query.join(ampersand)}`;
}

/**
 * chiavi di localStorage: l'ultimo profilo scelto e quante volte ognuno è stato
 * usato. L'ordine della tendina si impara dall'uso, perché i nomi dei profili
 * dipendono dalla configurazione di chi apre la pagina.
 */
export const LAST_PROFILE_KEY = "analogDeliveryTracker.lastProfile";
export const PROFILE_USAGE_KEY = "analogDeliveryTracker.profileUsage";

// =============================================================================
// Composizione del trackingId
// =============================================================================
// Il trackingId è `<attemptId>.PCRETRY_<n>` e cambia forma con il prodotto: i
// prodotti con feedback multipli hanno anche il tentativo, gli altri no. Le
// comunicazioni bonarie hanno un prefisso tutto loro e chiudono l'attemptId con
// il tipo di recapito; esistono solo come RS.
export const TRACKING_SCHEMES = {
  ANALOG_DOMICILE: { prefix: "PREPARE_ANALOG_DOMICILE", hasAttempt: true },
  SIMPLE_REGISTERED_LETTER: { prefix: "PREPARE_SIMPLE_REGISTERED_LETTER", hasAttempt: false },
  ANALOG_MESSAGE: { prefix: "PREPARE_ANALOG_MESSAGE", hasAttempt: true, suffix: "DELIVERYTYPE_RS" },
};

/** prefisso che distingue una comunicazione bonaria */
const BONARIE_PREFIX = TRACKING_SCHEMES.ANALOG_MESSAGE.prefix;

/** [valore, etichetta, schemi da interrogare] — il primo è il default */
export const PRODUCTS = [
  ["AUTO", "Tutti i prodotti",
   ["ANALOG_DOMICILE", "SIMPLE_REGISTERED_LETTER", "ANALOG_MESSAGE"]],
  ["AR", "AR — Raccomandata A/R", ["ANALOG_DOMICILE"]],
  ["890", "890 — Notifiche a mezzo posta", ["ANALOG_DOMICILE"]],
  ["RIR", "RIR — Internazionale A/R", ["ANALOG_DOMICILE"]],
  ["RS", "RS — Raccomandata semplice", ["SIMPLE_REGISTERED_LETTER", "ANALOG_MESSAGE"]],
  ["RIS", "RIS — Internazionale semplice", ["SIMPLE_REGISTERED_LETTER"]],
];

export const PRODUCT_SCHEMES = Object.fromEntries(
  PRODUCTS.map(([value, , schemes]) => [value, schemes]));

/** quanti destinatari e quanti tentativi provare prima di arrendersi */
export const MAX_RECINDEX = 9;
export const MAX_ATTEMPT = 5;

/** riconosce tutte le forme; `ATTEMPT` e `DELIVERYTYPE` sono opzionali */
export const TRACKING_RE =
  /^(?<prefix>[A-Z_]+)\.IUN_(?<iun>.+?)\.RECINDEX_(?<rec>\d+)(?:\.ATTEMPT_(?<att>\d+))?(?:\.DELIVERYTYPE_(?<delivery>[A-Z0-9]+))?\.PCRETRY_(?<pc>\d+)$/;

/** `attemptId` per lo schema indicato: è la partition key di ATTEMPT_INDEX. */
export function buildAttemptId(scheme, iun, recindex, attempt = 0) {
  const config = TRACKING_SCHEMES[scheme];
  const parts = [config.prefix, `IUN_${iun}`, `RECINDEX_${recindex}`];
  if (config.hasAttempt) parts.push(`ATTEMPT_${attempt}`);
  if (config.suffix) parts.push(config.suffix);
  return parts.join(".");
}

export function hasAttempt(scheme) {
  return TRACKING_SCHEMES[scheme].hasAttempt;
}

/** Vero per le comunicazioni bonarie, riconoscibili dal prefisso. */
export function isBonaria(trackingId) {
  return (trackingId ?? "").startsWith(`${BONARIE_PREFIX}.`);
}

/** `{recindex, attempt, pcretry}`; `attempt` è `null` per RS e RIS. */
export function parseTrackingId(trackingId) {
  const found = TRACKING_RE.exec(trackingId ?? "");
  if (!found) return { recindex: null, attempt: null, pcretry: null };
  const { rec, att, pc } = found.groups;
  return {
    recindex: Number(rec),
    attempt: att === undefined ? null : Number(att),
    pcretry: Number(pc),
  };
}
