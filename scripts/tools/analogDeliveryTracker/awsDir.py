"""awsdir — credenziali AWS dalla cartella ``.aws``, nel browser.

Un solo file da copiare in un altro progetto PyScript. Fa tre cose:

1. fa scegliere all'utente una cartella ``.aws`` e la legge (col selettore
   nativo, o con un file input dove quello è vietato);
2. elenca i profili e, per ognuno, le sorgenti di credenziali utilizzabili,
   ordinate per affidabilità e con l'account AWS dichiarato;
3. restituisce sessioni e client boto3 già configurati.

In Pyodide boto3 non funziona senza due aggiustamenti, applicati alla prima
sessione (vedi ``enable_boto3_in_pyodide``).

Uso::

    import awsdir

    aws = await awsdir.AwsDirectory.pick()      # da un gestore di evento
    ddb = aws.client("dynamodb", "sso_pn-core-dev", require_account=True)

Scelta esplicita della sorgente, quando in cache ci sono più account::

    for source in aws.sources("sso_pn-core-dev"):
        print(source.describe())
"""

from __future__ import annotations

import base64
import configparser
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

__version__ = "1.0.0"

__all__ = [
    "AwsDirectory",
    "CredentialSource",
    "AwsDirError",
    "NoCredentialsFound",
    "CredentialsExpired",
    "AccountMismatch",
    "enable_boto3_in_pyodide",
    "import_boto3",
    "account_from_access_key",
    "account_from_arn",
    "whoami",
]

MAX_FILE_BYTES = 2_000_000


class AwsDirError(Exception):
    """Errore generico della libreria."""


class NoCredentialsFound(AwsDirError):
    """Nessuna sorgente di credenziali per il profilo richiesto."""


class CredentialsExpired(AwsDirError):
    """Le sorgenti trovate sono tutte scadute."""


class AccountMismatch(AwsDirError):
    """Le credenziali sono di un account diverso da quello del profilo."""


# =============================================================================
# boto3 dentro Pyodide
# =============================================================================
_PYODIDE_REPORT = None

# Header che il browser non lascia impostare: li mette lui, con lo stesso valore
# che SigV4 ha già firmato (host e content-length su tutti), quindi saltarli non
# invalida la firma. `user-agent` non rientra nella firma di SigV4.
_FORBIDDEN_HEADERS = frozenset({
    "host", "content-length", "connection", "user-agent", "accept-encoding",
    "origin", "referer", "cookie", "date", "expect", "keep-alive",
    "transfer-encoding", "upgrade", "via", "trailer", "te",
})


class _RawBody:
    """Il minimo dell'oggetto urllib3 che botocore usa come ``raw``."""

    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0

    def read(self, amt=None):
        if amt is None:
            chunk, self._pos = self._data[self._pos:], len(self._data)
        else:
            chunk = self._data[self._pos:self._pos + amt]
            self._pos += len(chunk)
        return chunk

    def stream(self, amt=1024 * 1024, decode_content=None):
        while self._pos < len(self._data):
            yield self.read(amt)

    def close(self):
        pass

    @property
    def closed(self):
        return self._pos >= len(self._data)


def _xhr_send(self, request):
    """Sostituisce ``URLLib3Session.send`` con una XMLHttpRequest sincrona.

    Sincrona perché l'API di botocore non è async: blocca il thread della UI per
    la durata della chiamata, quindi chi la usa da un'interfaccia grafica deve
    cedere il controllo al browser prima (``await asyncio.sleep(0)``).
    """
    from js import XMLHttpRequest
    from botocore.awsrequest import AWSResponse
    from botocore.exceptions import EndpointConnectionError

    xhr = XMLHttpRequest.new()
    xhr.open(request.method, request.url, False)

    for name, value in request.headers.items():
        if name.lower() in _FORBIDDEN_HEADERS:
            continue
        if isinstance(value, (bytes, bytearray)):
            value = value.decode("utf-8")
        xhr.setRequestHeader(name, value)

    body = request.body
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8")

    try:
        xhr.send(body)
    except Exception as exc:                 # rete assente o preflight CORS rifiutato
        raise EndpointConnectionError(endpoint_url=request.url) from exc
    if xhr.status == 0:
        raise EndpointConnectionError(endpoint_url=request.url)

    headers = {}
    for line in (xhr.getAllResponseHeaders() or "").split("\r\n"):
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()

    payload = (xhr.responseText or "").encode("utf-8")
    return AWSResponse(request.url, xhr.status, headers, _RawBody(payload))


