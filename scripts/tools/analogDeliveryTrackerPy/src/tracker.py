"""Storico di una spedizione analogica a partire dallo IUN.

Legge le tabelle DynamoDB di pn-paper-tracker e costruisce, per ogni
destinatario della notifica, un'unica timeline che unisce gli eventi ricevuti,
gli errori generati dalle validazioni, le tappe del flusso e gli output prodotti.

Questo modulo è la pagina: stato, gestori degli eventi e avvio. Le query stanno
in ``data``, la costruzione della timeline in ``timeline``, l'HTML in ``view``.
"""

import asyncio
import json
from datetime import datetime, timedelta
from html import escape

from pyscript import document, when, window

import aws_dir

# boto3 va importato con lo shim di Pyodide già applicato: anticiparlo qui fa
# fallire subito il caricamento della pagina, invece che alla prima ricerca.
#
# Con ?transport=native si lascia il transport di urllib3, che da Pyodide 314
# instrada già via fetch da solo: è l'interruttore per provare se la
# sostituzione con XHR serva ancora. Il resoconto finisce in console.
_boot = aws_dir.enable_boto3_in_pyodide(
    patch_transport="transport=native" not in window.location.search)
aws_dir.import_boto3()
window.console.log("aws_dir:", str(_boot))

import constants as k
import data
import diagram
import timeline
import view


# =============================================================================
# Pagina
# =============================================================================

def el(element_id):
    return document.getElementById(element_id)


def closest(event, selector):
    """L'antenato del bersaglio che corrisponde al selettore, o ``None``.

    ``closest`` restituisce ``null`` quando non trova niente, e Pyodide lo
    converte in ``pyodide.ffi.jsnull``, che non è ``None``: senza questo
    controllo un clic fuori dai bottoni esplode su ``.dataset``. Si guarda
    l'attributo invece del valore, così vale su qualsiasi versione.
    """
    found = event.target.closest(selector)
    return found if hasattr(found, "dataset") else None


state = {"aws": None, "ddb": None, "logs": None, "log_cache": {}, "entry_logs": {},
         "payloads": {}, "region": None, "account": None,
         # quello che l'URL chiede e che si può onorare solo più tardi: il
         # profilo esiste quando la cartella .aws è letta, la region al collegamento
         "wanted_account": None, "wanted_region": None}


def say(element_id, message, tone="text-slate-500"):
    node = el(element_id)
    node.className = f"mt-3 text-sm {tone}"
    node.innerText = message


def say_error(element_id, message):
    """Come ``say``, ma coi due gesti che servono dopo un errore.

    Le righe rientrate sono comandi, e escono con un bottone «copia»; in coda
    «Rileggi la cartella», da premere una volta eseguito il comando.
    """
    node = el(element_id)
    node.className = "mt-3 text-sm text-rose-600"
    blocks = []
    for line in message.splitlines():
        if line.startswith("  ") and line.strip():
            command = escape(line.strip())
            blocks.append(
                '<span class="mt-1 flex flex-wrap items-center gap-2">'
                f'<code class="rounded bg-rose-50 px-2 py-1 font-mono text-xs '
                f'text-rose-900">{command}</code>'
                f'<button type="button" data-copy="{command}" '
                'class="shrink-0 cursor-pointer rounded border border-rose-200 px-2 py-1 text-xs '
                'text-rose-700 hover:bg-rose-50">copia</button></span>')
        else:
            blocks.append(f'<span class="block">{escape(line)}</span>')
    blocks.append(
        '<span class="mt-2 block"><button type="button" data-reload '
        'class="cursor-pointer rounded bg-brand px-3 py-1.5 text-xs font-medium text-white '
        'hover:bg-blue-900">Rileggi la cartella</button></span>')
    node.innerHTML = "".join(blocks)


async def copy_text(text):
    """Copia negli appunti, con la strada vecchia come riserva.

    ``navigator.clipboard`` non è concesso ai sotto-frame di altra origine se
    l'iframe non porta ``allow="clipboard-write"``: là resta solo
    ``execCommand``, che vuole il testo dentro un elemento e selezionato.
    """
    try:
        await window.navigator.clipboard.writeText(text)
        return True
    except Exception:
        pass

    node = document.createElement("textarea")
    node.value = text
    node.setAttribute("readonly", "")
    node.style.position = "fixed"
    node.style.opacity = "0"
    document.body.appendChild(node)
    try:
        node.select()
        return bool(document.execCommand("copy"))
    except Exception:
        return False
    finally:
        node.remove()


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


