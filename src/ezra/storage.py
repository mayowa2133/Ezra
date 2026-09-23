"""Object storage abstraction. Keys are POSIX-style relative paths such as
"sources/12/episode.mp4"; the media domain never sees a filesystem path or a
bucket name. Backends: local filesystem (default) and any S3-compatible store
(MinIO in docker-compose, AWS S3, R2, ...).

Media tools (ffmpeg, whisper) need real files, so `local_path()` returns a
readable path: the file itself on local storage, a cached download on S3."""

from __future__ import annotations

import hashlib
import mimetypes
import shutil
from abc import ABC, abstractmethod
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

from .config import get_settings


class StorageError(RuntimeError):
    pass


def validate_key(key: str) -> str:
    """Reject absolute paths, traversal and odd characters (path-traversal guard)."""
    if not key or key.startswith("/") or "\\" in key or "\x00" in key:
        raise StorageError(f"invalid storage key: {key!r}")
    parts = PurePosixPath(key).parts
    if any(p in ("..", ".") for p in parts):
        raise StorageError(f"invalid storage key: {key!r}")
    return "/".join(parts)


class Storage(ABC):
    name: str

    @abstractmethod
    def put_file(self, key: str, path: Path, content_type: str | None = None) -> str: ...

    @abstractmethod
    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> str: ...

    @abstractmethod
    def local_path(self, key: str) -> Path: ...

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...

    @abstractmethod
    def size(self, key: str) -> int: ...

    @abstractmethod
    def list(self, prefix: str) -> Iterator[str]: ...

    def url(self, key: str) -> str | None:
        """A directly fetchable URL when the backend can mint one (presigned S3)."""
        return None


class LocalStorage(Storage):
    name = "local"

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = (self.root / validate_key(key)).resolve()
        if self.root not in p.parents and p != self.root:
            raise StorageError(f"key escapes storage root: {key!r}")
        return p

    def put_file(self, key: str, path: Path, content_type: str | None = None) -> str:
        dest = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if Path(path).resolve() != dest:
            tmp = dest.with_suffix(dest.suffix + ".part")
            shutil.copyfile(path, tmp)
            tmp.replace(dest)
        return validate_key(key)

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> str:
        dest = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(dest)
        return validate_key(key)

    def local_path(self, key: str) -> Path:
        p = self._path(key)
        if not p.exists():
            raise StorageError(f"no object {key!r}")
        return p

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def size(self, key: str) -> int:
        return self.local_path(key).stat().st_size

    def list(self, prefix: str) -> Iterator[str]:
        base = self._path(prefix) if prefix else self.root
        if base.is_file():
            yield validate_key(prefix)
            return
        for p in sorted(base.rglob("*")):
            if p.is_file() and not p.name.endswith(".part"):
                yield p.relative_to(self.root).as_posix()


class S3Storage(Storage):
    name = "s3"

    def __init__(self, bucket: str, endpoint: str | None, region: str,
                 access_key: str | None, secret_key: str | None, cache_dir: Path, client=None):
        import boto3

        self.bucket = bucket
        self.cache_dir = cache_dir
        self.client = client or boto3.client(
            "s3", endpoint_url=endpoint, region_name=region,
            aws_access_key_id=access_key, aws_secret_access_key=secret_key)
        try:
            self.client.head_bucket(Bucket=bucket)
        except Exception:
            self.client.create_bucket(Bucket=bucket)

    def put_file(self, key: str, path: Path, content_type: str | None = None) -> str:
        key = validate_key(key)
        extra = {"ContentType": content_type or mimetypes.guess_type(key)[0] or "application/octet-stream"}
        self.client.upload_file(str(path), self.bucket, key, ExtraArgs=extra)
        return key

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> str:
        key = validate_key(key)
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data,
                               ContentType=content_type or mimetypes.guess_type(key)[0] or "application/octet-stream")
        return key

    def local_path(self, key: str) -> Path:
        key = validate_key(key)
        dest = self.cache_dir / "s3" / key
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(dest.suffix + ".part")
            try:
                self.client.download_file(self.bucket, key, str(tmp))
            except Exception as e:
                raise StorageError(f"no object {key!r}: {e}") from e
            tmp.replace(dest)
        return dest

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=validate_key(key))
            return True
        except Exception:
            return False

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=validate_key(key))
        (self.cache_dir / "s3" / key).unlink(missing_ok=True)

    def size(self, key: str) -> int:
        return int(self.client.head_object(Bucket=self.bucket, Key=validate_key(key))["ContentLength"])

    def list(self, prefix: str) -> Iterator[str]:
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                yield obj["Key"]

    def url(self, key: str) -> str | None:
        return self.client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": validate_key(key)}, ExpiresIn=3600)


_storage: Storage | None = None
_storage_config: tuple[str, ...] = ()


def get_storage() -> Storage:
    """The configured backend, rebuilt only when the configuration changes."""
    global _storage, _storage_config
    s = get_settings()
    config = (s.storage, str(s.storage_dir), s.s3_bucket, s.s3_endpoint or "")
    if _storage is None or config != _storage_config:
        if s.storage == "s3":
            _storage = S3Storage(s.s3_bucket, s.s3_endpoint, s.s3_region, s.s3_access_key,
                                 s.s3_secret_key, s.cache_dir)
        else:
            _storage = LocalStorage(s.storage_dir)
        _storage_config = config
    return _storage


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
