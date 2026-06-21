"""
remotesync.py — Bibliothèque de synchronisation de fichiers vers un serveur distant.
Protocoles supportés : FTP, FTPS, FTP/TLS, SFTP (SSH).
Configuration via fichier INI.
"""

from __future__ import annotations

import configparser
import ftplib
import hashlib
import logging
import os
import stat
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums & dataclasses
# ---------------------------------------------------------------------------

class Protocol(str, Enum):
    FTP   = "ftp"
    FTPS  = "ftps"
    FTPTLS = "ftptls"
    SFTP  = "sftp"


@dataclass
class SyncConfig:
    """Paramètres lus depuis le fichier INI."""
    protocol: Protocol
    host: str
    port: int
    username: str
    password: str = ""
    ssh_key_path: str = ""
    remote_base_dir: str = "/"
    passive_mode: bool = True
    timeout: int = 30
    delete_orphans: bool = False   # supprimer les fichiers distants absents en local
    dry_run: bool = False          # simuler sans transférer

    @classmethod
    def from_ini(cls, path: str | Path, section: str = "remotesync") -> "SyncConfig":
        cfg = configparser.ConfigParser()
        cfg.read(str(path))
        if section not in cfg:
            raise ValueError(f"Section [{section}] introuvable dans {path}")
        s = cfg[section]
        proto_str = s.get("protocol", "ftp").lower()
        try:
            protocol = Protocol(proto_str)
        except ValueError:
            raise ValueError(f"Protocole inconnu : {proto_str!r}. Valeurs acceptées : {[p.value for p in Protocol]}")

        default_ports = {Protocol.FTP: 21, Protocol.FTPS: 990,
                         Protocol.FTPTLS: 21, Protocol.SFTP: 22}
        port = int(s.get("port", default_ports[protocol]))

        return cls(
            protocol=protocol,
            host=s.get("host", ""),
            port=port,
            username=s.get("username", ""),
            password=s.get("password", ""),
            ssh_key_path=s.get("ssh_key_path", ""),
            remote_base_dir=s.get("remote_base_dir", "/"),
            passive_mode=s.getboolean("passive_mode", True),
            timeout=int(s.get("timeout", 30)),
            delete_orphans=s.getboolean("delete_orphans", False),
            dry_run=s.getboolean("dry_run", False),
        )


@dataclass
class SyncResult:
    """Résultat d'une opération de synchronisation."""
    uploaded: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return len(self.errors) == 0

    def __str__(self) -> str:
        return (
            f"SyncResult(uploaded={len(self.uploaded)}, skipped={len(self.skipped)}, "
            f"deleted={len(self.deleted)}, errors={len(self.errors)})"
        )


# ---------------------------------------------------------------------------
# Backend abstrait
# ---------------------------------------------------------------------------

class _BaseBackend(ABC):
    def __init__(self, cfg: SyncConfig):
        self.cfg = cfg

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def upload_file(self, local_path: Path, remote_path: str) -> None: ...

    @abstractmethod
    def remote_mtime(self, remote_path: str) -> Optional[float]:
        """Retourne le timestamp de modification distant, ou None si inconnu."""
        ...

    @abstractmethod
    def makedirs(self, remote_dir: str) -> None: ...

    @abstractmethod
    def list_remote(self, remote_dir: str) -> list[str]:
        """Liste récursive des fichiers sous remote_dir (chemins relatifs à remote_dir)."""
        ...

    @abstractmethod
    def delete_remote(self, remote_path: str) -> None: ...

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.disconnect()


# ---------------------------------------------------------------------------
# Backend FTP / FTPS / FTP+TLS
# ---------------------------------------------------------------------------