def _shim_urllib3():
    """Pyodide monta una urllib3 adattata a Emscripten da cui ``urllib3.util.ssl_``
    non esporta ``ssl`` né le costanti che botocore importa: senza quei nomi
    ``import boto3`` fallisce. Servono solo a far passare l'import, perché il
    contesto SSL non viene mai creato: l'handshake lo fa il browser.
    """
    import urllib3.util.ssl_ as ssl_module

    try:
        import ssl as stdlib_ssl
    except ImportError:
        stdlib_ssl = None

    placeholders = {
        "ssl": stdlib_ssl,
        "SSLContext": getattr(stdlib_ssl, "SSLContext", None),
        "SSLTransport": None,
        "HAS_SNI": True,
        "IS_PYOPENSSL": False,
        "IS_SECURETRANSPORT": False,
        "DEFAULT_CIPHERS": "",
        "OP_NO_COMPRESSION": 0x20000,
        "OP_NO_TICKET": 0x4000,
        "OP_NO_SSLv2": 0x1000000,
        "OP_NO_SSLv3": 0x2000000,
        "PROTOCOL_TLS": getattr(stdlib_ssl, "PROTOCOL_TLS", 2),
        "PROTOCOL_TLS_CLIENT": getattr(stdlib_ssl, "PROTOCOL_TLS_CLIENT", 16),
        "resolve_cert_reqs": lambda candidate: None,
        "resolve_ssl_version": lambda candidate: None,
        "create_urllib3_context": lambda *args, **kwargs: None,
        "ssl_wrap_socket": lambda *args, **kwargs: None,
    }
    added = [name for name in placeholders if not hasattr(ssl_module, name)]
    for name in added:
        setattr(ssl_module, name, placeholders[name])
    return added


def enable_boto3_in_pyodide(force: bool = False) -> dict:
    """Rende boto3 utilizzabile in Pyodide. Idempotente.

    Due interventi: i nomi mancanti in ``urllib3.util.ssl_`` (vedi
    :func:`_shim_urllib3`) e il transport di botocore, sostituito con
    XMLHttpRequest sincrona. Si patcha la **classe** ``URLLib3Session`` e non il
    nome nel modulo, perché ``EndpointCreator.create_endpoint`` la tiene come
    default di un argomento, valutato all'import.
    """
    global _PYODIDE_REPORT
    if _PYODIDE_REPORT is not None and not force:
        return _PYODIDE_REPORT

    added = _shim_urllib3()
    from botocore.httpsession import URLLib3Session

    URLLib3Session.__init__ = lambda self, *args, **kwargs: None
    URLLib3Session.send = _xhr_send
    URLLib3Session.close = lambda self: None

    _PYODIDE_REPORT = {"patched": True, "urllib3_shims": added, "transport": "XMLHttpRequest"}
    return _PYODIDE_REPORT


def import_boto3():
    """Prepara l'ambiente e restituisce ``boto3``.

    Serve perché un ``import boto3`` in cima a un modulo viene eseguito prima
    dello shim e fallisce con ``ImportError: cannot import name 'ssl' from
    'urllib3.util.ssl_'``.
    """
    enable_boto3_in_pyodide()
    import boto3
    return boto3


# =============================================================================
# Account
# =============================================================================
def account_from_arn(arn: str | None) -> str | None:
    """Estrae l'account id da un ARN. Vale per qualsiasi ARN AWS, compreso il
    ``TableArn`` di DynamoDB: è la fonte più affidabile quando
    ``sts:GetCallerIdentity`` è negato dal permission set."""
    if not arn:
        return None
    parts = arn.split(":")
    return parts[4] if len(parts) > 4 and parts[4] else None


