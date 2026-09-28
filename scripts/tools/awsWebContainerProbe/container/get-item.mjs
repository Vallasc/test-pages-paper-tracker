/**
 * Uno script Node come tanti: SDK AWS, credenziali da SSO, `GetItem`.
 *
 * Non sa di girare in un browser. È il codice che scriveresti comunque, e gira
 * identico da terminale:
 *
 *     npm --prefix container install
 *     AWS_PROFILE=sso_pn-core-dev node container/get-item.mjs \
 *         pn-PaperTrackings trackingId UNA_CHIAVE
 *
 * ## Perché `fromSSO` è anche un test
 *
 * `fromSSO` non si limita a leggere file: prende il token lasciato da
 * `aws sso login` in `sso/cache` e lo scambia per credenziali chiamando
 * `GetRoleCredentials` su `portal.sso.<region>.amazonaws.com`. Quell'endpoint
 * **non manda header CORS** — verificato — quindi da dentro un browser la
 * chiamata è vietata.
 *
 * Da qui il test, che è esattamente la domanda «le chiamate passano da un
 * proxy?»:
 *
 *   - `fromSSO` **fallisce** con un errore di rete → la chiamata è uscita
 *     davvero dal browser, che l'ha bloccata. Nessuno si è messo in mezzo.
 *   - `fromSSO` **riesce** → la richiesta ha raggiunto AWS pur non avendo la
 *     CORS: è passata da un'altra parte, e con dentro il bearer token SSO.
 *     Quello è il caso da non accettare.
 *
 * Il ripiego sulle credenziali già scritte su disco serve a completare la prova
 * anche nel primo caso, che è quello che vogliamo.
 */

import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { DynamoDBClient, GetItemCommand } from "@aws-sdk/client-dynamodb";
import { fromSSO } from "@aws-sdk/credential-providers";

const AWS_DIR = process.env.AWS_DIR || path.join(os.homedir(), ".aws");
const PROFILE = process.env.AWS_PROFILE || "default";
const REGION = process.env.AWS_REGION || "eu-south-1";

const [table, keyName, keyValue] = process.argv.slice(2);
if (!table || !keyName || !keyValue) {
  console.error("uso: get-item.mjs <tabella> <nome chiave> <valore chiave>");
  process.exit(2);
}

// L'SDK cerca la configurazione in `~/.aws`, che qui è montata altrove
process.env.AWS_CONFIG_FILE = path.join(AWS_DIR, "config");
process.env.AWS_SHARED_CREDENTIALS_FILE = path.join(AWS_DIR, "credentials");

console.log(`node ${process.version} — profilo ${PROFILE}, region ${REGION}`);
console.log(`.aws in ${AWS_DIR}: ${fs.existsSync(AWS_DIR) ? fs.readdirSync(AWS_DIR).join(", ") : "ASSENTE"}\n`);

// ------------------------------------------------------------ credenziali ---

async function viaSso() {
  console.log("1. fromSSO() — scambia il token SSO su portal.sso.…amazonaws.com");
  const credentials = await fromSSO({ profile: PROFILE })();
  console.log("   RIUSCITO. Attenzione: quell'endpoint non concede la CORS,");
  console.log("   quindi questa richiesta NON è uscita direttamente dal browser.");
  console.log("   Controlla l'audit di rete nella pagina: se non vedi");
  console.log("   portal.sso.….amazonaws.com, il token è passato da un proxy.\n");
  return credentials;
}

/**
 * Le credenziali che la CLI ha già scritto su disco, senza chiamare nessuno.
 *
 * `aws sso login` le lascia in `cli/cache`, e `aws sts assume-role` fa lo
 * stesso: sono già il risultato dello scambio, quindi qui non c'è rete.
 */
function viaCache() {
  const candidates = [];
  for (const dir of ["cli/cache", "sso/cache"]) {
    const full = path.join(AWS_DIR, dir);
    if (!fs.existsSync(full)) continue;
    for (const name of fs.readdirSync(full)) {
      let blob;
      try {
        blob = JSON.parse(fs.readFileSync(path.join(full, name), "utf8"));
      } catch { continue; }
      const creds = blob.Credentials ?? blob.credentials ?? blob.roleCredentials;
      const accessKeyId = creds?.AccessKeyId ?? creds?.accessKeyId;
      const secretAccessKey = creds?.SecretAccessKey ?? creds?.secretAccessKey;
      if (!accessKeyId || !secretAccessKey) continue;
      const expiration = new Date(creds.Expiration ?? creds.expiration ?? 0);
      if (expiration <= new Date()) continue;
      candidates.push({
        accessKeyId, secretAccessKey,
        sessionToken: creds.SessionToken ?? creds.sessionToken,
        expiration,
        origin: `${dir}/${name}`,
      });
    }
  }
  candidates.sort((a, b) => b.expiration - a.expiration);
  return candidates[0];
}

/**
 * Se il guasto è di rete o di credenziali, e la differenza conta.
 *
 * Solo un guasto di rete dice qualcosa sul proxy: significa che la richiesta è
 * uscita ed è stata fermata. Un token scaduto invece non dice niente — la
 * chiamata non è nemmeno partita — e spacciarlo per una prova sarebbe un
 * inganno, per giunta credibile.
 */
function isNetworkFailure(error) {
  const text = [error.name, error.message, error.cause?.message, error.cause?.code]
    .join(" ").toLowerCase();
  return /fetch failed|failed to fetch|networking|network error|cors|enotfound|econnrefused|econnreset|timeout|load failed/
    .test(text);
}

let credentials;
try {
  credentials = await viaSso();
} catch (error) {
  console.log(`   FALLITO: ${error.message.slice(0, 200)}`);
  if (isNetworkFailure(error)) {
    console.log("   → guasto di RETE: la richiesta è uscita e qualcuno l'ha");
    console.log("     fermata. In un browser è la CORS, ed è la risposta che");
    console.log("     vogliamo: nessun intermediario l'ha fatta passare.\n");
  } else {
    console.log("   → NON è un guasto di rete: la chiamata non è nemmeno");
    console.log("     partita, quindi non dice niente sul proxy. Di solito è");
    console.log("     la sessione SSO scaduta: rifai `aws sso login`.\n");
  }

  console.log("2. ripiego sulle credenziali già in cache (nessuna rete)");
  credentials = viaCache();
  if (!credentials) {
    console.error("   nessuna credenziale valida e non scaduta: rifai `aws sso login`,");
    console.error("   poi ricarica la cartella .aws nella pagina.");
    process.exit(1);
  }
  console.log(`   ${credentials.accessKeyId.slice(0, 4)}…${credentials.accessKeyId.slice(-4)} da ${credentials.origin}\n`);
}

// -------------------------------------------------------------- la chiamata ---

console.log(`3. GetItem con l'SDK su ${table} dove ${keyName} = ${keyValue}`);
const client = new DynamoDBClient({ region: REGION, credentials });

try {
  const answer = await client.send(new GetItemCommand({
    TableName: table,
    Key: { [keyName]: { S: keyValue } },
  }));
  console.log(answer.Item
    ? JSON.stringify(answer.Item, null, 2)
    : "   nessun elemento con quella chiave");
  console.log("\n→ AWS ha risposto: l'SDK ha firmato e la chiamata è arrivata.");
  console.log("  Ora guarda l'audit: dynamodb.%s.amazonaws.com deve comparire.", REGION);
} catch (error) {
  console.error(`   errore: ${error.name} — ${error.message}`);
  process.exit(1);
}
