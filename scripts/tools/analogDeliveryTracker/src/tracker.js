/**
 * Storico di una spedizione analogica a partire dallo IUN.
 *
 * Legge le tabelle DynamoDB di pn-paper-tracker e costruisce, per ogni
 * destinatario della notifica, un'unica timeline che unisce gli eventi
 * ricevuti, gli errori generati dalle validazioni, le tappe del flusso e gli
 * output prodotti.
 *
 * Questo modulo è la pagina: stato, gestori degli eventi e avvio. Le query
 * stanno in `data`, la costruzione della timeline in `timeline`, l'HTML in
 * `view`.
 */

import * as awsDir from "./aws_dir.js";
import { clients } from "./sdk.js";
import * as k from "./constants.js";
import * as data from "./data.js";
import * as diagram from "./diagram.js";
import * as timeline from "./timeline.js";
import * as view from "./view.js";
import { escape } from "./html.js";

// =============================================================================
// Pagina
// =============================================================================

function el(elementId) {
  return document.getElementById(elementId);
}

// Mermaid arriva come UMD, per poterlo servire con `integrity`: definisce
// `window.mermaid` e va solo configurato. Qui e non in un <script> inline,
// così la CSP può fare a meno di 'unsafe-inline'.
window.mermaid.initialize({ startOnLoad: false });

/**
 * Una region AWS è fatta così e basta.
 *
 * Senza questo controllo un `?region=attacker.com/` finisce in
 * `https://attacker.com/.console.aws.amazon.com/…`, cioè nei link che la pagina
 * offre da cliccare: non è XSS — lo schema è fisso — ma è una finta console a un
 * clic di distanza, e l'URL si porta dietro IUN e account. L'SDK si difende da
 * sé, rifiutando le region che non sono hostname validi; questi link no.
 */
const REGION_RE = /^[a-z0-9-]+$/;

const state = {
  aws: null, ddb: null, logs: null,
  logCache: {}, entryLogs: new Map(), payloads: new Map(), detailJson: null,
  region: null, account: null,
  // quello che l'URL chiede e che si può onorare solo più tardi: il profilo
  // esiste quando la cartella .aws è letta, la region al collegamento
  wantedAccount: null, wantedRegion: null,
};

function say(elementId, message, tone = "text-slate-500") {
  const node = el(elementId);
  node.className = `mt-3 text-sm ${tone}`;
  node.innerText = message;
}

/**
 * Come `say`, ma coi due gesti che servono dopo un errore.
 *
 * Le righe rientrate sono comandi, e escono con un bottone «copia»; in coda
 * «Rileggi la cartella», da premere una volta eseguito il comando.
 */
function sayError(elementId, message) {
  const node = el(elementId);
  node.className = "mt-3 text-sm text-rose-600";
  const blocks = [];
  for (const line of message.split("\n")) {
    if (line.startsWith("  ") && line.trim()) {
      const command = escape(line.trim());
      blocks.push(
        '<span class="mt-1 flex flex-wrap items-center gap-2">' +
        '<code class="rounded bg-rose-50 px-2 py-1 font-mono text-xs ' +
        `text-rose-900">${command}</code>` +
        `<button type="button" data-copy="${command}" ` +
        'class="shrink-0 cursor-pointer rounded border border-rose-200 px-2 py-1 text-xs ' +
        'text-rose-700 hover:bg-rose-50">copia</button></span>');
    } else {
      blocks.push(`<span class="block">${escape(line)}</span>`);
    }
  }
  blocks.push(
    '<span class="mt-2 block"><button type="button" data-reload ' +
    'class="cursor-pointer rounded bg-brand px-3 py-1.5 text-xs font-medium text-white ' +
    'hover:bg-blue-900">Rileggi la cartella</button></span>');
  node.innerHTML = blocks.join("");
}

/**
 * Copia negli appunti, con la strada vecchia come riserva.
 *
 * `navigator.clipboard` non è concesso ai sotto-frame di altra origine se
 * l'iframe non porta `allow="clipboard-write"`: là resta solo `execCommand`,
 * che vuole il testo dentro un elemento e selezionato.
 */
async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // si tenta la strada vecchia
  }

  const node = document.createElement("textarea");
  node.value = text;
  node.setAttribute("readonly", "");
  node.style.position = "fixed";
  node.style.opacity = "0";
  document.body.appendChild(node);
  try {
    node.select();
    return Boolean(document.execCommand("copy"));
  } catch {
    return false;
  } finally {
    node.remove();
  }
}

