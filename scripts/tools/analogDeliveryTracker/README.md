# Analog Delivery Tracker

Lo storico di una spedizione analogica, a partire dallo IUN o dal `trackingId`.

Legge le tabelle DynamoDB di `pn-paper-tracker` e costruisce, per ogni
destinatario della notifica, un'unica timeline che unisce gli eventi ricevuti,
gli errori delle validazioni, le tappe del flusso e gli output prodotti. Su
richiesta interroga anche CloudWatch e ricostruisce dai log gli output che non
sono finiti in nessuna tabella.

Le credenziali arrivano dalla cartella `~/.aws` della tua macchina, scelta col
selettore del browser. Niente server, niente backend: la pagina parla con AWS
per conto tuo, firmando le chiamate con le tue credenziali.

Va servita da un server statico qualunque — `npx serve`, l'estensione del tuo
editor, quello che hai sottomano. Non da `file://`: i moduli ES vogliono
un'origine.

## Come si usa

1. **Load .aws** e scegli la cartella `~/.aws`. Compaiono i profili, ordinati
   per ultimo usato e poi per frequenza.
2. Scegli il profilo: la pillola in alto diventa verde con region e account.
3. Cerca per **IUN** (con il prodotto, se lo sai) o direttamente per
   **trackingId**.

Dopo un `aws sso login` ripremi **Load .aws**: su Chromium il selettore **non
riappare** — la cartella resta ricordata, anche fra un refresh e l'altro — e i
file vengono riletti dal disco, quindi la credenziale nuova si vede subito. Il
profilo scelto resta quello.

Dove il selettore non c'è (Firefox, Safari) o è vietato (un iframe di origine
diversa dal top) la cartella va riscelta ogni volta: da un `<input>` non resta
nessun riferimento vivo da ricordare.

La ricerca finisce nell'URL, quindi si può condividere o mettere nei preferiti:

```
?iun=UWXU-VKXM-QVJZ-204009-M-1&product=AR&account=12345678&region=eu-south-1
```

L'account nell'URL non è un profilo — i nomi dei profili sono di chi apre la
pagina — ma viene tradotto nel profilo che punta a quell'account.

## Le opzioni avanzate

| | |
| --- | --- |
| **Output dry-run** | interroga anche `PaperTrackerDryRunOutputs`; costa una query per tracking |
| **Tappe di validazione e OCR** | aggiunge alla timeline `validationFlow` e le richieste OCR |
| **Recupera eventi prodotti dai log** | scarica i log attorno a ogni evento e ne ricava gli output verso delivery-push |
| **Ordina per** | arrivo sul tracker (default) o data dichiarata nell'evento |
| **Max RECINDEX / ATTEMPT** | quanto spingersi prima di arrendersi |
| **Log ± min** | la finestra attorno all'evento |

## Quanto costa una ricerca per IUN

Destinatari e tentativi sono numerati da zero senza salti, quindi si sale finché
si trova e ci si ferma al primo vuoto: una query per ogni `(RECINDEX, ATTEMPT)`,
più una che chiude ciascun livello. Il `PCRETRY` non si cerca — l'`attemptId` è
la partition key di `attemptId-pcRetry-index`, e una query restituisce già tutti
i retry di quel tentativo.

Per una spedizione con un destinatario e un tentativo sono **quattro query**, e
per uno IUN inesistente **una**.

## Il diagramma

Ogni tracking ha un bottone che disegna la macchina a stati del prodotto con
sopra il cammino percorso. I diagrammi stanno in `diagrams/`, scritti a mano in
Mermaid, con i codici evento sulle frecce.

Il verde tocca agli **stati base**. Un macro stato non si colora perché ci si è
entrati — quello lo prende il suo stato d'ingresso — ma perché lo si è
*lasciato*, cioè quando arriva il suo evento finale. Diventa **rosso**, con una
❌ al posto della spunta, quando il `businessState` del tracking è KO: non è
l'esito del singolo codice, perché lo stesso evento finale può chiudere bene o
male a seconda della pratica.

