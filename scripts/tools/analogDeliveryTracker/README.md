# Analog Delivery Tracker

Pagina statica (PyScript + Tailwind) che ricostruisce lo **storico completo di una
spedizione analogica a partire dallo IUN**, leggendo direttamente le tabelle
DynamoDB di `pn-paper-tracker` con le credenziali della tua cartella `~/.aws`.

Per ogni destinatario della notifica viene mostrata un'unica timeline che unisce:

* gli **eventi** ricevuti dal consolidatore (`PaperTrackings.events`), ognuno con
  la descrizione testuale dello `statusCode` presa dal catalogo di
  `EventStatusCodeEnum`;
* gli **errori** generati dalle validazioni (`PaperTrackingsErrors`), con la
  descrizione di categoria e causa;
* le **tappe del flusso** di validazione e le richieste/risposte **OCR**;
* gli **output dry-run** che il tracker avrebbe inviato a delivery-push.

Ogni voce espone sotto «Dettaglio JSON» l'item grezzo recuperato da DynamoDB, in
un albero colorato: nasce tutto aperto e ogni ramo si può richiudere.

L'`id` di un evento è il `messageId` della coda SQS: se lo stesso id compare su
più eventi dello stesso destinatario, la coda ha riconsegnato quel messaggio e
le voci vengono marcate con un badge «riconsegna coda».

## Come si usa

1. Fai login sul profilo che ti interessa e popola la cache delle credenziali:

   ```bash
   aws sso login --profile sso_pn-core-dev && aws sts get-caller-identity --profile sso_pn-core-dev
   ```

   Servono entrambi, per ogni profilo che vuoi usare: il login scrive solo il
   token in `sso/cache`, mentre le credenziali del ruolo finiscono in
   `cli/cache` quando lanci un comando con quel profilo. Gli stessi due comandi
   li trovi già uniti sotto ogni errore di credenziali, con un bottone per
   copiarli.

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

5. Inserisci lo IUN, scegli il prodotto e premi **Cerca**. Il `trackingId` viene
   composto da solo: sotto al campo vedi sempre la forma che verrà interrogata.

Il primo caricamento è lento: Pyodide installa `boto3` da PyPI.

## Come vengono trovati i tracking

I `trackingId` non sono indicizzati per IUN, quindi vengono ricostruiti. Le due
forme dipendono dal prodotto:

```
AR, 890, RIR   PREPARE_ANALOG_DOMICILE.IUN_<iun>.RECINDEX_<r>.ATTEMPT_<a>.PCRETRY_<p>
RS, RIS        PREPARE_SIMPLE_REGISTERED_LETTER.IUN_<iun>.RECINDEX_<r>.PCRETRY_<p>
```

Solo i prodotti con più feedback per IUN hanno l'`ATTEMPT`. Con il prodotto
«Tutti» vengono provate entrambe le forme.

Tutto tranne l'ultima parte è l'`attemptId`, partition key dell'indice
`attemptId-pcRetry-index`: una query per ogni coppia `(RECINDEX, ATTEMPT)`
restituisce tutti i `PCRETRY` di quel tentativo. La scansione si ferma al primo
`RECINDEX` vuoto e tollera un `ATTEMPT` mancante; i due limiti sono regolabili
da «Opzioni avanzate».

## File

| File | Cosa contiene |
| --- | --- |
| `index.html` | markup, Tailwind e configurazione PyScript |
| `constants.py` | region, prefisso tabelle, schemi di `trackingId` e cataloghi degli enum |
| `tracker.py` | query DynamoDB, costruzione della timeline e rendering |
| `awsDir.py` | libreria per leggere `~/.aws` e creare client boto3 nel browser (PyScript la monta come `awsdir.py`, quindi si importa con `import awsdir`) |

Region (`eu-south-1`) e prefisso delle tabelle (`pn`) sono costanti in
`constants.py`, non campi della pagina.

I cataloghi in `constants.py` (`STATUS_CODES`, `ERROR_CATEGORIES`,
`ERROR_CAUSES`) replicano gli enum Java: se cambiano `EventStatusCodeEnum`,
`ErrorCategory` o `ErrorCause`, vanno aggiornati anche qui.
