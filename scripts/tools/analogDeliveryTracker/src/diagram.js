/**
 * Il diagramma di flusso del prodotto, con sopra il cammino della spedizione.
 *
 * I file in `diagrams/` sono stateDiagram di Mermaid scritti a mano: i codici
 * evento stanno sulle frecce. Qui si legge quel testo, si capisce quali frecce
 * sono state percorse e si riscrive il sorgente con gli stati raggiunti in
 * verde.
 *
 * Il frontmatter non si tocca. `look: neo` e `layout: elk` sono i default degli
 * stateDiagram da Mermaid 12, e stanno scritti solo per non dipendere da quelli
 * di domani; `theme: redux` invece è una scelta, al posto del `redux-color` di
 * default: riquadri già colorati uno a uno toglierebbero l'occhio al verde del
 * cammino percorso.
 */

import * as k from "./constants.js";
import { escape } from "./html.js";

/**
 * Una transizione: "Stato --> Altro:CODICE resto". Il codice si ferma alle sole
 * lettere e cifre perché nei diagrammi 890 l'etichetta continua con le varianti
 * del documento: `RECAG005B<br>[ARCAD|CAD]\(\*\)`.
 */
const TRANSITION = /^\s*(\S+)\s*-->\s*([^\s:]+)\s*:\s*([A-Z0-9]+)/;
/** l'apertura di un macro stato: "state Consegnato {" */
const COMPOSITE = /^\s*state\s+(\w+)\s*\{/;
/** la freccia d'ingresso dentro un macro stato: "[*] --> Preesito1" */
const ENTRY = /^\s*\[\*\]\s*-->\s*(\w+)/;
/**
 * l'etichetta di uno stato, scritta a parte: "PresaInCarico:Presa in carico".
 * Una transizione non ci casca: fra il nome e i due punti ha la freccia.
 */
const LABEL = /^(\s*)(\w+)\s*:\s*(.+)$/;

/**
 * Il segno sul cammino, oltre al colore: si vede anche stampando in bianco e
 * nero, e sopravvive a chi distingue male il verde dal rosso. Lo portano le
 * frecce percorse e i macro stati chiusi — con la croce se il fascicolo si è
 * chiuso male. Gli stati base no: sono tanti, e il verde da solo li racconta.
 */
const EDGE_MARK = "✅";
const STATE_MARK = { concluso: "✅", fallito: "❌" };

/**
 * Le tinte dei chip della pagina, nella forma del classDef che genera l'editor
 * di Mermaid: bordo pieno, fondo chiarissimo, testo scuro. Il macro stato fa da
 * sfondo agli stati che contiene, quindi il suo fondo è più chiaro del loro per
 * non confondersi; rosso al posto del verde quando il fascicolo chiude in KO.
 */
const STYLE = {
  arrivato: "classDef arrivato stroke-width:1px,stroke-dasharray:none," +
            "stroke:#22C55E,fill:#DCFCE7,color:#14532D;",
  concluso: "classDef concluso stroke-width:1px,stroke-dasharray:none," +
            "stroke:#22C55E,fill:#F0FDF4,color:#14532D;",
  fallito: "classDef fallito stroke-width:1px,stroke-dasharray:none," +
           "stroke:#F43F5E,fill:#FFF1F2,color:#881337;",
};

export function available(product) {
  return product in k.DIAGRAMS;
}

/** I codici evento del tracking, una volta sola e nell'ordine d'arrivo. */
export function codes(tracking) {
  const found = [];
  for (const event of tracking.events ?? []) {
    const code = event.statusCode;
    if (code && !found.includes(code)) found.push(code);
  }
  return found;
}

/**
 * I macro stati, ognuno con lo stato base da cui si entra.
 *
 * Serve perché il verde va agli stati base: quando una freccia porta dentro un
 * macro stato, quel che si è davvero raggiunto è il suo stato d'ingresso.
 */
function composites(lines) {
  const found = new Map();
  const stack = [];
  for (const line of lines) {
    const opened = COMPOSITE.exec(line);
    if (opened) {
      stack.push(opened[1]);
      if (!found.has(opened[1])) found.set(opened[1], null);
      continue;
    }
    if (stack.length) {
      const entry = ENTRY.exec(line);
      if (entry && found.get(stack.at(-1)) === null) found.set(stack.at(-1), entry[1]);
      if (line.includes("}")) stack.pop();
    }
  }
  return found;
}

/**
 * I codici che nel diagramma stanno su una freccia sola.
 *
 * Da dove si arriva lo dice la sorgente della freccia percorsa, ma un codice
 * ripetuto non dice quale: il furto, per l'AR `RECRN006`, si può subire
 * partendo da tre stati diversi, e marcarli tutti accenderebbe un cammino mai
 * percorso. Per lo stesso motivo quelle frecce non prendono la spunta: se ne è
 * percorsa una, e segnarle tutte e tre direbbe il falso. Il bersaglio invece
 * resta buono, perché quelle tre frecce vanno tutte nello stesso posto.
 */
function unambiguous(lines) {
  const seen = new Map();
  for (const line of lines) {
    const found = TRANSITION.exec(line);
    if (found) seen.set(found[3], (seen.get(found[3]) ?? 0) + 1);
  }
  return new Set([...seen].filter(([, times]) => times === 1).map(([code]) => code));
}

/**
 * Il segno in coda all'etichetta dei macro stati chiusi.
 *
 * Un macro stato senza riga d'etichetta si chiama come il suo id, e allora la
 * riga gliela si scrive.
 */
function marked(lines, marks) {
  const out = [];
  const seen = new Set();
  for (let line of lines) {
    const found = LABEL.exec(line);
    if (found && marks.has(found[2])) {
      seen.add(found[2]);
      line = `${line.replace(/\s+$/, "")} ${marks.get(found[2])}`;
    }
    out.push(line);
  }
  return out.concat([...marks]
    .filter(([name]) => !seen.has(name))
    .map(([name, mark]) => `  ${name}:${name} ${mark}`));
}

/**
 * Il sorgente del diagramma con segnato il cammino percorso.
 *
 * In uno `stateDiagram` le frecce non si possono colorare con `classDef`,
 * quindi la freccia percorsa prende un ✅ e il verde va agli stati: quello in
 * cui porta e quello da cui parte. Spunta e stato di partenza valgono solo se
 * il codice sta su una freccia sola — vedi `unambiguous`; il colore dello stato
 * d'arrivo invece si dà comunque, ed è lui a dire che l'evento è arrivato.
 *
 * Un macro stato non diventa verde solo perché ci si è entrati: quello lo
 * prende il suo stato d'ingresso. Il macro stato si colora quando lo si è
 * *lasciato*, cioè quando arriva il suo evento finale — la freccia che da lui
 * porta a `[*]`, per l'AR i vari `RECRN00xC`. Lui la spunta ce l'ha, e diventa
 * una ❌ su fondo rosso quando il `businessState` del tracking è KO. Non è
 * l'esito del singolo codice: lo stesso evento finale può chiudere bene o male
 * a seconda della pratica, ed è il business a dirlo. Gli stati base dentro
 * restano verdi: il cammino è stato percorso, è l'esito a essere KO.
 */
export function highlight(text, arrived, business = "") {
  const lines = text.split("\n");
  const inside = composites(lines);
  const once = unambiguous(lines);
  const found = { arrivato: [], concluso: [], fallito: [] };
  const closing = business === "KO" ? "fallito" : "concluso";
  const out = [];

  const mark = (name, final = false) => {
    if (name === "[*]" || Object.values(found).some((names) => names.includes(name))) return;
    if (!inside.has(name)) {
      found.arrivato.push(name);
    } else if (final) {
      // un macro stato lo colora solo l'evento che chiude il fascicolo;
      // uscirne di lato, come il RECAG012 dell'890, non conclude niente
      found[closing].push(name);
    }
  };

  for (let line of lines) {
    const hit = TRANSITION.exec(line);
    if (hit && arrived.includes(hit[3])) {
      const [, source, target, code] = hit;
      if (once.has(code)) {
        line += ` ${EDGE_MARK}`;
        mark(source, target === "[*]");
      }
      // get() è undefined per uno stato base, ed è il suo ingresso per un macro
      mark(inside.get(target) || target);
    }
    out.push(line);
  }

  const marks = new Map();
  for (const [style, names] of Object.entries(found)) {
    if (!(style in STATE_MARK)) continue;
    for (const name of names) marks.set(name, STATE_MARK[style]);
  }
  const body = marked(out, marks);
  for (const [style, names] of Object.entries(found)) {
    if (names.length) body.push("", `  ${STYLE[style]}`, `  class ${names.join(",")} ${style}`);
  }
  return body.join("\n");
}

/**
 * Il bottone del diagramma, se per quel prodotto ce n'è uno.
 *
 * `button` è il costruttore dei bottoni della card: il disegno dei bottoni
 * resta uno solo, in `view`.
 */
export function action(tracking, button) {
  const product = tracking.productType;
  if (!available(product)) return "";
  return button("diagram", "Diagramma",
    `data-product="${escape(product)}" ` +
    `data-codes="${escape(codes(tracking).join(","))}" ` +
    `data-business="${escape(tracking.businessState ?? "")}"`);
}