def track_header_height():
    """Tiene ``--page-header`` allineata alla barra in cima alla pagina.

    Le intestazioni dei tracking ci si fermano sotto, e quella barra cambia
    altezza quando va a capo: meglio misurarla che indovinarne i pixel.
    """
    from pyodide.ffi import create_proxy

    bar = document.querySelector("header")

    def apply(*_):              # ResizeObserver chiama con (entries, observer)
        document.documentElement.style.setProperty(
            "--page-header", f"{bar.offsetHeight}px")

    apply()
    window.ResizeObserver.new(create_proxy(apply)).observe(bar)


def url_value(params, key):
    """Un parametro della query string, o ``None``. ``get`` da solo torna ``null``."""
    return params.get(key) if params.has(key) else None


def apply_url_params():
    """Riempie i campi dai parametri dell'URL, per poter condividere una ricerca.

    Il profilo non si sceglie qui: la cartella ``.aws`` non è ancora stata letta.
    Account e region restano da parte, li onorano ``fill_profiles`` e ``connect``.
    """
    params = window.URLSearchParams.new(window.location.search)
    tracking, iun = url_value(params, "trackingId"), url_value(params, "iun")
    if tracking or iun:
        el("sel-mode").value = "tracking" if tracking else "iun"
        el("inp-iun").value = tracking or iun
    product = url_value(params, "product")
    if product in k.PRODUCT_SCHEMES:
        el("sel-product").value = product
    state["wanted_account"] = url_value(params, "account")
    state["wanted_region"] = url_value(params, "region")


def remember_search(needle, by_tracking):
    """Rimette la ricerca nell'URL, così si può condividere o salvare."""
    params = window.URLSearchParams.new()
    params.set("trackingId" if by_tracking else "iun", needle)
    if not by_tracking:
        params.set("product", el("sel-product").value)
    for key, value in (("account", state["account"]), ("region", state["region"])):
        if value:
            params.set(key, value)
    # l'interruttore di prova del transport non deve sparire alla prima ricerca:
    # riscrivendo la query da zero si perderebbe, e il reload dopo tornerebbe a XHR
    current = window.URLSearchParams.new(window.location.search)
    if current.has("transport"):
        params.set("transport", current.get("transport"))
    try:
        window.history.replaceState(None, "", f"{window.location.pathname}?{params.toString()}")
    except Exception:
        pass            # iframe sandboxed: l'URL non si tocca, la ricerca sì


def connected_region():
    return state["region"] or k.DEFAULT_REGION


def connect():
    """Prende la credenziale attiva del profilo scelto e prepara il client."""
    aws, profile = state["aws"], el("sel-profile").value
    state["ddb"] = state["logs"] = None
    el("btn-search").disabled = True
    try:
        # senza require_account un profilo a cui non sei loggato ripiegherebbe,
        # in silenzio, sulle credenziali di un altro account
        source = aws.best_source(profile, require_account=True)
        region = state["wanted_region"] or aws.region_for(profile) or k.DEFAULT_REGION
        state["wanted_region"] = None       # vale solo per il primo collegamento
        state["ddb"] = aws.client("dynamodb", profile, source=source, region=region)
        state["logs"] = aws.client("logs", profile, source=source, region=region)
        state["region"] = region
    except aws_dir.AwsDirError as exc:
        mismatch = isinstance(exc, aws_dir.AccountMismatch)
        set_connection("ko", "Account diverso" if mismatch else "Credenziali non valide", str(exc))
        say_error("search-msg", str(exc))
        return

    remember_profile(profile)
    state["account"] = source.account
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
    # la classifica ricorda solo i profili che si sono connessi: senza questo,
    # una rilettura sposterebbe la scelta proprio mentre ne stai sistemando uno
    current = select.value
    chosen = current if current in names else names[0]

    # l'URL chiede un account, non un profilo: qui si traduce, e fra i profili
    # che puntano a quell'account vince il primo in classifica
    wanted, state["wanted_account"] = state["wanted_account"], None
    if wanted:
        for name in names:
            if aws.expected_account(name) == wanted:
                chosen = name
                break
    select.innerHTML = "".join(
        f'<option value="{escape(name)}"{" selected" if name == chosen else ""}>'
        f'{escape(name)}</option>' for name in names)
    select.disabled = False
    connect()


def selected_schemes():
    """Schemi di trackingId del prodotto scelto; in dubbio, li prova tutti."""
    return k.PRODUCT_SCHEMES.get(el("sel-product").value) or k.PRODUCT_SCHEMES["AUTO"]


def searching_by_tracking():
    return el("sel-mode").value == "tracking"


