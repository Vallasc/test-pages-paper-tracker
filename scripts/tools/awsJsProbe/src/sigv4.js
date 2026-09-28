/**
 * SigV4 a mano, con la sola WebCrypto del browser.
 *
 * Resta accanto alla strada con l'SDK per un motivo: mostra quanto poco serve
 * davvero. Firmare è un HMAC-SHA256 ripetuto quattro volte, e `crypto.subtle`
 * lo fa da sé — nessun pacchetto, nessun bundler, nessun download.
 */

const ALGORITHM = "AWS4-HMAC-SHA256";
const encoder = new TextEncoder();

function hex(buffer) {
  return [...new Uint8Array(buffer)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

async function sha256(text) {
  return hex(await crypto.subtle.digest("SHA-256", encoder.encode(text)));
}

async function hmac(key, text) {
  const imported = await crypto.subtle.importKey(
    "raw", key, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return new Uint8Array(await crypto.subtle.sign("HMAC", imported, encoder.encode(text)));
}

/**
 * Firma e invia una chiamata a un servizio AWS in JSON.
 *
 * `host` va firmato ma non spedito: è un header che il browser riserva a sé, e
 * lo mette lui con lo stesso valore dell'URL, quindi la firma torna. Lo stesso
 * problema c'è in `aws_dir.py` dell'Analog Delivery Tracker, dove la lista si
 * chiama `_FORBIDDEN`.
 */
export async function call({ credentials, region, service, target, payload }) {
  const host = `${service}.${region}.amazonaws.com`;
  const body = JSON.stringify(payload);
  const stamp = new Date().toISOString().replace(/[:-]|\.\d{3}/g, "");
  const day = stamp.slice(0, 8);

  const headers = {
    "content-type": "application/x-amz-json-1.0",
    host,
    "x-amz-date": stamp,
    "x-amz-target": target,
  };
  if (credentials.sessionToken) headers["x-amz-security-token"] = credentials.sessionToken;

  const names = Object.keys(headers).sort();
  const canonical = [
    "POST", "/", "",
    names.map((name) => `${name}:${headers[name].trim()}\n`).join(""),
    names.join(";"),
    await sha256(body),
  ].join("\n");

  const scope = `${day}/${region}/${service}/aws4_request`;
  const toSign = [ALGORITHM, stamp, scope, await sha256(canonical)].join("\n");

  let key = encoder.encode(`AWS4${credentials.secretAccessKey}`);
  for (const step of [day, region, service, "aws4_request"]) key = await hmac(key, step);
  const signature = hex(await hmac(key, toSign));

  headers.authorization = `${ALGORITHM} Credential=${credentials.accessKeyId}/${scope}, ` +
    `SignedHeaders=${names.join(";")}, Signature=${signature}`;
  delete headers.host;

  const response = await fetch(`https://${host}/`, { method: "POST", headers, body });
  const text = await response.text();
  const parsed = text ? JSON.parse(text) : {};
  if (!response.ok) {
    const kind = (parsed.__type ?? `HTTP ${response.status}`).split("#").pop();
    throw new Error(`${kind}: ${parsed.message ?? parsed.Message ?? text}`);
  }
  return parsed;
}