def account_from_access_key(access_key_id: str | None) -> str | None:
    """Ricava l'account id dall'access key, che lo contiene codificato.

    Il formato non è documentato da AWS: è un **indizio** per etichettare le
    sorgenti prima di connettersi, quando la cache SSO non porta un ARN.
    """
    if not access_key_id or len(access_key_id) < 12:
        return None
    try:
        raw = base64.b32decode(access_key_id[4:].upper())
        value = int.from_bytes(raw[:6], "big")
        return str((value & 0x7FFFFFFFFF80) >> 7).zfill(12)
    except Exception:
        return None


def whoami(session, region: str | None = None) -> dict | None:
    """``sts:GetCallerIdentity`` sulla sessione, oppure ``None`` se non risponde.

    Diversi permission set SSO negano STS pur lasciando lavorare gli altri
    servizi: per questo il fallimento non è un errore, è un ``None``.
    """
    try:
        return session.client("sts", region_name=region).get_caller_identity()
    except Exception:
        return None


# =============================================================================
# Lettura dei file
# =============================================================================
_WANTED_FILES = ("config", "credentials")
_WANTED_DIRS = ("cli/cache", "sso/cache")


#: handle della cartella scelta: riferimento vivo, quindi rileggerlo vede i file
#: aggiornati. Sta anche in IndexedDB, per sopravvivere ai refresh.
_dir_handle = None

_IDB_NAME = "awsdir"
_IDB_STORE = "handles"
_IDB_KEY = "aws-dir"


def _js_options(**values):
    """Un oggetto JS letterale, come lo vogliono le API del File System Access."""
    import js
    from pyodide.ffi import to_js

    return to_js(values, dict_converter=js.Object.fromEntries)


async def _await_request(request):
    """Attende un ``IDBRequest``: IndexedDB parla con eventi, non con promesse."""
    import asyncio
    from pyodide.ffi import create_proxy

    done = asyncio.get_running_loop().create_future()

    def settle(event):
        if not done.done():
            if event.type == "success":
                done.set_result(request.result)
            else:
                done.set_exception(AwsDirError(f"IndexedDB: {event.type}"))

    proxy = create_proxy(settle)
    request.addEventListener("success", proxy)
    request.addEventListener("error", proxy)
    try:
        return await done
    finally:
        proxy.destroy()


async def _idb_database():
    """Il database degli handle, creando l'object store alla prima apertura.

    Restituisce il database e non lo store: fra ``transaction()`` e la richiesta
    non ci deve stare un ``await``, o la transazione si chiude prima.
    """
    import js
    from pyodide.ffi import create_proxy

    request = js.indexedDB.open(_IDB_NAME, 1)

    def upgrade(event):
        database = event.target.result
        if not database.objectStoreNames.contains(_IDB_STORE):
            database.createObjectStore(_IDB_STORE)

    proxy = create_proxy(upgrade)
    request.addEventListener("upgradeneeded", proxy)
    try:
        database = await _await_request(request)
    finally:
        proxy.destroy()
    return database


async def _remembered_handle():
    """L'handle lasciato dalla visita precedente, o ``None``."""
    try:
        database = await _idb_database()
        store = database.transaction(_IDB_STORE, "readonly").objectStore(_IDB_STORE)
        return await _await_request(store.get(_IDB_KEY)) or None
    except Exception:
        return None


async def _remember_handle(handle):
    """Ricorda l'handle per il prossimo refresh; con ``None`` lo dimentica."""
    try:
        database = await _idb_database()
        store = database.transaction(_IDB_STORE, "readwrite").objectStore(_IDB_STORE)
        request = store.delete(_IDB_KEY) if handle is None else store.put(handle, _IDB_KEY)
        await _await_request(request)
    except Exception:
        pass                                # è una comodità: se salta, pazienza


async def _readable(handle, *, ask=False):
    """Vero se l'handle è ancora leggibile.

    Dopo un refresh il permesso può essere tornato «prompt»: con ``ask`` lo si
    richiede, e all'utente tocca un clic invece di ricercare la cartella.
    """
    try:
        state = await handle.queryPermission(_js_options(mode="read"))
        if state != "granted" and ask:
            state = await handle.requestPermission(_js_options(mode="read"))
        return state == "granted"
    except Exception:
        return False


