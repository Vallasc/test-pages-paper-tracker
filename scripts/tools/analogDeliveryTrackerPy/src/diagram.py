"""Il diagramma di flusso del prodotto, con sopra il cammino della spedizione.

I file in ``diagrams/`` sono stateDiagram di Mermaid scritti a mano: i codici
evento stanno sulle frecce. Qui si legge quel testo, si capisce quali frecce
sono state percorse e si riscrive il sorgente con gli stati raggiunti in verde.

Il frontmatter non si tocca. ``look: neo`` e ``layout: elk`` sono i default
degli stateDiagram da Mermaid 12, e stanno scritti solo per non dipendere da
quelli di domani; ``theme: redux`` invece è una scelta, al posto del
``redux-color`` di default: riquadri già colorati uno a uno toglierebbero
l'occhio al verde del cammino percorso.
"""

import re
from collections import Counter
from html import escape

import constants as k

#: Una transizione: "Stato --> Altro:CODICE resto". Il codice si ferma alle sole
#: lettere e cifre perché nei diagrammi 890 l'etichetta continua con le varianti
#: del documento: "RECAG005B<br>[ARCAD|CAD]\(\*\)".
TRANSITION = re.compile(r"^\s*(\S+)\s*-->\s*([^\s:]+)\s*:\s*([A-Z0-9]+)")
#: l'apertura di un macro stato: "state Consegnato {"
COMPOSITE = re.compile(r"^\s*state\s+(\w+)\s*\{")
#: la freccia d'ingresso dentro un macro stato: "[*] --> Preesito1"
ENTRY = re.compile(r"^\s*\[\*\]\s*-->\s*(\w+)")
#: l'etichetta di uno stato, scritta a parte: "PresaInCarico:Presa in carico".
#: Una transizione non ci casca: fra il nome e i due punti ha la freccia.
LABEL = re.compile(r"^(\s*)(\w+)\s*:\s*(.+)$")

#: Il segno sul cammino, oltre al colore: si vede anche stampando in bianco e
#: nero, e sopravvive a chi distingue male il verde dal rosso. Lo portano le
#: frecce percorse e i macro stati chiusi — con la croce se il fascicolo si è
#: chiuso male. Gli stati base no: sono tanti, e il verde da solo li racconta.
EDGE_MARK = "✅"
STATE_MARK = {"concluso": "✅", "fallito": "❌"}

#: Le tinte dei chip della pagina, nella forma del classDef che genera l'editor
#: di Mermaid: bordo pieno, fondo chiarissimo, testo scuro. Il macro stato fa da
#: sfondo agli stati che contiene, quindi il suo fondo è più chiaro del loro per
#: non confondersi; rosso al posto del verde quando il fascicolo chiude in KO.
STYLE = {
    "arrivato": ("classDef arrivato stroke-width:1px,stroke-dasharray:none,"
                 "stroke:#22C55E,fill:#DCFCE7,color:#14532D;"),
    "concluso": ("classDef concluso stroke-width:1px,stroke-dasharray:none,"
                 "stroke:#22C55E,fill:#F0FDF4,color:#14532D;"),
    "fallito":  ("classDef fallito stroke-width:1px,stroke-dasharray:none,"
                 "stroke:#F43F5E,fill:#FFF1F2,color:#881337;"),
}


def available(product):
    return product in k.DIAGRAMS


def codes(tracking):
    """I codici evento del tracking, una volta sola e nell'ordine d'arrivo."""
    found = []
    for event in tracking.get("events") or []:
        code = event.get("statusCode")
        if code and code not in found:
            found.append(code)
    return found


def _composites(lines):
    """I macro stati, ognuno con lo stato base da cui si entra.

    Serve perché il verde va agli stati base: quando una freccia porta dentro un
    macro stato, quel che si è davvero raggiunto è il suo stato d'ingresso.
    """
    found, stack = {}, []
    for line in lines:
        opened = COMPOSITE.match(line)
        if opened:
            stack.append(opened.group(1))
            found.setdefault(opened.group(1), None)
            continue
        if stack:
            entry = ENTRY.match(line)
            if entry and found[stack[-1]] is None:
                found[stack[-1]] = entry.group(1)
            if "}" in line:
                stack.pop()
    return found


