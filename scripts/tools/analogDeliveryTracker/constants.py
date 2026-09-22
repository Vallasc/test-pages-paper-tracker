"""Costanti e cataloghi dell'Analog Delivery Tracker.

Qui sta tutto ciò che dipende dalla configurazione degli ambienti PN o dagli
enum del microservizio: se cambiano quelli, si tocca solo questo file.
"""

import re

# =============================================================================
# AWS
# =============================================================================
#: usata quando il profilo non dichiara una region propria
DEFAULT_REGION = "eu-south-1"

#: prefisso delle tabelle DynamoDB (``${ProjectName}`` in storage.yml)
TABLE_PREFIX = "pn"

TABLES = {
    "trackings": f"{TABLE_PREFIX}-PaperTrackings",
    "errors": f"{TABLE_PREFIX}-PaperTrackingsErrors",
    "dry_run": f"{TABLE_PREFIX}-PaperTrackerDryRunOutputs",
}

#: indice su cui si interroga l'``attemptId``
ATTEMPT_INDEX = "attemptId-pcRetry-index"

#: chiavi di localStorage: l'ultimo profilo scelto e quante volte ognuno è stato
#: usato. L'ordine della tendina si impara dall'uso, perché i nomi dei profili
#: dipendono dalla configurazione di chi apre la pagina.
LAST_PROFILE_KEY = "analogDeliveryTracker.lastProfile"
PROFILE_USAGE_KEY = "analogDeliveryTracker.profileUsage"


# =============================================================================
# Composizione del trackingId
# =============================================================================
# Il trackingId è ``<attemptId>.PCRETRY_<n>`` e cambia forma con il prodotto:
# i prodotti con feedback multipli hanno anche il tentativo, gli altri no.
TRACKING_SCHEMES = {
    "ANALOG_DOMICILE": {
        "prefix": "PREPARE_ANALOG_DOMICILE",
        "has_attempt": True,
    },
    "SIMPLE_REGISTERED_LETTER": {
        "prefix": "PREPARE_SIMPLE_REGISTERED_LETTER",
        "has_attempt": False,
    },
}

#: (valore, etichetta, schemi da interrogare) — il primo è il default
PRODUCTS = (
    ("AUTO", "Tutti i prodotti", ("ANALOG_DOMICILE", "SIMPLE_REGISTERED_LETTER")),
    ("AR", "AR — Raccomandata A/R", ("ANALOG_DOMICILE",)),
    ("890", "890 — Notifiche a mezzo posta", ("ANALOG_DOMICILE",)),
    ("RIR", "RIR — Internazionale A/R", ("ANALOG_DOMICILE",)),
    ("RS", "RS — Raccomandata semplice", ("SIMPLE_REGISTERED_LETTER",)),
    ("RIS", "RIS — Internazionale semplice", ("SIMPLE_REGISTERED_LETTER",)),
)

PRODUCT_SCHEMES = {value: schemes for value, _, schemes in PRODUCTS}

#: quanti destinatari e quanti tentativi provare prima di arrendersi
MAX_RECINDEX = 9
MAX_ATTEMPT = 5

#: riconosce entrambe le forme; ``ATTEMPT`` è opzionale
TRACKING_RE = re.compile(
    r"^(?P<prefix>[A-Z_]+)\.IUN_(?P<iun>.+?)\.RECINDEX_(?P<rec>\d+)"
    r"(?:\.ATTEMPT_(?P<att>\d+))?\.PCRETRY_(?P<pc>\d+)$")


def build_attempt_id(scheme, iun, recindex, attempt=0):
    """``attemptId`` per lo schema indicato: è la partition key di ATTEMPT_INDEX."""
    config = TRACKING_SCHEMES[scheme]
    base = f"{config['prefix']}.IUN_{iun}.RECINDEX_{recindex}"
    return f"{base}.ATTEMPT_{attempt}" if config["has_attempt"] else base


def has_attempt(scheme):
    return TRACKING_SCHEMES[scheme]["has_attempt"]