/** localStorage non è disponibile ovunque (file://, modalità restrittive). */
function readSetting(key) {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeSetting(key, value) {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // niente memoria: la pagina funziona lo stesso
  }
}

function rememberedProfile() {
  return readSetting(k.LAST_PROFILE_KEY);
}

/** `{profilo: quante volte è stato usato}`, per ordinare la tendina. */
function profileUsage() {
  const raw = readSetting(k.PROFILE_USAGE_KEY);
  try {
    const counts = raw ? JSON.parse(raw) : {};
    return counts && typeof counts === "object" && !Array.isArray(counts) ? counts : {};
  } catch {
    return {};
  }
}

function rememberProfile(name) {
  writeSetting(k.LAST_PROFILE_KEY, name);
  const counts = profileUsage();
  counts[name] = Number(counts[name] ?? 0) + 1;
  writeSetting(k.PROFILE_USAGE_KEY, JSON.stringify(counts));
}

/** Ultimo usato in cima, poi i più usati, poi in ordine alfabetico. */
function compareProfiles(a, b, lastUsed, counts) {
  return (a !== lastUsed) - (b !== lastUsed) ||
         Number(counts[b] ?? 0) - Number(counts[a] ?? 0) ||
         a.localeCompare(b);
}

/** status: `off` | `ok` | `warn` | `ko`. */
function setConnection(status, text, detail = "") {
  const palette = {
    off: ["bg-slate-400", "border-slate-200 bg-slate-50 text-slate-600"],
    ok: ["bg-emerald-500", "border-emerald-200 bg-emerald-50 text-emerald-800"],
    warn: ["bg-amber-500", "border-amber-200 bg-amber-50 text-amber-800"],
    ko: ["bg-rose-500", "border-rose-200 bg-rose-50 text-rose-800"],
  };
  const [dot, pill] = palette[status];
  el("conn-dot").className = `h-2 w-2 rounded-full ${dot}`;
  el("conn-pill").className = "inline-flex items-center gap-2 rounded-full border px-3 py-1.5 " +
    `text-xs font-medium ${pill}`;
  el("conn-pill").title = detail || text;
  el("conn-text").innerText = text;
}

/**
 * Tiene `--page-header` allineata alla barra in cima alla pagina.
 *
 * Le intestazioni dei tracking ci si fermano sotto, e quella barra cambia
 * altezza quando va a capo: meglio misurarla che indovinarne i pixel.
 */
function trackHeaderHeight() {
  const bar = document.querySelector("header");
  const apply = () => {
    document.documentElement.style.setProperty("--page-header", `${bar.offsetHeight}px`);
  };
  apply();
  new ResizeObserver(apply).observe(bar);
}

/**
 * Riempie i campi dai parametri dell'URL, per poter condividere una ricerca.
 *
 * Il profilo non si sceglie qui: la cartella `.aws` non è ancora stata letta.
 * Account e region restano da parte, li onorano `fillProfiles` e `connect`.
 */
function applyUrlParams() {
  const params = new URLSearchParams(window.location.search);
  const tracking = params.get("trackingId");
  const iun = params.get("iun");
  if (tracking || iun) {
    el("sel-mode").value = tracking ? "tracking" : "iun";
    el("inp-iun").value = tracking || iun;
  }
  const product = params.get("product");
  if (product && Object.hasOwn(k.PRODUCT_SCHEMES, product)) el("sel-product").value = product;
  state.wantedAccount = params.get("account");
  const region = params.get("region");
  state.wantedRegion = region && REGION_RE.test(region) ? region : null;
}

/** Rimette la ricerca nell'URL, così si può condividere o salvare. */
function rememberSearch(needle, byTracking) {
  const params = new URLSearchParams();
  params.set(byTracking ? "trackingId" : "iun", needle);
  if (!byTracking) params.set("product", el("sel-product").value);
  for (const [key, value] of [["account", state.account], ["region", state.region]]) {
    if (value) params.set(key, value);
  }
  try {
    window.history.replaceState(null, "", `${window.location.pathname}?${params}`);
  } catch {
    // iframe sandboxed: l'URL non si tocca, la ricerca sì
  }
}

function connectedRegion() {
  return state.region || k.DEFAULT_REGION;
}

/**
 * Sceglie la credenziale attiva del profilo e ne ricava i client.
 *
 * `aws_dir` si ferma alla credenziale; i client li fa `sdk.js`, ed è lì che
 * l'SDK viene scaricato — cioè adesso, non all'apertura della pagina.
 */
