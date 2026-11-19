"""The download of a group file from QQ's file host: streamed to a local file, its SHA-256
worked out on the way, checked against the size QQ announced.

Only `https` addresses on a host under `qq.com` are fetched, and a redirect is followed only
to such an address: the service never fetches an address it has no reason to trust. An
address is never logged (it needs no login to fetch).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

MAX_REDIRECTS = 5


class GroupFileDownloadFailed(Exception):
    """The file could not be downloaded; the message says why, in Chinese, without the address."""


def qq_address(url: str) -> str:
    """`url` when it may be fetched: https, on `qq.com` or a host under it. Raises
    GroupFileDownloadFailed otherwise."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not (host == "qq.com" or host.endswith(".qq.com")):
        raise GroupFileDownloadFailed("下载地址不是 QQ 的 https 地址，没有下载")
    return url


async def fetch(client: httpx.AsyncClient, url: str, size: int, local: Path,
                progress: Callable[[int], None] | None = None) -> str:
    """Download `url` into `local` (made or overwritten) and return its SHA-256 (hex). The
    result must be `size` bytes. `progress(bytes so far)` as it comes. Raises
    GroupFileDownloadFailed."""
    address = qq_address(url)
    try:
        for _ in range(MAX_REDIRECTS + 1):
            async with client.stream("GET", address, follow_redirects=False) as r:
                if r.is_redirect:
                    address = qq_address(urljoin(address, r.headers.get("location", "")))
                    continue
                if r.status_code != 200:
                    raise GroupFileDownloadFailed(f"QQ 的文件服务器回答 HTTP {r.status_code}")
                digest, got = hashlib.sha256(), 0
                local.parent.mkdir(parents=True, exist_ok=True)
                with local.open("wb") as out:
                    async for chunk in r.aiter_bytes():
                        out.write(chunk)
                        digest.update(chunk)
                        got += len(chunk)
                        if progress:
                            progress(got)
                if got != size:
                    raise GroupFileDownloadFailed(f"下载到的文件大小不对：{got} 字节，QQ 说是 {size} 字节")
                return digest.hexdigest()
    except httpx.HTTPError as e:
        raise GroupFileDownloadFailed(f"下载中断：{type(e).__name__}") from None
    raise GroupFileDownloadFailed("QQ 的文件服务器转址太多次")
