/**
 * Credenziali AWS dalla cartella `.aws`, lette nel browser.
 *
 * Questo modulo legge e sceglie, non chiama: restituisce credenziali, e a
 * parlare con AWS ci pensa l'SDK. Chi vuole un client passa da `sdk.js`.
 *
 *     const aws = await AwsDirectory.pick();
 *     const source = aws.bestSource("sso_pn-core-dev", { requireAccount: true });
 *     const { ddb, logs } = await clients(source.credentials, aws.regionFor(profile));
 *
 * Nessuno scambio di token SSO, e non serve: `aws sso login` lascia credenziali
 * già pronte in `cli/cache`. Rifare il flusso OIDC dal browser non si potrebbe
 * comunque, perché `portal.sso.*.amazonaws.com` non concede la CORS.
 */

const WANTED_FILES = ["config", "credentials"];
const WANTED_DIRS = ["cli/cache", "sso/cache"];

/** un file di `.aws` più grande di così non è un file di `.aws` */
const MAX_FILE_BYTES = 2_000_000;

/**
 * Il riferimento alla cartella scelta: è vivo, quindi rileggerlo vede i file
 * aggiornati — è così che un `aws sso login` fatto nel frattempo si fa vedere
 * senza ricaricare la pagina. Sta anche in IndexedDB, per sopravvivere ai
 * refresh.
 */
let chosen = null;

/**
 * Questi nomi, e l'`id` passato al selettore, non seguono quello del modulo:
 * sono chiavi di memoria, e cambiarle farebbe dimenticare a tutti la cartella
 * già scelta e a Chrome da dove riaprire il selettore.
 */
const IDB_NAME = "awsdir";
const IDB_STORE = "handles";
const IDB_KEY = "aws-dir";

/** IndexedDB parla con eventi, non con promesse. */
function awaitRequest(request) {
  return new Promise((resolve, reject) => {
    request.addEventListener("success", () => resolve(request.result));
    request.addEventListener("error", () => reject(request.error));
  });
}

/**
 * Il database degli handle, creando l'object store alla prima apertura.
 *
 * Restituisce il database e non lo store: fra `transaction()` e la richiesta
 * non ci deve stare un `await`, o la transazione si chiude prima.
 */
function idbDatabase() {
  const request = indexedDB.open(IDB_NAME, 1);
  request.addEventListener("upgradeneeded", () => {
    const database = request.result;
    if (!database.objectStoreNames.contains(IDB_STORE)) database.createObjectStore(IDB_STORE);
  });
  return awaitRequest(request);
}

/**
 * L'handle lasciato dalla visita precedente, o `null`.
 *
 * In incognito, su Chrome 153, rileggerlo dopo un reload uccide il renderer: è
 * una regressione di Chromium, non un errore intercettabile. Fuori
 * dall'incognito non si presenta, e si è scelto di non pagarne il prezzo.
 */
async function rememberedHandle() {
  try {
    const database = await idbDatabase();
    const store = database.transaction(IDB_STORE, "readonly").objectStore(IDB_STORE);
    return await awaitRequest(store.get(IDB_KEY)) ?? null;
  } catch {
    return null;
  }
}

/** Ricorda l'handle per il prossimo refresh; con `null` lo dimentica. */
async function rememberHandle(handle) {
  try {
    const database = await idbDatabase();
    const store = database.transaction(IDB_STORE, "readwrite").objectStore(IDB_STORE);
    await awaitRequest(handle === null ? store.delete(IDB_KEY) : store.put(handle, IDB_KEY));
  } catch {
    // è una comodità: se salta, pazienza
  }
}

/**
 * Vero se l'handle è ancora leggibile.
 *
 * Dopo un refresh il permesso può essere tornato «prompt»: con `ask` lo si
 * richiede, e all'utente tocca un clic invece di ricercare la cartella.
 */
async function readable(handle, { ask = false } = {}) {
  try {
    let state = await handle.queryPermission({ mode: "read" });
    if (state !== "granted" && ask) state = await handle.requestPermission({ mode: "read" });
    return state === "granted";
  } catch {
    return false;
  }
}

// =============================================================================
// Errori
// =============================================================================
export class AwsDirError extends Error {
  constructor(message) {
    super(message);
    this.name = "AwsDirError";
  }
}

export class NoCredentialsFound extends AwsDirError {}
export class CredentialsExpired extends AwsDirError {}
export class AccountMismatch extends AwsDirError {}

// =============================================================================
// Lettura della cartella
// =============================================================================

/**
 * Il selettore nativo non esiste nei sotto-frame di origine diversa dal top.
 *
 * Chromium lo vieta e non esiste un `allow=` per autorizzarlo; leggere
 * `window.top.location.origin` fallisce esattamente negli stessi casi.
 */
