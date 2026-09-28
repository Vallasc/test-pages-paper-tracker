# AWS JS Probe

Un `GetItem` su DynamoDB nel browser, leggendo `~/.aws`, **in JavaScript e
basta**: nessun bundler, nessun runtime. Due strade per firmare, scelte dal
menù nella pagina:

- **a mano** — SigV4 con la WebCrypto che il browser ha già. Zero dipendenze,
  zero download, quaranta righe.
- **SDK** — `@aws-sdk/client-dynamodb` caricato come modulo ES da jsDelivr,
  come lo useresti in un progetto vero. Arriva alla prima chiamata, non prima.

Serve a rispondere a una domanda sola: l'Analog Delivery Tracker ha bisogno di
Pyodide e boto3, o quella era una scelta e non un vincolo?

## Cosa prova

| | Analog Delivery Tracker | qui, a mano | qui, con l'SDK |
| --- | --- | --- | --- |
| da scaricare | Pyodide + boto3, decine di MB | niente | 33 moduli, ~500 KB |
| transport | `URLLib3Session` sostituito a mano | `fetch` | `fetch` |
| firma | botocore | 40 righe di `crypto.subtle` | l'SDK |
| lettura di `~/.aws` | File System Access API via Pyodide | File System Access API | idem |

Il transport rattoppato — la parte più delicata dell'altro tool — **sparisce**.
Esiste solo perché botocore presume di avere un socket; `fetch` no.

## Verificato

- **Entrambe le firme sono valide.** `GetItem` su `pn-PaperTrackings` in
  `eu-south-1` con le credenziali di `sso_pn-core-dev` risponde senza elemento —
  e non `InvalidSignatureException`. Provata sia la firma a mano sia quella
  dell'SDK con lo stesso oggetto credenziali.
- **Il grafo dell'SDK è caricabile dal browser:** 33 moduli, ~500 KB, nessun
  builtin Node.
- **La CORS regge.** Il preflight `OPTIONS` verso `dynamodb.eu-south-1.amazonaws.com`
  risponde `Access-Control-Allow-Origin: *` e consente esattamente gli header
  che servono: `authorization, content-type, x-amz-date, x-amz-target,
  x-amz-security-token`.
- **I profili si leggono.** 17 profili dal `config`, e la graduatoria mette in
  cima la cache il cui account coincide con l'`sso_account_id` del profilo.

## L'SDK senza bundler

`@aws-sdk/client-dynamodb` si carica come modulo ES da jsDelivr, con un
`import()` dinamico dentro la funzione che lo usa: la pagina resta istantanea
se scegli la firma a mano.

Che funzioni non è scontato, e l'ho verificato percorrendo tutto il grafo dei
moduli come farebbe il browser: **33 moduli, circa 500 KB, nessun import di
builtin Node, niente che manchi**. Il merito è che jsDelivr risolve la
condizione *browser* del pacchetto, quindi tira `@smithy/fetch-http-handler` al
posto del gestore HTTP di Node. Con un CDN che non lo facesse, il primo
`import` di `node:http` farebbe cadere tutto.

Le credenziali si passano come oggetto letterale, scavalcando la catena dei
provider: nel browser andrebbe a cercare variabili d'ambiente e file che non
esistono. Restano quelle lette da `.aws`, identiche alle due strade.

## Le due trappole

**`host` si firma ma non si spedisce.** È un header riservato al browser, che lo
mette da sé con lo stesso valore dell'URL: se resta nel dizionario spedito,
`fetch` lo rifiuta; se non entra nella firma, AWS la rifiuta. Va in
`SignedHeaders` e poi si cancella. È lo stesso inciampo che in `aws_dir.py` ha
la lista `_FORBIDDEN`.

**Niente scambio di token SSO, e non serve.** `aws sso login` lascia credenziali
già pronte in `sso/cache`, e `aws sts assume-role` fa lo stesso in `cli/cache`:
qui si leggono e si usano. Rifare il flusso OIDC dal browser sarebbe un'altra
storia — quegli endpoint la CORS non la concedono.

## Cosa manca apposta

È una prova, non un tool. Non c'è la memoria della cartella in IndexedDB, non
c'è il controllo dei permessi dopo un refresh, non c'è `Query` né CloudWatch,
e la resa degli errori è una riga di testo. Il ripiego su
`<input webkitdirectory>` invece c'è, perché senza quello in un iframe di
origine diversa dal top il selettore non si apre e non si prova niente.

## File

| | |
| --- | --- |
| `index.html` | la pagina, con il suo CSS |
| `src/aws_dir.js` | cartella `.aws` → file, profili, credenziali in ordine |
| `src/sigv4.js` | la firma SigV4 a mano, con WebCrypto |
| `src/dynamo.js` | `GetItem`, per l'una o l'altra strada |
| `src/main.js` | stato, gestori, DOM |

Va servito da un server, non aperto con `file://`: i moduli ES vogliono
un'origine. Basta `python3 -m http.server` nella cartella.
