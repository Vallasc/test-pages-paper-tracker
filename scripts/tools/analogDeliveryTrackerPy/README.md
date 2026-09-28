# Analog Delivery Tracker

Pagina statica (PyScript + Tailwind) che ricostruisce lo **storico completo di una
spedizione analogica a partire dallo IUN**, leggendo direttamente le tabelle
DynamoDB di `pn-paper-tracker` con le credenziali della tua cartella `~/.aws`.

Per ogni destinatario della notifica viene mostrata un'unica timeline che unisce:

* gli **eventi** ricevuti (`PaperTrackings.events`), ognuno con
  la descrizione testuale dello `statusCode` presa dal catalogo di
  `EventStatusCodeEnum`;
* gli **errori** generati dalle validazioni (`PaperTrackingsErrors`), con la
  descrizione di categoria e causa;
* le **tappe del flusso** di validazione e le richieste/risposte **OCR**;
* gli **output dry-run** che il tracker avrebbe inviato a delivery-push,
  se accendi l'opzione: è una query in più per tracking.

Ogni voce porta due date: in alto l'arrivo sul tracker, in fondo lo
`statusTimestamp` dichiarato nell'evento, che può essere molto più
indietro. Le voci sono ordinate per arrivo; da «Opzioni avanzate» si passa alla
data evento.

Dentro la card di un destinatario ogni tracking ha il suo blocco, intestato col
tentativo e il retry (`ATTEMPT_0 · PCRETRY_1`) e col `trackingId` per esteso:
le voci di tentativi diversi non si mescolano in un'unica scaletta.
L'intestazione resta appiccicata sotto la barra in cima mentre si scorre il
blocco, così non si perde di vista a quale tracking appartiene quello che si sta
leggendo.

Nell'intestazione di ogni blocco ci sono i suoi link alla console: una query per
`trackingId` su ciascuna tabella che ha davvero risposto — col numero di record
accanto — e i log filtrati su quell'unico `trackingId`, già aperti
sull'intervallo che copre le sue voci. Ogni link porta accanto il bottone per
copiarne l'URL.

L'intervallo nel link dei log non è un vezzo: senza `start` e `end` la console
apre sulla finestra di default, e per un evento di ieri non trova niente.

La query è sulla sola partition key, quindi può restituire più record: in
`pn-PaperTrackingsErrors` e `pn-PaperTrackerDryRunOutputs` il `trackingId` è
accompagnato da `created` come sort key.

Il bottone copia usa `navigator.clipboard` e ripiega su `document.execCommand`:
dentro un iframe di altra origine il primo è vietato se l'iframe non porta
`allow="clipboard-write"`.

## Un limite noto: incognito su Chrome 153

L'handle della cartella `.aws` sta in IndexedDB, così un refresh non fa
ripartire dal selettore. In **incognito su Chrome 153** rileggerlo dopo un
reload manda in crash il renderer: è una regressione di Chromium fra la 151 e
la 153, e non è un errore intercettabile — la pagina muore e basta.

Non è aggirato: girarci intorno costava la cartella ricordata anche in
navigazione normale, dove il problema non si presenta. In incognito, quindi,
ricaricare la pagina la fa cadere; basta riaprirla.

In fondo a ogni voce ci sono due bottoni: **JSON** apre l'item grezzo recuperato
da DynamoDB, in un albero colorato che nasce tutto aperto e si richiude ramo per
ramo; **Log** apre i log del microservizio attorno a quell'evento. Usano lo
stesso modale.

L'`id` di un evento è il `messageId` della coda SQS: se lo stesso id compare su
più eventi dello stesso destinatario, la coda ha riconsegnato quel messaggio e
le voci vengono marcate con un badge «riconsegna coda».

## Come si usa

1. Fai login sul profilo che ti interessa e popola la cache delle credenziali:

   ```bash
   aws sso login --profile sso_pn-core-dev && \
     aws sts get-caller-identity --profile sso_pn-core-dev --no-cli-pager
   ```

   Servono entrambi, per ogni profilo che vuoi usare: il login scrive solo il
   token in `sso/cache`, mentre le credenziali del ruolo finiscono in
   `cli/cache` quando lanci un comando con quel profilo. `--no-cli-pager` tiene
   l'output fuori da `less`, che a schermo sembra un editor rimasto aperto. Gli
   stessi due comandi li trovi già uniti sotto ogni errore di credenziali, con
   un bottone per copiarli.