def apply_mode():
    """Adatta i campi che hanno senso solo quando si parte dallo IUN."""
    by_tracking = searching_by_tracking()
    el("inp-label").innerText = "trackingId" if by_tracking else "IUN"
    el("inp-iun").placeholder = (
        k.build_attempt_id("ANALOG_DOMICILE", "<IUN>", 0, 0) + ".PCRETRY_0"
        if by_tracking else "UWXU-VKXM-QVJZ-202609-M-1")
    for element_id in ("sel-product", "inp-max-rec", "inp-max-att"):
        el(element_id).disabled = by_tracking


# =============================================================================
# Eventi
# =============================================================================
async def reload_directory():
    """Rilegge la cartella e riconnette, senza ricaricare la pagina.

    È il gesto da fare dopo un ``aws sso login``: il selettore non riappare.
    """
    try:
        state["aws"] = await aws_dir.AwsDirectory.pick()
    except Exception as exc:
        set_connection("ko", "Cartella non letta", str(exc))
        say_error("search-msg", str(exc))
        return
    fill_profiles()


@when("click", "#btn-pick")
async def on_pick(event):
    await reload_directory()


@when("click", "#search-msg")
async def on_message_click(event):
    """Delega: i bottoni dell'errore nascono dopo, quindi si ascolta il contenitore."""
    button = closest(event, "[data-copy], [data-reload]")
    if button is None:
        return
    if button.hasAttribute("data-reload"):
        await reload_directory()
        return
    button.innerText = "copiato" if await copy_text(button.dataset.copy) else "copia a mano"


def open_detail(title, subtitle, body_html, wide=False):
    """Apre il modale, che è uno solo per JSON, log e diagrammi.

    ``wide`` allarga la finestra: un diagramma di flusso è largo quanto tutta la
    macchina a stati, mentre un JSON o una riga di log stanno in una colonna.
    """
    el("detail-modal-title").innerText = title
    el("detail-modal-sub").innerText = subtitle
    el("detail-modal-body").innerHTML = body_html
    el("detail-modal").classList.toggle("wide", wide)
    el("detail-modal").showModal()


async def show_diagram(product, codes, business):
    """Il diagramma del prodotto, con evidenziati gli eventi di quel tracking."""
    arrived = [code for code in codes.split(",") if code]
    open_detail(f"Diagramma {product}", ", ".join(arrived) or "nessun evento",
                '<p class="text-xs text-slate-500">Disegno il diagramma…</p>', wide=True)
    body = el("detail-modal-body")
    try:
        # Il look "neo" misura le etichette col font "Recursive Variable", che
        # arriva dalla rete: disegnare prima che sia pronto vuol dire squadrare
        # i riquadri su Arial e ritrovarsi i titoli tagliati quando il font
        # subentra. Il primo await lo chiede, il secondo aspetta che tutti i
        # font della pagina siano a posto.
        await window.document.fonts.load('16px "Recursive Variable"')
        await window.document.fonts.ready

        drawings = []
        for index, (title, path) in enumerate(k.DIAGRAMS[product]):
            source = await (await window.fetch(path)).text()
            drawn = await window.mermaid.render(
                f"diagram-svg-{index}", diagram.highlight(source, arrived, business))
            drawings.append(
                (f'<h3 class="mb-1 text-xs font-semibold uppercase tracking-wide '
                 f'text-slate-500">{escape(title)}</h3>' if title else "")
                + f'<div class="overflow-x-auto">{drawn.svg}</div>')

        body.innerHTML = (
            '<div class="max-h-[80vh] space-y-6 overflow-y-auto">'
            + "".join(drawings) + "</div>"
            '<p class="mt-2 text-xs text-slate-500">In verde gli stati raggiunti; '
            'un ✅ sulle frecce percorse e sul fascicolo chiuso, ❌ se si è '
            'chiuso in KO.</p>')
    except Exception as exc:
        body.innerHTML = (f'<p class="text-xs text-rose-600">'
                          f'{escape(f"{type(exc).__name__}: {exc}")}</p>')


