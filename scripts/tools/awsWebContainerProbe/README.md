# AWS WebContainer Probe

Uno script Node **non modificato** che legge `.aws` e interroga DynamoDB,
eseguito dentro un WebContainer nel browser.

Risponde alla domanda vera: si possono riusare gli script Node che già abbiamo,
invece di riscriverli per il browser?

```
node serve.mjs        →  http://localhost:8099
```

Il server non è un vezzo: senza i suoi header la pagina non parte (sotto). In
hosting statico, dove gli header non si possono impostare, ci pensa
`coi-serviceworker.js`.

## Come si legge

Lo script in `container/get-item.mjs` non sa di girare in un browser: è il
codice che scriveresti comunque, con l'SDK AWS e `fromSSO`. Gira identico da
terminale:

```
npm --prefix container install
AWS_PROFILE=sso_pn-core-dev node container/get-item.mjs \
    pn-PaperTrackings trackingId UNA_CHIAVE
```

La pagina fa quattro cose: accende il container, ci **copia dentro** `.aws` e
il `package.json`, esegue `npm install` e poi `node get-item.mjs`, riportando
tutto l'output.

## «Nessuna chiamata deve passare da un proxy»

Due strumenti, che si controllano a vicenda.

**L'audit di rete**, in fondo alla pagina, sorveglia il perimetro: il runtime
scaricato all'avvio, il CDN, e qualunque host inatteso. Va letto sapendo cosa
*non* vede: il container gira dentro un iframe di un'altra origine
(`<id>.w-corp-staticblitz.com`), che ha il suo `window`, quindi le sue chiamate
da qui non passano — DynamoDB compreso. Era una mia pretesa sbagliata, corretta
dopo la prima prova vera.

**`fromSSO` come cartina di tornasole.** Non legge solo file: scambia il token
SSO chiamando `GetRoleCredentials` su `portal.sso.<region>.amazonaws.com`, e
quell'endpoint non manda header CORS — verificato con un preflight, risponde
senza `Access-Control-Allow-Origin`. Quindi:

| `fromSSO` da dentro il container | cosa significa |
| --- | --- |
| fallisce con un **errore di rete** | la richiesta è uscita e la CORS l'ha fermata: nessuno in mezzo, è quello che vogliamo |
| **riesce** | ha raggiunto AWS senza avere la CORS, quindi è passata da un intermediario — col bearer token dentro |
| fallisce per altro (token scaduto) | non dice niente: la chiamata non è nemmeno partita |

Quella terza riga esiste perché la prima stesura dichiarava «la CORS l'ha
fermata» per qualunque errore, e un token scaduto l'avrebbe fatta mentire. Ora
lo script distingue i due casi e lo scrive.

### Il risultato

```
1. fromSSO() — scambia il token SSO su portal.sso.…amazonaws.com
   FALLITO: TimeoutError: socket hang up
   → guasto di RETE: la richiesta è uscita e qualcuno l'ha fermata.
```

e in console, dal browser:

```
Access to fetch at 'https://portal.sso.eu-west-1.amazonaws.com/federation/credentials?…'
from origin 'https://<id>.w-corp-staticblitz.com'
has been blocked by CORS policy: No 'Access-Control-Allow-Origin' header…
```

**Questa è la prova.** Un errore CORS esiste solo se il browser ha davvero
provato a raggiungere quell'host: l'URL è quello vero di AWS, non riscritto, e a
fermarlo è stata la politica del browser. Se qualcuno avesse instradato la
richiesta altrove non ci sarebbe stato niente da bloccare — e `fromSSO` sarebbe
*riuscito*. Il fallimento è la conferma.

Lo conferma anche il verso opposto: la `GetItem` su DynamoDB **riesce**, e AWS
accetta la firma. `host` è uno degli header firmati SigV4, quindi se qualcuno lo
avesse riscritto la firma sarebbe saltata.

Una cosa da sapere comunque: il codice gira in un iframe servito da StackBlitz,
e il runtime che esegue il tuo script arriva da loro. Il **traffico** non passa
dai loro server — questo è dimostrato — ma il **runtime** è roba loro, e quella
fiducia te la prendi lo stesso.

Terza precauzione, meno vistosa: **il service worker dell'isolamento tocca solo
la propria origine**. Intercettare anche le richieste ad AWS sarebbe stato
comodo e inutile, e l'avrebbe messo in mezzo proprio a quelle che non devono
avere nessuno in mezzo.

## I tre vincoli, verificati

**La pagina dev'essere cross-origin isolated.** Il WebContainer gira su
`SharedArrayBuffer`, che il browser concede solo con
`Cross-Origin-Opener-Policy: same-origin` e
`Cross-Origin-Embedder-Policy: require-corp`. `python3 -m http.server` non li
manda: da lì la pagina carica e poi fallisce al boot. Per questo c'è
`serve.mjs`, e la pagina controlla `crossOriginIsolated` prima di offrirti il
bottone. Verificato: il server risponde con entrambi gli header.

Sotto `require-corp` ogni risorsa esterna dev'essere consenziente. Il pacchetto
`@webcontainer/api` arriva da jsDelivr, che manda
`cross-origin-resource-policy: cross-origin` — verificato anche questo, quindi
passa.