Percorrere una freccia dice due cose: dove si è arrivati e da dove si partiva.
La seconda vale solo se quel codice sta su **una freccia sola**. Il furto
dell'AR, `RECRN006`, sta su tre — da `Presa in carico`, da `Inesito` e da `In
giacenza` — e marcarne le partenze accenderebbe stati mai attraversati. Per lo
stesso motivo quelle frecce non prendono la spunta. Il bersaglio invece va
sempre bene, perché le tre frecce finiscono tutte nello stesso posto.

## I log

Il bottone **Log** di una voce apre CloudWatch attorno a quell'istante, e solo
quando lo premi: una voce si interroga una volta sola e poi resta in memoria.

Con un codice evento la ricerca è in due tempi: prima le righe che nominano
`trackingId` e codice, poi si cerca la riga «Handling … and event: [CODE]», che
porta sempre il `trace_id`, e si riparte da quello. Così escono anche le righe
che il `trackingId` non lo scrivono, cioè quasi tutta la lavorazione.

`filterLogEvents` restituisce pagine vuote mentre scandaglia il gruppo, quindi
la paginazione prosegue finché c'è un `nextToken`: fermarsi alla prima pagina
vuota fa dire «nessun log» con i log presenti.

## Com'è fatto

| | |
| --- | --- |
| `index.html` | markup, Tailwind, Mermaid e il font del look `neo` |
| `diagrams/` | i diagrammi di flusso per prodotto |
| `src/constants.js` | region, tabelle, log group, schemi di `trackingId`, URL della console |
| `src/catalogs.js` | i 114 statusCode, le categorie d'errore, le cause, le tappe |
| `src/html.js` | `escape`, l'unica utilità che serve sia a `view` sia a `diagram` |
| `src/aws_dir.js` | cartella `.aws` → profili e credenziali, in ordine di affidabilità |
| `src/sdk.js` | l'SDK AWS dal CDN, e i due client |
| `src/data.js` | query a DynamoDB e CloudWatch, e decodifica di quello che tornano |
| `src/timeline.js` | dalle righe delle tabelle alle voci, raggruppate per destinatario |
| `src/diagram.js` | il diagramma del prodotto, con sopra il cammino percorso |
| `src/view.js` | composizione dell'HTML |
| `src/tracker.js` | la pagina: stato, gestori degli eventi, avvio |

Le dipendenze sono una catena, senza anelli: `html`/`constants` ← `sdk` ←
`data` ← `timeline` ← `diagram` ← `view` ← `tracker`. `escape` sta in un modulo
suo perché la usano sia `view` sia `diagram`, e `view` importa `diagram`.

Solo `tracker.js` tocca il DOM e tiene stato; gli altri sono funzioni che si
possono eseguire e collaudare fuori dal browser.

**`aws_dir` legge e sceglie, non chiama.** Restituisce una credenziale; i client
li costruisce `sdk.js`, e a parlare con AWS ci pensa l'SDK. È anche il momento
in cui l'SDK viene scaricato: chi apre la pagina e non si connette non scarica
niente.

## Tre cose da sapere

**Niente scambio di token SSO, e non serve.** `aws sso login` lascia credenziali
già pronte in `cli/cache`: si leggono e si usano. Rifare il flusso OIDC dal
browser non si potrebbe comunque, perché `portal.sso.*.amazonaws.com` non manda
header CORS.

**Le credenziali si passano come oggetto letterale**, scavalcando la catena dei
provider dell'SDK: nel browser andrebbe a cercare variabili d'ambiente e file
che non esistono.

**I due pacchetti dell'SDK restano in namespace separati.** Hanno 19 nomi in
comune — `ResourceNotFoundException` e `ThrottlingException` fra gli altri — che
sono classi diverse nei due servizi: fonderli farebbe vincere l'ultimo caricato,
e un `instanceof` guarderebbe la classe sbagliata senza dirlo a nessuno.

## Sicurezza

La pagina tiene in memoria credenziali AWS vive, con i permessi del profilo
scelto: tutto quello che segue esiste per quello.

- **CSP** in un `<meta>`, prima di ogni altra cosa. Niente `'unsafe-inline'`
  sugli script — in pagina non ce ne sono — e `connect-src` limitato a `'self'`
  e `*.amazonaws.com`: anche con uno script ostile accanto alle credenziali, non
  avrebbe dove spedirle.
- **SRI** su Tailwind, Mermaid e il font, tutti a versione esatta. Mermaid
  arriva come UMD apposta: `integrity` vale su un `<script src>`, mentre un
  `import` dentro un modulo non ha dove portarselo.
- **La `region` dall'URL è validata** con `/^[a-z0-9-]+$/`. Senza, un
  `?region=attacker.com/` fa puntare i link della console a
  `https://attacker.com/.console.aws.amazon.com/…`: non è XSS — lo schema è
  fisso — ma è una finta console a un clic, raggiunta da un link che sembra una
  normale condivisione. L'SDK si difende da sé e rifiuta le region che non sono
  hostname validi; quei link no. I link portano anche `noreferrer`, così IUN e
  account non viaggiano nel `Referer`.
