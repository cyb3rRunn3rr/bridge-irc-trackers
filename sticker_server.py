"""Servidor HTTP local que entrega as figurinhas autenticadas do site."""

import asyncio
import contextlib
import logging
import re

log = logging.getLogger(__name__)
PATH = re.compile(r"/stickers/(\d{1,9})")


class StickerServer:
    def __init__(self, site, port, base_url=None):
        self.site = site
        self.port = port
        self.base_url = (base_url or f"http://127.0.0.1:{port}").rstrip("/")

    def url(self, sid):
        return f"{self.base_url}/stickers/{sid}"

    async def handle(self, reader, writer):
        try:
            request = await asyncio.wait_for(reader.readline(), 10)
            # Os headers do cliente não são usados; apenas descartados.
            for _ in range(100):
                header = await asyncio.wait_for(reader.readline(), 10)
                if header in (b"\r\n", b"\n", b""):
                    break
            method, path = (request.decode("latin-1").split() + ["", ""])[:2]
            match = PATH.fullmatch(path.partition("?")[0])
            image = None
            if method in ("GET", "HEAD") and match:
                try:
                    image = await self.site.sticker_image(int(match[1]))
                except Exception as exc:
                    log.warning("Falha ao obter figurinha (%s).", type(exc).__name__)
            if image:
                status, kind, body, cache = "200 OK", image[0], image[1], "max-age=86400"
            else:
                status, kind, cache = "404 Not Found", "text/plain; charset=utf-8", "no-store"
                body = "Figurinha indisponível.\n".encode()
            head = (
                f"HTTP/1.1 {status}\r\nContent-Type: {kind}\r\n"
                f"Content-Length: {len(body)}\r\nCache-Control: {cache}\r\n"
                "X-Content-Type-Options: nosniff\r\nConnection: close\r\n\r\n"
            )
            writer.write(head.encode() + (b"" if method == "HEAD" else body))
            await asyncio.wait_for(writer.drain(), 30)
        except (TimeoutError, ConnectionError, ValueError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
