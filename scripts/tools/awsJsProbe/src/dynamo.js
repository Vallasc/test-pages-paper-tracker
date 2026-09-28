/**
 * Un `GetItem` su DynamoDB, per due strade.
 *
 * - **SDK**: `@aws-sdk/client-dynamodb`, come lo useresti in un progetto vero,
 *   caricato come modulo ES da jsDelivr. Nessun bundler: jsDelivr risolve la
 *   condizione *browser*, quindi arriva `@smithy/fetch-http-handler` e non il
 *   gestore HTTP di Node. Costa 33 moduli e circa 500 KB, scaricati alla prima
 *   chiamata e non prima.
 * - **a mano**: la firma SigV4 scritta in `sigv4.js`, zero dipendenze.
 *
 * Le due convivono apposta: la seconda dice che per parlare con DynamoDB dal
 * browser non serve niente, la prima che si può comunque usare quel che il
 * resto del mondo usa. Le credenziali sono le stesse — quelle lette da `.aws`
 * — e l'SDK le prende come oggetto, senza toccare la catena di provider, che
 * nel browser non avrebbe dove cercare.
 */

import { call } from "./sigv4.js";

const SDK = "https://cdn.jsdelivr.net/npm/@aws-sdk/client-dynamodb@3.1141.0/+esm";

let loaded = null;

/** Carica l'SDK una volta sola e dice quanto c'è voluto. */
export async function loadSdk() {
  if (loaded) return { module: loaded, took: 0 };
  const started = performance.now();
  loaded = await import(SDK);
  return { module: loaded, took: Math.round(performance.now() - started) };
}

export async function getItem({ mode, credentials, region, table, key }) {
  return mode === "sdk"
    ? viaSdk({ credentials, region, table, key })
    : viaHand({ credentials, region, table, key });
}

async function viaSdk({ credentials, region, table, key }) {
  const { module } = await loadSdk();
  const client = new module.DynamoDBClient({
    region,
    // L'oggetto letterale scavalca la catena dei provider: nel browser
    // leggerebbe variabili d'ambiente e file che non esistono.
    credentials: {
      accessKeyId: credentials.accessKeyId,
      secretAccessKey: credentials.secretAccessKey,
      sessionToken: credentials.sessionToken ?? undefined,
    },
  });
  try {
    const answer = await client.send(new module.GetItemCommand({
      TableName: table, Key: key,
    }));
    return { Item: answer.Item };
  } catch (error) {
    // l'SDK mette il codice del servizio in `name`, il resto in `message`
    throw new Error(`${error.name}: ${error.message}`);
  }
}

function viaHand({ credentials, region, table, key }) {
  return call({
    credentials, region,
    service: "dynamodb",
    target: "DynamoDB_20120810.GetItem",
    payload: { TableName: table, Key: key },
  });
}

/** Da `{"S": "x"}` a `"x"`, giusto per leggere la risposta senza rumore. */
export function plain(value) {
  if (value === null || typeof value !== "object") return value;
  const [kind, inner] = Object.entries(value)[0] ?? [];
  switch (kind) {
    case "S": case "B": return inner;
    case "N": return Number(inner);
    case "BOOL": return inner;
    case "NULL": return null;
    case "L": return inner.map(plain);
    case "M": return Object.fromEntries(
      Object.entries(inner).map(([name, item]) => [name, plain(item)]));
    case "SS": case "NS": case "BS": return inner;
    default: return value;
  }
}