def parse_tracking_id(tracking_id):
    """``(recindex, attempt, pcretry)``; ``attempt`` è ``None`` per RS e RIS."""
    match = TRACKING_RE.match(tracking_id or "")
    if not match:
        return None, None, None
    attempt = match.group("att")
    return (int(match.group("rec")),
            int(attempt) if attempt is not None else None,
            int(match.group("pc")))


# =============================================================================
# Cataloghi — allineati agli enum di it.pagopa.pn.papertracker.model
# =============================================================================
#: statusCode -> (descrizione, tipo evento, prodotto, esito, è demat finale)
STATUS_CODES = {
    "RECRN006":  ("Furto/Smarrimento/deterioramento", "RETRYABLE_EVENT", "AR", "PROGRESS", False),
    "RECRN013":  ("Non Rendicontabile", "RETRYABLE_EVENT", "AR", "PROGRESS", False),
    "RECRN015":  ("Causa Forza Maggiore", "INTERMEDIATE_EVENT", "AR", "PROGRESS", False),
    "RECRN001C": ("Consegnato - Fascicolo Chiuso", "FINAL_EVENT", "AR", "OK", False),
    "RECRN002C": ("Mancata consegna - Fascicolo Chiuso", "FINAL_EVENT", "AR", "OK", False),
    "RECRN002F": ("Irreperibilità Assoluta - Fascicolo Chiuso", "FINAL_EVENT", "AR", "KO", False),
    "RECRN003C": ("Consegnato presso Punti di Giacenza - Fascicolo Chiuso", "FINAL_EVENT", "AR", "OK", False),
    "RECRN004C": ("Mancata consegna presso Punti di Giacenza - Fascicolo Chiuso", "FINAL_EVENT", "AR", "OK", False),
    "RECRN005C": ("Compiuta giacenza - Fascicolo Chiuso", "FINAL_EVENT", "AR", "OK", False),
    "RECRN011":  ("In giacenza", "INTERMEDIATE_EVENT", "AR", "PROGRESS", False),
    "RECRN001A": ("Consegnato - pre-esito", "INTERMEDIATE_EVENT", "AR", "PROGRESS", False),
    "RECRN002A": ("Mancata consegna - pre-esito", "INTERMEDIATE_EVENT", "AR", "PROGRESS", False),
    "RECRN002D": ("Irreperibilità Assoluta - pre-esito", "INTERMEDIATE_EVENT", "AR", "PROGRESS", False),
    "RECRN003A": ("Consegnato presso Punti di Giacenza - pre-esito", "INTERMEDIATE_EVENT", "AR", "PROGRESS", False),
    "RECRN004A": ("Mancata consegna presso Punti di Giacenza - pre-esito", "INTERMEDIATE_EVENT", "AR", "PROGRESS", False),
    "RECRN005A": ("Compiuta giacenza pre-esito", "INTERMEDIATE_EVENT", "AR", "PROGRESS", False),
    "RECRN001B": ("Consegnato - In Dematerializzazione", "INTERMEDIATE_EVENT", "AR", "PROGRESS", True),
    "RECRN002B": ("Mancata consegna - In Dematerializzazione", "INTERMEDIATE_EVENT", "AR", "PROGRESS", True),
    "RECRN002E": ("Irreperibilità Assoluta - In Dematerializzazione", "INTERMEDIATE_EVENT", "AR", "PROGRESS", True),
    "RECRN003B": ("Consegnato presso Punti di Giacenza - In Dematerializzazione", "INTERMEDIATE_EVENT", "AR", "PROGRESS", True),
    "RECRN004B": ("Mancata consegna presso Punti di Giacenza - In Dematerializzazione", "INTERMEDIATE_EVENT", "AR", "PROGRESS", True),
    "RECRN005B": ("Compiuta giacenza - In Dematerializzazione", "INTERMEDIATE_EVENT", "AR", "PROGRESS", True),
    "RECRN010":  ("Inesito", "INTERMEDIATE_EVENT", "AR", "PROGRESS", False),
    "PNRN012":   ("Perfezionamento per decorrenza termini", "FINAL_EVENT", "AR", "OK", False),

    "RECRI001":  ("Avviato all'estero", "INTERMEDIATE_EVENT", "RIR", "PROGRESS", False),
    "RECRI002":  ("Ingresso nel paese estero", "INTERMEDIATE_EVENT", "RIR", "PROGRESS", False),
    "RECRI005":  ("Furto, smarrimento o deterioramento", "RETRYABLE_EVENT", "RIR", "PROGRESS", False),
    "RECRI003A": ("Consegnato - pre-esito", "INTERMEDIATE_EVENT", "RIR", "PROGRESS", False),
    "RECRI004A": ("Mancata consegna - pre-esito", "INTERMEDIATE_EVENT", "RIR", "PROGRESS", False),
    "RECRI003B": ("Consegnato - In Dematerializzazione", "INTERMEDIATE_EVENT", "RIR", "PROGRESS", True),
    "RECRI004B": ("Mancata consegna - In Dematerializzazione", "INTERMEDIATE_EVENT", "RIR", "PROGRESS", True),
    "RECRI003C": ("Consegnato - Fascicolo Chiuso", "FINAL_EVENT", "RIR", "OK", False),
    "RECRI004C": ("Mancata consegna - Fascicolo Chiuso", "FINAL_EVENT", "RIR", "KO", False),

    "CON998":    ("Scartato NODOC", "NOT_RETRYABLE_EVENT", "ALL", "PROGRESS", False),
    "CON997":    ("Scartato CAP/INTERNAZIONALE", "NOT_RETRYABLE_EVENT", "ALL", "PROGRESS", False),
    "CON996":    ("Scartato PDF", "CON996_EVENT", "ALL", "PROGRESS", False),
    "CON995":    ("Errore stampa", "NOT_RETRYABLE_EVENT", "ALL", "PROGRESS", False),
    "CON993":    ("Errore Stampa (parziale)", "NOT_RETRYABLE_EVENT", "ALL", "PROGRESS", False),
    "CON080":    ("Stampato ed Imbustato", "INTERMEDIATE_EVENT", "ALL", "PROGRESS", False),
    "CON020":    ("Affido conservato", "INTERMEDIATE_EVENT", "ALL", "PROGRESS", False),
    "CON010":    ("Distinta Elettronica inviata a Recapitista", "INTERMEDIATE_EVENT", "ALL", "PROGRESS", False),
    "CON011":    ("Distinta Elettronica Sigillata", "INTERMEDIATE_EVENT", "ALL", "PROGRESS", False),
    "CON012":    ("OK Distinta Elettronica da Recapitista", "INTERMEDIATE_EVENT", "ALL", "PROGRESS", False),
    "CON09A":    ("Materialità Pronta", "INTERMEDIATE_EVENT", "ALL", "PROGRESS", False),
    "CON09B":    ("Prenotazione Pickup", "SAVE_ONLY_EVENT", "ALL", "PROGRESS", False),
    "CON016":    ("PICKUP Sigillata", "INTERMEDIATE_EVENT", "ALL", "PROGRESS", False),
    "CON018":    ("Accettazione Recapitista", "INTERMEDIATE_EVENT", "ALL", "PROGRESS", False),

    "REC016":    ("Materialità Ritirata", "SAVE_ONLY_EVENT", "ALL", "PROGRESS", False),
    "REC018":    ("Accettazione Recapitista", "SAVE_ONLY_EVENT", "ALL", "PROGRESS", False),
    "REC991":    ("Mancata Accettazione Recapitista", "SAVE_ONLY_EVENT", "ALL", "PROGRESS", False),

    "P000":      ("Affidato al consolidatore", "SAVE_ONLY_EVENT", "ALL", "PROGRESS", False),
    "P001":      ("In progress", "SAVE_ONLY_EVENT", "ALL", "PROGRESS", False),
    "P011":      ("Errore di sintassi", "SAVE_ONLY_EVENT", "ALL", "PROGRESS", False),
    "P012":      ("Errore semantico", "SAVE_ONLY_EVENT", "ALL", "PROGRESS", False),
    "P013":      ("Errore di trasformazione", "SAVE_ONLY_EVENT", "ALL", "PROGRESS", False),

    "RECAG001A": ("Consegnato - pre-esito", "INTERMEDIATE_EVENT", "890", "PROGRESS", False),
    "RECAG002A": ("Consegnato a persona abilitata - pre-esito", "INTERMEDIATE_EVENT", "890", "PROGRESS", False),
    "RECAG003A": ("Mancata consegna - pre-esito", "INTERMEDIATE_EVENT", "890", "PROGRESS", False),
    "RECAG003D": ("Irreperibilità Assoluta - pre-esito", "INTERMEDIATE_EVENT", "890", "PROGRESS", False),
    "RECAG002B": ("Consegnato a persona abilitata - In Dematerializzazione", "INTERMEDIATE_EVENT", "890", "PROGRESS", True),
    "RECAG003B": ("Mancata consegna - In Dematerializzazione", "INTERMEDIATE_EVENT", "890", "PROGRESS", True),
    "RECAG001B": ("Consegnato - In Dematerializzazione", "INTERMEDIATE_EVENT", "890", "PROGRESS", True),
    "RECAG003E": ("Irreperibilità Assoluta - In Dematerializzazione", "INTERMEDIATE_EVENT", "890", "PROGRESS", True),
    "RECAG002C": ("Consegnato a persona abilitata - Fascicolo Chiuso", "FINAL_EVENT", "890", "OK", False),
    "RECAG003C": ("Mancata consegna - Fascicolo Chiuso", "FINAL_EVENT", "890", "OK", False),
    "RECAG001C": ("Consegnato - Fascicolo Chiuso", "FINAL_EVENT", "890", "OK", False),
    "RECAG003F": ("Irreperibilità Assoluta - Fascicolo Chiuso", "FINAL_EVENT", "890", "KO", False),
    "RECAG010":  ("Inesito", "INTERMEDIATE_EVENT", "890", "PROGRESS", False),
    "RECAG010A": ("Invio CAD", "SAVE_ONLY_EVENT", "890", "PROGRESS", False),
    "RECAG004":  ("Furto/Smarrimento/deterioramento", "RETRYABLE_EVENT", "890", "PROGRESS", False),
    "RECAG015":  ("Causa Forza Maggiore", "INTERMEDIATE_EVENT", "890", "PROGRESS", False),
    "RECAG013":  ("Non Rendicontabile", "RETRYABLE_EVENT", "890", "PROGRESS", False),
    "RECAG011A": ("In giacenza", "INTERMEDIATE_EVENT", "890", "PROGRESS", False),
    "RECAG011B": ("In giacenza - In Dematerializzazione", "STOCK_INTERMEDIATE_EVENT", "890", "PROGRESS", True),
    "RECAG012":  ("Accettazione 23L", "RECAG012_EVENT", "890", "OK", False),
    "RECAG012A": ("Accettazione 23L - pre-esito", "INTERMEDIATE_EVENT", "890", "PROGRESS", False),
    "RECAG005A": ("Consegnato presso Punti di Giacenza - pre-esito", "INTERMEDIATE_EVENT", "890", "PROGRESS", False),
    "RECAG005B": ("Consegnato presso Punti di Giacenza - In Dematerializzazione", "STOCK_INTERMEDIATE_EVENT", "890", "PROGRESS", True),
    "RECAG005C": ("Consegnato presso Punti di Giacenza - Fascicolo Chiuso", "FINAL_EVENT", "890", "PROGRESS", False),
    "RECAG006A": ("Consegna a persona abilitata presso Punti di Giacenza - pre-esito", "INTERMEDIATE_EVENT", "890", "PROGRESS", False),
    "RECAG006B": ("Consegna a persona abilitata presso Punti di Giacenza - In Dematerializzazione", "STOCK_INTERMEDIATE_EVENT", "890", "PROGRESS", True),
    "RECAG006C": ("Consegna a persona abilitata presso Punti di Giacenza - Fascicolo Chiuso", "FINAL_EVENT", "890", "PROGRESS", False),
    "RECAG007A": ("Mancata consegna presso Punti di Giacenza - pre-esito", "INTERMEDIATE_EVENT", "890", "PROGRESS", False),
    "RECAG007B": ("Mancata consegna presso Punti di Giacenza - In Dematerializzazione", "STOCK_INTERMEDIATE_EVENT", "890", "PROGRESS", True),
    "RECAG007C": ("Mancata consegna presso Punti di Giacenza - Fascicolo Chiuso", "FINAL_EVENT", "890", "PROGRESS", False),
    "RECAG008A": ("Compiuta giacenza - pre-esito", "INTERMEDIATE_EVENT", "890", "PROGRESS", False),
    "RECAG008B": ("Compiuta giacenza - In Dematerializzazione", "STOCK_INTERMEDIATE_EVENT", "890", "PROGRESS", True),
    "RECAG008C": ("Compiuta giacenza - Fascicolo Chiuso", "FINAL_EVENT", "890", "PROGRESS", False),

    "RECRS002A": ("Mancata consegna - pre-esito", "INTERMEDIATE_EVENT", "RS", "PROGRESS", False),
    "RECRS002D": ("Irreperibilità Assoluta - pre-esito", "INTERMEDIATE_EVENT", "RS", "PROGRESS", False),
    "RECRS004A": ("Mancata consegna presso Punti di Giacenza - pre-esito", "INTERMEDIATE_EVENT", "RS", "PROGRESS", False),
    "RECRS005A": ("Compiuta giacenza - pre-esito", "INTERMEDIATE_EVENT", "RS", "PROGRESS", False),
    "RECRS015":  ("Causa Forza Maggiore", "INTERMEDIATE_EVENT", "RS", "PROGRESS", False),
    "RECRS010":  ("Inesito", "INTERMEDIATE_EVENT", "RS", "PROGRESS", False),
    "RECRS002B": ("Mancata consegna - In Dematerializzazione", "INTERMEDIATE_EVENT", "RS", "PROGRESS", True),
    "RECRS002E": ("Irreperibilità Assoluta - In Dematerializzazione", "INTERMEDIATE_EVENT", "RS", "PROGRESS", True),
    "RECRS004B": ("Mancata consegna presso Punti di Giacenza - In Dematerializzazione", "INTERMEDIATE_EVENT", "RS", "PROGRESS", True),
    "RECRS005B": ("Compiuta giacenza - In Dematerializzazione", "INTERMEDIATE_EVENT", "RS", "PROGRESS", True),
    "RECRS001C": ("Consegnato - Fascicolo Chiuso", "FINAL_EVENT", "RS", "OK", False),
    "RECRS002C": ("Mancata consegna - Fascicolo Chiuso", "FINAL_EVENT", "RS", "KO", False),
    "RECRS003C": ("Consegnato presso Punti di Giacenza - Fascicolo Chiuso", "FINAL_EVENT", "RS", "OK", False),
    "RECRS002F": ("Irreperibilità Assoluta - Fascicolo Chiuso", "FINAL_EVENT", "RS", "KO", False),
    "RECRS004C": ("Mancata consegna presso Punti di Giacenza - Fascicolo Chiuso", "FINAL_EVENT", "RS", "KO", False),
    "RECRS005C": ("Compiuta giacenza - Fascicolo Chiuso", "FINAL_EVENT", "RS", "KO", False),
    "RECRS006":  ("Furto/Smarrimento/deterioramento", "RETRYABLE_EVENT", "RS", "PROGRESS", False),
    "RECRS013":  ("Non Rendicontabile", "RETRYABLE_EVENT", "RS", "PROGRESS", False),
    "RECRS011":  ("In giacenza", "SAVE_ONLY_EVENT", "RS", "PROGRESS", False),

    "RECRSI004A": ("Non Consegnato - pre-esito", "INTERMEDIATE_EVENT", "RIS", "PROGRESS", False),
    "RECRSI004B": ("Non Consegnato - In Dematerializzazione", "INTERMEDIATE_EVENT", "RIS", "PROGRESS", True),
    "RECRSI003C": ("Consegnato - Fascicolo Chiuso", "FINAL_EVENT", "RIS", "OK", False),
    "RECRSI004C": ("Non Consegnato - fascicolo Chiuso", "FINAL_EVENT", "RIS", "KO", False),
    "RECRSI005":  ("Furto/Smarrimento/deterioramento", "RETRYABLE_EVENT", "RIS", "PROGRESS", False),
    "RECRSI001":  ("Avviato all'estero", "SAVE_ONLY_EVENT", "RIS", "PROGRESS", False),
    "RECRSI002":  ("Ingresso nel paese estero", "SAVE_ONLY_EVENT", "RIS", "PROGRESS", False),
}

