"""Authenticated bounded operator storage. No key/config files or plaintext files."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import time
from uuid import UUID, uuid4

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

DUMP_LIMIT = 64 * 1024 * 1024
FILE_LIMIT = 96 * 1024 * 1024
INVENTORY_LIMIT = 16 * 1024 * 1024
RETENTION = 7 * 86400
TOMBSTONE_RETENTION = 30 * 86400
LEAF = "0017_scope_control_data_lifecycle"


class StorageFailure(Exception):
    """Only the closed code is exposed; never a filesystem/crypto exception."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def now_seconds():
    return int(time.time())


def uuid_text(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise StorageFailure("invalid_identifier") from None


def _json(data):
    return json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()


def _object(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    try:
        result = json.loads(data, object_pairs_hook=unique)
        if type(result) is not dict:
            raise ValueError()
        return result
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise StorageFailure("invalid_artifact") from None


def _name(value):
    if (
        not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value)
        or value in (".", "..")
        or value.startswith(".env")
    ):
        raise StorageFailure("unsafe_path")
    return value


def _regular(info):
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.getuid()
        and info.st_nlink == 1
        and stat.S_IMODE(info.st_mode) == 0o600
    )


class OwnedDirectory:
    def __init__(self, path):
        path = Path(path)
        if not path.is_absolute() or any(
            x in ("..", ".git", "static", "media", ".ssh", ".config", ".idea")
            or x.startswith(".env")
            for x in path.parts
        ):
            raise StorageFailure("unsafe_path")
        self.path = path
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in path.parts[1:]:
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                )
                os.close(fd)
                fd = child
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise StorageFailure("unsafe_permissions")
            self.fd = fd
            fd = None
        except OSError:
            raise StorageFailure("unsafe_storage") from None
        finally:
            if fd is not None:
                os.close(fd)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def names(self):
        return os.listdir(self.fd)

    def read(self, name, limit):
        name = _name(name)
        fd = None
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.fd)
            before = os.fstat(fd)
            if not _regular(before):
                raise StorageFailure("unsafe_artifact")
            if before.st_size > limit:
                raise StorageFailure("artifact_too_large")
            chunks = []
            size = 0
            while True:
                part = os.read(fd, min(65536, limit + 1 - size))
                if not part:
                    break
                chunks.append(part)
                size += len(part)
                if size > limit:
                    raise StorageFailure("artifact_too_large")
            after = os.fstat(fd)
            if (before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ) or size != before.st_size:
                raise StorageFailure("artifact_changed")
            return b"".join(chunks), before
        except FileNotFoundError:
            raise StorageFailure("artifact_missing") from None
        except OSError:
            raise StorageFailure("unsafe_artifact") from None
        finally:
            if fd is not None:
                os.close(fd)

    def unlink(self, name, expected):
        info = os.stat(_name(name), dir_fd=self.fd, follow_symlinks=False)
        if not _regular(info) or (info.st_dev, info.st_ino) != (
            expected.st_dev,
            expected.st_ino,
        ):
            raise StorageFailure("artifact_changed")
        os.unlink(name, dir_fd=self.fd)
        os.fsync(self.fd)

    def pending(self):
        try:
            os.mkdir(".pending", 0o700, dir_fd=self.fd)
            os.fsync(self.fd)
        except FileExistsError:
            pass
        return OwnedDirectory(self.path / ".pending")

    def atomic(self, name, data, *, replace=False):
        name = _name(name)
        fd = None
        reservation = None
        published = False
        temporary = ".part-" + uuid4().hex
        pending = self.pending()
        try:
            if replace:
                try:
                    self.read(name, max(FILE_LIMIT, INVENTORY_LIMIT * 2))
                except StorageFailure as exc:
                    if exc.code != "artifact_missing":
                        raise
            else:
                fd = os.open(
                    name,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=self.fd,
                )
                reservation = os.fstat(fd)
                os.close(fd)
                fd = None
            fd = os.open(
                temporary,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                0o600,
                dir_fd=pending.fd,
            )
            view = memoryview(data)
            while view:
                written = os.write(fd, view[:65536])
                if written <= 0:
                    raise StorageFailure("storage_write_failed")
                view = view[written:]
            os.fsync(fd)
            os.close(fd)
            fd = None
            if reservation:
                current = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
                if not _regular(current) or current.st_ino != reservation.st_ino:
                    raise StorageFailure("artifact_changed")
            os.replace(temporary, name, src_dir_fd=pending.fd, dst_dir_fd=self.fd)
            published = True
            os.fsync(self.fd)
            os.fsync(pending.fd)
        except OSError:
            raise StorageFailure("storage_write_failed") from None
        finally:
            if fd is not None:
                os.close(fd)
            try:
                os.unlink(temporary, dir_fd=pending.fd)
            except FileNotFoundError:
                pass
            if reservation and not published:
                try:
                    current = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
                    if _regular(current) and current.st_ino == reservation.st_ino:
                        os.unlink(name, dir_fd=self.fd)
                        os.fsync(self.fd)
                except FileNotFoundError:
                    pass
            pending.close()


