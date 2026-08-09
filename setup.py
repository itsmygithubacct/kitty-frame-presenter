"""Build hooks that keep public archives readable under a private umask."""

from __future__ import annotations

import gzip
import os
import tarfile
from pathlib import Path

from setuptools import setup
from setuptools.command.bdist_wheel import bdist_wheel
from setuptools.command.build_py import build_py
from setuptools.command.sdist import sdist


def _make_public(root: Path) -> None:
    for path in (root, *root.rglob("*")):
        if path.is_symlink():
            continue
        mode = path.stat().st_mode
        if path.is_dir():
            path.chmod(mode | 0o055)
        elif path.is_file():
            path.chmod(mode | 0o044)


def _source_date_epoch() -> int | None:
    value = os.environ.get("SOURCE_DATE_EPOCH")
    if value is None:
        return None
    try:
        epoch = int(value)
    except ValueError as exc:
        raise ValueError("SOURCE_DATE_EPOCH must be a non-negative integer") from exc
    if epoch < 0:
        raise ValueError("SOURCE_DATE_EPOCH must be a non-negative integer")
    return epoch


class PublicBuildPy(build_py):
    """Normalize package files copied from a mode-0600 source checkout."""

    def run(self) -> None:
        super().run()
        _make_public(Path(self.build_lib))


class PublicBdistWheel(bdist_wheel):
    """Normalize package metadata added after ``build_py`` completes."""

    def write_wheelfile(self, wheelfile_base: str, generator: str = "") -> None:
        if generator:
            super().write_wheelfile(wheelfile_base, generator)
        else:
            super().write_wheelfile(wheelfile_base)
        _make_public(Path(self.bdist_dir))


class PublicSdist(sdist):
    """Normalize every source-archive member before it is serialized."""

    def make_release_tree(self, base_dir: str, files: list[str]) -> None:
        super().make_release_tree(base_dir, files)
        _make_public(Path(base_dir))

    def make_archive(
        self,
        base_name: str,
        format: str,
        root_dir: str | None = None,
        base_dir: str | None = None,
        owner: str | None = None,
        group: str | None = None,
    ) -> str:
        epoch = _source_date_epoch()
        if (
            format != "gztar"
            or epoch is None
            or self.dry_run
            or owner is not None
            or group is not None
        ):
            return super().make_archive(
                base_name, format, root_dir, base_dir, owner, group
            )

        archive_name = base_name + ".tar.gz"
        archive_path = Path(archive_name)
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        source = Path(root_dir or ".") / (base_dir or "")
        archive_root = base_dir or source.name

        def normalized(info: tarfile.TarInfo) -> tarfile.TarInfo:
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mtime = epoch
            info.mode |= 0o055 if info.isdir() else 0o044
            return info

        with (
            archive_path.open("wb") as raw,
            gzip.GzipFile(fileobj=raw, mode="wb", mtime=epoch) as compressed,
            tarfile.open(
                fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT
            ) as archive,
        ):
            archive.add(source, arcname=archive_root, filter=normalized)
        return archive_name


previous_umask = os.umask(0o022)
try:
    setup(
        cmdclass={
            "bdist_wheel": PublicBdistWheel,
            "build_py": PublicBuildPy,
            "sdist": PublicSdist,
        }
    )
finally:
    os.umask(previous_umask)