function nativePickerAvailable() {
  try {
    if (!("showDirectoryPicker" in window)) return false;
    return window.top.location.origin === window.location.origin;
  } catch {
    return false;
  }
}

async function readTree(directory, prefix = "") {
  const files = {};
  for await (const [name, entry] of directory.entries()) {
    const at = prefix ? `${prefix}/${name}` : name;
    if (entry.kind === "directory") {
      // si scende solo lungo i rami che portano a una cartella voluta
      if (WANTED_DIRS.some((dir) => dir === at || dir.startsWith(`${at}/`))) {
        Object.assign(files, await readTree(entry, at));
      }
    } else if (isWanted(at)) {
      const file = await entry.getFile();
      if (file.size <= MAX_FILE_BYTES) files[at] = await file.text();
    }
  }
  return files;
}

async function readInput(list) {
  const files = {};
  for (const file of list) {
    // webkitRelativePath parte dal nome della cartella scelta: si scarta
    const at = file.webkitRelativePath.split("/").slice(1).join("/");
    if (isWanted(at) && file.size <= MAX_FILE_BYTES) files[at] = await file.text();
  }
  return files;
}

function isWanted(at) {
  return WANTED_FILES.includes(at) || WANTED_DIRS.some((dir) => at.startsWith(`${dir}/`));
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
    const header = /^\[(.+)\]$/.exec(line);
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

function accountFromArn(arn) {
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
function accountFromAccessKey(id) {
  if (!id || id.length < 14) return null;
  let bits = 0n;
  for (const character of id.slice(4, 14).toUpperCase()) {
    const value = BASE32.indexOf(character);
    if (value < 0) return null;
    bits = (bits << 5n) | BigInt(value);
  }
  return String(((bits >> 2n) & 0x7fffffffff80n) >> 7n).padStart(12, "0");
}

function mask(value) {
  return value && value.length > 8 ? `${value.slice(0, 4)}…${value.slice(-4)}` : "?";
}

// =============================================================================
// Una credenziale utilizzabile
// =============================================================================
class CredentialSource {
  constructor(fields) {
    Object.assign(this, {
      accessKeyId: null, secretAccessKey: null, sessionToken: null,
      expiresAt: null, account: null, accountIsGuess: false, role: null,
      origin: "", score: 0,
    }, fields);
  }

  /** Falso se scaduta. Senza scadenza dichiarata è considerata valida. */
  get valid() {
    return this.expiresAt === null || this.expiresAt > new Date();
  }

  /** Millisecondi che mancano alla scadenza, o `null`. */
  get expiresIn() {
    return this.expiresAt === null ? null : this.expiresAt - new Date();
  }

  describe() {
    const bits = [this.origin || "?", mask(this.accessKeyId)];
    if (this.account) {
      bits.push(`account ${this.account}${this.accountIsGuess ? " (dedotto)" : ""}`);
    }
    if (this.role) bits.push(this.role);
    const remaining = this.expiresIn;
    if (remaining !== null) {
      bits.push(`${this.valid ? "scade tra" : "scaduta da"} ${humanMinutes(Math.abs(remaining))}`);
    }
    return bits.join(" · ");
  }

  /** La forma che l'SDK si aspetta. `undefined` e non `null`: l'SDK lo omette. */
  get credentials() {
    return {
      accessKeyId: this.accessKeyId,
      secretAccessKey: this.secretAccessKey,
      sessionToken: this.sessionToken ?? undefined,
    };
  }
}

export function humanMinutes(milliseconds) {
  const minutes = Math.floor(milliseconds / 60000);
  if (minutes < 60) return `${minutes} min`;
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, "0")}m`;
}

/** Una scadenza può arrivare come ISO o come secondi/millisecondi epoch. */
function toDate(value) {
  if (!value) return null;
  if (typeof value === "number") return new Date(value > 1e11 ? value : value * 1000);
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

// =============================================================================
// La cartella .aws
// =============================================================================
/**
 * Apre il selettore nativo. Va chiamato da un gesto dell'utente.
 *
 * Niente `startIn`: ammette solo desktop/documents/downloads/music/pictures/
 * videos, e un valore fuori lista fa fallire la chiamata in validazione. Ci
 * pensa `id`, che riapre sull'ultima cartella scelta.
 */
async function showPicker() {
  try {
    return await window.showDirectoryPicker({ id: IDB_NAME, mode: "read" });
  } catch (error) {
    if (error.name === "AbortError") throw new AwsDirError("Nessuna cartella scelta.");
    throw new AwsDirError(`Selettore non disponibile: ${error.message}`);
  }
}

export class AwsDirectory {
  constructor(files, origin = "") {
    this.files = files;
    this.origin = origin;
    this.profiles = {};
    this.#buildProfiles();
  }

  #buildProfiles() {
    for (const [section, values] of Object.entries(parseIni(this.files.config ?? ""))) {
      // le sezioni `sso-session` si saltano: servono a rifare lo scambio del
      // token, che qui non si fa
      if (section === "default") this.profiles.default = { ...values };
      else if (section.startsWith("profile ")) this.profiles[section.slice(8).trim()] = { ...values };
    }
    // il file credentials ha la precedenza sulle chiavi statiche di config
    for (const [section, values] of Object.entries(parseIni(this.files.credentials ?? ""))) {
      this.profiles[section] = { ...(this.profiles[section] ?? {}), ...values };
    }
  }

  /** Da una mappa `percorso relativo -> contenuto`. Utile nei test. */
  static fromFiles(files, origin = "mapping") {
    return new AwsDirectory(files, origin);
  }

  /**
   * Fa scegliere la cartella all'utente e la legge.
   *
   * Va chiamata da un gestore di evento: sia il selettore sia la richiesta di
   * permesso su un handle ricordato vogliono un gesto dell'utente. **Dalla
   * seconda volta non riappare nulla**, ma i file sono riletti dal disco: è
   * così che un `aws sso login` fatto nel frattempo si fa vedere.
   *
   * Dove il selettore è vietato (sotto-frame di altra origine) o non esiste
   * (fuori da Chromium) ripiega su `fromInput`, e lì la cartella va riscelta
   * ogni volta, perché da un `<input>` non resta niente di vivo.
   */
  static async pick() {
    if (!nativePickerAvailable()) return AwsDirectory.fromInput();

    let handle = chosen ?? await rememberedHandle();
    if (handle !== null && !await readable(handle, { ask: true })) {
      // `requestPermission` consuma il gesto dell'utente, quindi qui non si può
      // più aprire il selettore: meglio dirlo che fallire dopo.
      chosen = null;
      await rememberHandle(null);
      throw new AwsDirError(
        "Permesso negato sulla cartella ricordata: premi di nuovo per sceglierne una.");
    }
    if (handle === null) {
      handle = await showPicker();
      await rememberHandle(handle);
    }

    const files = await readTree(handle);
    if (!Object.keys(files).length) {
      chosen = null;                        // sbagliata: non ricordarla
      await rememberHandle(null);
      throw new AwsDirError(
        "Nella cartella scelta non ci sono config, credentials né file di cache: " +
        "probabilmente non è ~/.aws.");
    }
    chosen = handle;
    return new AwsDirectory(files, `cartella scelta dall'utente (${handle.name})`);
  }

  /**
   * Ripiego con `<input type=file webkitdirectory>`.
   *
   * Serve nei sotto-frame di origine diversa, dove il selettore nativo è
   * vietato: l'input invece è un normale controllo di form e passa.
   */
  static fromInput() {
    chosen = null;                          // da un <input> non resta niente di vivo
    return new Promise((resolve, reject) => {
      const node = document.createElement("input");
      node.type = "file";
      node.webkitdirectory = true;
      node.style.display = "none";
      document.body.appendChild(node);

      const settle = async () => {
        node.remove();
        if (!node.files?.length) {
          reject(new AwsDirError("Nessuna cartella scelta."));
          return;
        }
        const files = await readInput(node.files);
        if (!Object.keys(files).length) {
          reject(new AwsDirError(
            "Nella cartella scelta non ci sono né 'config' né 'credentials': " +
            "è davvero la cartella .aws del tuo utente?"));
          return;
        }
        resolve(new AwsDirectory(files, "cartella scelta dall'utente (file input)"));
      };

      node.addEventListener("change", settle);
      node.addEventListener("cancel", () => {       // l'utente ha annullato
        node.remove();
        reject(new AwsDirError("Nessuna cartella scelta."));
      });
      node.click();
    });
  }

  /** Profili trovati, `default` per primo. */
  profileNames() {
    return Object.keys(this.profiles).sort((a, b) =>
      (a !== "default") - (b !== "default") || a.localeCompare(b));
  }

  #resolveProfile(profile) {
    if (profile) {
      if (!(profile in this.profiles)) {
        throw new AwsDirError(
          `Profilo '${profile}' non trovato. Disponibili: ${this.profileNames().join(", ") || "nessuno"}`);
      }
      return profile;
    }
    if (!("default" in this.profiles)) {
      throw new AwsDirError(
        `Nessun profilo 'default'. Disponibili: ${this.profileNames().join(", ") || "nessuno"}`);
    }
    return "default";
  }

  /** Region del profilo, o `null`. */
  regionFor(profile = null) {
    return this.profiles[this.#resolveProfile(profile)]?.region ?? null;
  }

  /** L'account a cui punta il profilo, se dichiarato (`sso_account_id`). */
  expectedAccount(profile = null) {
    return this.profiles[this.#resolveProfile(profile)]?.sso_account_id ?? null;
  }

  /**
   * Sorgenti utilizzabili per il profilo, dalla più alla meno affidabile.
   *
   * Le scadute restano in coda: servono a spiegare all'utente che deve
   * rinnovare la sessione, invece di far sparire tutto.
   */
  sources(profile = null) {
    const name = this.#resolveProfile(profile);
    const settings = this.profiles[name] ?? {};
    const wantedAccount = settings.sso_account_id;
    const found = [];

    if (settings.aws_access_key_id && settings.aws_secret_access_key) {
      found.push(new CredentialSource({
        accessKeyId: settings.aws_access_key_id,
        secretAccessKey: settings.aws_secret_access_key,
        sessionToken: settings.aws_session_token ?? null,
        account: accountFromAccessKey(settings.aws_access_key_id),
        accountIsGuess: true,
        origin: `profilo ${name}`,
        score: 100,
      }));
    }

    for (const [path, text] of Object.entries(this.files)) {
      if (!path.startsWith("cli/cache/") && !path.startsWith("sso/cache/")) continue;
      let blob;
      try {
        blob = JSON.parse(text);
      } catch {
        continue;                  // la cache SSO tiene anche i token, non solo le chiavi
      }
      const creds = blob.Credentials ?? blob.credentials ?? blob.roleCredentials;
      if (!creds || typeof creds !== "object") continue;
      const accessKeyId = creds.AccessKeyId ?? creds.accessKeyId;
      const secretAccessKey = creds.SecretAccessKey ?? creds.secretAccessKey;
      if (!accessKeyId || !secretAccessKey) continue;

      const arn = blob.AssumedRoleUser?.Arn ?? "";
      let account = accountFromArn(arn);
      let guessed = false;
      if (!account) {              // la cache SSO non porta un ARN
        account = accountFromAccessKey(accessKeyId);
        guessed = account !== null;
      }
      const role = arn.includes("/") ? arn.split("/")[1] : null;

      let score = 20;
      if (wantedAccount && account === wantedAccount) score += 40;
      if (settings.sso_role_name && role === settings.sso_role_name) score += 25;
      if (settings.role_arn && role === settings.role_arn.split("/").at(-1)) score += 40;

      const expiration = creds.Expiration ?? creds.expiration;
      const expiresAt = toDate(expiration);
      found.push(new CredentialSource({
        accessKeyId, secretAccessKey,
        sessionToken: creds.SessionToken ?? creds.sessionToken ?? null,
        expiresAt, account, accountIsGuess: guessed, role,
        origin: path,
        score: score + (expiresAt === null ? 10 : 0),
      }));
    }

    return found.sort((a, b) =>
      Number(b.valid) - Number(a.valid) ||
      b.score - a.score ||
      (b.expiresAt ?? 0) - (a.expiresAt ?? 0));
  }

  /**
   * La sorgente migliore, o un errore che spiega cosa fare.
   *
   * Con `requireAccount` rifiuta credenziali di un account diverso da
   * `sso_account_id`: senza quel controllo una tabella omonima in un altro
   * ambiente risponde 0 risultati invece di dare errore.
   */
  bestSource(profile = null, { allowExpired = false, requireAccount = false } = {}) {
    const name = this.#resolveProfile(profile);
    const found = this.sources(name);
    const fix = `  aws sso login --profile ${name} && ` +
                `aws sts get-caller-identity --profile ${name} --no-cli-pager`;
    if (!found.length) {
      throw new NoCredentialsFound(
        `Nessuna credenziale per il profilo '${name}'.\n${fix}`);
    }

    const usable = allowExpired ? found : found.filter((source) => source.valid);
    if (!usable.length) {
      throw new CredentialsExpired(
        `Le credenziali per '${name}' sono scadute (${found[0].describe()}).\n${fix}\n` +
        "Poi rileggi la cartella: il login da solo non tocca cli/cache.");
    }

    const wanted = this.expectedAccount(name);
    if (requireAccount && wanted) {
      const matching = usable.filter((source) => source.account === wanted);
      if (!matching.length) {
        const seen = [...new Set(usable.map((source) => source.account ?? "?"))].sort();
        throw new AccountMismatch(
          `Il profilo '${name}' punta all'account ${wanted}, ma le credenziali ` +
          `disponibili sono di: ${seen.join(", ")}.\n${fix}`);
      }
      return matching[0];
    }
    return usable[0];
  }
}