2. Servi la cartella via HTTP (`file://` non funziona: le chiamate ad AWS
   partono dal browser e da un'origine `null` il CORS le blocca):

   ```bash
   cd scripts/tools/analogDeliveryTracker
   python3 -m http.server 8000
   ```

3. Apri <http://localhost:8000/> e premi **Load .aws** per dare al browser
   l'accesso in lettura a `~/.aws`.

   Il selettore di cartelle di Chrome è vietato dentro un iframe la cui origine
   non è quella della pagina che lo ospita, e fuori da Chromium non esiste: in
   quei casi il bottone ripiega da solo su un `<input type="file"
   webkitdirectory>`, che legge la stessa cartella senza quel limite.

4. Scegli il profilo dal menu in alto: la credenziale attiva viene presa
   automaticamente (cache SSO, `credentials`, variabili d'ambiente…) e la pill in
   alto a destra mostra account e region del collegamento. L'ultimo profilo usato
   viene riproposto al caricamento successivo e i profili più usati restano in
   cima alla lista.

5. Inserisci lo IUN, scegli il prodotto e premi **Cerca**: il `trackingId` viene
   composto da solo, secondo gli schemi qui sotto.

   Con «Cerca per» su **trackingId** si interroga invece un singolo tracking,
   scritto per intero: niente ricostruzione, quindi prodotto e limiti di
   scansione restano spenti.

Il primo caricamento è lento: Pyodide installa `boto3` da PyPI. Le versioni
sono fissate in `index.html` — anche quella di `botocore`, che boto3 lascerebbe
libera dentro la `1.43.x` ed è il pacchetto di cui `aws_dir.py` rimpiazza il
transport.

## Ricerche condivisibili

Ogni ricerca finisce nell'URL, e riaprendolo la pagina si ritrova com'era:

```
?iun=ZXVK-VHWH-LUZK-202609-T-1&product=AR&account=830192246553&region=eu-south-1
?trackingId=PREPARE_ANALOG_DOMICILE.IUN_….PCRETRY_0&account=830192246553&region=eu-south-1
```

`account` non è un profilo: i nomi dei profili dipendono dal `~/.aws` di chi
apre la pagina, l'account id no. Letta la cartella, viene scelto il profilo che
punta a quell'account; se ce n'è più d'uno — `prod`, `prod-oncall`,
`prod-readonly` guardano lo stesso — vince quello più in alto nella classifica
d'uso. Se nessun profilo punta lì, la scelta resta quella di sempre.

La ricerca non parte da sola: la cartella `.aws` si può leggere solo dopo un
gesto dell'utente, quindi i campi si trovano pronti e resta da premere
**Cerca**.

## Come vengono trovati i tracking

I `trackingId` non sono indicizzati per IUN, quindi vengono ricostruiti. Le
forme dipendono dal prodotto:

```
AR, 890, RIR   PREPARE_ANALOG_DOMICILE.IUN_<iun>.RECINDEX_<r>.ATTEMPT_<a>.PCRETRY_<p>
RS, RIS        PREPARE_SIMPLE_REGISTERED_LETTER.IUN_<iun>.RECINDEX_<r>.PCRETRY_<p>
RS bonarie     PREPARE_ANALOG_MESSAGE.IUN_<iun>.RECINDEX_<r>.ATTEMPT_<a>.DELIVERYTYPE_RS.PCRETRY_<p>
```

Solo i prodotti con più feedback per IUN hanno l'`ATTEMPT`. Le comunicazioni
bonarie esistono solo come RS, hanno un prefisso proprio e chiudono l'attemptId
col tipo di recapito: scegliendo RS — o «Tutti» — vengono provate entrambe le
forme, e i destinatari trovati così portano il chip «bonarie».

Tutto tranne l'ultima parte è l'`attemptId`, partition key dell'indice
`attemptId-pcRetry-index`: una query per ogni coppia `(RECINDEX, ATTEMPT)`
restituisce tutti i `PCRETRY` di quel tentativo, che quindi non si cercano uno
per uno.

Destinatari e tentativi sono numerati da 0 senza salti, quindi la scansione sale
finché trova e si ferma al primo vuoto. Per una spedizione con un destinatario e
un tentativo sono 3 query, più 1 per gli errori di ogni tracking e 1 per gli
output dry-run se l'opzione è accesa. I due tetti in «Opzioni avanzate» contano
solo su spedizioni lunghe: di norma il ciclo finisce prima.

## Diagramma del flusso

Nell'intestazione di un tracking, accanto ai link, compare **Diagramma** quando
per quel `productType` esiste un file in `diagrams/` (per ora solo `AR`).
Mostra la macchina a stati del prodotto con evidenziato il cammino di quella
spedizione: le transizioni i cui `statusCode` sono arrivati portano un ✓, e gli
stati in cui portano sono verdi.

I codici stanno sulle frecce, e in uno `stateDiagram` le frecce non si possono
colorare con `classDef`: da qui il ✅ sull'etichetta della freccia e il colore
sullo stato di arrivo. Gli stati base si accontentano del colore — sono tanti, e
una spunta per ognuno è rumore; ce l'ha invece il macro stato che chiude, perché
lì il segno dice anche *come* si è chiuso. Il codice si legge fino al primo
carattere che non sia lettera maiuscola o cifra, perché nei diagrammi 890
l'etichetta continua con le varianti del documento:
`RECAG005B<br>[ARCAD|CAD]\(\*\)`. I codici che non appartengono alla macchina
del prodotto — quelli del consolidatore o della stampa, come `CON020` —
semplicemente non trovano riscontro e non segnano niente.

Percorrere una freccia dice due cose: dove si è arrivati e da dove si partiva.
La seconda vale solo se quel codice sta su **una freccia sola**. Il furto
dell'AR, `RECRN006`, sta su tre — da `Presa in carico`, da `Inesito` e da `In
giacenza`, tutte verso `Furto` — e marcarne le partenze accendeva stati mai
attraversati: con `P000, CON080, RECRN006, CON020` si illuminavano anche
`Inesito` e `In giacenza`. Ora la sorgente si marca solo se il codice è
inequivocabile, e per lo stesso motivo quelle frecce non prendono la spunta: ne
è stata percorsa una, segnarle tutte e tre direbbe il falso. Il bersaglio invece
va sempre bene, perché quelle tre frecce finiscono tutte nello stesso posto, ed
è il suo verde a dire che l'evento è arrivato.

Il verde tocca agli **stati base**. Un macro stato non si colora perché ci si è
entrati — quello lo prende il suo stato d'ingresso — ma perché lo si è
*lasciato*, cioè quando arriva il suo evento finale: le frecce `RECRN00xC` che
chiudono il fascicolo. Il suo verde è più chiaro di quello degli stati base,
perché gli fa da sfondo e non deve confondersi con loro, e diventa **rosso**
quando il `businessState` del tracking è KO — lo stesso valore del chip
"business" in testa al destinatario — e in quel caso la sua spunta diventa ❌.
Non è l'esito del singolo codice: lo stesso evento finale può chiudere bene o
male a seconda della pratica, ed è il business a dirlo. Gli stati base dentro
restano verdi: il cammino è stato percorso, è l'esito a essere KO.

Nel frontmatter dei `.mermaid`, `look: neo` e `layout: elk` sono i default degli
stateDiagram da **Mermaid 12** e stanno scritti solo per non dipendere da quelli
di domani; `theme: redux` invece è una scelta, al posto del `redux-color` di
default, perché riquadri già colorati uno a uno toglierebbero l'occhio al verde;
`themeVariables.edgeLabelBackground` mette su bianco i codici scritti sulle
frecce, che di serie stanno su una pillola grigia.

Il look `neo` misura le etichette col font *Recursive Variable*, che Mermaid
nomina ma non distribuisce: `index.html` lo carica da
`@fontsource-variable/recursive` e `tracker.show_diagram` aspetta
`document.fonts.ready` prima di disegnare — disegnare prima vuol dire squadrare
i riquadri su Arial e ritrovarsi i titoli tagliati quando il font subentra.

Aggiungere un prodotto è una riga in `constants.DIAGRAMS` più il file. Un
prodotto può averne più d'uno: l'890 ha il flusso di consegna e, a parte, il
dettaglio della giacenza, che nel primo è un riquadro solo. Il modale li disegna
uno sotto l'altro, ciascuno col suo titolo.

## Il transport di boto3

In Pyodide botocore non parla via socket: `aws_dir.py` sostituisce
`URLLib3Session.send` con una XMLHttpRequest sincrona. Da urllib3 2.x, però, il
pacchetto si inietta da solo un backend Emscripten quando
`sys.platform == "emscripten"`, quindi quella sostituzione potrebbe non servire
più.

Si può provare aprendo la pagina con **`?transport=native`**: il transport resta
quello di urllib3 e il resoconto finisce in console (`aws_dir: {...}`). Senza il
parametro vale la strada nota, con XHR.

**Provato su Pyodide 314 / Chrome 153: non funziona.** DynamoDB risponde `404`
con una pagina HTML «Page Not Found», che è esattamente ciò che l'endpoint
restituisce a un `POST /` senza il `Content-Type: application/x-amz-json-1.0`
(verificato a parte con curl: senza quell'header è 404, con header sbagliati o
mancanti ma content-type giusto è 400). Per strada il backend Emscripten perde
quell'intestazione.

Il percorso è questo: botocore chiama `urlopen(preload_content=False)`, quindi
si prova la via in streaming, che vuole SharedArrayBuffer e quindi la
cross-origin isolation (`COOP`/`COEP`) che un `python3 -m http.server` non dà;
si ripiega su `send_request`, che con la JSPI di Chrome usa `fetch`. Lì il
filtro degli header confronta i nomi **senza normalizzare il maiuscolo** contro
una lista tutta minuscola, al contrario del ramo XHR che fa `name.lower()`.

Morale: la sostituzione con XHR resta necessaria, e salta a mano gli header che
il browser vieta. L'interruttore è rimasto per rifare la prova su una Pyodide
futura.

## Log CloudWatch

Il bottone **Log** di una voce apre un modale con le righe di
`/aws/ecs/pn-paper-tracker` attorno a quell'evento, in una finestra di ±5 minuti
(regolabile da «Opzioni avanzate»). La query parte all'apertura del modale, non
prima, e il risultato di una voce resta in memoria: riaprirla non torna a
interrogare CloudWatch. La memoria si svuota a ogni nuova ricerca. Serve il
permesso `logs:FilterLogEvents`.

Per una voce che ha un suo codice la ricerca è in due passi: prima
`"<trackingId>" "<statusCode>"`, poi dalla riga
`Handling … and event: [<statusCode>]` si prende il `trace_id` e si ricerca solo
quello. Così escono anche le righe della lavorazione che il `trackingId` non lo
scrivono. Il codice è lo `statusCode` dell'evento, o l'`eventThrow` per le voci
di errore; «Tracking creato» resta sul solo `trackingId`, e il sottotitolo del
modale dice sempre quale filtro è finito in uso.

Il trace si prende da quella riga e non dalla presa in carico perché è l'unica
che lo porta sempre: `Handle message from pn-external_channel_to_paper_tracker`
su certi percorsi non ha nessun campo di tracciatura. Il nome del campo è
`trace_id`; altrove compare come `traceId`, e vengono letti entrambi.

Se la riga di lavorazione non c'è si ripiega sul primo passo, ma a partire dalla
presa in carico, così l'hop a monte — `…_to_paper_channel`, che è un'altra
lavorazione con un altro trace — resta comunque fuori.

Con **Recupera eventi prodotti dai log** — spenta di default — la ricerca
interroga CloudWatch attorno a tutti gli eventi di ogni tracking e aggiunge alla
timeline gli output inviati a delivery-push, ricavati dalle righe
`Sending to output target for event: {…}`. È l'equivalente di
`PaperTrackerDryRunOutputs` dove il dry-run è spento: là l'evento è una riga di
tabella, qui una riga di log. Quelle voci portano il badge «dai log», e il
codice mostrato è lo `statusDetail`, non lo `statusCode` (che lì vale `PROGRESS`
o simili). L'orario della voce è quello della riga di log — il momento in cui il
tracker l'ha prodotta — non una data dentro il payload.

Le finestre attorno a eventi vicini vengono fuse in una query sola, e le righe
scaricate restano in cache: aprendo dopo il modale di una voce, il primo passo
si filtra in memoria invece di tornare a CloudWatch.

`filter_log_events` restituisce pagine vuote mentre scandaglia il gruppo, quindi
la paginazione prosegue finché c'è un `nextToken`, non finché una pagina è
vuota: fermarsi alla prima pagina vuota fa dire «nessun log» con i log presenti.

## File

| File | Cosa contiene |
| --- | --- |
| `index.html` | markup, Tailwind e configurazione PyScript |
| `diagrams/` | i diagrammi di flusso per prodotto, in Mermaid |
| `src/constants.py` | region, prefisso tabelle, log group, schemi di `trackingId` e cataloghi degli enum |
| `src/data.py` | query a DynamoDB e CloudWatch, e decodifica di quello che tornano |
| `src/timeline.py` | dalle righe delle tabelle alle voci, raggruppate per destinatario |
| `src/diagram.py` | il diagramma del prodotto, con sopra il cammino percorso |
| `src/view.py` | composizione dell'HTML |
| `src/tracker.py` | la pagina: stato, gestori degli eventi, avvio |
| `src/aws_dir.py` | libreria per leggere `~/.aws` e creare client boto3 nel browser (PyScript la monta piatta, quindi si importa con `import aws_dir`) |

Le dipendenze sono una catena, senza anelli: `constants` ← `data` ← `timeline`
← `diagram` ← `view` ← `tracker`. Solo `tracker.py` tocca il DOM e tiene stato;
gli altri sono funzioni che si possono eseguire e collaudare fuori dal browser. Ogni
modulo nuovo va aggiunto anche alla mappa `files` in `index.html`, altrimenti
PyScript non lo monta: la chiave è il percorso da cui scaricarlo (`./src/…`),
il valore il nome con cui viene montato, che resta piatto — per questo gli
import restano `import data`, senza pacchetto.

Region (`eu-south-1`) e prefisso delle tabelle (`pn`) sono costanti in
`constants.py`, non campi della pagina.

I cataloghi in `constants.py` (`STATUS_CODES`, `ERROR_CATEGORIES`,
`ERROR_CAUSES`) replicano gli enum Java: se cambiano `EventStatusCodeEnum`,
`ErrorCategory` o `ErrorCause`, vanno aggiornati anche qui.
