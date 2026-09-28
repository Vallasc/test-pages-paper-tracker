/** La pagina: tiene lo stato, attacca i gestori, scrive nel DOM. */

import * as awsDir from "./aws_dir.js";
import { getItem, loadSdk, plain } from "./dynamo.js";

const state = { files: {}, profiles: {}, sources: [] };
const el = (id) => document.getElementById(id);

function say(text, kind = "") {
  el("message").textContent = text;
  el("message").className = kind;
}

function show(value) {
  el("output").textContent = typeof value === "string"
    ? value
    : JSON.stringify(value, null, 2);
}

el("btn-pick").onclick = async () => {
  try {
    const { origin, files } = await awsDir.pick(el("dir-input"));
    if (!Object.keys(files).length) {
      return say("Nessun file utile: è la cartella .aws?", "bad");
    }
    state.files = files;
    state.profiles = awsDir.buildProfiles(files);

    const names = Object.keys(state.profiles);
    el("profile").innerHTML = names.map((name) => `<option>${name}</option>`).join("");
    el("profile").disabled = names.length === 0;
    say(`${origin}: ${Object.keys(files).length} file, ${names.length} profili.`, "good");
    chooseProfile();
  } catch (error) {
    say(`${error.name}: ${error.message}`, "bad");
  }
};

function chooseProfile() {
  const name = el("profile").value;
  state.sources = awsDir.sourcesFor(state.files, state.profiles, name);
  el("source").innerHTML = state.sources
    .map((source, index) => `<option value="${index}">${awsDir.describe(source)}</option>`)
    .join("") || "<option>nessuna credenziale</option>";
  el("source").disabled = state.sources.length === 0;
  el("btn-get").disabled = state.sources.length === 0;

  const region = state.profiles[name]?.region;
  if (region) el("region").value = region;
}

el("profile").onchange = chooseProfile;

el("btn-get").onclick = async () => {
  const source = state.sources[Number(el("source").value)];
  if (!source) return;
  if (!awsDir.valid(source)) {
    say("Credenziali scadute: rifai `aws sso login` e ricarica la cartella.", "bad");
    return;
  }

  const mode = el("mode").value;
  el("btn-get").disabled = true;
  show("");

  try {
    if (mode === "sdk") {
      say("Carico l'SDK da jsDelivr…");
      const { took } = await loadSdk();
      if (took) say(`SDK caricato in ${took} ms. Chiamo DynamoDB…`);
    }
    say(`Chiamo DynamoDB (${mode === "sdk" ? "SDK" : "firma a mano"})…`);

    const started = performance.now();
    const answer = await getItem({
      mode,
      credentials: source,
      region: el("region").value.trim(),
      table: el("table").value.trim(),
      key: { [el("key-name").value.trim()]: { S: el("key-value").value.trim() } },
    });
    const took = Math.round(performance.now() - started);
    if (!answer.Item) {
      say(`Nessun elemento con quella chiave (${took} ms).`, "");
      show(answer);
    } else {
      const fields = Object.keys(answer.Item).length;
      say(`Trovato: ${fields} attributi in ${took} ms.`, "good");
      show(Object.fromEntries(
        Object.entries(answer.Item).map(([name, value]) => [name, plain(value)])));
    }
  } catch (error) {
    say(`${error.message}`, "bad");
    show("");
  } finally {
    el("btn-get").disabled = false;
  }
};