async def _show_directory_picker():
    """Apre il selettore nativo. Va chiamato da un gesto dell'utente.

    Niente ``startIn``: ammette solo desktop/documents/downloads/music/pictures/
    videos, e un valore fuori lista fa fallire la chiamata in validazione. Ci
    pensa ``id``, che riapre sull'ultima cartella scelta.
    """
    import js

    try:
        return await js.showDirectoryPicker(_js_options(id="awsdir", mode="read"))
    except Exception as exc:
        if getattr(exc, "name", "") == "AbortError":
            raise AwsDirError("Nessuna cartella scelta.") from None
        raise


async def _child(parent, name, *, directory=False):
    """Il figlio ``name``, o ``None`` se non c'è (o se non è leggibile)."""
    try:
        if directory:
            return await parent.getDirectoryHandle(name)
        return await parent.getFileHandle(name)
    except Exception:
        return None


async def _handle_text(handle):
    item = await handle.getFile()
    return None if item.size > MAX_FILE_BYTES else await item.text()


async def _read_handle_tree(root):
    """Legge config, credentials e le due cache da un ``FileSystemDirectoryHandle``.

    Ogni lettura passa dall'handle, quindi arriva sul disco: ripremere il
    bottone dopo un ``aws sso login`` rivede davvero i file nuovi.
    """
    files = {}
    for name in _WANTED_FILES:
        handle = await _child(root, name)
        if handle is not None:
            text = await _handle_text(handle)
            if text is not None:
                files[name] = text
    for sub in _WANTED_DIRS:
        folder = root
        for part in sub.split("/"):
            folder = await _child(folder, part, directory=True)
            if folder is None:
                break
        if folder is None:
            continue
        async for handle in folder.values():
            if handle.kind != "file" or not handle.name.endswith(".json"):
                continue
            text = await _handle_text(handle)
            if text is not None:
                files[f"{sub}/{handle.name}"] = text
    return dict(sorted(files.items()))


def _is_wanted(rel):
    """Vero per i percorsi relativi che interessano dentro ``.aws``."""
    if rel in _WANTED_FILES:
        return True
    folder, _, name = rel.rpartition("/")
    return folder in _WANTED_DIRS and name.endswith(".json")


def _native_picker_available():
    """Vero se ``showDirectoryPicker`` è utilizzabile in questo contesto.

    Chromium lo vieta nei sotto-frame la cui origine non è quella del frame
    top-level, e non esiste un ``allow=`` per autorizzarlo. Leggere
    ``window.top.location.origin`` fallisce esattamente negli stessi casi.
    """
    try:
        from js import window

        if not hasattr(window, "showDirectoryPicker"):
            return False
        return window.top.location.origin == window.location.origin
    except Exception:
        return False


async def _read_input_files(node):
    """Come :func:`_read_handle_tree`, ma da un ``<input webkitdirectory>``.

    ``webkitRelativePath`` comincia col nome della cartella scelta: va tolto.
    """
    files = {}
    for index in range(node.files.length):
        item = node.files.item(index)
        rel = str(item.webkitRelativePath).split("/", 1)[-1]
        if _is_wanted(rel) and item.size <= MAX_FILE_BYTES:
            files[rel] = await item.text()
    return dict(sorted(files.items()))


def _parse_ini(text):
    parser = configparser.ConfigParser(strict=False, inline_comment_prefixes=("#", ";"))
    try:
        parser.read_string(text)
    except configparser.Error:
        return {}
    return {name: dict(parser[name]) for name in parser.sections()}


def _build_profiles(files):
    profiles, sso_sessions = {}, {}
    for section, values in _parse_ini(files.get("config", "")).items():
        if section == "default":
            profiles.setdefault("default", {}).update(values)
        elif section.startswith("profile "):
            profiles.setdefault(section[8:].strip(), {}).update(values)
        elif section.startswith("sso-session "):
            sso_sessions[section[12:].strip()] = values
    # il file credentials ha la precedenza sulle chiavi statiche di config
    for section, values in _parse_ini(files.get("credentials", "")).items():
        profiles.setdefault(section, {}).update(values)
    return profiles, sso_sessions