ERROR_CATEGORIES = {
    "TRACKING_ID_NOT_FOUND": "TrackingId non trovato",
    "NOT_RETRYABLE_EVENT_ERROR": "Evento not retryable ricevuto",
    "RENDICONTAZIONE_SCARTATA": "Rendicontazione scartata",
    "DATE_ERROR": "Errore nella validazione delle date della sequenza",
    "LAST_EVENT_EXTRACTION_ERROR": "Errore nell'estrazione della sequenza dall'ultimo evento",
    "REGISTERED_LETTER_CODE_ERROR": "Errore nella validazione del registeredLetterCode",
    "DELIVERY_FAILURE_CAUSE_ERROR": "Errore nella validazione del deliveryFailureCause",
    "ATTACHMENTS_ERROR": "Errore nella validazione degli allegati della sequenza",
    "MAX_RETRY_REACHED_ERROR": "Numero massimo di retry raggiunto",
    "OCR_VALIDATION": "Errore nella validazione OCR",
    "DUPLICATED_EVENT": "Errore nella validazione della presenza di eventi duplicati",
    "INVALID_STATE_FOR_STOCK_890": "Stato non valido per giacenza 890",
    "INCONSISTENT_STATE": "Tracking incompleto o incoerente con la sequenza di eventi",
    "STATUS_CODE_ERROR": "Errore nella validazione della presenza degli elementi della sequenza",
}

