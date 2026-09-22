"""awsdir — credenziali AWS dalla cartella ``.aws``, nel browser e fuori.

Un solo file da copiare in un altro progetto. Fa tre cose:

1. legge una cartella ``.aws`` (nel browser la fa scegliere all'utente con
   ``pyscript.fs.mount``, altrove la legge da disco);
2. elenca i profili e, per ognuno, le sorgenti di credenziali utilizzabili,
   ordinate per affidabilità e con l'account AWS dichiarato;
3. restituisce sessioni e client boto3 già configurati.

In Pyodide boto3 non funziona senza due aggiustamenti, che la libreria applica da
sola alla prima sessione (vedi ``enable_boto3_in_pyodide``).

Uso nel browser (PyScript)::

    import awsdir

    aws = await awsdir.AwsDirectory.pick()      # apre il selettore di cartelle
    print(aws.profile_names())
    ddb = aws.client("dynamodb", "sso_pn-core-dev")
    ddb.list_tables()

Uso normale (CPython)::

    import awsdir

    aws = awsdir.AwsDirectory.from_path()       # ~/.aws
    ddb = aws.client("dynamodb", "sso_pn-core-dev")

Scelta esplicita della sorgente, quando in cache ci sono più account::

    for source in aws.sources("sso_pn-core-dev"):
        print(source.describe())
    ddb = aws.client("dynamodb", "sso_pn-core-dev", source=sources[0])
"""

from __future__ import annotations

import base64
import configparser
import json
import os
import sys
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
    "IN_PYODIDE",
]

#: vero quando il codice gira in Pyodide (PyScript, JupyterLite, …)
IN_PYODIDE = sys.platform == "emscripten"

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
    """Rende boto3 utilizzabile in Pyodide. Fuori da Pyodide non fa nulla.

    Chiamata da sola alla prima sessione; è idempotente, si può invocare prima
    per avere il resoconto di cosa è stato toccato.

    Due interventi:

    * i nomi mancanti in ``urllib3.util.ssl_`` (vedi :func:`_shim_urllib3`);
    * il transport di botocore, sostituito con XMLHttpRequest sincrona. Si
      patcha la **classe** ``URLLib3Session`` e non il nome nel modulo, perché
      ``EndpointCreator.create_endpoint`` la tiene come valore di default di un
      argomento, valutato all'import: riassegnare l'attributo non avrebbe effetto.
    """
    global _PYODIDE_REPORT
    if _PYODIDE_REPORT is not None and not force:
        return _PYODIDE_REPORT

    if not IN_PYODIDE:
        _PYODIDE_REPORT = {"patched": False, "reason": "non siamo in Pyodide"}
        return _PYODIDE_REPORT

    added = _shim_urllib3()
    from botocore.httpsession import URLLib3Session

    URLLib3Session.__init__ = lambda self, *args, **kwargs: None
    URLLib3Session.send = _xhr_send
    URLLib3Session.close = lambda self: None

    _PYODIDE_REPORT = {"patched": True, "urllib3_shims": added, "transport": "XMLHttpRequest"}
    return _PYODIDE_REPORT