async function connect() {
  const aws = state.aws;
  const profile = el("sel-profile").value;
  state.ddb = null;
  state.logs = null;
  el("btn-search").disabled = true;

  let source;
  let region;
  try {
    // senza requireAccount un profilo a cui non sei loggato ripiegherebbe, in
    // silenzio, sulle credenziali di un altro account
    source = aws.bestSource(profile, { requireAccount: true });
    region = state.wantedRegion || aws.regionFor(profile) || k.DEFAULT_REGION;
    state.wantedRegion = null;          // vale solo per il primo collegamento
    ({ ddb: state.ddb, logs: state.logs } = await clients(source.credentials, region));
    state.region = region;
  } catch (error) {
    const mismatch = error instanceof awsDir.AccountMismatch;
    setConnection("ko", mismatch ? "Account diverso" : "Credenziali non valide", error.message);
    sayError("search-msg", error.message);
    return;
  }

  rememberProfile(profile);
  state.account = source.account;
  el("btn-search").disabled = false;
  const remaining = source.expiresIn;
  setConnection("ok", `${region} · ${source.account || "account ignoto"}`,
                `${profile} — ${source.describe()} — scadenza: ` +
                (remaining === null ? "senza scadenza" : awsDir.humanMinutes(remaining)));
  say("search-msg", "");
}

async function fillProfiles() {
  const aws = state.aws;
  const lastUsed = rememberedProfile();
  const counts = profileUsage();
  const names = aws.profileNames().sort((a, b) => compareProfiles(a, b, lastUsed, counts));
  if (!names.length) {
    setConnection("ko", "Nessun profilo", "Il config letto non contiene profili");
    say("search-msg", "La cartella è stata letta, ma il suo 'config' non " +
        "contiene nessun profilo.", "text-rose-600");
    return;
  }

  const select = el("sel-profile");
  // la classifica ricorda solo i profili che si sono connessi: senza questo,
  // una rilettura sposterebbe la scelta proprio mentre ne stai sistemando uno
  const current = select.value;
  let chosen = names.includes(current) ? current : names[0];

  // l'URL chiede un account, non un profilo: qui si traduce, e fra i profili
  // che puntano a quell'account vince il primo in classifica
  const wanted = state.wantedAccount;
  state.wantedAccount = null;
  if (wanted) {
    const match = names.find((name) => aws.expectedAccount(name) === wanted);
    if (match) chosen = match;
  }
  select.innerHTML = names.map((name) =>
    `<option value="${escape(name)}"${name === chosen ? " selected" : ""}>` +
    `${escape(name)}</option>`).join("");
  select.disabled = false;
  await connect();
}

/** Schemi di trackingId del prodotto scelto; in dubbio, li prova tutti. */
function selectedSchemes() {
  const chosen = el("sel-product").value;
  return Object.hasOwn(k.PRODUCT_SCHEMES, chosen)
    ? k.PRODUCT_SCHEMES[chosen] : k.PRODUCT_SCHEMES.AUTO;
}

function searchingByTracking() {
  return el("sel-mode").value === "tracking";
}

/** Adatta i campi che hanno senso solo quando si parte dallo IUN. */
function applyMode() {
  const byTracking = searchingByTracking();
  el("inp-label").innerText = byTracking ? "trackingId" : "IUN";
  el("inp-iun").placeholder = byTracking
    ? `${k.buildAttemptId("ANALOG_DOMICILE", "<IUN>", 0, 0)}.PCRETRY_0`
    : "UWXU-VKXM-QVJZ-202609-M-1";
  for (const elementId of ["sel-product", "inp-max-rec", "inp-max-att"]) {
    el(elementId).disabled = byTracking;
  }
}

// =============================================================================
// Eventi
// =============================================================================

/**
 * Rilegge la cartella e riconnette, senza ricaricare la pagina.
 *
 * È il gesto da fare dopo un `aws sso login`: il selettore non riappare.
 */
async function reloadDirectory() {
  try {
    state.aws = await awsDir.AwsDirectory.pick();
  } catch (error) {
    setConnection("ko", "Cartella non letta", error.message);
    sayError("search-msg", error.message);
    return;
  }
  await fillProfiles();
}

el("btn-pick").addEventListener("click", reloadDirectory);

/** Delega: i bottoni dell'errore nascono dopo, quindi si ascolta il contenitore. */
el("search-msg").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-copy], [data-reload]");
  if (!button) return;
  if (button.hasAttribute("data-reload")) {
    await reloadDirectory();
    return;
  }
  button.innerText = await copyText(button.dataset.copy) ? "copiato" : "copia a mano";
});