ERROR_CAUSES = {
    "GIACENZA_DATE_ERROR": "Date di inizio e fine giacenza non coerenti",
    "OCR_DUPLICATED_EVENT": "Evento di risposta duplicato",
    "OCR_DRY_RUN_MODE": "Evento di risposta ignorato: modalità dry-run attiva per l'OCR",
    "OCR_KO": "Errore nella validazione dell'OCR",
    "OCR_UNSUPPORTED_PRODUCT": "Prodotto non supportato per la validazione OCR",
    "STOCK_890_REFINEMENT_MISSING": "Spedizione 890 non perfezionata",
    "STOCK_890_REFINEMENT_ERROR": "Errore nel perfezionamento della spedizione 890",
    "VALUE_AFTER_REFINEMENT": "Evento arrivato dopo la conclusione della spedizione o durante l'attesa dell'OCR",
    "VALUES_NOT_MATCHING": "Mancano degli allegati rispetto a quelli attesi",
    "INVALID_VALUES": "Errore nella validazione",
    "VALUES_NOT_FOUND": "Non sono presenti tutti gli statusCode previsti dalla macchina a stati",
    "SOURCETYPE_FILETYPE_INCOHERENT": "sourceType e fileType dell'allegato non sono coerenti",
}

#: tappe del flusso salvate su ``PaperTrackings.validationFlow``
FLOW_STEPS = (
    ("sequencesValidationTimestamp", "Sequenza validata"),
    ("refinementDematValidationTimestamp", "Demat di perfezionamento validata"),
    ("finalEventDematValidationTimestamp", "Demat dell'evento finale validata"),
    ("finalEventBuilderTimestamp", "Evento finale costruito"),
    ("recag012StatusTimestamp", "RECAG012 registrato"),
)
