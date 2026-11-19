"""The download of a group file from QQ's file host: streamed to a local file, its SHA-256
worked out on the way, checked against the size QQ announced. A download that broke off
continues from its part with a range request.

Only `https` addresses on a host under `qq.com` are fetched, and a redirect is followed only
to such an address: the service never fetches an address it has no reason to trust. An
address is never logged (it needs no login to fetch).
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

MAX_REDIRECTS = 5


class GroupFileDownloadFailed(Exception):
    """The file could not be downloaded; the message says why, in Chinese, without the address."""


class AddressRefused(GroupFileDownloadFailed):
    """The address (or one it redirects to) is not QQ's: trying again cannot help."""


def qq_address(url: str) -> str:
    """`url` when it may be fetched: https, on `qq.com` or a host under it. Raises
    AddressRefused otherwise."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not (host == "qq.com" or host.endswith(".qq.com")):
        raise AddressRefused("下载地址不是 QQ 的 https 地址，没有下载")
    return url


async def fetch(client: httpx.AsyncClient, url: str, size: int, local: Path,
                progress: Callable[[int], None] | None = None) -> str:
    """Download `url` into `local` and return its SHA-256 (hex). The result must be `size`
    bytes. A `local` that is part of the file already (a download that broke off) is
    continued with a range request; when the host answers with the whole file instead, it
    starts over. A download that breaks off leaves `local` for the next try; one of the
    wrong size removes it. `progress(bytes so far)` as it comes. Raises
    GroupFileDownloadFailed (AddressRefused for an address that is not QQ's)."""
    address = qq_address(url)
    have = local.stat().st_size if local.is_file() else 0
    if not 0 < have < size:  # nothing to continue (or more than the file: start over)
        have = 0
    headers = {"Range": f"bytes={have}-"} if have else {}
    try:
        for _ in range(MAX_REDIRECTS + 1):
            async with client.stream("GET", address, headers=headers, follow_redirects=False) as r:
                if r.is_redirect:
                    address = qq_address(urljoin(address, r.headers.get("location", "")))
                    continue
                if have and r.status_code == 206:
                    if not r.headers.get("content-range", "").startswith(f"bytes {have}-"):
                        local.unlink(missing_ok=True)  # an answer for another range: start over next time
                        raise GroupFileDownloadFailed("QQ 的文件服务器续传的位置不对，下次从头下载")
                    digest, got, mode = await asyncio.to_thread(sha256_of, local), have, "ab"
                elif r.status_code == 200:
                    digest, got, mode = hashlib.sha256(), 0, "wb"  # the whole file: from the start
                else:
                    raise GroupFileDownloadFailed(f"QQ 的文件服务器回答 HTTP {r.status_code}")
                local.parent.mkdir(parents=True, exist_ok=True)
                with local.open(mode) as out:
                    async for chunk in r.aiter_bytes():
                        out.write(chunk)
                        digest.update(chunk)
                        got += len(chunk)
                        if progress:
                            progress(got)
                if got != size:
                    local.unlink(missing_ok=True)  # not this file: the next try starts over
                    raise GroupFileDownloadFailed(f"下载到的文件大小不对：{got} 字节，QQ 说是 {size} 字节")
                return digest.hexdigest()
    except httpx.HTTPError as e:
        raise GroupFileDownloadFailed(f"下载中断：{type(e).__name__}") from None
    raise GroupFileDownloadFailed("QQ 的文件服务器转址太多次")


def sha256_of(path: Path) -> hashlib._Hash:
    """The SHA-256 of what `path` holds so far, to go on with."""
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest
