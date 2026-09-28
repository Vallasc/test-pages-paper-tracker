/**
 * Credenziali AWS dalla cartella `.aws`, nel browser e senza dipendenze.
 *
 * È il gemello in JavaScript di `aws_dir.py` dell'Analog Delivery Tracker,
 * ridotto a quel che serve per provare una chiamata: legge la cartella scelta
 * dall'utente, ricava i profili e ordina le credenziali utilizzabili.
 *
 * Non c'è nessuno scambio di token SSO, e non serve: `aws sso login` lascia in
 * `sso/cache` credenziali già pronte, e `aws sts assume-role` fa lo stesso in
 * `cli/cache`. Qui si leggono e basta.
 */

const WANTED_FILES = ["config", "credentials"];
const WANTED_DIRS = ["cli/cache", "sso/cache"];

/** Il selettore nativo non esiste in un iframe di origine diversa dal top. */
export function nativePickerAvailable() {
  try {
    return "showDirectoryPicker" in window &&
           window.top.location.origin === window.location.origin;
  } catch {
    return false;                      // leggere top.location fallisce negli stessi casi
  }
}

/** Apre il selettore, o ripiega sull'input, e restituisce i file letti. */
export async function pick(input) {
  if (nativePickerAvailable()) {
    const handle = await window.showDirectoryPicker({ id: "awsjs", mode: "read" });
    return { origin: handle.name, files: await readTree(handle) };
  }
  return new Promise((resolve, reject) => {
    input.onchange = async () => {
      if (!input.files.length) return reject(new Error("cartella non letta"));
      resolve({ origin: "input", files: await readInput(input.files) });
    };
    input.click();
  });
}

async function readTree(handle, prefix = "") {
  const files = {};
  for await (const [name, entry] of handle.entries()) {
    const path = prefix ? `${prefix}/${name}` : name;
    if (entry.kind === "directory") {
      // si scende solo lungo i rami che portano a una cartella voluta
      if (WANTED_DIRS.some((dir) => dir === path || dir.startsWith(`${path}/`))) {
        Object.assign(files, await readTree(entry, path));
      }
    } else if (isWanted(path)) {
      files[path] = await (await entry.getFile()).text();
    }
  }
  return files;
}

async function readInput(list) {
  const files = {};
  for (const file of list) {
    // webkitRelativePath parte dal nome della cartella scelta: si scarta
    const path = file.webkitRelativePath.split("/").slice(1).join("/");
    if (isWanted(path)) files[path] = await file.text();
  }
  return files;
}

function isWanted(path) {
  return WANTED_FILES.includes(path) ||
         WANTED_DIRS.some((dir) => path.startsWith(`${dir}/`));
}

