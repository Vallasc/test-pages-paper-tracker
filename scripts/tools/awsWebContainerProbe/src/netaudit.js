/**
 * Registra le richieste di rete che partono da **questa** pagina.
 *
 * ## Quel che vede, e quel che non vede
 *
 * Il WebContainer non gira in questo documento: gira dentro un iframe di
 * un'altra origine, `<id>.w-corp-staticblitz.com`. Quell'iframe ha il suo
 * `window`, quindi le sue `fetch` non passano da qui e in questa tabella **non
 * compaiono** — nemmeno quella verso DynamoDB.
 *
 * Perciò questo audit sorveglia il perimetro della pagina: il runtime scaricato
 * all'avvio, il CDN, e qualunque host inatteso. Non è, e non può essere, la
 * prova che le chiamate del container siano dirette.
 *
 * Quella prova la dà il browser, e in questo caso l'ha data: tentando
 * `fromSSO`, la console ha stampato un errore CORS che nomina per intero
 * `https://portal.sso.eu-west-1.amazonaws.com/federation/credentials`, con
 * l'origine dell'iframe. Un errore CORS è possibile solo se la richiesta è
 * partita dal browser verso quell'host: se qualcuno l'avesse instradata
 * altrove, non ci sarebbe stato niente da bloccare. Per vederle tutte:
 * DevTools → Network, con il filtro dei frame su "tutti".
 */

/** Host che ci aspettiamo, e perché. Tutto il resto va guardato. */
const EXPECTED = [
  [/(^|\.)amazonaws\.com$/, "aws"],
  [/(^|\.)webcontainer-api\.io$/, "runtime"],
  [/(^|\.)staticblitz\.com$/, "runtime"],
  [/(^|\.)stackblitz\.(com|io)$/, "runtime"],
  [/(^|\.)jsdelivr\.net$/, "cdn"],
];

const seen = new Map();
const listeners = [];

function classify(host) {
  for (const [pattern, kind] of EXPECTED) if (pattern.test(host)) return kind;
  return "altro";
}

function note(url, how) {
  let parsed;
  try {
    parsed = new URL(url, location.href);
  } catch {
    return;
  }
  if (parsed.origin === location.origin) return;   // i file della pagina stessa

  const key = `${parsed.host} ${how}`;
  const row = seen.get(key) ?? {
    host: parsed.host, how, kind: classify(parsed.host), count: 0, first: parsed.pathname,
  };
  row.count += 1;
  seen.set(key, row);
  for (const listener of listeners) listener(rows());
}

export function rows() {
  return [...seen.values()].sort((a, b) =>
    a.kind.localeCompare(b.kind) || a.host.localeCompare(b.host));
}

export function onChange(listener) {
  listeners.push(listener);
  listener(rows());
}

/**
 * Avvolge i tre canali di uscita. Va chiamata **prima** di accendere il
 * container: dopo, il codice che ci gira dentro avrebbe già preso i riferimenti
 * originali e le sue chiamate non si vedrebbero.
 */
export function install() {
  const realFetch = window.fetch;
  window.fetch = function (input, init) {
    note(typeof input === "string" ? input : input.url, "fetch");
    return realFetch.call(this, input, init);
  };

  const realOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    note(url, "xhr");
    return realOpen.call(this, method, url, ...rest);
  };

  const RealSocket = window.WebSocket;
  window.WebSocket = function (url, protocols) {
    note(url, "websocket");
    return new RealSocket(url, protocols);
  };
  window.WebSocket.prototype = RealSocket.prototype;
  Object.assign(window.WebSocket, RealSocket);
}
