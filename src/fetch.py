"""Download supplier files from URLs with a size cap and a basic private-network guard."""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

import httpx


def _public_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


async def fetch(url: str, max_bytes: int, allow_private: bool = False) -> tuple[str, bytes]:
    u = urlparse(url.strip())
    if u.scheme not in ('http', 'https') or not u.hostname:
        raise ValueError(f'not an http(s) URL: {url[:100]}')
    if not allow_private and not _public_host(u.hostname):
        raise ValueError(f'host {u.hostname} is not a public address')
    name = (u.path.rsplit('/', 1)[-1] or 'input')
    async with httpx.AsyncClient(follow_redirects=True, timeout=60, headers={'User-Agent': 'LatentLark-EUDR-Package/0.1'}) as c:
        async with c.stream('GET', url) as r:
            r.raise_for_status()
            cd = r.headers.get('content-disposition', '')
            if 'filename=' in cd:
                name = cd.split('filename=')[-1].strip('"; ')
            buf = bytearray()
            async for chunk in r.aiter_bytes():
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    raise ValueError(f'file is larger than the {max_bytes / 1e6:.0f} MB input cap')
    return name, bytes(buf)