- **I cataloghi si leggono con `Object.hasOwn`.** Un `statusCode` che si chiama
  come un membro di `Object.prototype` — `constructor`, `toString` — prima
  restituiva una funzione e faceva cadere l'intera ricerca.
- **Nessun segreto a riposo**: su disco finiscono l'handle della cartella e il
  nome del profilo, mai le chiavi.

Due rischi restano, e sono scelte più che bug:

**L'SDK AWS non ha SRI.** Arriva da `+esm`, che jsDelivr genera al volo e per
cui sconsiglia SRI, e un `import()` dinamico non avrebbe dove metterlo. Il
recinto lì è la CSP. Per toglierlo del tutto va vendorizzato nel repo.

**Su GitHub Pages l'origine è condivisa.** `https://<utente>.github.io` è la
stessa per *ogni* repo di quell'account pubblicato su Pages, e il permesso sulla
cartella `~/.aws` — come IndexedDB e localStorage — vive sull'origine. Un altro
progetto sulla stessa origine potrebbe riprendersi l'handle salvato e rileggere
la cartella. Si risolve con un dominio dedicato, non con il codice.

## Dove può inciampare

Il selettore di cartelle non si apre nei sotto-frame di origine diversa dal top:
Chromium lo vieta e non esiste un `allow=` per autorizzarlo. Lì la pagina
ripiega su `<input webkitdirectory>`, che è un normale controllo di form — e
perde la memoria della cartella, perché quel che si ricorda in IndexedDB è il
riferimento vivo che solo il selettore nativo restituisce.

**In incognito, su Chrome 153**, rileggere quel riferimento dopo un reload
uccide il renderer: è una regressione di Chromium, non un errore che si possa
intercettare. Fuori dall'incognito non si presenta, e si è scelto di tenere la
comodità invece di pagarne il prezzo ovunque.

Se il permesso sulla cartella ricordata è stato revocato, il primo clic lo
richiede e te lo dice: `requestPermission` consuma il gesto dell'utente, quindi
in quel giro il selettore non si può più aprire. Basta premere di nuovo.

Per lo stesso motivo `navigator.clipboard` è negato senza
`allow="clipboard-write"`, e i bottoni «copia» ripiegano su `execCommand`.

Il look `neo` di Mermaid misura le etichette col font *Recursive Variable*, che
Mermaid nomina ma non distribuisce: `index.html` lo carica, e il disegno aspetta
`document.fonts.ready`. Disegnare prima vuol dire squadrare i riquadri su Arial
e ritrovarsi i titoli tagliati quando il font subentra.
