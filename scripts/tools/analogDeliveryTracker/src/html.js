/** L'unica utilità condivisa fra chi compone HTML: scappare il testo. */

/**
 * Scappa anche gli apici: gli attributi in questa pagina si scrivono con le
 * virgolette doppie, ma un apice dentro un valore non deve comunque poter
 * chiudere niente.
 *
 * Sta in un modulo suo perché la usano sia `view` sia `diagram`, e `view`
 * importa `diagram`: tenerla in `view` faceva un ciclo fra i due.
 */
export function escape(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#x27;");
}