class OperatorStorage:
    def __init__(self):
        self.backups = None
        self.inventory_dir = None
        key = os.environ.get("SCOPE_BACKUP_KEY", "")
        try:
            self.cipher = Fernet(key.encode())
        except (ValueError, TypeError):
            raise StorageFailure("backup_key_missing_or_invalid") from None
        root = settings.SCOPE_BACKUP_ROOT
        inventory = settings.SCOPE_DELETION_INVENTORY_PATH
        if not root or not inventory:
            raise StorageFailure("storage_not_configured")
        root_path = Path(root)
        inventory_path = Path(inventory)
        if (
            not inventory_path.is_absolute()
            or inventory_path == root_path
            or root_path in inventory_path.parents
        ):
            raise StorageFailure("inventory_must_be_independent")
        try:
            self.backups = OwnedDirectory(root_path)
            self.inventory_dir = OwnedDirectory(inventory_path.parent)
            self.inventory_name = _name(inventory_path.name)
            if (
                self.inventory_name.startswith(".part-")
                or self.inventory_name == ".scope-lock"
            ):
                raise StorageFailure("unsafe_path")
        except Exception:
            self.close()
            raise

    def close(self):
        if self.backups:
            self.backups.close()
        if self.inventory_dir:
            self.inventory_dir.close()

    @contextmanager
    def locked(self):
        fd = None
        try:
            fd = os.open(
                ".scope-lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
                dir_fd=self.inventory_dir.fd,
            )
            if not _regular(os.fstat(fd)):
                raise StorageFailure("unsafe_lock")
            deadline = time.monotonic() + 5
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise StorageFailure("storage_busy")
                    time.sleep(0.05)
            yield self
        except OSError:
            raise StorageFailure("storage_unavailable") from None
        finally:
            if fd is not None:
                os.close(fd)

    def _decrypt(self, token):
        try:
            return self.cipher.decrypt(token)
        except (InvalidToken, ValueError, TypeError):
            raise StorageFailure("artifact_authentication_failed") from None

    def load_inventory(self, namespace=None, *, minimum_sequence=0, fresh=False):
        token, _ = self.inventory_dir.read(self.inventory_name, INVENTORY_LIMIT * 2)
        plaintext = self._decrypt(token)
        if len(plaintext) > INVENTORY_LIMIT:
            raise StorageFailure("inventory_too_large")
        data = _object(plaintext)
        if (
            set(data) != {"format", "install_id", "sequence", "refreshed_at", "entries"}
            or data["format"] != "scope-deletions-v1"
            or type(data["sequence"]) is not int
            or data["sequence"] < minimum_sequence
            or type(data["refreshed_at"]) is not int
            or data["sequence"] < 0
            or data["refreshed_at"] < 0
            or type(data["entries"]) is not list
        ):
            raise StorageFailure("invalid_inventory")
        if namespace is not None and data["install_id"] != uuid_text(namespace):
            raise StorageFailure("inventory_namespace_mismatch")
        uuid_text(data["install_id"])
        if (
            data["refreshed_at"] > now_seconds()
            or fresh
            and now_seconds() - data["refreshed_at"] > 86400
        ):
            raise StorageFailure("inventory_stale")
        seen = set()
        for row in data["entries"]:
            if (
                type(row) is not dict
                or set(row)
                != {"project_uuid", "team_uuid", "receipt_uuid", "deleted_at"}
                or type(row["deleted_at"]) is not int
                or not 0 <= row["deleted_at"] <= data["refreshed_at"]
            ):
                raise StorageFailure("invalid_inventory")
            for field in ("project_uuid", "team_uuid", "receipt_uuid"):
                uuid_text(row[field])
            if row["project_uuid"] in seen:
                raise StorageFailure("invalid_inventory")
            seen.add(row["project_uuid"])
        return data

    def save_inventory(self, data):
        data = {**data, "refreshed_at": now_seconds()}
        raw = _json(data)
        if len(raw) > INVENTORY_LIMIT:
            raise StorageFailure("inventory_too_large")
        self.inventory_dir.atomic(
            self.inventory_name, self.cipher.encrypt(raw), replace=True
        )
        return data

    def initial_inventory(self, state, *, has_receipts):
        try:
            return self.load_inventory(
                state.install_id, minimum_sequence=state.inventory_sequence
            )
        except StorageFailure as exc:
            if (
                exc.code != "artifact_missing"
                or state.inventory_sequence
                or has_receipts
            ):
                raise
        return self.save_inventory(
            {
                "format": "scope-deletions-v1",
                "install_id": str(state.install_id),
                "sequence": 0,
                "entries": [],
            }
        )

    def record_deletion(self, inventory, receipt):
        for row in inventory["entries"]:
            if row["project_uuid"] == str(receipt.project_uuid):
                if row["team_uuid"] != str(receipt.team_uuid) or row[
                    "receipt_uuid"
                ] != str(receipt.pk):
                    raise StorageFailure("inventory_conflict")
                return self.save_inventory(inventory)
        return self.save_inventory(
            {
                **inventory,
                "sequence": inventory["sequence"] + 1,
                "entries": [
                    *inventory["entries"],
                    {
                        "project_uuid": str(receipt.project_uuid),
                        "team_uuid": str(receipt.team_uuid),
                        "receipt_uuid": str(receipt.pk),
                        "deleted_at": now_seconds(),
                    },
                ],
            }
        )

    def write_backup(self, namespace, sequence, dump, *, pg_version, server_version):
        if type(dump) is not bytes or len(dump) > DUMP_LIMIT:
            raise StorageFailure("dump_too_large")
        identifier = str(uuid4())
        created = now_seconds()
        metadata = {
            "format": "scope-db-v1",
            "backup_id": identifier,
            "install_id": uuid_text(namespace),
            "sequence": sequence,
            "created_at": created,
            "expires_at": created + RETENTION,
            "migration_leaf": LEAF,
            "pg_major": pg_version,
            "server_major": server_version,
            "size": len(dump),
            "sha256": hashlib.sha256(dump).hexdigest(),
        }
        header = _json(metadata)
        token = self.cipher.encrypt(struct.pack(">I", len(header)) + header + dump)
        if len(token) > FILE_LIMIT:
            raise StorageFailure("backup_too_large")
        self.backups.atomic("scope-" + identifier + ".fernet", token)
        return metadata

    def read_backup(self, identifier, *, allow_expired=False):
        token, info = self.backups.read(
            "scope-" + uuid_text(identifier) + ".fernet", FILE_LIMIT
        )
        data = self._decrypt(token)
        if len(data) < 4:
            raise StorageFailure("invalid_backup")
        length = struct.unpack(">I", data[:4])[0]
        if length > 16384 or len(data) < 4 + length:
            raise StorageFailure("invalid_backup")
        meta = _object(data[4 : 4 + length])
        dump = data[4 + length :]
        expected = {
            "format",
            "backup_id",
            "install_id",
            "sequence",
            "created_at",
            "expires_at",
            "migration_leaf",
            "pg_major",
            "server_major",
            "size",
            "sha256",
        }
        if (
            set(meta) != expected
            or meta["format"] != "scope-db-v1"
            or meta["backup_id"] != uuid_text(identifier)
            or meta["migration_leaf"] != LEAF
            or meta["pg_major"] != 17
            or meta["server_major"] != 17
        ):
            raise StorageFailure("backup_profile_mismatch")
        uuid_text(meta["install_id"])
        if (
            any(
                type(meta[k]) is not int
                for k in ["created_at", "expires_at", "sequence", "size"]
            )
            or meta["sequence"] < 0
            or meta["created_at"] > now_seconds()
            or meta["expires_at"] != meta["created_at"] + RETENTION
            or len(dump) > DUMP_LIMIT
            or len(dump) != meta["size"]
            or hashlib.sha256(dump).hexdigest() != meta["sha256"]
        ):
            raise StorageFailure("invalid_backup")
        if not allow_expired and now_seconds() >= meta["expires_at"]:
            raise StorageFailure("backup_expired")
        return meta, dump, info

    def prune(self, *, execute=False):
        result = {
            "expired": [],
            "unknown": [],
            "pending": [],
            "latest_created_at": None,
        }
        for name in self.backups.names():
            match = re.fullmatch(r"scope-([0-9a-f-]{36})\.fernet", name)
            if match:
                try:
                    meta, _, info = self.read_backup(match[1], allow_expired=True)
                except StorageFailure:
                    result["unknown"].append("invalid_owned_artifact")
                    continue
                result["latest_created_at"] = max(
                    result["latest_created_at"] or 0, meta["created_at"]
                )
                if now_seconds() >= meta["expires_at"]:
                    result["expired"].append(match[1])
                    if execute:
                        self.backups.unlink(name, info)
            elif name != ".pending":
                result["unknown"].append("unknown_artifact")
        for directory in (self.backups, self.inventory_dir):
            pending = directory.pending()
            try:
                for name in pending.names():
                    if not re.fullmatch(r"\.part-[0-9a-f]{32}", name):
                        result["unknown"].append("unknown_pending_artifact")
                        continue
                    try:
                        _, info = pending.read(name, FILE_LIMIT)
                        if now_seconds() - info.st_mtime >= 86400:
                            result["pending"].append(name)
                            if execute:
                                pending.unlink(name, info)
                    except StorageFailure:
                        result["unknown"].append("invalid_pending_artifact")
            finally:
                pending.close()
        return result