def _to_datetime(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000 if value > 1e12 else value, timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _mask(value):
    return f"{value[:5]}…{value[-4:]}" if value and len(value) > 9 else (value or "")


# =============================================================================
# Sorgenti di credenziali
# =============================================================================
@dataclass(frozen=True)
class CredentialSource:
    """Una credenziale utilizzabile, con la sua provenienza."""

    access_key_id: str
    secret_access_key: str = field(repr=False)
    session_token: str | None = field(default=None, repr=False)
    expires_at: datetime | None = None
    account: str | None = None
    account_is_guess: bool = False
    role: str | None = None
    origin: str = ""
    score: int = 0

    @property
    def valid(self) -> bool:
        """Falso se scaduta. Senza scadenza dichiarata è considerata valida."""
        return self.expires_at is None or self.expires_at > datetime.now(timezone.utc)

    @property
    def expires_in(self) -> timedelta | None:
        return None if self.expires_at is None else self.expires_at - datetime.now(timezone.utc)

    def describe(self) -> str:
        bits = [self.origin or "?", _mask(self.access_key_id)]
        if self.account:
            bits.append(f"account {self.account}" + (" (dedotto)" if self.account_is_guess else ""))
        if self.role:
            bits.append(self.role)
        remaining = self.expires_in
        if remaining is not None:
            minutes = int(abs(remaining.total_seconds()) // 60)
            label = f"{minutes} min" if minutes < 60 else f"{minutes // 60}h {minutes % 60:02d}m"
            bits.append(f"scade tra {label}" if self.valid else f"scaduta da {label}")
        return " · ".join(bits)

    def as_boto3_kwargs(self) -> dict:
        kwargs = {
            "aws_access_key_id": self.access_key_id,
            "aws_secret_access_key": self.secret_access_key,
        }
        if self.session_token:
            kwargs["aws_session_token"] = self.session_token
        return kwargs


# =============================================================================
# La cartella .aws
# =============================================================================
class AwsDirectory:
    """Una cartella ``.aws`` letta in memoria.

    È un'istantanea: per rileggere, dopo un ``aws sso login``, richiama
    :meth:`pick`, che non riaprirà il selettore.
    """

    def __init__(self, files, origin: str = ""):
        self.files = dict(files)
        self.origin = origin
        self.profiles, self.sso_sessions = _build_profiles(self.files)
        self._sessions = {}

    # ------------------------------------------------------------------ init
    @classmethod
    def from_files(cls, files, origin: str = "mapping") -> "AwsDirectory":
        """Da un dizionario ``percorso relativo -> contenuto``. Utile nei test."""
        return cls(files, origin=origin)

    @classmethod
    async def pick(cls) -> "AwsDirectory":
        """Fa scegliere la cartella all'utente e la legge.

        Va chiamata da un gestore di evento: sia il selettore sia la richiesta
        di permesso su un handle ricordato vogliono un gesto dell'utente. Dalla
        seconda volta non riappare nulla, ma i file sono riletti dal disco.

        Non passa da ``pyscript.fs.mount``, che copia la cartella in un
        filesystem Emscripten e alle riletture restituiva la copia vecchia.

        Dove il selettore è vietato (sotto-frame di altra origine) o non esiste
        (fuori da Chromium) ripiega su :meth:`from_input`.
        """
        global _dir_handle

        if not _native_picker_available():
            return await cls.from_input()

        handle = _dir_handle if _dir_handle is not None else await _remembered_handle()
        if handle is not None and not await _readable(handle, ask=True):
            # ``requestPermission`` consuma il gesto dell'utente, quindi qui non
            # si può più aprire il selettore: meglio dirlo che fallire dopo.
            _dir_handle = None
            await _remember_handle(None)
            raise AwsDirError("Permesso negato sulla cartella ricordata: "
                              "premi di nuovo per sceglierne una.")
        if handle is None:
            handle = await _show_directory_picker()
            await _remember_handle(handle)

        files = await _read_handle_tree(handle)
        if not files:
            _dir_handle = None                  # sbagliata: non ricordarla
            await _remember_handle(None)
            raise AwsDirError(
                "Nella cartella scelta non ci sono config, credentials né file di cache: "
                "probabilmente non è ~/.aws.")
        _dir_handle = handle
        return cls(files, origin=f"cartella scelta dall'utente ({handle.name})")

    @classmethod
    async def from_input(cls) -> "AwsDirectory":
        """Fa scegliere la cartella con ``<input type=file webkitdirectory>``.

        Un file input non ha il limite del selettore sui sotto-frame di altra
        origine, e la sola lettura qui basta.
        """
        import asyncio

        from js import document
        from pyodide.ffi import create_proxy

        node = document.createElement("input")
        node.type = "file"
        node.multiple = True
        node.style.display = "none"
        for name in ("webkitdirectory", "directory"):   # il secondo per i non-Chromium
            node.setAttribute(name, "")
        document.body.appendChild(node)

        chosen = asyncio.get_running_loop().create_future()

        def on_close(event):                            # "cancel" se l'utente annulla
            if not chosen.done():
                chosen.set_result(None)

        proxy = create_proxy(on_close)
        node.addEventListener("change", proxy)
        node.addEventListener("cancel", proxy)
        try:
            node.click()
            await chosen
            empty = node.files.length == 0
            files = {} if empty else await _read_input_files(node)
        finally:
            proxy.destroy()
            node.remove()

        if empty:
            raise AwsDirError("Nessuna cartella scelta.")
        if not files:
            raise AwsDirError(
                "Nella cartella scelta non ci sono config, credentials né file di cache: "
                "probabilmente non è ~/.aws.")
        return cls(files, origin="cartella scelta dall'utente (file input)")

    # --------------------------------------------------------------- profili
    def profile_names(self) -> list:
        """Profili trovati, ``default`` per primo."""
        return sorted(self.profiles, key=lambda name: (name != "default", name))

    def _resolve_profile(self, profile):
        if profile:
            if profile not in self.profiles:
                raise AwsDirError(
                    f"Profilo '{profile}' assente. Disponibili: {', '.join(self.profile_names())}")
            return profile
        if "default" not in self.profiles:
            raise AwsDirError(
                f"Nessun profilo indicato e 'default' non esiste. "
                f"Disponibili: {', '.join(self.profile_names())}")
        return "default"

    def region_for(self, profile=None) -> str | None:
        """Region del profilo, o ``None``.

        Di proposito *non* ripiega su ``sso_region``: quella è la region del
        portale SSO, e manderebbe le chiamate dove una tabella omonima risponde
        0 risultati invece di dare errore.
        """
        return self.profiles.get(self._resolve_profile(profile), {}).get("region")

    def expected_account(self, profile=None) -> str | None:
        """L'account a cui punta il profilo, se dichiarato (``sso_account_id``)."""
        return self.profiles.get(self._resolve_profile(profile), {}).get("sso_account_id")

    # ------------------------------------------------------------- sorgenti
    def sources(self, profile=None) -> list:
        """Sorgenti utilizzabili per il profilo, dalla più alla meno affidabile.

        Le scadute restano in coda: servono a spiegare all'utente che deve
        rinnovare la sessione, invece di far sparire tutto.
        """
        name = self._resolve_profile(profile)
        settings = self.profiles.get(name, {})
        wanted_account = settings.get("sso_account_id")
        found = []

        access = settings.get("aws_access_key_id")
        secret = settings.get("aws_secret_access_key")
        if access and secret:
            found.append(CredentialSource(
                access_key_id=access, secret_access_key=secret,
                session_token=settings.get("aws_session_token"),
                account=account_from_access_key(access), account_is_guess=True,
                origin=f"profilo {name}", score=100))

        for path, text in self.files.items():
            if not path.startswith(("cli/cache/", "sso/cache/")):
                continue
            try:
                blob = json.loads(text)
            except ValueError:
                continue
            creds = blob.get("Credentials") or blob.get("credentials") or blob.get("roleCredentials")
            if not isinstance(creds, dict):
                continue
            access = creds.get("AccessKeyId") or creds.get("accessKeyId")
            secret = creds.get("SecretAccessKey") or creds.get("secretAccessKey")
            if not access or not secret:
                continue

            arn = (blob.get("AssumedRoleUser") or {}).get("Arn", "")
            account = account_from_arn(arn)
            guessed = False
            if not account:                       # la cache SSO non porta un ARN
                account = account_from_access_key(access)
                guessed = account is not None
            role = arn.split("/")[1] if "/" in arn else None

            score = 20
            if wanted_account and account == wanted_account:
                score += 40
            if settings.get("sso_role_name") and role == settings["sso_role_name"]:
                score += 25
            if settings.get("role_arn") and role == settings["role_arn"].split("/")[-1]:
                score += 40

            expires_at = _to_datetime(creds.get("Expiration", creds.get("expiration")))
            found.append(CredentialSource(
                access_key_id=access, secret_access_key=secret,
                session_token=creds.get("SessionToken") or creds.get("sessionToken"),
                expires_at=expires_at, account=account, account_is_guess=guessed,
                role=role, origin=path, score=score + (10 if expires_at is None else 0)))

        far_past = datetime.min.replace(tzinfo=timezone.utc)
        found.sort(key=lambda s: (s.valid, s.score, s.expires_at or far_past), reverse=True)
        return found

    def best_source(self, profile=None, *, allow_expired=False,
                    require_account=False) -> CredentialSource:
        """La sorgente migliore, o un errore che spiega cosa fare.

        Con ``require_account=True`` rifiuta credenziali di un account diverso da
        ``sso_account_id``: senza quel controllo una tabella omonima in un altro
        ambiente risponde 0 risultati invece di dare errore.
        """
        name = self._resolve_profile(profile)
        found = self.sources(name)
        if not found:
            raise NoCredentialsFound(
                f"Nessuna credenziale per il profilo '{name}'.\n"
                f"  aws sso login --profile {name} && "
                f"aws sts get-caller-identity --profile {name}")

        usable = found if allow_expired else [s for s in found if s.valid]
        if not usable:
            raise CredentialsExpired(
                f"Le credenziali per '{name}' sono scadute ({found[0].describe()}).\n"
                f"  aws sso login --profile {name} && "
                f"aws sts get-caller-identity --profile {name}\n"
                "Poi rileggi la cartella: il login da solo non tocca cli/cache.")

        wanted = self.expected_account(name)
        if require_account and wanted:
            matching = [s for s in usable if s.account == wanted]
            if not matching:
                raise AccountMismatch(
                    f"Il profilo '{name}' punta all'account {wanted}, ma le credenziali "
                    f"disponibili sono di: {', '.join(sorted({s.account or '?' for s in usable}))}.\n"
                    f"  aws sso login --profile {name} && "
                    f"aws sts get-caller-identity --profile {name}")
            return matching[0]
        return usable[0]

    # ----------------------------------------------------------------- boto3
    def session(self, profile=None, *, source=None, region=None,
                allow_expired=False, require_account=False):
        """``boto3.Session`` pronta all'uso. Le sessioni sono riusate."""
        boto3 = import_boto3()          # lo shim deve precedere l'import di boto3

        name = self._resolve_profile(profile)
        source = source or self.best_source(
            name, allow_expired=allow_expired, require_account=require_account)
        region = region or self.region_for(name)
        if not region:
            raise AwsDirError(
                f"Nessuna region per '{name}': passala a session()/client() o "
                f"scrivila nel profilo. (sso_region è la region del portale SSO, "
                f"non quella dei servizi.)")

        key = (name, source.access_key_id, region)
        if key not in self._sessions:
            self._sessions[key] = boto3.session.Session(region_name=region,
                                                        **source.as_boto3_kwargs())
        return self._sessions[key]

    def client(self, service_name: str, profile=None, *, source=None, region=None,
               allow_expired=False, require_account=False, **kwargs):
        """Client boto3, per esempio ``aws.client("dynamodb", "sso_pn-core-dev")``."""
        session = self.session(profile, source=source, region=region,
                               allow_expired=allow_expired, require_account=require_account)
        return session.client(service_name, **kwargs)

    def resource(self, service_name: str, profile=None, *, source=None, region=None,
                 allow_expired=False, require_account=False, **kwargs):
        """Resource boto3, per esempio ``aws.resource("dynamodb")``."""
        session = self.session(profile, source=source, region=region,
                               allow_expired=allow_expired, require_account=require_account)
        return session.resource(service_name, **kwargs)

    def __repr__(self):
        return (f"<AwsDirectory {self.origin!r} "
                f"profili={len(self.profiles)} file={len(self.files)}>")