def import_boto3():
    """Prepara l'ambiente e restituisce il modulo ``boto3``.

    Scorciatoia a prova di errore: ``import boto3`` scritto prima dello shim
    fallisce con ``ImportError: cannot import name 'ssl' from
    'urllib3.util.ssl_'``, e in cima a un modulo viene eseguito per primo.
    Con questa funzione l'ordine è garantito::

        import awsdir
        boto3 = awsdir.import_boto3()

        s3 = boto3.client("s3", region_name="eu-south-1",
                          aws_access_key_id=..., aws_secret_access_key=...)
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

    Il formato non è documentato da AWS: è un **indizio**, utile per etichettare
    le sorgenti prima di connettersi quando la cache SSO non porta un ARN.
    Per una certezza usa :func:`whoami` o :func:`account_from_arn` su una
    risorsa reale.
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


def _read_text(path):
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            return None
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def _read_tree(root):
    """Legge solo ciò che serve: config, credentials e le due cache."""
    files = {}
    for name in _WANTED_FILES:
        text = _read_text(os.path.join(root, name))
        if text is not None:
            files[name] = text
    for sub in _WANTED_DIRS:
        folder = os.path.join(root, *sub.split("/"))
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            if not name.endswith(".json"):
                continue
            text = _read_text(os.path.join(folder, name))
            if text is not None:
                files[f"{sub}/{name}"] = text
    return files


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
    """Una cartella ``.aws`` già letta in memoria.

    I file vengono letti una volta sola: nel browser la cartella viene smontata
    subito dopo, quindi l'oggetto resta utilizzabile senza riaprire il selettore.
    Per rileggere (per esempio dopo un ``aws sso login``) costruiscine uno nuovo.
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
    def from_path(cls, path: str = "~/.aws") -> "AwsDirectory":
        """Legge la cartella da disco (CPython)."""
        root = os.path.expanduser(path)
        if not os.path.isdir(root):
            raise AwsDirError(f"{root} non esiste o non è una cartella.")
        files = _read_tree(root)
        if not files:
            raise AwsDirError(f"In {root} non ci sono config, credentials né file di cache.")
        return cls(files, origin=root)

    @classmethod
    async def pick(cls, mount_point: str = "/aws", *, keep_mounted: bool = False) -> "AwsDirectory":
        """Apre il selettore di cartelle del browser (PyScript).

        Va chiamata da un gestore di evento: il selettore richiede un gesto
        dell'utente, e il primo ``await`` della catena deve essere il montaggio.
        """
        from pyscript import fs

        try:
            await fs.mount(mount_point, mode="read", id="awsdir")
        except TypeError:                       # firme diverse fra versioni di PyScript
            await fs.mount(mount_point)
        try:
            files = _read_tree(mount_point)
        finally:
            if not keep_mounted:
                try:
                    await fs.unmount(mount_point)
                except Exception:
                    pass
        if not files:
            raise AwsDirError(
                "Nella cartella scelta non ci sono config, credentials né file di cache: "
                "probabilmente non è ~/.aws.")
        return cls(files, origin=f"cartella scelta dall'utente ({mount_point})")

    @classmethod
    async def open(cls, path: str = "~/.aws", mount_point: str = "/aws") -> "AwsDirectory":
        """Selettore nel browser, disco altrove."""
        if IN_PYODIDE:
            return await cls.pick(mount_point)
        return cls.from_path(path)

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
        chosen = os.environ.get("AWS_PROFILE") or "default"
        if chosen not in self.profiles:
            raise AwsDirError(
                f"Nessun profilo indicato e '{chosen}' non esiste. "
                f"Disponibili: {', '.join(self.profile_names())}")
        return chosen

    def region_for(self, profile=None) -> str | None:
        """Region del profilo, poi le variabili d'ambiente, poi ``None``.

        Di proposito *non* ripiega su ``sso_region``: quella è la region del
        portale SSO, non quella dei servizi. Usarla manderebbe le chiamate in
        una region sbagliata, dove una tabella omonima risponde 0 risultati
        invece di dare errore.
        """
        settings = self.profiles.get(self._resolve_profile(profile), {})
        return (settings.get("region") or os.environ.get("AWS_REGION")
                or os.environ.get("AWS_DEFAULT_REGION"))

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

            source = CredentialSource(
                access_key_id=access, secret_access_key=secret,
                session_token=creds.get("SessionToken") or creds.get("sessionToken"),
                expires_at=_to_datetime(creds.get("Expiration", creds.get("expiration"))),
                account=account, account_is_guess=guessed, role=role,
                origin=path, score=score + (10 if _to_datetime(
                    creds.get("Expiration", creds.get("expiration"))) is None else 0))
            found.append(source)

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
                f"  aws sso login --profile {name}\n"
                f"  aws sts get-caller-identity --profile {name}   # popola ~/.aws/cli/cache")

        usable = found if allow_expired else [s for s in found if s.valid]
        if not usable:
            raise CredentialsExpired(
                f"Le credenziali per '{name}' sono scadute ({found[0].describe()}).\n"
                f"  aws sso login --profile {name}")

        wanted = self.expected_account(name)
        if require_account and wanted:
            matching = [s for s in usable if s.account == wanted]
            if not matching:
                raise AccountMismatch(
                    f"Il profilo '{name}' punta all'account {wanted}, ma le credenziali "
                    f"disponibili sono di: {', '.join(sorted({s.account or '?' for s in usable}))}.\n"
                    f"  aws sso login --profile {name}")
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
                f"Nessuna region per '{name}': passala a session()/client(), "
                f"scrivila nel profilo oppure esporta AWS_REGION. "
                f"(sso_region non viene usata: è la region del portale SSO, "
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