def _unambiguous(lines):
    """I codici che nel diagramma stanno su una freccia sola.

    Da dove si arriva lo dice la sorgente della freccia percorsa, ma un codice
    ripetuto non dice quale: il furto, per l'AR ``RECRN006``, si può subire
    partendo da tre stati diversi, e marcarli tutti accenderebbe un cammino mai
    percorso. Per lo stesso motivo quelle frecce non prendono la spunta: se ne
    è percorsa una, e segnarle tutte e tre direbbe il falso. Il bersaglio invece
    resta buono, perché quelle tre frecce vanno tutte nello stesso posto.
    """
    seen = Counter(found.group(3) for found in map(TRANSITION.match, lines) if found)
    return {code for code, times in seen.items() if times == 1}


def _marked(lines, marks):
    """Il segno in coda all'etichetta dei macro stati chiusi.

    Un macro stato senza riga d'etichetta si chiama come il suo id, e allora la
    riga gliela si scrive.
    """
    out, seen = [], set()
    for line in lines:
        found = LABEL.match(line)
        if found and found.group(2) in marks:
            seen.add(found.group(2))
            line = f"{line.rstrip()} {marks[found.group(2)]}"
        out.append(line)
    return out + [f"  {name}:{name} {mark}"
                  for name, mark in marks.items() if name not in seen]


def highlight(text, arrived, business=""):
    """Il sorgente del diagramma con segnato il cammino percorso.

    In uno ``stateDiagram`` le frecce non si possono colorare con ``classDef``,
    quindi la freccia percorsa prende un ✅ e il verde va agli stati: quello in
    cui porta e quello da cui parte. Spunta e stato di partenza valgono solo se
    il codice sta su una freccia sola — vedi ``_unambiguous``; il colore dello
    stato d'arrivo invece si dà comunque, ed è lui a dire che l'evento è
    arrivato.

    Un macro stato non diventa verde solo perché ci si è entrati: quello lo
    prende il suo stato d'ingresso. Il macro stato si colora quando lo si è
    *lasciato*, cioè quando arriva il suo evento finale — la freccia che da lui
    porta a ``[*]``, per l'AR i vari ``RECRN00xC``. Lui la spunta ce l'ha, e
    diventa una ❌ su fondo rosso quando il ``businessState`` del tracking è KO.
    Non è l'esito del singolo codice: lo stesso evento finale può chiudere bene
    o male a seconda della pratica, ed è il business a dirlo. Gli stati base
    dentro restano verdi: il cammino è stato percorso, è l'esito a essere KO.
    """
    lines = text.splitlines()
    composites = _composites(lines)
    once = _unambiguous(lines)
    marked = {name: [] for name in STYLE}
    closing = "fallito" if business == "KO" else "concluso"
    out = []

    def mark(name, final=False):
        if name == "[*]" or any(name in names for names in marked.values()):
            return
        if name not in composites:
            marked["arrivato"].append(name)
        elif final:
            # un macro stato lo colora solo l'evento che chiude il fascicolo;
            # uscirne di lato, come il RECAG012 dell'890, non conclude niente
            marked[closing].append(name)

    for line in lines:
        found = TRANSITION.match(line)
        if found and found.group(3) in arrived:
            source, target, code = found.groups()
            if code in once:
                line += f" {EDGE_MARK}"
                mark(source, final=target == "[*]")
            # get() è None per uno stato base, ed è il suo ingresso per un macro
            mark(composites.get(target) or target)
        out.append(line)

    out = _marked(out, {name: STATE_MARK[style]
                        for style, names in marked.items() if style in STATE_MARK
                        for name in names})
    for style, names in marked.items():
        if names:
            out += ["", "  " + STYLE[style], f"  class {','.join(names)} {style}"]
    return "\n".join(out)


def action(tracking, button):
    """Il bottone del diagramma, se per quel prodotto ce n'è uno.

    ``button`` è il costruttore dei bottoni della card: il disegno dei bottoni
    resta uno solo, in ``view``.
    """
    product = tracking.get("productType")
    if not available(product):
        return ""
    return button("diagram", "Diagramma",
                  f'data-product="{escape(product)}" '
                  f'data-codes="{escape(",".join(codes(tracking)))}" '
                  f'data-business="{escape(tracking.get("businessState") or "")}"')
