/** La pagina: avvia il container, ci monta `.aws`, lancia lo script, mostra l'output. */

import { pick, reread, profileNames, toTree } from "./aws_files.js";
import * as audit from "./netaudit.js";

// Prima di tutto, e soprattutto prima del boot: dopo, il codice del container
// avrebbe già in mano i riferimenti originali e le sue chiamate sfuggirebbero.
// Qui sta bene perché non tocca il DOM; il disegno della tabella si aggancia
// più sotto, quando `el` esiste.
audit.install();

const API = "https://cdn.jsdelivr.net/npm/@webcontainer/api@1.6.4/+esm";

const state = { container: null, files: null, script: null, manifest: null, installed: false };
const el = (id) => document.getElementById(id);

function say(id, text, kind = "") {
  el(id).textContent = text;
  el(id).className = kind;
}

function log(text) {
  el("output").textContent += text;
  el("output").scrollTop = el("output").scrollHeight;
}

function ready() {
  el("btn-run").disabled = !(state.container && state.files);
}

const KIND_LABEL = {
  aws: "AWS — deve essere diretta",
  runtime: "runtime del container",
  cdn: "CDN",
  altro: "⚠ da guardare",
};

function renderAudit(rows) {
  el("audit").innerHTML = rows.length === 0
    ? '<tr><td colspan="4">nessuna richiesta fuori origine, finora</td></tr>'
    : rows.map((row) => `<tr class="kind-${row.kind}">` +
        `<td>${row.host}</td><td>${row.how}</td>` +
        `<td>${row.count}</td><td>${KIND_LABEL[row.kind]}</td></tr>`).join("");
}

audit.onChange(renderAudit);

// Tutto quel che va storto finisce a schermo, non solo in console: qui una
// riga rossa che non si spiega vale un'ora di devtools.
for (const kind of ["error", "unhandledrejection"]) {
  window.addEventListener(kind, (event) => {
    log(`\n[${kind}] ${event.reason?.message ?? event.message ?? event.reason}\n`);
  });
}

// ------------------------------------------------------------------ boot ---

/**
 * L'isolamento cross-origin è il prerequisito del WebContainer, che gira su
 * `SharedArrayBuffer`. Lo si dice subito e accanto al bottone: scoprirlo dopo
 * il click, da un errore su `SharedArrayBuffer`, non aiuta nessuno.
 */
if (crossOriginIsolated) {
  say("boot-status", "Pagina cross-origin isolated: si può partire.", "good");
} else if (await window.coiPending) {
  // il service worker si è appena registrato: la pagina sta per ricaricarsi da
  // sé, e un messaggio d'errore qui sarebbe vero per mezzo secondo
  say("boot-status", "Registro il service worker dell'isolamento e ricarico…");
  el("btn-boot").disabled = true;
} else {
  say("boot-status",
      "Questa pagina non è cross-origin isolated, quindi SharedArrayBuffer è " +
      "negato e il container non può partire. In locale servila con " +
      "`node serve.mjs`; in hosting statico serve HTTPS, perché il service " +
      `worker che rimedia non si registra altrove (ora sei su ${location.origin}).`,
      "bad");
  el("btn-boot").disabled = true;
}

el("btn-boot").onclick = async () => {
  el("btn-boot").disabled = true;
  say("boot-status", "Scarico l'API e accendo il container…");
  const started = performance.now();
  try {
    // import dinamico, non in cima al modulo: se il CDN non risponde voglio un
    // messaggio, non una pagina che carica e non reagisce a niente
    const { WebContainer } = await import(API);
    state.container = await WebContainer.boot();
    // lo script e il suo manifest arrivano dalla stessa origine, come file qualsiasi
    state.script = await (await fetch("./container/get-item.mjs")).text();
    state.manifest = await (await fetch("./container/package.json")).text();
    say("boot-status",
        `Container acceso in ${Math.round(performance.now() - started)} ms — ` +
        `workdir ${state.container.workdir}.`, "good");
    ready();
  } catch (error) {
    say("boot-status", `Boot fallito — ${error.message}`, "bad");
    log(`\n${error.stack ?? error}\n`);
    el("btn-boot").disabled = false;
  }
};

