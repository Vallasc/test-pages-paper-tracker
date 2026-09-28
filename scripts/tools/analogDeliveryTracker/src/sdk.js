/**
 * L'SDK AWS, preso dal CDN come modulo ES.
 *
 * Arriva al primo collegamento e non prima: chi apre la pagina e non si connette
 * non scarica niente. Un `import()` ripetuto sullo stesso URL non riscarica, e
 * la promessa qui accanto fa sì che due chiamate in parallelo non chiedano due
 * volte.
 *
 * jsDelivr risolve la condizione *browser* del pacchetto, quindi arriva
 * `@smithy/fetch-http-handler` e non il gestore HTTP di Node: nessun `node:`
 * nel grafo, nessun bundler.
 */

const DYNAMO = "https://cdn.jsdelivr.net/npm/@aws-sdk/client-dynamodb@3.1141.0/+esm";
const LOGS = "https://cdn.jsdelivr.net/npm/@aws-sdk/client-cloudwatch-logs@3.1141.0/+esm";

let loading = null;

/**
 * I due pacchetti, ognuno nel suo namespace.
 *
 * Fonderli in un oggetto solo sarebbe più comodo e sbagliato: hanno 19 nomi in
 * comune — `ResourceNotFoundException`, `ThrottlingException`,
 * `LimitExceededException` fra gli altri — e sono classi *diverse* nei due
 * servizi. L'ultimo caricato vincerebbe, e un `instanceof` guarderebbe la
 * classe del servizio sbagliato senza dirlo a nessuno.
 */
export function sdk() {
  loading ??= Promise.all([import(DYNAMO), import(LOGS)])
    .then(([dynamodb, cloudwatch]) => ({ dynamodb, cloudwatch }));
  return loading;
}

/**
 * I due client che il tracker usa.
 *
 * Le credenziali si passano come oggetto, scavalcando la catena dei provider:
 * nel browser andrebbe a cercare variabili d'ambiente e file che non esistono.
 */
export async function clients(credentials, region) {
  const { dynamodb, cloudwatch } = await sdk();
  return {
    ddb: new dynamodb.DynamoDBClient({ region, credentials }),
    logs: new cloudwatch.CloudWatchLogsClient({ region, credentials }),
  };
}
