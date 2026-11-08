"""Cliente dos chats UNIT3D usados pelos bridges de trackers."""

import asyncio
import http.cookiejar
import json
import logging
import re
import time
import unicodedata
from collections import OrderedDict
from urllib.parse import unquote, urljoin

import httpx
import socketio
from bs4 import BeautifulSoup

from message_formatting import html_to_irc

log = logging.getLogger(__name__)

STICKER_TYPES = {"image/png", "image/webp", "image/jpeg", "image/gif"}
STICKER_MAX_BYTES = 5 * 1024 * 1024


def text_from_html(raw, base_url):
    return html_to_irc(raw, base_url, styled=False)


def sticker_fold(value):
    value = unicodedata.normalize("NFKD", str(value))
    value = "".join(c for c in value if not unicodedata.combining(c))
    return " ".join(value.casefold().split())


def load_sticker_names(path):
    """Nomes locais das figurinhas: {"ID": "nome"}; o site não é alterado."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} deve conter um objeto JSON.")
    names = {}
    for key, value in data.items():
        if not str(key).isdigit() or not isinstance(value, str):
            raise ValueError(f"{path.name}: use \"ID\": \"nome\" ({key!r}).")
        value = " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", value).split())
        if value:
            names[int(key)] = value
    return names


def parse_user(page):
    soup = BeautifulSoup(page, "html.parser")
    chat = soup.find(id="chatbody")
    data = chat.get("x-data", "") if chat else ""
    match = re.search(r"JSON\.parse\('((?:\\.|[^'])*)'\)", data)
    if not match:
        raise RuntimeError(
            "Página sem chat autenticado; exporte cookies novos do site."
        )
    # A string JavaScript contém JSON com aspas codificadas como \\u0022.
    encoded = match[1].replace("\\'", "'")
    user = json.loads(json.loads('"' + encoded.replace('"', '\\"') + '"'))
    csrf = soup.find("meta", attrs={"name": "csrf-token"})
    if not csrf or not user.get("id"):
        raise RuntimeError("Não foi possível obter usuário e CSRF do chat.")
    return user, csrf["content"]


class SiteChat:
    def __init__(self, cookies, room=1, base_url="https://capybarabr.com", sticker_names=None):
        self.base_url = base_url.rstrip("/")
        self.room = room
        self.channel = f"presence-chatroom.{room}"
        self.private_channel = None
        self.jar = http.cookiejar.MozillaCookieJar(str(cookies))
        self.jar.load(ignore_discard=True, ignore_expires=True)
        self.http = httpx.AsyncClient(
            base_url=self.base_url,
            cookies=self.jar,
            timeout=30,
            follow_redirects=False,
            headers={
                "User-Agent": "Mozilla/5.0 UNIT3D-IRC-Bridge/1.0",
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "Origin": self.base_url,
                "Referer": self.base_url + "/",
            },
        )
        self.user = {}
        self.ws_host = ""
        self.csrf_headers = {}
        self.history = OrderedDict()
        self.seen = OrderedDict()
        self.members = {}
        self.connected = asyncio.Event()
        self.on_message = self.on_presence = self.on_notice = None
        self.on_reaction = None
        self.on_private = None
        # Mensagem fixada na sala (só moderadores fixam no site).
        self.pinned = None
        self.on_pin = None
        self.private_history = OrderedDict()
        # Conversas privadas conhecidas: ID do outro usuário -> usuário.
        self.targets = {}
        self.on_user = None
        self.on_typing = None
        self.typing_received = OrderedDict()
        self.typing_sent_at = None
        self.typing_lock = asyncio.Lock()
        self.sio = None
        self.refresh_lock = asyncio.Lock()
        self.send_lock = asyncio.Lock()
        self.sending = None
        self.reaction_lock = asyncio.Lock()
        self.stickers = {}
        self.sticker_names = sticker_names or {}
        self.stickers_loaded = None
        self.sticker_cache = OrderedDict()

    async def refresh(self):
        async with self.refresh_lock:
            response = await self.http.get("/")
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            self.csrf_headers = {}
            self.http.headers.pop("X-CSRF-TOKEN", None)
            self.http.headers.pop("X-XSRF-TOKEN", None)
            csrf = soup.find("meta", attrs={"name": "csrf-token"})
            if csrf and csrf.get("content"):
                self.http.headers["X-CSRF-TOKEN"] = csrf["content"]
                self.csrf_headers = {"X-CSRF-TOKEN": csrf["content"]}
            else:
                xsrf = self.http.cookies.get("XSRF-TOKEN")
                if xsrf:
                    xsrf = unquote(xsrf)
                    self.http.headers.pop("X-CSRF-TOKEN", None)
                    self.http.headers["X-XSRF-TOKEN"] = xsrf
                    self.csrf_headers = {"X-XSRF-TOKEN": xsrf}

            try:
                self.user, _ = parse_user(response.text)
            except RuntimeError:
                self.user = await self.user_from_echoes()

            if not self.ws_host:
                script = next(
                    (
                        s["src"]
                        for s in soup.find_all("script", src=True)
                        if "/chat-" in s["src"]
                    ),
                    None,
                )
                if script:
                    response = await self.http.get(urljoin(self.base_url, script))
                    response.raise_for_status()
                    match = re.search(
                        r'broadcaster:"socket\.io",host:"([^"]+)"', response.text
                    )
                    if match:
                        self.ws_host = match[1]
                # Algumas instalações UNIT3D servem o chat por Socket.IO no
                # próprio domínio e não expõem o bundle de configuração no HTML.
                if not self.ws_host:
                    self.ws_host = self.base_url

    async def user_from_echoes(self):
        """Obter a conta autenticada pela API comum do chat UNIT3D."""
        response = await self.http.get("/api/chat/echoes")
        response.raise_for_status()
        try:
            rows = response.json().get("data", [])
        except (ValueError, AttributeError):
            rows = []
        for row in rows:
            user = row.get("user") if isinstance(row, dict) else None
            if isinstance(user, dict) and user.get("id") and user.get("username"):
                return user
        raise RuntimeError(
            "Não consegui identificar a conta pela API do chat; confira os cookies."
        )

    async def load_room(self, deliver=True):
        rooms = (await self.request("GET", "/api/chat/rooms"))["data"]
        room = next((r for r in rooms if int(r["id"]) == self.room), None)
        if room is None:
            raise RuntimeError(f"Sala {self.room} inexistente.")
        self.room_name = room["name"]
        await self.set_pin(room.get("pinned"), deliver)

    async def set_pin(self, pinned, deliver=True):
        pinned = pinned if isinstance(pinned, dict) and pinned.get("id") else None
        key = lambda p: p and (p.get("id"), p.get("pinned_at"))
        if key(pinned) == key(self.pinned):
            return
        self.pinned = pinned
        if deliver and self.on_pin:
            await self.on_pin(pinned)

    async def request(self, method, path, **kwargs):
        response = await self.http.request(method, path, **kwargs)
        if response.status_code in (401, 403, 419):
            await self.refresh()
            response = await self.http.request(method, path, **kwargs)
        response.raise_for_status()
        if "application/json" not in response.headers.get("content-type", ""):
            raise RuntimeError("API não retornou JSON; confira a sessão/cookies.")
        return response.json()

    async def initialize(self):
        await self.refresh()
        await self.load_room(deliver=False)
        try:
            await self.load_stickers()
        except Exception as exc:
            log.warning("Figurinhas indisponíveis (%s).", type(exc).__name__)
        await self.reconcile(deliver=False)
        try:
            await self.reconcile_private(deliver=False)
        except Exception as exc:
            log.warning("Mensagens privadas indisponíveis (%s).", type(exc).__name__)

    async def load_stickers(self):
        config = await self.request("GET", "/api/chat/config")
        stickers = {}
        for pack in config.get("stickers") or []:
            for sticker in pack.get("stickers") or []:
                if isinstance(sticker, dict) and str(sticker.get("id")).isdigit():
                    sid = int(sticker["id"])
                    site_name = str(sticker.get("name") or sid)
                    stickers[sid] = {
                        "id": sid,
                        "name": self.sticker_names.get(sid, site_name),
                        "site_name": site_name,
                        "pack": str(pack.get("name") or ""),
                    }
        self.stickers = stickers
        self.stickers_loaded = time.monotonic()

    def sticker_id(self, url):
        match = re.fullmatch(
            re.escape(self.base_url) + r"/authenticated-images/stickers/(\d+)",
            str(url or ""),
        )
        return int(match[1]) if match else None

    async def sticker_matches(self, query=""):
        """Figurinhas por ID, nome exato ou trecho do nome, sem acentos."""

        def find():
            wanted = sticker_fold(query)
            if wanted.isdigit():
                return [self.stickers[int(wanted)]] if int(wanted) in self.stickers else []
            # O nome local e o original do site são aceitos.
            names = [
                (sticker_fold(name), s)
                for s in self.stickers.values()
                for name in {s["name"], s.get("site_name", s["name"])}
            ]
            found = [s for name, s in names if name == wanted] or [
                s for name, s in names if wanted in name
            ]
            return list({s["id"]: s for s in found}.values())

        matches = find()
        # Recarregar ao não encontrar: novos pacotes podem ter sido publicados.
        stale = self.stickers_loaded is None or time.monotonic() - self.stickers_loaded > 60
        if not matches and stale:
            try:
                await self.load_stickers()
            except Exception as exc:
                log.warning("Falha ao atualizar figurinhas (%s).", type(exc).__name__)
            matches = find()
        return matches

    async def sticker_image(self, sid):
        if sid in self.sticker_cache:
            self.sticker_cache.move_to_end(sid)
            return self.sticker_cache[sid]
        response = await self.http.get(
            f"/authenticated-images/stickers/{sid}", headers={"Accept": "image/*"}
        )
        kind = response.headers.get("content-type", "").partition(";")[0].strip().lower()
        if (
            response.status_code != 200
            or kind not in STICKER_TYPES
            or len(response.content) > STICKER_MAX_BYTES
        ):
            return None
        self.sticker_cache[sid] = (kind, response.content)
        while len(self.sticker_cache) > 64:
            self.sticker_cache.popitem(last=False)
        return self.sticker_cache[sid]

    @staticmethod
    def is_private(message):
        return bool(message.get("receiver")) and not message.get("bot")

    def peer(self, message):
        """O outro participante de uma mensagem privada."""
        user = message.get("user") or {}
        if user.get("id") == self.user.get("id"):
            return message.get("receiver") or {}
        return user

    async def ingest(self, message, deliver=True, origin=None):
        if not isinstance(message, dict) or not message.get("id"):
            return
        if self.is_private(message):
            await self.ingest_private(message, deliver, origin)
            return
        mid = int(message["id"])
        # O evento WS pode chegar antes da resposta ao POST. Esperar a resposta
        # permite identificar qual cliente originou o envio e evitar eco duplo.
        if (
            origin is None
            and self.sending
            and (message.get("user") or {}).get("id") == self.user.get("id")
        ):
            await self.sending.wait()
        if mid in self.seen:
            cached = self.history.get(mid)
            user = message.get('user')
            if cached is not None and isinstance(user, dict):
                previous = cached.get('user') or {}
                cached['user'] = user
                if user.get('id') in self.members and 'group' in user:
                    self.members[user['id']]['group'] = user['group']
                if deliver and self.on_user and previous.get('group') != user.get('group'):
                    await self.on_user(user)
            await self.update_reactions(message, deliver)
            return
        self.seen[mid] = None
        self.history[mid] = message
        self.history = OrderedDict(sorted(self.history.items()))
        while len(self.seen) > 2000:
            self.seen.popitem(last=False)
        while len(self.history) > 100:
            self.history.popitem(last=False)
        if deliver and self.on_message:
            await self.on_message(message, origin)

    async def ingest_private(self, message, deliver=True, origin=None):
        mid = int(message["id"])
        if (
            origin is None
            and self.sending
            and (message.get("user") or {}).get("id") == self.user.get("id")
        ):
            await self.sending.wait()
        if mid in self.seen:
            return
        self.seen[mid] = None
        self.private_history[mid] = message
        self.private_history = OrderedDict(sorted(self.private_history.items()))
        while len(self.seen) > 2000:
            self.seen.popitem(last=False)
        while len(self.private_history) > 200:
            self.private_history.popitem(last=False)
        peer = self.peer(message)
        if peer.get("id"):
            self.targets[int(peer["id"])] = peer
        if deliver and self.on_private:
            await self.on_private(message, origin)

    async def reconcile_private(self, deliver=True):
        echoes = (await self.request("GET", "/api/chat/echoes"))["data"]
        for echo in echoes:
            target = echo.get("target") if isinstance(echo, dict) else None
            if isinstance(target, dict) and target.get("id"):
                self.targets.setdefault(int(target["id"]), target)
        for tid in list(self.targets)[:20]:
            messages = (
                await self.request("GET", f"/api/chat/private/messages/{tid}")
            )["data"]
            for message in sorted(messages, key=lambda m: int(m["id"])):
                await self.ingest(message, deliver)

    @staticmethod
    def reaction_users(message):
        return {
            (r["emoji"], name)
            for r in (message.get("reactions") or [])
            for name in r.get("users", [])
            if r.get("emoji")
        }

    async def update_reactions(self, message, deliver=True):
        if not isinstance(message, dict) or not message.get("id"):
            return
        mid = int(message["id"])
        existing = self.history.get(mid)
        if existing is None or "reactions" not in message:
            return
        before = self.reaction_users(existing)
        after = self.reaction_users(message)
        self.history[mid] = {**existing, "reactions": message.get("reactions") or []}
        if deliver and self.on_reaction and before != after:
            await self.on_reaction(mid, after - before, before - after)

    async def set_reaction(self, mid, emoji, remove=False):
        # A API alterna a reação. Consultar o estado antes do POST evita que
        # um pedido repetido de adicionar acabe removendo a reação existente.
        async with self.reaction_lock:
            await self.reconcile()
            message = self.history.get(mid)
            if message is None:
                raise ValueError("Mensagem fora do histórico disponível.")
            present = (emoji, self.user["username"]) in self.reaction_users(message)
            if present == (not remove):
                return
            data = await self.request(
                "POST", f"/api/chat/message/{mid}/reaction", json={"emoji": emoji}
            )
            await self.update_reactions(data.get("data"))

    async def reconcile(self, deliver=True):
        messages = (await self.request("GET", f"/api/chat/messages/{self.room}"))[
            "data"
        ]
        for message in sorted(messages, key=lambda m: int(m["id"])):
            await self.ingest(message, deliver)

    async def send(self, text, origin=None, reply_to=None, receiver=None):
        # Não repetir POST em timeout: a mensagem pode já ter sido publicada.
        async with self.send_lock:
            event = asyncio.Event()
            self.sending = event if origin is not None else None
            try:
                data = await self.request(
                    "POST",
                    "/api/chat/messages",
                    json={
                        "user_id": self.user["id"],
                        "chatroom_id": self.room,
                        "message": text,
                        "receiver_id": receiver,
                        "bot_id": None,
                        "targeted": receiver or 0,
                        "reply_to_id": reply_to,
                    },
                )
                message = data.get("data")
                if isinstance(message, dict) and message.get("id"):
                    if reply_to is not None:
                        message["_reply_to"] = reply_to
                    await self.ingest(message, origin=origin)
            finally:
                self.sending = None
                event.set()
            if not isinstance(message, dict) or not message.get("id"):
                await (self.reconcile_private() if receiver else self.reconcile())

    def cookie_header(self):
        request = self.http.build_request("GET", self.base_url + "/")
        return request.headers.get("cookie", "")

    async def notice(self, message):
        log.info(message)
        if self.on_notice:
            await self.on_notice(message)

    async def receive_typing(self, data):
        username = data.get('username') if isinstance(data, dict) else None
        if not isinstance(username, str) or not username or len(username) > 128:
            return
        if username == self.user.get('username') or re.search(r'[\x00-\x1f\x7f]', username):
            return
        now = time.monotonic()
        previous = self.typing_received.get(username)
        if previous is not None and now - previous < 3:
            return
        self.typing_received[username] = now
        self.typing_received.move_to_end(username)
        while len(self.typing_received) > 500:
            self.typing_received.popitem(last=False)
        if self.on_typing:
            await self.on_typing(username)

    async def send_typing(self, status):
        # O chat do site recebe somente pulsos, sem estados paused/done.
        if status != 'active' or not self.connected.is_set() or not self.sio or not self.sio.connected:
            return
        async with self.typing_lock:
            now = time.monotonic()
            if self.typing_sent_at is not None and now - self.typing_sent_at < 3:
                return
            self.typing_sent_at = now
            try:
                await self.sio.emit('client event', {'channel': self.channel,
                    'event': 'client-typing', 'data': {'username': self.user['username']}})
            except Exception as exc:
                log.debug('Falha no indicador de digitação: %s', type(exc).__name__)

    async def websocket(self):
        delay = 3
        while True:
            sio = socketio.AsyncClient(
                reconnection=False, logger=False, engineio_logger=False
            )
            self.sio = sio

            @sio.on("connect")
            async def connect():
                # O Laravel Echo envia os headers no auth do evento subscribe.
                self.private_channel = f"private-chatter.{self.user['id']}"
                for channel in (self.channel, self.private_channel):
                    await sio.emit(
                        "subscribe",
                        {
                            "channel": channel,
                            "auth": {
                                "headers": {
                                    **self.csrf_headers,
                                    "Cookie": self.cookie_header(),
                                }
                            },
                        },
                    )

            def payload(args):
                if args and isinstance(args[0], str):
                    if args[0] != self.channel:
                        return None
                    args = args[1:]
                return args[-1] if args else None

            @sio.on("presence:subscribed")
            async def subscribed(*args):
                rows = payload(args)
                if not isinstance(rows, list):
                    return
                self.members = {
                    int(r["user_info"]["id"]): r["user_info"]
                    for r in rows
                    if isinstance(r, dict) and r.get("user_info", {}).get("id")
                }
                self.connected.set()
                if self.on_presence:
                    await self.on_presence()
                await self.notice(
                    f"Chat conectado em tempo real ({len(self.members)} usuários)."
                )

            @sio.on("presence:joining")
            async def joining(*args):
                row = payload(args)
                info = row.get("user_info", {}) if isinstance(row, dict) else {}
                if info.get("id"):
                    self.members[int(info["id"])] = info
                    if self.on_presence:
                        await self.on_presence()

            @sio.on("presence:leaving")
            async def leaving(*args):
                row = payload(args)
                if isinstance(row, dict):
                    uid = row.get("user_id") or row.get("user_info", {}).get("id")
                    if uid:
                        self.members.pop(int(uid), None)
                        if self.on_presence:
                            await self.on_presence()

            @sio.on("new.message")
            async def message(*args):
                row = payload(args)
                if isinstance(row, dict):
                    await self.ingest(row.get("message", row))

            @sio.on("delete.message")
            async def deleted(*args):
                row = payload(args)
                msg = row.get("message", {}) if isinstance(row, dict) else {}
                if msg.get("id"):
                    self.history.pop(int(msg["id"]), None)
                    await self.notice(f"Mensagem #{msg['id']} removida no site.")
                    # O site desafixa a mensagem apagada sem mandar pin.message.
                    if self.pinned and str(self.pinned["id"]) == str(msg["id"]):
                        await self.set_pin(None)

            @sio.on("pin.message")
            async def pin(*args):
                row = payload(args)
                if isinstance(row, dict):
                    await self.set_pin(row.get("pinned"))

            @sio.on("reaction.message")
            async def reaction(*args):
                row = payload(args)
                if isinstance(row, dict):
                    await self.update_reactions(row.get("message", row))

            # Mensagens privadas chegam no canal do usuário, com o tipo no payload.
            @sio.on("App\\Events\\Chatter")
            async def chatter(*args):
                if not args or args[0] != self.private_channel:
                    return
                row = args[-1] if isinstance(args[-1], dict) else {}
                msg = row.get("message") if isinstance(row.get("message"), dict) else {}
                if row.get("type") == "new.message" and self.is_private(msg):
                    await self.ingest(msg)
                elif row.get("type") == "delete.message" and msg.get("id"):
                    if self.private_history.pop(int(msg["id"]), None) is not None:
                        await self.notice(f"Mensagem privada #{msg['id']} removida no site.")

            @sio.on('client-typing')
            async def typing(*args):
                await self.receive_typing(payload(args))

            @sio.on("subscription_error")
            async def subscription_error(*args):
                if args and args[0] == self.private_channel:
                    await self.notice("Canal privado recusado; DMs só pela atualização HTTP.")
                    return
                await self.notice("Assinatura do chat recusada; renovando sessão.")
                await sio.disconnect()

            @sio.on("disconnect")
            async def disconnect():
                self.connected.clear()

            try:
                await self.refresh()
                await sio.connect(
                    self.ws_host,
                    transports=["websocket"],
                    headers={"Cookie": self.cookie_header(), "Origin": self.base_url},
                    socketio_path="socket.io",
                )
                await asyncio.wait_for(self.connected.wait(), 20)
                delay = 3
                await self.reconcile()
                await self.reconcile_private()
                # Fixações feitas enquanto o socket estava fora.
                await self.load_room()
                await sio.wait()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.notice(
                    f"Chat indisponível ({type(exc).__name__}); nova tentativa em {delay}s."
                )
            finally:
                self.connected.clear()
                self.typing_received.clear()
                self.typing_sent_at = None
                if sio.connected:
                    await sio.disconnect()
                session = sio.eio.http
                if session and not session.closed:
                    await session.close()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def poll(self):
        while True:
            await asyncio.sleep(30)
            try:
                await self.reconcile()
                await self.reconcile_private()
            except Exception as exc:
                await self.notice(
                    f"Falha na atualização HTTP ({type(exc).__name__}); verifique os cookies."
                )

    async def close(self):
        await self.http.aclose()