// ------------------------------------------------------------------ .aws ---

el("btn-pick").onclick = async () => {
  try {
    const files = await pick(el("dir-input"));
    if (!Object.keys(files).length) {
      return say("message", "Nessun file utile: è la cartella .aws?", "bad");
    }
    state.files = files;
    const names = profileNames(files);
    el("profile").innerHTML = names.map((name) => `<option>${name}</option>`).join("");
    el("profile").disabled = names.length === 0;
    say("message", `${Object.keys(files).length} file letti, ${names.length} profili.`, "good");
    ready();
  } catch (error) {
    say("message", `${error.name}: ${error.message}`, "bad");
  }
};

// ------------------------------------------------------------------- run ---

el("btn-run").onclick = async () => {
  el("btn-run").disabled = true;
  el("output").textContent = "";
  say("message", "Monto i file e lancio lo script…");

  try {
    // Rilettura prima di montare: la copia dentro il container è un'istantanea,
    // e un `aws sso login` fatto nel frattempo altrimenti non la raggiunge.
    const fresh = await reread();
    if (fresh && Object.keys(fresh).length) {
      state.files = fresh;
      log(`(.aws riletta: ${Object.keys(fresh).length} file)\n`);
    }

    // `.aws` va copiata dentro: il filesystem del container non vede la macchina
    await state.container.mount({
      ".aws": { directory: toTree(state.files) },
      "get-item.mjs": { file: { contents: state.script } },
      "package.json": { file: { contents: state.manifest } },
    });

    const env = {
      // `workdir` è dove mount() ha appena scritto: meglio chiederlo che
      // indovinare il percorso interno del container
      AWS_DIR: `${state.container.workdir}/.aws`,
      // Il token SSO l'SDK lo cerca sotto `os.homedir()`, e non esiste una
      // variabile per spostare quella cartella: `AWS_CONFIG_FILE` copre config
      // e credentials, non `sso/cache`. Su POSIX `os.homedir()` segue `$HOME`,
      // quindi si fa combaciare la casa col workdir e il token si trova.
      HOME: state.container.workdir,
      AWS_PROFILE: el("profile").value,
      AWS_REGION: el("region").value.trim(),
    };

    // L'SDK AWS va installato dentro, una volta sola: è il npm del container,
    // che parla col registro attraverso il browser come tutto il resto.
    if (!state.installed) {
      say("message", "npm install dell'SDK AWS… (la prima volta è lenta)");
      log("$ npm install\n");
      const install = await state.container.spawn("npm", ["install"]);
      install.output.pipeTo(new WritableStream({ write: log }));
      if (await install.exit !== 0) throw new Error("npm install fallito");
      state.installed = true;
    }

    say("message", "Eseguo lo script…");
    log(`\n$ node get-item.mjs …\n`);
    const running = await state.container.spawn(
      "node",
      ["get-item.mjs", el("table").value.trim(),
       el("key-name").value.trim(), el("key-value").value.trim()],
      { env });

    running.output.pipeTo(new WritableStream({ write: log }));
    const code = await running.exit;

    // Nessun verdetto tratto dall'audit: il container gira nell'iframe di
    // un'altra origine, con il suo `window`, quindi le sue chiamate da qui non
    // si vedono. Il verdetto lo dà la console, dove un errore CORS nomina
    // l'host per intero — e un errore CORS c'è solo se la richiesta è partita.
    say("message",
        code === 0
          ? "Script finito. Dove sono andate le chiamate lo dicono la console e "
            + "DevTools → Network, con i frame su «tutti»."
          : `Script uscito con codice ${code}.`,
        code === 0 ? "good" : "bad");
  } catch (error) {
    say("message", `${error.message}`, "bad");
    log(`\n${error.stack ?? error}\n`);
  } finally {
    el("btn-run").disabled = false;
  }
};
