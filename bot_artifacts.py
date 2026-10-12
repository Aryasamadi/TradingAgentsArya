"""Encrypted packaging for private runtime state and report artifacts.

Files inside each encrypted archive are preserved byte-for-byte. This module
never edits agent output, report.json, or report.pdf; it only wraps/unwraps them.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import os
import pathlib
import shutil
import zipfile
from typing import Iterable

from nacl.secret import SecretBox


class ArtifactEncryptionError(RuntimeError):
    """An encrypted runtime artifact could not be processed safely."""


def _box(state_key: str) -> SecretBox:
    key_text = (state_key or "").strip()
    if not key_text:
        raise ArtifactEncryptionError("BOT_STATE_KEY is required; refusing plaintext artifact handling.")
    return SecretBox(hashlib.sha256(key_text.encode("utf-8")).digest())


def encrypt_file(source: str | pathlib.Path, destination: str | pathlib.Path, state_key: str) -> None:
    src = pathlib.Path(source)
    dst = pathlib.Path(destination)
    if not src.is_file():
        raise ArtifactEncryptionError(f"Source file not found: {src}")
    encrypted = bytes(_box(state_key).encrypt(src.read_bytes()))
    dst.parent.mkdir(parents=True, exist_ok=True)
    temp = dst.with_name(dst.name + ".tmp")
    with temp.open("wb") as handle:
        handle.write(encrypted)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, dst)
    src.unlink()


def decrypt_file(source: str | pathlib.Path, destination: str | pathlib.Path, state_key: str) -> None:
    src = pathlib.Path(source)
    dst = pathlib.Path(destination)
    try:
        plain = _box(state_key).decrypt(src.read_bytes())
    except Exception as exc:
        raise ArtifactEncryptionError("Encrypted file could not be decrypted with BOT_STATE_KEY.") from exc
    dst.parent.mkdir(parents=True, exist_ok=True)
    temp = dst.with_name(dst.name + ".tmp")
    with temp.open("wb") as handle:
        handle.write(plain)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, dst)


def _files_under(root: pathlib.Path) -> Iterable[pathlib.Path]:
    resolved_root = root.resolve()
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ArtifactEncryptionError("Symlinks are not allowed in runtime artifacts.")
        if path.is_file():
            resolved = path.resolve()
            if resolved_root not in resolved.parents:
                raise ArtifactEncryptionError("Artifact path escapes its runtime directory.")
            yield path


def encrypt_tree(directory: str | pathlib.Path, marker: str, state_key: str) -> None:
    root = pathlib.Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    marker_path = root / marker
    buf = io.BytesIO()
    try:
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in _files_under(root):
                if path == marker_path:
                    continue
                archive.write(path, path.relative_to(root).as_posix())
        encrypted = bytes(_box(state_key).encrypt(buf.getvalue()))
    except ArtifactEncryptionError:
        raise
    except Exception as exc:
        raise ArtifactEncryptionError(f"Could not package runtime directory {root}.") from exc

    temp = root.parent / f".{root.name}.{marker}.tmp"
    with temp.open("wb") as handle:
        handle.write(encrypted)
        handle.flush()
        os.fsync(handle.fileno())

    for child in list(root.iterdir()):
        if child.is_dir():
            shutil.rmtree(child)
        elif child != marker_path:
            child.unlink()
    os.replace(temp, marker_path)


def decrypt_tree(directory: str | pathlib.Path, marker: str, state_key: str) -> None:
    root = pathlib.Path(directory).resolve()
    marker_path = root / marker
    try:
        plain = _box(state_key).decrypt(marker_path.read_bytes())
        with zipfile.ZipFile(io.BytesIO(plain)) as archive:
            members = archive.infolist()
            for member in members:
                member_path = pathlib.PurePosixPath(member.filename)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise ArtifactEncryptionError("Invalid path in encrypted runtime archive.")
            for member in members:
                member_path = pathlib.PurePosixPath(member.filename)
                destination = root.joinpath(*member_path.parts)
                if member.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("wb") as handle:
                    handle.write(archive.read(member))
    except ArtifactEncryptionError:
        raise
    except Exception as exc:
        raise ArtifactEncryptionError("Encrypted runtime archive could not be decrypted or extracted.") from exc
    marker_path.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    encrypt_file_parser = sub.add_parser("encrypt-file")
    encrypt_file_parser.add_argument("source")
    encrypt_file_parser.add_argument("destination")
    decrypt_file_parser = sub.add_parser("decrypt-file")
    decrypt_file_parser.add_argument("source")
    decrypt_file_parser.add_argument("destination")
    for name in ("encrypt-tree", "decrypt-tree"):
        command = sub.add_parser(name)
        command.add_argument("directory")
        command.add_argument("marker")
    parser.add_argument("--state-key", default=os.environ.get("BOT_STATE_KEY", ""))
    args = parser.parse_args()

    if args.operation == "encrypt-file":
        encrypt_file(args.source, args.destination, args.state_key)
    elif args.operation == "decrypt-file":
        decrypt_file(args.source, args.destination, args.state_key)
    elif args.operation == "encrypt-tree":
        encrypt_tree(args.directory, args.marker, args.state_key)
    else:
        decrypt_tree(args.directory, args.marker, args.state_key)


if __name__ == "__main__":
    main()