/**
 * Apre il modale, che è uno solo per JSON, log e diagrammi.
 *
 * `wide` allarga la finestra: un diagramma di flusso è largo quanto tutta la
 * macchina a stati, mentre un JSON o una riga di log stanno in una colonna.
 */
function openDetail(title, subtitle, bodyHtml, wide = false) {
  el("detail-modal-title").innerText = title;
  el("detail-modal-sub").innerText = subtitle;
  el("detail-modal-body").innerHTML = bodyHtml;
  el("detail-modal").classList.toggle("wide", wide);
  el("detail-modal").showModal();
}

/** Il diagramma del prodotto, con evidenziati gli eventi di quel tracking. */
async function showDiagram(product, codes, business) {
  const arrived = codes.split(",").filter(Boolean);
  openDetail(`Diagramma ${product}`, arrived.join(", ") || "nessun evento",
             '<p class="text-xs text-slate-500">Disegno il diagramma…</p>', true);
  const body = el("detail-modal-body");
  try {
    // Il look "neo" misura le etichette col font "Recursive Variable", che
    // arriva dalla rete: disegnare prima che sia pronto vuol dire squadrare i
    // riquadri su Arial e ritrovarsi i titoli tagliati quando il font subentra.
    // Il primo await lo chiede, il secondo aspetta che tutti i font siano a posto.
    await document.fonts.load('16px "Recursive Variable"');
    await document.fonts.ready;

    const drawings = [];
    for (const [index, [title, path]] of k.DIAGRAMS[product].entries()) {
      const source = await (await fetch(path)).text();
      const drawn = await window.mermaid.render(
        `diagram-svg-${index}`, diagram.highlight(source, arrived, business));
      drawings.push(
        (title ? '<h3 class="mb-1 text-xs font-semibold uppercase tracking-wide ' +
                 `text-slate-500">${escape(title)}</h3>` : "") +
        `<div class="diagram-fit">${drawn.svg}</div>`);
    }

    body.innerHTML =
      '<div class="max-h-[86vh] space-y-6 overflow-y-auto">' + drawings.join("") + "</div>" +
      '<p class="mt-2 text-xs text-slate-500">In verde gli stati raggiunti; ' +
      'un ✅ sulle frecce percorse e sul fascicolo chiuso, ❌ se si è ' +
      'chiuso in KO.</p>';
  } catch (error) {
    body.innerHTML = failure(error);
  }
}

/** Delega: i risultati sono riscritti a ogni ricerca. */
el("results").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-detail], [data-copy], [data-collapse]");
  if (!button) return;

  if (button.hasAttribute("data-collapse")) {
    // le voci di un tentativo si possono chiudere: con dieci retry davanti,
    // scorrere fino a quello che interessa è il lavoro più noioso della pagina
    const open = button.getAttribute("aria-expanded") === "true";
    button.setAttribute("aria-expanded", String(!open));
    const section = button.closest("section");
    section.querySelector("ol").hidden = open;
    // nascondere la lista non toglie il margine dell'intestazione né il padding
    // della sezione: senza questa classe, fra un tentativo chiuso e il prossimo
    // resterebbe una fascia vuota
    section.classList.toggle("collapsed", open);
    return;
  }

  if (button.hasAttribute("data-copy")) {
    if (await copyText(button.dataset.copy)) {
      button.innerHTML = view.icon("check");
      button.title = "copiato";
    } else {
      button.title = "copia non riuscita: selezionalo a mano";
    }
    return;
  }

  if (button.dataset.detail === "diagram") {
    await showDiagram(button.dataset.product, button.dataset.codes, button.dataset.business);
    return;
  }

  if (button.dataset.detail === "json") {
    const [title, payload] = state.payloads.get(button.dataset.uid) ?? ["", null];
    state.detailJson = payload;
    openDetail("Dettaglio JSON", title, view.jsonBody(payload));
    return;
  }

  await showLogs(button.dataset.logs, new Date(button.dataset.at), button.dataset.code);
});

/** I log attorno a una voce di timeline. Interrogati una volta sola per voce. */
async function showLogs(trackingId, moment, code) {
  const minutes = Number(el("inp-log-min").value || k.LOG_WINDOW_MINUTES);
  const subtitle = `${trackingId}  ·  ${data.fmtTs(moment)}  ·  ±${minutes} min`;
  const body = el("detail-modal-body");
  openDetail("Log CloudWatch", subtitle,
             '<p class="text-xs text-slate-500">Cerco nei log…</p>');
  if (!state.logs) {
    body.innerHTML = '<p class="text-xs text-rose-600">Nessuna connessione AWS attiva.</p>';
    return;
  }

  const span = minutes * 60 * 1000;
  const start = new Date(moment.getTime() - span);
  const end = new Date(moment.getTime() + span);
  const memo = [trackingId, code, start.getTime(), end.getTime()].join("|");
  try {
    if (!state.entryLogs.has(memo)) {
      state.entryLogs.set(memo, await data.logsForEntry(
        state.logs, state.logCache, trackingId, code, start, end));
    }
    const { events, filter } = state.entryLogs.get(memo);
    el("detail-modal-sub").innerText = `${subtitle}  ·  ${filter}`;
    body.innerHTML = view.renderLogs(events, minutes);
  } catch (error) {
    body.innerHTML = failure(error);
  }
}

/** Un errore dentro il modale, sempre con la stessa faccia. */
function failure(error) {
  return `<p class="text-xs text-rose-600">${escape(`${error.name}: ${error.message}`)}</p>`;
}

/** Il modale non sta dentro #results: i suoi bottoni vogliono il loro gestore. */
el("detail-modal-body").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-copy-json]");
  if (!button) return;
  const text = JSON.stringify(state.detailJson, null, 2);
  button.innerHTML = await copyText(text)
    ? `${view.icon("check")}copiato`
    : `${view.icon("copy")}copia non riuscita`;
});

el("btn-detail-close").addEventListener("click", () => el("detail-modal").close());
el("sel-profile").addEventListener("change", connect);
el("sel-mode").addEventListener("change", applyMode);

el("btn-search").addEventListener("click", async () => {
  const byTracking = searchingByTracking();
  const needle = el("inp-iun").value.trim();
  if (!needle) {
    say("search-msg", byTracking ? "Inserisci un trackingId." : "Inserisci uno IUN.",
        "text-rose-600");
    return;
  }
  if (!state.ddb) {
    say("search-msg", "Nessuna connessione AWS attiva.", "text-rose-600");
    return;
  }

  rememberSearch(needle, byTracking);
  el("btn-search").disabled = true;
  el("results").innerHTML = "";
  state.logCache = {};            // due contenitori distinti: l'assegnamento a
  state.entryLogs = new Map();    // catena li farebbe condividere
  try {
    say("search-msg", "Avvio…");
    const progress = (text) => say("search-msg", text);
    const trackings = byTracking
      ? await data.queryByTrackingId(state.ddb, k.TABLES.trackings, needle)
      : await data.findByIun(
          state.ddb, selectedSchemes(), needle,
          Number(el("inp-max-rec").value || k.MAX_RECINDEX),
          Number(el("inp-max-att").value || k.MAX_ATTEMPT),
          progress);
    const { errors, dryRuns } = await data.collect(
      state.ddb, trackings, el("chk-dryrun").checked, progress);

    let logOutputs = {};
    if (el("chk-log-events").checked && state.logs) {
      logOutputs = await data.collectLogOutputs(
        state.logs, state.logCache, trackings,
        Number(el("inp-log-min").value || k.LOG_WINDOW_MINUTES), progress);
    }

    const recipients = timeline.buildRecipients(
      trackings, errors, dryRuns, logOutputs,
      el("chk-flow").checked, el("sel-order").value);
    state.payloads = new Map();
    el("results").innerHTML = view.render(needle, recipients, connectedRegion(), state.payloads);
    const fromLogs = Object.values(logOutputs).reduce((sum, found) => sum + found.length, 0);
    say("search-msg",
        `Trovati ${trackings.length} tracking su ${recipients.length} destinatari.` +
        (Object.keys(logOutputs).length ? ` ${fromLogs} output ricostruiti dai log.` : ""),
        "text-emerald-700");
  } catch (error) {
    say("search-msg", `${error.name}: ${error.message}`, "text-rose-600");
  } finally {
    el("btn-search").disabled = false;
  }
});

// =============================================================================
// Avvio
// =============================================================================
el("sel-product").innerHTML = k.PRODUCTS.map(([value, label]) =>
  `<option value="${value}">${escape(label)}</option>`).join("");
el("inp-max-rec").value = String(k.MAX_RECINDEX);
el("inp-max-att").value = String(k.MAX_ATTEMPT);
el("inp-log-min").value = String(k.LOG_WINDOW_MINUTES);
trackHeaderHeight();
applyUrlParams();           // prima di applyMode: decide la modalità
applyMode();
setConnection("off", "Non connesso", "Scegli la cartella ~/.aws");

// per ultimo, quando il resto è pronto: la rotella si ferma e il bottone si apre
el("btn-pick").innerHTML = "Load .aws";
el("btn-pick").title = "";
el("btn-pick").disabled = false;