/** Le sezioni di un file ini, senza i commenti. */
function parseIni(text) {
  const sections = {};
  let current = null;
  for (const raw of text.split("\n")) {
    let line = raw.trim();
    if (!line || line.startsWith("#") || line.startsWith(";")) continue;
    // un commento in coda va staccato da uno spazio: così un URL con "#" resta intero
    line = line.replace(/\s+[#;].*$/, "").trim();
    const header = line.match(/^\[(.+)\]$/);
    if (header) {
      current = sections[header[1].trim()] ??= {};
      continue;
    }
    const split = line.indexOf("=");
    if (current && split > 0) {
      current[line.slice(0, split).trim()] = line.slice(split + 1).trim();
    }
  }
  return sections;
}

/** I profili: `config` dà la struttura, `credentials` ha l'ultima parola. */
export function buildProfiles(files) {
  const profiles = {};
  for (const [section, values] of Object.entries(parseIni(files.config ?? ""))) {
    const name = section === "default" ? "default"
               : section.startsWith("profile ") ? section.slice(8).trim()
               : null;
    if (name) profiles[name] = { ...values };
  }
  for (const [section, values] of Object.entries(parseIni(files.credentials ?? ""))) {
    profiles[section] = { ...(profiles[section] ?? {}), ...values };
  }
  return profiles;
}

export function accountFromArn(arn) {
  const parts = (arn ?? "").split(":");
  return parts.length > 4 && parts[4] ? parts[4] : null;
}

const BASE32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";

/**
 * L'account id ricavato dall'access key, che lo porta codificato.
 *
 * Il formato non è documentato da AWS: è un **indizio** per etichettare le
 * sorgenti prima di connettersi, quando la cache SSO non porta un ARN. Dieci
 * simboli base32 sono 50 bit, e i primi 48 sono i sei byte che contano.
 */
export function accountFromAccessKey(id) {
  if (!id || id.length < 14) return null;
  let bits = 0n;
  for (const character of id.slice(4, 14).toUpperCase()) {
    const value = BASE32.indexOf(character);
    if (value < 0) return null;
    bits = (bits << 5n) | BigInt(value);
  }
  return String(((bits >> 2n) & 0x7fffffffff80n) >> 7n).padStart(12, "0");
}

/**
 * Le credenziali utilizzabili per il profilo, dalla più alla meno affidabile.
 *
 * Le scadute restano in coda invece di sparire: servono a dire che tocca
 * rifare `aws sso login`, che è un'informazione, non un guasto.
 */
export function sourcesFor(files, profiles, name) {
  const settings = profiles[name] ?? {};
  const found = [];

  if (settings.aws_access_key_id && settings.aws_secret_access_key) {
    found.push({
      accessKeyId: settings.aws_access_key_id,
      secretAccessKey: settings.aws_secret_access_key,
      sessionToken: settings.aws_session_token ?? null,
      account: accountFromAccessKey(settings.aws_access_key_id),
      accountIsGuess: true,
      expiresAt: null,
      origin: `profilo ${name}`,
      score: 100,
    });
  }

  for (const [path, text] of Object.entries(files)) {
    if (!path.startsWith("cli/cache/") && !path.startsWith("sso/cache/")) continue;
    let blob;
    try {
      blob = JSON.parse(text);
    } catch {
      continue;                        // la cache SSO tiene anche i token, non solo le chiavi
    }
    const creds = blob.Credentials ?? blob.credentials ?? blob.roleCredentials;
    if (!creds) continue;
    const accessKeyId = creds.AccessKeyId ?? creds.accessKeyId;
    const secretAccessKey = creds.SecretAccessKey ?? creds.secretAccessKey;
    if (!accessKeyId || !secretAccessKey) continue;

    const arn = blob.AssumedRoleUser?.Arn ?? "";
    let account = accountFromArn(arn);
    const accountIsGuess = !account;
    if (accountIsGuess) account = accountFromAccessKey(accessKeyId);
    const role = arn.includes("/") ? arn.split("/")[1] : null;

    let score = 20;
    if (settings.sso_account_id && account === settings.sso_account_id) score += 40;
    if (settings.sso_role_name && role === settings.sso_role_name) score += 25;

    const expiration = creds.Expiration ?? creds.expiration;
    found.push({
      accessKeyId, secretAccessKey,
      sessionToken: creds.SessionToken ?? creds.sessionToken ?? null,
      account, accountIsGuess, role,
      expiresAt: toDate(expiration),
      origin: path,
      score: score + (expiration ? 0 : 10),
    });
  }

  return found.sort((a, b) =>
    Number(valid(b)) - Number(valid(a)) ||
    b.score - a.score ||
    (b.expiresAt ?? 0) - (a.expiresAt ?? 0));
}

/** Una scadenza può arrivare come ISO o come secondi/millisecondi epoch. */
function toDate(value) {
  if (!value) return null;
  if (typeof value === "number") return new Date(value > 1e11 ? value : value * 1000);
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

export function valid(source) {
  return !source.expiresAt || source.expiresAt > new Date();
}

export function describe(source) {
  const key = `${source.accessKeyId.slice(0, 4)}…${source.accessKeyId.slice(-4)}`;
  const account = source.account
    ? `account ${source.account}${source.accountIsGuess ? " (dedotto)" : ""}`
    : "account ignoto";
  const when = !source.expiresAt ? "senza scadenza"
    : valid(source) ? `scade ${source.expiresAt.toISOString().slice(0, 19)}Z`
    : `SCADUTA ${source.expiresAt.toISOString().slice(0, 19)}Z`;
  return `${key} · ${account} · ${when} · ${source.origin}`;
}
