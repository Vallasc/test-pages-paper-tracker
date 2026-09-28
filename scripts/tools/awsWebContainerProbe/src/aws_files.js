/**
 * La cartella `.aws` dell'utente, letta col selettore e pronta da montare.
 *
 * Qui sta il punto dolente del WebContainer: il suo filesystem è virtuale, non
 * vede la macchina. I file vanno *copiati* dentro, e per averli bisogna
 * comunque passare dalla File System Access API — cioè dalla stessa strada
 * dell'MVP senza container.
 */

const WANTED_FILES = ["config", "credentials"];
const WANTED_DIRS = ["cli/cache", "sso/cache"];

/** Il selettore nativo non esiste in un iframe di origine diversa dal top. */
function nativePickerAvailable() {
  try {
    return "showDirectoryPicker" in window &&
           window.top.location.origin === window.location.origin;
  } catch {
    return false;
  }
}

// Il riferimento vivo alla cartella scelta, quando il selettore nativo c'è:
// tenerlo permette di rileggere senza chiedere di nuovo niente all'utente.
let chosen = null;

export async function pick(input) {
  if (nativePickerAvailable()) {
    chosen = await window.showDirectoryPicker({ id: "awswc", mode: "read" });
    return readTree(chosen);
  }
  chosen = null;                       // da un <input> non resta niente di vivo
  return new Promise((resolve, reject) => {
    input.onchange = async () => {
      if (!input.files.length) return reject(new Error("cartella non letta"));
      resolve(await readInput(input.files));
    };
    input.click();
  });
}

/**
 * Rilegge la cartella scelta, se il riferimento è ancora buono.
 *
 * Quel che si monta nel container è una **copia**, scattata quando hai scelto
 * la cartella: un `aws sso login` fatto dopo non la raggiunge, e ci si ritrova
 * a firmare con credenziali scadute senza capire perché. Rileggere prima di
 * ogni esecuzione toglie di mezzo la trappola. Dal ripiego con `<input>` non si
 * può: lì i file sono istantanee, e il riferimento non sopravvive.
 */
export async function reread() {
  if (!chosen) return null;
  // dopo un refresh il permesso può essere tornato a "prompt"
  if (chosen.queryPermission && await chosen.queryPermission({ mode: "read" }) !== "granted") {
    if (await chosen.requestPermission({ mode: "read" }) !== "granted") return null;
  }
  return readTree(chosen);
}

async function readTree(directory, prefix = "") {
  const files = {};
  for await (const [name, entry] of directory.entries()) {
    const at = prefix ? `${prefix}/${name}` : name;
    if (entry.kind === "directory") {
      if (WANTED_DIRS.some((dir) => dir === at || dir.startsWith(`${at}/`))) {
        Object.assign(files, await readTree(entry, at));
      }
    } else if (isWanted(at)) {
      files[at] = await (await entry.getFile()).text();
    }
  }
  return files;
}

async function readInput(list) {
  const files = {};
  for (const file of list) {
    const at = file.webkitRelativePath.split("/").slice(1).join("/");
    if (isWanted(at)) files[at] = await file.text();
  }
  return files;
}

function isWanted(at) {
  return WANTED_FILES.includes(at) || WANTED_DIRS.some((dir) => at.startsWith(`${dir}/`));
}

/** I nomi dei profili, per il menù: basta leggere le intestazioni di `config`. */
export function profileNames(files) {
  const names = [];
  for (const line of (files.config ?? "").split("\n")) {
    const header = line.trim().match(/^\[(.+)\]$/);
    if (!header) continue;
    const section = header[1].trim();
    if (section === "default") names.push("default");
    else if (section.startsWith("profile ")) names.push(section.slice(8).trim());
  }
  return names;
}

/**
 * Da `{"cli/cache/x.json": "…"}` all'albero che `mount()` si aspetta.
 *
 * La forma è la sua: ogni cartella è `{directory: {…}}`, ogni file
 * `{file: {contents}}`. Non c'è modo di dargli una mappa piatta.
 */
export function toTree(files) {
  const tree = {};
  for (const [at, contents] of Object.entries(files)) {
    const parts = at.split("/");
    let here = tree;
    for (const part of parts.slice(0, -1)) {
      here[part] ??= { directory: {} };
      here = here[part].directory;
    }
    here[parts.at(-1)] = { file: { contents } };
  }
  return tree;
}
