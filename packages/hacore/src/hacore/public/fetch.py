"""Download selected members of a remote ZIP archive with HTTP range requests.

IDEAL ships its hourly readings inside a 6.4 GB archive; the hourly members are about
160 MB. ``RangeFile`` is a read-only, seekable file over HTTP ranges, so ``zipfile``
reads the central directory and the chosen members only.

Integrity: the SHA-256 of the central directory (pinned in ``datasets.json``) fixes the
member list with sizes and CRC-32, and ``zipfile`` checks every member's CRC-32 while
extracting. A changed archive fails one or the other.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import urllib.request
import zipfile
from importlib.resources import files
from pathlib import Path
from typing import Any

CHUNK = 1 << 20  # bytes fetched at least per request
USER_AGENT = "hammond-validation (+https://github.com/xomtra/Xomtra)"


class RangeFile(io.RawIOBase):
    def __init__(self, url: str, size: int | None = None) -> None:
        self.url = url
        self.pos = 0
        self.requests = 0
        self.size = size if size is not None else self._head()
        self._buf_start = 0
        self._buf = b""

    def _open(self, headers: dict[str, str], method: str = "GET"):  # type: ignore[no-untyped-def]
        req = urllib.request.Request(  # noqa: S310 - https URL from datasets.json
            self.url, headers={"User-Agent": USER_AGENT, **headers}, method=method
        )
        self.requests += 1
        return urllib.request.urlopen(req, timeout=120)  # noqa: S310

    def _head(self) -> int:
        with self._open({}, "HEAD") as r:
            return int(r.headers["Content-Length"])

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self.pos, io.SEEK_END: self.size}[whence]
        self.pos = max(0, base + offset)
        return self.pos

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = self.size - self.pos
        n = min(n, self.size - self.pos)
        if n <= 0:
            return b""
        end = self.pos + n
        buf_end = self._buf_start + len(self._buf)
        if not (self._buf_start <= self.pos and end <= buf_end):
            fetch_end = min(self.size, max(end, self.pos + CHUNK)) - 1
            with self._open({"Range": f"bytes={self.pos}-{fetch_end}"}) as r:
                if r.status != 206:
                    raise OSError(f"server ignored the range request ({r.status})")
                self._buf = r.read()
            self._buf_start = self.pos
        off = self.pos - self._buf_start
        out = self._buf[off : off + n]
        self.pos += len(out)
        return out

    def readinto(self, b) -> int:  # type: ignore[no-untyped-def]
        data = self.read(len(b))
        b[: len(data)] = data
        return len(data)


def central_directory_sha256(zf: zipfile.ZipFile) -> str:
    """SHA-256 over the member list: name, sizes and CRC-32 of every member, in order."""
    h = hashlib.sha256()
    for info in zf.infolist():
        h.update(
            f"{info.filename}\t{info.compress_size}\t{info.file_size}\t{info.CRC:08x}\n".encode()
        )
    return h.hexdigest()


def extract(
    url: str, pattern: str, dest: Path, *, expected_sha256: str | None = None
) -> tuple[str, list[Path]]:
    """Members of the remote archive whose name matches ``pattern`` into ``dest``
    (flat). Returns the central directory hash and the files written; raises if it
    differs from ``expected_sha256``."""
    rx = re.compile(pattern)
    dest.mkdir(parents=True, exist_ok=True)
    src = RangeFile(url)
    with zipfile.ZipFile(io.BufferedReader(src, buffer_size=CHUNK)) as zf:
        digest = central_directory_sha256(zf)
        if expected_sha256 and digest != expected_sha256:
            raise ValueError(f"archive changed: member list {digest} != {expected_sha256}")
        written = []
        members = sorted(
            (i for i in zf.infolist() if rx.search(i.filename)), key=lambda i: i.header_offset
        )
        for info in members:
            out = dest / Path(info.filename).name
            with zf.open(info) as f:  # checks the CRC-32 at the end
                out.write_bytes(f.read())
            written.append(out)
    return digest, written


def datasets() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        files("hacore.public").joinpath("datasets.json").read_text(encoding="utf-8")
    )
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hacore.public.fetch")
    parser.add_argument("dataset", choices=["ideal"])
    parser.add_argument("dest", type=Path)
    args = parser.parse_args(argv)
    d = datasets()[args.dataset]
    digest, written = extract(
        d["url"], d["members"], args.dest, expected_sha256=d.get("member_list_sha256")
    )
    pinned = d.get("member_list_sha256")
    print(
        json.dumps(
            {
                "dataset": args.dataset,
                "member_list_sha256": digest,
                "pinned": pinned is not None,
                "files": len(written),
                "bytes": sum(p.stat().st_size for p in written),
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
