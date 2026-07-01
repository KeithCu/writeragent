# WriterAgent - adapted from Pooch v1.8.2 pooch/processors.py (BSD-3-Clause), pruned.
from __future__ import annotations

import abc
import os
from tarfile import TarFile
from zipfile import ZipFile

from plugin.contrib.pooch.utils import get_logger


def _safe_archive_member(name: str) -> bool:
    normalized = os.path.normpath(name)
    return not normalized.startswith("/") and ".." not in normalized.split(os.sep)


class ExtractorProcessor(abc.ABC):
    def __init__(self, members=None, extract_dir=None) -> None:
        self.members = members
        self.extract_dir = extract_dir

    @property
    @abc.abstractmethod
    def suffix(self) -> str:
        raise NotImplementedError

    @abc.abstractmethod
    def _extract_file(self, fname: str, extract_dir: str) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    def _all_members(self, fname: str) -> list[str]:
        raise NotImplementedError

    def __call__(self, fname: str, action: str, pooch) -> list[str]:
        del pooch
        if self.extract_dir is None:
            extract_dir = fname + self.suffix
        else:
            archive_dir = fname.rsplit(os.path.sep, maxsplit=1)[0]
            extract_dir = os.path.join(archive_dir, self.extract_dir)

        members = self._all_members(fname) if not self.members else list(self.members)
        if (
            action in ("update", "download")
            or not os.path.exists(extract_dir)
            or not all(os.path.exists(os.path.join(extract_dir, m)) for m in members)
        ):
            os.makedirs(extract_dir, exist_ok=True)
            self._extract_file(fname, extract_dir)

        fnames: list[str] = []
        for path, _, files in os.walk(extract_dir):
            for filename in files:
                relpath = os.path.normpath(os.path.join(os.path.relpath(path, extract_dir), filename))
                if self.members is None or any(relpath.startswith(os.path.normpath(m)) for m in self.members):
                    fnames.append(os.path.join(path, filename))
        return fnames


class Unzip(ExtractorProcessor):
    @property
    def suffix(self) -> str:
        return ".unzip"

    def _all_members(self, fname: str) -> list[str]:
        with ZipFile(fname, "r") as archive:
            return [info.filename for info in archive.infolist() if not info.is_dir()]

    def _extract_file(self, fname: str, extract_dir: str) -> None:
        with ZipFile(fname, "r") as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                if not _safe_archive_member(info.filename):
                    raise RuntimeError(f"Unsafe zip member path: {info.filename!r}")
                if self.members is not None and info.filename not in self.members:
                    continue
                get_logger().info("Extracting %r from %r to %r", info.filename, fname, extract_dir)
                target = os.path.join(extract_dir, info.filename)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with archive.open(info) as src, open(target, "wb") as dst:
                    dst.write(src.read())


class Untar(ExtractorProcessor):
    @property
    def suffix(self) -> str:
        return ".untar"

    def _all_members(self, fname: str) -> list[str]:
        with TarFile.open(fname, "r:*") as archive:
            return [info.name for info in archive.getmembers() if info.isfile()]

    def _extract_file(self, fname: str, extract_dir: str) -> None:
        with TarFile.open(fname, "r:*") as archive:
            if self.members is None:
                for member in archive.getmembers():
                    if not member.isfile() or not _safe_archive_member(member.name):
                        if member.isfile() and not _safe_archive_member(member.name):
                            raise RuntimeError(f"Unsafe tar member path: {member.name!r}")
                        continue
                get_logger().info("Untarring contents of %r to %r", fname, extract_dir)
                for member in archive.getmembers():
                    if member.isfile() and _safe_archive_member(member.name):
                        extracted = archive.extractfile(member)
                        if extracted:
                            target = os.path.join(extract_dir, member.name)
                            os.makedirs(os.path.dirname(target), exist_ok=True)
                            with open(target, "wb") as dst:
                                dst.write(extracted.read())
                return

            for member_name in self.members:
                if not _safe_archive_member(member_name):
                    raise RuntimeError(f"Unsafe tar member path: {member_name!r}")
                subdir_members = [info for info in archive.getmembers() if os.path.normpath(info.name).startswith(os.path.normpath(member_name))]
                get_logger().info("Extracting %r from %r to %r", member_name, fname, extract_dir)
                for member in subdir_members:
                    if member.isfile() and _safe_archive_member(member.name):
                        extracted = archive.extractfile(member)
                        if extracted:
                            target = os.path.join(extract_dir, member.name)
                            os.makedirs(os.path.dirname(target), exist_ok=True)
                            with open(target, "wb") as dst:
                                dst.write(extracted.read())