@when("click", "#results")
async def on_results_click(event):
    """Delega: i risultati sono riscritti a ogni ricerca."""
    button = closest(event, "[data-detail], [data-copy]")
    if button is None:
        return

    if button.hasAttribute("data-copy"):
        if await copy_text(button.dataset.copy):
            button.innerHTML = view.icon("check")
            button.title = "copiato"
        else:
            button.title = "copia non riuscita: selezionalo a mano"
        return

    if button.dataset.detail == "diagram":
        await show_diagram(button.dataset.product, button.dataset.codes,
                           button.dataset.business)
        return

    if button.dataset.detail == "json":
        title, payload = state["payloads"].get(button.dataset.uid, ("", None))
        open_detail("Dettaglio JSON", title, view.json_body(payload))
        return

    tracking_id = button.dataset.logs
    moment = datetime.fromisoformat(button.dataset.at)
    minutes = int(el("inp-log-min").value or k.LOG_WINDOW_MINUTES)
    window = timedelta(minutes=minutes)
    body = el("detail-modal-body")
    subtitle = f"{tracking_id}  ·  {data.fmt_ts(moment)}  ·  ±{minutes} min"
    open_detail("Log CloudWatch", subtitle,
                '<p class="text-xs text-slate-500">Cerco nei log…</p>')

    if not state["logs"]:
        body.innerHTML = '<p class="text-xs text-rose-600">Nessuna connessione AWS attiva.</p>'
        return
    await asyncio.sleep(0)              # l'XHR di botocore è sincrona: prima si ridisegna
    memo = (tracking_id, button.dataset.code, moment - window, moment + window)
    try:
        if memo not in state["entry_logs"]:      # una voce si interroga una volta sola
            state["entry_logs"][memo] = data.logs_for_entry(
                state["logs"], state["log_cache"], *memo)
        events, how = state["entry_logs"][memo]
    except Exception as exc:
        body.innerHTML = (f'<p class="text-xs text-rose-600">'
                          f'{escape(f"{type(exc).__name__}: {exc}")}</p>')
        return
    el("detail-modal-sub").innerText = f"{subtitle}  ·  {how}"
    body.innerHTML = view.render_logs(events, minutes)


@when("click", "#btn-detail-close")
def on_detail_close(event):
    el("detail-modal").close()


@when("change", "#sel-profile")
def on_profile_change(event):
    connect()


@when("change", "#sel-mode")
def on_mode_change(event):
    apply_mode()


@when("click", "#btn-search")
async def on_search(event):
    by_tracking = searching_by_tracking()
    needle = el("inp-iun").value.strip()
    if not needle:
        say("search-msg", "Inserisci un trackingId." if by_tracking else "Inserisci uno IUN.",
            "text-rose-600")
        return
    if not state["ddb"]:
        say("search-msg", "Nessuna connessione AWS attiva.", "text-rose-600")
        return

    remember_search(needle, by_tracking)
    el("btn-search").disabled = True
    el("results").innerHTML = ""
    state["log_cache"] = {}         # due dizionari distinti: l'assegnamento a
    state["entry_logs"] = {}        # catena li farebbe condividere
    try:
        say("search-msg", "Avvio…")
        await asyncio.sleep(0)
        if by_tracking:
            trackings = data.query_by_tracking_id(state["ddb"], k.TABLES["trackings"], needle)
        else:
            trackings = await data.find_by_iun(
                state["ddb"], selected_schemes(), needle,
                int(el("inp-max-rec").value or k.MAX_RECINDEX),
                int(el("inp-max-att").value or k.MAX_ATTEMPT),
                lambda text: say("search-msg", text))
        errors, dry_runs = await data.collect(
            state["ddb"], trackings, el("chk-dryrun").checked,
            lambda text: say("search-msg", text))

        log_outputs = {}
        if el("chk-log-events").checked and state["logs"]:
            log_outputs = await data.collect_log_outputs(
                state["logs"], state["log_cache"], trackings,
                int(el("inp-log-min").value or k.LOG_WINDOW_MINUTES),
                lambda text: say("search-msg", text))

        recipients = timeline.build_recipients(trackings, errors, dry_runs, log_outputs,
                                      el("chk-flow").checked, el("sel-order").value)
        state["payloads"] = {}
        el("results").innerHTML = view.render(needle, recipients, connected_region(),
                                              state["payloads"])
        from_logs = sum(len(found) for found in log_outputs.values())
        say("search-msg",
            f"Trovati {len(trackings)} tracking su {len(recipients)} destinatari."
            + (f" {from_logs} output ricostruiti dai log." if log_outputs else ""),
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
el("inp-log-min").value = str(k.LOG_WINDOW_MINUTES)
track_header_height()
apply_url_params()          # prima di apply_mode: decide la modalità
apply_mode()
set_connection("off", "Non connesso", "Scegli la cartella ~/.aws")

# per ultimo, quando il resto è pronto: la rotella si ferma e il bottone si apre
el("btn-pick").innerHTML = "Load .aws"
el("btn-pick").title = ""
el("btn-pick").disabled = False