**Il filesystem è virtuale.** Il container non vede la tua macchina: i file si
copiano dentro con `mount()`, nella forma ad albero che vuole lui
(`{directory: {…}}`, `{file: {contents}}`). Per *avere* quei file serve
comunque la File System Access API. Cioè: il passaggio più fastidioso non se ne
va, se ne aggiunge uno.

E quella copia è un'**istantanea**: un `aws sso login` fatto dopo non la
raggiunge, e ti ritrovi a firmare con credenziali scadute senza capire perché.
Per questo la pagina rilegge la cartella dal riferimento vivo prima di ogni
esecuzione — è la stessa trappola che nell'Analog Delivery Tracker si era
presentata come «le credenziali risultano scadute anche dopo il login».

**`$HOME` va fatto combaciare col workdir.** L'SDK cerca il token SSO sotto
`os.homedir()`, e non esiste una variabile per spostare quella cartella:
`AWS_CONFIG_FILE` copre `config` e `credentials`, non `sso/cache`. Su POSIX
`os.homedir()` segue `$HOME`, quindi basta passarlo allo `spawn`. Senza,
`fromSSO` non trova il token e dice *invalid* — un fallimento che non c'entra
niente con la rete e che falserebbe il test del proxy.

**L'SSO cachato funziona, lo scambio del token no.** `aws sso login` lascia
credenziali già pronte in `cli/cache`: quelle si leggono e si usano. Ma
`fromSSO` dell'SDK non le guarda — rifà lo scambio chiamando il portale SSO, e
quell'endpoint la CORS non la concede. Il login, e lo scambio, li fai fuori.

## Su GitHub Pages

GitHub Pages non lascia impostare header HTTP, quindi la pagina arriverebbe
senza isolamento e il container non partirebbe. Rimedia `coi-serviceworker.js`:
un service worker che intercetta le risposte della propria origine e i due
header glieli aggiunge lui. Lo stesso file fa due mestieri — caricato dalla
pagina si registra e ricarica **una volta sola**, perché la prima navigazione
non era ancora sotto il suo controllo; eseguito come service worker, riscrive
le risposte.

I suoi vincoli non sono aggirabili: file a parte, servito dalla **tua** origine
e non da un CDN, e pagina in HTTPS o su localhost. GitHub Pages li soddisfa
tutti. In locale non dà fastidio: vede che `serve.mjs` ha già fatto il lavoro e
non si registra nemmeno.

Resta invece insuperabile l'altra strada: **dentro un iframe di origine diversa
dal top non funziona comunque**. La permissions policy `cross-origin-isolated`
ha `self` come allowlist di default, quindi un iframe cross-origin ne è escluso
a meno che la pagina che lo incorpora non scriva `allow="cross-origin-isolated"`
sull'iframe *e* sia a sua volta isolata. Se quella pagina non è tua, non c'è
service worker che tenga: il WebContainer va aperto a tutta pagina.

## Provato, e funziona

Su Chrome, 27 settembre 2026:

```
$ npm install
added 31 packages in 3s

$ node get-item.mjs …
node v22.22.3 — profilo sso_pn-core-dev, region eu-south-1
.aws in /home/cu1u…bz5f/.aws: cli, config, credentials, sso
…
3. GetItem con l'SDK su pn-PaperTrackings dove trackingId = XXXXX
   nessun elemento con quella chiave
→ AWS ha risposto: l'SDK ha firmato e la chiamata è arrivata.
```

Il dubbio che restava era se StackBlitz instradasse la `fetch` in un proprio
proxy riscrivendo l'host: in quel caso la firma SigV4 sarebbe saltata, perché
`host` è uno degli header firmati. **AWS ha accettato la firma**, quindi l'host
è arrivato intatto. Resta da confermare guardando l'audit che l'host compaia
per quello che è.

Nota di lettura: `npm install` dentro il container funziona e va in rete verso
il registro, ma quel traffico è del runtime, non delle credenziali.

## Confronto onesto

| | `awsJsProbe` | qui |
| --- | --- | --- |
| da scaricare | niente | il runtime WebContainer |
| header speciali | nessuno | COOP + COEP, da un server o da un service worker |
| su GitHub Pages | subito | solo col service worker, e a tutta pagina |
| in un iframe cross-origin | ci sta | no, la permissions policy lo esclude |
| `.aws` | letta e usata | letta, **poi copiata** nel container |
| il codice | da riscrivere in JS per il browser | quello che hai già |

L'unica colonna che il WebContainer vince è l'ultima, ed è la ragione per cui ha
senso guardarlo: se gli script Node esistono e sono tanti, riscriverli costa più
di tutto il resto. Se invece il codice va scritto da zero, sta pagando un prezzo
alto per niente.

## File

| | |
| --- | --- |
| `serve.mjs` | server statico con COOP/COEP, per lo sviluppo in locale |
| `coi-serviceworker.js` | gli stessi header via service worker, per l'hosting statico |
| `index.html` | la pagina, con il suo CSS |
| `src/main.js` | boot, mount, spawn, output |
| `src/aws_files.js` | cartella `.aws` → file → albero per `mount()` |
| `src/netaudit.js` | ogni richiesta fuori origine, per vedere chi parla con chi |
| `container/get-item.mjs` | lo script Node che gira dentro (e da terminale) |
| `container/package.json` | le dipendenze che `npm install` tira dentro il container |
