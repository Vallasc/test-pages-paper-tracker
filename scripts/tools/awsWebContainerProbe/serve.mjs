/**
 * Server statico che serve la pagina *cross-origin isolated*.
 *
 * Senza questo non c'è MVP: il WebContainer gira su `SharedArrayBuffer`, che il
 * browser concede solo a una pagina isolata. `python3 -m http.server` non manda
 * questi header, quindi la pagina caricherebbe e poi fallirebbe al boot.
 *
 *     node serve.mjs        →  http://localhost:8099
 */

import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.dirname(fileURLToPath(import.meta.url));
const PORT = Number(process.env.PORT ?? 8099);

const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".css": "text/css; charset=utf-8",
};

http.createServer((request, response) => {
  // una richiesta malformata è roba di tutti i giorni: non deve spegnere il
  // server, e `new URL` solleva su cose come "//" o un escape rotto
  let asked;
  try {
    asked = decodeURIComponent(new URL(request.url, "http://localhost").pathname);
  } catch {
    response.writeHead(400).end("bad request");
    return;
  }
  const file = path.join(ROOT, asked === "/" || asked.endsWith("/")
                               ? path.join(asked, "index.html") : asked);

  // il path non deve poter uscire dalla cartella servita
  if (!file.startsWith(ROOT) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
    response.writeHead(404).end("not found");
    return;
  }

  response.writeHead(200, {
    "content-type": TYPES[path.extname(file)] ?? "application/octet-stream",
    // i due header dell'isolamento, più CORP perché la pagina non sia
    // a sua volta rifiutata se qualcuno la incorpora
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-embedder-policy": "require-corp",
    "cross-origin-resource-policy": "cross-origin",
    "cache-control": "no-store",
  });
  fs.createReadStream(file).pipe(response);
}).listen(PORT, () => {
  console.log(`http://localhost:${PORT}  (COOP same-origin, COEP require-corp)`);
});