class _FTPBackend(_BaseBackend):
    def __init__(self, cfg: SyncConfig):
        super().__init__(cfg)
        self._ftp: Optional[ftplib.FTP] = None

    def connect(self) -> None:
        p = self.cfg.protocol
        if p == Protocol.FTPS:
            self._ftp = ftplib.FTP_TLS()
            self._ftp.connect(self.cfg.host, self.cfg.port, timeout=self.cfg.timeout)
            self._ftp.auth()
            self._ftp.prot_p()
        elif p == Protocol.FTPTLS:
            self._ftp = ftplib.FTP_TLS()
            self._ftp.connect(self.cfg.host, self.cfg.port, timeout=self.cfg.timeout)
            self._ftp.login(self.cfg.username, self.cfg.password)
            self._ftp.prot_p()
        else:  # plain FTP
            self._ftp = ftplib.FTP()
            self._ftp.connect(self.cfg.host, self.cfg.port, timeout=self.cfg.timeout)

        if p != Protocol.FTPTLS:
            self._ftp.login(self.cfg.username, self.cfg.password)

        if self.cfg.passive_mode:
            self._ftp.set_pasv(True)

        logger.info("FTP connecté à %s:%s", self.cfg.host, self.cfg.port)

    def disconnect(self) -> None:
        if self._ftp:
            try:
                self._ftp.quit()
            except Exception:
                self._ftp.close()
            self._ftp = None

    def upload_file(self, local_path: Path, remote_path: str) -> None:
        self.makedirs(os.path.dirname(remote_path))
        with open(local_path, "rb") as fh:
            self._ftp.storbinary(f"STOR {remote_path}", fh)

    def remote_mtime(self, remote_path: str) -> Optional[float]:
        try:
            resp = self._ftp.sendcmd(f"MDTM {remote_path}")
            # Format : "213 YYYYMMDDHHMMSS"
            ts_str = resp[4:].strip()
            from datetime import datetime, timezone
            dt = datetime.strptime(ts_str, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            return None

    def makedirs(self, remote_dir: str) -> None:
        if not remote_dir or remote_dir == "/":
            return
        parts = remote_dir.replace("\\", "/").split("/")
        path = ""
        for part in parts:
            if not part:
                continue
            path += "/" + part
            try:
                self._ftp.mkd(path)
            except ftplib.error_perm as e:
                if "550" not in str(e):  # 550 = déjà existant
                    raise

    def list_remote(self, remote_dir: str) -> list[str]:
        result: list[str] = []
        try:
            items = self._ftp.nlst(remote_dir)
        except ftplib.error_temp:
            return result
        for item in items:
            try:
                # Essayer d'entrer dedans → c'est un répertoire
                self._ftp.cwd(item)
                self._ftp.cwd("/")
                sub = self.list_remote(item)
                result.extend(sub)
            except ftplib.error_perm:
                result.append(item)
        return result

    def delete_remote(self, remote_path: str) -> None:
        self._ftp.delete(remote_path)


# ---------------------------------------------------------------------------
# Backend SFTP (SSH)
# ---------------------------------------------------------------------------

class _SFTPBackend(_BaseBackend):
    def __init__(self, cfg: SyncConfig):
        super().__init__(cfg)
        self._ssh = None
        self._sftp = None

    def connect(self) -> None:
        try:
            import paramiko  # type: ignore
        except ImportError:
            raise ImportError(
                "Le module 'paramiko' est requis pour le protocole SFTP.\n"
                "Installez-le avec : pip install paramiko"
            )

        self._ssh = paramiko.SSHClient()
        self._ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs: dict = dict(
            hostname=self.cfg.host,
            port=self.cfg.port,
            username=self.cfg.username,
            timeout=self.cfg.timeout,
        )
        if self.cfg.ssh_key_path:
            connect_kwargs["key_filename"] = self.cfg.ssh_key_path
        else:
            connect_kwargs["password"] = self.cfg.password

        self._ssh.connect(**connect_kwargs)
        self._sftp = self._ssh.open_sftp()
        logger.info("SFTP connecté à %s:%s", self.cfg.host, self.cfg.port)

    def disconnect(self) -> None:
        if self._sftp:
            self._sftp.close()
        if self._ssh:
            self._ssh.close()
        self._sftp = self._ssh = None

    def upload_file(self, local_path: Path, remote_path: str) -> None:
        self.makedirs(os.path.dirname(remote_path))
        self._sftp.put(str(local_path), remote_path)

    def remote_mtime(self, remote_path: str) -> Optional[float]:
        try:
            attrs = self._sftp.stat(remote_path)
            return float(attrs.st_mtime)
        except Exception:
            return None

    def makedirs(self, remote_dir: str) -> None:
        if not remote_dir:
            return
        parts = remote_dir.replace("\\", "/").split("/")
        path = ""
        for part in parts:
            if not part:
                continue
            path += "/" + part
            try:
                self._sftp.stat(path)
            except FileNotFoundError:
                self._sftp.mkdir(path)

    def list_remote(self, remote_dir: str) -> list[str]:
        result: list[str] = []
        try:
            attrs_list = self._sftp.listdir_attr(remote_dir)
        except Exception:
            return result
        for attr in attrs_list:
            full = remote_dir.rstrip("/") + "/" + attr.filename
            if stat.S_ISDIR(attr.st_mode):
                result.extend(self.list_remote(full))
            else:
                result.append(full)
        return result

    def delete_remote(self, remote_path: str) -> None:
        self._sftp.remove(remote_path)


# ---------------------------------------------------------------------------
# Classe principale
# ---------------------------------------------------------------------------

class RemoteSync:
    """
    Synchronise des fichiers locaux vers un serveur distant.

    Exemple d'utilisation ::

        sync = RemoteSync("config.ini")
        result = sync.sync_directory("/var/www/html", "/public_html")
        result = sync.sync_file("/var/www/html/index.html", "/public_html/index.html")
        print(result)
    """

    def __init__(self, config_path: str | Path, section: str = "remotesync"):
        self.config = SyncConfig.from_ini(config_path, section)
        self._backend: _BaseBackend = self._build_backend()

    # ------------------------------------------------------------------
    # API publique
    # ------------------------------------------------------------------

    def sync_file(
        self,
        local_path: str | Path,
        remote_path: Optional[str] = None,
    ) -> SyncResult:
        """
        Synchronise un fichier unique.

        :param local_path:  Chemin local du fichier source.
        :param remote_path: Chemin distant du fichier cible.
                            - Si omis : ``remote_base_dir/<nom_du_fichier>``
                            - Si relatif (ne commence pas par «/») :
                              ``remote_base_dir/<remote_path>``
                            - Si absolu : utilisé tel quel.
        """
        result = SyncResult()
        local = Path(local_path)
        if not local.is_file():
            result.errors.append(f"Fichier local introuvable : {local}")
            return result

        if remote_path is None:
            remote_path = self.config.remote_base_dir.rstrip("/") + "/" + local.name
        else:
            remote_path = self._resolve_remote(remote_path)

        try:
            with self._backend:
                self._sync_single(local, remote_path, result)
        except Exception as exc:
            logger.exception("Erreur de connexion")
            result.errors.append(str(exc))
        return result

    def sync_directory(
        self,
        local_dir: str | Path,
        remote_dir: Optional[str] = None,
    ) -> SyncResult:
        """
        Synchronise récursivement un répertoire local vers le serveur distant.

        :param local_dir:  Répertoire local source.
        :param remote_dir: Répertoire distant cible.
                           - Si omis : ``remote_base_dir``
                           - Si relatif (ne commence pas par «/») :
                             ``remote_base_dir/<remote_dir>``
                           - Si absolu : utilisé tel quel.
        """
        result = SyncResult()
        local = Path(local_dir)
        if not local.is_dir():
            result.errors.append(f"Répertoire local introuvable : {local}")
            return result

        if remote_dir is None:
            remote_dir = self.config.remote_base_dir
        else:
            remote_dir = self._resolve_remote(remote_dir)

        try:
            with self._backend:
                local_files = {
                    f.relative_to(local).as_posix()
                    for f in local.rglob("*")
                    if f.is_file()
                }

                # Upload / skip
                for rel in sorted(local_files):
                    local_file = local / rel
                    remote_file = remote_dir.rstrip("/") + "/" + rel
                    self._sync_single(local_file, remote_file, result)

                # Suppression des orphelins distants
                if self.config.delete_orphans:
                    remote_files = self._backend.list_remote(remote_dir)
                    for rf in remote_files:
                        rel = rf[len(remote_dir):].lstrip("/")
                        if rel not in local_files:
                            if not self.config.dry_run:
                                self._backend.delete_remote(rf)
                            result.deleted.append(rf)
                            logger.info("[DELETED] %s", rf)

        except Exception as exc:
            logger.exception("Erreur de connexion")
            result.errors.append(str(exc))

        return result

    # ------------------------------------------------------------------
    # Méthodes internes
    # ------------------------------------------------------------------

    def _resolve_remote(self, remote: str) -> str:
        """
        Résout un chemin distant :
        - chemin absolu (commence par «/») → retourné inchangé
        - chemin relatif → ``remote_base_dir/remote``
        """
        if remote.startswith("/"):
            return remote
        return self.config.remote_base_dir.rstrip("/") + "/" + remote

    def _build_backend(self) -> _BaseBackend:
        if self.config.protocol == Protocol.SFTP:
            return _SFTPBackend(self.config)
        return _FTPBackend(self.config)

    def _sync_single(self, local: Path, remote: str, result: SyncResult) -> None:
        """Décide d'uploader ou de sauter un fichier, met à jour result."""
        remote_mtime = self._backend.remote_mtime(remote)
        local_mtime = local.stat().st_mtime

        if remote_mtime is not None and local_mtime <= remote_mtime:
            result.skipped.append(remote)
            logger.debug("[SKIP]   %s (distant plus récent ou identique)", remote)
            return

        action = "[DRY-RUN]" if self.config.dry_run else "[UPLOAD]"
        logger.info("%s %s → %s", action, local, remote)

        if not self.config.dry_run:
            try:
                self._backend.upload_file(local, remote)
                result.uploaded.append(remote)
            except Exception as exc:
                msg = f"{remote}: {exc}"
                result.errors.append(msg)
                logger.error("[ERROR]  %s", msg)
        else:
            result.uploaded.append(remote)
